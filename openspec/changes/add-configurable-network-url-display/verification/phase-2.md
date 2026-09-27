# Phase 2 Verification Evidence

## 2026-09-27 (corrected streaming line accounting, tasks 2.1–2.11)

The earlier revision in this file described **atomic whole-line buffering**
(the `line_limit_bytes=None` bypass removed and every source line held until
newline before neutralization). The revised change contract rejected that
behavior: task 2.8/2.9 require prompt committed-prefix delivery without
waiting for newline, an *appended* (not replacing) oversized marker, and
suffix discard through newline or stream termination. This section records the
corrective implementation and replaces the superseded atomic claims.

### One streaming decoder / line-accounting / neutralizer pipeline

`RedactingStream` (default collector path and `redact_tail`) and
`NpmDiagnosticStream` (structured path used with `make_stream_factory`) share
the same bounded `TerminalSafeLineFramer`, which composes the shared
`_Utf8ByteDecoder` and `TerminalControlNeutralizer`. The fixed order is:

```text
incremental UTF-8 decode → streaming source-line byte accounting →
terminal-control neutralization → secret/URL projection (or secret redaction)
```

- `TerminalSafeLineFramer` holds a persistent `TerminalControlNeutralizer` for
  the current line and feeds decoded characters in batches. Committed
  terminal-safe prefixes are returned immediately; a line is never atomically
  buffered before delivery.
- The 64 KiB limit is measured on decoded **source** bytes before `\xNN`
  expansion. The neutralizer's independent 8 KiB control-sequence bound is
  unchanged.
- A raw newline always ends the source line even inside an open control
  string. At that boundary the pending control state is finalized (an
  unterminated sequence becomes `[INCOMPLETE_CONTROL_SEQUENCE]`), the newline
  is emitted, and a fresh neutralizer starts the next line so control payloads
  cannot merge lines.
- When the current line first exceeds the bound, exactly one
  `[sanitized oversized diagnostic]` marker is appended after the already
  emitted prefix. `TerminalControlNeutralizer.discard_pending()` drops any
  pending control sequence so ambiguous control content cannot escape. The
  remaining source content is discarded through the next newline or stream
  termination; a later newline resumes normal processing and stream
  termination never appends a second marker.
- The default path redacts the neutralized prefix promptly. The structured
  path feeds the neutralized prefix to the URL/secret projector promptly and
  releases the projected text as a non-finalized committed prefix as soon as
  the decoder, neutralizer, and projector commit it, before any newline; the
  retained tail is appended from those committed prefixes immediately. Each
  retained prefix is also accumulated for the current logical line so that, at
  the record boundary, a finalized line still carries the complete URL-free
  text plus its normalized hostnames and ordered fingerprints. Both paths
  enforce the same source-line bound and emit the same marker semantics.
- `StreamChunk` now distinguishes the two collector events with a `finalized`
  flag: `finalized=False` is a committed safe prefix (empty structured
  metadata, never a complete npm diagnostic); `finalized=True` is a finalized
  logical line. A second `overflowed` flag marks the truncated line: prefixes
  released before the bound is crossed carry `overflowed=False`, while the
  fixed overflow-marker chunk and the finalized truncated line carry
  `overflowed=True`. The framer appends the fixed marker through a private
  `_OverflowMarker` `str` subclass, so a source line that merely contains the
  marker's characters is still ordinary text and is never misclassified as an
  overflow. Finalization never re-appends the line text, so a prefix retained
  once is not retained twice.

### Corrected tests

`tests/test_npm_environment_streaming.py` (111 tests):

- `TestIncrementalDecoder.test_two_byte_character_split_across_reads` and
  `test_eof_flushes_final_partial_without_newline` assert prompt prefix
  delivery instead of newline-buffered delivery.
- `TestRedactingStream.test_line_one_byte_over_appends_one_oversized_marker`,
  `test_oversized_line_drains_then_recovers_on_next_line`, and
  `test_unterminated_oversized_line_appends_one_marker_at_eof` assert the
  appended marker, immediate notification, suffix discard, recovery, and no
  duplicate marker at stream termination.
- `TestTerminalNeutralizationLineFraming` filters the structured path to
  finalized lines and asserts the committed prefix is delivered before the
  marker.
- `TestCollectStreams.test_concurrent_streams_reach_sink_before_exit`
  restores the established no-newline regression (`b"stdout-no-newline"`),
  and `test_structured_prefix_reaches_sink_before_newline` proves the
  structured path delivers a committed prefix to the sink before the record
  boundary.
- `TestCommittedPrefixStreaming` (strengthened) exercises the 2.8/2.9 matrix
  through both collector paths without a newline: prompt prefix delivery,
  prefix-plus-exactly-one-marker before the boundary, suffix discard, newline
  preservation and recovery, exactly-at-limit delivery, no second marker at
  stream termination, and overflow while a control string is pending. It no
  longer accepts a structured-path latency exception.
- `TestTerminalNeutralizationRouteAudit` now admits only `finalized` chunks to
  the presentation, proving complete-line grouping does not wait for or parse a
  committed prefix.

`tests/test_npm_diagnostic_collection_phase8.py` (41 tests):

- The `_collect` helper returns finalized lines only; `_collect_all` exposes
  the prefix channel for the new assertions.
- `TestCommittedPrefixDelivery` (new) feeds a safe prefix without newline into
  `NpmDiagnosticStream` and proves the committed prefix is observable before
  newline or `finish()`, the tail contains it immediately, and split UTF-8,
  fragmented terminal controls, secret prefixes, and URL candidates release
  only committed-safe content. It also proves finalization neither retains nor
  re-delivers a prefix twice.
- `TestCompleteLineConsumers` (new) proves a partial prefix creates no complete
  npm diagnostic, the research fetch fixture is exactly one finalized event
  with host/fingerprint metadata, and multi-line input yields one finalized
  event per record boundary with a tail that preserves occurrences.
- The three oversized-line cases now assert the committed prefix plus one
  appended marker and that the discarded suffix and its metadata never enter a
  live event or the retained tail.

### Production presentation route for committed prefixes

The corrective review found that the collector delivered prefixes but the
production `pi_assembly._structured_sink` discarded every `finalized=False`
chunk, so the operator still saw nothing until a newline. The production route
now carries committed prefixes to the authorized local presentation actor:

- `docker/versioning/host_progress.py` defines the internal
  `HostDiagnosticPrefix` event (`phase`, `step`, `stream`, `text`,
  `logical_resource`, `finalized`, `overflowed`). It is deliberately **not**
  part of `HostBuildEvent`, the external SDK event union, so an SDK callback
  can never observe a provisional prefix.
- `InternalDirectHostEventSink` gains `admit_prefix`, implemented by
  `HostEventEnqueueAdapter`. Prefixes are admitted best-effort on the bounded
  telemetry lane and never consume control capacity. A pending *non-finalized*
  prefix of the same stream is replaced in place, so redundant provisional
  updates cannot consume the bounded inbox or displace the finalized record
  boundary; a finalized overflow boundary is never superseded.
- `_structured_sink` keeps committed prefixes internal and distinguishes the
  overflowed finalized line by sink type. A non-finalized prefix routes only
  to the internal presentation actor as a `HostDiagnosticPrefix` and never to
  an external SDK callback. A finalized overflowed line routes to the internal
  prefix finalization (so the actor does not render the already-visible prefix
  and marker twice) or, for an external SDK sink, to a single bounded
  `HostStructuredDiagnostic` whose text is the committed URL-free prefix plus
  `[sanitized oversized diagnostic]`, classified as a neutral `status` with no
  hostname or URL-fingerprint metadata. Classification, npm parsing, identity,
  and grouping still consume only complete finalized lines; a truncated line
  is never classified as warning/retry/timeout/error or parsed as an npm fetch
  event.
- `HostPresentationState.admit_prefix` presents a provisional prefix without
  entering the coalescer: interactive mode replaces the mutable diagnostic
  slot promptly, and ``lines`` mode holds it pending until its record boundary
  so nothing incomplete becomes a durable record. An overflow marker is
  rendered durably at once in ``lines`` mode (or left in the slot in
  interactive mode), and the finalized overflow boundary finalizes the
  truncated line exactly once without re-appending the visible prefix.
  Provisional state is cleared when the complete line is admitted or at a
  step/phase terminal, and renderer failure discards it.

New regressions:

- `tests/test_host_presentation_phase9.py` (84 tests):
  `TestProvisionalPrefixPresentation` (interactive slot prompt and
  accumulation, no group or deadline from a prefix, complete-line replacement
  without a duplicate write, ``lines`` pending behavior, prompt overflow
  marker in both modes, overflow lines not coalesced, per-stream isolation,
  terminal discard) and `TestPrefixMailboxAdmission` (telemetry lane,
  in-place supersession, finalized overflow never superseded, dropped prefix
  is not a diagnostic omission).
- `tests/test_constructor_pi_assembly.py` (29 tests):
  `TestFacadeProvisionalPrefixRoute` exercises the real
  `NpmDiagnosticStream -> collect_streams -> pi_assembly._structured_sink ->
  host event sink -> presentation actor -> renderer` path. It proves a
  no-newline prefix becomes visible before the record boundary, the complete
  line is finalized once with a retained tail that holds it once, an external
  SDK callback receives exactly one finalized diagnostic and no provisional
  prefix, an oversized line reaches the external SDK as exactly one bounded
  safe truncation event only after its record boundary (committed safe prefix
  plus one marker, no discarded suffix or metadata, neutral `status`), EOF
  finalizes it once without a second marker, and the internal `lines` and
  `interactive` actor finalizes the provisional overflow exactly once without
  a duplicate structured diagnostic.
- `tests/test_npm_diagnostic_collection_phase8.py` (45 tests):
  `TestOverflowBoundaryTagging` and `TestOverflowFinalization` prove the
  overflowed finalized line is tagged, a source line that merely contains the
  marker text is not treated as an overflow, the discarded suffix and its
  metadata never reach the tail, and stream termination adds no second marker.

### Commands and results

```sh
ty check docker --python-version 3.14 --output-format concise
./scripts/check-types
python -m unittest \
  tests.test_npm_environment_streaming \
  tests.test_npm_diagnostic_collection_phase8 \
  tests.test_constructor_pi_assembly \
  tests.test_host_presentation_phase9 \
  tests.test_host_diagnostic_grouping_phase7 \
  tests.test_host_diagnostic_identity_phase7 \
  tests.test_host_diagnostic_projection \
  tests.test_host_failure_output_regression \
  tests.test_locked_assembly_observability_phase8 \
  tests.test_host_observability_acceptance_phase10 \
  tests.test_npm_environment_phase3_deadline \
  tests.test_npm_environment_cancellation \
  tests.test_npm_environment_output_validation \
  tests.test_npm_environment_cleanup \
  tests.test_npm_environment_publication_cleanup \
  tests.test_output_configuration \
  tests.test_npm_environment_publication \
  tests.test_npm_environment_phase5_introspection
git diff --check
```

Results:

- `ty check docker --python-version 3.14 --output-format concise`: All checks
  passed.
- `./scripts/check-types` (`docker` + `tests/typing`): All checks passed.
- Focused suite: `Ran 581 tests in 13.185s` — `OK`.
- `git diff --check`: clean (exit 0).

Full discovery (`python -m unittest discover -s tests -p 'test_*.py'`):
`Ran 4113 tests in 50.838s` — `FAILED (failures=1, skipped=13)`.

The only failure is the pre-existing, unrelated
`test_npm_environment_phase6_introspection.TestScenarioCoverage.test_every_spec_scenario_is_mapped_to_a_test`:
the canonical `openspec/specs/locked-npm-environment-assembly/spec.md` lists
54 scenarios while `tests/data/npm_environment_scenario_coverage.json` maps
36. Phase 2 keeps exactly one directly relevant entry for "Arbitrarily long
diagnostic has no newline" (backed by the strengthened overflow and
committed-prefix tests in this change) and deliberately does not repair the
unrelated coverage gap.

### Task status

Tasks 2.1–2.11 are complete. 2.10 and 2.11 were reopened twice by the
corrective review.

The first correction was required because the production `_structured_sink`
dropped every `finalized=False` chunk: the low-level collector delivered
prefixes, but the operator still saw nothing before a newline.

The second correction was required because that fix also suppressed every
`overflowed` finalized line from external SDK callbacks, so an SDK caller lost
the fixed safe truncation notification it used to receive. The corrected
routing keeps provisional prefixes internal, finalizes the internal
provisional display exactly once, and delivers one finalized bounded URL-free
truncation event (`<committed URL-free prefix>[sanitized oversized
diagnostic]`, neutral `status`, no discarded suffix or metadata) to an external
SDK sink. Both tasks are complete again only because the production
presentation route and SDK isolation are covered by the production-path
regressions above. The atomic whole-line buffering described in the superseded
revision of this file is no longer present in the implementation or the tests.

## 2026-09-26 (first revision — superseded)

First Phase 2 revision; superseded by the corrective streaming contract above.

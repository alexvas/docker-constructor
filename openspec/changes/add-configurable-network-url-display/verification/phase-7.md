# Phase 7 verification — presentation concurrency and failure isolation

Change: `add-configurable-network-url-display` (schema `spec-driven`).
Scope: tasks 7.1–7.12. This evidence file is separate from the specification
artifacts.

## Deliverables

### Collection-to-presentation envelope wiring

Phase 5 built the internal `HostDiagnosticEnvelope` and its policy-aware
grouping, and Phase 6 selected the single retained tail, but both explicitly
deferred the collection integration to Phase 7. This phase completes it:

- `docker/npm_environment/streaming.py` — `StreamChunk` gains three
  presentation-only, internal fields at the end (all defaulted, so positional
  construction is unchanged): `local_text` (the mode-selected local
  representation fragment), `fetch_key` (`FetchGroupKey | None`), and
  `fetch_text` (canonical latency-free fetch rendering).  On a committed
  prefix `local_text` is the newly committed selected fragment, and it may be
  `None` for a chunk that advances only the safe structured projection; fetch
  identity is always absent until the complete line finalizes.  None of the
  three is part of the external SDK DTO.
- `docker/versioning/npm_diagnostic_stream.py` — the collector feeds
  terminal-safe fragments to the incremental fetch recognizer and, at the
  record boundary, `_select_presentation` derives:
  - `redacted`: local text is the URL-free projection; a recognized fetch gets
    a redacted `FetchGroupKey` and a latency-free canonical text.
  - `host-path`: local text is the normalized hostname/safe-path rendering
    accumulated from the selected ``host-path`` projector; a recognized fetch
    gets a host/path key and canonical text built from that projector's
    sanitized `SafeHostPath` fact.
  - `exact`: local text is the terminal-safe source line; `fetch_key` is
    `None`, so every aggregation path stays bypassed.
  An overflowed (truncated) line is never recognized, so it stays an ordinary
  diagnostic with no fetch identity.  Committed prefixes use the same
  selection: `_emit_selected_prefix` shares the dedicated `host-path` selected
  projector with the retained tail and releases the terminal-safe source
  fragment for `exact`, so a partial `host-path`/`exact` line is never
  displayed redacted before its newline.  The safe projector still runs
  independently to build the URL-free finalized text, hostnames,
  fingerprints, and SDK-safe diagnostics, and a safe-only prefix chunk carries
  `local_text=None` so it is never displayed.
- `docker/versioning/host_progress.py` — `HostDiagnosticEnvelope` joins the
  internal `HostPresentationEvent` union (still excluded from
  `HostBuildEvent`), and `InternalDirectHostEventSink` gains
  `admit_diagnostic`.
- `docker/versioning/host_presentation.py` — `HostEventEnqueueAdapter`
  implements `admit_diagnostic` as bounded non-blocking admission;
  `_is_diagnostic` recognises the envelope; the worker dispatches it to
  `HostPresentationState.admit_diagnostic`. Mailbox admission mechanics,
  telemetry/control capacities, and the external DTO are unchanged.
- `docker/versioning/pi_assembly.py` — module-level, testable builders
  `structured_diagnostic_for` (URL-free, path-free DTO) and
  `presentation_envelope_for` (mode-selected local text plus optional fetch
  identity), plus `route_committed_prefix`, `route_finalized_diagnostic`,
  `route_overflow_diagnostic`, and `overflow_diagnostic_for`.  A finalized
  line is fanned out independently: the nominal internal actor (when present)
  receives the envelope, and the optional SDK sink (when present)
  *independently* receives only the safe DTO.  `_route_internal` and
  `_route_sdk` call them on their separate delivery paths (see "Split internal
  presentation from the SDK dispatcher").
  A recognized `host-path` fetch keeps the canonical hostname/path line and
  therefore never appends the legacy bracketed-hostname form.

### Mode-selected committed prefixes

A partial diagnostic must not change representation at its newline.  The
prefix route therefore releases the selected local fragment as soon as it is
committed:

- `NpmDiagnosticStream._emit_selected_prefix` releases the new selected
  fragment before the safe projection finalizes the record.  `redacted`
  selects the URL-free projection already released by `_emit_projected`;
  `host-path` reuses the dedicated selected projector that also feeds the one
  retained tail, so the live and retained representations never diverge;
  `exact` releases the terminal-safe neutralized source fragment immediately
  without waiting for the URL/secret projector.
- A selected fragment becomes a presentation-only `StreamChunk` with
  `text=""` and `local_text=<fragment>`; a chunk that only advances the safe
  structured projection keeps `local_text=None` and is never displayed.  No
  selected local content is ever placed in `text`, `hostnames`, or
  `url_fingerprints`, so an SDK-facing consumer cannot observe it.
- `pi_assembly.route_committed_prefix` admits the cumulative selected prefix
  to the internal actor only.  `_route_internal` appends `chunk.local_text`,
  ignores `local_text=None`, and offers the prefix to no SDK channel.  The
  overflow marker follows the selected committed prefix, and the discarded
  suffix never reaches live presentation, the retained tail, SDK events, or
  the finalized local text.
- The complete line still supersedes the provisional snapshot, so the visible
  prefix is never duplicated at finalization.

### Fail-closed abort finalization

A line finalized by reader failure or cancellation must not resolve a token
that the streaming sanitizer withheld.  The `finish(abort=...)` policy is
applied by every projector:

- `NpmDiagnosticStream.finish` forwards its `abort` flag to
  `self._projector.finish(abort=abort)` and to the selected ``host-path``
  projector's `finish(abort=abort)`.  The finalized `host-path` local text is
  the output that projector already produced incrementally, so the live text
  and the retained tail share exactly one fail-closed policy.
- For `hello https://example.com/path` finalized with `abort=True`, the
  finalized `local_text` and the retained tail are both
  `hello [sanitized incomplete token]`, and `example.com/path` never appears
  in the live text, the retained tail, or the structured `text`.  The same
  holds for ambiguous scheme prefixes such as `hello htt`.  A clean EOF still
  resolves a complete URL normally.

### Record newline versus EOF finalization

A record newline is not EOF: a trailing partial secret or scheme prefix before
it is ordinary text.  Because the selected ``host-path`` projector is fed the
actual streaming source segments, it sees the terminating newline and resolves
every pending token at its real boundary; the former complete-source reprojection
(`_project_host_path`) is gone.  The record boundary is handled as follows:

- `_emit_projected` passes `newline_terminated=True` when it consumes the
  record newline; `finish()` leaves it `False`.
- `_selected_local_text(newline_terminated=True)` removes exactly the one
  final newline from the accumulated selected output -- no other whitespace is
  touched -- so the finalized live text matches the live prefixes and the
  retained tail.  For an unterminated line the selected projector's
  `finish(abort=abort)` result is used unchanged.
- Live and retained representations therefore agree for newline-terminated
  ambiguous tokens: `hello sec\n` gives `hello sec`,
  `hello git:\n` gives `hello git:`, and `hello https://\n` gives the same
  sanitized representation on both channels.  Clean-EOF and aborted-EOF
  behavior is unchanged: `hello sec` fails closed at either EOF policy, a
  clean EOF resolves a complete URL, and an aborted EOF keeps it fail-closed.

### Canonical fetch rendering secret safety

`canonical_fetch_text` sanitizes only the URL-derived host/path with its
`secrets` parameter.  The fixed `npm http fetch` prefix, method, exact status,
and the optional `attempt #N` and `(cache …)` clauses are copied from the
source line, so a configured secret in any of them was redacted from the
selected diagnostic but restored by the canonical rendering and grouped under
it (`secret topsecret` + `(cache topsecret)` redacted `local_text` yet rendered
`(cache topsecret)` in `fetch_text`).  Fetch selection now proves the
canonical form cannot restore a secret before assigning the identity:

- `npm_fetch.fetch_source_fields_text(record)` returns the canonical text of
  the source-derived, non-URL fields (prefix, method, status, and the optional
  attempt and cache clauses).  The URL-derived portion is excluded because the
  shared projector already sanitizes it.
- `npm_fetch.fetch_source_fields_text(record)` returns the canonical text of
  the source-derived, non-URL fields (prefix, method, status, and the optional
  attempt and cache clauses).  The URL-derived portion is excluded because the
  shared projector already sanitizes it.  The streaming recognizer feeds those
  fields through `npm_fetch._SecretFieldMatcher` incrementally, so the check no
  longer reconstructs them from a retained source line; the pure helper stays
  for isolated callers.
- `NpmDiagnosticStream._select_presentation` consults the recognizer's
  `canonical_unsafe` flag.  When a configured secret occurs in a preserved
  field it keeps the already-sanitized ordinary diagnostic and returns
  `fetch_key=None, fetch_text=None`, so the line is never grouped and no
  canonical rendering is presented or delivered to the SDK.
- The guard covers every preserved field and case-sensitive substring: a
  secret in the method (`GET`), an exact or partial status (`200`, `20`), an
  attempt clause (`#3`), a cache outcome (`topsecret`, with `sec` as a
  substring), or one spanning two adjacent fields (`GET 200`).  An unrelated
  secret leaves ordinary aggregation intact.

### Incremental fetch recognition without a full source buffer

`NpmDiagnosticStream` retained a second, terminal-safe copy of every line in
`_source_line_chars` purely so the finished line could be handed to
`parse_npm_fetch_line` and, for `host-path`, reprojected.  That made a
secret-bearing full source line live on for the whole record even though the
selected representation was already safe.  Bounded source retention is still
source retention, so the buffer and the reprojection are gone:

- The complete-source buffer, every append to it, its join, and its resets are
  removed.  `redacted` and `host-path` never keep a terminal-safe source line.
- A single mode-selected accumulator keeps only the *selected* safe output:
  `redacted` reuses the URL-free projection it already carries as `text`,
  `host-path` accumulates the ``host-path`` projector's sanitized host/path
  rendering, and `exact` accumulates the terminal-safe source it selected.
  Only `exact` may retain terminal-safe source, because that source is its
  selected representation.
- `_project_host_path(source, …)` is removed.  The finalized `host-path` text
  is the accumulated output the selected projector already produced
  incrementally; exactly the record newline is stripped for a terminated line,
  and an unterminated line keeps the selected projector's
  `finish(abort=abort)` policy.  Newline, clean-EOF, and aborted-EOF behavior is
  unchanged, and the finalized text still matches the live prefixes and the
  retained tail because all three come from the same projector output.
- `NpmFetchRecognizer` replaces the complete-source `parse_npm_fetch_line`
  call.  It consumes terminal-safe fragments and validates the grammar
  incrementally, retaining only the grammar position, the method (at most 16
  characters), the three-digit status, bounded significant-digit latency and
  attempt state, the cache outcome, and malformed/overflowed/incomplete flags.
  The URL authority is parsed without ever holding the raw authority: user
  information is consumed and discarded the moment an `@` is seen, a
  candidate that can no longer be a valid `host[:port]` is dropped
  immediately, and only the user-information-free host candidate is kept until
  the authority delimiter.  It is then normalized through
  `normalized_authority_host`/`literal_authority_host` and discarded.  There is
  no authority-length grammar limit; the authoritative bounds are the 64 KiB
  diagnostic line, 8 KiB pending sanitizer state, and 18 significant
  latency/attempt digits.  The recognizer matches the complete-line parser for
  every authority whose user information does not itself percent-decode to a
  structural byte or NFKC-expand to a structural delimiter, and otherwise
  fails closed so it never admits a line the complete-line parser rejects.
- The recognizer returns a parser-minimal `SafeFetchRecord` (method, status,
  optional attempt, optional cache outcome) that carries no URL.  `host-path`
  grouping attaches the bounded, already-sanitized `SafeHostPath` fact the
  selected projector produced; `fetch_group_identity` and
  `canonical_fetch_text` accept either record and never re-derive the URL from
  source.  The pure `NpmFetchRecord`/`parse_npm_fetch_line` API remains for
  isolated callers and tests.
- The recognizer resets at every record boundary: newline, empty line,
  overflow, EOF finalization, and abort.  Once a line overflows or becomes
  grammatically invalid it stops retaining token content and only waits for the
  next boundary.  An oversized or malformed line therefore stays an ordinary
  diagnostic with no group identity, and parser state never leaks into the
  following record.

### Independent SDK fan-out

A single `event_sink` previously had to be either the facade presentation inbox
or the external SDK callback.  That made SDK diagnostics disappear as soon as a
presentation session owned live output.  The fix separates the two channels:

- `PiAssemblyRequest` and `BuildRequest` gain an optional `sdk_event_sink`,
  guarded independently of `event_sink` and threaded through
  `_materialize_pi_for_build` to both the injected materializer and the
  production `PiAssemblyRequest`.
- `materialize_pi` derives `internal_sink` (the `event_sink` only when it is an
  `InternalDirectHostEventSink`) and an independent `sdk_sink` (an explicit
  `sdk_event_sink`, falling back to a plain non-internal `event_sink` for
  backward compatibility).  It never infers that the SDK sink is absent merely
  because `event_sink` is the internal actor.
- `route_finalized_diagnostic` always admits the envelope to `internal_sink`
  when present (inside its own `try`/`except`), then independently emits the
  safe DTO to `sdk_sink` through the guarded `emit` boundary.  An internal
  admission failure cannot suppress SDK delivery, and an SDK callback failure
  cannot affect internal presentation or assembly.
- `route_overflow_diagnostic` finalizes the internal overflow boundary and
  independently emits exactly one bounded safe truncation diagnostic to the
  SDK sink.  Provisional prefixes are still never routed to the SDK.
- Presentation-only sessions keep direct reader-thread admission.  The two
  channels are kept physically separate (see the next subsection), so an SDK
  callback can never run on a reader thread and an SDK backlog can never delay
  an internal admission.

### Split internal presentation from the SDK dispatcher

A combined internal + SDK configuration previously sent both channels through
one `_structured_sink` behind a single lossy `SinkDispatcher`.  Queue
saturation could then drop finalized chunks while still accepting later
provisional prefixes: the per-stream prefix buffer was never reset at a
dropped record boundary, so the next prefix snapshot concatenated two
unrelated records and could grow past the line bound.

- `SplitDiagnosticSink` (in `npm_environment.streaming`) names the two paths
  explicitly.  `direct`, when present, is invoked on the reader thread for
  every chunk and admits into the bounded, non-blocking presentation mailbox;
  only `finalized` chunks are *also* submitted to the one dispatcher that owns
  `dispatched`, so provisional prefix traffic never enters the lossy queue.
- `pi_assembly` no longer has a combined `_structured_sink`.  `_route_internal`
  performs the prefix accumulation and internal envelope admission directly;
  `_route_sdk` emits only the finalized, safe `HostStructuredDiagnostic` and
  runs only on the dispatcher thread.  An SDK-only configuration uses
  `SplitDiagnosticSink(None, _route_sdk)`, so prefixes never consume SDK queue
  capacity there either.
- Prefix accumulation stays before any lossy queue.  The per-stream buffer is
  reset at every finalized record, including an overflow record, before the
  envelope is admitted, and each admitted snapshot is cumulative, so mailbox
  supersession replaces the provisional rendering instead of combining lines.
- Lifecycle is unchanged: the SDK dispatcher is still drained and finished
  inside `collect_streams` -- before `assemble_environment` returns and before
  the operation's terminal phase event is admitted -- with the existing
  callback budgets, one truncation notice, failure isolation, and cleanup
  preserved.

### Failure isolation

Renderer failure and bounded completion are already isolated by
`HostPresentationState._safe` (a renderer exception disables rendering without
raising) and the session's single `WORKER_JOIN_SECONDS` completion budget.
Phase 7 adds the mode-parameterised coverage proving the primary collection
result, the selected retained tail, the external DTO, and domain cleanup stay
independent of presentation failure.

## RED evidence

Nine runtime-only captures isolate final-line wiring, independent fan-out,
separated internal and SDK delivery under SDK backlog, mode-selected committed
prefixes, fail-closed finalization (abort and record boundary), canonical fetch
secret safety, incremental fetch recognition, and raw authority retention.
Nothing on disk is modified by any capture.

### Collection wiring

Reverts only `NpmDiagnosticStream._select_presentation` to return the projected
text with no fetch identity.  Routing stays the real fan-out.

```
python3 -B openspec/changes/add-configurable-network-url-display/verification/phase7_wiring_red.py
# Ran 42 tests
# RED SUMMARY (wiring): failures= 15 errors= 0
```

The 15 failures are behavioural, not import errors:

- 7.4 real-output matrix (7): `redacted` lines do not aggregate and carry no
  fetch key; `host-path` does not aggregate by host/path or bypass the legacy
  bracketed hostname; `exact` does not carry terminal-safe source text; and the
  envelope does not carry the mode-selected local text (3 subtests).
- finalized-line fan-out (1): `test_both_sinks_receive_independent_events`
  loses the presentation-only envelope identity.
- 7.5 high-volume acceptance (3): interactive `redacted` never reaches
  `— N requests`, the `lines` window summary is absent, and same-resource
  `host-path` aggregation/separation is absent.
- 7.3 close/drain (2): a pending fetch group does not finalize with its count
  before completion, and the actor no longer reuses fetch identity across
  step terminals.
- finalized prefix replacement (2): with the finalized local text reverted to
  the safe projection, the `host-path` and `exact` complete lines no longer
  carry their selected representation.

### Independent fan-out

Reverts only the `route_finalized_diagnostic`/`route_overflow_diagnostic`
routing to the previous exclusive behaviour: when a nominal internal actor
exists, the SDK channel is suppressed, and overflow is mutually exclusive.
The collection wiring stays real.

```
python3 -B openspec/changes/add-configurable-network-url-display/verification/phase7_fanout_red.py
# Ran 7 tests
# RED SUMMARY (fan-out): failures= 4 errors= 0
```

The 4 failures are behavioural and directly target the helper-level fan-out:

- `test_both_sinks_receive_independent_events` — the SDK list stays empty while
  the internal actor is fed.
- `test_internal_admission_failure_still_delivers_the_sdk_event` — the early
  return suppresses the independent SDK delivery.
- `test_mailbox_saturation_does_not_suppress_sdk_delivery` — a saturated inbox
  drops the envelope and the SDK event is lost too.
- `test_overflow_fans_out_to_both_channels` — the SDK receives no truncation
  diagnostic.

The end-to-end `TestFacadeProvisionalPrefixRoute` tests are no longer part of
this capture: after the split (next subsection) the production internal and SDK
pathways are physically separate, so the SDK diagnostic can no longer be
suppressed by the exclusive helper revert.

### Mode-selected committed prefixes

Reverts only the prefix release to the previous behaviour: `host-path` and
`exact` fed the safe (redacted) projection to the live prefix and released no
selected local fragment, so a partial line rendered redacted until its
newline.  Routing and final-line selection stay real.

```
python3 -B openspec/changes/add-configurable-network-url-display/verification/phase7_prefix_red.py
# Ran 24 tests
# RED SUMMARY (prefix): failures= 16 errors= 0
```

The 16 failures are behavioural and target the prefix defect in every layer:

- collector (`TestCommittedPrefixDisplayModes`): `host-path` and `exact`
  committed prefixes do not carry the selected representation or match the
  selected tail, the fragmented-URL matrix loses host-path/exact, and the
  selected-prefix chunks no longer keep the structured text empty (6).
- routing (`TestCommittedPrefixRouting`): the `host-path` and `exact` prefix
  snapshots are redacted before the record boundary and finalization
  duplicates the prefix (4).
- assembly (`TestFacadeProvisionalPrefixRoute`): the production route no
  longer shows the selected prefix before the newline, the overflow
  selected-prefix/marker ordering is redacted, and the finalized line
  duplicates the provisional prefix, for `host-path` and `exact` (6).

Representative assertion:

```
AssertionError: unexpectedly None : ... assertIsNotNone(chunk.fetch_key)
AssertionError: 1 != 0 : ... assertEqual(1, len(sdk_events))
AssertionError: 'registry.example.com/pkg/-/pkg-1.0.0.tgz' not found in
  'npm error network GET <redacted> failed'
```

### Fail-closed abort finalization

Reverts only the abort policy of the selected ``host-path`` projector to the
previous clean EOF (``projector.finish()`` instead of
``projector.finish(abort=abort)``) while keeping the record-newline handling,
so a line finalized at reader failure or cancellation resolves a trailing URL
token the streaming sanitizer withheld.  Every other route stays real.  Runs
the abort-finalization regressions plus the collector abort and
presentation-isolation classes.

```
python3 -B openspec/changes/add-configurable-network-url-display/verification/phase8_abort_finalization_red.py
# Ran 18 tests
# RED SUMMARY (abort finalization): failures= 9 errors= 0
```

The 9 failures are behavioural and directly target the divergence:

- `test_abort_keeps_live_and_retained_fail_closed` — the finalized live
  `local_text` exposes `example.com/path` while the retained tail kept
  `[sanitized incomplete token]`.
- `test_abort_covers_ambiguous_scheme_prefixes` (2 subtests for `hello htt`
  and `hello https`) — the clean-EOF policy resolved or flushed the prefix
  instead of failing closed.
- `TestCollectionAbortFinalization`
  (`test_sibling_reader_failure_host_path_fails_closed`,
  `test_interruption_host_path_fails_closed`,
  `test_sibling_reader_failure_fails_closed`,
  `test_interruption_fails_closed`) — the real collector finalized the
  reader-failure and interruption lines open.
- `TestPresentationIsolation.test_reader_failure_host_path_stays_fail_closed_on_both_channels`
  — the internal envelope exposed `example.com/path` after a reader failure.
- `test_caller_abort_signal_reaches_both_readers` — the abort signal no longer
  reached both readers fail-closed.

### Record newline versus EOF finalization

Reverts only the record-boundary policy of the selected ``host-path``
projector: the record newline is dropped before the selected projector sees
it, so a newline-terminated line is finalized as if it ended at EOF.  A
newline-terminated partial secret or scheme prefix then fails closed in the
finalized live text while a real record boundary should have resolved it as
ordinary text.

```
python3 -B openspec/changes/add-configurable-network-url-display/verification/phase8_record_boundary_red.py
# Ran 9 tests
# RED SUMMARY (record boundary): failures= 4 errors= 0
```

The 4 failures are behavioural and exactly the newline-terminated cases; the
abort and clean-EOF cases keep passing, so the capture isolates the record
boundary and proves the abort fix is preserved:

- `test_newline_terminated_partial_secret_is_ordinary_text` — `hello sec\n`
  corrects to `hello sec` on both channels but the EOF policy rendered
  `hello [sanitized incomplete token]`.
- `test_newline_terminated_scheme_prefix_is_ordinary_text` — `hello git:\n`
  rendered the incomplete-token marker instead of `hello git:`.
- `test_newline_terminated_bare_url_prefix_matches_retained` —
  `hello https://\n` produced a different representation on the live and
  retained channels.

### Canonical fetch rendering secret safety

Reverts only the incremental secret guard by making the recognizer's
`_SecretFieldMatcher.feed` a no-op, so the `canonical_unsafe` flag is never
set.  An unchecked canonical rendering whose method, status, attempt, or cache
clause came from the source then reaches `fetch_text`, grouping, presentation,
and the SDK.  Everything else stays real.

```
python3 -B openspec/changes/add-configurable-network-url-display/verification/phase8_canonical_secret_red.py
# Ran 8 tests
# RED SUMMARY (canonical secret): failures= 20 errors= 0
```

The 20 failures are behavioural and cover every preserved field, both display
modes, and both renderers:

- collector (`TestFetchCanonicalizationSecretSafety`): the cache token
  (`miss`, `topsecret`), method (`GET`), status (`200`, `20`), attempt (`#3`),
  substring (`sec` in `topsecret`), and a secret spanning two source fields
  (`GET 200`).  Without the guard `fetch_key`/`fetch_text` are populated and
  the secret reappears in `local_text` and the retained tail.
- presentation
  (`TestFetchCanonicalizationSecretSafety.test_interactive_and_lines_renderers_never_restore_the_secret`):
  the interactive and `lines` renderers, the SDK DTO, and the retained tail
  restore the secret in both `redacted` and `host-path` modes.

The unrelated-secret control passes under the revert, so the failures are
specific to secret-bearing canonical fields.

### Incremental fetch recognition

Reinstalls the retired retention behaviour without touching the sources:
the collector keeps a complete terminal-safe `_source_line_chars` buffer and
`host-path` finalization reprojects it with a second projector, and the
recognizer retains the complete fed source text.  Everything else stays real.

```
python3 -B openspec/changes/add-configurable-network-url-display/verification/phase8_incremental_fetch_red.py
# Ran 13 tests
# RED SUMMARY (incremental fetch): failures= 7 errors= 0
```

The seven failures are behavioural and target exactly the removed retention:

- `test_no_complete_source_buffer_is_retained` (both modes): the collector owns
  `_source_line_chars`, so `redacted`/`host-path` hold a full source line.
- `test_parser_state_never_retains_credentials_query_or_fragment` (every chunk
  size, now inspected after *every* fragment): the recognizer state contains
  the raw URL, credentials, query, and fragment text.

The fail-closed, long-authority, fragmented-grouping, host-path agreement,
overflow, and malformed-URL tests keep passing under the revert, so the capture
isolates the retention defect rather than an unrelated behaviour.

### Raw authority buffer

Reinstalls only the retired raw authority retention: every authority character
(including the `user:password@` user-information prefix) is appended to a
`_legacy_authority` list, so a credential-bearing authority substring survives
between `feed()` calls again.  Everything else stays real.

```
python3 -B openspec/changes/add-configurable-network-url-display/verification/phase8_authority_buffer_red.py
# Ran 2 tests
# RED SUMMARY (authority buffer): failures= 6 errors= 0
```

The six failures are the per-fragment retention regressions: the collector and
parser state contain `credname`, `credsecret`, `credname:credsecret`, and the
complete authority/URL at intermediate chunk boundaries.  The tests inspect the
retained state after every fragment, and container items are concatenated
without a separator, so a raw buffer stored as a `list[str]` of characters is
reconstructed rather than hidden.

### Combined internal + SDK dispatching

Reinstates only the pre-fix delivery model: `SplitDiagnosticSink` is replaced
by a legacy class whose `direct` is `None` and whose `dispatched` target calls
both the internal and the SDK handlers, and whose `should_dispatch` accepts
every chunk.  Internal and SDK delivery therefore share one lossy
`SinkDispatcher` and provisional prefixes enter the queue.  The collector, the
presentation actor, the SDK routing, and the callback budgets stay real.

```
python3 -B openspec/changes/add-configurable-network-url-display/verification/phase8_combined_sink_red.py
# Ran 1 test
# RED SUMMARY (combined sink): failures= 1 errors= 0
```

A one-slot SDK queue plus a callback blocked on the first finalized record
delays (then drops) the queued internal prefixes, so the internal renderer
never sees a later record's provisional snapshot.  The regression test fails
on `set(prompt) == set(markers[1:])`, proving the capture isolates the
combined-dispatch defect instead of an unrelated behaviour.

## GREEN and INTROSPECT results

`tests/test_presentation_wiring_phase7.py` drives the production collector
through the production envelope builder into the single presentation actor, and
independently into the SDK channel.  42 tests pass:

```
python3 -B -m unittest tests.test_presentation_wiring_phase7
# Ran 42 tests in 5.9s — OK
```

Task mapping:

- 7.1 — `TestModeSaturationOrdering`: for every mode, telemetry saturation
  fills exactly `TELEMETRY_CAPACITY` admissions, the next diagnostic is
  recorded as a bounded omission, control capacity stays fully available, and
  accepted events keep their admission order; the omission counter is bounded.
- 7.2 — `TestRendererFailureIsolation`: a renderer exception permanently
  disables rendering (one attempt, no synchronous fallback, no retry on a
  later diagnostic); a blocked write stays within the shared five-second
  completion budget; and no renderer operation runs after successful
  completion. Mode-parameterised.
- 7.3 — `TestCloseDrainOrdering`: a pending fetch group finalizes with its
  count before completion; an unterminated line finalizes at stream
  `finish()`; the final report is written before any later diagnostic output;
  and the actor is reused across step terminals.
- 7.4 — `TestRealOutputIntegrationMatrix`: the cache-miss fixture line fed
  through decoding, collection, projection, parser, envelope identity, and
  presentation aggregates in `redacted` without URL or latency, aggregates by
  safe hostname/path in `host-path` (and separates other resources) without
  legacy bracketed-hostname attachment, and stays complete unaggregated
  terminal-safe source in `exact`, while the external SDK DTO stays URL-free
  and path-free in every mode.
- 7.5 — `TestHighVolumeAcceptance`: redacted interactive counts reach
  `— 400 requests`; redacted `lines` emits exactly one counted summary at the
  one-second window; host-path aggregates 150 same-resource fetches and
  separates a second resource; exact keeps 300 lines unaggregated; and exactly
  one byte-bounded selected retained tail is produced for 500 lines in every
  mode.
- 7.6–7.7 — `TestFinalizedDiagnosticRouting`: independent fan-out — with both
  sinks present the mailbox receives exactly one `HostDiagnosticEnvelope` and
  the SDK sink receives exactly one URL-free, path-free
  `HostStructuredDiagnostic`; internal-only fields (`fetch_key`, `fetch_text`,
  `presentation_text`) never leak into the DTO.  Isolation is asserted too: an
  exploding internal admission still delivers the SDK event, a throwing SDK
  callback still admits the envelope, and mailbox saturation does not suppress
  SDK delivery.  `test_overflow_fans_out_to_both_channels` covers the overflow
  boundary on both channels.  The end-to-end
  `test_constructor_pi_assembly.TestFacadeProvisionalPrefixRoute`
  `test_combined_sinks_fan_out_overflow_without_provisional_sdk_prefix` proves
  the production `_route_internal`/`_route_sdk` split finalizes the internal
  overflow boundary exactly once, emits exactly one safe truncation diagnostic
  to the SDK, and never routes a provisional prefix to the SDK.
  `test_constructor_build_orchestration.TestMaterializationBoundary`
  `test_sdk_sink_is_threaded_to_the_injected_pi_materializer` proves the facade
  request threads both sinks independently to the injected Pi materializer.
- 7.4/7.7 committed prefixes — `TestCommittedPrefixRouting` drives the
  collector and `route_committed_prefix` into a real actor and asserts the
  mode-selected prefix is presented before the newline, the SDK channel only
  ever sees the finalized URL-free DTO, the complete line replaces the
  provisional snapshot without duplication, and admission never waits for a
  consumer.  Collector-level coverage lives in
  `tests/test_npm_diagnostic_collection_phase8.TestCommittedPrefixDisplayModes`
  (URL-free/secret/terminal-control/partial-URL/partial-secret/split-UTF-8
  prefixes match the selected tail) and
  `tests/test_npm_environment_streaming` (the real `collect_streams` route
  through `make_stream_factory` in every mode); the production assembly route
  is covered by `TestFacadeProvisionalPrefixRoute`
  `test_mode_selected_prefix_is_visible_before_the_record_boundary`,
  `test_overflow_prefix_and_marker_use_the_selected_representation`, and
  `test_finalized_line_replaces_the_provisional_prefix_without_duplication`.
- 7.8 — covered by 7.2 and 7.3 plus the retained isolation below.
- 7.9 — covered by 7.5 plus `TestPresentationIsolation`.
- 7.10 — `TestPresentationIsolation.test_admission_never_waits_for_capacity`
  and `test_producer_callback_never_waits_for_the_consumer`: with no consumer,
  the producer admission path returns promptly and drops beyond capacity.
- 7.11 — `TestPresentationIsolation`: a failing renderer leaves the external
  DTO and the selected retained tail unchanged; saturation leaves the retained
  tail unchanged; a renderer failure during real `collect_streams` keeps the
  mode-selected tail; and a renderer failure during a reader failure keeps the
  reader failure primary with its retained tail attached.
- abort finalization — `TestAbortFailClosedHostPathFinalization` proves a
  `host-path` line finalized with `abort=True` keeps the live `local_text` and
  the retained tail identical and fail-closed for a trailing complete URL and
  for ambiguous scheme prefixes (`hello htt`, `hello https`, `hello https:`),
  while a clean EOF and a record newline still resolve a complete URL.
  `TestCollectionAbortFinalization` adds reader-failure and interruption
  coverage through the real `collect_streams`; `TestPresentationIsolation`
  adds the same for the internal envelope and the SDK diagnostic.
- record boundary — `TestHostPathRecordBoundaryFinalization` proves a
  newline-terminated `host-path` line finalizes to the same text the stream
  and the retained tail already rendered, minus only the terminator:
  `hello sec\n` (secret `secret`) keeps `hello sec`, `hello git:\n` keeps
  `hello git:`, and `hello https://\n` matches the retained representation.
  The same class keeps the distinct EOF behaviour: `hello sec` fails closed
  at clean and aborted EOF, a clean EOF resolves a complete URL, and an
  aborted EOF keeps it fail-closed.
- canonical fetch secret safety —
  `tests/test_npm_diagnostic_collection_phase8.TestFetchCanonicalizationSecretSafety`
  proves a configured secret in the method, status, attempt, cache answer, a
  substring of those, or spanning two of them makes the collector keep the
  sanitized ordinary diagnostic with `fetch_key=None` and `fetch_text=None`,
  so the secret never reappears in `text`, `local_text`, or the retained tail
  in either `redacted` or `host-path` mode; an unrelated secret still
  aggregates.  `tests/test_presentation_wiring_phase7.TestFetchCanonicalizationSecretSafety`
  drives the same line through the interactive and `lines` renderers and
  asserts no renderer text, SDK DTO, or retained tail restores the secret
  while the sanitized ordinary diagnostic is still shown.
- incremental fetch recognition —
  `tests/test_npm_diagnostic_collection_phase8.TestIncrementalFetchRecognition`
  proves the collector owns no `_source_line_chars` (or any full-source)
  buffer, a fragmented fetch line still groups in `redacted` and `host-path`,
  a long authority with no internal length limit still groups, the recognizer
  state never retains credentials, query, fragment, or raw URL (inspected
  after *every* fragment), a secret split across chunks or spanning two parser
  fields disables grouping, malformed and incomplete URLs stay ordinary
  diagnostics, an overflowed line never groups and discards parser state,
  `host-path` finalized text equals both the live prefixes and the retained
  tail minus its terminator, and the newline/clean-EOF/aborted-EOF policies
  are preserved.
  `tests/test_npm_fetch_phase4.TestIncrementalFetchRecognizer` proves the
  recognizer matches `parse_npm_fetch_line` for every accepted and rejected
  shape, is chunk-boundary independent, reproduces the source-field secret
  verdict, and groups identically from a URL-free `SafeFetchRecord`.
  `tests/test_npm_fetch_phase4.TestIncrementalFetchRecognizerEquivalence`
  proves the incremental recognizer is `parse_npm_fetch_line` equivalent over
  realistic userinfo/port/IPv4/IPv6/encoded/Unicode/`?`/`#` authorities, at
  every two-way split point (immediately before and after `@`, `:`, `[`, `]`,
  and the URL delimiters), and over long (>2 KiB) authorities across chunk
  boundaries, never over-accepts a generated malformed authority, and keeps no
  credential in state at any fragment (`_state_text` concatenates container
  items so a character-list authority buffer cannot hide).
- combined sink saturation —
  `tests/test_constructor_pi_assembly.TestFacadeProvisionalPrefixRoute`
  `test_combined_sink_saturation_keeps_internal_records_prompt_and_bounded`
  drives the production materializer with both sinks, a one-slot SDK queue, and
  a callback blocked on the first finalized record.  Internal prefixes still
  render before each later record's boundary while the SDK callback is blocked,
  no provisional snapshot ever contains two records' markers or exceeds its own
  line bound, every internal finalized record still arrives, and the SDK
  channel only ever receives bounded, safe, finalized
  `HostStructuredDiagnostic` events (with at least one dropped by the saturated
  queue).  The test also snapshots the delivered SDK count immediately after
  `collect_streams` returns and asserts it never changes afterwards, proving
  the dispatcher is drained (and its callbacks are finished) before the
  operation's terminal phase event.  `test_overflow_record_resets_the_internal_prefix_buffer` proves an
  overflow record is a real prefix boundary.  The unit-level
  `tests/test_npm_environment_streaming.TestSplitDiagnosticSink` proves a
  provisional prefix never enters the dispatcher queue, the direct channel
  always runs on the reader thread, the dispatched channel always runs on the
  dispatcher thread, and a saturated dispatcher never drops or delays a direct
  admission.

## VALIDATE

```
python3 -B -m unittest tests.test_presentation_wiring_phase7
# Ran 42 tests in 5.9s — OK

python3 -B -m unittest \
  tests.test_presentation_wiring_phase7 \
  tests.test_constructor_pi_assembly \
  tests.test_constructor_build_orchestration \
  tests.test_host_presentation_phase5 \
  tests.test_host_presentation_phase9 \
  tests.test_host_observability_acceptance_phase10 \
  tests.test_host_diagnostic_grouping_phase7 \
  tests.test_npm_diagnostic_collection_phase8 \
  tests.test_retained_tail_phase6 \
  tests.test_npm_fetch_phase4 \
  tests.test_npm_environment_streaming \
  tests.test_npm_environment_cancellation \
  tests.test_npm_environment_phase3_deadline \
  tests.test_npm_environment_cleanup \
  tests.test_host_failure_output_regression \
  tests.test_output_configuration \
  tests.test_constructor_project_root_phase7
# Ran 777 tests in 50.5s — OK

python3 -m unittest discover -s tests
# Ran 4450 tests in 84.4s — OK (skipped=13)

./scripts/check-types
# All checks passed!
```

## Out of scope

Phase 7 does not change the external `HostStructuredDiagnostic` contract,
mailbox admission policy, telemetry/control capacities, the retained-tail byte
bound, or the presentation worker model.  Documentation, example configuration,
and the cross-capability release matrix remain Phases 8 and 9 (tasks 8.1–9.8).

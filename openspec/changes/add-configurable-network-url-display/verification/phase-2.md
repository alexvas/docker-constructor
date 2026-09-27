# Phase 2 Verification Evidence

## 2026-09-27 (uniform bounded framing on both collector paths)

### One bounded pipeline for both stream implementations

`RedactingStream` (the default collector path used when no `stream_factory`
is supplied, and by `redact_tail`) and `NpmDiagnosticStream` (the structured
path used with `make_stream_factory`) now share the **same** bounded
`TerminalSafeLineFramer`. The fixed order is:

```text
incremental UTF-8 decode → 64 KiB source-line framing → terminal neutralization → secret/URL projection (or secret redaction)
```

- The `line_limit_bytes=None` bypass was removed from
  `TerminalSafeLineFramer`; the constructor accepts only a positive integer
  limit. There is no production caller that needs unbounded framing.
- `RedactingStream` constructs `TerminalSafeLineFramer()` with the default
  `DIAGNOSTIC_LINE_LIMIT_BYTES` (64 KiB).
- The 64 KiB limit is measured on decoded **source** bytes before `\xNN`
  expansion.
- A raw newline always ends the source line even inside an open control
  string, so control payloads cannot merge lines and pending control state is
  finalized at the boundary.
- A source line over the limit is replaced by exactly
  `OVERSIZED_DIAGNOSTIC_MARKER`, its content is drained through the next
  newline, and normal processing resumes afterward. An unterminated oversized
  final line emits only the marker at EOF.

### Immediate delivery

The default path no longer emits safe prefixes mid-line. That was the only
behavior that required unbounded framing, and emitting before the line is
complete is incompatible with replacing an oversized line by one fixed
marker. Every source line is retained only up to the 64 KiB bound and emitted
at its newline (or at EOF); a completed line is still delivered live, before
the pipe closes. No unbounded path was restored.

### Fail-closed truncated secret handling

With line framing, the text reaching `RedactingStream._feed_text` at EOF is a
whole finalized line (or a fixed marker), not just a small overlap. On an
aborted finalization, only the longest trailing suffix that is a proper
prefix of a known secret is replaced with `<redacted>`; already-safe content
and the fixed overflow/incomplete markers are emitted intact. The explicit
`readers`-failure redaction test still proves a split secret prefix
(`SUPERS…`) is redacted rather than flushed.

### Tests

Structured path (`make_stream_factory` → `NpmDiagnosticStream`):
- raw CSI, OSC, C0 controls, incomplete control strings, and oversized
  control strings are terminal-safe in every `NetworkUrlDisplay` mode;
- the 64 KiB line bound emits one marker and recovers on the next line;
- real interactive and lines `HostPresentationState` rendering;
- real text failure report and real JSON `host_failure.tail`.

Default path (no `stream_factory` → `RedactingStream`):
- `TestRedactingStream`: line exactly at the 64 KiB limit is emitted
  unchanged; one byte over emits exactly `OVERSIZED_DIAGNOSTIC_MARKER`; the
  discarded content appears in neither output nor tail; processing resumes
  after the next newline; an unterminated oversized line emits only the
  marker at EOF; secrets spanning a framed line boundary are matched once.
- `TestCollectStreams.test_default_path_oversized_line_marker_recovery_and_bounded_tail`:
  a real default `collect_streams` call delivers exactly one marker, drops
  the oversized content, delivers the following line, and keeps the retained
  tail bounded.
- `TestDefaultCollectorFailurePaths`: cancellation finalizes an incomplete
  control and an oversized line with only the fixed marker; reader failure
  after an incomplete control fails closed; reader failure after oversized
  input stays bounded; the sibling reader keeps draining and output stays
  terminal-safe.
- `TestTerminalNeutralizationRouteAudit`: default-path raw-control safety,
  oversized control + oversized line markers, text failure report in every
  display mode, and the real JSON failure tail, all with no `stream_factory`.
- `TestIncrementalDecoder` and `TestRedactingStream` adjusted for line
  boundaries (payload emitted at the newline or at EOF).

### Commands and results

```sh
./scripts/check-types
python -m unittest \
  tests.test_npm_environment_streaming \
  tests.test_npm_diagnostic_collection_phase8 \
  tests.test_host_failure_output_regression \
  tests.test_host_presentation_phase9 \
  tests.test_npm_environment_output_validation \
  tests.test_host_diagnostic_projection \
  tests.test_host_diagnostic_grouping_phase7 \
  tests.test_npm_environment_cancellation
python -m unittest \
  tests.test_locked_assembly_observability_phase8 \
  tests.test_constructor_pi_assembly \
  tests.test_npm_environment_streaming \
  tests.test_npm_diagnostic_collection_phase8 \
  tests.test_npm_environment_publication_cleanup \
  tests.test_host_diagnostic_projection \
  tests.test_output_configuration \
  tests.test_npm_environment_cleanup \
  tests.test_host_observability_acceptance_phase10 \
  tests.test_npm_environment_phase3_deadline
git diff --check
```

Results: typecheck passed; the focused suite (372 tests) and the
streaming-consumer suite (347 tests) passed; `git diff --check` clean.

Full discovery (`python -m unittest discover -s tests`) runs 4069 tests with
one unrelated, pre-existing failure:
`test_npm_environment_phase6_introspection.TestScenarioCoverage.test_every_spec_scenario_is_mapped_to_a_test`
(the canonical archived spec has scenarios absent from
`tests/data/npm_environment_scenario_coverage.json`). Neither that spec nor
the coverage map is modified by this change, so the failure is independent of
this work.

### Task status

Tasks 2.1–2.7 are complete. 2.5–2.7 were held open while the default and
structured collector paths did not yet share the same line bound; both paths
now enforce the same bound and pass the same safety assertions.

## 2026-09-27 (pipeline-order correction)

Earlier revision: corrected the structured path to decode → frame → neutralize.
Its `RedactingStream` remained unbounded; that gap is fixed above.

## 2026-09-26

First Phase 2 revision. Superseded.

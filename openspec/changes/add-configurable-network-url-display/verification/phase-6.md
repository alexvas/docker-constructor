# Phase 6 verification — one mode-selected retained tail and channel isolation

Scope: tasks 6.1–6.15 of
`openspec/changes/add-configurable-network-url-display/tasks.md`.

## Deliverables

### Mode-selected URL rendering hook (`docker/versioning/diagnostic_projection.py`)

`DiagnosticProjector` gained an optional, low-level `url_formatter`
(`Callable[[SafeHostPath], str]`). Every safety decision is unchanged: only an
already-validated `SafeHostPath` fact is passed to the formatter, and the
default remains the fixed `<redacted>` replacement used by the external SDK
projection. This lets the collector derive a normalized hostname/path render
from the same bounded projection without moving output policy into the
projector.

### One retained tail per stream (`docker/versioning/npm_diagnostic_stream.py`)

`NpmDiagnosticStream`, `make_stream_factory`, and `project_tail` now accept
`network_url_display`. Each stream feeds exactly one bounded tail selected
before capture:

- `redacted` — the URL-free sanitized projection (unchanged default);
- `host-path` — a dedicated projector renders each removed URL as its
  `SafeHostPath.text` (`hostname + canonical-safe path`, falling back to
  `/redacted-path`);
- `exact` — terminal-safe source text, bounded by the existing tail byte limit.

The projected-safe form used for external SDK events is computed transiently
in every mode and never becomes a second retained tail. In `host-path` and
`exact` no source content is retained unless the mode selects it.

### Failed collections carry their selected tails (`docker/npm_environment/streaming.py`)

`collect_streams` now builds the single `StreamingCapture` after both reader
threads join and the dispatcher finalizes, then attaches it to the exception
it raises:

- `StreamReaderFailure` and a control-flow interruption raised by the
  coordinator (`KeyboardInterrupt`/`SystemExit`) carry the same
  `StreamingCapture` under `RETAINED_CAPTURE_ATTR` plus the already-selected
  `diagnostic_tail`/`diagnostic_stream` pair (stderr preferred, then stdout).
- The attached strings reference the existing per-stream buffers — no second
  retained buffer is introduced — and the original exception, its cleanup
  notes, and the truncation notice are preserved.

### Failure context and the concurrent-timeout path (`docker/npm_environment/execution.py`)

- `assemble` copies a failed collection's `diagnostic_tail`/`diagnostic_stream`
  verbatim into the `LockedNpmError("executor_failure", …)` it already raised,
  instead of reducing the reader failure to exception text alone. The detail
  string is still projected through the selected tail projector; the retained
  tail is never re-projected.
- The concurrent deadline path, where the deadline fires while
  `collect_streams` is already raising a reader failure or interruption, reads
  the attached capture through `_attached_retained_capture` and passes both
  streams plus the truncation notice into `_timeout_error`. The deadline stays
  primary; the concurrent failure remains bounded secondary context.

### Interruption reporting at the orchestration/CLI boundary

A control-flow interruption raised while diagnostics were being captured still
propagates, but it is no longer silent about the selected retained context:

- `build_orchestration.describe_host_failure` exposes the same structural
  phase/step/resource attribution and already-selected retained tail that
  ordinary failure results carry, for an interruption that never becomes a
  `BuildResult`.
- `ExitKind.INTERRUPTED` names the transient failure-render result (the facade
  re-raises rather than returning it; its exit code is the conventional 130).
- The facade factors the existing `format_failure_report` call and JSON
  `host_failure` payload into `_host_failure_report`/`_host_failure_data`, and
  uses them for both the ordinary failure path and the interruption path.
- A live presentation session keeps ownership of interruption reporting.
  `_real_dispatcher` handles a `KeyboardInterrupt` *before* closing the
  session: `_route_interruption_report_to_session` extracts the retained
  context, builds the report, and submits it with `submit_final_report`.  Only
  then does the existing bounded `session.shutdown()` run, after which the
  original interruption is re-raised unchanged.  This mirrors the ordinary
  failure path, which already submits its report through the same actor.
- Ownership is explicit, not inferred from output.  Submitting sets
  `INTERRUPTION_REPORT_OWNED_ATTR` on the exception before any
  admission/rendering attempt.  `main` reads that marker and never renders a
  session-owned report again, even when admission, rendering, or shutdown
  fails.
- Where no session owns output (JSON, or text with no live sink),
  `_report_interruption_directly` emits the report once through the ordinary
  text or JSON renderer, then re-raises.  Text mode renders the report only -
  the structured `data` block is JSON-only, matching the ordinary path, so the
  tail appears once under the retained-context label.
- Interruption reporting is exception-safe and best-effort.  Context
  extraction, report construction, submission, rendering, and output are each
  guarded: a reporting failure (for example a broken output stream) can never
  replace the original interruption, and a failed output stream is never
  retried.
- Interruption semantics are unchanged: `execute_build` still cleans up
  confinement/snapshot state and releases the constructor lock, the session's
  bounded shutdown still runs, and the `KeyboardInterrupt` still propagates
  (exit 130).  An interruption with no retained context (for example
  `check-updates`) still produces no report.

### Failure-context extraction

`_host_failure_context` already reads `diagnostic_tail`/`diagnostic_stream`
from the exception chain. Because the assembled wrapper, the direct
reader-failure/interruption exceptions, and the interrupting CLI path now
carry those attributes, the selected retained context reaches
`format_failure_report` and the JSON `host_failure.tail` unchanged. The report
still renders nonempty retained context exactly once under the existing
`Retained diagnostics (may repeat live output):` label and omits the section
for an empty tail.

## RED evidence

### Original retention implementation (tasks 6.1–6.5)

The retention contract is checked by the revision-tolerant probe
`verification/phase6_retention_probe.py`. It asserts, for every mode and both
with and without an external SDK sink, that the single retained tail is the
selected representation while SDK events stay URL-free. When the checked-out
revision predates the request selector it calls the pre-change constructor
signature instead of passing `network_url_display`, so a pre-change checkout
fails on retained **content**, never on a missing import or unsupported
argument. The follow-up failure-propagation fix does not touch retention, so
this is the correct pre-implementation check for 6.1–6.5.

Baseline revision (commit immediately before the Phase 6 implementation):

```
05f9996e516d78f6c63e04bb9495e54ac6f31fc7  Add policy-aware npm fetch presentation grouping

git worktree add --detach /tmp/phase6-baseline HEAD
cp openspec/changes/add-configurable-network-url-display/verification/phase6_retention_probe.py \
   /tmp/phase6-baseline/openspec/changes/add-configurable-network-url-display/verification/
cd /tmp/phase6-baseline
python3 openspec/changes/add-configurable-network-url-display/verification/phase6_retention_probe.py
# exit 1 — 6 failing retention assertion(s)
```

Pre-implementation failures (content assertions, no import/signature errors):

```
PASS 6.1-6.3 stream tail [redacted]
FAIL 6.1-6.3 stream tail [host-path]: retained 'npm http fetch GET 200 <redacted> 15ms (cache miss)\n',
     expected 'npm http fetch GET 200 registry.npmjs.org/npm-http-research-fixture/-/npm-http-research-fixture-1.0.0.tgz 15ms (cache miss)\n'
FAIL 6.1-6.3 stream tail [exact]: retained 'npm http fetch GET 200 <redacted> 15ms (cache miss)\n',
     expected 'npm http fetch GET 200 https://registry.npmjs.org/npm-http-research-fixture/-/npm-http-research-fixture-1.0.0.tgz 15ms (cache miss)\n'
PASS 6.4 no-sink capture [redacted]
FAIL 6.4 no-sink capture [host-path]: ... (redacted retained)
FAIL 6.4 no-sink capture [exact]: ... (redacted retained)
PASS 6.5 with-sink capture [redacted]
FAIL 6.5 with-sink capture [host-path]: ... (redacted retained)
FAIL 6.5 with-sink capture [exact]: ... (redacted retained)

6 failing retention assertion(s)
```

Against the current implementation the same probe passes:

```
python3 openspec/changes/add-configurable-network-url-display/verification/phase6_retention_probe.py
# exit 0 — 0 failing retention assertion(s)
```

### Interruption reporting at the facade boundary

The interruption end-to-end tests are run against the CLI with only the new
`except KeyboardInterrupt` reporting removed (the retained capture attaches to
the interrupt either way). This isolates the wiring: the orchestrator already
preserved the context, but the facade emitted nothing.

```
# remove the main() interruption-reporting block, then:
python3 -B -m unittest tests.test_retained_tail_phase6.TestInterruptionEndToEnd
# Ran 4 tests — FAILED (failures=7)
```

The 7 failures are per-mode text (3), per-mode JSON (3), and the
presentation-worker case (1); the orchestration-boundary case already passes
because the orchestration layer preserved the retained context all along.

### Presentation ownership and exception-safe reporting (this fix)

The ownership regressions are run against the CLI with session routing and the
best-effort guards removed, while the ownership marker stays defined so the
tests fail on behavior rather than an attribute error. The result is the
reported bug: the session never receives the report, and a broken output
stream masks the interruption.

```
# revert _real_dispatcher's session routing and main()'s guarded direct
# reporting to the previous (session-shutdown + unconditional print) form:
python3 -B -m unittest tests.test_retained_tail_phase6.TestInterruptionPresentationOwnership
# Ran 5 tests — FAILED (failures=3, errors=2)
```

- 3 behavioral failures: `test_healthy_session_owns_the_report_exactly_once`,
  `test_failed_renderer_never_falls_back_or_retries`, and
  `test_blocked_renderer_stays_within_shutdown_budget` all find the session
  `durable` report count is 0 instead of 1 (the report was never submitted).
- 2 errors: `test_broken_text_output_still_propagates_the_interruption` and
  `test_broken_json_output_still_propagates_the_interruption` show a
  `BrokenPipeError` escaping and masking the original `KeyboardInterrupt`.

With session routing and the guards restored all 5 pass.

### Follow-up failure propagation (previously recorded, separate)

The following RED run is the earlier follow-up fix that carried retained
context through reader-failure/interruption and the concurrent-timeout path.
It rolled back only `streaming.py`/`execution.py` (mode-selected retention was
already implemented) and is kept here separately from the retention RED above.
It was captured before the interruption end-to-end tests were added, when the
module had 37 tests.

```
git diff -- docker/npm_environment/streaming.py docker/npm_environment/execution.py > /tmp/phase6_fix.patch
git checkout -- docker/npm_environment/streaming.py docker/npm_environment/execution.py
python3 -B -m unittest tests.test_retained_tail_phase6
# Ran 37 tests in 17.7s — FAILED (failures=18)
git apply /tmp/phase6_fix.patch
```

The 18 failures were reader-failure, interruption, already-shown-live,
terminal-safety, assembler-request-propagation, and concurrent-timeout
behavioral assertions — not import errors.

## GREEN and INTROSPECT results

`tests/test_retained_tail_phase6.py` runs the real production paths after
diagnostics have been captured:

```
python3 -B -m unittest tests.test_retained_tail_phase6
# Ran 46 tests in 23.5s — OK
```

Task mapping:

- 6.1–6.3 — `TestSelectedRetainedTail`: redacted is ordered and URL-free; the
  host-path tail renders the safe hostname/path with the `/redacted-path`
  fallback and no duplicate redacted tail; exact preserves bounded
  terminal-safe source URLs, credentials, proxy detail, query, fragment, and
  caller secrets.
- 6.4–6.5 — `TestSingleRepresentationMatrix`: with and without an SDK sink the
  retained tail is exactly the one selected representation, while every SDK
  event stays transient, redacted, URL-free, and path-free.
- 6.6–6.7 — `TestFailureChannelMatrix`: for every mode, nonempty, empty,
  truncated, timeout, interruption, reader-failure, and already-shown-live
  cases run through the real `collect_streams`/`_exit_failure`/`_timeout_error`
  paths and are rendered through both `format_failure_report` and the facade
  JSON channel. Assertions cover the mode-selected content, the 64 KiB byte
  bound, terminal-control inertness, a single retained-context section, and —
  for JSON — one valid document with `event_sink is None`.
- 6.6–6.7 (interruption, end to end) — `TestInterruptionEndToEnd`: a real
  `collect_streams` call is interrupted by SIGINT after serving a diagnostic,
  and the resulting `KeyboardInterrupt` propagates through the real
  `orchestrate_build` → `_real_dispatcher` → `main` boundary (no hand-built
  `BuildResult`, no mocked orchestration return). For all three modes it
  asserts the text report contains the selected tail exactly once under the
  retained-context label; JSON emits one valid `status: "interrupted"` document
  with `data.host_failure.tail` and no live sink; the presentation worker is
  joined; the constructor lock is released; and the interruption still
  propagates.
- 6.6–6.7 (presentation ownership) — `TestInterruptionPresentationOwnership`:
  a live session owns the retained interruption report. A healthy session
  submits the report exactly once before the bounded shutdown and `main` does
  not re-render it; a failed renderer still never triggers a synchronous
  fallback or a second write; a blocked renderer stays within the existing
  `WORKER_JOIN_SECONDS` shutdown budget; a broken text or JSON output stream
  cannot replace the original `KeyboardInterrupt` (and is not retried); and
  the build lock is still released in every case.
- 6.8 — `TestPresentationOnlyInvariance`: three otherwise equivalent requests
  differing only by the selector have identical input identity, output
  identity, tree digest, evidence bytes, normalized npm/Docker argv, and
  lifecycle steps; cache lookup/reuse ignores the selector; each mode stays
  independently verifiable; the failure primary result and cleanup client are
  identical.
- 6.9–6.12 — `TestAssemblerRequestBinding` and
  `TestAssemblerReaderFailurePropagation`: the request selector is bound before
  capture, is rejected before effects when invalid, and a reader failure keeps
  its selected tail while the primary `executor_failure` and the cleanup
  client are unchanged. `TestConcurrentTimeoutReaderFailure` proves a fired
  deadline concurrent with a reader failure keeps the timeout primary and
  reuses the attached tails.
- 6.13–6.14 — `TestSingleBufferOwnership`: exactly one attribute of each
  stream is a retained deque, the alternate projector owns no tail buffer,
  source content is never retained in `redacted` or `host-path`, and the
  collector returns that same single tail.

## VALIDATE

```
python3 -B -m unittest \
  tests.test_retained_tail_phase6 \
  tests.test_npm_diagnostic_collection_phase8 \
  tests.test_npm_environment_streaming \
  tests.test_host_path_projection \
  tests.test_npm_environment_cancellation \
  tests.test_npm_environment_phase3_deadline \
  tests.test_constructor_facade \
  tests.test_output_configuration \
  tests.test_constructor_project_root_phase7 \
  tests.test_npm_environment_cleanup \
  tests.test_npm_environment_publication_cleanup \
  tests.test_npm_environment_publication \
  tests.test_npm_environment_acceptance \
  tests.test_npm_environment_execution \
  tests.test_locked_assembly_observability_phase8 \
  tests.test_host_presentation_phase5 \
  tests.test_host_presentation_phase9 \
  tests.test_constructor_host_progress \
  tests.test_host_diagnostic_grouping_phase7 \
  tests.test_host_failure_output_regression
# Ran 708 tests in 37.0s — OK

python3 -m unittest discover -s tests
# Ran 4340 tests in 72.6s — OK (skipped=13)

python3 openspec/changes/add-configurable-network-url-display/verification/phase6_retention_probe.py
# 0 failing retention assertion(s)

./scripts/check-types
# All checks passed!

openspec validate add-configurable-network-url-display --strict
# Change 'add-configurable-network-url-display' is valid

git diff --check
# clean
```

## Out of scope

Phase 6 does not populate the internal presentation envelope from complete raw
npm lines, route envelopes to the local actor, or change the external SDK DTO.
Populating and routing the envelope, and any change to mailbox admission,
control capacity, or live `host-path`/`exact` rendering remain Phase 7 work
(tasks 7.6–7.9). Phase 6 only fixes which representation the one retained tail
holds, how failure text/JSON channels read it, and how a live presentation
session owns an interruption report while any direct report stays best-effort
exception-safe.

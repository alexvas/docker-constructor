# Phase 6BC partial adapter GREEN and reader-origin blocker

Status: **resolved by user authorization of ProcessResult.reader_failure_stream**.
The historical investigation below is retained; current implementation and
verification are recorded in `phase6bc-reader-source-verification.md`.
The prior join-precedence
blocker is resolved; reader joins still use ordinary=(). No additional task
checkbox was checked (122/145 overall; 5/11 Phase 6BC). Phase 6BD/7, specification
contracts and the index were not changed in this apply session.

## Partial changes, not acceptance

- `execution.py::DockerRunExecutor.run_streaming` now delegates acquisition and
  collection to one OwnedProcess. Its former inline ownership/coordinator body
  is replaced; obsolete standalone helpers still need removal/reconciliation.
- A private prepared removal action retains a separate default-owned captured
  helper. Container identity, absence/error interpretation and the public
  removal-attempt marker remain domain policy. The compatibility plan requests
  local stop, bounded removal, then generic local cleanup under the existing
  six-grace total; no cooperative Docker policy was introduced.
- `_DiagnosticCollector` in streaming.py handles incremental domain streams and
  sink dispatch only, without owning readers, processes or pipes. Typed terminal
  notifications finalize each stream independently; snapshots are not replayed.
- Callback reporting uses bounded domain secret/URL masking. Existing generic
  secondaries are projected through bounded sanitization rather than copied raw.
- Generic coordination observes asynchronous prepared-action settlement inside
  its remaining local reserve before publishing incomplete status. A private
  nonblocking wait(0) observation fallback supports old injected clients lacking
  poll. These partial changes still require explicit dedicated seam/fault tests
  and complete regression acceptance; they do not expand completed 6BB evidence.

Captured migration remains partial from the previous session. API/docs/import
baselines, old fixture seams/returned raw-pipe assertions, obsolete helpers,
full diagnostic compatibility and complete new-action lifecycle coverage are
not finished. No GREEN task can yet be checked.

## New blocking contract gap

Existing streaming callers require the domain StreamReaderFailure when reading
fails (including its stream tag and sanitized detail). Shared precedence also
requires an unexpected join OSError to outrank an ordinary reader OSError and
remain a join defect, not be relabeled as a reader failure.

The currently authorized supported snapshot exposes bounded bytes, returncode,
timeout, cleanup failures and stream outcomes. READ_FAILED identifies a stream
that failed, not the source of the authoritative exception. If reading fails
and join then raises OSError, both the read-only case and the join-winning case
have READ_FAILED and an authoritative OSError. With two failed streams the
outcome tuple also cannot reliably identify which read exception won.

The adapter therefore cannot in general recover correct failure provenance
from the supported observations. Mapping every OSError whenever READ_FAILED
exists would silently disguise join defects (including the newly clarified
policy). Reading private owner ledgers, inspecting traceback function names or
matching exception text is not a supported projection contract. Changing the
generic exception to a wrapper would break its identity guarantee. Returning
plain OSError for actual reads breaks preserved domain error compatibility.

Added the paired regression
`test_reader_oserror_projection_must_not_disguise_join_oserror_defect` in
`tests/test_npm_environment_phase6bc_streaming.py`. It proves the generic exact
winner in both cases, retains the reader failure, checks both sole releases and
cleans up every controlled owner. The read-only branch then requires the legacy
domain StreamReaderFailure; the join branch requires the exact join OSError,
not StreamReaderFailure. The current adapter fails the read-only assertion.

Decision requested: authorize a minimal supported bounded provenance extension
(for example, delivery-time exception metadata identifying a winning reader's
ProcessStream, absent/None for non-reader authority, with immutable prior
publication/late-observation rules), or select another explicit provenance
contract. This needs a coherent planning update before implementation. No such
extension was added, no traceback/private-ledger workaround was used and the
domain error requirement was not weakened.

## Verification (all complete logs retained)

Before adapter edits, full suite:
`/tmp/docker-constructor-pytest.HGDknL.log`, exit **1**,
**35 failed, 7039 passed, 13 skipped, 11 warnings, 17777 subtests passed**.

Intermediate focused adapter gate:
`/tmp/docker-constructor-6bc-adapter-green.ghNtUf.log`, exit **1**,
**3 failed, 50 passed**. Follow-up focused gate
`/tmp/docker-constructor-6bc-adapter-check.HIf4XC.log`, exit **1**,
**2 failed, 310 passed**.

Intermediate full suite:
`/tmp/docker-constructor-pytest.iaC8bh.log`, exit **2**,
**103 failed, 6968 passed, 13 skipped, 13 warnings, 17769 subtests passed**,
125.63s. xdist stopped after an interrupting worker in an old interruption
fixture. The failure section was extracted to
`/tmp/6bc-adapter-current-failures.log` and read. Failures include missing-poll
factory seams, retained controlled workers from failed legacy tests, raw-pipe
return assertions, superseded setup/collector/supervisor seams, domain reader
error/diagnostic compatibility, API/docs/import gates and timeout behavior.
Warnings include fork deprecations and unhandled old test interruption threads;
none were suppressed. This full verification is unsuccessful and predates the
wait(0) fallback and final bounded secondary projection changes. Do not claim
that those later partial corrections fix the full suite without rerunning it.

Follow-up scoped gate before final note projection:
`/tmp/docker-constructor-6bc-adapter-runtime.e1FyfZ.log`, exit **1**,
**2 failed, 338 passed**. Final scoped runtime gate before adding the provenance
regression: `/tmp/docker-constructor-6bc-adapter-runtime.iZseF6.log`, exit **0**,
**340 passed**, 2.46s. Command:

```text
.venv/bin/python -m pytest tests/test_npm_environment_phase6bc_streaming.py tests/test_npm_environment_phase6bc_captured.py tests/test_process_captured_action_faults.py tests/test_process_compatibility_action_gate.py tests/test_process_stop_policy.py tests/test_process_stop_policy_faults.py tests/test_process_callback_reporting.py tests/test_process_callback_interruption.py -n 8 --dist loadfile --tb=short -q
```

New provenance RED:
`/tmp/docker-constructor-6bc-reader-origin-red.zdZSnm.log`, exit **1**,
**1 failed, 7 passed**, 1.62s. Command:

```text
.venv/bin/python -m pytest tests/test_npm_environment_phase6bc_streaming.py -n 8 --dist loadfile --tb=short -q
```

Final static gate `/tmp/docker-constructor-6bc-adapter-static.vghuyG.log`:
`ty check docker --python-version 3.14 --output-format concise`,
`git diff --check` and strict OpenSpec validation each exit **0**. An earlier
missing int|None narrowing was corrected before this final static run. Static
success and scoped runtime passes are not integrated acceptance.

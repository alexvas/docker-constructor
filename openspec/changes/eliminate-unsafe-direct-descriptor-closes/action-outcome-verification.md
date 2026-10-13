# Retained action-boundary outcomes

## Implementation

Each prepared stage has a separate shared `CleanupFailures` ledger. Start and
cancel failures update both that ledger and the aggregate ledger. Observation
settlement flags advance independently; its error is the cumulative action's
winning error, not merely the latest observer return value. Successful
observation therefore cannot erase a boundary failure. Per-action precedence
retains the first interruption, then first defect, then first ordinary failure,
independently of the aggregate operation primary.

All updates construct new immutable `ActionResult` instances. Delivered tuples,
errors and exception diagnostics are not modified by late observation. Observer
diagnostics remain bounded per precedence class; start/cancel remain once-only.
The lifecycle guide documents cumulative action outcomes. No consumer, policy
budget, ownership, specification body or task scope changed.

## Tests and verification

21 new regressions cover both start/cancel boundaries across OSError, RuntimeError
and KeyboardInterrupt, settled and retained resources, successful observations,
late interruption escalation, first-interruption identity, separate action vs
aggregate operation primary, immutable delivered snapshots, independent stream
release and repeat shutdown. A nine-case start/cancel matrix checks shared
precedence between the two failures.

Complete stdout/stderr logs and runner exit statuses:

| Check | Log | Result |
| --- | --- | --- |
| RED | `/tmp/process-action-outcome-red.K7lGGl.log` | exit 1; 21 failed |
| Initial focused GREEN attempt | `/tmp/process-action-outcome-focused.2HQwQ8.log` | exit 1; 6 failed, 300 passed |
| Final focused gate | `/tmp/process-action-outcome-focused.vTRzF6.log` | exit 0; 306 passed |
| Full eight-worker pytest | `/tmp/docker-constructor-pytest.hv4bVU.log` | exit 0; 6831 passed, 13 skipped, 11 warnings, 17775 subtests passed; 30.40s |
| Typecheck | `/tmp/process-action-outcome-typecheck.eY2ayF.log` | exit 0; all checks passed |
| Configured unittest discovery | `/tmp/process-action-outcome-unittest.VTqAzw.log` | exit 0; 6426 tests, 13 skipped, OK; 140.186s |
| Whitespace | `/tmp/process-action-outcome-whitespace.aJpWyC.log` | exit 0; clean |

The initial focused attempt incorrectly expected repeat shutdown to return the
old action tuple after late completion. The API returns the latest observation;
the assertion now compares that latest tuple while retaining all assertions
that the originally delivered tuple/error/diagnostics remain unchanged.

Pytest ran with the project virtual environment, eight workers and loadfile
distribution. Configured typecheck/unittest were executed directly because the
green-loop launcher was already unavailable. No warnings were suppressed and
no Git index operations were performed.

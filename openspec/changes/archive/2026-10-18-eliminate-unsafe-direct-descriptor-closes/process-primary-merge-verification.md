# Concurrent primary merge correction

Shutdown merging preserves the existing original operation exception and replays
already-recorded interruptions before adding a later caller's active exception.
An active Exception remains an operation primary, not an unexpected cleanup
defect; later operation exceptions are retained secondarily under ordinary policy.
The first recorded interruption keeps exact authority across concurrent callers.

Barrier-controlled regression blocks reader joins, installs the first caller's
KeyboardInterrupt, then confirms the second caller's merge before releasing
cleanup. Both callers receive the same completed snapshot with the exact first
interruption and the second retained once. Repeated shutdown/observation does not
duplicate diagnostics; both pipe releases and terminate/reap occur exactly once.
All controlled workers are released and joined in finally.

Complete stdout/stderr logs and runner statuses:

- RED: `/tmp/docker-constructor-process-merge-red.69Pn13.log`, exit 1;
  1 failed, 96 passed. Both outcomes incorrectly selected the second interruption.
- GREEN, process lifecycle/API/ownership/docs gate:
  `/tmp/docker-constructor-process-merge-focused.H6wuuc.log`, exit 0;
  115 passed in 1.74s.
- Full `.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q`:
  `/tmp/docker-constructor-pytest.3wNZjs.log`, exit 0;
  6619 passed, 13 skipped, 11 warnings, 17775 subtests in 30.59s.
  Warnings remain visible and unsuppressed.
- `ty check docker --python-version 3.14 --output-format concise`: exit 0,
  All checks passed.
- `git diff --check`: exit 0, clean.

No specification/task checkbox or index changes.

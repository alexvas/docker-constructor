# Refresh undelivered completed cleanup outcomes

After merging an active operation primary, shutdown refreshes an already
published but undelivered phase outcome from the updated failure ledger. The
completion flag is preserved; delivered outcomes remain unchanged.

Deadline-controlled regressions await completed cleanup without delivery, then
exercise explicit shutdown and context exit with ValueError/KeyboardInterrupt
and ordinary/defective close failures in either pipe position. They assert exact
authoritative identity, displaced failure retained secondarily once, unchanged
causes, immutable prior snapshots, repeat-observation identity, and exactly-once
pipe release and child termination/reaping. Controlled cleanup is finalized in
finally, with no surviving supported workers.

Complete stdout/stderr and runner statuses:

- RED lifecycle suite: `/tmp/docker-constructor-process-status-red.E17eg4.log`,
  exit 1; 16 failed, 97 passed in 2.53s.
- GREEN lifecycle plus API/ownership/docs suite:
  `/tmp/docker-constructor-process-status-focused.8xfP0Y.log`, exit 0;
  131 passed in 2.14s.
- Full `.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q`:
  `/tmp/docker-constructor-pytest.pAqNma.log`, exit 0;
  6635 passed, 13 skipped, 11 warnings, 17775 subtests in 30.31s.
  Warnings are retained without suppression.
- `ty check docker --python-version 3.14 --output-format concise`: exit 0,
  All checks passed.
- `git diff --check`: exit 0, clean.

No specification/task checkbox or index changes.

# Close the status-publication / phase-notification race

Shutdown recomputes an undelivered snapshot from the current ledger under the
coordination lock immediately before marking it delivered. It preserves the
published completion state and no longer relies on `_phase_done` to decide
whether merged authority is included. Already-delivered snapshots are unchanged.

The regression intercepts `_phase_done.set()` after autonomous deadline cleanup
has published `_status`. A barrier keeps the notification unset; a second
barrier confirms the operation caller has merged its primary before allowing the
notification. Explicit shutdown and context exit both cover ValueError and
KeyboardInterrupt. Tests assert exact authoritative identity, retained close
failure once, unchanged old snapshots, repeat-result identity, one release per
pipe and one terminate/reap sequence. Barriers and workers are released/joined
in finally; completed cleanup leaves no supported workers.

Complete stdout/stderr and runner statuses:

- RED lifecycle suite: `/tmp/docker-constructor-process-gap-red.gCOAp0.log`,
  exit 1; 4 failed, 113 passed in 2.25s. The exact publication gap returned the
  stale close OSError in all four cases.
- GREEN lifecycle/API/ownership/docs suite:
  `/tmp/docker-constructor-process-gap-focused.EkKc1P.log`, exit 0;
  135 passed in 2.19s.
- Full `.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q`:
  `/tmp/docker-constructor-pytest.k8Vhiz.log`, exit 0;
  6639 passed, 13 skipped, 11 warnings, 17775 subtests in 33.31s.
  Warnings are retained and unsuppressed.
- `ty check docker --python-version 3.14 --output-format concise`: exit 0,
  All checks passed.
- `git diff --check`: exit 0, clean.

No specification/task checkbox or index changes.

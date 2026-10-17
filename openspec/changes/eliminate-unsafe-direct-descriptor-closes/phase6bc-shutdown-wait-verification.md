# Follow-up: interrupted phase wait after incomplete collection

## Fix

`docker/process.py::_shutdown` now unconditionally refreshes lifecycle status
from the current ledger under the lock before selecting control flow. A
previously delivered status no longer hides an interruption recorded by this
call while continuation publication is paused. Previously returned immutable
results and already-delivered failure suppression remain unchanged.

No acquisition, ownership, close, cleanup budget or worker policy changed.

## Deterministic regression

`tests/test_process_shutdown_wait_interruption.py` covers capture/stream and
explicit shutdown/context exit. A stalled reader keeps cleanup incomplete;
a publication barrier pauses the continuation after its initial incomplete
publication and before phase notification. After collection returns, a patched
phase wait raises a specific KeyboardInterrupt. Without any continuation
publication, each test requires that exact exception, its cleanup attachment,
COORDINATION/None status, unchanged delivered snapshot, no premature stream
release, and no replay on repeated cleanup. Releasing both barriers must finish
cleanup, join the continuation, and release each stream exactly once.

The structural process proof now requires an unconditional ledger refresh and
status observation in the same top-level lock block. A mutation fixture rejects
restoring the former delivery-state guard rather than merely dropping the old
structural assertion.

## Verification

Every pytest invocation used `.venv/bin/python`, eight workers, loadfile
scheduling, short tracebacks and quiet output. Complete stdout/stderr and runner
exit statuses were retained.

- RED: four deterministic regressions failed with DID NOT RAISE, exit 1:
  `/tmp/docker-constructor-wait-red.3JS87E.log`.
- Initial focused run: 188 passed, one structural-contract failure, exit 1:
  `/tmp/docker-constructor-wait-green.siaK8f.log`. The obsolete guarded-refresh
  assertion was replaced by the stronger unconditional-refresh/mutation check.
- GREEN focused regressions, join-interruption, delivery, lifecycle and contract:
  **190 passed**, exit 0: `/tmp/docker-constructor-wait-green.kY83Yq.log`.
- Full suite: **7722 passed, 13 skipped, 11 warnings, 17753 subtests passed**,
  49.37s, exit 0: `/tmp/docker-constructor-pytest.lAV1sz.log`.
  Multiprocessing/fork deprecation warnings remain unsuppressed.
- Typecheck (`ty check docker --python-version 3.14 --output-format concise`),
  `git diff --check` and strict OpenSpec validation each exited 0:
  `/tmp/docker-constructor-wait-static.SkrkEf.log`.

No specification/task changes, Phase 6BD/7 work, commits or index operations.

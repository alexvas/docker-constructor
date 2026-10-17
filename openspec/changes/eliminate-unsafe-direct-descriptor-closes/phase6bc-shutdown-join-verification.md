# Follow-up: post-collection shutdown join interruption

## Defect and fix

After collection delivery, context exit intentionally suppresses delivered
failure authority. However, `_shutdown` recorded a new continuation-join
KeyboardInterrupt through `_action` without setting its local interruption
marker, so neither context exit nor explicit shutdown propagated the new
control flow.

`docker/process.py` now tracks the first interruption encountered by this
shutdown call, including the continuation join. Before initial delivery it
preserves global first-interruption precedence. After delivery it propagates
this call's interruption, not an older delivered interruption. Existing
cleanup attachment, JOIN/None classification, immutable delivered results,
independent cleanup and repeat-cleanup suppression remain unchanged. No new
acquisition, release authority, budget, or close attempt is introduced.

## Regression coverage

`tests/test_process_shutdown_join_interruption.py` exercises capture and stream,
post-collection context exit and explicit shutdown, clean and already-failed
collection, exact KeyboardInterrupt identity, cleanup attachment, JOIN context,
unchanged collection snapshots, once-only stream release and repeat cleanup.
A separate case proves a new join interruption wins over an already-delivered
collection interruption without replaying the latter.

## Verification

All pytest invocations used the project virtual environment, eight workers,
loadfile scheduling, short tracebacks and quiet output. Complete stdout/stderr
and runner status are retained in each log.

- RED: eight original regression combinations failed with DID NOT RAISE;
  runner exit 1: `/tmp/docker-constructor-join-red.t3F3Ti.log`.
- GREEN: regressions plus process contract, failure delivery and callback
  interruption suites: **86 passed**, exit 0:
  `/tmp/docker-constructor-join-green.YyH9S7.log`.
- Complete suite: **7717 passed, 13 skipped, 11 warnings, 17753 subtests passed**,
  49.51s, exit 0: `/tmp/docker-constructor-pytest.tUuAqm.log`.
  Warnings remain unsuppressed multiprocessing/fork deprecations.
- `ty check docker --python-version 3.14 --output-format concise`,
  `git diff --check`, and strict OpenSpec validation each exited 0:
  `/tmp/docker-constructor-join-static.zGunO5.log`.

No specification/task changes, Phase 6BD/7 work, commits or index operations.

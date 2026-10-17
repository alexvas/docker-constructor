# Phase 6BC.3 RED: main/helper faults, races and retained outcomes

## Deliverables

`tests/test_process_captured_action_faults.py` adds 72 combined cases across:
main normal versus launch-hook setup failure, active ValueError versus first
KeyboardInterrupt, main join plus main close failure, helper transfer/join/
close failure in either pipe and ordinary/defective/interrupted helper action.
The test action prepares its nonblocking worker before main acquisition;
helper execution uses run_captured with default local ownership and a grace
allocation inside the absolute action deadline. No seam closes candidates.
Assertions bind exact precedence, exhaustive releases/reaping, same tracked
owner across transfers/cleanup, at-most-once action requests and unchanged
repeat observations. Completed helper setup failure may return None (generic
launch need not expose a handle after completed cleanup); fixture tracking
still verifies the actual generic owner and its released resources.

Two exact `_status`-published/`_phase_done`-unset barriers combine captured
helper release failure with a later ValueError/KeyboardInterrupt merge. Old
snapshots remain immutable, current undelivered authority refreshes, requests
and diagnostics are not duplicated.

A real main/helper subprocess case retains a blocked descriptor reader on the
helper, verifies independent main reaping/pipe release, explicitly incomplete
managed resources, later safe sole release/late error observation and no
mutation of previously delivered main authority or LifecycleOutcome. Every
controlled worker is released in finally; all acquired streams are released
through their existing designated owners.

`tests/test_process_stream_outcomes.py` adds deterministic concurrent invocation
serialization with a lock probe, once-only per-invocation end notifications,
no later byte replay, failure subscription without snapshot/note replacement,
and interruption identity with independent end-notification attempts. Earlier
registration/completion-before-registration, chunk-before-end and independent
prompt clean-EOF tests remain required.

`tests/test_process_failure_delivery.py` now binds real retained-live-reader
ABORTED outcomes, notification-before-delivery, outcome immutability and no
late automatic chunk/end callbacks. ABORTED is explicitly distinct from the
reader-stop/resource-release barrier.

## RED evidence

Command:

```text
.venv/bin/python -m pytest tests/test_process_captured_action_faults.py tests/test_process_stream_outcomes.py tests/test_process_failure_delivery.py tests/test_npm_environment_phase6bc_captured.py tests/test_process_failure_snapshot.py tests/test_npm_environment_phase6bc_streaming.py -n 8 --dist loadfile --tb=short -q
```

Complete stdout/stderr `/tmp/docker-constructor-6bc-task3-red.IismoM.log`, exit
**1**, **137 failed, 3 passed**, 1.65s. This is deliberately unsuccessful RED
verification, not production acceptance. No collection errors or surviving
controlled-worker cleanup failures were reported. Failure summary covers the
absent typed/snapshot API, absent adapter delegation/return contract and every
new main/helper case. Helper transfer assertions accept None after completed
setup cleanup while requiring actual generic ownership through fixture
tracking; mandatory real retained-handle cases are not relaxed. An earlier
intermediate run `/tmp/docker-constructor-6bc-task3-red.MPTLrQ.log` (exit 1,
137 failed/3 passed, 1.66s) is historical.

Task 6BC.3 is a test deliverable only. Phase 6BC.4's domain-specific sanitization,
removal/staging, abort-safe/no-replay and independent DiagnosticStream.finish
cases remain required before adapter GREEN work.

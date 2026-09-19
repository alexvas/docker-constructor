# Phase 3 Verification — Heartbeat Coordinator

Change: `improve-host-build-observability`
Phase: 3 (tasks 3.1–3.10)
Date: 2026-09-18
Scope: one orchestration-owned activity coordinator with an injected monotonic
clock and waiter; no transport/facade wiring yet.

## Deliverables

- New module `docker/versioning/activity_monitor.py`
  - `HostActivityMonitor` wraps exactly one `(HostPhase, HostStep)` operation.
  - Emits one `HostStepEvent(STARTED)` on construction and emits exactly one
    `HostStepEvent(SUCCEEDED|FAILED)` only after bounded heartbeat production
    has been confirmed stopped. Failed stop confirmation is contained and
    prevents premature terminal delivery.
  - Owns a bounded daemon heartbeat producer thread, its own stop signal, and
    its own producer-completion signal.
  - Emits `HostHeartbeatEvent` facts only; never text, never a wall-clock
    timestamp, never a Docker/process/network probe.
- New tests `tests/test_host_activity_monitor.py` (48 tests).

### Timer semantics (kept distinct)

| Clock | Reset by | Exposed as |
| --- | --- | --- |
| Heartbeat schedule | nothing (activity-independent) | first fact at `FIRST_HEARTBEAT_SECONDS = 3.0`, then one-second steps measured from the previous *scheduled* deadline (`<= HEARTBEAT_INTERVAL_SECONDS = 1.0`) |
| Diagnostic silence | stdout/stderr only, and only when the step declares `expects_diagnostic_stream` | `diagnostic_silence_seconds` exactly from `DIAGNOSTIC_SILENCE_SECONDS = 120.0` and thereafter |
| Last activity | every `diagnostic` or `transport_progress` observation | `last_activity_kind` + `last_activity_age_seconds` together, only once the whole monotonic age is `>= 1` |
| Fixed deadline | never (owned by the wrapped operation) | `remaining_deadline_seconds`, nonnegative, only when `deadline_seconds` was supplied |

Transport progress updates only the last-activity clock: it resets neither
diagnostic silence nor the heartbeat schedule. Presentation latency is not a
clock input to the schedule: the next deadline advances from the previous
scheduled deadline, never from the clock read after publication.

### Waiter contract

`HeartbeatWaiter` is authoritative: `set` requests interruption and `wait`
returns `True` once a stop was requested and `False` once its timeout elapsed.
A waiter that ignores both `set()` and its own timeout is outside the contract,
and the contract is documented where it is declared and validated at
construction (a waiter without callable `wait`/`set` is rejected), rather than
being accommodated at runtime. Lifecycle semantics are never weakened for it:
the coordinator keeps an interruptible-stop path that does not depend on the
injected waiter, so a waiter whose `set()` is a no-op (or raises) still cannot
keep the producer alive past the shutdown budget, and a waiter that ignores its
timeout can neither force an escaping exception nor an early terminal event.

## RED evidence (tasks 3.1–3.6)

Tests were authored before the coordinator existed:

```
$ python -m unittest tests.test_host_activity_monitor
ImportError: Failed to import test module: test_host_activity_monitor
...
ModuleNotFoundError: No module named 'docker.versioning.activity_monitor'
Ran 1 test in 0.000s
FAILED (errors=1)
```

## GREEN evidence (tasks 3.7–3.8)

`docker/versioning/activity_monitor.py` implemented against the RED fixtures.

```
$ python -m unittest tests.test_host_activity_monitor
Ran 33 tests in 0.137s
OK
```

Two initially authored fixtures had an off-by-one expectation after
`pump(121)` (the last pumped heartbeat is elapsed 123, so the next fact is
124); the fixtures were corrected to derive the expectation from the last
observed heartbeat rather than hardcoding 122. No production change was
required.

## INTROSPECT (task 3.9)

Reviewed the phase diff for the enumerated defect classes.

| Concern | Finding |
| --- | --- |
| Coupled heartbeat/activity timers | None. Schedule, silence, last-activity, and deadline state are separate; activity only writes last-activity state. |
| Transport-driven silence reset | None. `record_transport_progress` never touches `_last_diagnostic_at`. |
| Mislabeled silence | None. Silence is emitted only when `expects_diagnostic_stream` is true and is always `>= 120`. |
| Wall-clock activity timestamps | None. `grep -n "time.time\|datetime" docker/versioning/activity_monitor.py` → no matches; only `time.monotonic` is used as the injectable default clock. |
| Ambiguous initial activity | None. Both last-activity fields stay absent until a `>= 1s` whole monotonic age exists. |
| Negative/stale ages | None. `max(0, floor(...))` for elapsed/remaining; silence and age are omitted unless `>= 120` / `>= 1`. |
| Real sleeps in tests | None. `grep -n "sleep(" tests/test_host_activity_monitor.py` → no matches; tests drive a condition-variable waiter or a clock-advancing waiter. |
| Unbounded joins | Defect found twice: (a) the first revision rescheduled from the post-emission clock, so presentation latency stretched the heartbeat cadence, and (b) a later revision could spend three separate join/wait timeouts. Both corrected below: every join is bounded and all confirmation steps share one deadline. |
| Post-terminal callbacks | None. Terminal request immediately rejects activity and heartbeat publication; delivery is separately guarded for exactly-once emission. |
| Renderer dependencies | None. The module imports no presentation, transport, Docker, or filesystem modules. |
| Inferred network/process status | None. The closed heartbeat field set contains no prose, status, or exception object. |
| Changed operation exceptions | Defect found twice: (a) unisolated waiter calls could replace the wrapped result, and (b) an unconfirmable stop raised `AssertionError` out of `finish`. Both corrected below: every shutdown failure is contained. |

### Adversarial probes (first revision)

1. Regressing (non-monotonic) clock:

   ```
   regressing-clock beats:
      3 None None None 47
      4 None diagnostic 1 45
   ```

   Elapsed/silence/age/remaining all stayed within their DTO bounds.

2. Randomized fuzz (200 trials: random clock jumps `0..60s`, random activity
   interleavings, random deadlines, both diagnostic-stream applicability
   values):

   ```
   fuzz: 200 trials valid, no thread survived
   ```

   Every emitted `HostHeartbeatEvent` satisfied `elapsed >= 0`, jointly
   absent/present last-activity, `silence is None or silence >= 120`, and
   `remaining is None or remaining >= 0`; every trial emitted exactly one
   terminal step event and left no live coordinator thread.

3. Post-terminal race (clock advanced far and the waiter released concurrently
   with `finish`): the terminal step event was the last event.

4. Wedged producer that ignores the stop request *and* its own wait timeout:
   this probe was used to justify both a terminal event that could be withheld
   and an `AssertionError` raised out of the coordinator, neither of which the
   specification permits. A dependency that violates both halves of the
   `HeartbeatWaiter` contract is out of scope: the withholding behaviour, the
   escaping assertion, and the probe have all been removed, and the contract is
   documented at the declaration site instead.

## Follow-up correction — fixed-deadline cadence and shutdown lifecycle

Three revisions of this phase were reviewed; the corrections below are the final
behaviour. In particular, no coordinator failure may escape and no terminal
event may be delivered before production is confirmed stopped.

### 1. Sink/callback latency stretched the heartbeat cadence

The first revision rescheduled with `next_at = clock() + HEARTBEAT_INTERVAL_SECONDS`
using the clock read *after* the sink accepted the fact, so presentation
latency was added to every start-to-start interval (0.4 s of latency produced
`[3, 4, 5, 7, 8]` instead of `[3, 4, 5, 6, 7]`).

Fix: the schedule advances from the previous *scheduled* deadline through a
single, separately testable rule, so a slow sink can never move an interval and
an overrun can never produce a catch-up burst:

```python
def _next_heartbeat_deadline(previous: float, observed: float) -> float:
    deadline = previous + HEARTBEAT_INTERVAL_SECONDS
    if deadline <= observed:
        missed = math.floor((observed - deadline) / HEARTBEAT_INTERVAL_SECONDS) + 1
        deadline += missed * HEARTBEAT_INTERVAL_SECONDS
    return deadline
```

### 2. Terminal delivery requires explicit stop confirmation

`finish` separates terminal request from terminal delivery. The first request
stores its state, rejects activity, and raises the monitor-owned stop signal.
A budget-exhausted or failed confirmation defers delivery rather than treating
failure as success; a later `finish()` retries bounded confirmation and emits
exactly one event with the original state once shutdown is confirmed.

### 3. Waiter and join failures could escape, and are now contained

`finish` previously called `waiter.set()` and `join()` unguarded, so a waiter
or join failure replaced the wrapped operation's result. `set()` failure is
contained, and the confirmation sequence is:

```python
    def _stop_producer(self) -> bool:
        deadline = self._shutdown_clock() + HEARTBEAT_JOIN_TIMEOUT_SECONDS
        return self._confirm_producer_stopped(deadline)

    def _confirm_producer_stopped(self, deadline: float) -> bool:
        while True:
            try:
                if self.join(timeout=self._shutdown_remaining(deadline)):
                    return True
            except Exception:
                pass
            if self._producer_done.is_set():
                return self._confirm_thread_exit()
            remaining = self._shutdown_remaining(deadline)
            if remaining <= 0.0:
                return False
            try:
                self._producer_done.wait(timeout=remaining)
            except Exception:
                return False
```

`_run` always sets `_producer_done` from its `finally` block, so shutdown has
two independent confirmations: the bounded `join` and the producer's own
completion report followed by a bounded final thread-exit join. Every
stop-confirmation operation receives only the time left on one shared deadline;
there is no post-budget fallback. A raising `set()`, `join()`, or completion
wait is caught inside the monitor and cannot replace the wrapped operation's
result, but it also cannot count as successful confirmation. Only after a
successful confirmation is the terminal event emitted.

### Out-of-contract waiters are documented, not accommodated

An earlier revision raised `AssertionError("heartbeat producer did not stop")`
when the stop could not be confirmed, which made a coordinator failure able to
escape and replace the wrapped operation's result. That assertion is removed.
No shutdown failure may propagate: if the single budget is exhausted, which only
a waiter ignoring both `set()` and its own timeout can cause, the monitor
neither grants extra time nor delivers the terminal event prematurely. It does
not perform an unbounded completion wait or thread join; a later bounded retry
delivers the required terminal event after confirmation. For every waiter that
honours its timeout the producer exits inside the budget
(`HEARTBEAT_JOIN_TIMEOUT_SECONDS = 5.0` exceeds `FIRST_HEARTBEAT_SECONDS = 3.0`,
the longest single wait), which `TestBoundedJoin.test_join_bound_exceeds_the_longest_producer_wait`
asserts and
`TestMonitorValidation.test_waiter_contract_requires_bounded_waits_and_a_stop_request`
documents at the declaration site.

### Terminal-path contract assertions

`_MonitorTestCase.assert_single_terminal_after_shutdown` asserts, for every
supported terminal path: the producer's completion signal is set, the producer
is not alive, the sink observed `is_alive is False` when the event arrived,
exactly one succeeded/failed event was delivered, and it is the last event
(no heartbeat follows). `TestTerminalLifecycle` applies it to the success,
failure, timeout, cancellation, and waiter-failure paths, and additionally
asserts that later activity is rejected and that repeated `finish()` calls
neither duplicate nor replace the terminal event.

| Case | Test |
| --- | --- |
| Success / failure / timeout / cancellation paths | `TestTerminalLifecycle.test_every_terminal_path_delivers_exactly_one_step_terminal` |
| Waiter failure path | `TestTerminalLifecycle.test_waiter_failure_path_delivers_exactly_one_step_terminal` |
| Idempotent finish, single terminal retained | `TestTerminalLifecycle.test_finish_is_idempotent` (asserts the first state survives) |
| No heartbeat after the terminal event | `TestTerminalLifecycle.test_no_heartbeat_is_delivered_after_the_terminal_event` |
| Activity rejected after terminal | `TestTerminalLifecycle.test_later_activity_is_rejected_after_terminal` |
| Moderate sink latency does not move the schedule | `TestHeartbeatCadence.test_sink_latency_does_not_extend_the_scheduled_intervals` (latency 0.4 s, scheduled deadlines `[3, 4, 5, 6, 7]`) |
| Schedule arithmetic never schedules a past deadline or a burst | `TestHeartbeatCadence.test_next_deadline_advances_from_the_previous_scheduled_deadline`, `test_next_deadline_never_schedules_a_past_deadline_or_a_burst` |
| Shutdown bound covers the longest producer wait | `TestBoundedJoin.test_join_bound_exceeds_the_longest_producer_wait` |
| Budget exhaustion has no fresh or unbounded wait; delivery is deferred then retried | `TestBoundedJoin.test_budget_exhaustion_prevents_terminal_delivery` |
| Raising primary and final confirmation joins defer delivery; retry retains state | `TestBoundedJoin.test_confirmation_failure_prevents_terminal_delivery` |
| Concurrent retries retain the first requested state and emit once | `TestBoundedJoin.test_concurrent_retries_deliver_the_first_requested_terminal_once` |
| Activity and heartbeat injected-clock failures are contained | `TestClockFailureIsolation` |
| Shutdown clock fails after injected deadline creation | `TestClockFailureIsolation.test_shutdown_clock_failure_after_deadline_uses_real_budget` confirms a same-attempt real-monotonic budget, successful terminal delivery, and no fresh timeout |
| Shutdown clock is separate from the scheduling clock | The two preceding tests inject `shutdown_clock` while the scheduling clock is frozen/advanced by `_TickingWaiter` |
| Waiter contract is validated and documented | `TestMonitorValidation.test_waiter_contract_requires_bounded_waits_and_a_stop_request`, `test_rejects_invalid_configuration` (`waiter`, `shutdown_clock`) |
| Producer stops even when the waiter ignores `set()` | `TestBoundedJoin.test_finish_stops_a_producer_whose_waiter_ignores_set` |
| `set()` failure does not escape or alter the result | `TestBoundedJoin.test_set_failure_is_secondary_to_the_wrapped_result` |
| Join failure still delivers the terminal event | `TestBoundedJoin.test_join_failure_still_delivers_the_terminal_event` |
| Race between concurrent activity and terminal | `TestTerminalLifecycle.test_concurrent_terminal_and_activity_is_race_safe` |
| Absent and throwing sinks preserve the result | `TestTerminalLifecycle.test_absent_sink_preserves_the_primary_result`, `test_throwing_sink_preserves_the_primary_result` |

The 2.5 s-latency fixture that accepted a three-second heartbeat gap was
removed: a sink callback that overruns the one-second interval is outside the
bounded, prompt, non-blocking sink contract, and the cadence requirement that
subsequent intervals be no greater than one second is never relaxed. Overrun
handling is covered at the schedule level only
(`test_next_deadline_never_schedules_a_past_deadline_or_a_burst`), which asserts
that the arithmetic can neither schedule a past deadline nor replay missed
slots.

### Pre-fix behaviour reproduced

Each probe replaces exactly one production behaviour with an earlier revision
and runs one current test in its own process (isolated so a stuck probe cannot
contaminate the next one).

| Probe (replaced behaviour) | Result |
| --- | --- |
| Next deadline measured from the post-emission clock | `test_sink_latency_...` fails: elapsed `[3, 4, 5, 7, 8]` instead of `[3, 4, 5, 6, 7]` |
| Terminal request treated as permanently delivered after an unconfirmed join | retry coverage fails: the original state cannot be delivered exactly once after a later confirmed shutdown |
| Stop sequence giving each confirmation step a fresh `HEARTBEAT_JOIN_TIMEOUT_SECONDS` or an unbounded fallback | `test_budget_exhaustion_prevents_terminal_delivery` fails: a fresh/unbounded wait is observed, or deferred delivery cannot be retried after confirmed shutdown |
| `finish` calling `waiter.set()` unguarded | `test_set_failure_...` fails: `RuntimeError: stop signal failed` escapes `finish()` |
| `finish` calling `join()` unguarded | `test_join_failure_...` fails: `RuntimeError: join failed` escapes `finish()` |
| `finish` with an already-dead producer (waiter-failure path) | `test_waiter_failure_path_...` passes pre-fix: contract guard for the contained-failure path, not a regression guard |

The earlier probe that replaced the producer's wait loop with one that never
consults the monitor-owned stop signal is no longer part of the suite: with the
current design such a producer is out of contract, the monitor never raises for
it, and the probe could therefore only demonstrate an unbounded wait rather than
a regression.

## VALIDATE (task 3.10)

```
$ python -m unittest tests.test_host_activity_monitor
Ran 48 tests in 0.122s
OK

$ python -m unittest tests.test_host_activity_monitor tests.test_host_operational_events \
    tests.test_host_diagnostic_projection tests.test_constructor_host_progress \
    tests.test_constructor_build_output tests.test_constructor_pi_assembly
Ran 178 tests in 1.194s
OK

$ ty check docker --python-version 3.14 --output-format concise
All checks passed!

$ python -m unittest discover -s tests -p 'test_*.py'
Ran 3726 tests in 28.941s
OK (skipped=13)

$ openspec validate --changes "improve-host-build-observability" --json
valid: True
issues: []
```

The coordinator suite was also run 10 times consecutively to confirm that
terminal delivery, bounded shutdown confirmation, and contained confirmation
failures are free of timing flakiness:

```
$ for i in $(seq 1 10); do python -m unittest tests.test_host_activity_monitor; done
10 OK
```

Every test class derives from `_MonitorTestCase`, whose cleanup asserts that no
thread named `host-activity-heartbeat` survives the test, so no test leaves a
coordinator thread alive.

### Focused coverage map

| Task | Tests |
| --- | --- |
| 3.1 fixed cadence | `TestHeartbeatCadence` (8, incl. sink latency, scheduled-deadline assertions, and the extracted scheduling rule) |
| 3.2 diagnostic silence | `TestDiagnosticSilence` (4) |
| 3.3 activity separation | `TestActivitySeparation` (2) |
| 3.4 last activity | `TestLastActivityFacts` (5) |
| 3.5 deadline facts | `TestDeadlineFacts` (4) |
| 3.6 terminal lifecycle | `TestTerminalLifecycle` (9, incl. concurrency, absent/throwing sink, waiter failure, and the terminal-path contract), `TestBoundedJoin` (6, incl. strict stop-before-terminal confirmation, failure isolation, and the single shared shutdown budget) |
| validation | `TestMonitorValidation` (5, incl. the documented waiter contract) |

## Files changed

- `docker/versioning/activity_monitor.py` (new)
- `tests/test_host_activity_monitor.py` (new)
- `openspec/changes/improve-host-build-observability/tasks.md` (3.1–3.10 checked)

No file was staged or unstaged. `docker/versioning/host_progress.py`,
`docker/versioning/pi_assembly.py`, and the facade were not modified: wiring
heartbeat production into acquisition, lock, cache, npm, and publication
boundaries belongs to Phases 5 and 7.

## Phase-boundary note

Phase 3 does not emit `HostTransportProgressEvent`. `record_transport_progress`
records the latest cumulative byte count and exposes it via
`HostActivityMonitor.received_bytes` so Phase 5 can publish the latest value at
heartbeat cadence; publishing one event per chunk is explicitly out of scope.

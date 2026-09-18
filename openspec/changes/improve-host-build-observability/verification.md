# Phase 1 Verification: Operational Event Protocol

Change: `improve-host-build-observability`
Schema: `spec-driven`
Scope: tasks 1.1–1.7 only

## Deliverable summary

Phase 1 adds one immutable, presentation-neutral operational event union in
`docker/versioning/host_progress.py` alongside the unchanged `HostPhaseEvent`
lifecycle:

- Closed members: `HostStep`, `HostStepState`, `HostLastActivityKind`,
  `HostDiagnosticClassification`, and the existing `HostPhase` /
  `HostDiagnosticStream`.
- Immutable DTOs: `HostStepEvent` (with explicit closed
  `expects_diagnostic_stream` applicability), `HostTransportProgressEvent`,
  `HostHeartbeatEvent`, and `HostStructuredDiagnostic` (URL-free text plus a
  separately normalized hostname tuple).
- `HostOperationalEvent` union and extended `HostBuildEvent` union;
  `HostEventSink`, `guard_sink`, and `emit` accept the extended union.
- `HostEventMailbox` bounded non-blocking admission and
  `HostEventEnqueueAdapter` prompt-returning facade enqueue boundary.
  `try_admit()` acquires the mailbox lock with `acquire(blocking=False)`; lock
  contention is an immediate admission failure (`False`) rather than a block.
  `dropped` counts only capacity drops observed while holding the admission
  lock; the contention path touches no shared counter, so admission is
  non-blocking even while a producer holds `GuardedHostEventSink`
  serialization.
- `GuardedHostEventSink` accepts an optional injectable lock for instrumenting
  serialized admission; failure-isolation semantics are unchanged.

No formatting, exception objects, terminal escapes, direct printing, wall-clock
timestamps, or output-policy-dependent domain payloads were introduced.
`HostPhaseEvent` fields, values, and sink-failure semantics are unchanged.

## RED evidence (before GREEN)

Command:

```
python -m unittest tests.test_host_operational_events -v
```

Result: `FAILED (errors=1)` with
`ImportError: cannot import name 'HostDiagnosticClassification' from
'docker.versioning.host_progress'` — the operational event union did not exist.

## GREEN evidence (after implementation)

Command:

```
python -m unittest tests.test_host_operational_events -v
```

Result: `Ran 14 tests ... OK`.

Coverage mapped to the Phase 1 contract:

| Contract | Test |
| --- | --- |
| Closed phase/step/terminal-state/applicability/stream/kind members | `test_step_event_requires_closed_members_and_declares_stream_applicability`, `test_closed_step_membership`, `test_heartbeat_last_activity_jointly_absent_or_valid`, `test_structured_diagnostic_carries_url_free_text_and_normalized_hosts` |
| Diagnostic silence absent without an expected stream | `test_heartbeat_diagnostic_silence_requires_expected_stream` |
| Joint last-activity validity; reject fractional/sub-second/zero/negative/open-string/invalid types and wall-clock timestamps | `test_heartbeat_last_activity_jointly_absent_or_valid`, `test_heartbeat_rejects_wall_clock_activity_timestamps` |
| No exception/format objects | `test_operational_events_carry_no_exception_or_formatting_objects` |
| Lifecycle pair neither replaced nor mutated | `test_operational_events_neither_replace_nor_mutate_lifecycle_pair` |
| Shared serialized failure-isolated delivery | `test_operational_and_lifecycle_events_share_serialized_delivery` |
| Absent/throwing sink cannot affect primary operation | `test_omitted_or_throwing_sink_never_affects_primary_operation` |
| Bounded non-blocking enqueue, no render/wait/flush/stop/join under the guarded lock, prompt return under saturation | `test_adapter_only_enqueues_without_render_wait_flush_or_join`, `test_mailbox_rejects_nonpositive_capacity` |
| Prompt return while the mailbox lock is contended from another thread; contended event not admitted; no render/wait/flush/stop/join | `test_lock_contention_is_a_prompt_non_blocking_drop` |

## Focused regression run (task 1.7)

Command:

```
python -m unittest tests.test_host_operational_events \
  tests.test_constructor_host_progress tests.test_constructor_build_output \
  tests.test_constructor_build_orchestration tests.test_constructor_pi_assembly \
  tests.test_bound_pi_assembly_execution_phase6_registry
```

Result: `Ran 145 tests ... OK (skipped=7)`. Existing `HostPhaseEvent`
consumers and lifecycle/progress rendering remain compatible.

## Non-blocking admission hardening

Saturation (full capacity) and lock contention are distinct failures, and both
must return promptly without affecting the primary operation.

- Saturation: the lock is acquired, capacity is exhausted, `dropped` is
  incremented under the lock, and `try_admit()` returns `False`.
- Contention: `acquire(blocking=False)` fails, `try_admit()` returns `False`
  immediately, no shared counter is touched, and no presentation work happens.

`test_lock_contention_is_a_prompt_non_blocking_drop` holds `mailbox._lock`
from a background thread, invokes the enqueue adapter through
`GuardedHostEventSink`, and asserts bounded return (`< 0.5 s`), non-admission,
unchanged `dropped`, and no render/wait/flush/stop/join. Monkeypatching the old
blocking `with self._lock` implementation reproduces a `2.000 s` callback
return, which fails the assertion, confirming the test detects the regression.

## Focused regression run (task 1.7, re-run after hardening)

Command:

```
python -m unittest tests.test_host_operational_events \
  tests.test_constructor_host_progress tests.test_constructor_build_output \
  tests.test_constructor_build_orchestration tests.test_constructor_pi_assembly \
  tests.test_bound_pi_assembly_execution_phase6_registry \
  tests.test_ownership_cutover_phase5
```

Result: `Ran 190 tests ... OK (skipped=7)`. Phase 1 focused checks still pass,
so the Phase 1 boxes remain checked.

## Typecheck (task 1.7)

Command:

```
ty check docker --python-version 3.14 --output-format concise
```

Result: `All checks passed!`

## Full-suite result

`python -m unittest discover -s tests -p 'test_*.py'` now runs `3600` tests:

```
Ran 3600 tests in 28.074s
OK (skipped=13)
```

A pre-existing, unrelated error surfaced during the Phase 1 run:
`test_ownership_cutover_phase5.TestMovedClauseInventory.test_delta_spec_declares_the_requirement_removed`
hardcoded the pre-archive predecessor path
`openspec/changes/extract-local-project-configuration/specs/runtime-host-access/spec.md`,
while the predecessor now lives at
`openspec/changes/archive/2026-09-18-extract-local-project-configuration`.
That test was corrected to use the module's existing archive-aware
`_delta_spec_path("runtime-host-access")` helper (which already searched
`_SPEC_CACHE_DIRS`), so the delta spec is found whether the change is active or
archived. The full suite is now green.

## INTROSPECT result (task 1.6)

The phase diff was reviewed for free-form step names, mutable payloads,
exception/message leakage, terminal escapes, direct printing, lifecycle
expansion, and changed sink-failure semantics. No defect was found in that
review. A follow-up review of the producer admission path found that
`HostEventMailbox.try_admit()` used a blocking `with self._lock`, so a producer
holding `GuardedHostEventSink` serialization could block on mailbox lock
contention. That protocol-boundary defect was corrected with non-blocking lock
acquisition (see "Non-blocking admission hardening"); no other defect was
found.

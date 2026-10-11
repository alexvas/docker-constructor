# Registry fixture cleanup race — diagnosis and verification

The user authorized investigation of the unrelated full-suite blocker after
the delayed URL activation fix. This follow-up changes test-harness cleanup
only; it does not implement later OpenSpec phases or alter production shutdown
budgets, scheduling, registry fixtures, stress inputs or exception policy.

## Root cause

`LocalRegistryProxy.__exit__` submitted two independent loop callbacks:
`server.close`, then `controller.release.set`. The registry thread can process
the first callback, return from `serve_forever`, complete `asyncio.run` and close
its loop before the owner submits the second. Thus cleanup intermittently
raises `RuntimeError: Event loop is closed` at the second submission.

`controller.release` is a `threading.Event`, not an asyncio primitive. Cleanup
now sets it directly on the owner thread before requesting server close, then
submits only the server-close callback. Paused response workers are released
before asyncio shutdown waits for executor threads. No RuntimeError is caught
or suppressed; the existing `join(10)` and live-thread failure remain intact.

## Deterministic regression

`TestRegistryProxyCleanup.test_cleanup_releases_waiters_before_loop_can_close`
simulates a loop which executes the first callback and closes before the next
submission. No certificate, network, race timing or retry is needed. Against
the old harness it reproduces the exact error on the second callback. Against
the fix it verifies release is already set when server close runs, exactly one
loop submission, and the unchanged join/live-thread checks.

## Verification (full stdout/stderr and actual statuses preserved)

| Evidence | Command / result | Exit |
| --- | --- | --- |
| `registry_cleanup_red.log` | `.venv/bin/python -m pytest tests/test_bound_pi_assembly_execution_phase6_registry.py -n 8 --dist loadfile --tb=short -q`: 1 failed, 4 passed, 7 skipped; deterministic reproduction | 1 |
| `registry_cleanup_green.log` | Same command: 5 passed, 7 skipped | 0 |
| `phase3_validation.log` | `.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q`: 6352 passed, 13 skipped, 11 warnings, 17535 subtests passed | 0 |
| `registry_cleanup_types.log` | `./scripts/check-types`: All checks passed | 0 |

The failed full-suite run was copied before revalidation to
`historical-phase3/phase3_validation_registry_cleanup_failure.log` (exit 1).
No tests were excluded, no warnings suppressed, and no repeated runs were used
to claim a flaky pass: the race was reproduced deterministically and fixed
before the new full-suite run. Existing fork warnings remain in the final log.
Production code and P3 regression/audit scripts are unchanged since their
successful delayed-activation checks. `phase3_planning_validation.log` records
the final diff/OpenSpec checks. P3 task 3.14 can close again; overall progress is
44/72, P3 14/14, with P4–P6 pending.

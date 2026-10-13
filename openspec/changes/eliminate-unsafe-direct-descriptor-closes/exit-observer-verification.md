# Context exit and raised-observer escalation verification

## Corrections

- `OwnedProcess.__exit__` defaults to context shutdown. With no active body
  primary, its natural-exit poll runs as a shared cleanup action with explicit
  ordinary OSError classification. A poll failure cannot escape before the
  coordinator request. Active body primaries still skip reason-selection polling.
- `_observe_action` no longer suppresses every raised error after the first
  observer exception. Raised and returned errors share the existing per-action
  precedence-class diagnostic bound: ordinary, defect and interruption. A new
  class escalates; repeated raised errors of a previously recorded class retain
  the previous observation and first recorded exception identity. The obsolete
  blanket boolean observer-failure state is removed.
- No budgets, domain policies, stream ownership, consumer APIs or specification
  bodies changed. Delivered results and exception diagnostics remain immutable;
  late escalation is visible only through subsequent cleanup observations.

## Tests

15 new regression cases cover:

- Nine context-exit poll/release combinations of OSError, RuntimeError and
  KeyboardInterrupt: exact winning identity, shared precedence, both independent
  stream releases, sibling diagnostics, child terminate/reap and repeat shutdown.
- Two active body-primary cases: skipped reason poll, retained primary identity
  and complete independent cleanup.
- Four raised-observer escalation cases: OSError → KeyboardInterrupt and
  OSError → RuntimeError → KeyboardInterrupt, before and after delivery.
  Repeated fresh exceptions cannot replace the first interruption identity or
  grow retained diagnostics. Repairing the observer completes retained cleanup
  without retrying action start/cancel or changing delivered errors/diagnostics.

## Verification

All stdout/stderr logs and exit statuses were preserved.

| Check | Complete log | Result |
| --- | --- | --- |
| RED, isolated regressions | `/tmp/process-exit-observer-red.9Q8iBU.log` | exit 1; 13 failed, 2 passed |
| Focused lifecycle/policy/API gate | `/tmp/process-exit-observer-focused.RQ0dBI.log` | exit 0; 285 passed |
| Full eight-worker pytest | `/tmp/docker-constructor-pytest.PG4sGD.log` | exit 0; 6810 passed, 13 skipped, 11 warnings, 17775 subtests passed; 30.33s |
| Typecheck | `/tmp/process-exit-observer-typecheck.714tLR.log` | exit 0; all checks passed |
| Configured unittest discovery | `/tmp/process-exit-observer-unittest.yytkHD.log` | exit 0; 6426 tests, 13 skipped, OK; 139.786s |
| Whitespace | `/tmp/process-exit-observer-whitespace.XZjGbF.log` | exit 0; clean |

Pytest used `.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q`.
Configured typecheck and unittest commands were executed directly with the
project environment because the green-loop launcher was already unavailable.
Warnings were not suppressed. No Git index, task checkbox or later-phase scope
was changed.

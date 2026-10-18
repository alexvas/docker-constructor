# Phase 6BB generic stop-policy extension evidence

This is implementation evidence, not an amendment to planning requirements.
No Phase 6BC/6BD/7 or Docker-wrapper implementation is included.

## RED: 6BB.14–6BB.17

- Initial pre-production run: `/tmp/6bb-policy-red.hMKygC.log`, exit 1,
  45 new contract failures and 120 passing baseline lifecycle tests.
- Extended readiness fault RED: `/tmp/6bb-policy-fault-red.pQTUGv.log`, exit 1,
  invalid readiness acknowledgement was allowed to reach the factory.
- Complete RED replay against the unchanged HEAD baseline via a temporary
  pytest module loader (repository implementation not replaced):
  `/tmp/6bb-policy-complete-red.vEkxw0.log`, exit 1, 59 new contract failures,
  120 baseline lifecycle tests passed. Loader `/tmp/policy_red_baseline.py`,
  baseline `/tmp/6bb-policy-baseline.py` obtained with `git show HEAD:docker/process.py`.

Contracts cover validation and readiness before acquisition, prepared idle
resource cleanup, all shutdown triggers, distinct natural finalization, fake
monotonic stage clocks, three-stage plans, shared action/wait caps, local reserve,
immediate failure advancement, coordinator-owned compatibility ordering,
operation/action/close precedence, once-only initiation/cancellation, pending
owned resources, the publication/notification gap and first interruption,
bounded draining without consumer progress, separately default-owned real
auxiliary clients, partial startup/reader failure, a live reader barrier,
descendant-held write ends, concurrent deadline/shutdown and unrelated-fd safety.
Every test-controlled retained worker/resource is settled in a finally block.

## GREEN / INTROSPECT: 6BB.18–6BB.23

- Generic implementation adds frozen policy/stage/context/action-result types
  and a nonblocking managed-action protocol to `docker.process` only.
- Policy validation/readiness precede the sole main acquisition; readiness
  failure and factory failure cancel all partially prepared actions once and
  retain pending resource observation even when no main owner is returned.
- Stop-request and reader cancellation are distinct. Configured draining
  retains bounded tails without depending on callback-spool consumption.
- Stage initiation and child waiting share fixed absolute caps inside one
  total deadline, preserving a local reserve. Local-stop-before-action permits
  compatibility ordering without independent main-child authority.
- Default lifecycle tests retain their original behavior and primary/race
  fixes. One coordinator owns signaling/reaping/barriers/release/publication.
- Pending request/resource settlement prevents false completion. Stage-order
  action snapshots are immutable. Once-only cleanup requests and late
  observation never restart stages or mutate delivered exception diagnostics.
- Guide examples execute: three baseline examples, a separately default-owned
  auxiliary operation, and negative budget/interface/ownership examples.
- API/import/AST proofs cover readiness, all triggers, action aliases, reserve,
  shared clocks, local ordering, drain/backpressure, action retention and locked
  delivery. Mutation fixtures reject raw-main arguments, fresh budgets,
  observer/resource completion bypasses, action retries, inheritance/second
  coordinators, and previously characterized stream/new-module evasions.
- Initial typechecks exposed missing Self attribute declarations and a nullable
  action closure; both were fixed without suppressions. Final typecheck:
  `/tmp/6bb-policy-typecheck.lhWoGK.log`, exit 0, all checks passed.

## Focused VALIDATE: 6BB.24

Final focused gate: `/tmp/6bb-policy-gate.ujudo5.log`, exit 0,
**360 passed, 509 subtests passed**, 4.10 seconds. Includes generic policy,
policy faults, baseline lifecycle, API/dependency/mutation/executable-guide,
6BA stream/cleanup/convergence/documentation and descriptor-convergence suites.
This is new extension acceptance, not reuse of historical baseline logs.

## Full VALIDATE: 6BB.25

| Check | Complete log | Exit status / result |
| --- | --- | --- |
| `.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q` | `/tmp/docker-constructor-pytest.EqtRdn.log` | 0; 6738 passed, 13 skipped, 11 warnings, 17775 subtests passed; 31.36s |
| `ty check docker --python-version 3.14 --output-format concise` | `/tmp/6bb-policy-final-typecheck.FtzOnA.log` | 0; All checks passed |
| `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'` | `/tmp/6bb-policy-unittest.qx5ZDA.log` | 0; 6426 tests, 13 skipped; OK; 140.740s |
| `git diff --check` | `/tmp/6bb-policy-whitespace.SFHgs2.log` | 0; clean |

An earlier full pytest run also passed (`/tmp/docker-constructor-pytest.jrWE72.log`,
exit 0, same counts). The final full run includes the strengthened callable-alias
control proof. Unittest does not discover pytest-function-only extension tests;
the full pytest run validates those in addition to the unchanged unittest suite.
Existing multiprocessing fork deprecation warnings were retained, not suppressed.

`npx --no-install pi-green-loop check` could not start because its runner package
is not installed: `/tmp/6bb-policy-green-loop.2oWLI5.log`, exit 1. This is not
reported as a passing runner invocation. Both commands declared in
`pi-green-loop.json` were instead executed directly with the project virtual
environment; their independent successful statuses/logs are recorded above.

All 6BB tasks are complete. No 6BC, 6BD or Phase 7 checkbox was advanced, and no
Docker-wrapper implementation/real-daemon acceptance is claimed. Specification
bodies were not edited. No staging, commit or archive operation was performed.

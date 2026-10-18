# Phase 6BC: independent action initiation after child-observation failure

## Authorization and implementation

The user explicitly authorized revising the completed 6BB tests for the new
scenario: child.poll() failure must not suppress independent cleanup action
initiation. This resolves the historical compatibility-plan blocker; it does
not authorize dropping domain removal or adding a new policy API.

`docker/process.py::_run_policy` now separates read-only child observation
from managed-action initiation. Observation runs through the existing
`_action(..., ordinary=(OSError,))` ledger. On failure the nullable returncode
remains None, and the independent start receives StopContext with the original
reason and stage deadline. Observation failure does not become a managed-action
start error; actual start errors still use the existing action ledger and
transition/cancellation paths. All first-interruption, active-primary,
independent release, phase-cap, total-budget and late-publication rules remain.
No descriptor acquisition/release path or public signature was added.

`docs/process-lifecycle.md` clarifies unknown returncode and safe independent
cleanup after an observation failure, including interruption precedence.

## Tests and introspection

- Revised existing 6BB single/persistent poll-failure matrices to require
  prepare/start/cancel and None returncode on failed context observation.
- Retained exact primary/interruption identities, exhaustive independent pipe
  release, reaping, repeat cleanup and unchanged delivered outcomes.
- Added 27 combined poll/start/operation-primary cases, covering ordinary
  errors, defects and first/later interruption ordering. Both action stages
  receive a once-only start/cancel despite persistent child-observation failure.
- Updated the process ownership/API proof for the bounded nullable observation
  dataflow. New negative mutations reject bypassing failure accumulation or
  supplying the raw process instead of the observed returncode. Existing
  budget/ownership evasion fixtures remain required.

## Complete logs and exit statuses

| Verification | Full stdout/stderr log | Exit | Result |
| --- | --- | --- | --- |
| Revised fault tests plus compatibility gate before production edit | `/tmp/docker-constructor-6bc-poll-red.nfcDMw.log` | 1 | 22 failed, 88 passed; expected RED |
| Initial broader generic gate | `/tmp/docker-constructor-6bc-poll-green.bTlfBX.log` | 1 | 2 failed, 305 passed; introspection still expected the old direct poll expression; proof and negative fixtures were updated, not removed |
| Generic lifecycle/policy/fault/API/ownership/executable-guide and compatibility gate | `/tmp/docker-constructor-6bc-poll-green.lRIQd2.log` | 0 | 337 passed, 2.90s |
| `ty check docker --python-version 3.14 --output-format concise` | `/tmp/docker-constructor-6bc-poll-typecheck.kD7lCJ.log` | 0 | All checks passed |
| Full `.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q` | `/tmp/docker-constructor-pytest.PiVvnJ.log` | 1 | 39 failed, 6860 passed, 13 skipped, 11 warnings, 17775 subtests passed; 30.61s; unsuccessful full verification |
| `git diff --check` | `/tmp/docker-constructor-6bc-poll-diff.ngPaqR.log` | 0 | Clean |

The full-suite failure summary contains the existing 39 Phase 6BC RED
failures: missing process_result, absent typed stream outcomes, legacy captured
return/missing-stream behavior and absent adapter delegation/preparation.
Those failures were not excluded, skipped, weakened or claimed as passing.
The authorized polling correction and its generic regression gate pass, but
this is not acceptance of all Phase 6BC.

## Remaining scope

No additional task checkbox was marked complete: 6BC.7 requires the actual
adapter compatibility action plan, not only this prerequisite correction.
Phase 6BC remains 1/10 complete (6BC.2's RED deliverable), overall 118/144.
Remaining RED matrices, adapter migrations, generic failure snapshots/typed
stream delivery, documentation/API gates and final verification are still
pending. Phase 6BD and Phase 7 were not changed. The historical blocker file
is now explicitly marked resolved.

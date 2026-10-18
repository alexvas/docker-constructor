# Authorized CI prerequisite / fixture repair ledger

User explicitly authorized fixing the test fixtures as well as provisioning,
without skips, reduced parallelism or weakened lifecycle guarantees.
Base revision: `66ccbc203b690b63b856d1d9bd5111083c1616a8` (initial tree clean).
Earlier P1–P6 results are historical; affected test-preparation/source evidence
must be refreshed on the repaired state. Historical formatting/autofix reports
are not rewritten or represented as current verification.

## Observed remote job

Public job metadata retrieved with:
`curl -L --fail --silent --show-error https://api.github.com/repos/alexvas/docker-constructor/actions/jobs/117696661870 -o /tmp/ci-job-117696661870.json`
(exit 0). Job/run IDs 117696661870 / 39134675467, head revision matches base,
conclusion **failure**. Privilege provisioning, Python dependencies and type
check succeeded; complete gate failed; diagnostic upload succeeded. The user
supplied failure excerpt reports 6 failed, 8044 passed, 9 skipped, 18007
subtests passed, 11 fork DeprecationWarnings on Python 3.14.8. This is a user-
provided excerpt plus independently observed public metadata, not a downloaded
full log. `gh run view ... --log` exited 4 (no GitHub authentication), and the
public job-log REST request exited 22 / HTTP 403. No private credentials were
requested or read. Public web search did not expose the job log.

## RED work orders (all instantiated before GREEN)

| ID | One target / action | Prerequisites | Expected output / verification |
| --- | --- | --- | --- |
| CI-N | Provision reviewed Node/npm in Validation, matching research constants | user authorization, observed remote failure | `test_ci_provisions_the_reviewed_node_and_npm` |
| CI-C | Replace developer-only CA input with existing public test certificate | user authorization, observed clean-checkout failure | `test_ca_failure_fixture_does_not_read_a_developer_bundle` runs original behavior test while developer-bundle reads fail |
| CI-L | Bind cleanup requests to their own temporary project identity | user authorization, observed lock contention | `test_cleanup_requests_have_independent_project_lock_identities` asserts two requests use different supplied project roots |
| CI-A | Observe retained cleanup before asserting cumulative action-failure transport | user authorization, observed incomplete capture | `test_cumulative_failure_contract_allows_a_retained_cleanup_continuation` holds external cleanup incomplete until wait_cleanup observes the same continuation; original complete-at-capture assumption must fail |

RED command:
`.venv/bin/python -m pytest tests/enforcement/test_ci_fixture_isolation.py --tb=short -q`
Exit **1**, **4 failed**, 0.40 seconds, no warning summary. Each failure matches
its named target, not a missing tool. Complete stdout/stderr captured temporarily.
The original process failure reproduces deterministically with an unsettled
helper; this proves the test's synchronous-settlement assumption is invalid,
not that the remote scheduler is conclusively the sole cause. No production
process defect has been demonstrated, and no shutdown budget will be enlarged.

- [x] CI-N RED
- [x] CI-C RED
- [x] CI-L RED
- [x] CI-A RED
- [x] CI-N GREEN: workflow declaration matches reviewed versions; local Node/npm research and workflow contracts pass. New hosted provisioning execution remains unobserved.
- [x] CI-C GREEN: unchanged public PEM moved to a pure shared fixture; clean-checkout and inventory-independence regressions pass.
- [x] CI-L GREEN: temporary project/repo/inventory identities; both original cleanup-effect assertions and isolation regression pass.
- [x] CI-A GREEN: same-continuation settlement, cumulative action/coordination diagnostics and repeat observations pass. Deterministic cases require start exactly once before helper delay and zero starts after supervisor exhausts the stage cap.

## RED refinement before further GREEN

The first focused GREEN attempt exited 1: **3 failed, 189 passed, 1 reviewed
skip, 22 subtests passed** (25.32 s). Full output captured. Two cleanup tests
still inherited corporate-trust from the live inventory; the delayed-supervisor
case also retained a legitimate earlier CleanupIncomplete alongside the action
error. These are refinements of CI-L/CI-A, not unrelated product changes.

CI-L now requires independent temporary repo/inventory/project roots, using
the existing stable-project fixture helper. CI-A additionally requires retaining
all earlier snapshot failures while proving the single action failure is not
duplicated. A cap-expired stage may legitimately never start; preparation and
cancellation remain exactly once, start remains at-most-once. No action is
restarted, no budget is enlarged, and no coordination failure is erased.

Refined RED command:
`.venv/bin/python -m pytest tests/enforcement/test_ci_fixture_isolation.py tests/test_constructor_phase4_named_context.py::TestPhase4Cleanup --tb=short -q`
Exit **1**, **4 failed, 3 passed**, 0.72 s. CI-N/CI-C are already passing, while
CI-L/CI-A fail exactly their refined assertions. Source changes invalidate the
first focused run; final checks must be rerun after the remaining GREEN edits.

## Full-suite RED refinement for CI-C

Focused checks passed (192 passed, 1 reviewed skip, 22 subtests; exit 0), as did
150 process reporting repetitions. The subsequent complete `scripts/check-green`
run exited **1**: **1 failed, 8050 passed, 13 reviewed skips, 18028 subtests,
11 fork warnings**, 53.91 s. Existing inventory-independence enforcement caught
the proposed certificate import pulling in a live-inventory test module at
import time. That import is rejected, not an accepted fix or a green result.
CI-C GREEN must instead move the unchanged public test certificate to a pure
shared fixture module, leaving both consumers independent of each other's
module-level inventory reads. All affected checks will be rerun.

Final current-state verification and fingerprints are recorded in
`p6-ci-repair-results.md`. No earlier phase result proves the repaired state
green; all final manifest gates were freshly rerun.

# Captured-helper cleanup snapshot — test expectation repair

## Scope and checked source

Reported CI run: https://github.com/alexvas/docker-constructor/actions/runs/39178280717
Artifact URL supplied by the user:
https://github.com/alexvas/docker-constructor/actions/runs/39178280717/artifacts/12154852830
Diagnosis uses the supplied failure excerpt and local reproduction; the artifact
was not downloaded. This is a test-contract correction authorized after
architecture discussion, not an architectural migration or production fix.

Checked base: `1cdd98ed281bb5d4ab3b70160f1a43b276327f13`.
The only source changes against that base are these two files:

| File | Final SHA-256 |
| --- | --- |
| `tests/test_process_captured_action_faults.py` | `3433ade39f3c65670941108bf62896183f9d7907197dc5a0efdfc85871a46143` |
| `tests/enforcement/test_ci_lifecycle_scheduling.py` | `fd424d96404fce055d60b0fb213cccda057887056797f155dcd806f768b59b4e` |

Fingerprints obtained with
`sha256sum tests/test_process_captured_action_faults.py tests/enforcement/test_ci_lifecycle_scheduling.py`.
All production code, configured operation/cleanup budgets, generated architecture
inventory, skip policy and specification/task files remain unchanged. No index
mutation or commit command was executed; staging changed externally during work.
Previous reports describe historical source states, not this correction.

Environment: Linux container; CPython **3.14.7**, Ruff **0.17.1**, ty **0.0.73**,
pytest **9.1.1**, pytest-xdist **3.8.0**. CPython 3.14.8/hosted acceptance were
not executed locally. Versions obtained with:
`.venv/bin/python -c 'import sys; from importlib.metadata import version; print(sys.version); print({n:version(n) for n in ("ruff", "ty", "pytest", "pytest-xdist")})'` (exit 0).

## Diagnosis and repair

Bounded setup/shutdown may legitimately return an immutable incomplete snapshot.
Waiting for `CapturedAction.done` neither updates that snapshot nor proves the
main owner's independent cleanup completed. The old test asserted final cleanup
and final error authority against the earlier snapshot.

The test now obtains a fresh result from **the same** `owner.wait_cleanup(1)`
before its final assertions. Its existing one-second cleanup observation bound
is reused; production budgets and stop stages are not enlarged. Assertions still
require completion, exact final winner, all four injected errors, original
helper/main ownership, one factory call, exactly one close per pipe, unchanged
repeated cleanup and no remaining workers.

Setup exception assertions distinguish an already active primary interrupt
(which must propagate) from a helper interrupt discovered after launch returns
(which remains in the final result but cannot be thrown retroactively). The
final interrupt precedence assertion is unchanged.

Twelve deterministic regressions cover setup/shutdown, ValueError/KeyboardInterrupt
primary and OSError/RuntimeError/KeyboardInterrupt helper transfer faults. They
admit the real managed action, hold its start acknowledgement, and force the
initial coordination wait to return incomplete without sleeping through a
production deadline. Both setup launch and subsequent capture observations use
that controlled early-return seam until release. Only after helper work is done
does the fixture release main cleanup. Thus the old snapshot contains only the
primary, remains incomplete forever, and cannot contain the later main join
failure. The corrected test must observe the retained continuation instead.
All real owner cleanup and pipe-release assertions still run, including teardown.

## RED and intermediate checks

The **final regression** was run against the original test function from the
checked base (loaded in memory; no source restoration/index mutation): **12 failed,
106 deselected**, exit **1**, 0.28 s. Failures are at the original line 236,
`assert result.complete and ...`, not missing helper admission or teardown.
Reproduction command (temporary input is regenerated, not a durable evidence link):

```bash
git show HEAD:tests/test_process_captured_action_faults.py >/tmp/p6-snapshot-original.py
.venv/bin/python - <<'PY'
import ast
from pathlib import Path
from unittest import mock
import pytest
from tests import test_process_captured_action_faults as fixture
from tests.enforcement.green_fixtures import read_text
name = 'test_main_and_captured_helper_faults_share_precedence_and_single_owners'
tree = ast.parse(read_text(Path('/tmp/p6-snapshot-original.py')))
node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
node.decorator_list = []
namespace = vars(fixture).copy()
exec(compile(ast.Module(body=[node], type_ignores=[]), '<original-fixture>', 'exec'), namespace)
with mock.patch.object(fixture, name, namespace[name]):
    raise SystemExit(pytest.main(['tests/enforcement/test_ci_lifecycle_scheduling.py', '-k', 'captured_fault_fixture', '--tb=short', '-q']))
PY
```

Here HEAD was the checked base above; use that revision explicitly if reproducing
after a later commit. Earlier regression-fixture iterations were unsuccessful
(12 failed/185 passed; later 1 failed/196 passed twice): they respectively held
policy before action admission or allowed setup capture to consume the stage
allowance. They were corrected, not excluded. One Ruff format check exited 1
for a missing blank line; formatting was applied before final verification.
Earlier passing gates (54.27/120.47 s) predate the final regression refinement
and are not current acceptance evidence.

## Fresh final verification

Complete stdout/stderr was captured temporarily and runner exit statuses preserved.
No permanent full-log storage or temporary-log links are required for this report.

| Exact command | Exit | Result |
| --- | --- | --- |
| `.venv/bin/python -m pytest tests/test_process_captured_action_faults.py tests/enforcement/test_ci_lifecycle_scheduling.py -n 8 --dist loadfile --tb=short -q` | 0 | **197 passed**, 2.13 s |
| `scripts/check-green` | 0 | **8169 passed, 13 reviewed skips, 18028 subtests, 11 warnings**, 53.97 s |
| `taskset -c 0,1 scripts/check-green` | 0 | **8169 passed, 13 reviewed skips, 18028 subtests, 11 warnings**, 120.29 s |

Both full gates independently passed lint, formatting of **407 sources**, compile,
types and the unchanged full-suite command:
`.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q`.
Eight workers remain enabled on two CPUs. No filters/exclusions, skip allowances,
xfails or warning suppression were introduced. The 13 skips remain three missing
docker-dev cases, one missing-sudo case and nine explicit standalone Docker opt-ins;
the 11 warnings remain multiprocessing/fork DeprecationWarnings. The four local
privilege-related skips account for the applicability difference from hosted CI.
Dockerfile is unchanged; Hadolint was not run and is inapplicable to this change.
`git diff --check` and `git diff --cached --check` both exited 0.

Additional final-source controlled repeats, exit **0**, **120 cases passed on two CPUs**:

```bash
taskset -c 0,1 .venv/bin/python - <<'PY'
from itertools import product
from tests.enforcement.test_ci_lifecycle_scheduling import test_captured_fault_fixture_observes_final_cleanup_after_early_return as check
count = 0
for _ in range(10):
    for setup, primary, helper in product((False, True), (ValueError, KeyboardInterrupt), (OSError, RuntimeError, KeyboardInterrupt)):
        check(setup, primary, helper)
        count += 1
print(f'{count} controlled incomplete-snapshot cases passed on two CPUs')
PY
```

Fresh hosted verification is still required. No P6 hosted acceptance task was
marked complete, and no architectural change or P7 work was implemented.

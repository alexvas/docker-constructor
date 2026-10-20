# CI run 39271161989 — isolate fixture preconditions from short deadlines

## Checked source and limits

Reported artifact: https://github.com/alexvas/docker-constructor/actions/runs/39271161989/artifacts/12195702335
The supplied excerpt reports **2 failed, 8171 passed, 9 skips, 18028 subtests,
11 warnings**, CPython **3.14.8**, 219.38 s. The artifact was not downloaded:
`gh api repos/alexvas/docker-constructor/actions/artifacts/12195702335` exited
**4** because GitHub CLI has no authentication. Diagnosis uses the excerpt,
source inspection and controlled local reproduction, not additional artifact data.

Checked base: `b46ede4c279da4b5d3f9f7a43edf04c33addf314`; initially clean.
Only these three source files changed against the base:

| File | Final SHA-256 |
| --- | --- |
| `tests/test_process_failure_delivery.py` | `9ea45b699c4bf529d56c29392325515e9d8e64bc7694487d01231914453bf5a5` |
| `tests/test_npm_environment_phase4_lifecycle.py` | `c30c56d380f213dfa14eed5c6910a2b60e1f02e08d5b6fa0d58c297042a2dcab` |
| `tests/enforcement/test_ci_lifecycle_scheduling.py` | `332e235868507090d5b72ef6607b146036a8c4cb7486f7c0e1831dbb887a85b4` |

Fingerprints: `sha256sum tests/test_process_failure_delivery.py tests/test_npm_environment_phase4_lifecycle.py tests/enforcement/test_ci_lifecycle_scheduling.py` (exit 0).
Production code, operation/cleanup budgets, skip policy, architecture inventory,
specifications and task completion boxes are unchanged. No staging, unstaging,
reset or commit command was executed. Previous verification reports apply only
to their historical source states.

Environment: Linux container, CPython **3.14.7**, Ruff **0.17.1**, ty **0.0.73**,
pytest **9.1.1**, pytest-xdist **3.8.0**, Node **24.18.0**, npm **11.16.0**.
Version commands (all exit 0):
`.venv/bin/python -c 'import sys; from importlib.metadata import version; print(sys.version); print({n: version(n) for n in ("ruff", "ty", "pytest", "pytest-xdist")})'`,
`node --version`, `npm --version`.
Hosted acceptance and CPython 3.14.8 verification remain outstanding.

## Findings and test-only correction

1. **Held live reader / late close failure:** the fixture also required a real
   sleeping child to terminate, possibly receive SIGKILL, and be reaped inside
   a 50 ms cleanup allowance. Final reap may legitimately exhaust its remaining
   ~16 ms. That records `TimeoutExpired`; `CleanupIncomplete` is synthesized only
   when an incomplete result has no recorded failure. Therefore requiring it to
   be the first failure did not isolate the intended live-reader boundary.
   The fixture now runs a finite child and reaps it through its existing sole
   `OwnedProcess` before starting supervision. The stdout reader remains truly
   blocked and its pipe retained. All existing assertions remain: exact
   `CleanupIncomplete` classification, no exception output attachment, aborted
   stream status, immutable snapshots/callbacks/notes, later close failure,
   eventual release of both pipes and refusal of a second collection.
2. **Repeated short-grace descriptor isolation:** a 100 ms operation deadline
   included interpreter startup, although the fixture required natural exit 0.
   A slow interpreter can correctly be terminated with -15. The fixture now
   establishes natural exit/reap through the same owner before supervision.
   The original short allowances, 20 repetitions, pipe closure, absence of workers
   and unrelated-fd read/write checks remain. Added assertions require exact
   `ok\n` output and an empty error list. The existing child observer remains
   composed around the startup-precondition wrapper, not bypassed.

Both five-second waits are fixture-startup watchdogs **before** short deadline
activation, not extended production cleanup budgets. No test expectations were
relaxed to accept TimeoutExpired, -15, absent output, leaked owners or missing
close diagnostics. No independent signal/wait/reap authority was introduced.

Three new regressions inject final-reap expiry (capture and stream) or a 200 ms
interpreter delay in all 20 descriptor-isolation runs. The delegated subprocess
factory remains called only inside the prepared `OwnedProcess` owner.
These isolate the two reproduced assumptions; they do not prove all scheduling
races absent or imply that short-budget reap can never time out in production.

## RED evidence and unsuccessful intermediate checks

Before correction:
`.venv/bin/python -m pytest tests/enforcement/test_ci_lifecycle_scheduling.py -k 'isolates_reader_retention or descriptor_fixture' --tb=short -q`
exited **1**: **3 failed, 118 deselected**, 0.56 s. Failures reproduced the supplied
`TimeoutExpired` versus `CleanupIncomplete` assertion and -15 versus 0 assertion.
After correction, an intermediate focused check exited **1**, **2 failed,
151 passed**: patch ordering bypassed the existing child observer. Composition
was repaired; the child-close assertions were retained, not removed.

The final regressions were also run against the two original functions loaded
in memory from the checked base: **3 failed, 118 deselected**, exit **1**, 0.45 s.
No worktree restoration or index changes were needed. Exact reproduction:

```bash
git show b46ede4c279da4b5d3f9f7a43edf04c33addf314:tests/test_process_failure_delivery.py >/tmp/p6-ci4-original-delivery.py
git show b46ede4c279da4b5d3f9f7a43edf04c33addf314:tests/test_npm_environment_phase4_lifecycle.py >/tmp/p6-ci4-original-lifecycle.py
.venv/bin/python - <<'PY'
import ast
from pathlib import Path
from unittest import mock
import pytest
from tests import test_process_failure_delivery as delivery
from tests import test_npm_environment_phase4_lifecycle as lifecycle
from tests.enforcement.green_fixtures import read_text

def original(path, module, name):
    tree = ast.parse(read_text(Path(path)))
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
    node.decorator_list = []
    namespace = vars(module).copy()
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<original-fixture>', 'exec'), namespace)
    return namespace[name]

reader = 'test_real_live_reader_value_and_late_release_are_isolated'
descriptors = 'test_small_grace_repeated_runs_do_not_affect_unrelated_descriptors'
with (
    mock.patch.object(delivery, reader, original('/tmp/p6-ci4-original-delivery.py', delivery, reader)),
    mock.patch.object(lifecycle.TestLifecyclePrimitives, descriptors, original('/tmp/p6-ci4-original-lifecycle.py', lifecycle, descriptors)),
):
    raise SystemExit(pytest.main(['tests/enforcement/test_ci_lifecycle_scheduling.py', '-k', 'isolates_reader_retention or descriptor_fixture', '--tb=short', '-q']))
PY
```

Temporary inputs are regenerated by the command, not durable evidence links.

## Fresh final verification

Complete stdout/stderr was captured temporarily with each runner's exit status
preserved. All following results apply to the final source fingerprints above.

| Exact command | Exit | Result |
| --- | --- | --- |
| `.venv/bin/python -m pytest tests/test_process_failure_delivery.py tests/test_npm_environment_phase4_lifecycle.py tests/enforcement/test_ci_lifecycle_scheduling.py -n 8 --dist loadfile --tb=short -q` | 0 | **153 passed**, 7.50 s |
| `scripts/check-green` | 0 | **8172 passed, 13 reviewed skips, 18028 subtests, 11 warnings**, 54.37 s |
| `taskset -c 0,1 scripts/check-green` | 0 | **8172 passed, 13 reviewed skips, 18028 subtests, 11 warnings**, 119.19 s |
| `git diff --check` | 0 | No whitespace errors |
| `git diff --cached --check` | 0 | No staged whitespace errors |

Both full gates passed lint, formatting, compilation and types independently,
and ran the unchanged `.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q`.
Eight workers were retained, including on two CPUs. No failure exclusions,
xfails, skip additions, warning suppression or enlarged production budgets.
The 13 local skips are three docker-dev prerequisites, one missing sudo and
nine Docker opt-ins; hosted CI has four fewer privilege-related skips. All 11
multiprocessing/fork DeprecationWarnings remain visible. Dockerfile is unchanged;
Hadolint was not run and is not applicable to this test-only change.

Additional final-source controlled repeats on two CPUs, exit **0**:
**30 cases passed**, comprising **20 held-reader cases and 200 delayed-interpreter
runs**, with original owner/descriptor/worker assertions executed each time:

```bash
taskset -c 0,1 .venv/bin/python - <<'PY'
from tests.enforcement.test_ci_lifecycle_scheduling import (
    test_live_reader_fixture_isolates_reader_retention_from_final_reap_timeout as reader,
    test_small_grace_descriptor_fixture_does_not_charge_interpreter_startup as descriptors,
)
for _ in range(10):
    reader('capture')
    reader('stream')
    descriptors()
print('30 controlled cases passed on two CPUs: 20 held-reader cases and 200 delayed-interpreter runs')
PY
```

A successful hosted run on the final state is still required for P6 acceptance.
No P7 work or architectural migration is included.

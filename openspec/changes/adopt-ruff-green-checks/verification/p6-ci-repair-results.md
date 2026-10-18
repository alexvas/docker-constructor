# P6 authorized CI repair — current local verification

## Scope and state

Base: `66ccbc203b690b63b856d1d9bd5111083c1616a8`. The user authorized test
fixture repairs in addition to provisioning. This report supersedes earlier
local source-state results for the affected preparation/P6 controls; historical
phase reports remain unmodified and describe only their original states.

| Current manifest | Entries | SHA-256 |
| --- | --- | --- |
| [p6-ci-repair-source.sha256](p6-ci-repair-source.sha256) | 406 Python sources | `c68f1044e8b21a619a04d91bebaf966b350e4f3547452f98a2bdf83ba2505df3` |
| [p6-ci-repair-controls.sha256](p6-ci-repair-controls.sha256) | 13 control/doc/entry-point files | `46acedee2f7a77264bb95be4f7dadae9e5b7628abdecd8a06b1c04884ddcd46f` |

Both manifests use sorted inventory paths / explicit control paths with
`sha256sum` entries. No pi-green-loop configuration was reintroduced.
`git diff HEAD --name-only -- docker` returned no paths: production code,
process budgets, runtime inventory and Dockerfile are unchanged. No stress
fixture, test collection, lifecycle implementation, warning or skip policy was
weakened. All changes are workflow prerequisites, test data/isolation and test
observations consistent with the existing process contract.

Environment: Linux container, CPython **3.14.7**, Ruff **0.17.1**, ty **0.0.73**,
pytest **9.1.1**, pytest-xdist **3.8.0**, Node **v24.18.0**, npm **11.16.0**.
Sudo/docker-dev remain unavailable locally. The observed remote job used Python
3.14.8; that interpreter and newly revised hosted workflow were not executed
locally. No remote success for the repair is claimed.

## Observed remote job / diagnosis

[GitHub job 117696661870](https://github.com/alexvas/docker-constructor/actions/runs/39134675467/job/117696661870)
was independently checked via its public REST metadata (HTTP 200): head SHA is
the base above; privilege provisioning, Python dependencies, type checking and
diagnostic upload succeeded; the complete gate failed. The full log API
returned HTTP 403 and unauthenticated gh exited 4, so the six failure traces
and counts come from the user's supplied excerpt, not a claimed full-log
inspection. See [p6-ci-repair-ledger.md](p6-ci-repair-ledger.md) for commands and
all RED/intermediate unsuccessful verification outcomes.

Corrections:

1. Validation provisions reviewed Node **24.18.0** using pinned setup-node
   `49933ea5288caeca8642d1e84afbd3f7d6820020`, installs npm **11.16.0**, and
   verifies both versions. Public commit lookup returned HTTP 200. The canonical
   test guide now documents these previously omitted prerequisites. Local
   research remains genuine Node/npm execution, not mocked version success.
2. The disappearing-CA test uses the unchanged public self-signed test PEM,
   moved into inert `tests/certificate_fixtures.py`. Neither developer CA data
   nor a live-inventory test-module import is needed. The original test module
   retains its `_VALID_PEM` alias. AST extraction from `git show HEAD:...`
   confirmed exact certificate equality, SHA-256
   `d2b5322c347b2573533a98f388dcd89945ada86b159b471cf584ca6f0c3c8bf2`.
3. Both cleanup tests construct independent temporary project/repo/inventory
   trees via the existing stable-project helper. They still require the
   publication error / runner interruption and snapshot deletion; only shared
   checkout lock/configuration coupling is removed.
4. The cumulative-action test waits for the **same** retained continuation
   before asserting final complete cleanup. It asserts the action failure is
   present exactly once, earlier snapshot diagnostics survive, no unrelated
   failures appear, and repeated cleanup observations do not duplicate/erase
   failures. A legitimate initial `CleanupIncomplete` is retained, not hidden.
   Preparation/cancellation remain exactly once; start is at most once because
   an exhausted stage cap can prevent it. Deterministic fixtures require
   exactly one start for a delayed helper, and zero starts when a deliberately
   delayed supervisor misses its stage cap. No shutdown retry or new shutdown
   budget was introduced.

The added five regression cases prove reviewed Node/npm declaration,
clean-checkout CA independence, separate project identities, delayed helper
settlement and delayed supervisor settlement. They never add skips/xfails.
All descriptor/process work delegates to existing owned fixture/helpers; no
new production acquisition, close, handoff or shutdown branch was added.

## Fresh final verification

Complete stdout/stderr was captured temporarily for each runner; actual runner
exit statuses were preserved. No filtered output was used for acceptance.
Earlier failures are retained in the ledger, not relabelled successful.

Focused command:

```bash
.venv/bin/python -m pytest tests/enforcement/test_ci_fixture_isolation.py tests/test_process_strict_actions_and_reporting.py tests/test_constructor_build_materialization.py tests/test_constructor_phase4_named_context.py tests/test_npm_http_logging_research.py tests/test_constructor_corporate_network_red.py tests/test_inventory_independence.py tests/enforcement/test_green_integration.py -n 8 --dist loadfile --tb=short -q
```

Exit **0**: **267 passed, 1 reviewed environmental skip, 81 subtests passed**,
25.37 s; no warning summary.

Complete gate: `scripts/check-green`, exit **0**. Each underlying mandatory
command succeeded on the final state:

| Command | Exit | Result |
| --- | --- | --- |
| `scripts/check-python-lint` | 0 | No unresolved findings. |
| `scripts/check-python-format` | 0 | **406 files already formatted**. |
| `scripts/check-python-compile` | 0 | Full shared inventory compiles. |
| `scripts/check-types` | 0 | All checks passed, unchanged production/typing scope. |
| `.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q` | 0 | **8051 passed, 13 reviewed skips, 18028 subtests passed, 11 warnings**, 53.83 s. |

The five additional tests explain the increase from 8046 to 8051 local passes.
All 13 skips retain the prior individually documented conditions (three
missing docker-dev tests, one missing-sudo test and nine explicit standalone
Docker opt-ins). They were printed and approved by the unchanged policy, not a
fixed count allowance. Warnings remain the 11 unsuppressed CPython
multiprocessing/fork DeprecationWarnings in transaction-locking and constructor
build generation/transaction tests. No unexpected skip/xfail/xpass appeared.
The Dockerfile-only Hadolint check remains inapplicable (Dockerfile unchanged),
not represented as executed/passed.

Process repetition command, exit **0**, **150 cases passed**:

```bash
.venv/bin/python - <<'PY'
import sys
sys.dont_write_bytecode = True
import pytest
from tests.enforcement.test_ci_fixture_isolation import test_cumulative_failure_contract_allows_a_retained_cleanup_continuation
from tests.test_process_strict_actions_and_reporting import test_cumulative_action_failures_are_not_duplicated_or_erased
for _ in range(100):
    test_cumulative_action_failures_are_not_duplicated_or_erased()
for delay in ('helper', 'supervisor'):
    for _ in range(25):
        with pytest.MonkeyPatch.context() as patch:
            test_cumulative_failure_contract_allows_a_retained_cleanup_continuation(patch, delay)
print('150 cases passed: 100 ordinary, 25 delayed helper, 25 delayed supervisor')
PY
```

Syntax/certificate probe, exit **0**: YAML parsed, **five** Bash blocks valid,
PyYAML **6.0.3**, exact unchanged PEM confirmed. Exact command:

```bash
uv run --no-project --python .venv/bin/python --with pyyaml python - <<'PY'
import sys
sys.dont_write_bytecode = True
import ast
import hashlib
from pathlib import Path
import yaml
from tests.enforcement.green_fixtures import read_text
from scripts.python_checks import run_captured
from tests.certificate_fixtures import TEST_CA_PEM
workflow = yaml.load(read_text(Path('.github/workflows/validation.yml')), Loader=yaml.BaseLoader)
assert set(workflow['on']) == {'push', 'pull_request'}
checked = 0
for step in workflow['jobs']['production-type-check']['steps']:
    if 'run' in step:
        result = run_captured(['bash', '-n', '-c', step['run']], root=Path.cwd())
        assert result.clean and result.returncode == 0, result.stderr
        checked += 1
original = run_captured(['git', 'show', 'HEAD:tests/test_constructor_corporate_network_red.py'], root=Path.cwd())
assert original.clean and original.returncode == 0
old_pem = next(ast.literal_eval(node.value) for node in ast.parse(original.stdout).body if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == '_VALID_PEM' for target in node.targets))
assert old_pem == TEST_CA_PEM
print(f'YAML parsed; {checked} Bash blocks valid; PyYAML {yaml.__version__}')
print('Unchanged test PEM SHA-256:', hashlib.sha256(TEST_CA_PEM.encode('ascii')).hexdigest())
PY
```

`git diff --check` and `git diff --cached --check` exited **0**. Full logs are
not permanent evidence dependencies; no temporary paths are used as durable
links. No assistant staging, unstaging, reset or commit occurred; externally
changed index state was preserved.

## Remaining remote acceptance

Local repair work orders pass, but **6.11 and 6.25 remain unchecked** until the
revised hosted provisioning/full gate is observed successfully for the pushed
repair state. The previous remote job's successful privilege provisioning does
not prove the newly added Node/npm provisioning or the repaired suite. No P7
work or final change acceptance is claimed.

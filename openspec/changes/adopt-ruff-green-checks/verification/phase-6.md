# Phase 6 — permanent control implementation; remote provisioning pending

**Status: PARTIAL / exit not yet accepted.** Tasks 6.1–6.10 and 6.12–6.24 are
accepted locally (23/25). Task **6.11** awaits the user's pushed GitHub
provisioning run. Consequently **6.25** stays unchecked. No Phase 7 work is
claimed. The user explicitly assigned remote workflow execution to their push;
its absence is not a blocker to implementing the other permanent controls.

## Checked state and environment

Base revision: `5ca7307ee9be494853b9eaf6ac40e01f871f5a17`, plus the permanent
control delta identified by these complete manifests:

| Manifest | Entries | SHA-256 |
| --- | --- | --- |
| [p6-source-after.sha256](p6-source-after.sha256) | 404 scoped Python sources | `f14c9d617ee4fd675a255e3e1bc4717037af799d663eef70e7119d7d94856828` |
| [p6-controls-after.sha256](p6-controls-after.sha256) | 14 workflow/doc/config/entry-point files | `76452f4cac3dddba1ec8cc50fde3689cb1e88d36114704513a2c1d260b942b22` |

`scripts/check-green` mode is **0755**. Evidence and task-checkbox changes are
outside these source/control manifests. All 397 P5 source hashes still match:
`sha256sum -c openspec/changes/adopt-ruff-green-checks/verification/p5-source-after.sha256`
exited **0**. P5 full-run evidence is historical, not a substitute for this run.
The Python delta adds seven files; no existing product/test Python file was
rewritten. No runtime dependency, Dockerfile, image inventory, lifecycle
contract, stress construction, test exclusion or historical evidence was
modified by this continuation.

Local environment: Linux container, non-root dev UID/GID 1000, CPython 3.14.7,
Ruff 0.17.1, ty **0.0.73** in `.venv`, pytest 9.1.1, pytest-xdist 3.8.0.
The gate prepends `.venv/bin` to PATH, so the final type check uses the same
pin as CI, not the older globally installed local tool. Sudo, docker-dev and
Docker remain absent locally. CI provisions privilege prerequisites; ordinary
Docker-backed acceptance remains explicit standalone opt-in.

## Implemented deliverables / introspection

- `AGENTS.md` requires G after every major phase/cohesive behavior/refactor
  unit and before every commit regardless of size. Existing lifecycle and
  evidence rules remain intact. All three maintained READMEs link the same
  contributor guide in Russian/English/Chinese with the same requirement.
- `scripts/check-green` / `scripts/green_checks.py` execute the manifest's
  lint, format, compilation, type and exact full parallel pytest commands.
  Every independent check runs; the first nonzero status survives subsequent
  successful checks. Incomplete lifecycle cleanup cannot be green. CLI scope
  and mutation arguments are rejected. `pi-green-loop.json` selects that same
  gate instead of auto-detected narrower commands.
- `tests/conftest.py` loads `tests.green_skip_policy` for the mandated command.
  It reviews controller reports from xdist, prints individual IDs/reasons,
  rejects unreviewed skips and any xfail/xpass, and never replaces an existing
  failed exit with success. The individually enumerated opt-in conditions and
  four local privilege cases are documented in `docs/python-checks.md`.
  Privilege skips are forbidden in GitHub CI/privileged-helper mode. There is
  no numeric skip allowance or broad filename/reason exemption.
- `.github/workflows/validation.yml` retains push/PR, the existing pinned
  actions, read-only repository permission and explicit `scripts/check-types`.
  It provisions Python 3.14 plus the declared development group and P1 test
  prerequisites, then invokes the identical gate. The original type command
  remains independently required and is also included in the gate.
- The workflow captures complete gate stdout/stderr, saves the actual status,
  prints the log and exits with that status. Its separate `always()` artifact
  upload retains logs seven days, never overriding a failed check. No fix,
  write-format, automatic commit, scope filter or failure-swallowing command
  is present. Four workflow shell blocks also pass Bash syntax checking.
- Ruff locally and in CI uses `pyproject.toml` required version 0.17.1,
  target py314, and exactly E4/E7/E9/F/I/B/SIM115/RUF100. The shared selector
  covers the same 404 Python files including extensionless sources; format and
  lint enforce inventory parity. Neither type/test scope was narrowed. The
  Dockerfile-only Hadolint command remains conditional and was not applicable
  because Dockerfile is unchanged; it was not represented as executed/passed.
- The guide distinguishes local-before-commit checks from post-push/PR
  independent verification, and explicitly disclaims hooks, automatic commits
  and branch-protection administration. Durable summaries are self-contained;
  temporary full logs or seven-day CI artifacts suffice, with no permanent
  full-log requirement or temporary-path evidence dependency.

See [p6-contract-ledger.md](p6-contract-ledger.md) for RED results, per-contract
acceptance and the owned fixture-read lifecycle matrix. The earlier
[p6-workflow-provisioning.md](p6-workflow-provisioning.md) and
[p6-prerequisite-probe.md](p6-prerequisite-probe.md) describe older workflow
states; this report supersedes their source/control fingerprints.

## Final local verification

Complete stdout/stderr was captured temporarily for each runner, with its
actual exit preserved. The first GREEN run (50 contracts / 8043 suite tests)
is historical: later contract-strengthening edits were followed by the fresh
runs below. No output filtering or warning suppression was used for acceptance.

| Exact command | Exit | Current result |
| --- | --- | --- |
| `.venv/bin/python -m pytest tests/enforcement/test_green_integration.py tests/enforcement/test_green_skip_policy.py tests/enforcement/test_green_fixture_lifecycle.py --tb=short -q` | 0 | **53 passed**, 0.88 seconds, no warnings. |
| `scripts/check-green` | 0 | All five independent commands below succeeded. |
| `scripts/check-python-lint` (inside gate) | 0 | No unresolved findings. |
| `scripts/check-python-format` (inside gate) | 0 | **404 files already formatted**. |
| `scripts/check-python-compile` (inside gate) | 0 | Complete shared inventory compiles without execution/bytecode. |
| `scripts/check-types` (inside gate) | 0 | All checks passed; production docker and tests/typing retained. |
| `.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q` (inside gate) | 0 | **8046 passed, 13 skipped, 18027 subtests passed, 11 warnings**, 54.01 seconds. |
| `sh -n scripts/check-green` | 0 | Wrapper syntax accepted. |
| `git diff --check` and `git diff --cached --check` | 0 each | No whitespace diagnostics at the checked state; read-only index inspection only. |

Warnings: 11 unsuppressed CPython multiprocessing/fork DeprecationWarnings in
the transaction-locking, constructor-generation lock-parity and constructor
build-transaction tests. No pytest runner errors, unknown skip reasons,
xfails/xpasses or unresolved Ruff/ty diagnostics occurred.

Additional syntax probe (not a project dependency), exact command:

```bash
uv run --no-project --python .venv/bin/python --with pyyaml python - <<'PY'
import sys
sys.dont_write_bytecode = True
from pathlib import Path
import yaml
from docker._owned_file import owned_file
from scripts.python_checks import run_captured
with owned_file(lambda: Path('.github/workflows/validation.yml').open('r'), label='workflow syntax probe') as stream:
    workflow = yaml.load(stream, Loader=yaml.BaseLoader)
assert set(workflow['on']) == {'push', 'pull_request'}
steps = workflow['jobs']['production-type-check']['steps']
checked = 0
for step in steps:
    if 'run' in step:
        result = run_captured(['bash', '-n', '-c', step['run']], root=Path.cwd())
        assert result.clean and result.returncode == 0, result.stderr
        checked += 1
print(f'YAML parsed; {checked} run blocks passed bash -n; PyYAML {yaml.__version__}')
PY
```

Exit **0**: PyYAML **6.0.3**, YAML parsed and **four** Bash blocks accepted.
This is parsing/syntax validation, not a GitHub execution or proof of
hosted-runner prerequisites. Final read-only whitespace and both manifest
`sha256sum -c` checks also exited **0** after preparing this report.

## Current skip review (6.24)

The final mandated run prints every individual skipped node ID and reason.
They match all 13 dispositions in [skip-inventory.md](skip-inventory.md):

- Three exact foreign-owner test IDs: absent docker-dev locally.
- The exact differing-UID private-snapshot test: absent sudo locally.
- Seven exact registry acceptance IDs and the exact host deadline test:
  standalone hermetic runner not selected.
- The exact real pinned Node-container smoke ID: explicit real-Docker opt-in
  not selected.

The policy accepts these exact identity/reason/condition combinations, not
this count. Installable privilege prerequisites must not skip on CI, where
PRIVILEGED_HELPERS=1 is set. There is no collection reduction: the suite grew
by 53 tests relative to the 7993-passed predecessor run. Existing P5 source
fingerprints remain unchanged. The extra subtest follows source inventory
coverage; no stress fixture was reduced.

## Remaining acceptance

**6.11:** actual privileged clean-environment provisioning must be observed on
the pushed GitHub runner. Python dependency installation and pinned type
checking were already probed successfully, and current YAML/shell/contracts
pass locally, but local missing sudo/account cannot prove remote success.

**6.25:** publish an accepted exit only after 6.11 succeeds for this state (or
a newly fingerprinted/revalidated repair). Until then this is a partial report,
not an accepted P6 exit. No Phase 7 task is checked. No staging, unstaging,
reset, commit or other index mutation was performed by the assistant; external
index changes were left untouched.

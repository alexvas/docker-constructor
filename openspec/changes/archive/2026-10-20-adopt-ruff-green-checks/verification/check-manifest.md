# P1 check manifest

Two distinct sections are kept: checks that already existed before Ruff
adoption (`existing-required-checks`, mandatory throughout P1–P7 for their
scope) and the adoption checks added in P2 (`new-adoption-checks`, pending in
P1). See the [P1 exit report](phase-1.md) for phase statuses.

Discovery sources: `scripts/`, `pyproject.toml`,
`.github/workflows/validation.yml`, repository instructions in root
`AGENTS.md` and the maintained READMEs. `pi-green-loop.json` does not exist in
this repository, so it contributes no command. `pyproject.toml` configures ty
only; it declares no Ruff or pytest command.

## `existing-required-checks`

| ID | Exact invocation | Source configuration | Required scope | Activation | P1 status |
| --- | --- | --- | --- | --- | --- |
| E1 | `.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q` | [`docs/testing.md`](../../../../docs/testing.md) (canonical test guide; linked from root `AGENTS.md` and the READMEs) and design §5 (mandated full pytest) | complete `tests/` collection, including `tests/enforcement/`; no subset substitution | P1–P7, every phase | exit **0**: 7909 passed, 13 skipped, 18026 subtests passed, 11 warnings (current state, `P1-A13`/`P1-A14` revision; the earlier 7908-passed result is historical) |
| E2 | `scripts/check-types` (executes `ty check --output-format concise docker tests/typing`) | `scripts/check-types`; `pyproject.toml` `requires-python = ">=3.14"`, `[tool.ty.environment] python-version = "3.14"`, `[tool.ty.src] include = ["docker"]`; `.github/workflows/validation.yml` | production `docker/` **and** `tests/typing/`; scope must not be narrowed | P1–P7 | exit **0** (`All checks passed!`) |
| E4 | `scripts/check-dockerfile` (runs pinned `hadolint/hadolint@sha256:27173fe2…` in Docker) | `scripts/check-dockerfile`, `.hadolint.yaml`, [`docs/testing.md`](../../../../docs/testing.md) | `Dockerfile` only; conditional on Dockerfile changes | whenever `Dockerfile` changes | **not executed / not applicable in P1** — Docker CLI and daemon are absent (`docker: command not found`); this change does not modify `Dockerfile`. Provision command is recorded in [`environment-prerequisites.md`](environment-prerequisites.md). |

Notes:

- Test-running instructions live in [`docs/testing.md`](../../../../docs/testing.md). Commit
  `c0b58ae` centralized them there from `README.md`/`README.en.md`/`README.zh.md`,
  so the READMEs are no longer a source for commands. The guide makes the
  eight-worker full-suite pytest command the default, states that serial
  `unittest discover` is not the default, and asks for that command only when a
  task explicitly requires it. This change does not require it, so it is not a
  manifest entry or a phase gate. E3 is retired; its historical P1 results remain
  in [`phase-1.md`](phase-1.md). Existing check IDs are kept stable.
- The CI workflow installs `ty==0.0.73`; it runs the same `scripts/check-types`
  entry point. There is no separate "production ty" command beyond E2 because
  no `pi-green-loop.json` exists. E2 is not replaced or narrowed.
- The older `tests/enforcement/README.md` command
  `.venv/bin/python -m pytest tests/enforcement -n 8 --dist loadfile --tb=short -q`
  is a focused diagnostic command, not the mandated full-suite check.

## `new-adoption-checks`

These entry points were added in P2 and are implemented by
[`scripts/python_checks.py`](../../../../scripts/python_checks.py). The exact
invocations below are the only supported check commands; the contributor guide
is [`docs/python-checks.md`](../../../../docs/python-checks.md). All three run
from the repository root, use the project virtual environment, and return the
underlying check status (never a `tee`/`grep`/wrapper status).

| New check | Exact invocation | Implemented in | Required to pass from |
| --- | --- | --- | --- |
| Shared compilation | `scripts/check-python-compile` | P2 (`scripts/python_checks.py compile`) | P2 onward |
| Full-inventory format check | `scripts/check-python-format` | P2 (`scripts/python_checks.py format`) | P3 onward |
| Full-inventory Ruff lint | `scripts/check-python-lint` | P2 (`scripts/python_checks.py lint`) | P5 onward |

Activation status at P2 exit (reconfirmed on the consolidated current source
state recorded in [phase-2.md](phase-2.md)):

- **Shared compilation** is active and exits **0** for the complete inventory.
  P1 used the recorded one-off command in
  [`phase-1.md`](phase-1.md#task-114--one-off-compilation); P2 onward uses this
  entry point. Clean-copy subprocess regressions confirm both the wrapper and
  direct CLI load repository support modules without creating source-tree
  bytecode.
- **Format check** is not yet required to pass. Its P2 diagnostic run exited
  **1** (371 files would be reformatted, 25 already formatted) and is
  recorded as P3 migration backlog, never as a pass.
- **Lint** is not yet required to pass. Its P2 diagnostic run exited **1**
  (1304 findings) and is recorded as P5 migration backlog, never as a pass.

The tooling compares the shared inventory with default `ruff check --show-files`
discovery before every Ruff run and fails on mismatches, so a configuration
exclusion cannot silently drop an inventoried module. Extensionless Python
scripts are passed explicitly because Ruff's default recursion only discovers
`.py`/`.pyi`. Each mode also validates runtime coverage: lint runs
`ruff check --show-files` over the explicit inventory, and format parses the
processed-file report from `ruff format --check --verbose`. An omitted path,
including one hidden by `force-exclude` or `[tool.ruff.format].exclude`, is
named and fails the check. `--root <dir>` exists for the fixture-based tooling
contract tests; the manifest invocations above use the repository root default.

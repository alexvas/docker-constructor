# Python source checks

One deterministic inventory feeds Ruff linting, Ruff formatting and syntax
compilation for the repository-owned Python source. The tooling lives in
[`scripts/python_checks.py`](../scripts/python_checks.py); the entry points
below are thin wrappers that run it in the project virtual environment.

Run everything from the repository root. The canonical test commands are in
[the testing guide](testing.md).

## Installation

The development toolchain is declared as a dependency group, so no check
depends on an unrecorded global installation:

```bash
uv venv --python 3.14 .venv                 # only when .venv does not exist
uv pip install --python .venv/bin/python pytest pytest-xdist
uv pip install --python .venv/bin/python --group dev
```

`[dependency-groups].dev` in `pyproject.toml` pins `ruff==0.17.1`, and the same
version is Ruff's `required-version`. A different Ruff refuses to run instead
of silently checking with another policy. The compile and format entry points
require `.venv/bin/python`; they exit 127 with a diagnostic when it is missing.
Ruff is resolved from the interpreter running the tooling, then from `PATH`.

## Commands

| Purpose | Command | Required to pass from |
| --- | --- | --- |
| Compile every selected source | `scripts/check-python-compile` | P2 of the adoption |
| Check formatting (read-only) | `scripts/check-python-format` | P3 of the adoption |
| Check lint (read-only) | `scripts/check-python-lint` | P5 of the adoption |
| Print the inventory (diagnostic) | `.venv/bin/python scripts/python_checks.py list` | diagnostic only |

- Each entry point returns the underlying check status. A finding is a
  nonzero status; it is never rewritten as success by a pipe, wrapper or
  summary.
- `--root <directory>` runs the same checks against another repository root
  that has `docker/`, `tests/` and `scripts/`. The tooling contract tests use
  it for fixture trees.
- `ruff check` and `ruff format --check` run with the repository
  configuration, `--no-cache`, and no fixing flags. Check mode never rewrites
  a source file, and compilation never writes bytecode.

## Source selection rules

- **Roots:** `docker/`, `tests/`, `scripts/`. A missing root is an error, never
  an empty inventory.
- **Included:** every `.py` and `.pyi` file under those roots, plus every
  extensionless file whose first line is a recognized Python shebang. A
  recognized shebang is either a direct interpreter path whose final component
  is `python`, `python3`, or a versioned `python3.<minor>` name
  (`#!/usr/bin/python3.14`), or `/usr/bin/env` followed by one of those names
  (`#!/usr/bin/env python3`). Trailing arguments are ignored. The selector does
  not parse a shell command, interpret other `env` options, or execute the
  interpreter. A Python script without a recognizable shebang must be listed
  in `EXPLICIT_PYTHON_SOURCES` in `scripts/python_checks.py`; an unlisted
  extensionless file with no shebang is an error, not a silent omission.
- **Excluded, each with a recorded reason:** `__pycache__`, `.git`,
  `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `.venv`, `vendor` and
  `generated` directories; symbolic links are never followed, and a symlinked
  `.py`/`.pyi` file is an error; non-Python file extensions and non-Python
  shebangs are not Python source.
- `openspec/**` planning and historical evidence is outside the scoped roots.
- **Same inventory for Ruff.** The selected paths are passed to Ruff
  explicitly, including extensionless scripts, because Ruff's default
  recursion only discovers `.py`/`.pyi`. Before running, the tooling compares
  the inventory with default `ruff check --show-files` discovery: it rejects a
  configuration exclusion that hides a selected module, while discovery
  beneath `vendor`, `generated` or cache directories is ignored because those
  paths are intentionally outside the shared inventory. It then validates
  coverage for the requested mode: lint uses `ruff check --show-files` with the
  explicit inventory, and format uses the read-only
  `ruff format --check --verbose` processed-file report. Every selected path,
  including extensionless scripts, must appear in the mode report; otherwise
  the path is named and the check fails. This catches `force-exclude` and
  `[tool.ruff.format].exclude` overrides even when Ruff is given explicit
  filenames.
- **Compilation is a syntax check.** It runs `compile()` on the raw source
  bytes, so a source-encoding declaration is honoured, no module code is
  imported or executed, and no bytecode is left in the source tree.  The
  tooling disables bytecode writing before importing its own repository
  support modules, so direct CLI and wrapper execution cannot create
  `__pycache__` entries while loading those modules.
- **Deterministic output.** Paths are sorted, repository-relative POSIX paths
  without duplicates.

## Temporary migration backlog

While the Ruff adoption is in progress, a lint or format run may still report
findings. That run is recorded with its actual nonzero status as **migration
backlog**; it is never called a passing check, and it does not become green by
excluding files, suppressing rules or narrowing the inventory. A check that is
already active for the current phase, and every existing repository check,
must still pass.

Once the migration reaches manual resolution, all three commands are
mandatory: full-inventory lint has zero unresolved diagnostics, the format
check exits 0, and compilation exits 0 for the complete inventory. From then
on, a new nonzero status blocks the change like any other failed check.

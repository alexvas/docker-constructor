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
uv pip install --python .venv/bin/python pytest pytest-xdist ty==0.0.73
uv pip install --python .venv/bin/python --group dev
export PATH="$PWD/.venv/bin:$PATH"
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

## Permanent green gate (G)

After every major change and before **any commit**, run:

```bash
scripts/check-green
```

A major change is a completed implementation phase or a cohesive behavior or
refactor unit. Every commit requires G regardless of size, on the final
candidate state. Any later source edit invalidates the affected results.

The gate runs all three source checks above, `scripts/check-types` (both
`docker/` and `tests/typing/`), and the exact full-suite command in
[testing.md](testing.md). It attempts every independent check and returns the
first failing status; incomplete process cleanup also fails. No scope or fix
arguments are supported. Run this entry point directly; no pi-green-loop
configuration is required. When the Dockerfile changes, also run its
conditional gate from the testing guide.

For temporary complete diagnostic retention, run in a separate Bash shell:

```bash
log=$(mktemp /tmp/docker-constructor-green.XXXXXX.log)
scripts/check-green >"$log" 2>&1
status=$?
cat "$log"
printf 'Temporary log: %s; gate exit: %s\n' "$log" "$status"
exit "$status"
```

Do not use a `tee`/filter/upload exit status as the gate result. Keep the full
log while diagnosing and summarizing. Durable evidence records the source
fingerprint, commands, environment, actual statuses/counts, warnings and
limitations; it does not depend on temporary paths or permanently stored logs.

### Independent GitHub verification

The existing Validation workflow runs on push and pull request. It provisions
Python 3.14, the same development group, test tools and pinned ty, then runs the
same gate without write-mode fixes or commits. Complete gate output is uploaded
on success or failure with **seven-day retention**; uploading cannot change a
failed gate into success. The retained explicit type-check step is also
required. CI sets `PRIVILEGED_HELPERS=1` and provisions `sudo`, `runuser`,
`docker-dev` and `nobody`, so missing installable privilege prerequisites fail
rather than becoming accepted skips. The invoking user must differ from
`docker-dev`; the latter's group is a test ownership fixture, not Docker daemon
access.

Local checks are required **before committing**; GitHub checks the pushed tip
and PR independently, not every intermediate local commit. Hooks, automatic
commits and branch-protection settings are **not implemented** here. A remote
run is not claimed by a local simulation.

### Reviewed skip applicability

`tests/conftest.py` loads `tests.green_skip_policy` for the mandated pytest
command, including xdist. The report lists every skipped test and reason.
Unknown test/reason pairs and unexpected xfail/xpass force failure, even if
pytest otherwise reports success. There is no fixed numeric skip allowance.

Only these reviewed cases are allowed (full individual IDs are enumerated in
the policy module):

| Tests | Exact reason / condition |
| --- | --- |
| Foreign-owner snapshot hard-link rejection, two-build ownership maintenance, and foreign-owned constructor project | `an existing 'docker-dev' account is required`; ordinary local environments only, never CI or privileged-helper mode. |
| Differing-UID private snapshot traversal | `sudo runuser -u nobody unavailable: [Errno 2] No such file or directory: 'sudo'`; ordinary local environments without sudo only, never CI or privileged-helper mode. Other sudo failures are not blanket-approved. |
| Seven Docker-backed registry acceptance tests | `run through validate-bound-pi-assembly-execution-phase6-registry`; only while `BOUND_PI_PHASE6_HERMETIC` is not `1`. |
| Docker-backed host deadline/reaping acceptance | `run through validate-bound-pi-assembly-execution-phase6-host`; only while `BOUND_PI_PHASE6_HERMETIC` is not `1`. |
| Real pinned Node container smoke | `set NPM_ENV_REAL_DOCKER=1 to run the real pinned-container smoke test`; only while `NPM_ENV_REAL_DOCKER` is not `1`. |

The opt-in Docker checks require their standalone runners and usable Docker;
they are genuinely inapplicable in the ordinary unit-suite run. Do not enable
them merely to trim skips or silently accept missing CI packages. New reasons,
platform-specific conditions or unexpected increases require review and a
specific documented disposition before acceptance; never add a broad pattern
or a regression-hiding skip/xfail.

### Historical migration backlog

The P1–P4 activation schedule in the command table describes historical
migration checkpoints only. Since manual resolution (P5), all lint, formatting
and compilation checks must pass. No migration-backlog exception applies to
current permanent checks.

## Justified specific suppressions

A suppression is acceptable only when the necessary construct cannot be
expressed without changing an enforced contract. Use the narrowest code,
place the rationale next to the directive, and record the accepted reason in
the P5 resolution ledger. Blanket per-file suppressions and rule disabling are
not accepted.

The recurring reviewed exceptions are:

- **`E402` in tests** where the test module must configure its import path or
  optional fixture state before importing repository modules. The imports stay
  in place because moving them would change import-time setup.
- **`B904` and `B905` in lifecycle cleanup paths** where a re-raised cleanup
  result already carries the authoritative cause and secondary diagnostics, or
  where the audited coordinator's fixed-width `zip` shape is part of the
  ownership contract. Explicit chaining or `strict=True` would change the
  contract, not clarify it.
- **`B023` in the audited process coordinator** where `start_action` must keep
  its enforced signature while executing within the current loop iteration.
- **`B010` for dynamic cleanup attachment** to `BaseException` instances; a
  direct attribute assignment is rejected by the configured type checker.
- **Re-export shims and compatibility modules** such as `docker/versions.py`
  and `docker/transactions/errors.py`. Their imports are intentionally public
  bindings, so they are preserved with explicit `__all__` entries or redundant
  aliases rather than deleted.

See the [P5 suppression rationales](../openspec/changes/adopt-ruff-green-checks/verification/p5-resolution-ledger.md#suppression-rationales)
for the accepted per-target reasons and work-order IDs.

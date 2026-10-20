# Running tests and checks

Run commands from the repository root. This is the canonical test-running guide
for this project; README files and agent instructions should link here instead
of maintaining their own commands.

## Python tests

Use the project virtual environment with `pytest` and `pytest-xdist` installed.
If these test-runner dependencies are missing, install them with:

```bash
.venv/bin/python -m pip install pytest pytest-xdist
```

The default local full-suite command uses eight workers, distributing whole files:

```bash
.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q
```

`scripts/check-green` defaults to eight workers locally. The CI workflow sets
`GREEN_CHECKS_WORKERS=2` for its two-core runner, changing only pytest parallelism,
not the suite or other checks. `GREEN_CHECKS_WORKERS` accepts any integer from
`1` through `1024` (inclusive); invalid values fail before checks run.
To reproduce the CI gate locally, run `GREEN_CHECKS_WORKERS=2 scripts/check-green`.

For a focused enforcement-only check (not a substitute for the full suite):

```bash
.venv/bin/python -m pytest tests/enforcement -n 8 --dist loadfile --tb=short -q
```

The ordinary suite does not invoke Docker, but its local HTTPS/npm research
fixtures execute **Node 24.18.0 and npm 11.16.0**. Use the project image or
provision that Node version with your host's version manager, then install and
verify the reviewed npm version:

```bash
npm install --global npm@11.16.0
node --version                         # must report v24.18.0
npm --version                          # must report 11.16.0
```

CI provisions both explicitly; the runner's preinstalled Node/npm are not
assumed suitable. These are test-environment prerequisites, not changes to the
managed image inventory. Corporate CA tests use public test certificates and
must not require a developer's ignored `.docker-local` trust bundle.

The tests use unittest-compatible cases. The parallel
runner requires pytest and pytest-xdist; do not use serial `unittest discover`
as the default. If a task explicitly requires `unittest discover`, run that
command as well: pytest does not replace an explicitly required verification.

### Capturing results

Capture complete stdout/stderr temporarily and preserve the runner's exit
status. Run this Bash block in a subshell or separate shell because it exits:

```bash
log=$(mktemp /tmp/docker-constructor-pytest.XXXXXX.log)
.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q \
  >"$log" 2>&1
status=$?
printf 'Temporary full test log: %s\nExit status: %s\n' "$log" "$status"
tail -n 80 "$log"
exit "$status"
```

Keep the complete log available while diagnosing failures and preparing the
verification summary. If the tail is insufficient, read the complete log or
extract the failure section:

```bash
awk '/^=+ (FAILURES|ERRORS) =+/{show=1} show' "$log"
```

Never use a filtered pipeline such as `... | grep "FAIL:"` as verification:
it hides errors and the final summary, and its exit status is not the runner's
status. Every nonzero runner status means unsuccessful verification. Do not
exclude failures or suppress warnings to claim success.

Store durable verification evidence separately from specifications: include the
checked revision or source-state fingerprint, exact commands, relevant
environment, exit statuses, result counts, significant warnings, and limitations.
Historical results apply only to their checked source state; source changes
require fresh checks. Full logs need not be permanently retained or committed;
do not use temporary container-local log paths as durable evidence links.

### Type checks

Run the type checker independently of the Python tests and preserve its exit
status:

```bash
scripts/check-types
```

This checks maintained production modules under `docker/` using the Python 3.14
semantics configured in `pyproject.toml`. Run it in the project image, which
provides `ty` and the intended Python installation.

### Python source checks

The shared lint, format and syntax-compilation checks run over one inventory of
repository-owned Python source. Installation, exact commands, source-selection
rules and the temporary migration-backlog distinction are documented in
[python-checks.md](python-checks.md).

## Docker image tests

Changes that modify the Dockerfile must be checked on the host, from the
repository root, using the Hadolint-based script:

```bash
scripts/check-dockerfile
```

The script requires Docker, which is available on the host but not inside the
container where the agent runs. It uses the immutably pinned official Hadolint
image; Hadolint is not installed through Python.

Python tests do not replace this check. Preserve the script's exit status and
report any failure. If the host-side check cannot be run, explicitly report it
as not performed rather than claiming successful verification.

# Local eight-worker and CI two-worker gate profiles

## Historical split-profile source (superseded by range validation below)

Base revision: `70598f677e45214d93f7ccc8e1d197b52bdccaa4`.
The checked source is that revision plus these changes (SHA-256):

| File | SHA-256 |
| --- | --- |
| `scripts/green_checks.py` | `f3879900e80e4f9a21a25306f551cc1cac041f2f266be6ea177d1a398c03fcf8` |
| `tests/enforcement/test_green_integration.py` | `90f42087a4ebf6865aaed0cc10d5d6dedd4878cb2a59a24aaf77b48709ecd7c3` |
| `.github/workflows/validation.yml` | `e3adc8d33e8ab0a93c3ef56cf6ee6efa401a9aa755531ba196c7db2952a95597` |
| `docs/testing.md` | `7f560c04a68d9d710ffdc80e6e92ca4239bda5327fe95231abef2354f4332b5f` |

Fingerprint command, exit 0:
`sha256sum scripts/green_checks.py tests/enforcement/test_green_integration.py .github/workflows/validation.yml docs/testing.md`.

The local gate still defaults to `-n 8`. Only the CI gate step sets
`GREEN_CHECKS_WORKERS=2`, selecting `-n 2`. Both retain the complete test suite,
`--dist loadfile --tb=short -q`, independent lint/format/compile/type checks,
full diagnostic capture and failure-status propagation. Only the supported
worker profiles `2` and `8` are accepted; invalid settings fail before checks.
Contract tests cover both profiles, success/failure propagation, invalid
settings and detection of a workflow regression to eight workers.
No production lifecycle code, budgets, specifications or completion boxes changed.

## Environment and verification

Linux container; CPython 3.14.7, Ruff 0.17.1, ty 0.0.73, pytest 9.1.1,
pytest-xdist 3.8.0, Node 24.18.0, npm 11.16.0. Version commands, each exit 0:

```bash
.venv/bin/python -c 'import sys; from importlib.metadata import version; print(sys.version); print({n: version(n) for n in ("ruff", "ty", "pytest", "pytest-xdist")})'
node --version
npm --version
```

Complete stdout/stderr was captured temporarily and runner exit statuses were
preserved. No temporary logs are required as durable evidence links.

| Exact command | Exit | Result |
| --- | --- | --- |
| `.venv/bin/ruff check scripts/green_checks.py tests/enforcement/test_green_integration.py` | 0 | All checks passed |
| `.venv/bin/ruff format --check scripts/green_checks.py tests/enforcement/test_green_integration.py` | 0 | Two files already formatted |
| `.venv/bin/python -m pytest tests/enforcement/test_green_integration.py -n 8 --dist loadfile --tb=short -q` | 0 | 45 passed, 0.98 s |
| `scripts/check-green` | 0 | 8213 passed, 13 skipped, 11 warnings, 18028 subtests, 54.72 s |
| `GREEN_CHECKS_WORKERS=2 taskset -c 0,1 scripts/check-green` | 0 | 8213 passed, 13 skipped, 11 warnings, 18028 subtests, 163.06 s |
| `git diff --check` | 0 | No whitespace errors |

Both full gates independently passed lint, formatting, compilation and types.
Their logs confirmed the exact respective pytest commands:

```bash
.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q
.venv/bin/python -m pytest tests -n 2 --dist loadfile --tb=short -q
```

The 13 local skips remain three docker-dev prerequisites, one missing sudo and
nine Docker opt-ins. All 11 existing multiprocessing/fork DeprecationWarnings
remain visible. No failures were excluded or warnings suppressed. Dockerfile
is unchanged; Hadolint is not applicable and was not run.

An earlier verification attempt for the superseded all-two-worker edit was
aborted on user correction; it supplies no successful verification evidence.
The results above apply only to the final split-profile source fingerprint.
The supplied successful hosted result precedes this worker-profile change;
fresh hosted verification of the new workflow remains outstanding. Local
CPython is 3.14.7 rather than hosted 3.14.8; the two-CPU run is not a hosted
runner benchmark or proof that two workers are faster. No commits or staging
changes were performed.

## Current correction: integer worker counts from 1 through 1024

The user requested removing the two-value restriction. Current validation accepts
integer counts in the inclusive range 1–1024, normalizes the pytest argument to
decimal, and rejects invalid or out-of-range input before executing any checks.
Local default eight and explicit CI two are unchanged. Tests cover both range
boundaries, intermediate counts, leading zeros, malformed input, out-of-range
values and error diagnostics. These are injected-runner tests: they do not
actually launch up to 1024 workers or run nested suites.

Base remains `70598f677e45214d93f7ccc8e1d197b52bdccaa4`. Current SHA-256 values,
from the same fingerprint command above (exit 0):

| File | SHA-256 |
| --- | --- |
| `scripts/green_checks.py` | `6f72b405f35a8f0b26e5102f79078d3d09d442694c550e576c937e332358e5ab` |
| `tests/enforcement/test_green_integration.py` | `6614d6196ec214a32b8285486a534e3ca3a06aea23238c600f94cec4b9b9983f` |
| `docs/testing.md` | `dfbe7d0a78104a95abf7a73012881be2bf5e0a3aa5ad9d3d0ab7d85a3eaa5ecd` |
| `.github/workflows/validation.yml` | `e3adc8d33e8ab0a93c3ef56cf6ee6efa401a9aa755531ba196c7db2952a95597` |

Fresh checks, same local environment as recorded above, complete temporary
stdout/stderr capture and preserved runner exit statuses:

| Exact command | Exit | Result |
| --- | --- | --- |
| `.venv/bin/ruff check scripts/green_checks.py tests/enforcement/test_green_integration.py` | 0 | All checks passed |
| `.venv/bin/ruff format --check scripts/green_checks.py tests/enforcement/test_green_integration.py` | 0 | Two files already formatted |
| `.venv/bin/python -m pytest tests/enforcement/test_green_integration.py -n 8 --dist loadfile --tb=short -q` | 0 | 58 passed, 1.01 s |
| `scripts/check-green` | 0 | 8226 passed, 13 skipped, 11 warnings, 18028 subtests, 54.94 s |
| `git diff --check` | 0 | No whitespace errors |
| `git diff --cached --check` | 0 | No staged whitespace errors |

The full gate passed lint, formatting, compilation, types and the canonical
local eight-worker suite. Skip reasons and unsuppressed fork warnings are
unchanged from above. The earlier real two-worker run is historical evidence
only; it was not rerun for this source state. Current injected-runner tests
verify the two-worker command and status behavior without claiming hosted
acceptance. No production lifecycle or Dockerfile changes; no Hadolint run,
commits or staging changes.

# P2 correction — vendor/generated directory exclusions

- **Change:** `adopt-ruff-green-checks`
- **Status:** supporting detail for the consolidated P2 exit report
  ([`phase-2.md`](phase-2.md)); that report is authoritative for the phase
  verdict. This report covers the vendor/generated directory-exclusion
  correction and refers to the same current source state as phase-2. The
  separate Ruff parity, mode-coverage and source-tree bytecode follow-ups are
  recorded in phase-2.
- **Checked source state:** `HEAD` `6b24592effa121a22261ef9833accd8ff754a497`
  plus the current P2 working-tree changes. The canonical P2 fingerprints are
  test-input
  `e513ebf21468fdfa61ed99c250a8b2ddf7cabd454fa55bcafb8a5ab63620148a`
  (324 files), production
  `dd0bb36a5a0d47d0786f3073172978d96d1325a9211a7321d28503f6babc46e4`
  (122 files, unchanged from P1) and combined scoped
  `a00d459dcb69c8055b2aa80489c24bb6143c702d00d7eed178b5185cd51457f0`
  (460 files). These source edits were unstaged at verification time; the
  content fingerprint is independent of index state. The relevant files have
  these SHA-256 digests:
  - `scripts/python_checks.py`:
    `8d309cc49c062a375e40adf72bd2144431f37fc8902ced49618560f75d53a49b`
  - `tests/enforcement/test_python_syntax_compilation.py`:
    `a53b2bfbd36f45aff187abce60728da7d9dec1fcd79a52c0ceb7cc98a7b96298`
  - `tests/enforcement/test_python_source_selection.py`:
    `0a80fce4bd8177a379011681336fd81eff823c75a7ed9e75fd9f88d76a6a8f13`
  - `tests/enforcement/test_python_ruff_parity.py`:
    `27cd8906ce6472889e5ac64f13c487a612aef54e943a4476901988778a0b933d`
  - `docs/python-checks.md`:
    `efd6e01009af8c09dd435de29eb2e821affa58d0f5151c53fb3a089adf8063f5`
- **Environment:** CPython 3.14.7 (`.venv`), pytest 9.1.1, pytest-xdist 3.8.0,
  Ruff 0.17.1, Linux container, Docker absent.

## Change covered

- Added `vendor` and `generated` to `EXCLUDED_DIRECTORY_NAMES`; all existing
  cache and VCS exclusions remain.
- Changed the recorded exclusion reason to
  `vendor, generated or cache directory; not scanned`.
- Added a physical-directory fixture test creating real `vendor/` and
  `generated/` directories, including nested Python files, beneath each scoped
  root (`docker/`, `tests/`, `scripts/`). The test asserts the files are absent
  from the selection, every excluded directory has a non-empty recorded reason,
  and neighboring ordinary Python files remain selected.
- Retained the existing directory-symlink test unchanged; it continues to
  verify separately that directory symlinks are not followed.
- Updated the source-selection guide to list the two new excluded directories.

## Verification commands and results

All commands ran from the repository root. Complete stdout/stderr was captured
to temporary logs while diagnosing; those container-local paths are not durable
evidence links. The consolidated five-module focused run in phase-2 passed 82
tests on this same state: 28 selector, 28 lifecycle and 26
compilation/parity/status.

| # | Command | Exit status | Result |
| --- | --- | --- | --- |
| 1 | `.venv/bin/python -m pytest tests/enforcement/test_python_source_selection.py -n 4 --dist loadfile --tb=short -q` | **0** | 28 passed in 0.48 s |
| 2 | `.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q` | **0** | 7991 passed, 13 skipped, 18026 subtests passed, 11 warnings in 53.55 s |
| 3 | `scripts/check-types` | **0** | `All checks passed!` |
| 4 | `scripts/check-python-compile` | **0** | no output; every selected source compiled without bytecode or application execution |
| 5 | `.venv/bin/ruff check --no-cache scripts/python_checks.py tests/enforcement/test_python_syntax_compilation.py tests/enforcement/test_python_source_selection.py tests/enforcement/test_python_ruff_parity.py` | **0** | `All checks passed!` |
| 6 | `.venv/bin/ruff format --check --no-cache scripts/python_checks.py tests/enforcement/test_python_syntax_compilation.py tests/enforcement/test_python_source_selection.py tests/enforcement/test_python_ruff_parity.py` | **0** | `4 files already formatted` |
| 7 | `.venv/bin/python scripts/python_checks.py list` | **0** | 396 selected paths, 69 recorded exclusions |

The 11 full-suite warnings were `DeprecationWarning` messages about
`multiprocessing` forking a multi-threaded process from
`tests/test_constructor_build_generation_integration.py`,
`tests/test_transactions_locking.py` and
`tests/test_constructor_build_transactions.py`. No warning was suppressed.

## Limitations

- The full-inventory Ruff lint and format gates remain scheduled for P5 and P3
  respectively. The consolidated phase-2 run records their current nonzero
  diagnostic statuses (1304 findings; 371 files would be reformatted) as
  backlog, not as passes; this report additionally confirms Ruff lint and
  format are clean on the four Python files exercised above.
- The repository currently contains no physical `vendor/` or `generated/`
  directory beneath the scoped roots, so command 7 emitted no such repository
  exclusion. The new behavior is exercised by the isolated fixture test in
  command 1.
- `scripts/check-dockerfile` was not applicable: this correction does not
  modify `Dockerfile`. Docker is also absent in this environment.
- The current P2 source state is uncommitted; these corrections' source edits
  were unstaged at verification time. This session performed no `git add`,
  unstage, reset, checkout or commit. The fingerprints and digests above
  identify the current content.

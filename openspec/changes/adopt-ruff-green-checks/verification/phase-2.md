# Phase 2 exit report — shared tooling

> **Consolidated current state:** this report includes the post-exit
> vendor/generated directory-exclusion, Ruff parity, mode-coverage and
> source-tree bytecode corrections and is the authoritative P2 exit evidence.
> All accepted results below were rerun on the current source state identified
> in the header. [`p2-exclusion-correction.md`](p2-exclusion-correction.md) is
> retained as supporting detail and refers to the same state.

- **Change:** `adopt-ruff-green-checks`
- **Schema:** `spec-driven`
- **Phase:** P2 (shared tooling) only. P3–P7 are not started.
- **Exit source-state fingerprint:** test-input SHA-256
  `e513ebf21468fdfa61ed99c250a8b2ddf7cabd454fa55bcafb8a5ab63620148a`
  (324 files under `docs/` + `tests/`); production SHA-256
  `dd0bb36a5a0d47d0786f3073172978d96d1325a9211a7321d28503f6babc46e4`
  (122 files under `docker/`, **unchanged from P1**); combined scoped SHA-256
  `a00d459dcb69c8055b2aa80489c24bb6143c702d00d7eed178b5185cd51457f0`
  (460 files under `docker/`, `docs/`, `tests/`, `scripts/`). Method: the
  sorted-path-plus-content-digest command recorded in
  [`phase-1.md`](phase-1.md), re-run on this source state; caches excluded and
  `openspec/` outside the fingerprint. The fingerprints describe the current
  P2 source content: `HEAD` `6b24592effa121a22261ef9833accd8ff754a497` plus
  the P2 working-tree changes. At verification time these corrections' source
  edits were unstaged; the content fingerprint is independent of index state
  and identifies the same source when staged unchanged.
- **Environment:** CPython 3.14.7 (`.venv`), pytest 9.1.1, pytest-xdist 3.8.0,
  Ruff 0.17.1, uv 0.12.24, Linux container, Docker absent.
- **Verdict:** P2 accepted on the current source state. All P2 deliverables are
  present, R exits 0, the existing required checks and shared compilation
  succeed, and the lint/format runs are recorded as migration backlog with
  their actual nonzero status — not as passes.

## Deliverables

| Deliverable | File |
| --- | --- |
| Ruff declaration and fixed policy | [`pyproject.toml`](../../../../pyproject.toml) (`[dependency-groups].dev`, `[tool.ruff]`, `[tool.ruff.lint]`) |
| Shared selector, lint/format/compile entry points | [`scripts/python_checks.py`](../../../../scripts/python_checks.py), `scripts/check-python-lint`, `scripts/check-python-format`, `scripts/check-python-compile` |
| Focused tooling tests | [`tests/enforcement/test_python_source_selection.py`](../../../../tests/enforcement/test_python_source_selection.py), [`test_python_syntax_compilation.py`](../../../../tests/enforcement/test_python_syntax_compilation.py), [`test_python_ruff_parity.py`](../../../../tests/enforcement/test_python_ruff_parity.py), [`test_python_check_status.py`](../../../../tests/enforcement/test_python_check_status.py), [`test_python_check_lifecycle.py`](../../../../tests/enforcement/test_python_check_lifecycle.py), fixture helper [`python_check_fixtures.py`](../../../../tests/enforcement/python_check_fixtures.py) |
| Contributor guide | [`docs/python-checks.md`](../../../../docs/python-checks.md), linked from [`docs/testing.md`](../../../../docs/testing.md) |
| Updated check manifest | [`check-manifest.md`](check-manifest.md) (`new-adoption-checks` now concrete; `existing-required-checks` unchanged) |
| P2 source inventory snapshot | [`source-inventory-p2.txt`](source-inventory-p2.txt) (396 paths) |
| P2 Ruff finding inventory | [`ruff-findings-p2.txt`](ruff-findings-p2.txt) (1304 findings) |
| P2 exit summary | this file |

## RED (tasks 2.1–2.4)

Command:

```bash
.venv/bin/python -m pytest \
  tests/enforcement/test_python_source_selection.py \
  tests/enforcement/test_python_syntax_compilation.py \
  tests/enforcement/test_python_ruff_parity.py \
  tests/enforcement/test_python_check_status.py \
  -n 4 --dist loadfile --tb=line -q
```

Actual result before implementation: **exit 1 — 2 failed, 29 errors in 0.37 s**.
The recorded gaps were real: `FileNotFoundError` for the missing
`scripts/python_checks.py` (all selector/compile/parity/status cases),
`FileNotFoundError` for the missing `scripts/check-python-compile` entry point,
and `KeyError: 'dependency-groups'` because no Ruff group or policy was
declared. No working code was deliberately broken and no artificial failure was
injected.

### 2.1 Source-selector contracts — added

[`test_python_source_selection.py`](../../../../tests/enforcement/test_python_source_selection.py)
covers nested `.py`/`.pyi`, recognized extensionless Python shebangs (direct
`python`/`python3`/`python3.<minor>` paths and `/usr/bin/env` plus one of those
names), rejected shebang boundaries (`#!/bin/notpython`, `python2`,
`env python3x`, `env -S python3`, bare `env`, `#!`), explicit non-shebang
entries (and the unclassified-without-shebang error), shell exclusion,
non-Python-extension exclusion, each with a recorded reason, a directory
symlink that is not followed, a symlinked `.py` that is an error, an unreadable
extensionless candidate, filenames with spaces, cache-directory exclusion,
physical `vendor`/`generated` directories beneath every scoped root (with
nested files), sorted/unique/repeatable output and a missing scoped root.

### 2.2 Compilation contracts — added

[`test_python_syntax_compilation.py`](../../../../tests/enforcement/test_python_syntax_compilation.py)
covers valid extensionless Python, `.pyi` sources, invalid syntax reported with
the repository-relative path, a latin-1 source-encoding declaration, absence of
application side effects, absence of bytecode/cache output, source-content
preservation and an unreadable selected source. Two subprocess regressions copy
the Docker package and entry points into a clean temporary repository with no
bytecode caches, remove `PYTHONDONTWRITEBYTECODE` and `PYTHONPYCACHEPREFIX`, and
run both the documented wrapper and the direct CLI. Each must exit 0 without
creating any `.pyc` or `__pycache__` entry; the original six imported-module
bytecode files make both cases fail when the guard is removed.

### 2.3 Ruff inventory-parity contracts — added

[`test_python_ruff_parity.py`](../../../../tests/enforcement/test_python_ruff_parity.py)
exposes a selected module hidden by a `[tool.ruff] exclude` entry (parity error
and `lint` status 1) and a selected extensionless script absent from default
`ruff check --show-files` discovery but still checked by the runner. A real-Ruff
end-to-end fixture places lint-invalid `.py` files beneath `vendor/` and
`generated/` in all scoped roots, proves Ruff still discovers them, proves
parity ignores those deliberately out-of-scope paths, and proves `lint` exits
0 without checking them. Two further end-to-end regressions cover mode-specific
runtime coverage: a `force-exclude`d extensionless script and an unformatted
module hidden by `[tool.ruff.format].exclude` must both name the omitted path,
exit 1 before claiming success, and leave source digests unchanged. The module
also pins the real-repository parity result to zero errors.

### 2.4 Check-runner status contracts — added

[`test_python_check_status.py`](../../../../tests/enforcement/test_python_check_status.py)
asserts lint status **1** for an undefined name, format status **1** for an
unformatted module, syntax status **1** for invalid syntax, status **0** for
clean cases, unchanged source digests and no `.ruff_cache`, plus 127 for a
missing Ruff. The shell entry point is executed end-to-end and preserves the
underlying status **1**.

## GREEN (tasks 2.5–2.12)

### 2.5 Development Ruff 0.17.1 — declared

`[dependency-groups].dev = ["ruff==0.17.1"]`. Clean-environment check:

```bash
uv venv --python 3.14 <tmp>/venv
uv pip install --python <tmp>/venv/bin/python --group dev
<tmp>/venv/bin/ruff --version
```

Both commands exited **0**; `ruff 0.17.1` and
`importlib.metadata.version("ruff") == "0.17.1"`. Installing the group into the
project `.venv` (`uv pip install --python .venv/bin/python --group dev`) exited
**0**. The runtime inventory is unchanged: `pyproject.toml` declares no runtime
dependencies, `docker/` digests are identical to P1, and the installed tool
list contains only pytest/pytest-xdist support packages and `ruff 0.17.1`.

### 2.6 Fixed Ruff policy — configured

`required-version = "==0.17.1"`, `target-version = "py314"`, and
`[tool.ruff.lint] select = ["E4", "E7", "E9", "F", "I", "B", "SIM115", "RUF100"]`.
No `preview`, `unsafe-fixes`, `extend-select`, `ignore` or `ALL`. Focused tests
read the declaration and the effective settings (`ruff check --show-settings`):
`linter.unresolved_target_version = 3.14`, `linter.preview = disabled`,
`formatter.preview = disabled`, and every enabled rule code belongs to the
selected policy (both `SIM115` and `RUF100` present).

### 2.7 Shared source selection — implemented

`select_sources()` walks `docker/`, `tests/`, `scripts/` deterministically,
selects `.py`/`.pyi`, extensionless files with a recognized Python shebang and
explicit `EXPLICIT_PYTHON_SOURCES` entries, records every exclusion reason, and
raises `SourceSelectionError` for an unreadable/unclassified candidate, a
missing root or a symlinked Python source. The excluded-directory set includes
`vendor` and `generated` as well as cache/VCS directories; a physical-directory
fixture proves nested files under those names are omitted while each directory
retains a nonempty exclusion reason.

A shebang is recognized only when the interpreter is a direct path whose final
component is `python`, `python3` or a versioned `python3.<minor>` name, or when
`/usr/bin/env` is followed by one of those names. The earlier substring check
(`python` anywhere in the line) was replaced by this name boundary; the change
is preventive because no current file was misclassified. Files with any other
extension now also record an explicit exclusion reason. Reconciliation with
P1:

```bash
.venv/bin/python scripts/python_checks.py list > <p2 list>
LC_ALL=C comm -23 <p1 source-inventory.txt> <p2 list>   # empty
LC_ALL=C comm -13 <p1 source-inventory.txt> <p2 list>   # 6 new .py files
```

All 389 P1 paths remain selected; the only additions are this phase's six new
`.py` files (`scripts/python_checks.py` and five test modules). The three new
`scripts/check-python-*` entry points are shell scripts and are therefore
classified as non-Python with a recorded reason, like `scripts/check-types`.
The snapshot is [`source-inventory-p2.txt`](source-inventory-p2.txt): 396
paths = 393 `.py` + 3 extensionless Python scripts, sorted and unique.

### 2.8 Non-mutating lint checking — implemented

`scripts/check-python-lint` runs
`ruff check --no-cache -- <shared inventory>` through one `OwnedProcess` and
returns Ruff's status. It first requires zero default-discovery inventory
parity errors. Out-of-scope Ruff discoveries beneath deliberately excluded
directories do not count as parity errors, while a selected file missing from
discovery still does. It then validates lint coverage by running
`ruff check --show-files` over the explicit inventory, so extensionless scripts
are covered and `force-exclude` cannot silently omit them. Focused lint cases
in 2.3–2.4 pass; sources are unchanged after a failing run.

### 2.9 Non-mutating format checking — implemented

`scripts/check-python-format` runs
`ruff format --check --no-cache -- <shared inventory>`; no write mode is
reachable. Before that run, formatter coverage uses the read-only
`ruff format --check --verbose` processed-file report so `force-exclude` and
`[tool.ruff.format].exclude` cannot silently omit a selected path. Format cases
in 2.3–2.4 pass and digests are unchanged.

### 2.10 Shared syntax compilation — implemented

`compile_sources()` reads each selected file through one `OwnedDescriptor`
(`O_RDONLY|O_NOFOLLOW|O_CLOEXEC`, `dir_fd`-relative) and calls
`compile(source_bytes, relative, "exec", dont_inherit=True)`. Encoding
declarations are honoured, nothing is imported or executed, and no bytecode is
written. The tooling sets `sys.dont_write_bytecode = True` before importing
`docker.filesystem` and `docker.process`, so those support imports cannot
leave `.pyc` files beneath the source tree during direct CLI or wrapper
execution. The 2.2 and 2.4 syntax cases pass.

### 2.11 Contributor check guide — written

[`docs/python-checks.md`](../../../../docs/python-checks.md) documents the
installation commands, the exact inventory rules, the three entry points with
their activation phases, the read-only guarantees, `--root`, and the
temporary-migration-backlog distinction. It is linked from
[`docs/testing.md`](../../../../docs/testing.md) (README links are P6 work).

### 2.12 Manifest entry points — recorded

[`check-manifest.md`](check-manifest.md) now records the exact invocations
(`scripts/check-python-compile`, `scripts/check-python-format`,
`scripts/check-python-lint`) with activation P2/P3/P5 and the P2 diagnostic
statuses. The `existing-required-checks` section (E1, E2, E4) is preserved
without narrowing any command or scope.

## INTROSPECT (tasks 2.13–2.15)

### 2.13 Helper review against repository contracts — accepted

Review findings, recorded as the required ownership map:

| Concern | Ownership path used | Reason |
| --- | --- | --- |
| Directory traversal | `DirectoryDescriptor.open_secure_path(..., require_owner=False)`, `open_directory`, `list_names`, `stat_child(follow_symlinks=False)`, `with child:` | Read-only tree walk; no-mutation capability child opens; the read-only ownership relaxation matches `docker/npm_environment/tree.py` and grants no mutation authority. |
| Regular-file read | `os.open(name, O_RDONLY|O_NOFOLLOW|O_CLOEXEC, dir_fd=directory.fd)` adopted immediately into `OwnedDescriptor(PosixDescriptorOps(), fd, label=...)`; data via `os.read(owner.fd)`; release via `with owner:` | One owner per open; `closefd`-style file wrappers are not used; the owner is the sole release authority. |
| Ruff invocation | `OwnedProcess.launch(..., popen_kwargs={"cwd": ...})` with exactly one `capture()` or `stream()` attempt; no direct child wait/kill/close and no pipe adoption | The process owner retains one `OwnedStream` per pipe and owns shutdown/cleanup; status is read from the result, and lifecycle failures are preserved in authoritative order (`CommandResult.failures`) with `CommandResult.clean` derived from completeness and failure absence. |
| Status mapping | `run_ruff_check` returns the child status; incomplete/failed lifecycle maps to failure, never success | Underlying status is preserved, never masked by streaming or a wrapper. |
| Shell vs Python | Entry points are `/bin/sh` shims whose only work is selecting `.venv/bin/python` and `exec`-ing `scripts/python_checks.py <mode>` | Not chosen to avoid contracts: the shell layer owns no descriptor/process; the Python layer that does own descriptors/processes follows the filesystem/process contracts. |

Required lifecycle tests live in
[`test_python_check_lifecycle.py`](../../../../tests/enforcement/test_python_check_lifecycle.py)
and now cover all three ownership paths (28 tests). The earlier revision only
covered regular-file reads; the review was reopened for directory traversal and
the `run_captured`/`run_streaming` wrappers.

**Regular-file reads (8 tests)**

| Required case | Test |
| --- | --- |
| Operation-plus-release failure (primary preserved) | `test_read_failure_stays_primary_over_a_close_failure` |
| Sole release failure | `test_sole_close_failure_is_reported` |
| Interruption identity | `test_close_interruption_identity_is_preserved` |
| Repeat cleanup (no second release) | `test_repeat_cleanup_after_a_failed_close_is_not_retried` |
| Independent cleanup | `test_independent_reads_release_each_descriptor_once` |
| Open-failure mapping | `test_open_failure_maps_to_a_selection_error` |
| Failed command not reported clean | `test_failed_command_is_not_reported_as_clean` |
| Unavailable executable is a discovery error | `test_unavailable_executable_is_a_discovery_error` |

**Directory traversal (7 tests, real `DirectoryDescriptor` owners)**

| Required case | Test |
| --- | --- |
| Traversal-plus-directory-release failure (primary preserved) | `test_traversal_failure_stays_primary_over_directory_release_failure` |
| Sole directory-release failure reported | `test_directory_release_failure_is_reported` |
| Release interruption identity with parent release | `test_directory_release_interruption_keeps_identity_and_parent_is_released` |
| Repeat directory cleanup (no second release) | `test_repeated_directory_cleanup_does_not_retry_a_failed_release` |
| Child-release failure does not prevent parent release | `test_child_directory_release_failure_does_not_prevent_parent_release` |
| Listing failure is a bounded selection error | `test_directory_listing_failure_is_a_bounded_selection_error` |
| Source-root release failure is reported | `test_source_root_release_failure_is_reported` |

**Process wrappers (13 tests, each captured/streaming case parameterized)**

| Required case | Test |
| --- | --- |
| Primary collection failure before release failure | `test_process_wrapper_keeps_primary_failure_before_release_failure` (captured and streaming) |
| Cleanup-only failure cannot be clean | `test_process_wrapper_reports_cleanup_only_failure` (captured and streaming) |
| Collection interruption identity | `test_process_wrapper_keeps_collection_interruption_identity` (captured and streaming) |
| Owner cleanup interruption identity | `test_process_wrapper_keeps_owner_cleanup_interruption_identity` (captured and streaming) |
| Incomplete cleanup never clean; observed via `cleanup_status`/`wait_cleanup` | `test_process_wrapper_never_reports_incomplete_cleanup_as_clean` (captured and streaming) |
| Real owner release failure; same owner, no repeated release | `test_real_owner_cleanup_failure_is_not_clean_and_repeats_no_release` (captured and streaming) |
| Streaming callback interruption survives cleanup failure | `test_streaming_callback_interruption_survives_cleanup_failure` |

The expanded matrix exposed two propagation/reporting gaps, fixed within
ownership, failure propagation and cleanup reporting only (no new owner, close
or retry):

- A raw `OSError` from directory listing or capability release escaped the
  bounded `SourceSelectionError` contract; those paths are now wrapped at the
  traversal/release boundary while preserving the original cause and any
  secondary release diagnostic (child, source-root and listing paths).
- `CommandResult` dropped the process owner's lifecycle failures; it now keeps
  a defaulted `failures` tuple in authoritative order, so a cleanup-only or
  mixed failure cannot collapse to a bare status.

Reopened RED evidence: a temporary scratch copy of `scripts/python_checks.py`
with only the two release-wrapping blocks and the `failures` field reverted was
exercised directly (never replacing the working-tree file). A close-only child
directory failure raised a raw `OSError` (`Errno 5, injected directory close
failure`) instead of the bounded `SourceSelectionError`, and the summary
dataclass exposed only `clean`, `returncode`, `stderr`, `stdout` with no
failure record. Those are the two expectations the new tests assert; the
working tree then implemented only the bounded wrap and the failure-preserving
field.

No ownership behaviour was changed to manufacture failures; the new tests drive
the real owners except the deliberately incomplete-cleanup observation, which
uses a documented-contract stub because the real owner completes
deterministically on this platform. The new tooling files are clean under the
declared Ruff policy and under the existing type check, which does not include
`scripts/`.

### 2.14 Initial full Ruff findings — captured

Command (recorded one-off; uses the shared selection and concise output):

```bash
.venv/bin/python - <<'PY'
import importlib.util, re, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("python_checks", "scripts/python_checks.py")
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)
root = Path.cwd()
selection = m.select_sources(root)
result = m.run_captured(
    [m.find_ruff(), "check", "--no-cache", "--output-format", "concise",
     "--", *selection.selected],
    root=root,
)
pattern = re.compile(r"^.+?:\d+:\d+: [A-Z]+\d+ ")
findings = [line for line in result.stdout.decode().splitlines() if pattern.match(line)]
Path("openspec/changes/adopt-ruff-green-checks/verification/ruff-findings-p2.txt").write_text(
    "\n".join(findings) + "\n", encoding="utf-8"
)
print("exit", result.returncode, "findings", len(findings))
PY
```

Actual result: **exit 1, 1304 findings, 0 parity errors** in the complete
396-file inventory. The complete rule/location inventory is
[`ruff-findings-p2.txt`](ruff-findings-p2.txt). Per-rule counts:

| Rule | Count | Rule | Count | Rule | Count |
| --- | ---: | --- | ---: | --- | ---: |
| I001 | 348 | F401 | 247 | E702 | 236 |
| E402 | 122 | B023 | 79 | B904 | 44 |
| F841 | 42 | F811 | 27 | E701 | 25 |
| RUF100 | 21 | E401 | 21 | B905 | 20 |
| B017 | 16 | F821 | 12 | E731 | 10 |
| E741 | 7 | B007 | 7 | F541 | 6 |
| B018 | 6 | SIM115 | 4 | B010 | 2 |
| B008 | 2 | | | | |

SIM115 locations (retained for P5 review; SIM115 is not an ownership proof):

- `tests/test_constructor_runtime_installer.py:452:15`
- `tests/test_constructor_runtime_installer.py:2973:11`
- `tests/test_transactions_phase7_runtime_projection.py:283:19`
- `tests/versioning/support/inventory_builder.py:14:9`

This is migration backlog with a nonzero status, not a green lint check. No
finding is suppressed, excluded or treated as a pass.

### 2.15 Version/configuration/source parity — accepted

- Installed `ruff --version` is `0.17.1`; the declared dev group and
  `required-version` both require `==0.17.1`; a different Ruff refuses to run.
- The effective configuration resolves to the repository
  `pyproject.toml` (`Settings path`), target 3.14, preview disabled.
- Documented commands all use one selection: 396 selected paths = 393 Ruff-
  visible `.py` files + 3 extensionless Python scripts, with
  `inventory_parity_errors(...) == ()` on the real repository. No silent
  exclusion exists.
- Ruff discovery still reports `.py` files beneath physical `vendor` and
  `generated` trees. Parity ignores only such intentionally excluded paths
  when they are absent from the selection; a selected module hidden by Ruff
  configuration remains an error.
- Runtime mode coverage verifies that every selected path appears in the
  requested mode's processed set: lint uses explicit-inventory
  `check --show-files`, while format parses the `format_path` entries from
  `format --check --verbose`. An omitted extensionless script or formatter-
  excluded module is named and returns 1 before the success path.

## VALIDATE (tasks 2.16–2.21)

### 2.16 Focused tooling tests — accepted

```bash
.venv/bin/python -m pytest \
  tests/enforcement/test_python_source_selection.py \
  tests/enforcement/test_python_syntax_compilation.py \
  tests/enforcement/test_python_ruff_parity.py \
  tests/enforcement/test_python_check_status.py \
  tests/enforcement/test_python_check_lifecycle.py \
  -n 4 --dist loadfile --tb=short -q
```

Result: **exit 0 — 82 passed in 1.03 s**. All source, compilation, parity,
policy, status and expanded lifecycle fixtures pass (28 lifecycle tests; 28
selector tests; 26 compilation/parity/status tests).

### 2.17 R — mandated full pytest — accepted

Command:
`.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q`

Result: **exit 0 — 7991 passed, 13 skipped, 11 warnings, 18026 subtests
passed in 53.55 s**. Collection rose from 7909 to 7991 passed tests (+82 new
tooling tests: 28 selector, 28 lifecycle and 26 compilation/parity/status); no
test was removed, skipped or weakened. Warnings remain the unsuppressed CPython
multiprocessing/fork `DeprecationWarning` families. A separate reporting run
(`-q -rs`, exit 0, same 7991/13 counts and reasons, 53.43 s) supplied the skip
reasons; it did not replace the mandated command. All results in this report
refer to the current fingerprint above; earlier intermediate runs were
superseded by these final runs after the lifecycle, selector-hardening,
vendor/generated-exclusion, Ruff parity, mode-coverage and source-tree
bytecode changes.

### 2.18 Manifest checks mandatory in P2 — accepted for scope

| Check | Exact command | Exit | Result |
| --- | --- | ---: | --- |
| E1 mandated pytest | `.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q` | 0 | 7991 passed, 13 skipped, 11 warnings, 18026 subtests |
| E2 type check | `scripts/check-types` | 0 | `All checks passed!`; no unresolved ty diagnostics (local ty 0.0.85; CI pin 0.0.73 remains a P6/P7 verification item) |
| E4 Dockerfile gate | `scripts/check-dockerfile` | not run | Docker absent (`docker: command not found`) and `Dockerfile` is unchanged; documented environmental limitation, not a pass |
| Shared compilation (active in P2) | `scripts/check-python-compile` | 0 | no diagnostics for the 396-path inventory |
| Full-inventory format check (diagnostic in P2) | `scripts/check-python-format` | 1 | 371 files would be reformatted, 25 already formatted — **P3 backlog, not a pass** |
| Full-inventory lint (diagnostic in P2) | `scripts/check-python-lint` | 1 | 1304 findings — **P5 backlog, not a pass** |

All rows above were rerun on the current source state. The lint and format
results remain migration backlog with their actual nonzero statuses.

### 2.19 Shared compilation for R — accepted

`scripts/check-python-compile` exited **0** with no output for the complete
396-path inventory (success produces no stdout/stderr). The clean-copy
subprocess regressions additionally confirm that both the wrapper and direct
CLI load repository support modules without creating bytecode.

### 2.20 Phase-exit skip review — accepted

The current full run reports the same 13 skips with the same reasons as the
reviewed P1 inventory (three `docker-dev` account prerequisites, one
`sudo runuser -u nobody` prerequisite, eight explicit Docker-backed
opt-in/standalone-runner conditions, one `NPM_ENV_REAL_DOCKER=1` opt-in). No
new skip, xfail/xpass or collection reduction was introduced; no count is
frozen.

### 2.21 P2 exit report — published

All P2 deliverables exist, all P2 tasks are accepted above, and R, the existing
required checks, shared compilation and the vendor/generated, Ruff parity,
mode-coverage and source-tree bytecode corrections refer to the current
source-content fingerprint recorded at the top of this report. The lint/format
diagnostics are backlog with actual nonzero statuses.


## Review follow-up: selector, exclusion, parity, coverage and bytecode — accepted

After the lifecycle matrix close-out, reviews asked for two preventive selector
corrections, one exclusion-list correction, one Ruff parity correction, one
mode-specific coverage correction and one source-tree bytecode correction. No
current repository file was misclassified; the current `scripts/` classification
is unchanged at five Python files (two `.py`, three shebang scripts) and nine
shell scripts.

- Non-Python extensions under the scoped roots now append an `Exclusion` with
  the repository-relative path and reason before continuing; they are no
  longer silently skipped.
- The substring shebang marker was removed. Recognition is now limited to a
  direct path whose final component is `python`, `python3` or
  `python3.<minor>`, or `/usr/bin/env` followed by one of those names. There is
  no shell parsing, no interpreter execution and no `libmagic`; additional
  `env` options are not interpreted.
- `vendor` and `generated` are now excluded alongside cache/VCS directories,
  each with a recorded reason. A physical-directory fixture beneath every
  scoped root covers direct and nested Python files; the separate
  directory-symlink test remains unchanged.
- `inventory_parity_errors()` now ignores Ruff-discovered paths beneath those
  deliberately excluded directories. The end-to-end parity fixture uses real
  Ruff, puts lint-invalid files under `vendor/` and `generated/`, confirms Ruff
  discovers them, confirms parity reports no error, and confirms `lint` exits
  0. A selected file hidden by Ruff configuration is still rejected.
- Mode-specific coverage now proves Ruff processed every selected path. Lint
  uses explicit-inventory `check --show-files`, so extensionless scripts are
  included and `force-exclude` cannot hide them. Format uses the read-only
  `format --check --verbose` `format_path` report, so `[tool.ruff.format].exclude`
  cannot hide an unformatted module. Regression tests name the omitted path,
  require exit 1, and verify unchanged source digests.
- The tooling now sets `sys.dont_write_bytecode = True` before importing its
  repository support modules. Clean-copy subprocess regressions run both
  `scripts/check-python-compile` and direct `scripts/python_checks.py compile`
  with bytecode-suppression environment variables removed. Each exits 0 and
  leaves no `.pyc` or `__pycache__` entry. Removing the guard during mutation
  verification made both cases fail with the two generated cache directories
  and six `.pyc` files that caused the reported clean-copy defect.

Fresh commands on the current source state:

```bash
.venv/bin/python -m pytest tests/enforcement/test_python_source_selection.py \
  -n 4 --dist loadfile --tb=short -q
.venv/bin/python -m pytest \
  tests/enforcement/test_python_source_selection.py \
  tests/enforcement/test_python_syntax_compilation.py \
  tests/enforcement/test_python_ruff_parity.py \
  tests/enforcement/test_python_check_status.py \
  tests/enforcement/test_python_check_lifecycle.py \
  -n 4 --dist loadfile --tb=short -q
.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q
.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q -rs
scripts/check-types
scripts/check-python-compile
scripts/check-python-format
scripts/check-python-lint
.venv/bin/python scripts/python_checks.py list
```

| Command | Exit | Observed result |
| --- | ---: | --- |
| selector tests | 0 | 28 passed in 0.48 s |
| focused tooling tests | 0 | 82 passed in 1.03 s |
| mandated full pytest | 0 | 7991 passed, 13 skipped, 11 warnings, 18026 subtests in 53.55 s |
| skip reporting run (`-rs`) | 0 | same 7991/13 counts and reasons in 53.43 s |
| `scripts/check-types` | 0 | `All checks passed!` |
| `scripts/check-python-compile` | 0 | no output (396-path inventory) |
| `scripts/check-python-format` | 1 | 371 would be reformatted, 25 already formatted — P3 backlog, not a pass |
| `scripts/check-python-lint` | 1 | 1304 findings, 653 fixable — P5 backlog, not a pass |
| `scripts/python_checks.py list` | 0 | 396 selected; `scripts/` = 5 selected + 9 non-Python shebang exclusions |

Regenerated [`source-inventory-p2.txt`](source-inventory-p2.txt) has 396 paths
and [`ruff-findings-p2.txt`](ruff-findings-p2.txt) has 1304 findings; both now
diff-match the current source state exactly. The header fingerprints were
recomputed on that state. Earlier intermediate runs — including the
pre-hardening, pre-exclusion-correction, pre-parity-correction,
pre-coverage-correction and pre-bytecode-correction counts — are historical and
are superseded by the values above.

## Limitations and disclosures

- The current P2 source state is uncommitted; the source edits from these
  corrections were unstaged at verification time. This session performed no
  `git add`, unstage, reset, checkout or commit. The header fingerprint covers
  the 460 scoped files and is independent of index state.
- Local Ruff runs do not prove CI-pinned behavior; the CI environment and the
  ty pin 0.0.73 are verified in P6/P7. No remote GitHub run was performed in
  P2.
- `scripts/check-dockerfile` was not executed because Docker is absent; its
  scope (`Dockerfile`) is untouched by this phase.
- The lint and format diagnostics are migration backlog for P3–P5. They are
  explicitly not green checks, and no file, rule or failure was suppressed to
  change their status.
- The incomplete-cleanup wrapper case uses a documented-contract owner stub
  because the real owner's shutdown completes deterministically for these
  children on this platform; the real owner is used for the release-failure,
  interruption and repeated-cleanup cases. The stub covers only wrapper
  mapping/observation, not internal owner cleanup.
- Directory and process fault injection monkeypatches the ownership adapter
  (`PosixDescriptorOps.statat`/`listdir`, `os.close`) or the shared stream
  release (`OwnedStream.close`); it does not replace the owner implementation,
  so the production cleanup and precedence paths are exercised.

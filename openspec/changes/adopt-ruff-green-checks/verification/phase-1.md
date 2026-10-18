# Phase 1 exit report — baseline and enforcement preparation

- **Change:** `adopt-ruff-green-checks`
- **Schema:** `spec-driven`
- **Phase:** P1 (baseline/preparation) only. P2–P7 are not started.
- **Current exit source-state fingerprint:** test-input SHA-256
  `276edd8e5526e0f9e0fcccb647b57158f7a4a767ac09415b420bd95b4fae72c9`
  (317 files under `docs/` + `tests/`, including the test-owned
  `tests/fixtures/process-architecture-inventory.json`); production SHA-256
  `dd0bb36a5a0d47d0786f3073172978d96d1325a9211a7321d28503f6babc46e4`
  (122 files under `docker/`); combined scoped SHA-256
  `b5d743e7cb1cfb2d338dfedb11fa6221fe2e128405f817ffbbd6daa46d355255`
  (449 files under `docker/`, `docs/`, `tests/`, `scripts/`). Method and exact
  command in the re-verification section.
- **Historical exit fingerprint (superseded):** HEAD
  `a573bb9fc49a4deaf3f6c4461d0df60b6a247a4d`; test-input delta SHA-256
  `7cc47137f6db61c39b9defc4077d0122aa3003d9ac3176b2363b6a4a4303c37b`
  (computed over `docs/`, `tests/` and the then-active-change JSON fixture; see
  the closing note). It describes only the pre-revision source state.
- **Verdict:** P1 accepted on the current source state. All concrete preparation
  work orders and deliverables are present; R exits 0; the only unexecuted
  existing command is a Dockerfile-scoped gate whose Docker prerequisite is
  documented as absent and whose scope this change does not touch.

## Deliverables

| Deliverable | File |
| --- | --- |
| Source inventory (389 paths) | [`source-inventory.txt`](source-inventory.txt) |
| Source classification/exclusions | [`source-inventory.md`](source-inventory.md) |
| Two-section check manifest | [`check-manifest.md`](check-manifest.md) |
| Environment prerequisites | [`environment-prerequisites.md`](environment-prerequisites.md) |
| Baseline/exit skip inventory | [`skip-inventory.md`](skip-inventory.md) |
| Layout-sensitive assertion inventory | [`layout-assertion-inventory.md`](layout-assertion-inventory.md) |
| Preparation work-order ledger (RED/GREEN) | [`preparation-ledger.md`](preparation-ledger.md) |
| Enforcement-to-Ruff map | [`enforcement-ruff-map.md`](enforcement-ruff-map.md) |
| Canonical architecture/pipe inventory (test-owned) | [`tests/fixtures/process-architecture-inventory.json`](../../../../tests/fixtures/process-architecture-inventory.json) |
| P1 exit summary | this file |

## Baseline (task 1.4) — historical

This baseline describes the pre-revision source state only. Current gate results
are in the re-verification section.

Command:
`.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q`

Result: **exit 1** — **1 failed, 7898 passed, 13 skipped, 11 warnings,
18026 subtests passed**. The failure was the stale archived inventory path
recorded in `P1-A0`/`P1-A11`. It is reported as an unsuccessful baseline, not a
pass. The baseline skip reasons are inventoried in `skip-inventory.md`.

## Task acceptance

### 1.1 Source inventory — accepted

389 paths (386 `.py` + 3 extensionless Python scripts) are listed sorted and
unique in `source-inventory.txt`. Each scoped root, every extensionless
candidate and every exclusion has a recorded classification/reason in
`source-inventory.md`. Unreadable candidates would abort generation (none
found). Cache/generated directories and `openspec/**` are excluded; directory
symlinks are not followed (none present).

### 1.2 Two-section check manifest — accepted

`check-manifest.md` records E1–E4 with exact invocation, source configuration
and required scope, and an explicit pending-P2 `new-adoption-checks` section.
`pi-green-loop.json` is absent, so there is no separate production ty command
beyond `scripts/check-types`; E2 keeps the full `docker tests/typing` scope.
E3 is the conditional `unittest discover` command defined by
[`docs/testing.md`](../../../../docs/testing.md) — run only when a task
explicitly requires it, never instead of E1. Hadolint (E4) is recorded with
its Docker-only scope.

### 1.3 Environment-prerequisite manifest — accepted

`environment-prerequisites.md` names tools/versions, provisioning commands and
limitations for every existing check, including the absent Docker
prerequisite and the local/CI ty version difference. Missing prerequisites are
not represented as passes.

### 1.4 Baseline full pytest — accepted

Entry recorded above with the actual exit status and counts.

### 1.5 Baseline skip inventory — accepted

All 13 skip IDs, reasons and applicability conditions are recorded with
dispositions; no fixed skip count is frozen.

### 1.6 Layout-sensitive assertion inventory — accepted

`layout-assertion-inventory.md` lists `P1-A0`…`P1-A12`, each with the
assertion, protected invariant and harmless mutation, including the
secure-walker `descriptors.py:239` expectation. Recorded no-change
dispositions cover the remaining mutation sites whose proofs are already
AST-based and whose snippets the probe left intact.

### 1.7 Preparation work orders instantiated — accepted

`preparation-ledger.md` instantiates each RED/GREEN instance with one target,
action, prerequisite, expected output, exact verification and before/after
result. All RED instances were executed before GREEN; no instance says "and
other findings".

### 1.8 Enforcement-to-Ruff map — accepted

`enforcement-ruff-map.md` classifies every enforcement module/contract as
retained, with concrete rule overlap and retained negative fixtures. No rule is
enabled and no check is deleted in P1. No demonstrably equivalent candidate
exists in the agreed rule set.

### 1.9 Assertion-preparation documentation — accepted

Every changed assertion links to an accepted work order in the ledger; no
unreviewed assertion change remains. The architecture fingerprint preserves
docstring text; `P1-A12` adds a focused mutation proving a docstring-content
change invalidates the fixture (while a formatter-only reindent does not).
Changes are confined to `tests/enforcement/`, three lifecycle documents' test
links and this change's verification artifacts.

### 1.10 INTROSPECT — P1 source diff — accepted

`git status` for the prepared delta contains **no `docker/` production source
changes** and no `Dockerfile`, `pyproject.toml`, `pi-green-loop.json`,
dependency, stress-size or test-fixture reduction. Test collection increased by
10 tests relative to the baseline (7912 collected incl. failures/skips → 7922
collected incl. skips): 9 focused layout tests plus the restored inner
completion-predicate case (`P1-A13`); no test was removed. Lifecycle ownership,
primary-error, budget and architecture guarantees remain covered by the
retained checks.

### 1.11 INTROSPECT — environment/skip dispositions — accepted

No unresolved blocker remains in the manifest for the Python scope. Docker is a
documented environmental limitation for the Dockerfile-only gate. The archived
descriptor/process migration is not listed as pending: the proposal records it
complete and `openspec/changes/archive/2026-10-18-eliminate-unsafe-direct-descriptor-closes/`
exists.

## Validation (tasks 1.12–1.15)

### 1.12 R — mandated full pytest — accepted

Historical result for the pre-revision prepared state (see the warning about
stale fingerprints in the re-verification section):

Command:
`.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q`

- Baseline (before preparation): exit **1**, 1 failed / 7898 passed / 13
  skipped / 11 warnings / 18026 subtests.
- Prepared exit state: exit **0**, **7908 passed, 13 skipped, 11 warnings,
  18026 subtests passed** in 53.56 s.
- Warnings are the unsuppressed CPython multiprocessing/fork
  `DeprecationWarning` families from the test processes. No warning was
  suppressed and no failing status was summarized into a pass.

Current result after the P1 revision (`P1-A13`, `P1-A14`): exit **0**, **7909
passed, 13 skipped, 11 warnings, 18026 subtests passed** in 53.21 s — see the
re-verification section for the full command set.

### 1.13 Existing checks mandatory in P1 — accepted for scope

Historical results (pre-revision source state). Commands and their required
scope follow [`docs/testing.md`](../../../../docs/testing.md):

| Check | Result |
| --- | --- |
| `scripts/check-types` | exit **0**, `All checks passed!`; no unresolved ty diagnostics. Local ty 0.0.85; CI pin 0.0.73 is recorded as a limitation to verify in P6/P7. |
| `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'` (additional; only when a task explicitly requires it) | exit **0**, `Ran 6465 tests`, `OK (skipped=13)`. |
| E1 mandated pytest | exit **0** (above). |
| `scripts/check-dockerfile` | **not applicable in P1**: Docker is not installed (`docker: command not found`); the check is Dockerfile-scoped and this change does not modify `Dockerfile`. Provisioning is documented. This is a documented environmental limitation, not a success claim. |

The current-state run of the same checks is in the re-verification section.

`new-adoption-checks` (lint/format/compilation entry points) remain pending P2
and are not required in P1.

### 1.14 One-off compilation — accepted

Recorded command (reads the inventory, respects PEP 263 via `tokenize.open`,
does not import application code and writes no bytecode):

```sh
.venv/bin/python - <<'PY'
import tokenize
from pathlib import Path

inventory = Path('openspec/changes/adopt-ruff-green-checks/verification/source-inventory.txt')
paths = [line for line in inventory.read_text(encoding='utf-8').splitlines() if line]
for path in paths:
    with tokenize.open(path) as handle:
        source = handle.read()
    compile(source, path, 'exec')
print(f'compiled {len(paths)} files')
PY
```

Result: exit **0**, `compiled 389 files`. No source-tree bytecode was created by
this command (`compile()` cannot write `.pyc`; the only recent `.pyc` files came
from the test runner and are excluded cache files).

### 1.15 Exit-run skip review — accepted

The current exit run has the same 13 documented skips as the baseline; every
reason and applicability condition is reviewed in `skip-inventory.md`. No
hidden collection reduction: collection rose by 10 tests (9 focused layout
tests plus the restored `P1-A13` case) and no regression-hiding skip/xfail was
added.

## Layout-preparation probe (diagnostic, not an activated check) — historical

This probe ran before `P1-A13`/`P1-A14`; its 305-test count is historical.

A temporary copy was formatted with `ruff 0.17.1 format --target-version py314
docker tests scripts` (367 reformatted, 19 unchanged). Before preparation,
`pytest tests/enforcement` on the copy exited 1 with 25 failures (24 layout
couplings + the stale inventory path). After preparation, with the prepared
tests/docs/fixture copied into the same formatted copy, it exited **0** with
**305 passed, 669 subtests passed**. This is diagnostic evidence that the P1
preparation removes layout coupling; it does not activate or replace P3/P5
checks. The temporary copy path is not used as a durable evidence link.

## Re-verification after P1 revision (P1-A13, P1-A14) — current

The revision restored the inner completion-predicate negative fixture
(`P1-A13`) and moved the single authoritative architecture inventory from the
active change directory to
`tests/fixtures/process-architecture-inventory.json` (`P1-A14`). Every earlier
result in this report is historical and applies only to the superseded source
state.

Environment: CPython 3.14.7 from `.venv`; pytest with pytest-xdist, 8 workers,
`--dist loadfile`; Linux container; Docker absent.

Test-running instructions follow the canonical guide
[`docs/testing.md`](../../../../docs/testing.md) (linked from root `AGENTS.md`
and the READMEs): the default verification is the eight-worker full-suite
pytest command plus `scripts/check-types`; serial `unittest discover` is not the
default and is run only when a task explicitly requires it, never as a
substitute for pytest. `docs/testing.md` also scopes `scripts/check-dockerfile`
to `Dockerfile` changes on the host.

| # | Exact command | Exit | Result |
| --- | --- | --- | --- |
| Focused enforcement suite | `.venv/bin/python -m pytest tests/enforcement -n 8 --dist loadfile --tb=short -q` | 0 | `306 passed, 669 subtests passed in 27.73s` |
| E1 mandated full suite | `.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q` | 0 | `7909 passed, 13 skipped, 11 warnings, 18026 subtests passed in 53.21s` |
| Type checks | `scripts/check-types` | 0 | `All checks passed!` |
| Additional unittest discovery (not required by the canonical default) | `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'` | 0 | `Ran 6465 tests`, `OK (skipped=13)` |
| One-off compilation | the command under 1.14 | 0 | `compiled 389 files` |
| Dockerfile gate | `scripts/check-dockerfile` | not run | Docker absent (`docker: command not found`); `Dockerfile` untouched. Documented limitation, not a success claim. |

The 11 full-suite warnings remain the unsuppressed CPython multiprocessing/fork
`DeprecationWarning` families. The 13 skips are the same reviewed skips in
`skip-inventory.md`. Collection rose by one test relative to the pre-revision
prepared state (7908 → 7909 passed; enforcement 305 → 306) because `P1-A13`
restored the inner-`or` completion case. No test was removed, skipped or
weakened. The additional unittest discovery run passed but is not a gate for
this task: it is recorded as confirmation, and its omission would not change
the verdict.

Source-state fingerprint method (sorted paths plus their content digests,
serialized as `<path>\0<sha256>\n`; caches excluded):

```sh
.venv/bin/python - <<'PY'
import hashlib
from pathlib import Path

def fingerprint(roots):
    entries = []
    for root in roots:
        for path in sorted(Path(root).rglob('*')):
            if not path.is_file():
                continue
            if '__pycache__' in path.parts or path.suffix in {'.pyc', '.pyo'}:
                continue
            entries.append((path.as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()))
    digest = hashlib.sha256()
    for name, file_digest in entries:
        digest.update(f'{name}\0{file_digest}\n'.encode())
    return len(entries), digest.hexdigest()

print('tests+docs', fingerprint(['docs', 'tests']))
print('docker', fingerprint(['docker']))
print('all scoped', fingerprint(['docker', 'docs', 'tests', 'scripts']))
PY
```

Result: `tests+docs` 317 files
`276edd8e5526e0f9e0fcccb647b57158f7a4a767ac09415b420bd95b4fae72c9`; `docker`
122 files `dd0bb36a5a0d47d0786f3073172978d96d1325a9211a7321d28503f6babc46e4`;
`all scoped` 449 files
`b5d743e7cb1cfb2d338dfedb11fa6221fe2e128405f817ffbbd6daa46d355255`. The
fixture is under `tests/`, so it is covered by the `tests+docs` fingerprint; the
`openspec/` tree is deliberately excluded because only its completion boxes and
evidence text may change.

## Limitations and disclosures

- No remote GitHub/CI run was performed in P1; local commands are reported as
  local.
- `scripts/check-dockerfile` was not executed because Docker is absent; its
  conditional scope is documented.
- Local ty is 0.0.85, while CI pins ty==0.0.73; local success is not claimed as
  CI-pin verification.
- Ruff is not installed in P1; the probe used `uv tool run ruff@0.17.1` as a
  diagnostic only. Lint/format backlog remains for P2–P5.
- The verification JSON/TXT/Markdown files are evidence, not product source.
  The test-input fingerprint covers `docs/` and `tests/`, including the
  test-owned `tests/fixtures/process-architecture-inventory.json`, so this
  report can be completed after the checked run.

## Workflow disclosure

No `git add`, unstage, reset, restore, commit or other version-control
operation was executed by this session. The working changes appeared in the
index during the session (external index maintenance); they were not
interpreted as authorization to stage or commit. No commit was created.

## Exit criteria

| P1 exit criterion | Status |
| --- | --- |
| All concrete 1.x tasks and `P1-A*` instances accepted | met |
| All listed P1 deliverables present | met |
| R exit 0 with durable summary | met |
| Existing checks mandatory for the Python scope successful | met (Dockerfile-only Hadolint recorded as environmental/not applicable) |
| No unresolved ty diagnostics | met |
| Full inventory compiles without source bytecode | met |
| Source diff free of product behavior/stress/collection reductions | met |
| Skips justified and no hidden collection reduction | met |
| No unauthorized version-control operation by this session | met |

**P1 complete. P2–P7 remain unimplemented.**

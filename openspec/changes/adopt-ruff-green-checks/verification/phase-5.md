# Phase 5 exit report — manual resolution (3.2)

- **Change:** `adopt-ruff-green-checks`
- **Schema:** `spec-driven`
- **Phase:** P5 (manual resolution) only. P6–P7 are not started.
- **Base:** accepted P4 source state plus the corrected P5 manual-fix
  working-tree delta. P5 task checkboxes and evidence are outside the source
  delta.
- **Environment:** CPython 3.14.7 (`.venv`), pytest 9.1.1, pytest-xdist 3.8.0,
  Ruff 0.17.1, ty 0.0.85, Linux container, Docker absent.
- **Verdict:** P5 is accepted after compatibility and ledger corrections. The
  full-inventory Ruff check reports **0** findings, the format check reports
  **397 files already formatted**, shared compilation exits **0**, the existing
  type check exits **0**, and the full mandated pytest run exits **0**. All 667
  original residual findings are covered by the corrected resolution ledger,
  and the restored `NumericVersion` public export has direct regression
  coverage.

## Source-state fingerprints

The P5 source manifest uses the shared Python inventory and sorted `sha256sum`
entries. It includes all product, test and script Python files; the generated
architecture fixture and documentation are tracked separately.

| State | Manifest | Manifest SHA-256 |
| --- | --- | --- |
| P5 pre-fix input | [`p5-source-before.sha256`](p5-source-before.sha256) (396 entries) | `ebcd150801e9a29761239848793f1071497fffdf55858709a5248b69ca8bb854` |
| P5 accepted output | [`p5-source-after.sha256`](p5-source-after.sha256) (397 entries) | `30f505cc0c1a5f6e70a9c0124031666e584883551defcd2a758f09ef2929c1bd` |

The accepted delta changes or adds **164 Python source files**: the 162 P5
manual-fix files, the modified `tests/versioning/test_model.py`, and the new
`tests/test_p5_ledger_behavior.py`. The generated architecture fixture changed
from the P4 hash
`226a38273a62664740e0a0e518afc2b8ccea145e855fde7b024ac77d13512506`
to
`c2a182fe3ed519e753953049ed441b88b8a49332cb4366d57615d4d2c1fdf5c0`
after adding the focused compatibility regression test. The contributor guide
[`docs/python-checks.md`](../../../../docs/python-checks.md) records the
accepted suppression policy and links to the resolution ledger.

## Deliverables

| Deliverable | File |
| --- | --- |
| Pre-fix residual inventory | [`p5-ruff-findings-before.txt`](p5-ruff-findings-before.txt) (667 findings; SHA-256 `5f5895783fe5b990c0f085553abdd39a1d27a20b10a3271e79c3d05ed9ae5da8`) |
| Pre-fix per-file source manifest | [`p5-source-before.sha256`](p5-source-before.sha256) (396 entries) |
| Accepted post-fix per-file source manifest | [`p5-source-after.sha256`](p5-source-after.sha256) (397 entries) |
| Corrected diagnostic disposition and resolution ledger | [`p5-resolution-ledger.md`](p5-resolution-ledger.md) (614 rows; 613 original root-cause work orders plus `P5-V001`; 162 target-evidence commands plus `P5-A001`) |
| Structural ledger check | [`p5-ledger-check.txt`](p5-ledger-check.txt) |
| Target-command collection evidence | [`p5-target-collect.txt`](p5-target-collect.txt) |
| P5 exit summary | this file |

## RED (tasks 5.1–5.2)

The P4 residual inventory was 667 findings across 162 files:

| Rule | Findings | Rule | Findings |
| --- | ---: | --- | ---: |
| `F401` | 247 | `B905` | 20 |
| `E402` | 122 | `B017` | 16 |
| `B023` | 79 | `F821` | 12 |
| `B904` | 44 | `E731` | 10 |
| `F841` | 42 | `B007` | 7 |
| `F811` | 26 | `E741` | 7 |
| `RUF100` | 21 | `B018` | 6 |
|  |  | `SIM115` | 4 |
|  |  | `B010` | 2 |
|  |  | `B008` | 2 |

Every finding is listed in `p5-resolution-ledger.md` with a unique work-order
ID, its original location/message, corrected classification, actual GREEN
transformation, preserved behavior, target-specific assertion and broader
verification. The two loop variables in the same `start_action` closure and the
inseparable re-export sets are grouped with all affected locations named; 54
diagnostics are covered by those grouped rows.

No `P5-D` target was demonstrated: the original findings were dead/redundant
imports and locals, obsolete suppressions, explicit import/exception/closure
simplifications, annotation repairs, and intentional compatibility/lifecycle
constructs. No original finding proved a product-behavior defect.

## GREEN (tasks 5.3–5.5)

The corrected ledger contains **614 accepted rows**:

- **613 original root-cause work orders** cover all 667 findings.
- **1 validation-discovered repair** (`P5-V001`) removes a random-temp-path
  false positive from the configuration-document test without weakening its
  rejected-value assertion.
- Corrected classification counts: **462 simplification** and **152
  intentional construct** rows; **0 `P5-D` defects**. The `P5-S`/`P5-N` ID
  prefixes retain their original finding-order allocation; the class column is
  authoritative after diff review.
- Every row names the behavior that must remain and a file-targeted
  `P5-T###` command or `P5-A001` focused assertion. Full-suite and lint checks
  are additional evidence, not substitutes for those row-specific checks.

The primary actions were:

- Removed unused imports/locals and duplicate bindings after confirming they
  were not public re-exports; restored compatibility re-exports explicitly.
- Added explicit exception chaining for ordinary mapping raises.
- Bound closure loop variables with default parameters where the callback
  executes within the same iteration.
- Added specific exception types to blind test assertions; repaired
  annotation imports; converted assigned lambdas to nested functions.
- Made `zip` truncation explicit where preserving existing behavior.
- Preserved the audited process-coordinator shape and cleanup re-raise
  semantics with narrow, explained suppressions rather than source-shape
  changes.
- Converted temporary-file uses to context managers while retaining
  `delete=False` path behavior.
- Replaced callable defaults with `None` sentinels and constructed the value
  inside the function.
- Moved the late production `E402` imports to the top-level import block, or
  removed the late imports when they were unused; only test setup imports keep
  a specific `E402` suppression.

### Compatibility repair

`P5-S054` is retained as the historical work-order ID but corrected to
**intentional construct**. The original P5 edit removed
`NumericVersion` from `docker/versioning/model.py` as an unused import. Review
confirmed that the import was a documented compatibility re-export and that
`from docker.versioning.model import NumericVersion` raised `ImportError`.

The repair adds:

```python
from .constraints import NumericVersion as NumericVersion  # compatibility re-export
```

The focused regression is
`tests/versioning/test_model.py::TestCompatibilityExports::test_numeric_version_is_reexported_from_model`.
It asserts both that the import path works and that the exported object is
identical (`assertIs`) to `docker.versioning.constraints.NumericVersion`.

### Fixture regeneration

The new focused test file changed the generated architecture inventory. The
first post-correction full-suite run failed three architecture-fixture
assertions; after regenerating
`tests/fixtures/process-architecture-inventory.json` with the documented
`canonical_architecture_report(_sources())` command, the three targeted tests
and the full suite passed. The final fixture hash is recorded above.

### 5.4 Manual-fix formatting

```bash
ruff format --no-cache -- <changed Python files>
```

Result: **21 files reformatted** across the initial P5 pass and the correction
round. The final read-only check is `scripts/check-python-format`, exit **0**,
`397 files already formatted`.

### 5.5 Contributor guidance

[`docs/python-checks.md`](../../../../docs/python-checks.md) documents the
specific accepted suppression families (`E402` test setup, lifecycle `B904`/
`B905`, coordinator `B023`, dynamic `B010`, and compatibility re-exports) and
links to the [ledger suppression rationales](p5-resolution-ledger.md#suppression-rationales).
No blanket per-file suppression or rule disabling was introduced.

## INTROSPECT (tasks 5.6–5.8)

- **5.6 Resolution review:** every original finding maps to a ledger row; no
  configuration rule, inventory exclusion or test collection was weakened.
  Test edits outside the lint findings are `P5-V001` (random-path false
  positive repair), the `NumericVersion` compatibility identity assertion,
  and the focused closure-binding assertion for the cancellation evidence
  script.
- **5.7 Suppression review:** each recurring suppression has a specific code
  and a named necessity rationale. `RUF100` obsolete directives were removed;
  no blanket or obsolete suppression remains. Production `E402` imports were
  moved or removed rather than suppressed.
- **5.8 Scope review:** the process coordinator retained its audited
  `start_action` signature, fixed-width `zip` semantics and cleanup
  re-raise/cause behavior. Lifecycle, descriptor, process-budget, stress and
  architecture enforcement tests remain protected and pass.

## VALIDATE (tasks 5.9–5.16)

| Task | Exact command | Exit | Result |
| --- | --- | ---: | --- |
| 5.9 work-order regression/equivalence | `.venv/bin/python -m pytest tests/versioning/test_model.py tests/test_p5_ledger_behavior.py tests/test_constructor_build_cleanup.py tests/test_filesystem_directory_descriptors.py tests/test_process_adapter_compatibility.py tests/test_process_lifecycle.py tests/test_process_stop_policy.py tests/test_process_stop_policy_faults.py tests/test_configuration_document_validation_phase1.py tests/enforcement/test_process_contract.py tests/enforcement/test_layout_independence.py tests/enforcement/test_process_integrated_convergence.py tests/enforcement/test_release_inventory.py -n 8 --dist loadfile --tb=short -q` | 0 | 663 passed, 1165 subtests passed |
| target-specific command collection | each of the 162 commands in the ledger, with `--collect-only -q` substituted for `-q` | 0 | 162/162 collected; 0 failures |
| 5.10 full-inventory lint | `scripts/check-python-lint` | 0 | `All checks passed!`; zero unresolved findings |
| 5.11 full-inventory format | `scripts/check-python-format` | 0 | `397 files already formatted` |
| 5.12 G — full pytest | `.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q` | 0 | 7993 passed, 13 skipped, 11 warnings, 18026 subtests passed |
| 5.13 existing type check (E2) | `scripts/check-types` | 0 | `All checks passed!` |
| 5.13 existing test check (E1) | mandated full pytest above | 0 | same 7993/13/11/18026 result |
| 5.13 shared compilation | `scripts/check-python-compile` | 0 | all 397 inventory paths compile; no source-tree bytecode |
| 5.13 format adoption check | `scripts/check-python-format` | 0 | 397 files already formatted |
| 5.13 lint adoption check | `scripts/check-python-lint` | 0 | zero findings |
| 5.13 E4 — Dockerfile | `scripts/check-dockerfile` | not run | Docker CLI/daemon absent (`docker` lookup exit 1); `Dockerfile` unchanged, documented environmental limitation, not a pass |
| 5.14 shared compilation for G | `scripts/check-python-compile` | 0 | same successful inventory result |
| 5.15 skip review | `.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q -rs` | 0 | 7993 passed / 13 skipped; reasons reviewed below |
| 5.16 P5 exit report | this file | — | resolution ledger and G refer to the same corrected final state |

### 5.15 Skip review

The additional `-rs` report exited **0** with 7993 passed / 13 skipped. The 13
reasons are the reviewed applicability conditions:

- 3 require an existing `docker-dev` account;
- 1 requires `sudo runuser -u nobody` (unavailable in the container);
- 8 are explicit Phase-6 Docker-backed opt-in/standalone-runner conditions;
- 1 requires `NPM_ENV_REAL_DOCKER=1` and a usable Docker daemon.

No new skip, xfail/xpass or collection reduction appeared.

### Ledger and source-state checks

- `p5-ledger-check.txt`: **exit 0**; 614 rows, 614 unique IDs/checkboxes,
  462 simplification / 152 intentional construct, 0 `P5-D`, all rows `PASS`
  with a named target-specific assertion, 667 original findings, 54 grouped
  diagnostics, and 162 target-evidence commands.
- `p5-target-collect.txt`: **162/162 target commands** collected with exit 0;
  the same modules are executed by the full suite.
- `sha256sum -c p5-source-after.sha256`: **exit 0** for all 397 entries.
- `git diff --check`: **exit 0**.
- `openspec validate adopt-ruff-green-checks --json`: **exit 0** (1/1 passed).

## Limitations and disclosures

- Full-inventory lint is green on the recorded final source state; the
  historical P4 backlog of 667 is retained only as RED evidence.
- `P5-D` is zero because no original finding demonstrated a product-behavior
  defect. `P5-S054` is a P5 compatibility regression discovered during
  post-review, not an original P5-D finding; it is repaired and protected by
  the named identity assertion. `P5-V001` is a validation-only test-fragility
  repair.
- The 162 file-targeted commands were collection-checked individually; the
  full suite executes their test modules. They are not separate full-suite
  substitutes.
- `E402` suppressions are limited to test modules whose path/setup statements
  must precede imports; production `E402` imports were moved or removed. Each
  remaining suppression has an adjacent explanation and a ledger row.
- Lifecycle/coordinator suppressions are limited to the named process.py
  constructs and cleanup accumulator re-raises.
- The Dockerfile check was not executed because Docker is absent; `Dockerfile`
  is unchanged.
- The 11 full-suite warnings are the unsuppressed CPython
  multiprocessing/fork `DeprecationWarning` families. They were not suppressed
  or converted into passes.
- No remote GitHub CI run was performed; all results are local.
- No `git add`, unstage, reset, restore, checkout, commit or other
  version-control mutation was performed.

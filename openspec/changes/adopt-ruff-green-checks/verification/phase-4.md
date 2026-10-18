# Phase 4 exit report — safe automatic fixes (3.1)

- **Change:** `adopt-ruff-green-checks`
- **Schema:** `spec-driven`
- **Phase:** P4 (safe automatic fixes) only. P5–P7 are not started.
- **Base:** `HEAD` `25085a1` (accepted P3 formatter-only state) plus the
  P4 working-tree delta. P4 completion checkboxes and verification evidence
  are outside that source delta.
- **Environment:** CPython 3.14.7 (`.venv`), pytest 9.1.1, pytest-xdist 3.8.0,
  Ruff 0.17.1, ty 0.0.85, Linux container, Docker absent.
- **Verdict:** P4 is accepted on the reviewed automatic-only delta. The
  accepted delta changes 191 inventory source files plus the test-owned
  generated architecture inventory. It contains no unique import
  symbol/module removals (one duplicate identical `OverridePolicy` import was
  collapsed) and only five behavior-preserving `F541` semantic work orders (six
  literal fixes) across four files; the two runtime diagnostics are separate work
  orders. The
  full-inventory format check exits 0, R exits 0, E2 exits 0 and shared
  compilation exits 0. Full-inventory Ruff lint still exits 1 with 667
  findings and is explicitly P5 migration backlog, never a pass.

## Source-state fingerprints

Method is the P1 fingerprint method: sorted paths plus content digests,
serialized as `<path>\0<sha256>\n`, with caches excluded. The per-file P4
manifests use the equivalent sorted `sha256sum` form.

| State | `docs/` + `tests/` | `docker/` | All scoped |
| --- | --- | --- | --- |
| P3 formatted input | 324 `54b162d11fe8eaac344b2b72bf013c0fc0b60ca454ebb018fbc35c126291a86f` | 122 `743365d51cc0b6d94e8953e1e5813c2023d730eb444d568f8eaf7d3da6085188` | 460 `2a8c701d8107b6ae33348e0b5074a3f027a80acaac226b53f7fd58d9a9d00310` |
| P4 accepted output | 324 `00c103ac4bb8016b7724aa3b2e91c4ffc3efa7d8f3b8ac41e621fe325c625e5e` | 122 `96f3d6772d04657b44cf2c607714af2d6a4bfed3a98ee890ae530fc37bfe2600` | 460 `85a089aafe32adfbb9176406edc6b42d998180ba8da584b2031bfac1d22ee034` |

[`p4-source-before.sha256`](p4-source-before.sha256) is identical to
[`p3-source-after.sha256`](p3-source-after.sha256) (396 entries; `diff` exit
0). [`p4-source-after.sha256`](p4-source-after.sha256) identifies the exact
396-file inventory state checked by the final format, test, type and compile
runs. The 191 changed inventory files and regenerated
`tests/fixtures/process-architecture-inventory.json` are the complete tracked
P4 delta.

## Deliverables

| Deliverable | File |
| --- | --- |
| Pre-autofix per-file source manifest | [`p4-source-before.sha256`](p4-source-before.sha256) (396 entries) |
| Accepted post-autofix per-file source manifest | [`p4-source-after.sha256`](p4-source-after.sha256) (396 entries) |
| Post-format Ruff findings | [`p4-ruff-findings-before.txt`](p4-ruff-findings-before.txt) (1006 findings) |
| Residual Ruff findings for P5 | [`p4-ruff-findings-residual.txt`](p4-ruff-findings-residual.txt) (667 findings) |
| Automatic-change review ledger / work orders | [`p4-review-ledger.md`](p4-review-ledger.md) (317 targets) |
| P4 exit summary | this file |

## RED (task 4.1)

Command:

```bash
mapfile -t files < <(.venv/bin/python scripts/python_checks.py list)
.venv/bin/ruff check --no-cache --output-format concise -- "${files[@]}"
```

Result: **exit 1 — 1006 findings** on the 396-path inventory. The complete
location/rule inventory is
[`p4-ruff-findings-before.txt`](p4-ruff-findings-before.txt). The source
manifest [`p4-source-before.sha256`](p4-source-before.sha256) identifies the
exact autofix input, and it matches the P3 accepted formatted state.

Rule counts before autofix: 311 `I001`, 247 `F401`, 122 `E402`, 79 `B023`,
44 `B904`, 42 `F841`, 27 `F811`, 21 `RUF100`, 21 `E401`, 20 `B905`, 16
`B017`, 12 `F821`, 10 `E731`, 7 `E741`, 7 `B007`, 6 `F541`, 6 `B018`, 4
`SIM115`, 2 `B010`, 2 `B008`.

## GREEN (tasks 4.2–4.3)

### 4.2 Full-inventory safe fix — executed, reviewed and narrowed

The mandated command was run exactly without unsafe fixes:

```bash
.venv/bin/ruff check --no-cache --fix --output-format concise -- "${files[@]}"
```

Result: **exit 1 — `1023 errors (653 fixed, 370 remaining)`**, 215 source
files changed. The residual findings were not called a pass.

[`p4-review-ledger.md`](p4-review-ledger.md) records the review and rejects
three automatic-change classes:

1. **`F401` removals** broke intentional re-export modules and test-module
   symbol re-exports. A diagnostic full pytest run reported **263 failed,
   3445 passed, 284 errors, 15633 subtests passed** with `ImportError`s
   including `carry_secondary_diagnostics` from `docker.transactions.errors`,
   `api` from `tests.test_process_stop_policy`, and the `docker.versions`
   shim. All F401 changes were restored to the pre-fix state and remain
   residual backlog.
2. **`RUF100` removals** were evaluated with a narrowed rule set and removed
   still-needed `# noqa: E402` directives in `scripts/python_checks.py`,
   `scripts/validate-phase5` and
   `tests/test_constructor_migration_contract.py`. All RUF100 changes were
   restored to the pre-fix state and remain residual backlog for P5.
3. **The `B010` rewrite** in `docker/process.py` changed
   `setattr(error, "cleanup", self)` to `error.cleanup = self`. E2 then exited
   **1** with two `error[unresolved-attribute]` diagnostics on
   `BaseException`. The file was restored and the two B010 findings remain
   residual backlog.

After restoring those rejected classes, the accepted automatic command was:

```bash
.venv/bin/ruff check --no-cache --fix --select I001,E401,F541,B010 --output-format concise -- "${files[@]}"
```

Result: **exit 0 — `Found 332 errors (332 fixed, 0 remaining)`**. Review then
rejected the two B010 sites, leaving **330 retained fixes across 191 source
files** consisting only of `I001` ordering, `E401` comma-splitting and the
six `F541` literal fixes grouped into five work orders. AST comparison of the
accepted files found **0 unique import symbols/modules added or removed**; one
duplicate identical `OverridePolicy` import was collapsed to one binding.

### 4.3 Reformat autofix-changed files — formatter-only

```bash
mapfile -t changed < <(git diff --name-only -- 'docker/*' 'tests/*' 'scripts/*')
.venv/bin/ruff format --no-cache -- "${changed[@]}"
```

Result: **exit 0 — files left unchanged** on the accepted delta (the run
after the initial accepted fix reported 192 unchanged; the isolated
`docker/process.py` restore also reported 1 unchanged). The additional delta
therefore contains no formatter output beyond an already-formatted state, and
the full-inventory read-only format check exits 0. No helper, test-logic,
configuration or handwritten repair is in the delta.

## INTROSPECT (tasks 4.4–4.6)

### 4.4 Automatic-change review ledger — accepted, block-level

[`p4-review-ledger.md`](p4-review-ledger.md) reviews each changed import block
separately: **311 blocks across 190 files** (184 module-scope and 127
function/class/conditional-scope blocks), plus five independent semantic work
orders (six `F541` literals) and one generated-inventory target. Each import
row identifies the file, the enclosing module/function/class/conditional scope,
the before/after line ranges,
the exact import order change and a block-text digest. It assesses normalized
local-name bindings, initialization/execution point, repository import-cycle
membership, and registration side effects for that block. It does not infer
preserved behavior from a file-wide unchanged import set.

### 4.5 Review work orders — accepted

The ledger instantiates **317** work orders (`P4-R001`–`P4-R317`): 311 changed
import blocks, five independent semantic work orders (six `F541` literals) and
one generated-inventory target. Every row has a completion checkbox, `P4-B`
prerequisites, an exact before/after relation, an expected result, and a named
passing check. The previous file-level `P4-R191` is split into `P4-R312` (`gateway.mapping`) and
`P4-R313` (`host-access.address`), so each runtime diagnostic has its own
unchanged-string evidence and verification.

The block distribution is 232 `I001` reorders, 53 layout/comma-only changes
(19 `E401` comma splits and 34 line-layout-only blocks) and 26 whitespace-only
inter-block blank-line normalizations. The five retained semantic work orders are:

- `docker/versioning/runtime_verification.py` — `gateway.mapping` failure
  message becomes an identical plain string (`P4-R312`).
- `docker/versioning/runtime_verification.py` — `host-access.address` failure
  message becomes an identical plain string (`P4-R313`).
- `tests/test_constructor_host_access_acceptance.py` — two adjacent
  placeholder-free mount literals keep identical implicit concatenation
  (`P4-R314`).
- `tests/test_constructor_phase4_named_context.py` — the asserted Dockerfile
  text is unchanged (`P4-R315`).
- `tests/test_npm_environment_policy.py` — the asserted `--env` text is
  unchanged (`P4-R316`).

The original targeted review command passed **451 tests, 1 skipped**. The
cycle-sensitive ledger check passed **385 tests, 698 subtests**, and the
semantic-fix check passed **69 tests, 1 skipped**. The rejected
F401/RUF100/B010 changes are not accepted work orders; they are explicitly
listed in the ledger and residual inventory rather than hidden. No additional
test repair or hand edit was needed.

A structural ledger check exited **0**: it re-derived 311 unique block keys from
the 396-file AST state, found 317 contiguous work-order IDs/checkboxes with no
duplicates, and confirmed every row carries `P4-B`, before/after evidence, a
`PASS` result and a named verification alias. It also records the duplicate
`OverridePolicy` binding collapse and the `urllib` package-binding case.

### 4.6 Residual Ruff findings — captured for P5

[`p4-ruff-findings-residual.txt`](p4-ruff-findings-residual.txt) contains the
complete remaining location/rule inventory: **667 findings, exit 1**.
Counts: 247 `F401`, 122 `E402`, 79 `B023`, 44 `B904`, 42 `F841`, 26 `F811`,
21 `RUF100`, 20 `B905`, 16 `B017`, 12 `F821`, 10 `E731`, 7 `E741`, 7 `B007`,
6 `B018`, 4 `SIM115`, 2 `B010`, 2 `B008`. This includes every rejected
automatic change and is never represented as a successful lint check.

## VALIDATE (tasks 4.7–4.13)

| Task | Exact command | Exit | Result |
| --- | --- | ---: | --- |
| 4.7 ledger checks | `.venv/bin/python -m pytest tests/test_process_lifecycle.py tests/test_process_stop_policy.py tests/test_process_stop_policy_faults.py tests/test_process_compatibility_action_gate.py tests/test_constructor_runtime_verification.py tests/test_constructor_host_access_acceptance.py tests/test_constructor_phase4_named_context.py tests/test_npm_environment_policy.py tests/enforcement/test_layout_independence.py tests/enforcement/test_process_integrated_convergence.py -n 8 --dist loadfile --tb=short -q` | 0 | 451 passed, 1 skipped |
| 4.7 block/cycle ledger checks | `.venv/bin/python -m pytest tests/test_diagnostic_recognizer_phase3.py tests/test_host_download_observability.py tests/test_version_effective.py tests/test_constructor_verification.py tests/enforcement/test_descriptor_close_convergence.py tests/enforcement/test_process_contract.py tests/enforcement/test_process_integrated_convergence.py tests/enforcement/test_layout_independence.py -n 8 --dist loadfile --tb=short -q` | 0 | 385 passed, 698 subtests passed |
| 4.7 semantic ledger checks | `.venv/bin/python -m pytest tests/test_constructor_runtime_verification.py tests/test_constructor_host_access_acceptance.py tests/test_constructor_phase4_named_context.py tests/test_npm_environment_policy.py -n 8 --dist loadfile --tb=short -q` | 0 | 69 passed, 1 skipped |
| 4.8 format check | `scripts/check-python-format` | 0 | `396 files already formatted` |
| 4.9 R — full pytest | `.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q` | 0 | 7991 passed, 13 skipped, 11 warnings, 18026 subtests passed |
| 4.10 existing checks | E2 `scripts/check-types` | 0 | `All checks passed!` |
| 4.10 / 4.11 compilation | `scripts/check-python-compile` | 0 | no output for all 396 inventory paths |
| 4.10 format | `scripts/check-python-format` | 0 | `396 files already formatted` |
| 4.10 E4 | `scripts/check-dockerfile` | not run | Docker CLI/daemon absent (`docker` lookup exit 1); `Dockerfile` unchanged, documented environmental limitation, not a pass |

Full-inventory lint is not a P4 gate and remains the 667-finding P5 backlog.
No existing required check was narrowed or replaced. E2 covered
`docker tests/typing` with the configured Python 3.14 semantics. Compilation
covered every selected file without importing/executing application code or
leaving source-tree bytecode.

### 4.12 Phase-exit skip review — accepted

The additional `-rs` report (exit 0; same 7991/13 counts and reasons) confirmed
the reviewed skip inventory: three skips require an existing `docker-dev`
account, one requires `sudo runuser -u nobody`, eight are explicit Phase-6
Docker-backed opt-in/standalone-runner conditions, and one requires
`NPM_ENV_REAL_DOCKER=1` plus a usable Docker daemon. No new skip, xfail/xpass
or collection reduction appeared.

### 4.13 P4 exit report — published

The automatic-only delta, the 317-target block-level review ledger, the
667-finding residual backlog and every P4 validation result refer to the same
accepted source state identified by
[`p4-source-after.sha256`](p4-source-after.sha256) and the P4 fingerprints
above. All P4 tasks are accepted for this state.

## Limitations and disclosures

- Full-inventory Ruff lint remains P5 migration backlog: 667 findings, exit 1.
  No lint-green claim is made, and no finding was removed by weakening
  configuration, tests or inventory coverage.
- The Dockerfile check was not executed because Docker is absent; `Dockerfile`
  is unchanged.
- The 11 full-suite warnings are the unsuppressed CPython
  multiprocessing/fork `DeprecationWarning` families. They were not suppressed
  or converted into passes.
- No remote GitHub CI run was performed; all results are local.
- No `git add`, unstage, reset, restore, checkout, commit or other
  version-control mutation was performed. Rejected automatic changes were
  restored from the captured temporary before-state, and the test-owned
  architecture fixture was regenerated by its documented command.
- The block-level ledger expansion is documentation-only. It did not change
  source content or invalidate the unchanged-source test results; the fingerprints
  remain those of the accepted P4 source state.

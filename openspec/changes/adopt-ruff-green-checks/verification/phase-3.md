# Phase 3 exit report — formatting only

- **Change:** `adopt-ruff-green-checks`
- **Schema:** `spec-driven`
- **Phase:** P3 (formatting only) only. P4–P7 are not started.
- **Base:** `HEAD` `d938527dc2e9b5903b5179df3840e34ec5541b6a` (P2 accepted)
  plus the formatter-only P3 working-tree delta. P3 completion checkboxes and
  verification evidence are outside the source delta.
- **Environment:** CPython 3.14.7 (`.venv`), pytest 9.1.1, pytest-xdist 3.8.0,
  Ruff 0.17.1, ty 0.0.85, Linux container, Docker absent.
- **Verdict:** P3 accepted on the formatted source state. The source delta is
  exactly Ruff formatter output, all changed-file ASTs are equivalent, the
  formatter is idempotent, the full-inventory format check exits 0, and R exits
  0. Full-inventory lint remains P4/P5 migration backlog and is not claimed as
  a pass.

## Source-state fingerprints

Method: sorted paths plus content digests serialized as
`<path>\0<sha256>\n`, with caches excluded (the method recorded in
[`phase-1.md`](phase-1.md)).

| State | `docs/` + `tests/` | `docker/` | All scoped |
| --- | --- | --- | --- |
| P2 baseline | 324 `e513ebf21468fdfa61ed99c250a8b2ddf7cabd454fa55bcafb8a5ab63620148a` | 122 `dd0bb36a5a0d47d0786f3073172978d96d1325a9211a7321d28503f6babc46e4` | 460 `a00d459dcb69c8055b2aa80489c24bb6143c702d00d7eed178b5185cd51457f0` |
| P3 formatted | 324 `54b162d11fe8eaac344b2b72bf013c0fc0b60ca454ebb018fbc35c126291a86f` | 122 `743365d51cc0b6d94e8953e1e5813c2023d730eb444d568f8eaf7d3da6085188` | 460 `2a8c701d8107b6ae33348e0b5074a3f027a80acaac226b53f7fd58d9a9d00310` |

The formatted fingerprints identify the same state checked by the final
format/idempotence runs, full pytest and compilation. The source delta is
uncommitted; the fingerprints are content-based and independent of index
state.

## Deliverables

| Deliverable | File |
| --- | --- |
| Machine-generated format delta | 371 inventory source files; no helper, test-logic, configuration or import-sort edits |
| Before-state per-file baseline | [`p3-source-before.sha256`](p3-source-before.sha256) (396 entries) |
| After-state per-file manifest | [`p3-source-after.sha256`](p3-source-after.sha256) (396 entries) |
| AST equivalence report | [`p3-ast-equivalence.md`](p3-ast-equivalence.md) |
| Formatter idempotence report | [`p3-idempotence.md`](p3-idempotence.md) |
| Delta purity report | [`p3-delta-purity.md`](p3-delta-purity.md) |
| P3 exit summary | this file |

## RED (tasks 3.1–3.2)

### 3.1 Pre-format format check — recorded

`scripts/check-python-format` exited **1** with **371 files would be
reformatted, 25 files already formatted**. The source inventory contained
**396 selected paths and 69 recorded exclusions**, and its selected-path list
matched [`source-inventory-p2.txt`](source-inventory-p2.txt) exactly (`diff`
exit 0). The pre-format failure is recorded as migration work, not as a pass.

### 3.2 P2 source baseline — captured

A temporary pre-format copy contained all **396** inventory files. The durable
before-state manifest contains one SHA-256 entry per path:
[`p3-source-before.sha256`](p3-source-before.sha256). The temporary copy
supplied the before-state for AST and deterministic-replay comparison; the
temporary path is not a durable evidence link. The reconfirmed P2 fingerprints
are in the table above.

## GREEN (task 3.3)

The complete 396-path inventory was formatted with the repository policy:

```bash
mapfile -t files < <(.venv/bin/python scripts/python_checks.py list)
.venv/bin/ruff format --no-cache -- "${files[@]}"
```

The actual run used the inventory written to a temporary file after it was
verified against the durable P2 snapshot.

Result: **exit 0 — `371 files reformatted, 25 files left unchanged`**.

The source delta therefore contains 371 modified inventory files:
**35,148 insertions and 18,167 deletions**. No source outside the inventory
changed.

## INTROSPECT (tasks 3.4–3.5)

### 3.4 AST equivalence — accepted

[`p3-ast-equivalence.md`](p3-ast-equivalence.md) records the comparison:
parsing with `type_comments=True`, dumping with
`include_attributes=False`, normalizing only `TypeIgnore.lineno` to 0 and
formatter-permitted docstring whitespace with `inspect.cleandoc`.

Result: **371/371 changed files equivalent; 25 unchanged; 0 AST
differences.** Type-ignore tags and ignored code were preserved. The only
docstring changes were formatter indentation/trailing-newline normalization in
two files. No source-sensitive test repair or handwritten code change was
needed or introduced.

### 3.5 Delta purity — accepted

[`p3-delta-purity.md`](p3-delta-purity.md) records a deterministic replay:
formatting a copy of the captured baseline with the same Ruff 0.17.1 command
and configuration produced exactly the current bytes for all **396/396** files
(**371 reformatted, 25 unchanged** in the replay). `git diff --summary`
reported no mode/rename/binary changes; `git diff --check` exited 0. Every
changed source path is an inventory path. The only tracked non-inventory
change is the P3 completion checkboxes in `tasks.md`.

The delta is suitable for a separate explicitly authorized formatting-only
commit. No staging or commit was performed.

## VALIDATE (tasks 3.6–3.12)

### 3.6 Second formatter pass — idempotent

[`p3-idempotence.md`](p3-idempotence.md) records the second formatter pass:
**exit 0 — `396 files left unchanged`**; SHA-256 comparison against
`p3-source-after.sha256` found **396 unchanged, 0 changed**.

### 3.7 Full-inventory format check — accepted

`scripts/check-python-format` exited **0** with `396 files already formatted`.
This command also validated inventory/Ruff discovery parity and formatter
coverage before the read-only check.

### 3.8 R — full pytest — accepted

Command:
`.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q`

Result: **exit 0 — 7991 passed, 13 skipped, 11 warnings, 18026 subtests
passed in 56.17 s**. The 11 warnings remain the unsuppressed CPython
multiprocessing/fork `DeprecationWarning` families. A separate
`-q -rs` run (exit 0, same 7991/13 counts and reasons, 53.53 s) supplied the
skip reasons; it did not replace the mandated command.

### 3.9 P3 manifest checks — accepted for scope

| Check | Exact command | Exit | Result |
| --- | --- | ---: | --- |
| E1 full pytest | `.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q` | 0 | 7991 passed, 13 skipped, 11 warnings, 18026 subtests |
| E2 type check | `scripts/check-types` | 0 | `All checks passed!` (ty 0.0.85 local; CI pin remains a later verification item) |
| E4 Dockerfile gate | `scripts/check-dockerfile` | not run | Docker CLI/daemon absent (`docker` lookup exit 1) and `Dockerfile` unchanged; documented environmental limitation, not a pass |
| Shared compilation | `scripts/check-python-compile` | 0 | no output for the complete 396-path inventory |
| Full-inventory format check | `scripts/check-python-format` | 0 | `396 files already formatted` |

Full-inventory Ruff lint is not a P3 gate. P2 recorded **1304 findings** as
migration backlog; this report does not represent that snapshot as current
after formatting. P4 task 4.1 will capture the post-format diagnostics before
autofix.

### 3.10 Shared compilation for R — accepted

`scripts/check-python-compile` was rerun independently for the formatted
inventory: **exit 0**, no stdout/stderr. The full inventory compiled without
importing/executing application code or leaving source-tree bytecode.

### 3.11 Phase-exit skip review — accepted

The `-rs` run reported the same 13 reviewed skips with the same reasons and
applicability conditions as [`skip-inventory.md`](skip-inventory.md):

- three skips require an existing `docker-dev` account;
- one requires `sudo runuser -u nobody`;
- eight are explicit Phase-6 Docker-backed opt-in/standalone-runner conditions;
- one requires `NPM_ENV_REAL_DOCKER=1` and a usable Docker daemon.

No new skip, xfail/xpass or collection reduction appeared. The runner exited 0.

### 3.12 P3 exit report — published

All P3 tasks are accepted on the same formatted state. AST equivalence,
deterministic replay/purity, idempotence, full-inventory format check, R, the
existing required checks and shared compilation all refer to the P3 formatted
fingerprints above. The source delta is formatter-only and uncommitted.

## Limitations and disclosures

- Full-inventory lint remains P4/P5 migration backlog; no lint-green claim is
  made. No unsafe edits, import sorting or manual repairs were performed.
- The Dockerfile check is not applicable in this environment because Docker is
  absent, and P3 does not modify `Dockerfile`.
- The 11 full-suite warnings are unsuppressed and are not treated as passes or
  failures by suppression.
- No remote GitHub CI run was performed.
- No staging, commit, reset, checkout or other unauthorized version-control
  operation was performed.

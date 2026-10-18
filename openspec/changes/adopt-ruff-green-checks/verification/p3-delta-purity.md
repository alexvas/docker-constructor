# P3 formatter-delta purity report

- **Change:** `adopt-ruff-green-checks`
- **Baseline:** [`p3-source-before.sha256`](p3-source-before.sha256)
- **Formatted state:** [`p3-source-after.sha256`](p3-source-after.sha256)
- **Tool:** Ruff 0.17.1 with the repository `pyproject.toml` configuration.

## Deterministic replay

A temporary copy of the 396-file pre-format baseline was formatted with the
same command and configuration used for the working tree:

```bash
.venv/bin/ruff format --config pyproject.toml --no-cache -- "${expected_files[@]}"
```

Result: **exit 0 — `371 files reformatted, 25 files left unchanged`**.

Every replayed output file was then compared byte-for-byte with the actual
working-tree file by SHA-256: **396 compared, 396 exact matches, 0
mismatches**. The working-tree source delta therefore consists exactly of
Ruff formatter output generated from the captured P2 baseline.

## Working-tree delta audit

- **371** inventory files changed: **35,148 insertions, 18,167 deletions**.
- **25** inventory files were already formatted and remained byte-identical.
- Every changed source path is in the 396-path shared inventory.
- `git diff --summary` reported no file-mode, rename, copy or binary changes.
- `git diff --check` exited **0**.
- The only tracked non-inventory change is the P3 completion checkboxes in
  `openspec/changes/adopt-ruff-green-checks/tasks.md`; verification reports and
  hash manifests are outside the source delta.
- No source-sensitive test repair, import sort, helper edit, configuration
  change or handwritten source change occurred.
- No staging, commit, reset, checkout or other version-control mutation was
  performed.

The delta is suitable for a separate formatting-only commit if the user
explicitly authorizes one.

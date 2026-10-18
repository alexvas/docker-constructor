# P3 formatter idempotence report

- **Change:** `adopt-ruff-green-checks`
- **State:** the formatted P3 source state identified by the post-format
  fingerprints in [`phase-3.md`](phase-3.md).
- **Tool:** Ruff 0.17.1, no cache.

## Second formatter pass

After the first format and the AST/purity review, the same complete inventory
was formatted again:

```bash
mapfile -t files < <(.venv/bin/python scripts/python_checks.py list)
.venv/bin/ruff format --no-cache -- "${files[@]}"
```

Result: **exit 0 — `396 files left unchanged`**.

The per-file SHA-256 values were then recomputed and compared with
[`p3-source-after.sha256`](p3-source-after.sha256): **396 compared, 396
unchanged, 0 changed**.

## Subsequent full format check

`scripts/check-python-format` then validated the shared inventory, Ruff
discovery parity and formatter coverage before running the read-only check.

Result: **exit 0 — `396 files already formatted`**.

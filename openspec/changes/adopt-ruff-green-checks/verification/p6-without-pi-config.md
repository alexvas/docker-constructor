# P6 follow-up — no pi-green-loop configuration

At the user's explicit request, removed the untracked `pi-green-loop.json`.
Local/CI wiring remains `scripts/check-green`; the documentation and contract
no longer require the optional pi-green-loop configuration. No specification
text or index state was changed. This explicit user choice supersedes the
previous config-based wiring description in `phase-6.md` and the P6 ledger.
The old manifests/results are historical, not current-state verification.

Checked state: the source/control state in `phase-6.md`, minus
`pi-green-loop.json`, with these replacement file SHA-256 values:

- `tests/enforcement/test_green_integration.py`:
  `12b724c4555853066a920640075b1b28d042f0bee809567f1c2c5eb28d8e7ae4`
- `docs/python-checks.md`:
  `67008852a4ebd6f79ad80c2c5d265b77752678a5bb6c3ce4654e39adeeee6fe5`

Fresh verification: `scripts/check-green`, exit **0**; complete stdout/stderr
captured temporarily with actual status preserved. All five commands in the
gate succeeded: lint, format (404 files), syntax compilation, type checking,
and `.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q`.
Pytest: **8046 passed, 13 reviewed skips, 18027 subtests passed, 11 warnings**,
54.12 seconds. Environment remains CPython 3.14.7, Ruff 0.17.1, ty 0.0.73,
pytest 9.1.1 and xdist 3.8.0. Warnings remain the unsuppressed CPython
multiprocessing/fork DeprecationWarnings. All individual skip reasons were
printed and approved by the unchanged policy. `git diff --check` exited **0**.

No remote CI success is claimed; 6.11/6.25 still await the user's CI result.

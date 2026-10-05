# Phase 1 Verification Record — Lightweight Cleanup Foundation

Date: 2026-10-05T13:42:30Z

Change: `extract-generic-descriptor-capabilities`

Scope: Phase 1 only. Phases 2–8 remain unstarted.

## Production files added

```text
docker/filesystem/__init__.py   # empty; no imports or re-exports
docker/filesystem/cleanup.py    # CleanupFailures, attach_secondary, carry_secondary_diagnostics
```

## Production files changed

```text
docker/transactions/cleanup.py  # now a direct compatibility import of the shared class
docker/transactions/errors.py   # generic diagnostic helpers replaced by compatibility imports
```

## Test files added / changed

```text
tests/test_filesystem_cleanup.py                     # added
tests/test_transactions_phase9b_carry.py             # task 1.3 adaptation
tests/test_transactions_phase9b_remap_boundaries.py  # architecture allowlist admits the foundation owner
```

## Commands and results

RED evidence (before implementation):

```text
python -m unittest tests.test_filesystem_cleanup
  -> ImportError: No module named 'docker.filesystem'  (FAILED, errors=1)

python -m unittest \
  tests.test_transactions_phase9b_carry.CarrySecondaryDiagnosticsTests.\
  test_operation_grants_no_filesystem_or_mapping_authority
  -> ModuleNotFoundError: No module named 'docker.filesystem'  (FAILED, errors=1)
```

GREEN and phase gate (task 1.10):

```text
python -m unittest tests.test_filesystem_cleanup \
  tests.test_transactions_phase9a_accumulator \
  tests.test_transactions_phase9b_carry
  -> Ran 76 tests ... OK
```

Boundary / architecture regression guard:

```text
python -m unittest tests.test_transactions_phase9b_remap_boundaries
  -> Ran 9 tests ... OK
```

Typecheck:

```text
ty check docker --python-version 3.14 --output-format concise
  -> All checks passed!
```

Full repository suite:

```text
python -m unittest discover -s tests -p 'test_*.py'
  -> Ran 5281 tests ... OK (skipped=13)
```

## Task-by-task evidence

- **1.1** `CleanupFailuresTruthTableTests` covers no primary, ordinary primary,
  process-control primary, ordinary cleanup failure, unexpected cleanup defect,
  interruption, multiple independent actions, and terminal single-use (run after
  completion and second `complete`).
- **1.2** `AttachSecondaryContractTests` and
  `CarrySecondaryDiagnosticsContractTests` cover identity de-duplication,
  frozen domain exceptions (`_FrozenDomainError` and the real
  `docker.npm_environment.errors.LockedNpmError`), bounded notes, bounded
  repeated attachment, source-order carrying, no-op, and chain preservation.
- **1.3** `test_operation_grants_no_filesystem_or_mapping_authority` no longer
  filters by transaction-local `__module__`; it asserts
  `docker.transactions.errors.carry_secondary_diagnostics is
  docker.filesystem.cleanup.carry_secondary_diagnostics`, asserts the shared
  function's defining module, and inspects that shared function for prohibited
  `path`/`unlink`/`remove` authority.
- **1.4** `FoundationImportIsolationTests` runs an isolated subprocess that
  imports `docker.filesystem.cleanup` and proves no `docker.transactions`,
  `docker.npm_environment`, `docker.versioning`, `docker.constructor_cli`, or
  `docker.launcher` module loaded, no aggregate name is exposed on
  `docker.filesystem`, and the package declares no `__all__`.
- **1.5** `docker/filesystem/__init__.py` is empty. Importing
  `docker.filesystem` yields only the auto-bound `cleanup` submodule attribute;
  no name owned by `cleanup`, `operations`, or `descriptors` is exposed.
- **1.6** `CleanupFailures`, `attach_secondary`, and
  `carry_secondary_diagnostics` are implemented in
  `docker/filesystem/cleanup.py` with the Phase 9A precedence and
  diagnostic-retention behavior. Tasks 1.1/1.2 pass using only the foundation.
- **1.7** `docker.transactions.cleanup` re-exports the shared
  `CleanupFailures`; `docker.transactions.errors` imports
  `attach_secondary`, `carry_secondary_diagnostics`, and
  `_MAX_SECONDARY_NOTES` from the foundation. Identity check:

  ```text
  docker.transactions.cleanup.CleanupFailures is docker.filesystem.cleanup.CleanupFailures            -> True
  docker.transactions.errors.attach_secondary is docker.filesystem.cleanup.attach_secondary            -> True
  docker.transactions.errors.carry_secondary_diagnostics is docker.filesystem.cleanup.carry_secondary_diagnostics -> True
  ```

  No transaction-local wrapper exists and no `__module__` is rewritten; the
  shared functions report `docker.filesystem.cleanup`.
- **1.8** `FoundationImportBoundaryTests` asserts `cleanup.py` imports only
  `sys.stdlib_module_names`, `__init__.py` contains no `Import`/`ImportFrom`
  and no `__all__` assignment, and no `docker.filesystem` module imports a
  domain package or `docker.transactions`.
- **1.9** `FoundationOwnershipTests` compares identity, defining modules, and
  `inspect.signature` between the transaction compatibility imports and the
  foundation API, and pins the exact public signatures of
  `CleanupFailures.__init__`/`run`/`complete`, `attach_secondary`, and
  `carry_secondary_diagnostics`.
- **1.10** The gate command above passes: 76 tests, OK.

## Scope note

The change proposal's Impact section explicitly lists "architecture allowlist
tests" as affected. `tests/test_transactions_phase9b_remap_boundaries.py` globs
`docker/**/*.py` and previously treated any non-transaction module defining
`carry_secondary_diagnostics` as a boundary violation. Because the foundation
now intentionally owns that primitive, the allowlist admits
`docker/filesystem/` as a centralized owner alongside `docker/transactions/`.
Domain remap sites remain restricted exactly as before.

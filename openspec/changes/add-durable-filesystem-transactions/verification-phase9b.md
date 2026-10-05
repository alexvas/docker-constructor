# Phase 9B — Narrow Secondary-Diagnostic Remapping (verification)

**Change:** `add-durable-filesystem-transactions`
**Phase:** 9B (tasks 9B.1–9B.6)
**Result:** PASS

## Delivered artifacts

- `docker/transactions/errors.py` — new narrow
  `carry_secondary_diagnostics(target, source)` operation beside the secondary
  storage contract. It reads only the diagnostics `source` already retained
  (`TransactionError.secondary` or the generic private slot), copies them in
  source order, and delegates attachment to the existing centralized
  `attach_secondary`. It accepts no caller-supplied iterable, consumes nothing,
  and changes no target identity, `__cause__`, `__context__`, or unrelated
  notes. It is deliberately **not** re-exported from top-level
  `docker.transactions`.
- Twelve wrapper-to-raw-cause remap sites migrated to the carry operation
  (inventory below).
- Tests:
  - `tests/test_transactions_phase9b_carry.py` (9B.1, 15 tests)
  - `tests/test_transactions_phase9b_remap_boundaries.py` (9B.2, 9 tests)

## Carry contract (9B.1, 9B.3)

`carry_secondary_diagnostics(cause, wrapper)`:

- Object-capable targets (any exception that permits attribute storage, plus
  `TransactionError`) retain the **secondary exception objects** and de-duplicate
  them **by identity** across repeated carries.
- Targets that cannot carry attributes receive only bounded
  `BaseException.add_note()` text; the per-primary note budget
  (`_MAX_SECONDARY_NOTES`, counting pre-existing notes) bounds repeated carries
  without promising object retention or identity de-duplication.
- No diagnostics is a no-op: no private slot, no notes.
- The source is not mutated and is never carried itself; `__cause__`,
  `__context__`, and unrelated notes are not carried.
- Target identity is preserved; the operation returns `None`.
- The signature is exactly `(target, source)`; a third argument raises
  `TypeError`.

## Migrated remap inventory (9B.2, 9B.4)

All twelve audited wrapper-to-raw-cause sites now call
`carry_secondary_diagnostics(cause, exc)` and no longer read shared storage:

| # | Module | Site | Line |
| --- | --- | --- | --- |
| 1 | `docker/npm_environment/publication.py` | `_identity_lock_failure` | 156 |
| 2 | `docker/npm_environment/publication.py` | `_release_identity_lock` capability release | 188 |
| 3 | `docker/versioning/rendering.py` | effective-inventory replacement mapping | 1733 |
| 4 | `docker/versioning/artifact_cache.py` | `_identity_lock_failure` | 1440 |
| 5 | `docker/versioning/artifact_cache.py` | `FileIdentityLock.release` | 1534 |
| 6 | `docker/versioning/build_cache.py` | `ConstructorProjectBuildLock.release` | 986 |
| 7 | `docker/versioning/build_cache.py` | `_raise_lock_failure` | 1105 |
| 8 | `docker/versioning/build_cache.py` | `_raise_marker_failure` | 1241 |
| 9 | `docker/versioning/build_cache.py` | `_validate_existing_blob` | 1436 |
| 10 | `docker/versioning/build_cache.py` | blob durable-unlink mapping | 1470 |
| 11 | `docker/versioning/effective.py` | runtime projection transaction mapping | 1134 |
| 12 | `docker/versioning/effective.py` | runtime projection capability mapping | 1147 |

The two approved explicit aggregators are unchanged and keep the low-level
`attach_secondary`:

| Aggregator | Location | Why it is not a remap |
| --- | --- | --- |
| Lock descriptor release aggregation | `docker/transactions/locking.py:254` | Constructs a new `LockError` from the first ordinary release failure and attaches the later ones; it does not replace a wrapper with a raw cause. |
| Caller-owned build-lock release reporting | `docker/versioning/build_orchestration.py:901` | Returns the ordinary release failure under a caller-owned result model **and** attaches it to the primary; the accumulator/carry return contract does not express this. |

## Review (9B.5)

Every remaining diagnostic writer/reader was audited:

- **Shared storage/attachment** lives only in
  `docker/transactions/errors.py` (`_retained_secondary`, `attach_secondary`,
  `TransactionError.add_secondary`, `_add_secondary_notes`). No domain module
  reads `_transaction_secondary`, and no `list(exc.secondary)` remap remains
  anywhere in `docker/`.
- **`CleanupFailures`** continues to use the low-level `attach_secondary`
  internally; it is not a remap.
- **Accepted non-remap reader:** `docker/versioning/project_state.py:250`
  (`if exc.secondary:`) inspects the **public** `TransactionError.secondary`
  contract on a typed `DestinationExists` to distinguish a clean concurrent
  collision from a collision whose temporary cleanup also failed. It does not
  replace the wrapper with its raw cause (it raises `ProjectStateError ... from
  exc`, preserving the wrapper and its diagnostics as the cause), so the narrow
  carry operation does not apply. Classified as accepted, documented here.
- **Excluded, domain-owned diagnostic models** (per design): npm
  process-lifecycle/streaming `add_note` paths in
  `docker/npm_environment/execution.py` / `streaming.py` (redaction and
  result-precedence models), activity-monitor policy, user/presentation
  outputs, and metadata cache storage (deferred to
  `revalidate-update-metadata`).

No finding required a code change. Specifically verified:

- No accidental source consumption (a fresh list is copied).
- No duplicate identity on object-capable targets (centralized de-duplication).
- No unbounded textual fallback (bounded per-primary note budget).
- No cause/context transfer and no changed exception chaining (asserted by
  tests, and the migrated sites still `raise cause` exactly as before).
- No misuse of the carry operation for cleanup orchestration or caller-owned
  aggregation (the two aggregators keep `attach_secondary`).

## Validation (9B.6)

Focused suites:

```text
python -m unittest tests.test_transactions_phase9b_carry \
  tests.test_transactions_phase9b_remap_boundaries
  -> Ran 24 tests ... OK
```

Affected transaction / cache / projection / npm / rendering / locking /
orchestration suites:

```text
python -m unittest \
  tests.test_transactions_phase9b_carry \
  tests.test_transactions_phase9b_remap_boundaries \
  tests.test_transactions_phase9a_accumulator \
  tests.test_transactions_phase9a_audit \
  tests.test_transactions_phase9a_build_cache_cleanup \
  tests.test_transactions_locking \
  tests.test_transactions_phase6_effective_projection \
  tests.test_transactions_phase7_artifact_specialization \
  tests.test_transactions_phase8_npm_specialization \
  tests.test_transactions_phase8_npm_leaf_mechanics \
  tests.test_npm_environment_publication \
  tests.test_npm_environment_publication_cleanup \
  tests.test_constructor_build_cache_paths \
  tests.test_constructor_build_cache_permissions \
  tests.test_constructor_build_orchestration \
  tests.test_constructor_build_generation_integration \
  tests.test_version_effective
  -> Ran 438 tests ... OK (skipped=1)
```

Architecture/introspection suites:

```text
python -m unittest tests.test_transactions_phase9_boundaries \
  tests.test_transactions_phase9_consumer_inventory
  -> Ran 37 tests ... OK
```

Type check:

```text
scripts/check-types
  -> All checks passed!
```

Complete suite:

```text
python -m unittest discover -s tests -p 'test_*.py'
  -> Ran 5247 tests ... OK (skipped=13)
```

Diff check:

```text
git diff --check
  -> clean (no whitespace errors)
```

No lint tool is configured in this repository (`.github/workflows/validation.yml`
runs only the pinned `ty` production type check); there is no separate lint
command to run.

### Recorded summary

- **Migrated sites:** the twelve listed above.
- **Unchanged aggregators:** `locking._release_descriptor` and
  `build_orchestration._release_build_lock`.
- **Exclusions:** the npm process-lifecycle/streaming, activity-monitor,
  user/presentation, and metadata-cache diagnostic models.
- **Raw causes, stages, exception identity, chaining, domain mappings, and
  cleanup precedence:** unchanged across all twelve sites.

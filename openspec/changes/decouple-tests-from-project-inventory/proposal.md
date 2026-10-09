# Proposal

## Why

Behavioral tests currently copy or mutate the project's live `docker-constructor.toml`, so routine dependency updates and removal of optional extensions invalidate unrelated launcher, schema, progress, and artifact tests. Following `15c1fdb`, discovery ran 5,694 tests in 95.529 seconds with 148 failures, 11 errors, and 13 skips; the coverage must be repaired rather than deleted or weakened.

## What Changes

- Introduce explicit, stable test-owned reviewed-inventory fixtures and small scenario builders that never derive their baseline from the project's inventory.
- Separate exact behavioral tests from real-inventory contract checks; preserve real TOML parsing, schema validation, orchestration, artifact verification, and cleanup integration.
- Migrate launcher fixtures, shared build support, runtime-schema tests, Pi release tests, artifact selection, progress reporting, effective-state/environment tests, and visual-boundary checks to the appropriate fixture or live-inventory lane.
- Retain every broken scenario and assertion intent, including inherited launcher cases, malformed integrity, unsupported algorithms, alternate-version overrides, shared identities, and effect ordering.
- Add an independence regression gate and record coverage mapping and validation evidence outside planning artifacts.

## Capabilities

### New Capabilities

None. This is test tooling and test refactoring only; `skip_specs: true` is declared.

### Modified Capabilities

None. Existing production contracts, including `configuration-document-validation`, remain unchanged.

## Impact

Only test fixtures, test helpers, and tests are implementation targets. Principal affected paths are `tests/build_test_support.py`, `tests/test_constructor_launcher.py`, `tests/test_constructor_inventory_runtime.py`, `tests/test_constructor_build_materialization.py`, `tests/test_constructor_pi_inventory.py`, `tests/test_constructor_check_updates_acceptance.py`, `tests/test_constructor_facade.py`, `tests/test_version_effective.py`, and `tests/test_version_visual_boundaries.py`; their shared-helper consumers must also be audited. No changes to production code, dependency versions, the real reviewed/local configuration, public APIs, or production specifications are authorized.

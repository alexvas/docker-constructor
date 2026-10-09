# Tasks

Only tests, test fixtures/helpers, and separate implementation evidence may change. Do not edit production code or the project's reviewed/local inventory. Keep all existing behavioral scenarios; skips, expected failures, removed negative cases, or weakening assertions are not fixes. Follow design.md; record results and coverage mapping in `verification.md`, not in planning artifacts. Each group must pass its focused gate before proceeding.

## 1. Baseline and fixture ownership

- [ ] 1.1 Capture full discovery results and fully qualified failure/error cases in `verification.md`; audit canonical TOML reads/copies/mutations and `tests/build_test_support.py` consumers, classify behavioral versus live-contract ownership, and verify every reported failed case or inherited family has an assertion-intent entry.
- [ ] 1.2 Add committed stable reviewed TOML fixtures under `tests/fixtures/` covering mandatory build stages, Pi release metadata, default/alternate runtime artifact versions, and override policies; add self-tests using the real loader and verify every positive baseline loads without repository configuration or network access.
- [ ] 1.3 Add small test-data helpers for fresh temporary project documents, deterministic artifact bytes/SRI, optional Dockerfile/local companion, and named scenario mutations; verify two independently created fixtures cannot contaminate one another and negative mutations reject the intended field rather than an unrelated missing field.
- [ ] 1.4 Document fixture ownership and usage in test-helper docstrings or a fixture README; verify examples run and explicitly distinguish stable behavioral data from live-project contract tests.

## 2. Shared build support and launcher integration

- [ ] 2.1 Replace the real-inventory copy in `tests/build_test_support.py` with stable test data while preserving its existing exported interfaces where safe; run helper self-tests and audited consumer suites, adjusting only behavioral fixture assumptions and recording any intentionally live callers.
- [ ] 2.2 Refactor `TestRunTransaction._make_fixture_toml()` to use an independent complete runtime scenario with locally derived integrity for default and alternate artifacts; verify all inherited launcher success, override, cache, dry-run, TTY, mount, projection, cleanup, and shared-identity cases pass in `python -m unittest tests.test_constructor_launcher`.
- [ ] 2.3 Repair malformed-integrity and unsupported-algorithm fixture creation without searching the real inventory; verify their precise rejection and no-network/no-cache/no-Docker assertions, as well as orchestration ordering and rejected-version cases, pass and retain their original intent.
- [ ] 2.4 Update the coverage ledger for shared-helper consumers and all inherited launcher families; verify no previously covered scenario was removed, skipped, or replaced with a setup-only assertion.

## 3. Schema, artifact, and Pi release assertions

- [ ] 3.1 Migrate behavioral canonical reads in `tests/test_constructor_inventory_runtime.py` to stable fixtures; retain full extension preservation, override-neighbor rejection, unknown validation field, and pre-effect negative scenarios; verify the entire module passes with exact intended error fields.
- [ ] 3.2 Move exact artifact URL/SHA/platform/order expectations in `tests/test_constructor_build_materialization.py` onto fixed reviewed test data; verify the module passes and a deliberately mismatched expected artifact is detected without deriving expectations from the selector under test.
- [ ] 3.3 Separate fixed Pi release-contract behavior from the current reviewed Pi version in `tests/test_constructor_pi_inventory.py`; verify source/package/repository/tag/provider assertions remain exact on a fixture and the real project inventory still receives an explicit release-contract smoke check.
- [ ] 3.4 Record migrated schema/artifact/Pi assertion mappings and focused commands in `verification.md`; verify every originally failing case in these modules is accounted for.

## 4. Reporting, effective state, and visual boundaries

- [ ] 4.1 Supply a stable target fixture to progress tests in `tests/test_constructor_check_updates_acceptance.py` and `tests/test_constructor_facade.py`; verify exact totals/order, event updates, line clearing, handled diagnostics, and interruption identity in both complete module suites, without hardcoding the live inventory's target count.
- [ ] 4.2 Migrate exact default-extension, version, and environment assertions in `tests/test_version_effective.py` to a stable fixture; verify all override/selection/environment scenarios pass and do not silently skip an absent optional extension.
- [ ] 4.3 Split `tests/test_version_visual_boundaries.py` into fixed-fixture display/header examples and live declared-owner checks; verify canonical header checks derive owner membership independently from raw TOML and negative visual-boundary tests still detect missing/misplaced headers.
- [ ] 4.4 Record reporting/effective/visual coverage mappings and focused results in `verification.md`; verify exact behavioral oracles were preserved rather than replaced with production-generated expected output.

## 5. Live inventory contracts and independence regressions

- [ ] 5.1 Consolidate explicit live-inventory checks for successful loading, selected artifact/platform agreement, Pi source contract, effective-state/environment propagation, and declared-owner visual headers; add test-owned valid variants with bumped versions and removed optional extensions plus inconsistent negative variants, and verify valid changes pass while malformed or inconsistent declarations fail.
- [ ] 5.2 Add an executable narrow dependency audit for migrated behavioral fixture sources, with explicit live-contract exceptions; verify it detects representative forbidden reads/copies of the repository inventory while accepting committed fixture reads and intentional live tests.
- [ ] 5.3 Add a subprocess regression denying reads of the resolved repository `docker-constructor.toml` before helper/test imports and executing representative migrated behavioral suites; verify it detects the original copy/read coupling, passes after migration, and never edits the repository inventory.
- [ ] 5.4 Document the two test lanes, permitted live checks, and dependency-bump expectations next to fixture support; record RED/GREEN independence evidence in `verification.md` and verify future version/optional-extension updates require no behavioral expectation edits.

## 6. Integration validation

- [ ] 6.1 Run all migrated module suites and every audited shared-helper consumer; verify all pass and record command outputs plus complete old-to-new coverage mapping with no newly introduced skips or expected failures.
- [ ] 6.2 Run `python -m unittest discover -s tests -p 'test_*.py'`; require zero failures/errors, record total cases, duration, skips and their unchanged reasons, and investigate any discrepancy in discovered coverage rather than relying on count alone.
- [ ] 6.3 Run `git diff --check`, the executable independence audit, and inspect changed paths; verify no production code, real inventory, or production spec changed, and record results in `verification.md`.
- [ ] 6.4 Run `openspec validate decouple-tests-from-project-inventory --strict`; require success before reporting the implementation complete.

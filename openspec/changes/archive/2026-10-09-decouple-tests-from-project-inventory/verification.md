# Verification evidence — decouple-tests-from-project-inventory

Implementation evidence for the change. Planning artifacts
(`proposal.md`, `design.md`, `tasks.md`) are not modified except for task
checkboxes.

## 1. Baseline discovery (pre-change, commit `c529364`)

```
$ python -m unittest discover -s tests -p 'test_*.py'
Ran 5694 tests in 95.450s
FAILED (failures=148, errors=11, skipped=13)
```

Raw output: captured during the implementation session. The 159
failure/error result lines collapse to the families below; inherited
launcher methods appear once per inheriting class
(`TestRunTransaction`, `TestOrchestrateRunExecutionModes`,
`TestEndToEndPlanningGuards`).

### Failure / error inventory with assertion intent and lane

| Family (module / tests) | Result | Assertion intent | Lane |
| --- | --- | --- | --- |
| `test_constructor_launcher.TestRunTransaction`, `TestOrchestrateRunExecutionModes`, `TestEndToEndPlanningGuards` — `test_successful_run_produces_run_args`, `test_successful_run_invokes_executor`, `test_tty_on_by_default`, `test_interactive_on_by_default`, `test_valid_override_produces_success`, `test_override_selects_alternate_artifact`, `test_default_no_overrides_produces_success`, `test_process_result_captured`, `test_container_name_allocated`, `test_no_executor_and_not_dry_run_is_error`, `test_no_inspector_is_error`, `test_executor_*`, `test_dry_run_*` (20 cases), `test_projection_*` (10 cases), `test_workspace_1to1_in_result`, `test_projection_mounted_readonly`, `test_shared_integrity_*`, `test_cache_*`, `test_malformed_integrity_fails_before_any_effect`, `test_unsupported_algorithm_fails_before_any_effect`, `test_default_factory_*` | 142 fail + 9 err | Launcher success/override/cache/dry-run/TTY/mount/projection/cleanup/shared-identity/negative-pre-effect behaviour | Behavioural (stable fixture) |
| `test_constructor_launcher.TestOrchestrationOrdering.test_event_order` | 1 fail | validate→materialize→projection→vector→docker→cleanup ordering | Behavioural (stable fixture) |
| `test_constructor_inventory_runtime.TestClosedRuntimeSchema` — `test_valid_full_extension_is_preserved`, `test_reviewed_extension_overrides_accept_declared_versions` (×2 subtests), `test_reviewed_runtime_packages_all_present`, `test_unknown_validation_field_rejected` | 2 fail + 3 err | Full-extension preservation, override-neighbour rejection, reviewed package membership, unknown validation-field rejection | Behavioural (stable fixture) + live structural |
| `test_constructor_build_materialization.TestBuildArtifactSelection.test_exact_effective_linux_amd64_pairs` | 1 fail | Exact rustup/uv/rtk/fd URL+SHA+order | Behavioural (stable fixture) |
| `test_constructor_pi_inventory.TestReviewedPiReleaseContract.test_real_inventory_records_pi_release_contract` | 1 fail | Pi source/package/repository/tag/provider contract | Behavioural (fixture) + live source contract |
| `test_constructor_check_updates_acceptance.TestCheckUpdatesAcceptance.test_interactive_progress_then_clean_report` | 1 fail | Interactive progress line count/step | Behavioural (stable target fixture) |
| `test_constructor_facade.TestInteractiveProgress` — `test_progress_content_compact_targets_and_event_updates`, `test_cleanup_before_handled_diagnostics`, `test_interruption_clears_line_and_propagates` | 3 fail | Exact target count/order, line clearing, interruption identity | Behavioural (stable target fixture) |
| `test_version_effective.TestDefaultSelection.test_pi_extensions_preserved`, `TestEnvironmentMapping.test_no_missing_extensions` | 2 fail | Default extension preservation and env mapping | Behavioural (stable fixture) |
| `test_version_visual_boundaries.TestCanonicalInventoryVisualHeaders` — `test_code_owners_match_canonical_blocks`, `test_visual_header_immediately_before_each_owner` (×2 owners) | 3 fail | Declared-owner headers | Behavioural examples (fixture) + live declared owners |

### Root causes

* `tests/build_test_support.py` and
  `TestRunTransaction._make_fixture_toml()` /
  `TestOrchestrationOrdering._make_fixture_toml()` copy/mutate the real
  `docker-constructor.toml`. After `15c1fdb` removed `pi-read`/`pi-usage`
  the injected `pi-read` artifact table creates an incomplete extension
  and a schema error inherited by three launcher classes.
* `test_constructor_inventory_runtime` reads and appends to the canonical.
* `test_constructor_build_materialization` pins pre-bump artifact values.
* `test_constructor_pi_inventory` pins Pi `0.85.1`.
* Progress tests pin the live target count (`14`) and last target
  (`pi-usage`).
* `test_version_effective` and `test_version_visual_boundaries` pin
  removed optional extensions.

## 2. Canonical TOML coupling audit

Classification of every `docker-constructor.toml` read/copy/mutation in
`tests/` (from `grep -rn "docker-constructor.toml" tests/`).

### Migrated to the behavioural fixture lane

| Site | Nature | New source |
| --- | --- | --- |
| `tests/build_test_support.py` | copy of real inventory | `tests/fixtures/stable_inventory.toml` |
| `tests/test_constructor_launcher.py` `_make_fixture_toml` (×2) | copy + mutate | stable fixture copy + local SRI |
| `tests/test_constructor_launcher.py` `_make_malformed_inventory`, `_make_single_extension_fixture` | copy + mutate | stable fixture copy |
| `tests/test_constructor_inventory_runtime.py` `_read_canonical`, `_canonical_path` | read + append | stable fixture text/path |
| `tests/test_constructor_build_materialization.py` `selection()`, rustup contract | read | stable fixture via helper |
| `tests/test_version_effective.py` `_default_inventory` | read | stable fixture via helper |
| `tests/test_version_visual_boundaries.py` fixed examples | read | stable fixture via helper |
| `tests/test_constructor_pi_inventory.py` fixed release contract | read | stable fixture via helper |
| `tests/test_constructor_check_updates_acceptance.py`, `tests/test_constructor_facade.py` progress | read via CLI project dir | stable fixture project directory |

### Retained intentionally as live-contract tests

| Site | Live invariant |
| --- | --- |
| `tests/test_constructor_pi_inventory.py` node/npm/Pi source | Reviewed node/npm metadata, digest pin, Pi source contract (no version literal) |
| `tests/test_constructor_inventory_runtime.py` live class | Real document loads; every declared extension loads; metadata_file safe |
| `tests/test_version_visual_boundaries.py` live class | Every declared owner in raw TOML has the matching header |
| `tests/versioning/...`, configuration/validation, corporate-network, host-access suites | Pre-existing real-inventory integration contracts outside this change's scope |

### Shared-helper (`tests/build_test_support.py`) consumers

Audited: `test_constructor_project_state_phase1.py`,
`test_build_context_confinement.py`, `test_retained_tail_phase6.py`,
`test_output_configuration.py`, `test_constructor_project_root_phase7.py`,
`test_constructor_project_root_phase3.py`,
`test_constructor_build_materialization.py`, `test_constructor_acceptance.py`,
`test_constructor_phase4_named_context.py`,
`test_constructor_build_orchestration.py`,
`test_constructor_build_output_acceptance.py`,
`test_constructor_build_output.py`. They consume `INVENTORY_PATH` as an
opaque project document and inject fake artifact materialization, so they
are version-independent. Results recorded in §6.

## 3. Group 1 — baseline, fixtures, helpers, ownership

### 1.1 Baseline and audit

* Baseline discovery captured above.
* Full discovery output inspected; every reported failure/error family has
  an assertion-intent entry above.
* Canonical TOML reads/copies/mutations and `build_test_support.py`
  consumers audited and classified above.

### 1.2 Stable fixture

* `tests/fixtures/stable_inventory.toml` committed. It carries mandatory
  build stages, Pi release metadata, default/alternate runtime artifact
  versions (`pi-read` `0.2.1` + `0.3.0`), and override policies.
* `tests/test_inventory_fixtures.py::TestStableFixtureBaseline` loads
  every positive baseline through the real loader, including from an
  unrelated working directory, with no repository configuration or
  network access:
  `python -m unittest tests.test_inventory_fixtures` → **15 tests, OK**.

### 1.3 Fresh-document helpers and named mutations

* `tests/inventory_fixtures.py` provides fresh temporary project
  documents (`write_stable_project`, `write_stable_inventory`),
  deterministic artifact bytes/SRI (`artifact_bytes`,
  `artifact_integrity`, `rewrite_artifact_integrities`), and named
  mutations (`remove_extension`).
* `TestFreshDocumentsAreIndependent` and `TestNamedMutations` verify:
  two independently created fixtures cannot contaminate one another; a
  `scheme` mutation rejects `scheme`; a removed mandatory artifact table
  rejects `pi-proxy.artifacts` (not an unrelated field).

### 1.4 Fixture ownership documentation

* `tests/fixtures/README.md` documents the two lanes, permitted live
  checks, and dependency-bump expectations. The helper docstrings and
  README distinguish stable behavioural data from live-project contract
  tests; the usage snippet matches the implemented API.

## 4. Group 2 — shared build support and launcher integration

### 2.1 `tests/build_test_support.py`

* `INVENTORY_PATH` is now written from `stable_inventory_text()` instead
  of copied from the repository. The unused `shutil` import was removed.
* Helper self-tests: `python -m unittest tests.test_inventory_fixtures` →
  **15 tests, OK**.
* Audited shared-helper consumer suites (excluding
  `test_constructor_build_materialization`, migrated in group 3):

```
$ python -m unittest \
    tests.test_constructor_project_state_phase1 \
    tests.test_build_context_confinement \
    tests.test_retained_tail_phase6 \
    tests.test_output_configuration \
    tests.test_constructor_project_root_phase7 \
    tests.test_constructor_project_root_phase3 \
    tests.test_constructor_phase4_named_context \
    tests.test_constructor_build_output_acceptance \
    tests.test_constructor_build_output
Ran 171 tests in 29.894s
OK (skipped=1)

$ python -m unittest tests.test_constructor_acceptance tests.test_constructor_build_orchestration
Ran 158 tests in 4.229s
OK
```

No intentionally live callers were introduced.

### 2.2 `TestRunTransaction._make_fixture_toml()`

* Now opens an independent copy of the committed stable fixture and
  rewrites every `url`/`integrity` pair to locally derived bytes. The
  default `pi-read` `0.2.1` and reviewed `0.3.0` alternate come from the
  fixture; no repository inventory read.
* Full inherited suite (success, override, cache, dry-run, TTY, mount,
  projection, cleanup, shared-identity):

```
$ python -m unittest tests.test_constructor_launcher
Ran 263 tests in 1.550s
OK
```

### 2.3 Malformed-integrity / unsupported-algorithm / single-extension fixtures

* `_make_malformed_inventory` and `_make_single_extension_fixture` now
  build from the stable fixture text. No repository inventory search.
* Precise rejection, no-network/no-cache/no-Docker, orchestration
  ordering, and rejected-version assertions all pass in the launcher
  suite above.

### 2.4 Coverage ledger — inherited launcher families

No scenario was removed, skipped, or reduced to a setup-only assertion.
The 45 `TestRunTransaction` cases, 50
`TestOrchestrateRunExecutionModes` cases (inherited), 47
`TestEndToEndPlanningGuards` cases (inherited), and the
`TestOrchestrationOrdering` event-order case all pass with their
original assertions. Exact override/cache/dry-run oracles remain
independent of the projection selector under test.

## 5. Group 3 — schema, artifact, and Pi release assertions

### 3.1 `tests/test_constructor_inventory_runtime.py`

* `_read_canonical()` now returns `stable_inventory_text()` and
  `TestClosedRuntimeSchema._canonical_path()` returns the stable fixture
  path. Every append-snippet / negative scenario therefore starts from the
  committed fixture. Full extension preservation, override-neighbour
  rejection, unknown validation field, and pre-effect negatives retained.
* New `TestLiveInventoryRuntimeContract` reads the real inventory and
  asserts structural / self-consistency invariants only (successful load;
  every declared extension has npm source/provider, its version present in
  `artifacts`, sha512 integrity, validation metadata, and satisfies its own
  override constraint). No extension membership or version is pinned.

```
$ python -m unittest tests.test_constructor_inventory_runtime
Ran 61 tests in 0.103s
OK
```

Originally failing cases accounted for: `test_valid_full_extension_is_preserved`,
`test_reviewed_extension_overrides_accept_declared_versions` (×2),
`test_reviewed_runtime_packages_all_present`, `test_unknown_validation_field_rejected`
— all pass against the fixture.

### 3.2 `tests/test_constructor_build_materialization.py`

* `selection()`, `test_rustup_is_pinned_to_reviewed_immutable_archive_source`,
  and the orchestration/transport tests now build from the committed
  fixture through `write_stable_project`; no repository inventory read.
* The exact URL/SHA/platform/order oracle is a module constant
  (`_EXPECTED_LINUX_AMD64_PAIRS`), independent of `select_build_artifacts`.
* New `test_fixed_oracle_detects_mismatched_artifact` proves the oracle
  detects a deliberately mismatched fixture artifact (a changed uv sha256)
  without deriving the expectation from the selector under test.

```
$ python -m unittest tests.test_constructor_build_materialization
Ran 20 tests in 0.214s
OK
```

### 3.3 `tests/test_constructor_pi_inventory.py`

* New `TestStablePiReleaseContract` asserts the exact fixture version
  (`0.85.1`) plus source/package/repository/tag contract and effective
  projection.
* `TestReviewedPiReleaseContract` now performs a live source-contract smoke
  check (type/package/repository/tag, non-empty version) without pinning
  the current version.

```
$ python -m unittest tests.test_constructor_pi_inventory
Ran 15 tests in 0.020s
OK
```

### 3.4 Mapping

| Originating failure | Original intent | New owner |
| --- | --- | --- |
| `test_valid_full_extension_is_preserved` | full extension retention | fixture (`pi-read` 0.2.1) |
| `test_reviewed_extension_overrides_accept_declared_versions` | override accept/reject neighbour | fixture |
| `test_reviewed_runtime_packages_all_present` | declared membership | fixture |
| `test_unknown_validation_field_rejected` | exact invalid field | fixture |
| `test_exact_effective_linux_amd64_pairs` | exact artifact oracle | fixture constant |
| `test_real_inventory_records_pi_release_contract` | Pi release contract | fixture (exact) + live (source contract) |

## 6. Group 4 — reporting, effective state, and visual boundaries

### 4.1 Progress tests

* `tests/test_constructor_check_updates_acceptance.py` and
  `tests/test_constructor_facade.py` now run `check-updates` against an
  independent stable project document (`write_stable_project`), supplied
  through `--project-directory`. The exact totals/order come from the
  committed fixture (14 targets, last `runtime.pi-extensions.pi-usage`),
  never from the live inventory.
* Two facade tests that previously invoked `main`/`_run` directly were
  given the same `--project-directory`, so no progress test reads the
  repository inventory.
* Event updates, line clearing, handled diagnostics, and interruption
  identity are asserted unchanged.

```
$ python -m unittest tests.test_constructor_check_updates_acceptance tests.test_constructor_facade.TestInteractiveProgress
Ran 10 tests in 0.121s
OK

$ python -m unittest tests.test_constructor_facade
Ran 164 tests in 0.699s
OK
```

### 4.2 `tests/test_version_effective.py`

* `_INVENTORY_TOML` now points at the stable fixture; all exact default
  extension/version/environment assertions run against fixed test data.
* `test_pi_extensions_preserved` and `test_no_missing_extensions` pass;
  the absent-extension guard (`PI_READ_VERSION`/`PI_USAGE_VERSION`/
  `PI_PROXY_VERSION`) now fails loudly if the fixture ever drops one.

```
$ python -m unittest tests.test_version_effective
Ran 38 tests in 0.021s
OK
```

### 4.3 `tests/test_version_visual_boundaries.py`

* Fixed-fixture lane `TestStableFixtureVisualHeaders`: exact
  owner→display mapping (`CANONICAL_OWNERS`) plus header-immediately-before
  checks against the stable fixture.
* Negative boundary test `test_missing_or_misplaced_header_is_detected`
  proves the header check detects both a removed header and a moved
  (misplaced) header.
* Live-contract lane `TestLiveInventoryVisualHeaders`: derives declared
  owners from the document (via validated targets grouped by replacement
  owner) and asserts each owner's header, without hardcoding membership.
* `TestFragmentVisualHeaders` retains independent `display_path` oracles.

```
$ python -m unittest tests.test_version_visual_boundaries
Ran 9 tests in 0.012s
OK
```

### 4.4 Mapping

| Originating failure | Original intent | New owner |
| --- | --- | --- |
| `test_interactive_progress_then_clean_report` | exact interactive progress | stable fixture project |
| `test_progress_content_compact_targets_and_event_updates` | count/order + one line | stable fixture project |
| `test_cleanup_before_handled_diagnostics` | clear before diagnostics | stable fixture project |
| `test_interruption_clears_line_and_propagates` | interruption identity | stable fixture project |
| `test_pi_extensions_preserved` | default extension preservation | stable fixture |
| `test_no_missing_extensions` | env mapping completeness | stable fixture |
| `test_code_owners_match_canonical_blocks` | declared owners | fixture (exact) + live (derived) |
| `test_visual_header_immediately_before_each_owner` | header placement | fixture (exact) + live (derived) |

No exact behavioural oracle was replaced by production-generated expected
output.

## 7. Group 5 — live-inventory contracts and independence regressions

### 5.1 Consolidated live-inventory contract

`tests/test_live_inventory_contract.py` is the single live-contract lane.
`TestLiveInventoryContract` asserts, against the real
`docker-constructor.toml`:

* successful load and per-extension self-consistency;
* selected artifact / platform agreement (each selected `linux-amd64`
  artifact matches its declared URL and SHA-256);
* Pi source contract and its effective-projection propagation;
* effective-state / environment propagation (declared tool versions, node
  base image derivation, and every declared extension's env value);
* declared-owner visual headers.

`TestValidVariants` proves the contract accepts a test-owned bumped
reviewed version (pi-read `0.3.0`) and a removed optional extension
(pi-read removed). `TestInconsistentVariantsRejected` proves a bumped
version without an artifact, a mismatched artifact URL, and a plain-npm Pi
source are all rejected.

```
$ python -m unittest tests.test_live_inventory_contract
Ran 11 tests in 0.018s
OK
```

### 5.2 Narrow executable dependency audit

`tests/test_inventory_independence.py`:

* `TestMigratedSourcesHaveNoRepositoryCoupling` scans every migrated
  behavioural source for repository-inventory references. The detection is
  narrow: it requires the inventory filename **and** a repository-root
  construct, so temporary files that merely share the filename are
  accepted. Live references are allowed only through named per-line
  markers in `LIVE_CONTRACT_EXCEPTIONS` (never a blanket file exemption).
* `TestAuditDetectsRepresentativeCoupling` detects the original
  `build_test_support.py` copy, a direct `ROOT / "docker-constructor.toml"`
  read, and the launcher copy pattern.
* `TestAuditAcceptsLegitimateSources` accepts committed fixture reads,
  temporary outputs named like the inventory, and the named live
  exceptions.

```
$ python -m unittest tests.test_inventory_independence.TestMigratedSourcesHaveNoRepositoryCoupling tests.test_inventory_independence.TestAuditDetectsRepresentativeCoupling tests.test_inventory_independence.TestAuditAcceptsLegitimateSources
Ran 8 tests in 0.003s
OK
```

### 5.3 Subprocess read-denial regression

`TestSubprocessDeniesRepositoryInventoryReads` installs a `sys.addaudithook`
`open` denial for the resolved repository inventory in a subprocess, then:

* **GREEN**: imports helpers and runs representative migrated suites
  (`test_inventory_fixtures`, launcher `TestRunTransaction`,
  inventory-runtime `TestClosedRuntimeSchema`, materialization
  `TestBuildArtifactSelection`, `test_version_effective`, check-updates
  acceptance, facade `TestInteractiveProgress`, and fixture visual
  headers). The runner asserts a non-zero loaded test count. All pass with
  the repository inventory denied.
* **RED**: a synthetic copy of the original coupling
  (`shutil.copyfile(REPO/docker-constructor.toml, tmp)`) and a direct
  `Path(_REPO, "docker-constructor.toml").read_text()` both fail with
  `repository inventory read denied`.
* The repository inventory hash is asserted unchanged before/after.

```
$ python -m unittest tests.test_inventory_independence.TestSubprocessDeniesRepositoryInventoryReads
Ran 4 tests in 1.061s
OK
```

### 5.4 Two-lane documentation and bump expectations

* `tests/fixtures/README.md` documents the behavioural and live-contract
  lanes, the permitted live checks, and that dependency bumps /
  optional-extension changes must not require behavioural expectation
  edits. The helper module docstrings carry the same contract.
* Variant acceptance (§5.1) demonstrates that a bumped version and a
  removed optional extension pass the live contract without editing any
  behavioural oracle.

## 8. Group 6 — integration validation

### 6.1 Migrated suites + shared-helper consumers

All migrated module suites and every audited `tests/build_test_support.py`
consumer run together:

```
$ python -m unittest \
    tests.test_inventory_fixtures \
    tests.test_constructor_launcher \
    tests.test_constructor_inventory_runtime \
    tests.test_constructor_build_materialization \
    tests.test_constructor_pi_inventory \
    tests.test_constructor_check_updates_acceptance \
    tests.test_constructor_facade \
    tests.test_version_effective \
    tests.test_version_visual_boundaries \
    tests.test_live_inventory_contract \
    tests.test_inventory_independence \
    tests.test_constructor_project_state_phase1 \
    tests.test_build_context_confinement \
    tests.test_retained_tail_phase6 \
    tests.test_output_configuration \
    tests.test_constructor_project_root_phase7 \
    tests.test_constructor_project_root_phase3 \
    tests.test_constructor_acceptance \
    tests.test_constructor_phase4_named_context \
    tests.test_constructor_build_orchestration \
    tests.test_constructor_build_output_acceptance \
    tests.test_constructor_build_output
Ran 941 tests in 37.718s
OK (skipped=1)
```

The single skip is the pre-existing environmental
`sudo runuser -u nobody unavailable` case. No newly introduced skips or
expected failures.

### Coverage mapping (old → new)

| Group | Old failing family | New lane / file |
| --- | --- | --- |
| 2 | 142 launcher failures + 9 errors across `TestRunTransaction`, `TestOrchestrateRunExecutionModes`, `TestEndToEndPlanningGuards`; `TestOrchestrationOrdering.test_event_order` | stable fixture (launcher) |
| 3 | `test_valid_full_extension_is_preserved`, `test_reviewed_extension_overrides_accept_declared_versions` (×2), `test_reviewed_runtime_packages_all_present`, `test_unknown_validation_field_rejected` | stable fixture (inventory runtime) + live structural |
| 3 | `test_exact_effective_linux_amd64_pairs` | fixed constant oracle (materialization) |
| 3 | `test_real_inventory_records_pi_release_contract` | fixture exact + live source contract |
| 4 | `test_interactive_progress_then_clean_report`; facade `test_progress_content_*`, `test_cleanup_before_handled_diagnostics`, `test_interruption_clears_line_and_propagates` | stable fixture project (reporting) |
| 4 | `test_pi_extensions_preserved`, `test_no_missing_extensions` | stable fixture (effective) |
| 4 | `test_code_owners_match_canonical_blocks`, `test_visual_header_immediately_before_each_owner` (×2) | fixture exact + live derived (visual) |

No previously covered scenario was removed, skipped, or reduced to a
setup-only assertion.

### 6.2 Full discovery

```
$ python -m unittest discover -s tests -p 'test_*.py'
Ran 5768 tests in 99.043s
OK (skipped=13)
```

Before: `Ran 5694 tests in 95.450s / FAILED (failures=148, errors=11, skipped=13)`.
After: 0 failures, 0 errors, 13 skips (+74 new tests from the fixture,
audit, live-contract, independence, and review-regression suites). The 13
skips are unchanged pre-existing environmental skips:

* `run through validate-bound-pi-assembly-execution-phase6-host` (1);
* `run through validate-bound-pi-assembly-execution-phase6-registry` (7);
* `an existing 'docker-dev' account is required` (3);
* `sudo runuser -u nobody unavailable` (1);
* `set NPM_ENV_REAL_DOCKER=1 to run the real pinned-container smoke test` (1).

### 6.3 Static boundary + audit

```
$ git diff --check
(no output, exit 0)

$ python -m unittest tests.test_inventory_independence
Ran 12 tests in 1.063s
OK
```

Changed paths: only `tests/` and
`openspec/changes/decouple-tests-from-project-inventory/` (tasks.md,
verification.md). No production code, no real reviewed/local inventory
(`docker-constructor.toml`, `docker-constructor.local.toml`), and no
production spec changed.

### 6.4 OpenSpec validation

```
$ openspec validate decouple-tests-from-project-inventory --strict
Change 'decouple-tests-from-project-inventory' is valid
```

## 9. Review revision (independent membership, fixture Node metadata, zero extensions, audit hardening)

This section supersedes the corresponding claims in §5–§6 where they
conflict.

### 9.1 Independent live visual-header owner membership

* New `tests/live_owner_audit.py` enumerates owners **directly from parsed
  TOML** (`independent_update_owners`): a table declaring an `update`
  subtable owns itself unless an ancestor already owns it (nested rustup
  belongs to its rust parent); each Pi extension owns its own block. It
  never calls production target/grouping code.
* `tests/test_version_visual_boundaries.py`:
  * `TestStableFixtureVisualHeaders` compares the independent set to the
    exact `CANONICAL_OWNERS` mapping and to production membership
    (`_assert_independent_membership`);
  * `TestLiveInventoryVisualHeaders.test_every_declared_owner_has_matching_header`
    derives owners independently, cross-checks production membership, and
    checks each owner's header with an independently computed display path;
  * `test_production_omission_is_detected` (fixture) and
    `test_owner_membership_omission_is_detected` (live) simulate production
    dropping `build.stages.toolchain.ty` and assert the membership
    assertion fails.
* `tests/test_live_inventory_contract.py` `_LiveContractMixin` gained
  `assert_owner_membership` (independent vs production, with `omit_paths`)
  and `assert_owner_headers` now checks the independent set; a live
  omission regression was added.

### 9.2 Node/npm literals on the fixture lane

`tests/test_constructor_pi_inventory.py` now has three lanes:

* `TestStableNodeVersions` — exact `24.18.0` / `11.16.0` / digest
  assertions against `stable_inventory_path()`; the
  `node_version_never_inferred_from_tag` test is unchanged.
* `TestLiveNodeMetadataContract` — real inventory load and
  environment propagation with expectations derived from the declared
  inputs (registry/repository/tag/digest), no fixed release literals.
* `TestSmokeToolConsistency` — compares the standalone smoke runner's
  `SMOKE_NODE_VERSION` / `SMOKE_NPM_VERSION` against the **test-owned
  stable fixture** (not the repository inventory). The standalone runner
  accepts a caller-provided image, so its constants are not repository
  inventory policy; the audit allowlist no longer covers this class, and
  the test runs in the read-denial subprocess suite.
* `TestNodeMetadataVariant` — a valid fixture variant with changed Node
  tag/version/npm/digest loads and propagates, proving the live checks do
  not pin today's release.

### 9.3 Zero optional extensions accepted

* `assertTrue(inventory.runtime_pi_extensions)` removed from
  `TestLiveInventoryRuntimeContract.test_repository_inventory_loads`
  (now asserts a `Mapping`) and from
  `_LiveContractMixin.assert_load_contract`; per-extension assertions
  execute only for declared extensions.
* `tests/inventory_fixtures.py` gained `with_empty_pi_extensions`, which
  removes every optional extension and declares an explicit
  `[runtime.pi-extensions]` table.
* `TestValidVariants.test_empty_optional_extensions_variant_passes` loads
  that variant through the real loader, runs the load and
  effective-environment contracts, and asserts no extension env entries
  are invented. `test_empty_extension_variant_owner_headers_pass` runs the
  owner-header contract on it. `tests/test_inventory_fixtures.py` has a
  self-test for the variant.
* The fixed fixture's exact three-extension assertions are unchanged.

### 9.4 Dependency-audit bypass hardening

`tests/test_inventory_independence.py` replaced line/substring matching
with an AST analysis:

* `find_repository_inventory_coupling` parses the source and follows
  multiline path expressions and straightforward root/path aliases
  (`root = ROOT`, `_REPO = _THIS_DIR.parent`, `path = ROOT / (...)`).
* Live access is restricted to **named test scopes**
  (`LIVE_CONTRACT_SCOPES`) and authorized module-level symbols
  (`LIVE_CONTRACT_SYMBOLS`); the old substring markers are gone. A
  behavioral test referencing an authorized handle, or constructing an
  inline path, fails the audit.
* New negative regressions: multiline path, path alias, root alias, live
  handle misuse from a behavioral test, and inline access from a
  behavioral test. Positive regressions: committed fixture reads,
  temporary output paths, authorized module-level symbol, live handle
  inside its named scope, and inline access inside a named live scope.
* `tests/test_live_inventory_contract.py` was added to the audited
  migrated sources.

### 9.5 Reverification

```
$ python -m unittest <all migrated suites + shared-helper consumers>
Ran 960 tests in 37.814s
OK (skipped=1)

$ python -m unittest discover -s tests -p 'test_*.py'
Ran 5758 tests in 98.693s
OK (skipped=13)

$ python -m unittest tests.test_inventory_independence
Ran 19 tests in 1.350s
OK

$ git diff --check
(no output, exit 0)

$ openspec validate decouple-tests-from-project-inventory --strict
Change 'decouple-tests-from-project-inventory' is valid
```

Changed paths remain limited to `tests/` and the change directory; the
repository inventory, local inventory, and production specs are unchanged.

## 10. Review revision 2 (SRI algorithms, smoke decoupling, audit purity, quoted owner keys)

This section supersedes the corresponding claims in §9 where they conflict.

### 10.1 Accept every supported integrity algorithm

Production's SRI contract (`docker.versioning.integrity.validate_integrity`)
accepts `sha256`/`sha384`/`sha512` with fixed digest byte lengths. The
former `artifact.integrity.startswith("sha512-")` assertions rejected valid
`sha256`/`sha384` declarations.

* `tests/test_constructor_inventory_runtime.py` and
  `tests/test_live_inventory_contract.py` now validate integrity with the
  production `is_valid_integrity` predicate instead of one algorithm; the
  "selected version declares its artifact" checks are unchanged.
* Valid algorithm variants:
  * `TestClosedRuntimeSchema.test_valid_sri_integrity_algorithms_accepted`
    loads a fixture snippet for each of `sha256`/`sha384`/`sha512`;
  * `TestValidVariants.test_sri_algorithm_variants_pass` rewrites the
    stable fixture per algorithm with `rewrite_artifact_integrities` and
    runs the full `assert_load_contract`.
* Negatives retained/added: missing, malformed, truncated, and bad-base64
  integrity tests remain; `test_unsupported_integrity_algorithm_rejected`
  covers `sha1-`.

Coverage mappings: 3.1 (full extension preservation now algorithm-agnostic),
5.1 (valid SRI variants).

### 10.2 Remove repository coupling from smoke consistency

`docker.npm_environment.smoke.run_smoke` accepts a caller-provided image;
its `SMOKE_NODE_VERSION`/`SMOKE_NPM_VERSION` constants are not repository
inventory policy.

* `TestSmokeToolConsistency.test_stable_fixture_matches_smoke_tool_expectations`
  now compares the smoke constants to the **stable fixture**, never the live
  inventory; the docstring no longer claims every reviewed Node bump must
  update the standalone constants.
* `TestSmokeToolConsistency` was removed from `LIVE_CONTRACT_SCOPES`.
* Added to `REPRESENTATIVE_BEHAVIOURAL_TESTS`, so it executes under the
  repository-inventory read-denial audit hook.

Coverage mapping: 3.3 (fixed Pi/Node behavior separated from the reviewed
release; live metadata checks limited to load/propagation).

### 10.3 Audit exceptions restricted to pure path bindings

`tests/test_inventory_independence.py` no longer exempts an authorized
symbol's whole assignment. A module-level live handle is recognized only
when its value is a **pure path construction** (`_is_path_construction`:
string/name leaves, `/` operators, path constructors, navigation
attributes/methods, and pure `os.path.*` helpers).

* Import-time reads are rejected:
  * `REAL_INVENTORY = load_inventory(REPO_ROOT / "docker-constructor.toml")`;
  * `REAL_INVENTORY = (REPO_ROOT / "docker-constructor.toml").read_text()`;
  * `REAL_INVENTORY = (REPO_ROOT / "docker-constructor.toml").read_bytes()`.
* Uses of a permitted path handle outside an authorized live scope still
  fail (`test_detects_live_handle_misuse_from_behavioral_test`).
* New negatives: `test_rejects_import_time_loader_call`,
  `test_rejects_import_time_read_text`, `test_rejects_import_time_read_bytes`.
  New positive: `test_accepts_pure_path_binding_with_navigation`; the
  existing pure `REPO_ROOT / "..."` binding test remains.

Coverage mapping: 5.2 (executable dependency audit).

### 10.4 Quoted TOML owner keys

`tests/live_owner_audit.owner_header_violations` previously located owners
only via the unquoted string `[<owner>]`, so a quoted dotted extension key
(`runtime.pi-extensions."foo.bar"`) was reported missing. It now parses
each candidate header with `tomllib` into logical key segments
(`_table_header_segments`) and compares them to production
`updates.path_segments(owner)`, keeping the dotted name a single key. The
visual comment immediately before the matching table is still required.

* `tests/inventory_fixtures.with_dotted_extension` appends a valid
  `[runtime.pi-extensions."foo.bar"]` extension with its
  `# --- pi-extensions.foo.bar ---` header.
* `TestValidVariants.test_dotted_extension_owner_headers_pass` verifies
  membership and header pass; `test_dotted_extension_variant_passes`
  verifies it loads and propagates.
* Negatives: `test_dotted_extension_missing_header_is_detected` and
  `test_dotted_extension_misplaced_header_is_detected`.

Coverage mappings: 4.3 (independent owner/header checks),
5.1 (valid variants).

### 10.5 Reverification

```
$ python -m unittest <all migrated suites + shared-helper consumers>
Ran 970 tests in 37.777s
OK (skipped=1)

$ python -m unittest <six affected modules>
Ran 152 tests in 1.580s
OK

$ python -m unittest discover -s tests -p 'test_*.py'
Ran 5768 tests in 99.043s
OK (skipped=13)

$ python -m unittest tests.test_inventory_independence
Ran 23 tests in 1.371s
OK

$ git diff --check
(no output, exit 0)

$ openspec validate decouple-tests-from-project-inventory --strict
Change 'decouple-tests-from-project-inventory' is valid
```

Changed paths remain limited to `tests/` and the change directory; the
repository inventory, local inventory, and production specs are unchanged.

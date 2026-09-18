# Phase 1 Validation Record

Validation evidence for the shared configuration-document boundary and its
typed-error projection. Kept out of `tasks.md` per project convention.

## Scope

- `docker/versioning/configuration_document_validation.py`
- `docker/versioning/inventory.py` (reviewed/local loaders and owner-error sites)
- `docker/versioning/errors.py` (`VersionConfigError.field`)

## Checks

| Check | Command | Result |
| --- | --- | --- |
| Focused boundary suites | `python -m unittest tests.test_configuration_document_validation_phase1 tests.test_constructor_inventory_build tests.test_constructor_inventory_root tests.test_constructor_inventory_runtime tests.test_constructor_pi_inventory tests.test_constructor_host_access_acceptance tests.test_constructor_host_access_doctor_red tests.test_constructor_host_access_launch_red tests.test_constructor_host_access_migration_red tests.test_constructor_host_access_red tests.test_constructor_host_access_verify_red tests.test_constructor_build_orchestration tests.test_version_visual_boundaries tests.test_version_effective` | 372 tests, all pass |
| Production type check | `ty check docker --python-version 3.14 --output-format concise` | All checks passed |
| Whitespace | `git diff --check` and `git diff --cached --check` | clean |
| Full suite delta vs `HEAD` | `python -m unittest discover -s tests` | 23 pre-existing, unrelated failures remain; no new failures |

## Typed-error contract

- Every owner schema failure reachable from `load_inventory()` is projected to
  `ConfigurationDocumentError` with the reviewed role, the resolved inventory
  path, the fixed `schema_error` classification, and the canonical field path.
- `DocumentIdentity` normalizes `role` through `DocumentRole(...)`, so only the
  closed `reviewed` and `local` roles can reach parsing or error projection;
  any other value is rejected at construction, before `tomllib` is invoked.
- `configuration` classification is a closed `DocumentErrorClassification` enum
  (`malformed_toml`, `schema_error`); `ConfigurationDocumentError` normalizes it
  and renders from fixed labels, so arbitrary caller text can never be published.
- `capture_owner_result()` invokes the schema callback and returns a safe
  outcome, so the callback frame (which may close over parsed document data) is
  never retained; `release_owner_result()` raises the captured projection from a
  frame that holds only that outcome, and `_parse_toml()` returns safe syntax
  metadata instead of raising in the source-holding frame.
- The projection never exposes the owner message, arguments, traceback, or
  rejected value; `__context__` and `__cause__` are both `None`.
- Malformed TOML and owner-schema errors expose no sentinel, parsed mapping, or
  rejected value through retained traceback-frame locals or callable closures;
  regression-tested by `test_malformed_toml_secret_is_not_reachable_from_traceback_locals`
  and `test_schema_error_hides_parsed_data_and_closures_from_its_traceback`.
- Constraint parsing (`_parse_override_policy`) and version parsing/mismatch
  sites carry safe field paths and never embed `str(exc)` or the rejected value.

## Reviewed-inventory fixture alignment

The reviewed `docker-constructor.toml` dropped the `highlight-js` and
`pi-tui-kit` extensions and bumped `pi`/`pi-usage`. The inventory tests were
updated to assert the current reviewed inventory instead of the removed entries:

- `tests/test_constructor_inventory_runtime.py::TestClosedRuntimeSchema::test_reviewed_runtime_packages_all_present`
  asserts the current set (`pi-proxy`, `pi-read`, `pi-usage`).
- `tests/test_constructor_inventory_runtime.py::TestClosedRuntimeSchema::test_reviewed_extension_overrides_accept_declared_versions`
  asserts each reviewed override accepts its own version and rejects a neighbor.
- `tests/test_constructor_pi_inventory.py::TestReviewedPiReleaseContract::test_real_inventory_records_pi_release_contract`
  asserts the reviewed Pi release `0.85.1`.
- `tests/test_version_visual_boundaries.py` drops `highlight-js`/`pi-tui-kit`
  from the canonical owner map, so the visual-header contract tracks the
  current canonical blocks.
- `tests/test_version_effective.py::TestEnvironmentMapping::test_no_missing_extensions`
  no longer expects `HIGHLIGHT_JS_VERSION`/`PI_TUI_KIT_VERSION`.

No reviewed entries were added back to `docker-constructor.toml`.

# Phase 2 Validation Record

Validation evidence for the local aggregate configuration boundary. Kept out of
`tasks.md` per project convention.

## Scope

- `docker/versioning/local_project_configuration.py` (aggregate owner:
  fixed companion resolution, closed four-table registry, dispatch to
  domain-owned parsers, immutable aggregate, and the Phase 1-routed loaders;
  contains no field/type/default or semantic validation).
- `docker/versioning/host_access.py` (new; runtime host-access owner of the
  `[host-access]` field/type/default and IPv4/IPv6 plus `docker-gateway`
  semantics).
- `docker/versioning/cache_storage.py` (user-cache-storage owner; new
  `parse_local_cache_config` for the `[cache]` table).
- `docker/versioning/corporate_network.py` (new; corporate-network owner of the
  `[corporate-trust]` and `[network.proxy]` field/type/default and
  credential-free proxy URL semantics).
- `docker/versioning/inventory.py` (reviewed/local loaders delegate to the
  aggregate owner; removed the duplicate local parser and error projection).
- `tests/test_local_project_configuration_phase2.py` (new);
  `tests/test_configuration_document_validation_phase1.py` (boundary-routing
  assertion retargeted at both boundary consumers).

## Owner modules

The aggregate owner registers exactly four tables and dispatches each to the
imported parser owned by its capability:

| Table | Owning capability | Owner module | Parser |
| --- | --- | --- | --- |
| `[host-access]` | runtime host-access | `docker/versioning/host_access.py` | `parse_local_host_access` |
| `[cache]` | user-cache-storage | `docker/versioning/cache_storage.py` | `parse_local_cache_config` |
| `[corporate-trust]` | corporate-network | `docker/versioning/corporate_network.py` | `parse_local_corporate_trust` |
| `[network.proxy]` | corporate-network | `docker/versioning/corporate_network.py` | `parse_local_network_proxy` |

The owner modules perform no file I/O and never parse TOML; the aggregate
boundary resolves the fixed companion, parses it once through
`parse_configuration_document()`, and projects owner errors through
`capture_owner_result()`/`release_owner_result()` exactly as in Phase 1.

## Checks

| Check | Command | Result |
| --- | --- | --- |
| Focused local-aggregate suites | `python -m unittest tests.test_local_project_configuration_phase2 tests.test_configuration_document_validation_phase1 tests.test_constructor_corporate_network_red tests.test_constructor_corporate_network_acceptance_red tests.test_constructor_corporate_network_build_red tests.test_constructor_corporate_network_run_red tests.test_constructor_host_access_red tests.test_constructor_host_access_acceptance tests.test_constructor_host_access_launch_red tests.test_constructor_host_access_migration_red tests.test_constructor_host_access_doctor_red tests.test_constructor_host_access_verify_red tests.test_constructor_project_inputs tests.test_constructor_build_orchestration tests.versioning.test_cache_storage tests.versioning.test_cache_storage_security tests.test_constructor_cache_contracts` | 442 tests, all pass |
| Phase 2 RED → GREEN | `python -m unittest tests.test_local_project_configuration_phase2` | RED: `ModuleNotFoundError: docker.versioning.local_project_configuration`; GREEN: 28 tests, all pass |
| Production type check | `ty check docker --python-version 3.14 --output-format concise` | All checks passed |
| Whitespace | `git diff --check` and `git diff --cached --check` | clean |
| OpenSpec | `openspec validate extract-local-project-configuration --strict` | valid |
| Full suite | `python -m unittest discover -s tests` | 3463 tests, `OK (skipped=13)` |

## Phase 2 deliverable mapping

- **Fixed optional companion resolution** (`2.1`, `2.6`):
  `resolve_local_companion_path()` returns exactly
  `<selected-project>/docker-constructor.local.toml` via the fixed basename and
  exposes no ancestor, workspace, installation-root, alternate-basename, or
  custom-lookup option; covered by `TestFixedLocalCompanionResolution`.
- **One shared-boundary read/parse per transaction** (`2.2`, `2.6`):
  `load_local_project_configuration()` and
  `load_optional_local_project_configuration()` route the companion through
  `parse_configuration_document()` once, then dispatch every domain from that
  single mapping; covered by `TestSingleParsedDocumentPerTransaction`
  (one `tomllib.loads` call populates all four domains; domain parsers never
  reopen the document).
- **Immutable aggregate result** (`2.6`, `2.7`): the aggregate is the frozen
  `LocalConfig` with frozen domain members; mutation raises
  `FrozenInstanceError`.
- **Explicit closed four-table registry** (`2.3`, `2.7`): `_LOCAL_DOMAIN_TABLES`
  is a fixed tuple exposing `LOCAL_TABLE_NAMES = {host-access, cache,
  corporate-trust, network}` and, for each table, the parser imported from its
  owning capability module; unknown top-level tables — including a future
  `[output]` — are rejected before any domain parser runs; covered by
  `TestClosedLocalTableRegistry` and `TestDomainOwnedDispatch`.
- **Owner-schema diagnostics retain Phase 1 identity** (`2.4`, `2.7`): unknown
  fields, misplaced fields, and domain-invalid values raise owner
  `InventoryError`s carrying a structured `field`, projected by
  `capture_owner_result()`/`release_owner_result()` to
  `ConfigurationDocumentError` with the local role, resolved companion path,
  fixed `schema_error` classification, and canonical field — never the rejected
  value; covered by
  `TestOwnerSchemaDiagnosticsRetainDocumentIdentity`.
- **Absence-compatible defaults** (`2.5`, `2.6`): an absent companion is
  resolved through `load_optional_local_project_configuration()`, which returns
  `validate_local_document({}, host_access_mode=host_access_mode)` instead of
  constructing `LocalConfig()` directly. The main reviewed/local transaction
  does the same: `inventory._validate_project_documents()` runs
  `validate_local_document({}, host_access_mode=host_access_mode)` when no
  local document is present, so `load_project_configuration()` never
  constructs domain defaults. Both an absent companion and an existing empty
  companion therefore obtain their defaults from the domain owners, keep the
  `companion.exists()` check, never call `load_local_project_configuration()`
  or parse a file, never create the file, and ignore ancestor/alternate
  basenames; covered by `TestAbsentCompanionDefaults`, including
  `test_project_configuration_absent_companion_uses_domain_defaults`, which
  asserts the four registered parsers receive `None` and the returned local
  configuration equals the domain-owned defaults.
- **Owner-supplied absent-table defaults** (`2.5`, `2.6`, `2.8`):
  `validate_local_document()` dispatches every registered table, passing
  `raw.get(table.toml_table)` — including `None` for an absent table. Each
  owner parser returns its own default (`parse_local_host_access(None, …)` →
  `LocalHostAccess()`, `parse_local_cache_config(None, …)` →
  `LocalCacheConfig()`, `parse_local_corporate_trust(None, …)` →
  `LocalCorporateTrust()`, `parse_local_network_proxy(None, …)` →
  `LocalNetworkProxy()`), and the aggregate composes only the parser-returned
  values via `LocalConfig(**selected)`, importing no domain types. An explicit
  non-table value such as `cache = "invalid"` is still rejected and is never
  treated as absent, and unknown top-level tables are still rejected before
  dispatch; covered by `TestAbsentTableDefaults`. The missing-companion loader
  path dispatches the same four parsers with `None` and the caller's
  `host_access_mode` exactly once each, as asserted by
  `test_missing_companion_dispatches_all_parsers_with_the_mode`.
- **No new table / no `[output]`:** asserted directly by the registry closure
  tests.

## INTROSPECT findings (`2.8`)

- No duplicate `tomllib` parsing or error projection remains in
  `inventory.py`; the local table parsers and `_validate_proxy_url` moved out
  of `inventory.py` and were then placed with their domain owners, and
  `inventory.py` contains no local table parser or `tomllib` call.
- The aggregate owner contains no field/type/default or semantic validation:
  `[host-access]` parsing (including `ipaddress` IPv4/IPv6 and the
  `docker-gateway` exception) lives in `host_access.py`, `[cache]` parsing in
  `cache_storage.py`, and `[corporate-trust]`/`[network.proxy]` parsing
  (including `urllib.parse` credential-free proxy URL validation) in
  `corporate_network.py`. Absent-table defaults are supplied by those owner
  parsers, not constructed by the aggregate: `validate_local_document()`
  dispatches all four tables even when absent, the missing-companion loader
  branch and the reviewed/local transaction
  (`_validate_project_documents`) both route through
  `validate_local_document({})` rather than returning `LocalConfig()`
  directly, and the aggregate imports only `LocalConfig` from
  `model` and composes `LocalConfig(**selected)` from the parser-returned
  values. `local_project_configuration.py` imports no
  `ipaddress`, `urllib`, or domain state types and defines no `[host-access]`/
  `[cache]`/`[corporate-trust]`/`[network.proxy]` field semantics;
  `TestDomainOwnedDispatch` asserts the registry callables are the owner
  module functions, that dispatch reaches each registered parser, and that the
  aggregate source contains no IP or proxy-URL parsing.
- Each owner parser retains its exact table field paths, defaults, types, and
  error `field` metadata, so accepted values, defaults, and typed diagnostics
  are unchanged; the domain parser still receives only its already-parsed
  table and performs no file I/O or TOML parsing.
- Registration is closed (fixed tuple, `frozenset` of names); no dynamic or
  permissive registration and no `[output]`.
- No repeated reads: one parse per loader call; domain parsers never touch the
  filesystem.
- Results are immutable (`LocalConfig` and all domain members are frozen).
- No partial result: an invalid table raises before any aggregate is returned.
- Import graph is acyclic: `local_project_configuration` imports only
  `model`/`errors`/`configuration_document_validation` plus the three domain
  owner modules (`host_access`, `cache_storage`, `corporate_network`), which
  import only `model`/`errors`; no owner imports the aggregate, and `inventory`
  depends on `local_project_configuration`, never the reverse.
- Correction applied: `resolve_local_corporate_settings()` now delegates its
  resolution and absence handling to
  `load_optional_local_project_configuration()` instead of re-implementing the
  companion-exists check.

# Phase 3 Validation Record

Validation evidence for cache-root ownership, kept out of `tasks.md` per
project convention.

**Phase 3 made no production change.** Both the ownership audit and the
release-order audit found no concrete bypass, so there was nothing to correct
in production. The tests added in this phase characterize and preserve
existing behavior; they are baseline/architecture/regression coverage, not
RED tests, and none was expected to fail. Tasks 3.4–3.7 were reconciled in
`tasks.md` to AUDIT / REGRESSION / CONDITIONAL GREEN, matching the binding
contract's rule that an AUDIT may find no gap. See "Planning reconciliation".

## Scope

- `docker/versioning/cache_storage.py` — unchanged authority for configured
  and default root resolution, lexical normalization, dangerous-root policy,
  no-follow root/descendant inspection, permissions, ownership, fallback,
  and named children.
- `docker/versioning/local_project_configuration.py` — unchanged: aggregate
  `[cache]` parsing stays shape/type/default only, and
  `LocalConfig.cache.dir` remains untrusted input.
- `docker/versioning/build_cache.py` — unchanged. Its thin
  `_resolve_cache_root()` adapter delegates default-root selection to
  `cache_storage.resolve_default_root()` and adds no cache policy.
- `tests/test_cache_root_ownership_phase3.py` — ownership and
  duplicate-policy architecture coverage.
- `tests/test_cache_release_ordering_phase3.py` — release-order regression
  coverage for the effect categories in task 3.6.

## Checks

| Check | Command | Result |
| --- | --- | --- |
| Ownership suite | `python -m unittest tests.test_cache_root_ownership_phase3` | 11 tests, `OK` |
| Release-order suite | `python -m unittest tests.test_cache_release_ordering_phase3` | 14 tests, `OK` |
| Focused cache suites | `python -m unittest tests.test_cache_root_ownership_phase3 tests.test_cache_release_ordering_phase3 tests.versioning.test_cache_storage tests.versioning.test_cache_storage_security tests.versioning.test_http_cache_handoff tests.test_constructor_cache_contracts tests.test_constructor_build_persistence tests.test_constructor_build_orchestration tests.test_constructor_cross_consumer_cache tests.test_constructor_build_cache_paths tests.test_constructor_build_cache_permissions tests.test_version_cache` | 323 tests, `OK (skipped=1)` |
| Build persistence | `python -m unittest tests.test_constructor_build_persistence` | 44 tests, `OK` |
| Build materialization | `python -m unittest tests.test_constructor_build_materialization` | 18 tests, `OK` |
| Production type check | `ty check docker --python-version 3.14 --output-format concise` | All checks passed |
| Full suite | `python -m unittest discover -s tests` | 3488 tests, `OK (skipped=13)` |
| Whitespace | `git diff --check` and `git diff --cached --check` | clean |

Every suite above passes as written; none was a failing RED test at any point
in this phase.

## Existing behavioral baseline

`cache_storage.py` predates this change and is the existing behavioral owner
of every configured/default-root and child clause. This table maps each
clause to its owner and the focused test that already characterized it.

| Clause | Owner | Focused baseline test |
| --- | --- | --- |
| Empty/relative/`~` configured path | `resolve_local_root` | `TestLocalRootResolution.test_empty_local_root_rejected`, `test_relative_local_root_rejected`, `test_tilde_prefixed_local_root_rejected` |
| Lexical normalization | `_normalize` / `resolve_local_root` | `test_lexical_variants_of_dedicated_child_normalized`, `test_double_leading_slash_roots_rejected` |
| Root/home/XDG/XDG-ancestor matrix | `resolve_local_root` | `test_xdg_cache_home_rejected`, `test_home_directory_rejected`, `test_filesystem_root_rejected`, `test_ancestor_of_xdg_rejected`, `test_lexical_equivalents_of_unsafe_roots_rejected` |
| XDG/`~/.cache` fallback | `resolve_default_root` / `resolve_effective_root` | `TestDefaultRootResolution.*`, `TestXdgEligibility.*` |
| No-follow selected root | `prepare_local_root` / `_inspect_entry` | `TestLocalRootSecurity.test_symlinked_selected_root_rejected`, `test_non_directory_selected_root_rejected`, `test_unsecurable_existing_root_rejected` |
| No-follow encountered descendants | `_harden_tree` / `_DESCENDANT_PARTS` | `test_symlinked_descendant_rejected`, `test_non_directory_descendant_rejected`, `test_foreign_owned_descendant_rejected`, `test_unsecurable_descendant_rejected` |
| Permissions `0700`/`0600`/`0444` and no-parent-chmod | `_create_and_secure` / `_harden_tree` | `TestDefaultRootDirectoryHardening.*`, `TestHttpEntryMode.*`, `TestVerifiedBlobMode.*`, `TestParentModesUnchanged.*` |
| Named children and host-access independence | `versioning_child`, `runtime_artifacts_*_child` | `TestCacheChildDerivation.*`; `tests/test_constructor_cross_consumer_cache.py` |
| No companion creation | aggregate loaders | `TestAbsentCompanionDefaults.*` |

## Ownership architecture coverage

These tests record that the baseline authority above is not duplicated or
bypassed. They are architecture guards over existing behavior, not RED tests.

- `test_every_authority_function_is_defined_by_cache_storage` — every
  resolver/preparation/child API is callable on `cache_storage`.
- `test_no_other_module_defines_an_authority_function` — no `docker/` module
  other than `cache_storage.py` defines one.
- `test_consumers_call_cache_owned_apis` — `transports`, `project_state`,
  `build_orchestration`, `build_cache`, `constructor_cli`, and `launcher`
  call a cache-owned API.
- `test_build_cache_thin_adapter_adds_no_policy` — the one consumer adapter
  calls only `resolve_default_root` and no policy primitive.
- `test_no_module_outside_cache_storage_builds_canonical_children` — no
  `/ "versioning"`, `/ "runtime-artifacts"`, or `os.path.join(..., ...)`
  canonical-child assembly outside `cache_storage.py`.
- `test_no_shared_cache_consumer_normalizes_or_validates_the_root` and
  `test_no_shared_cache_consumer_compares_cache_root_to_home_or_xdg` — no
  consumer applies `normpath`/`isabs`/`islink`/`realpath`/`lstat`/`chmod`/
  `mkdir` to a shared-cache-named value, and none compares a cache root
  against home/XDG/filesystem root.
- `test_aggregate_cache_parser_has_no_environment_or_filesystem_logic` and
  `test_configured_dir_remains_untrusted_until_cache_storage_resolves_it` —
  aggregate parsing owns only table shape, unknown fields, `dir` type, and
  the absent default; `LocalConfig.cache.dir` is returned verbatim.
- `test_cache_storage_rejects_the_same_untrusted_values` — the same empty,
  relative, and filesystem-root values are rejected by
  `resolve_local_root`.
- `TestDetectorIsNotVacuous` — the duplicate-policy detector flags a
  synthetic re-implementation (normalization, absolute validation,
  home comparison, canonical children), so the architecture tests have
  teeth.

Documented scope rule for the duplicate-policy tests: a primitive is a
violation only when it acts on a value whose name denotes the *shared*
constructor cache root (an identifier/attribute containing `cache`) or
assembles a canonical shared child. Passing `XDG_CACHE_HOME`/`home` into a
cache-storage API is delegation and is never flagged. `artifact_cache.py`
is out of scope: its `cache_root` parameter is the project-scoped artifact
containment root, an independently owned policy.

## Release-order regression coverage

These tests characterize the existing release ordering (cache-owned validation
before effects) for each effect category. They are regression coverage, not
RED tests; the run-path tests include a positive control proving the effect
probes are reachable on a valid root, and the real-`execute_build` build
tests prove a lexically valid root with an unsafe encountered descendant is
rejected before the first build effect.

| Effect | Production entry point | Invalid cache condition | Cache validation/preparation | Effect probe | Test |
| --- | --- | --- | --- | --- | --- |
| Cache mutation | `cache_storage.prepare_local_root` / `prepare_resolved_root` | `[cache].dir` is `/`, relative, or empty; or an existing `<root>/runtime-artifacts` descendant is a symlink | `_harden_tree` → `_inspect_entry` then `_create_and_secure` | `_create_and_secure`/`_harden_tree` mocked and called-never asserted; root tree unchanged | `TestRunEffectsBlockedBeforeRelease.test_dangerous_root_does_not_mutate_any_cache_entry`, `TestCacheOwnedDescendantMutationOrdering.test_symlinked_descendant_rejected_before_create`, `test_unsafe_root_rejected_before_inspection`, `test_valid_root_prepares_expected_named_children`; baseline `tests/versioning/test_cache_storage_security.py::test_unsafe_local_roots_rejected_before_write` |
| Network access | `orchestrate_run` → `artifact_cache.materialize_selected_artifacts(transport=…)` | same dangerous root, or valid root with a symlinked `versioning`/`runtime-artifacts` descendant | Step 1b `resolve_effective_root`; Step 3b `prepare_resolved_root` | `_artifact_fetcher` recorder plus `urllib.request.urlopen` patched to raise | `test_dangerous_root_blocks_every_effect`, `test_unsafe_descendant_blocks_every_effect`; positive control `test_valid_root_reaches_effect_boundaries`; end-to-end network proof in `tests/test_constructor_cross_consumer_cache.py` (asserts artifact fetches occurred on the valid path) |
| Artifact publication | `orchestrate_run` → `artifact_cache.materialize_selected_artifacts` (publish) | same dangerous root or unsafe descendant | same | `materialize_selected_artifacts` mocked and `assert_not_called` | same three tests |
| Container execution | `orchestrate_run` → `request.executor.run` | same dangerous root or unsafe descendant | same | `_RecordingExecutor.calls` empty | same three tests; positive control asserts `executor.calls` non-empty on the valid path |
| Docker execution | `orchestrate_build` → real `execute_build` → `docker build` | `[cache].dir` is `/` or empty (lexical), or a lexically valid root with an unsafe existing `projects` descendant (symlink or non-directory) | lexical: `plan_build` `resolve_effective_root`; encountered descendant: `execute_build` `prepare_project_root` + `resolve_project_state` no-follow inspection | record-and-raise probes on `_materialize_artifacts`, `_publish_projection`, and `runner`; none may be reached | `TestBuildDockerExecutionBlockedBeforeRelease.test_dangerous_root_blocks_docker_execution`, `test_empty_root_blocks_docker_execution`; real-`execute_build` `TestBuildUnsafeDescendantBlockedBeforeRelease.test_symlinked_project_descendant_blocks_all_build_effects`, `test_non_directory_project_descendant_blocks_all_build_effects`; positive controls `TestBuildDockerExecutionBlockedBeforeRelease.test_valid_dedicated_root_reaches_execution_boundary` and `TestBuildUnsafeDescendantBlockedBeforeRelease.test_valid_root_reaches_first_build_effect` |

Build-path descendant scope: `execute_build` prepares the shared root with
`prepare_project_root()` and then validates only its own project-scoped
`projects` child through `resolve_project_state()`; it never reads or writes
the global `runtime-artifacts`/`versioning` subtrees, so a symlink there is
not an encountered build descendant and does not block a build. That scope is
pinned by `TestBuildUnsafeDescendantBlockedBeforeRelease.test_global_cache_subtrees_are_not_build_path_descendants`,
while the run path that does use the global subtree rejects it
(`TestRunEffectsBlockedBeforeRelease.test_unsafe_descendant_blocks_every_effect`).

## Audit result: no implementation gap found

- The ownership audit found **no duplicated root-selection policy**.
- Every consumer already delegates to cache-storage APIs.
  `build_cache._resolve_cache_root()` is a thin adapter that adds no policy;
  calling the exported `resolve_default_root()` is delegation, not an
  independent decision.
- The release-order audit found **no concrete release bypass**: cache-owned
  validation/preparation completes before cache mutation, network access,
  artifact publication, container execution, and Docker execution.
- Real `execute_build` returns an OPERATIONAL result and reaches no
  materialization, publication, or Docker runner when the cache root has an
  unsafe existing `projects` descendant, so descendant validation precedes
  every build effect.
- The added tests characterize and preserve existing behavior; **no
  production change was required**.
- `git diff HEAD -- docker` is empty for this phase; `cache_storage.py`,
  `local_project_configuration.py`, `build_cache.py`, and all consumers are
  unchanged.
- No companion creation is introduced by any cache path.

## Planning reconciliation

- The binding contract permits an AUDIT to conclude that no implementation
  gap exists, forbids manufacturing a failing RED test, and makes a
  CONDITIONAL GREEN satisfied when its prerequisite audits find no gap.
- Tasks 3.4–3.7 were reclassified in `tasks.md` accordingly: 3.4 ownership
  audit, 3.5 ownership regression coverage, 3.6 release-order regression
  coverage, and 3.7 conditional correction.
- The audits found **no concrete bypass**, so 3.5/3.6 needed no focused
  failing test and 3.7 required **no production change**.
- Task wording, this evidence record, and the implementation history now
  describe the same outcome; tasks 3.4–3.7 are checked and Phase 3 is
  complete.

## Verified invariants (3.8/3.9)

Confirmed invariants of existing behavior, not GREEN implementation changes:

- Aggregate `[cache]` parsing is limited to table shape, unknown-field
  rejection, `dir` value type, and the absent default. It performs no
  absolute-path, XDG/home, ownership, or filesystem validation and does not
  thread environment/filesystem state into parsing. `LocalConfig.cache.dir`
  is returned verbatim until `cache_storage` resolves it.
- Reproducibility is limited to reviewed/local source separation, reviewed
  TTL, projection exclusion, and consumer child-format mapping; root
  selection and safety remain delegated to cache storage. No output changed.

## INTROSPECT findings (3.10/3.11)

- No independent root normalization, dangerous-root policy, XDG/home
  fallback, or canonical child definition exists outside `cache_storage.py`;
  the architecture tests enforce this for every shared-cache consumer.
- Internal overrides remain explicitly test-only seams
  (`RunRequest._constructor_cache_root`, `_artifact_cache_root`,
  `_artifact_locks_root`, `_artifact_tmp_root`); production roots continue to
  be resolved or prepared by `cache_storage` before use, and arbitrary test
  artifact roots are never reinterpreted as constructor cache roots.
- `project_state.resolve_project_state()` continues to delegate to
  `prepare_default_root()`; no second XDG/default implementation was added.
- `artifact_cache.py` owns an independent project-scoped blob-containment
  policy; the launcher's `islink`/`realpath` check on the injected
  `configured_root` is a containment/test-seam defense, not shared-root
  selection policy. Both are explicitly out of scope.
- No filesystem-sensitive validation was moved into aggregate parsing, so
  validation timing, diagnostics, paths, fallback, and effect ordering are
  unchanged.
- No companion creation was introduced by any cache path.

## User-visible behavior

Phase 3 introduces **no production behavior change**. `build_cache._resolve_cache_root()`
already delegated to `cache_storage.resolve_default_root()`, and both that
selector and `resolve_effective_root()` are owned by `cache_storage`, so no
policy moved and no selector was replaced. Accepted configuration, cache
paths, permissions, diagnostics, and effect ordering are unchanged.

# Phase 4 Validation Record

Validation evidence for the domain-consumer migration, kept out of `tasks.md`
per project convention.

## Scope

- `docker/versioning/inventory.py` — `load_project_configuration()` is the one
  command transaction: it routes both fixed documents through the Phase 1
  boundary, returns one reviewed inventory and one aggregate local result, and
  derives the local parsing mode from the validated reviewed
  `[runtime.host-access]` policy when the caller does not override it.
- `docker/versioning/readonly_service.py` — `validate`, `show`, and
  `check-updates` load both documents once through the shared transaction and
  pass the aggregate local result to the update-discovery cache consumer.
- `docker/versioning/transports.py` — `build_transports()` consumes only the
  `LocalCacheConfig` cache slice (`local_cache=`), never the aggregate.
- `docker/launcher.py` — `orchestrate_run()` uses the shared transaction and
  passes only `local.host_access` to `_resolve_host_access()`.
- `docker/versioning/build_orchestration.py` — `plan_build()` continues to use
  the shared transaction; `_resolve_doctor_host_access()` now validates both
  documents before probing or persisting a gateway; `execute_build()` now runs
  the build-context confinement boundary before materialization, publication,
  or Docker.
- `docker/constructor_cli.py` — the `verify` command performs one shared
  transaction and derives host-access, corporate-network, and cache inputs from
  it; the two verify helpers accept pre-loaded state and still self-load when
  invoked directly.
- `docker/versioning/build_context_confinement.py` (new) — the production
  build-context confinement boundary: derives the effective context, computes
  context-relative paths for both fixed documents, preserves existing ignore
  rules, and materializes a transaction-owned Dockerfile copy plus its
  Dockerfile-specific ignore file.
- `.dockerignore` — excludes both fixed source TOML documents from this
  repository's default context as **defense in depth only**; it is not the
  enforcement mechanism.
- `tests/test_domain_consumer_migration_phase4.py` (new) — routing, parse-count,
  ownership, independence, and no-leak coverage.
- `tests/test_build_context_confinement.py` (new) — production build-context
  confinement: default/explicit/nested/outside contexts, ignore-rule
  preservation, negation override, fail-closed behavior, and transaction
  cleanup.

## Checks

| Check | Command | Result |
| --- | --- | --- |
| Phase 4 consumer suite | `python -m unittest tests.test_domain_consumer_migration_phase4` | 9 tests, `OK` |
| Build-context confinement suite | `python -m unittest tests.test_build_context_confinement` | 29 tests, `OK` |
| Build orchestration | `python -m unittest tests.test_constructor_build_orchestration` | 83 tests, `OK` |
| Build transactions | `python -m unittest tests.test_constructor_build_transactions` | 30 tests, `OK` |
| Build snapshot | `python -m unittest tests.test_constructor_build_snapshot` | 13 tests, `OK (skipped=2)` |
| Dockerfile contracts | `python -m unittest tests.test_dockerfile_contracts` | 14 tests, `OK` |
| Production type check | `ty check docker --python-version 3.14 --output-format concise` | All checks passed |
| OpenSpec strict | `openspec validate extract-local-project-configuration --strict` | valid |
| Whitespace | `git diff --check` / `git diff --cached --check` | clean |
| Full suite | `python -m unittest discover -s tests` | 3526 tests, `OK (skipped=13)` |

## RED → GREEN

- `TestOneSharedDocumentTransaction.test_command_transactions_parse_each_document_once`
  was RED for `validate` (0 local parses), `show` (0), `doctor` (0), and `run`
  (2 local parses) and is GREEN after the migration: each command records
  exactly one `reviewed` and one `local` boundary parse.
- `TestOneSharedDocumentTransaction.test_verify_transaction_parses_local_document_once`
  was RED (2 local parses via cache + corporate loaders) and is GREEN (1) after
  the single shared transaction.
- `TestMalformedLocalFailsBeforeEffects.test_readonly_and_doctor_commands_reject_malformed_local`
  was RED for `validate`, `show`, and `doctor` (all returned `SUCCESS`) and is
  GREEN (all `CONFIG`).
- `TestDomainSliceOwnership.test_run_does_not_reload_local_companion_for_host_access`
  was RED (`_resolve_host_access` called `load_local_config_for_inventory()`)
  and is GREEN (runtime host access consumes the shared `[host-access]` slice).
- `TestDomainSliceOwnership.test_check_updates_passes_only_the_cache_slice_to_transports`
  was RED (`build_transports(local_config=LocalConfig)`) and is GREEN
  (`build_transports(local_cache=LocalCacheConfig)`).

The remaining Phase 4 coverage is compatibility/ownership regression over
already-correct behavior rather than a RED failure, and is recorded as such:

- `TestCorporateNetworkIndependence.test_proxy_is_usable_without_host_access`
  proves corporate proxy input remains usable with host access disabled and no
  host-access variables are emitted. The shared transaction did not change this
  behavior; the test characterises it after the migration.
- `TestLocalSourceConfinement.test_local_state_absent_from_serialization_and_vectors`
  and `test_effective_projections_exclude_local_aggregate` preserve the
  existing no-leak behavior of reviewed serialization and the effective
  projections.

## Domain-slice ownership mapping

| Consumer | Owning slice consumed | Source |
| --- | --- | --- |
| Runtime host access (`launcher._resolve_host_access`) | `LocalConfig.host_access` | shared transaction |
| Cache (`transports.build_transports`, build/run/verify root resolution) | `LocalConfig.cache` | shared transaction |
| Corporate network (build/run planning) | `LocalConfig.corporate_trust`, `LocalConfig.network_proxy` | shared transaction |
| Reviewed inventory (`validate`, `show`, `check-updates`, build, run, doctor, verify) | `Inventory` | same shared transaction |

`LocalConfig` aggregate values are composed once and never reopened. The
remaining `resolve_local_corporate_settings()`/`load_local_config_for_inventory()`
call sites are either the fallback in a directly-invoked verify helper or
out-of-scope maintenance/acceptance shell scripts.

## Build-context confinement boundary (4.5, corrected)

The earlier Phase 4 revision relied on this repository's `.dockerignore` and a
test that only inspected that text. That was insufficient: it protected only
one repository and could not protect external constructor projects, explicit
build contexts, projects with their own `.dockerignore`, or nested projects.
The production boundary now lives in
`docker/versioning/build_context_confinement.py` and runs inside
`execute_build()`.

- **Effective context.** The planner derives the context from the rendered
  `BuildRenderInputs.build_context` (default: the selected project root, or an
  explicit `BuildRequest.context`) and resolves it canonically with
  `os.path.realpath`. A symlinked context root therefore cannot confuse
  containment. The caller-supplied lexical context is retained as a secondary
  root so a symlinked context ancestor cannot make an in-context entry look
  external. A document entry outside every root is not transmitted and needs no
  rule.
- **Lexical document entries.** Both fixed document entries are made absolute
  lexically (`os.path.abspath`; no `realpath`/`resolve`) and compared against
  the context roots. Containment is decided by the entry Docker would transmit,
  never by the symlink target: `docker-constructor.toml` or its companion may be
  a symlink inside the context whose target lives outside, and the in-context
  entry is still forced into the ignore file. The ignore rule names the lexical
  entry (for example `project/docker-constructor.local.toml`), never the
  resolved target. Because paths are only made absolute (never dereferenced),
  a missing optional companion and a symlinked reviewed document both yield the
  correct entry.
- **Exact relative paths.** Both entries are added as exact context-relative
  POSIX paths (for example `project/docker-constructor.toml`), never as broad
  basename globs.
- **Rule preservation.** The generated ignore file is seeded with the existing
  applicable rules verbatim. A Dockerfile-specific `<Dockerfile>.dockerignore`
  takes precedence over the context-root `.dockerignore`, matching Docker, so
  neither source of project rules is silently discarded.
- **Requested Dockerfile owns ignore selection.** The plan keeps two explicit
  paths: `dockerfile_requested` (lexical absolute path passed or implied by the
  caller, symlinks not followed) and `dockerfile_source` (canonical resolved
  file). Ignore rules are looked up beside `dockerfile_requested`, exactly where
  Docker associates them, so a Dockerfile symlink never causes the rules beside
  its target to be read and never silently discards the project's own rules.
  `dockerfile_source` is used only to copy the Dockerfile bytes. Relative
  Dockerfile paths are interpreted relative to the effective context while
  preserving the lexical requested location for lookup.
- **Ignore encoding fails closed.** Ignore files are read as UTF-8; filesystem
  *and* decoding failures are projected to `ConfinementError`. A malformed
  ignore file never falls back to the context-root file, is never silently
  skipped, and never escapes `execute_build()` as a raw exception. Error
  messages name only the path, never the file contents.
- **Forced exclusions last.** The two forced exclusions are appended after the
  seed rules, so Docker's last-match-wins semantics make them immune to a later
  user negation such as `!docker-constructor.local.toml`.
- **Transaction-owned Dockerfile.** The requested Dockerfile's exact bytes are
  copied into private constructor transaction state and the generated
  `<generated-Dockerfile>.dockerignore` is placed beside it. Only the generated
  Dockerfile path is passed through `--file`; the original context remains the
  final positional Docker argument, so `COPY` semantics stay context-relative.
- **Confirming case (`test_build_context_confinement`):**
  - default context:
    `TestDefaultAndExplicitContexts.test_default_context_equal_to_project_root`
  - explicit context:
    `test_explicit_context_equal_to_project_root`
  - explicit/nested context:
    `TestNestedAndOutsideContexts.test_nested_project_uses_context_relative_paths`
  - inactive case (unrelated external inventory):
    `test_inventory_outside_build_context_skips_confinement`,
    `TestUnitPlanning.test_inactive_when_no_document_is_contained`
  - rule preservation:
    `TestIgnoreRulePreservation.test_later_negation_cannot_reinclude_local_companion`,
    `test_existing_project_rules_are_preserved`,
    `test_dockerfile_specific_ignore_takes_precedence`,
    `test_missing_optional_companion_still_excludes_both_paths`
  - symlinked reviewed document:
    `TestSymlinkedDocuments.test_symlinked_reviewed_document_is_excluded`,
    `TestUnitPlanning.test_symlinked_documents_keep_lexical_relative_paths`
  - symlinked local companion:
    `TestSymlinkedDocuments.test_symlinked_local_companion_is_excluded`
  - execution-level symlink regression:
    `TestSymlinkedDocuments.test_symlinked_entries_are_inspected_during_docker_call`
  - symlinked Dockerfile (requested path owns ignore selection):
    `TestUnitPlanning.test_symlinked_dockerfile_uses_requested_ignore_file`,
    `TestSymlinkedDockerfile.test_symlinked_dockerfile_uses_requested_ignore_file`
  - malformed ignore encoding fails closed:
    `TestUnitPlanning.test_invalid_utf8_root_ignore_raises`,
    `test_invalid_utf8_dockerfile_ignore_raises_without_fallback`,
    `TestFailClosed.test_malformed_ignore_encoding_fails_without_effects`.

  The symlink tests create external targets and symlink the in-context entries
  to them, then assert confinement stays active, the lexical entries are the
  forced rules, and the resolved outside targets never appear as rules. The
  execution-level test drives `execute_build()` with a recording runner and
  inspects the generated `Dockerfile.dockerignore` while it still exists,
  proving both lexical entries are ignored and neither target path is named.
  All other confirming tests capture the final Docker invocation through a
  recording runner and inspect the generated ignore file while it still exists,
  asserting the generated `--file`, the presence of `Dockerfile.dockerignore`,
  the exact exclusion rules, and the original context as the final argument.
- **Fail-closed (`TestFailClosed`):** a missing/unreadable Dockerfile, an
  unreadable or invalid-UTF-8 `.dockerignore`, undeterminable containment, or
  un-publishable generated state returns `CONFIG`/`OPERATIONAL` with empty
  `build_args` and no runner, publisher, or materializer invocation.
- **Cleanup (`TestTransactionCleanup`):** generated state is removed after
  success, a Docker failure, a materialization failure, and an unexpected
  exception, reusing `cleanup_artifact_snapshot`. Confinement is published only
  after `recover_abandoned_snapshots()` (which clears the transaction root) and
  is removed on every exit path.
- The repository `.dockerignore` entries remain, clearly commented as defense
  in depth.

## INTROSPECT findings (4.9)

- No command orchestration bypasses the shared transaction. `validate`,
  `show`, `check-updates`, `build`, `run`, `doctor`, and `verify` all call
  `load_project_configuration()` exactly once per command.
- The reviewed raw re-read in `check-updates --suggest`
  (`load_inventory_raw`) still routes through
  `configuration_document_validation.parse_configuration_document`; it is a
  reviewed-document read for suggestion-fragment rendering, never a local
  companion reopen.
- `_resolve_verify_host_access()` and `_resolve_verify_corporate_network()` no
  longer reopen the companion when the command supplies pre-loaded state; the
  `local_config=None` fallback exists only for direct callers/tests.
- No cross-domain access was introduced: `transports` reads only
  `LocalCacheConfig`; `_resolve_host_access` reads only `LocalHostAccess`;
  corporate consumers read only `LocalCorporateTrust`/`LocalNetworkProxy`.
- Vector/path drift: the migrated run/build/verify paths continue to produce
  the same Docker vectors and cache/generated-state paths. The build vector
  gains `--file <generated-Dockerfile>` only when a fixed document entry is
  lexically inside the context (including a symlinked entry whose target is
  outside); when both entries are outside, the original `--file` is
  preserved unchanged (`test_inventory_outside_build_context_skips_confinement`).
- Host-access coupling: corporate proxy/trust remain usable with host access
  disabled; no host-access mapping or proxy port is emitted in that mode.
- Configuration leakage: reviewed serialization, effective build/runtime
  projections, the run vector, and the Docker build context exclude the local
  companion path, its contents, and the aggregate; the separate fixed
  `.docker-local/corporate-ca-bundle.crt` remains permitted and is still
  mounted/validated only on the enabled path.
- Correction applied: broad enforcement replaces the repository-only
  `.dockerignore` reliance. The Dockerfile COPY-source visibility contract,
  build transactions, and snapshot suites still pass.

## User-visible behavior

Phase 4 introduces **no accepted-configuration, default, cache-path, or
container-visible behavior change**. The migration centralises document
parsing and threads the single aggregate result to domain owners. Two
observable changes are required by the specifications:

- commands previously did not validate a present-but-malformed local companion
  (`validate`, `show`, `doctor`, disabled-mode `verify`) and now fail closed
  before effects, as required by `configuration-document-validation`; and
- a build whose context contains either fixed document now passes
  `--file <transaction-owned Dockerfile>` and its generated
  `<Dockerfile>.dockerignore`, so Docker transmits neither host-only document.
  The selected context directory itself is never modified.

# Phase 5 — Ownership Cutover

## Scope

Phase 5 removes the last compatibility surface and locks the Phase 1–4
ownership in with architecture tests. It does not change accepted
configuration, defaults, cache paths, Docker vectors, projections, or
container-visible values.

## Deliverables

- `tests/test_ownership_cutover_phase5.py` (44 tests) — AST-level architecture
  guards plus the removed-scenario/atomic-obligation contract inventory.
- `tests/data/moved_local_state_obligations.json` — the independent semantic
  source for the removed requirement's atomic obligations. It holds the
  requirement title, the main spec and archived fallback paths, a derivation
  note, the documented non-normative connector set, and the 17
  `{obligation_id, source_excerpt}` entries. It was derived by hand from the
  pre-cutover requirement body and is **not** generated from the test
  inventory.
- `tests/test_moved_local_state_obligations_phase5.py` (9 tests) — focused
  functional regressions for the moved obligations whose earlier coverage was
  only indirect: no-follow inspection of an existing selected cache root,
  reviewed dependency/update/artifact/host-access-policy and cache-TTL
  isolation, dangerous-root diagnostics, corporate trust/proxy usable without
  host-access state, and a disabled-host-access test proving a configured
  `[cache].dir` is used as the shared cache root without any `[host-access]`
  state.
- `tests/test_malformed_local_state_phase5.py` (7 tests) — one branch-specific
  regression for each malformed local-companion input (unknown top-level key,
  unknown nested key, malformed syntax, invalid host address, invalid cache
  value, invalid corporate-trust value, invalid network proxy). Each asserts
  the structured field path or fixed `malformed_toml` classification, no
  rejected-value leak, and failure before network, cache mutation, artifact
  materialization/publication, and Docker/container execution.
- `docker/versioning/inventory.py` — removed the two obsolete pure-alias
  companion loaders `load_local_config` and `load_local_config_for_inventory`.
  Both were defined only in the reviewed-inventory module, added no policy, and
  delegated to `local_project_configuration`. `resolve_local_corporate_settings`
  is retained: it adds fixed-bundle validation and is not a pure alias.
- Callers migrated to the aggregate owner API:
  `local_project_configuration.load_local_project_configuration` and
  `load_optional_local_project_configuration` (tests and the two
  `docker/collect-runtime-artifact-*.sh` helper scripts). Call sites keep the
  same behavior; the owner API exposes no `repository_root` or custom-lookup
  option, so a repository-local fallback is now structurally impossible.

## RED → GREEN

**5.1 RED** — `TestProjectTomlParsingOwnedByBoundary`:
`test_only_allowlisted_production_modules_parse_toml` scans **every** production
module under `docker/` and requires any `tomllib`/`tomli` user to appear in the
explicit non-project allowlist (the boundary plus `effective.py`,
`verification.py`, `runtime_verification.py`, `runtime_installer.py`, and
`providers/rust.py`, which parse generated projections or a Cargo manifest).
`test_project_document_consumers_never_parse_toml` independently flags any
module that both locates a project document (references
`LOCAL_COMPANION_BASENAME`, `resolve_local_companion_path`, an aggregate loader,
or a boundary entrypoint) and parses TOML; the scan is dynamic, so a newly added
consumer module is covered without editing a hard-coded list.
`test_parse_error_projection_is_owned_by_the_boundary` keeps the
`TOMLDecodeError` owners to the boundary and the generated-projection
round-trip. These passed on first run: Phase 4 already routed both fixed
documents through `configuration_document_validation`, so the audit records
**no remaining independent project-TOML parser or parse-error projector**. No
failing RED was manufactured (per the implementation binding contract).
`TestProjectTomlDetectorIsNotVacuous` proves the detector flags each parser
form (`import tomllib`, `import tomllib as parser`, `from tomllib import
loads`, `import tomli as tomllib`, module attribute chains) and a parsing
statement introduced in a newly added module, while an explicitly allowlisted
unrelated parser is not flagged.

**5.2 / 5.3 RED** — `TestLocalAggregateOwnedByAggregateModule` and
`TestDependencyDirectionAndSoleOwners`:
- `LocalConfig` is constructed only by the aggregate owner. The detector
  resolves qualified (model.LocalConfig(...)) and aliased forms (imported or
  assigned aliases such as `Config = model.LocalConfig`);
  `TestAggregateConstructionDetectorIsNotVacuous` proves every form is caught
  and that a non-`LocalConfig` call is not a false positive.
- `resolve_local_companion_path`, `validate_local_document`, the table registry,
  and the aggregate loaders are defined only by the aggregate owner.
- `host_access.py` references no aggregate/cache/corporate/proxy type or parser.
- Domain modules (`host_access`, `cache_storage`, `corporate_network`) import
  neither the aggregate owner nor the document boundary.
- `cache_storage` remains an acyclic leaf (allowed imports only).
- Phase 2/4 modules add no cache-root normalization/dangerous-root/no-follow
  policy; the cache authority API is defined only by `cache_storage`.
- **RED:** `test_inventory_module_exposes_no_legacy_companion_loaders` failed
  with `['load_local_config', 'load_local_config_for_inventory']`. All other
  architecture tests passed; they are guards, not drivers.

**5.5 GREEN** — no independent parsing/error wrapper remained to remove; task
5.1 stays green.

**5.6 GREEN** — the two obsolete companion loaders are removed and every
caller is migrated; task 5.2 and 5.3 pass. RED count before the change: 1
failure; after: 17/17 pass.

**5.4 / 5.7 GREEN** — `TestMovedClauseInventory` reconciles an inventory of
**atomic normative obligations** (not sentences) and scenarios against an
**independent** semantic source. `_find_authoritative_requirement` parses the
pre-cutover requirement `Store constructor-project machine-local state
separately` from `openspec/specs/runtime-host-access/spec.md` (falling back to
the archived change that introduced it,
`2026-09-10-decouple-constructor-project-root`).

- Nine **scenario headings** are inventoried (`MOVED_CLAUSES`), each with one or
  more regression tests.
- Seventeen **atomic obligations** load from the independent manifest
  (`_load_obligation_manifest` → `tests/data/moved_local_state_obligations.json`)
  and are mapped by `MOVED_OBLIGATIONS`. Each carries one SHALL-level outcome:
  fixed companion location/basename, the closed local table set, absolute
  dedicated cache root, separate named cache-child directories, empty/relative
  rejection, lexical normalization, XDG equality, home directory, filesystem
  root, ancestor-of-XDG, no-follow inspection, symlink rejection, reviewed
  dependency/update/artifact/host-access-policy isolation, reviewed cache-TTL
  isolation, corporate trust independence, proxy independence, and proxy-URL
  non-derivation. There is no sentence-count assumption: the obligation count
  is the atomic obligation count, not the requirement's six sentences.
- The manifest is the *only* authority for which obligations exist; the test
  module declares none inline. `test_every_atomic_obligation_is_inventoried`
  first checks the manifest's `requirement_title` equals the removed
  requirement, then that its excerpts each occur exactly once, then runs the
  source-coverage validator, and only lastly compares the manifest's obligation
  ids with `MOVED_OBLIGATIONS` for exact equality and uniqueness (rejecting
  both an omission and an invention).
- **Source-coverage algorithm** (`_source_coverage_errors` /
  `_untiled_text`): the requirement body is whitespace-normalized; every
  manifest excerpt must occur exactly once; the excerpts are ordered by
  position in the body; and the prefix, every inter-excerpt gap, and the tail
  must each be tileable — via a dynamic-programming segmentation — by the
  manifest's documented `non_normative_connectors`. Any residue is reported as
  **uncovered normative text**. A connector carrying a normative marker is
  rejected (`test_manifest_connectors_are_documented_as_non_normative`), so the
  tiling transitively places every `SHALL` / `SHALL NOT` / `neither` / `nor` /
  `reject` / `derive` / `require` / `enabled` / `override` / `independent`
  marker inside a manifest excerpt;
  `test_every_normative_marker_lies_within_a_manifest_excerpt` asserts this
  directly. This makes completeness independent of the mapping layer: a
  dropped manifest entry leaves an un-tileable gap that no longer matches any
  connector, and the omitted text is named in the failure.
- Every destination capability/requirement/scenario is checked against the
  capability delta spec, and every referenced regression must resolve and pass.
- Scenarios whose THEN set names several independent branches declare a required
  branch set; removing one mapped regression fails the coverage test.
  - `Rejecting shared or dangerous local cache roots`
    (`DANGEROUS_ROOT_BRANCH_REGRESSIONS`): home, filesystem root,
    ancestor-of-XDG, rejection before mutation, and path-naming/
    dedicated-directory diagnostics.
  - `Falling back when local cache directory is absent`
    (`FALLBACK_BRANCH_REGRESSIONS`): absolute-XDG use, missing-XDG creation at
    `0700`, existing-writable-XDG acceptance, empty/relative fallback,
    non-directory and unwritable rejection without fallback, and no companion
    creation.
  - `Rejecting malformed local state` (`MALFORMED_STATE_BRANCH_REGRESSIONS`):
    unknown top-level key (`local.output`), unknown nested key
    (`local.host-access.foo`), malformed TOML syntax (`malformed_toml`),
    invalid host address (`local.host-access.address`), invalid cache value
    (`local.cache.dir`), invalid corporate-trust value
    (`local.corporate-trust.enabled`), and invalid network proxy
    (`local.network.proxy.url`).  `test_malformed_state_branch_removal_is_detected`
    proves removing any one required branch is flagged.

**Cache-without-host-access correction.** The earlier mapping for
`Loading a dedicated local cache root without host access` pointed at
`test_custom_inventory_with_local_cache`, which sets
`[runtime.host-access] enabled = true` and therefore could not prove
independence from host access.  That mapping is removed and replaced by
`TestLocalCacheRootWithoutHostAccess.test_configured_local_cache_root_is_used_with_host_access_disabled`,
which loads a project whose reviewed policy sets
`[runtime.host-access] enabled = false` and a companion declaring only a valid
absolute `[cache].dir`.  It asserts the resolved `plan.cache_root` equals the
configured directory, the build vector emits no `HOST_ACCESS_ADDRESS`,
`HOST_PROXY_PORT`, or `--add-host`, the local `[host-access]` slice has no
address, and a real cache consumer (`build_transports`) prepares the configured
root's `versioning` child while the default XDG root is never created.
`test_cache_without_host_access_scenario_uses_a_disabled_test` additionally
asserts the discouraged test is absent from the mapping.

**Malformed-state coverage.** The two malformed-TOML command tests are retained
for the syntax branch but are no longer treated as complete coverage of
`Rejecting malformed local state`; the scenario now maps to the
`configuration-document-validation` requirement `Reject invalid project
configuration before effects` / scenario `Local configuration is invalid` and
to the branch-specific regressions above.  Each branch regression in
`tests/test_malformed_local_state_phase5.py` asserts the structured diagnostic
(field path, or fixed `malformed_toml` classification with no rejected-value
leak) and drives `orchestrate_run` and `orchestrate_build` with effect probes:
an exploding `urllib.request.urlopen`, a recording container executor, a
patched `materialize_selected_artifacts`, a patched `execute_build`, and
filesystem assertions that no cache root or cache child was created.  Branches
whose scenario requires recovery guidance (host address, cache value,
corporate trust, network proxy) also assert the owner message is actionable.

The focused regressions in `tests/test_moved_local_state_obligations_phase5.py`
close the previously indirect coverage: no-follow inspection (dangling symlink,
symlinked parent component, existing root secured in place), reviewed-state and
cache-TTL isolation (schema rejection plus reviewed projection unchanged),
dangerous-root diagnostics, corporate trust/proxy usable without host-access
state, and the corrected disabled-host-access cache-root regression.
`tests/test_malformed_local_state_phase5.py` supplies the branch-specific
malformed-state diagnostics and effect-ordering regressions.

Non-vacuous omission controls (the manifest, not `MOVED_OBLIGATIONS`, is
reduced):

- `test_manifest_obligation_omission_fails_source_coverage` removes **every**
  manifest entry one at a time and asserts the source-coverage validator
  reports uncovered normative text for each;
- `test_compound_obligation_omissions_are_detected_from_source` does the same
  for the list/subordinate obligations `reviewed_cache_ttl_isolation`,
  `ancestor_of_xdg_rejection`, `proxy_url_non_derivation`,
  `separate_named_cache_children`, `home_directory_rejection`, and
  `proxy_independence`, which a set comparison against `MOVED_OBLIGATIONS`
  alone cannot distinguish;
- `test_obligation_omission_is_detected` keeps the separate mapping-layer check
  (removing an entry from `MOVED_OBLIGATIONS`).

RED proofs (temporary mutations, production/test/data files restored
byte-for-byte after each run):

- a non-allowlisted production module importing `tomllib` → broad parser guard
  fails;
- an aliased `LocalConfig` construction outside the owner → construction guard
  fails;
- a project-document consumer adding its own `tomllib` parse → consumer guard
  fails;
- dropping one inventory scenario → scenario completeness fails;
- deleting one entry from the checked-in `moved_local_state_obligations.json` →
  `test_every_atomic_obligation_is_inventoried`,
  `test_each_obligation_maps_to_a_destination_and_regressions`, and
  `test_manifest_obligation_omission_fails_source_coverage` fail, with the
  omitted text reported as uncovered;
- dropping one atomic obligation from the manifest inside the test → source
  coverage reports the omitted normative text from the spec alone (verified for
  all 17, including the compound clauses above);
- dropping one atomic obligation from `MOVED_OBLIGATIONS` → mapping
  completeness fails (separate layer);
- renaming an inventoried source scenario → completeness fails;
- a drifted obligation excerpt absent from the spec → source reconciliation
  fails;
- duplicating an obligation, mapping an unknown id, or mapping an obligation
  with an empty regression tuple → inventory guard fails;
- removing one required dangerous-root, fallback, or malformed-state branch
  regression → branch coverage fails;
- changing the corrected cache test's companion so `[cache].dir` is absent →
  `plan.cache_root` falls back to the default XDG root and the cache-root
  assertion fails (the disabled-host-access cache test is non-vacuous);
- making a malformed-state branch valid (e.g. `[cache] dir = 5` → a valid
  absolute path) while keeping the expected field → `assertRaises` fails, and
  dropping the unknown-key body → the diagnostic assertion fails.

`TestMovedClauseDetectorIsNotVacuous` additionally proves a bogus destination
requirement/scenario, an unresolvable regression test, a mapping-layer
omission, a duplicate, an unknown obligation id, a drifted excerpt, and a
removed branch regression are all flagged. No source clause was weakened.

## INTROSPECT findings (5.8)

- Complete production diff: removal of two pure aliases plus a docstring
  reference; no behavior change.
- No `[output]`: the aggregate registry stays closed to the four existing
  tables and still rejects unknown top-level tables (Phase 2 coverage).
- No open registration, no new CLI or environment source.
- No parser duplication: only `configuration_document_validation` parses the
  fixed documents. Every other production TOML user is explicitly allowlisted
  as a non-project parser, and any project-document consumer that parses TOML
  is flagged dynamically (no hard-coded consumer list). `effective.py`'s
  `TOMLDecodeError` handling is limited to the generated runtime-projection
  round-trip and is explicitly allow-listed.
- No aggregate duplication: `LocalConfig` is constructed only by its owner,
  including qualified and aliased construction forms.
- No cache-safety loss: `cache_storage` remains the sole root/child authority.
- No configuration exposure: Phase 4 confinement coverage is unchanged.
- No duplicate authority and no stale adapter remain.
- The obligation set has one external authority: the checked-in
  `tests/data/moved_local_state_obligations.json` manifest. The test module
  declares no obligation inline and does not regenerate the manifest from
  `MOVED_OBLIGATIONS`; deleting a manifest entry is therefore a source-text
  regression, not a self-consistent edit.

## VALIDATE (5.9, 5.10, 5.11)

| Check | Command | Result |
| --- | --- | --- |
| Focused named suites | `python -m unittest` (configuration, cache, host-access, corporate-network, inventory, projection, Docker vector, doctor, confinement, architecture, malformed-state) | 988 tests, `OK` |
| Full unit/integration | `python -m unittest discover -s tests -p 'test_*.py'` | 3586 tests, `OK (skipped=13)` |
| Typecheck (CI entrypoint) | `sh scripts/check-types` | `All checks passed!` |
| Lint | not configured for Python (no ruff/flake8/mypy); CI runs types only | n/a |
| Build checks | included in the suite (build orchestration, Dockerfile contracts, build vector, confinement) | `OK` |
| Strict OpenSpec | `openspec validate extract-local-project-configuration --strict` | valid |
| Whitespace | `git diff --check` / `git diff --cached --check` | clean |

Phase 5 leaves the change ready for synchronization and archive before
`improve-host-build-observability` is applied.

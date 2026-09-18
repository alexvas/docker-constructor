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

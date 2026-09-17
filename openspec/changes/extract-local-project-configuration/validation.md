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

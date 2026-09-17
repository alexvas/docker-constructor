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
| Focused boundary suites | `python -m unittest tests.test_configuration_document_validation_phase1 tests.test_constructor_inventory_* tests.test_constructor_pi_inventory tests.test_constructor_host_access_* tests.test_constructor_build_orchestration` | 327 tests, only the three documented fixture-drift cases fail |
| Production type check | `ty check docker --python-version 3.14 --output-format concise` | All checks passed |
| Whitespace | `git diff --check` and `git diff --cached --check` | clean |
| Full suite delta vs `HEAD` | `python -m unittest discover -s tests` | No new failures; 26 pre-existing failures remain |

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

## Pre-existing fixture drift

Three cases fail because the reviewed `docker-constructor.toml` and its pi
release snapshot no longer match the checked-in fixtures (`highlight-js` and
`pi-tui-kit` are absent; the pi release version is stale):

- `tests/test_constructor_inventory_runtime.py::TestClosedRuntimeSchema::test_five_runtime_packages_all_present`
- `tests/test_constructor_inventory_runtime.py::TestClosedRuntimeSchema::test_pi_tui_kit_override_stays_within_pi_usage_dependency_range`
- `tests/test_constructor_pi_inventory.py::TestReviewedPiReleaseContract::test_real_inventory_records_pi_release_contract`

These are unchanged fixture drift at `HEAD`, not defects of the
schema-projection or configuration-document work, and the missing entries are
intentionally not added by this change.

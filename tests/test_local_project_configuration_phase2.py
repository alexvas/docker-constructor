"""Phase 2 contract tests for the local aggregate configuration boundary."""
from __future__ import annotations

import dataclasses
import inspect
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from docker.versioning.configuration_document_validation import (
    ConfigurationDocumentError,
    DocumentErrorClassification,
    DocumentRole,
)
from docker.versioning import cache_storage, corporate_network, host_access
from docker.versioning import inventory
from docker.versioning import local_project_configuration as local_aggregate
from docker.versioning.errors import InventoryError
from docker.versioning.local_project_configuration import (
    LOCAL_COMPANION_BASENAME,
    LOCAL_TABLE_NAMES,
    _LOCAL_DOMAIN_TABLES,
    load_local_project_configuration,
    load_optional_local_project_configuration,
    resolve_local_companion_path,
    validate_local_document,
)
from docker.versioning.model import (
    LocalCacheConfig,
    LocalConfig,
    LocalCorporateTrust,
    LocalHostAccess,
    LocalNetworkProxy,
)

_ALL_TABLES = (
    '[host-access]\n'
    'address = "10.0.2.2"\n'
    '\n'
    '[cache]\n'
    'dir = "/var/tmp/cache"\n'
    '\n'
    '[corporate-trust]\n'
    'enabled = true\n'
    '\n'
    '[network.proxy]\n'
    'url = "http://proxy.internal:3128"\n'
    'no_proxy = "localhost,127.0.0.1"\n'
)


class _LocalTest(unittest.TestCase):
    def _companion(self, content: str) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / LOCAL_COMPANION_BASENAME
        path.write_text(content)
        return path

    def _project(self, content: str, *, name: str = "docker-constructor.toml") -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        inventory_path = root / name
        inventory_path.write_text("")
        (root / LOCAL_COMPANION_BASENAME).write_text(content)
        return inventory_path

    def _fixed_project(self) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        inventory_path = root / "docker-constructor.toml"
        inventory_path.write_text("")
        return inventory_path

    def _load_error(self, companion: Path) -> ConfigurationDocumentError:
        with self.assertRaises(ConfigurationDocumentError) as raised:
            load_local_project_configuration(companion)
        return raised.exception


class TestFixedLocalCompanionResolution(_LocalTest):
    """Task 2.1: exactly <selected-project>/docker-constructor.local.toml."""

    def test_companion_is_always_the_fixed_basename_beside_the_inventory(self) -> None:
        self.assertEqual(LOCAL_COMPANION_BASENAME, "docker-constructor.local.toml")
        self.assertEqual(
            resolve_local_companion_path(Path("/project/docker-constructor.toml")),
            Path("/project/docker-constructor.local.toml"),
        )
        # A custom inventory basename never becomes an alternate companion basename.
        self.assertEqual(
            resolve_local_companion_path(Path("/project/custom.toml")),
            Path("/project/docker-constructor.local.toml"),
        )

    def test_resolution_never_searches_ancestors_or_the_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as outer:
            outer_root = Path(outer)
            project = outer_root / "selected-project"
            project.mkdir()
            inventory_path = project / "docker-constructor.toml"
            inventory_path.write_text("")
            selected_companion = project / LOCAL_COMPANION_BASENAME
            selected_companion.write_text("")
            ancestor_companion = outer_root / LOCAL_COMPANION_BASENAME
            ancestor_companion.write_text("")

            elsewhere = outer_root / "elsewhere"
            elsewhere.mkdir()
            (elsewhere / LOCAL_COMPANION_BASENAME).write_text("")
            old_cwd = Path.cwd()
            try:
                os.chdir(elsewhere)
                resolved = resolve_local_companion_path(inventory_path)
            finally:
                os.chdir(old_cwd)

            self.assertEqual(selected_companion, resolved)
            self.assertNotEqual(ancestor_companion, resolved)

    def test_resolution_exposes_no_custom_lookup_option(self) -> None:
        parameters = inspect.signature(resolve_local_companion_path).parameters
        self.assertEqual(list(parameters), ["inventory_path"])
        self.assertFalse(any(
            kind is inspect.Parameter.VAR_KEYWORD
            for kind in (p.kind for p in parameters.values())
        ))


class TestSingleParsedDocumentPerTransaction(_LocalTest):
    """Task 2.2: one shared-boundary parse feeds every local domain."""

    def test_all_four_domains_share_exactly_one_parse(self) -> None:
        companion = self._companion(_ALL_TABLES)
        with patch.object(tomllib, "loads", wraps=tomllib.loads) as loads:
            local = load_local_project_configuration(companion)
        self.assertEqual(1, loads.call_count)
        self.assertEqual("10.0.2.2", local.host_access.address)
        self.assertEqual("/var/tmp/cache", local.cache.dir)
        self.assertTrue(local.corporate_trust.enabled)
        self.assertEqual("http://proxy.internal:3128", local.network_proxy.url)
        self.assertEqual("localhost,127.0.0.1", local.network_proxy.no_proxy)

    def test_domain_parsers_never_reopen_the_document(self) -> None:
        raw = tomllib.loads(_ALL_TABLES)
        with patch.object(
            Path, "read_bytes", side_effect=AssertionError("domain parser reopened the file")
        ):
            local = validate_local_document(raw)
        self.assertEqual("/var/tmp/cache", local.cache.dir)

    def test_repeated_parsing_is_not_required_to_compose_domains(self) -> None:
        companion = self._companion(_ALL_TABLES)
        with patch.object(tomllib, "loads", wraps=tomllib.loads) as loads:
            for _ in range(3):
                load_local_project_configuration(companion)
        self.assertEqual(3, loads.call_count)


class TestClosedLocalTableRegistry(_LocalTest):
    """Task 2.3: exactly the four existing tables; unknown tables rejected."""

    def test_registry_is_closed_to_the_four_existing_tables(self) -> None:
        self.assertEqual(
            frozenset({"host-access", "cache", "corporate-trust", "network"}),
            LOCAL_TABLE_NAMES,
        )
        self.assertNotIn("output", LOCAL_TABLE_NAMES)

    def test_exactly_four_existing_tables_are_accepted(self) -> None:
        local = load_local_project_configuration(self._companion(_ALL_TABLES))
        self.assertIsInstance(local, LocalConfig)
        self.assertEqual(
            {
                "address": local.host_access.address,
                "dir": local.cache.dir,
                "enabled": local.corporate_trust.enabled,
                "url": local.network_proxy.url,
            },
            {
                "address": "10.0.2.2",
                "dir": "/var/tmp/cache",
                "enabled": True,
                "url": "http://proxy.internal:3128",
            },
        )

    def test_every_unknown_top_level_table_is_rejected(self) -> None:
        for table in ("output", "observability", "build", "runtime", "cachex"):
            with self.subTest(table=table):
                with self.assertRaises(InventoryError) as raised:
                    validate_local_document({table: {}})
                self.assertEqual(f"local.{table}", raised.exception.field)

    def test_output_table_is_rejected_through_the_typed_boundary(self) -> None:
        companion = self._companion('[output]\ndir = "/tmp/output"\n')
        error = self._load_error(companion)
        self.assertEqual("local.output", error.field)
        self.assertIs(DocumentRole.LOCAL, error.role)
        self.assertIs(DocumentErrorClassification.SCHEMA_ERROR, error.classification)
        self.assertEqual(companion.resolve(), error.path)

    def test_network_table_rejects_unknown_siblings(self) -> None:
        with self.assertRaises(InventoryError) as raised:
            validate_local_document({"network": {"foo": {}}})
        self.assertEqual("local.network.foo", raised.exception.field)


class TestOwnerSchemaDiagnosticsRetainDocumentIdentity(_LocalTest):
    """Task 2.4: owner failures keep Phase 1 document path plus field."""

    def test_unknown_and_misplaced_fields_keep_path_and_field(self) -> None:
        cases = (
            ('[cache]\naddress = "10.0.2.2"\n', "local.cache.address"),
            ("[host-access]\ncache = 1\n", "local.host-access.cache"),
            ("[network.proxy]\nfoo = 1\n", "local.network.proxy.foo"),
            ('[corporate-trust]\npath = "/etc/ssl/custom.crt"\n', "local.corporate-trust.path"),
        )
        for body, field in cases:
            with self.subTest(field=field):
                companion = self._companion(body)
                error = self._load_error(companion)
                self.assertEqual(field, error.field)
                self.assertIs(DocumentRole.LOCAL, error.role)
                self.assertEqual(companion.resolve(), error.path)
                self.assertIs(DocumentErrorClassification.SCHEMA_ERROR, error.classification)

    def test_domain_invalid_values_keep_path_and_field_without_the_value(self) -> None:
        secret = "rejected-proxy-secret-4b1e"
        cases = (
            ('[host-access]\naddress = "not-an-ip"\n', "local.host-access.address"),
            ("[cache]\ndir = 5\n", "local.cache.dir"),
            ('[corporate-trust]\nenabled = "yes"\n', "local.corporate-trust.enabled"),
            (f'[network.proxy]\nurl = "ftp://{secret}:1"\n', "local.network.proxy.url"),
        )
        for body, field in cases:
            with self.subTest(field=field):
                companion = self._companion(body)
                error = self._load_error(companion)
                self.assertEqual(field, error.field)
                self.assertEqual(companion.resolve(), error.path)
                self.assertNotIn(secret, str(error))

    def test_owner_parsers_also_carry_the_structured_field(self) -> None:
        with self.assertRaises(InventoryError) as raised:
            validate_local_document({"cache": {"address": "10.0.2.2"}})
        self.assertEqual("local.cache.address", raised.exception.field)


class TestAbsentCompanionDefaults(_LocalTest):
    """Task 2.5: absent companion yields immutable defaults, no file created."""

    def test_absent_companion_returns_immutable_domain_defaults(self) -> None:
        inventory_path = self._fixed_project()
        companion = inventory_path.with_name(LOCAL_COMPANION_BASENAME)
        self.assertFalse(companion.exists())

        local = load_optional_local_project_configuration(inventory_path)

        self.assertEqual(LocalConfig(), local)
        self.assertFalse(companion.exists())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            local.cache = LocalCacheConfig()  # type: ignore[misc]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            local.cache.dir = "/unsafe"  # type: ignore[misc]

    def test_missing_companion_dispatches_all_parsers_with_the_mode(self) -> None:
        inventory_path = self._fixed_project()
        companion = inventory_path.with_name(LOCAL_COMPANION_BASENAME)
        self.assertFalse(companion.exists())

        calls: list[tuple[str, object, str | None]] = []
        spies = tuple(
            dataclasses.replace(
                entry,
                parse=lambda value, mode, _entry=entry: (
                    calls.append((_entry.toml_table, value, mode))
                    or _entry.parse(value, mode)
                ),
            )
            for entry in _LOCAL_DOMAIN_TABLES
        )
        with patch.object(local_aggregate, "_LOCAL_DOMAIN_TABLES", spies):
            local = load_optional_local_project_configuration(
                inventory_path, host_access_mode="docker-gateway"
            )

        self.assertEqual(
            ["host-access", "cache", "corporate-trust", "network"],
            [table for table, _, _ in calls],
        )
        self.assertTrue(all(value is None for _, value, _ in calls))
        self.assertTrue(all(mode == "docker-gateway" for _, _, mode in calls))
        self.assertEqual(LocalConfig(), local)
        self.assertFalse(companion.exists())

        # An absent companion is never parsed or routed through the file loader.
        with patch.object(
            local_aggregate, "load_local_project_configuration"
        ) as file_loader:
            load_optional_local_project_configuration(inventory_path)
        file_loader.assert_not_called()
        self.assertFalse(companion.exists())

    def test_project_configuration_absent_companion_uses_domain_defaults(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        reviewed = Path(directory.name) / "docker-constructor.toml"
        reviewed.write_text((repo_root / "docker-constructor.toml").read_text())
        companion = reviewed.with_name(LOCAL_COMPANION_BASENAME)
        self.assertFalse(companion.exists())

        calls: list[object] = []
        spies = tuple(
            dataclasses.replace(
                entry,
                parse=lambda value, mode, _entry=entry: (
                    calls.append(value) or _entry.parse(value, mode)
                ),
            )
            for entry in _LOCAL_DOMAIN_TABLES
        )
        with patch.object(local_aggregate, "_LOCAL_DOMAIN_TABLES", spies):
            _, local = inventory.load_project_configuration(
                reviewed, host_access_mode="docker-gateway"
            )

        self.assertEqual([None, None, None, None], calls)
        self.assertEqual(LocalConfig(), local)
        self.assertFalse(companion.exists())

    def test_optional_loader_ignores_ancestor_and_alternate_basenames(self) -> None:
        with tempfile.TemporaryDirectory() as outer:
            outer_root = Path(outer)
            project = outer_root / "selected-project"
            project.mkdir()
            inventory_path = project / "docker-constructor.toml"
            inventory_path.write_text("")
            (outer_root / LOCAL_COMPANION_BASENAME).write_text("[cache]\ndir = \"/ancestor\"\n")
            (project / "custom.local.toml").write_text("[cache]\ndir = \"/custom\"\n")

            local = load_optional_local_project_configuration(inventory_path)

            self.assertIsNone(local.cache.dir)
            self.assertFalse((project / LOCAL_COMPANION_BASENAME).exists())


class TestDomainOwnedDispatch(_LocalTest):
    """Task 2.8: the aggregate owner is closed registration plus dispatch."""

    def test_registry_references_imported_domain_owned_parsers(self) -> None:
        expected = {
            "host-access": host_access.parse_local_host_access,
            "cache": cache_storage.parse_local_cache_config,
            "corporate-trust": corporate_network.parse_local_corporate_trust,
            "network": corporate_network.parse_local_network_proxy,
        }
        self.assertEqual(set(expected), set(LOCAL_TABLE_NAMES))
        for entry in _LOCAL_DOMAIN_TABLES:
            with self.subTest(table=entry.toml_table):
                self.assertIs(expected[entry.toml_table], entry.parse)
                self.assertEqual(
                    expected[entry.toml_table].__module__, entry.parse.__module__
                )

    def test_validate_dispatches_recognized_tables_to_registered_parsers(self) -> None:
        raw = tomllib.loads(_ALL_TABLES)
        seen: list[str] = []
        spies = tuple(
            dataclasses.replace(
                entry,
                parse=lambda value, mode, _entry=entry: (
                    seen.append(_entry.toml_table) or _entry.parse(value, mode)
                ),
            )
            for entry in _LOCAL_DOMAIN_TABLES
        )
        with patch.object(local_aggregate, "_LOCAL_DOMAIN_TABLES", spies):
            local = validate_local_document(raw)
        self.assertEqual(
            ["host-access", "cache", "corporate-trust", "network"], seen
        )
        self.assertEqual("10.0.2.2", local.host_access.address)
        self.assertEqual("/var/tmp/cache", local.cache.dir)

    def test_unknown_table_is_rejected_before_any_registered_parser_runs(self) -> None:
        raw = tomllib.loads('[output]\ndir = "/tmp"\n')
        seen: list[str] = []
        spies = tuple(
            dataclasses.replace(
                entry,
                parse=lambda value, mode, _entry=entry: (
                    seen.append(_entry.toml_table) or _entry.parse(value, mode)
                ),
            )
            for entry in _LOCAL_DOMAIN_TABLES
        )
        with patch.object(local_aggregate, "_LOCAL_DOMAIN_TABLES", spies):
            with self.assertRaises(InventoryError):
                validate_local_document(raw)
        self.assertEqual([], seen)

    def test_aggregate_module_contains_no_domain_specific_validation(self) -> None:
        source = inspect.getsource(local_aggregate)
        forbidden = (
            "ipaddress",
            "ip_address",
            "urllib",
            "urlsplit",
            "_validate_proxy_url",
            "_parse_host_access_table",
            "_parse_cache_table",
            "_parse_corporate_trust_table",
            "_parse_network_table",
        )
        for token in forbidden:
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def test_owner_modules_own_the_moved_field_and_semantic_validation(self) -> None:
        # Positive control: the semantics left the aggregate owner.
        self.assertIn("ipaddress", inspect.getsource(host_access))
        self.assertIn("urlsplit", inspect.getsource(corporate_network))
        self.assertIn("parse_local_cache_config", inspect.getsource(cache_storage))


class TestAbsentTableDefaults(_LocalTest):
    """Absent tables are defaulted by their owning capability, not the aggregate."""

    def test_every_domain_parser_is_invoked_for_an_empty_document(self) -> None:
        seen: list[str] = []
        spies = tuple(
            dataclasses.replace(
                entry,
                parse=lambda value, mode, _entry=entry: (
                    seen.append(_entry.toml_table) or _entry.parse(value, mode)
                ),
            )
            for entry in _LOCAL_DOMAIN_TABLES
        )
        with patch.object(local_aggregate, "_LOCAL_DOMAIN_TABLES", spies):
            local = validate_local_document({})
        self.assertEqual(
            ["host-access", "cache", "corporate-trust", "network"], seen
        )
        self.assertEqual(LocalConfig(), local)

    def test_owner_parsers_supply_the_absent_table_defaults(self) -> None:
        self.assertEqual(
            LocalHostAccess(), host_access.parse_local_host_access(None, None)
        )
        self.assertEqual(
            LocalCacheConfig(), cache_storage.parse_local_cache_config(None, None)
        )
        self.assertEqual(
            LocalCorporateTrust(),
            corporate_network.parse_local_corporate_trust(None, None),
        )
        self.assertEqual(
            LocalNetworkProxy(),
            corporate_network.parse_local_network_proxy(None, None),
        )

    def test_present_table_does_not_change_absent_table_defaults(self) -> None:
        local = validate_local_document({"cache": {"dir": "/var/tmp/cache"}})
        self.assertEqual("/var/tmp/cache", local.cache.dir)
        self.assertEqual(LocalHostAccess(), local.host_access)
        self.assertEqual(LocalCorporateTrust(), local.corporate_trust)
        self.assertEqual(LocalNetworkProxy(), local.network_proxy)

    def test_explicit_non_table_values_are_still_rejected(self) -> None:
        for table, field in (
            ("host-access", "local.host-access"),
            ("cache", "local.cache"),
            ("corporate-trust", "local.corporate-trust"),
            ("network", "local.network"),
        ):
            with self.subTest(table=table):
                with self.assertRaises(InventoryError) as raised:
                    validate_local_document({table: "invalid"})
                self.assertEqual(field, raised.exception.field)

    def test_absent_table_is_not_equivalent_to_an_invalid_value(self) -> None:
        self.assertEqual(LocalConfig(), validate_local_document({}))
        with self.assertRaises(InventoryError):
            validate_local_document({"cache": "invalid"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

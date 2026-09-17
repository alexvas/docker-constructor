"""RED contracts for typed reviewed host-access and local constructor state.

These tests intentionally precede the Phase 1 implementation.  They define the
public configuration boundary; run this module before the GREEN tasks and expect
failures until that boundary exists.
"""
from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path

from docker.versions import InventoryError, load_inventory
from docker.versioning.configuration_document_validation import (
    ConfigurationDocumentError,
    DocumentRole,
)
from docker.versioning.effective import resolve_build_projection, resolve_runtime, to_plain_data
from docker.versioning.rendering import serialize_effective_build


_REPO_ROOT = Path(__file__).resolve().parents[1]
_CANONICAL = (_REPO_ROOT / "docker-constructor.toml").read_text()


def _write_toml(content: str, *, directory: Path | None = None, name: str | None = None) -> Path:
    if directory is not None:
        path = directory / (name or "inventory.toml")
        path.write_text(content)
        return path
    handle, path = tempfile.mkstemp(suffix=".toml")
    with os.fdopen(handle, "w") as stream:
        stream.write(content)
    return Path(path)


def _remove_table(content: str, table: str) -> str:
    """Remove one TOML table structurally, regardless of its comments/body."""
    pattern = re.compile(rf"(?ms)^\[{re.escape(table)}\]\n.*?(?=^\[|\Z)")
    updated, count = pattern.subn("", content)
    if count > 1:
        raise AssertionError(f"fixture has duplicate [{table}] tables")
    if re.search(rf"(?m)^\[{re.escape(table)}\]$", updated):
        raise AssertionError(f"failed to remove [{table}] from fixture")
    return updated


def _set_table(content: str, table: str, body: str) -> str:
    updated = _remove_table(content, table)
    updated += f"\n[{table}]\n{body.rstrip()}\n"
    if len(re.findall(rf"(?m)^\[{re.escape(table)}\]$", updated)) != 1:
        raise AssertionError(f"failed to inject exactly one [{table}] table")
    return updated


def _flatten(value: object) -> list[object]:
    if isinstance(value, dict):
        result: list[object] = list(value)
        for child in value.values():
            result.extend(_flatten(child))
        return result
    if isinstance(value, (list, tuple)):
        return [item for child in value for item in _flatten(child)]
    return [value]


class _InventoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.paths: list[Path] = []

    def tearDown(self) -> None:
        for path in self.paths:
            path.unlink(missing_ok=True)

    def inventory(
        self,
        extra: str = "",
        *,
        cache: str | None = None,
        directory: Path | None = None,
        name: str | None = None,
    ) -> Path:
        # Fixtures own these optional sections so future canonical policy or
        # comments cannot change what a test case exercises.
        content = _remove_table(_remove_table(_CANONICAL, "runtime.host-access"), "cache")
        if cache is not None:
            content = _set_table(content, "cache", cache)
        content += "\n" + extra
        path = _write_toml(content, directory=directory, name=name)
        if directory is None:
            self.paths.append(path)
        return path

    def local(self, content: str) -> Path:
        """Write raw local TOML: it is never an inventory overlay."""
        path = _write_toml(content)
        self.paths.append(path)
        return path


class TestReviewedHostAccessPolicyRed(_InventoryTest):
    def test_absent_policy_defaults_to_disabled(self) -> None:
        inventory = load_inventory(self.inventory())
        self.assertFalse(inventory.runtime.host_access.enabled)

    def test_disabled_policy_is_accepted_without_mode_or_proxy_port(self) -> None:
        policy = load_inventory(self.inventory(
            "[runtime.host-access]\nenabled = false\n"
        )).runtime.host_access
        self.assertFalse(policy.enabled)
        self.assertIsNone(policy.mode)
        self.assertIsNone(policy.proxy_port)

    def test_disabled_policy_rejects_mode_and_proxy_port(self) -> None:
        for setting in ('mode = "docker-gateway"', "proxy-port = 1080"):
            with self.subTest(setting=setting):
                with self.assertRaisesRegex(
                    InventoryError, r"runtime\.host-access\.(mode|proxy-port)"
                ):
                    load_inventory(self.inventory(
                        "[runtime.host-access]\nenabled = false\n" + setting
                    ))

    def test_enabled_policy_requires_supported_mode(self) -> None:
        for mode in ("", "host-gateway", "docker", "external"):
            extra = "[runtime.host-access]\nenabled = true\n"
            if mode:
                extra += f'mode = "{mode}"\n'
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(InventoryError, r"runtime\.host-access\.mode"):
                    load_inventory(self.inventory(extra))

    def test_enabled_policy_allows_omitted_proxy_port_and_rejects_unknown_keys(self) -> None:
        for mode in ("docker-gateway", "external-address"):
            with self.subTest(mode=mode):
                policy = load_inventory(self.inventory(
                    "[runtime.host-access]\nenabled = true\n"
                    f'mode = "{mode}"\n'
                )).runtime.host_access
                self.assertTrue(policy.enabled)
                self.assertEqual(policy.mode, mode)
                self.assertIsNone(policy.proxy_port)

        with self.assertRaisesRegex(InventoryError, r"runtime\.host-access\.unknown"):
            load_inventory(self.inventory(
                "[runtime.host-access]\nenabled = true\n"
                'mode = "docker-gateway"\nunknown = true\n'
            ))

    def test_enabled_policy_accepts_only_valid_integer_proxy_ports(self) -> None:
        for port in (0, 65536, -1, "true", '"1080"'):
            with self.subTest(port=port):
                with self.assertRaisesRegex(InventoryError, r"runtime\.host-access\.proxy-port"):
                    load_inventory(self.inventory(
                        "[runtime.host-access]\nenabled = true\n"
                        'mode = "docker-gateway"\n'
                        f"proxy-port = {port}\n"
                    ))
        for mode in ("docker-gateway", "external-address"):
            for port in (1, 1080, 65535):
                with self.subTest(mode=mode, port=port):
                    policy = load_inventory(self.inventory(
                        "[runtime.host-access]\nenabled = true\n"
                        f'mode = "{mode}"\nproxy-port = {port}\n'
                    )).runtime.host_access
                    self.assertEqual(policy.proxy_port, port)


class TestLocalCompanionRed(_InventoryTest):
    def test_canonical_and_custom_paths_use_the_fixed_companion(self) -> None:
        from docker.versioning.inventory import resolve_local_companion_path

        self.assertEqual(
            resolve_local_companion_path(Path("docker-constructor.toml")),
            Path("docker-constructor.local.toml"),
        )
        self.assertEqual(
            resolve_local_companion_path(Path("/path/custom.toml")),
            Path("/path/docker-constructor.local.toml"),
        )

    def test_custom_inventory_loads_fixed_companion_without_repository_fallback(self) -> None:
        from docker.versioning.inventory import load_local_config_for_inventory

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            repository = root_path / "repository"
            workspace = root_path / "workspace"
            repository.mkdir()
            workspace.mkdir()
            _write_toml('[cache]\ndir = "/repository-fallback"\n',
                        directory=repository, name="docker-constructor.local.toml")
            inventory = self.inventory(directory=workspace, name="custom.toml")
            _write_toml('[cache]\ndir = "/custom-local"\n',
                        directory=workspace, name="docker-constructor.local.toml")

            # The production default inventory root is an authoritative
            # boundary, not the process CWD.  Inject it explicitly so a
            # fallback implementation cannot accidentally pass this test.
            local = load_local_config_for_inventory(
                inventory, repository_root=repository
            )
            self.assertEqual(local.cache.dir, "/custom-local")

            # Removing the fixed companion must produce absent local state,
            # never the injected repository-local fallback value.
            (workspace / "docker-constructor.local.toml").unlink()
            absent = load_local_config_for_inventory(
                inventory, repository_root=repository
            )

        self.assertIsNone(absent.cache.dir)

    def test_closed_local_schema_and_values(self) -> None:
        from docker.versioning.inventory import load_local_config

        valid = self.local('[host-access]\naddress = "10.0.2.2"\n[cache]\ndir = "/tmp/cache"\n')
        local = load_local_config(valid)
        self.assertEqual(local.host_access.address, "10.0.2.2")
        self.assertEqual(local.cache.dir, "/tmp/cache")

        ipv6 = self.local('[host-access]\naddress = "2001:db8::1"\n')
        self.assertEqual(
            load_local_config(ipv6, host_access_mode="external-address").host_access.address,
            "2001:db8::1",
        )

        invalid = (
            "not toml = [",
            "[unknown]\nvalue = true",
            "[host-access]\nunknown = \"x\"",
            "[cache]\nunknown = \"x\"",
            "[cache]\ndir = 42",
            '[host-access]\naddress = "not an address"',
        )
        for content in invalid:
            with self.subTest(content=content):
                path = self.local(content)
                with self.assertRaisesRegex(InventoryError, r"(host-access|cache|local)"):
                    load_local_config(path)

    def test_local_validation_errors_name_the_exact_path_and_recovery(self) -> None:
        from docker.versioning.inventory import load_local_config

        cases = (
            ("[host-access]\nfoo = true\n", "local.host-access.foo"),
            ("[cache]\nfoo = true\n", "local.cache.foo"),
            ("[host-access]\naddress = 42\n", "local.host-access.address"),
            ("[cache]\ndir = 42\n", "local.cache.dir"),
        )
        for content, path in cases:
            with self.subTest(path=path):
                local_path = self.local(content)
                with self.assertRaises(ConfigurationDocumentError) as raised:
                    load_local_config(local_path)
                error = raised.exception
                self.assertEqual("schema_error", error.classification)
                self.assertEqual(DocumentRole.LOCAL, error.role)
                self.assertEqual(local_path.resolve(), error.path)
                self.assertEqual(path, error.field)

    def test_external_address_rejects_host_gateway_token(self) -> None:
        from docker.versioning.inventory import load_local_config

        path = self.local('[host-access]\naddress = "host-gateway"\n')
        with self.assertRaisesRegex(InventoryError, r"host-access\.address"):
            load_local_config(path, host_access_mode="external-address")


class TestCacheAndProjectionRed(_InventoryTest):
    def test_cache_dir_is_local_but_ttl_remains_reviewed(self) -> None:
        with self.assertRaises(ConfigurationDocumentError) as raised:
            load_inventory(self.inventory(cache='dir = "/reviewed"'))
        self.assertEqual("schema_error", raised.exception.classification)
        self.assertEqual("cache.dir", raised.exception.field)
        self.assertNotIn("/reviewed", str(raised.exception))

        from docker.versioning.cache_storage import resolve_local_root
        from docker.versioning.inventory import load_local_config

        reviewed = load_inventory(self.inventory(cache="ttl = 123"))
        local_cfg = load_local_config(self.local("[cache]\ndir = \"/local-cache\"\n"))

        # Directory is machine-local; TTL stays reviewed policy.
        self.assertEqual(local_cfg.cache.dir, "/local-cache")
        self.assertEqual(reviewed.cache.ttl, 123)

        # The local directory is the dedicated constructor root.
        root = resolve_local_root(
            local_cfg.cache.dir,
            xdg_cache_home=os.environ.get("XDG_CACHE_HOME"),
            home=Path.home(),
        )
        self.assertEqual(root, Path("/local-cache"))

    def test_host_and_local_state_never_enter_dependency_projections(self) -> None:
        from docker.versioning.inventory import load_local_config_for_inventory

        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            inventory_path = self.inventory(
                "[runtime.host-access]\nenabled = true\n"
                'mode = "external-address"\nproxy-port = 45678\n',
                directory=directory_path, name="custom.toml",
            )
            inventory = load_inventory(inventory_path)
            companion = _write_toml(
                '[host-access]\naddress = "192.0.2.10"\n[cache]\ndir = "/local-cache"\n',
                directory=directory_path, name="docker-constructor.local.toml",
            )
            self.assertEqual(companion, inventory_path.with_name("docker-constructor.local.toml"))
            local = load_local_config_for_inventory(
                inventory_path, host_access_mode="external-address"
            )
            self.assertEqual(local.host_access.address, "192.0.2.10")

            _, runtime = resolve_runtime(inventory.runtime, {})
            build = resolve_build_projection(inventory.build, {})
            runtime_data = to_plain_data(runtime)
            build_data = serialize_effective_build(build)

        # Test the serialized projection boundaries, not dataclass repr().
        runtime_values = _flatten(runtime_data)
        build_values = _flatten(build_data)
        for forbidden in (
            "host_access", "host-access", "192.0.2.10", "/local-cache",
            45678, "docker-constructor.local.toml",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, runtime_values)
                self.assertNotIn(forbidden, build_values)


if __name__ == "__main__":
    unittest.main()

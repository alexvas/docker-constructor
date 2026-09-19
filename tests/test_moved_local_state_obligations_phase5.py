"""Focused functional regressions for atomic obligations moved out of
``runtime-host-access``.

``tests/test_ownership_cutover_phase5.py`` inventories every scenario and
every atomic normative obligation of the removed requirement ``Store
constructor-project machine-local state separately`` and maps each one to a
destination capability/requirement/scenario and one or more regression tests.

This module supplies the focused regressions that the pre-existing suite did
not exercise directly:

* no-follow inspection of an existing selected cache root (including a
  dangling symlink and a symlinked parent component);
* reviewed dependency/update/artifact/host-access-policy and cache-TTL
  isolation of the local companion;
* dangerous-root diagnostics that name the configured ``[cache].dir`` and
  require a dedicated owned directory;
* corporate trust and proxy configuration being usable without any
  host-access state.

Each regression here is referenced by exactly one inventory entry (or, for a
tree of branches, by the scenario entry that names it).
"""
from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from docker.versioning import cache_storage, inventory
from docker.versioning.errors import InventoryError
from docker.versioning.local_project_configuration import (
    load_local_project_configuration,
    validate_local_document,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]


class _CacheLayoutTestCase(unittest.TestCase):
    """Common temporary XDG/home layout for cache-storage regressions."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="moved-obligation-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.xdg = self.base / "xdg" / "cache"
        self.xdg.mkdir(parents=True)

    def _prepare_local(self, value: str) -> Path:
        return cache_storage.prepare_local_root(
            value, xdg_cache_home=str(self.xdg), home=self.home
        )


class TestNoFollowInspectionOfExistingSelectedRoot(_CacheLayoutTestCase):
    """The selected root is inspected without following a symlink."""

    def test_dangling_symlink_selected_root_rejected_without_creating_target(self) -> None:
        target = self.base / "missing-target"
        link = self.base / "link"
        link.symlink_to(target, target_is_directory=True)

        with self.assertRaises(cache_storage.CacheStorageError):
            self._prepare_local(str(link))

        self.assertTrue(link.is_symlink())
        self.assertFalse(
            target.exists(),
            "no-follow inspection must not create the symlink target",
        )

    def test_symlinked_parent_component_rejected_without_following(self) -> None:
        real = self.base / "real"
        real.mkdir()
        link = self.base / "link"
        link.symlink_to(real, target_is_directory=True)

        with self.assertRaises(cache_storage.CacheStorageError):
            self._prepare_local(str(link / "cache-dir"))

        self.assertEqual([], list(real.iterdir()))

    def test_existing_selected_root_is_inspected_and_secured_in_place(self) -> None:
        os.chmod(self.xdg, 0o755)
        value = self.xdg / "docker-constructor-custom"
        value.mkdir()
        os.chmod(value, 0o755)

        root = self._prepare_local(str(value))

        self.assertEqual(value, root)
        self.assertEqual(0o700, stat.S_IMODE(value.stat().st_mode))
        self.assertEqual(0o755, stat.S_IMODE(self.xdg.stat().st_mode))


class TestDangerousRootDiagnostics(unittest.TestCase):
    """Every shared or dangerous root reports the field and a dedicated remedy."""

    XDG = "/xdg/cache"
    HOME = Path("/home/testuser")

    def test_dangerous_root_diagnostics_name_cache_dir_and_require_dedicated(self) -> None:
        for value in (str(self.HOME), "/", "/xdg"):
            with self.subTest(value=value):
                with self.assertRaises(cache_storage.CacheStorageError) as raised:
                    cache_storage.resolve_local_root(
                        value, xdg_cache_home=self.XDG, home=self.HOME
                    )
                message = str(raised.exception)
                self.assertIn("local.cache.dir", message)
                self.assertIn("dedicated", message)


class TestReviewedStateIsolation(unittest.TestCase):
    """The local companion cannot declare or alter reviewed policy fields."""

    def test_local_companion_cannot_declare_reviewed_cache_ttl(self) -> None:
        with self.assertRaises(InventoryError) as raised:
            validate_local_document({"cache": {"ttl": 3600}})
        self.assertEqual("local.cache.ttl", raised.exception.field)

    def test_local_companion_cannot_declare_reviewed_inventory_tables(self) -> None:
        for table in ("build", "runtime", "update", "artifacts"):
            with self.subTest(table=table):
                with self.assertRaises(InventoryError) as raised:
                    validate_local_document({table: {"value": 1}})
                self.assertEqual(f"local.{table}", raised.exception.field)

    def test_local_companion_does_not_change_reviewed_projection(self) -> None:
        canonical = (_REPO_ROOT / "docker-constructor.toml").read_text(
            encoding="utf-8"
        )
        with tempfile.TemporaryDirectory(prefix="reviewed-isolation-") as tmp:
            root = Path(tmp)
            inventory_path = root / "docker-constructor.toml"
            inventory_path.write_text(canonical, encoding="utf-8")
            baseline = inventory.load_inventory(inventory_path)

            (root / "docker-constructor.local.toml").write_text(
                '[cache]\ndir = "/tmp/phase5-reviewed-isolation-cache"\n'
                '[network.proxy]\nurl = "http://proxy.example.test:8080"\n',
                encoding="utf-8",
            )
            after = inventory.load_inventory(inventory_path)

        self.assertEqual(baseline, after)
        self.assertEqual(baseline.cache, after.cache)
        self.assertEqual(baseline.stages, after.stages)
        self.assertNotIn("phase5-reviewed-isolation-cache", repr(after))


class TestCorporateNetworkIndependence(unittest.TestCase):
    """Corporate trust and proxy never require host-access state."""

    def _companion(self, content: str) -> Path:
        tmp = tempfile.TemporaryDirectory(prefix="moved-corp-")
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "docker-constructor.local.toml"
        path.write_text(content, encoding="utf-8")
        return path

    def test_corporate_trust_and_proxy_need_no_host_access_state(self) -> None:
        local = load_local_project_configuration(
            self._companion(
                "[corporate-trust]\nenabled = true\n"
                '[network.proxy]\nurl = "http://proxy.example.test:8080"\n'
            )
        )
        self.assertTrue(local.corporate_trust.enabled)
        self.assertEqual(
            "http://proxy.example.test:8080", local.network_proxy.url
        )
        self.assertIsNone(local.host_access.address)


class TestLocalCacheRootWithoutHostAccess(unittest.TestCase):
    """A configured local cache root is used with host access disabled.

    Replaces the previous mapping to ``test_custom_inventory_with_local_cache``,
    which enabled ``[runtime.host-access]`` and therefore could not prove
    independence from host access.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="moved-cache-no-host-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.xdg = self.root / "xdg" / "cache"
        self._saved_env = {
            key: os.environ.get(key) for key in ("HOME", "XDG_CACHE_HOME")
        }
        os.environ["HOME"] = str(self.home)
        os.environ["XDG_CACHE_HOME"] = str(self.xdg)
        self.addCleanup(self._restore_env)

    def _restore_env(self) -> None:
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _project(self) -> tuple[Path, Path]:
        inventory_path = self.root / "docker-constructor.toml"
        inventory_path.write_text(
            (_REPO_ROOT / "docker-constructor.toml").read_text(encoding="utf-8")
            + "\n[runtime.host-access]\nenabled = false\n",
            encoding="utf-8",
        )
        (self.root / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        configured = self.root / "dedicated-cache"
        (self.root / "docker-constructor.local.toml").write_text(
            f'[cache]\ndir = "{configured}"\n', encoding="utf-8"
        )
        return inventory_path, configured

    def test_configured_local_cache_root_is_used_with_host_access_disabled(self) -> None:
        from docker.versioning.build_orchestration import BuildRequest, plan_build
        from docker.versioning.dispatch_types import ExitKind
        from docker.versioning.transports import build_transports

        inventory_path, configured = self._project()

        _reviewed, local = inventory.load_project_configuration(inventory_path)
        self.assertIsNone(
            local.host_access.address,
            "host access is disabled, so no address state may be required",
        )
        self.assertEqual(str(configured), local.cache.dir)

        plan = plan_build(
            BuildRequest(
                inventory_path=str(inventory_path),
                repo_root=str(self.root),
                project_root=str(self.root),
                context=str(self.root),
                dockerfile=str(self.root / "Dockerfile"),
                platform="linux-amd64",
                tag=None,
                overrides={},
                cache=True,
                pull=False,
                progress="auto",
                uid=None,
                gid=None,
                confirmed=True,
                dry_run=True,
            )
        )
        self.assertEqual(ExitKind.SUCCESS, plan.exit_kind, plan.message)
        assert plan.cache_root is not None
        self.assertEqual(configured, plan.cache_root)
        rendered = plan.display_string or ""
        self.assertNotIn("HOST_ACCESS_ADDRESS", rendered)
        self.assertNotIn("HOST_PROXY_PORT", rendered)
        self.assertNotIn("--add-host", rendered)

        # A real cache consumer prepares the configured root, not the default.
        build_transports(local_cache=local.cache)
        self.assertTrue((configured / "versioning").is_dir())
        self.assertFalse(
            self.xdg.exists(),
            "the default XDG cache root must not be created when [cache].dir is set",
        )


if __name__ == "__main__":
    unittest.main()

"""RED contracts for Phase 4 integrated acceptance (task 4.2).

These tests exercise the public CLI facade (``docker/docker-constructor.py``
→ ``main`` → dispatcher → orchestration → render) rather than calling the
orchestration functions directly.  They cover the Phase 1–3 corporate
network contract end to end:

* valid configured build/run planning;
* disabled compatibility;
* custom-inventory companion resolution;
* bundle error handling before Docker;
* proxy URI rejection before Docker;
* explicit-only ``NO_PROXY``;
* host-access independence.

No test touches a real Docker daemon, socket, or network.  Build and run
use ``--dry-run``; verification uses a scripted process runner.
"""

from __future__ import annotations

import io
import json
import os
import re
import shlex
import shutil
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CANONICAL = (_REPO_ROOT / "docker-constructor.toml").read_text()

_SYSTEM_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"

_PROXY_URL = "http://proxy.corp.example:3128"
_NO_PROXY = "localhost,.corp.example"

_PROXY_URL_NAMES = (
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
)
_PROXY_BYPASS_NAMES = ("NO_PROXY", "no_proxy")

_VALID_BUNDLE = "-----BEGIN CERTIFICATE-----\nAQIDBAU=\n-----END CERTIFICATE-----\n"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


# ── facade helpers ──────────────────────────────────────────────────────


def _load_mod():
    from docker import constructor_cli

    return constructor_cli


def _run(mod, argv, **kw):
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = mod.main(list(argv), **kw)
    return rc, out.getvalue(), err.getvalue()


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _json_data(out: str) -> dict:
    return json.loads(out)


# ── fake repository layout ──────────────────────────────────────────────


@contextmanager
def _fake_repo(mod, *, companion=None, bundle=None):
    """Create a hermetic repository root and redirect ``_REPO_ROOT``.

    The facade derives both the default inventory path and the
    repository-local corporate bundle from ``docker.constructor_cli._REPO_ROOT``,
    so patching it keeps every public command path isolated from the real
    repository.
    """
    with tempfile.TemporaryDirectory() as root:
        root_path = Path(root)
        (root_path / "docker-constructor.toml").write_text(_CANONICAL)
        shutil.copyfile(_REPO_ROOT / "Dockerfile", root_path / "Dockerfile")
        if companion is not None:
            (root_path / "docker-constructor.local.toml").write_text(companion)
        if bundle is not None:
            bundle_dir = root_path / ".docker-local"
            bundle_dir.mkdir()
            (bundle_dir / "corporate-ca-bundle.crt").write_text(bundle)
        original_cwd = Path.cwd()
        try:
            os.chdir(root_path)
            with mock.patch.object(mod, "_INSTALLATION_ROOT", root_path):
                yield root_path
        finally:
            os.chdir(original_cwd)


# ── dry-run safety fakes ────────────────────────────────────────────────


class _BombExecutor:
    def run(self, argv, *, interactive=False):
        del argv, interactive
        raise AssertionError("docker run must not execute during dry-run")


class _BombInspector:
    def __init__(self, runner=None):
        del runner

    def list_names(self):
        raise AssertionError("container inspection must not run")


def _bomb_projection(*_args, **_kwargs):
    raise AssertionError("projection creation must not run")


# ── vector inspection helpers ───────────────────────────────────────────


def _collect_mounts(args) -> list[dict[str, str]]:
    mounts = []
    it = iter(args)
    for token in it:
        if token == "--mount":
            raw = next(it)
            kv = {}
            for pair in raw.split(","):
                k, _, v = pair.partition("=")
                kv[k] = v
            mounts.append(kv)
    return mounts


def _find_mount(mounts, dst):
    for m in mounts:
        if m.get("dst") == dst:
            return m
    return None


def _collect_env(args) -> dict[str, str]:
    env = {}
    it = iter(args)
    for token in it:
        if token == "--env":
            raw = next(it)
            k, _, v = raw.partition("=")
            env[k] = v
    return env


def _build_arg_pairs(args) -> dict[str, str]:
    if isinstance(args, str):
        args = shlex.split(args.split("\n", 1)[-1])
    it = iter(args)
    pairs = {}
    for token in it:
        if token == "--build-arg":
            raw = next(it)
            name, _, value = raw.partition("=")
            pairs[name] = value
    return pairs


def _assert_no_corporate_build_args(self, args) -> None:
    pairs = _build_arg_pairs(args)
    for name in (
        "PI_CORPORATE_PROXY_URL",
        "PI_CORPORATE_NO_PROXY",
        "PI_CORPORATE_CA_PATH",
        "CORPORATE_TRUST_ENABLED",
    ):
        self.assertNotIn(name, pairs, f"unexpected corporate arg {name}")


def _assert_no_proxy_run_env(self, env: dict[str, str]) -> None:
    for name in _PROXY_URL_NAMES + _PROXY_BYPASS_NAMES:
        self.assertNotIn(name, env, f"unexpected proxy env {name}")


# ── Phase 4.2 integrated acceptance tests ───────────────────────────────


class TestConfiguredBuildRunAcceptanceRed(unittest.TestCase):
    """Valid configured corporate settings flow through build/run planning."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = _load_mod()
        cls.companion = (
            "[corporate-trust]\nenabled = true\n"
            f'[network.proxy]\nurl = "{_PROXY_URL}"\n'
            f'no_proxy = "{_NO_PROXY}"\n'
        )

    def test_configured_build_dry_run_carries_corporate_args(self) -> None:
        with _fake_repo(self.m, companion=self.companion, bundle=_VALID_BUNDLE):
            rc, out, _ = _run(
                self.m,
                ["--output", "json", "build", "--dry-run"],
            )
            self.assertEqual(0, rc, out)
            data = _json_data(out)
            self.assertEqual("success", data["status"])
            pairs = _build_arg_pairs(data["data"]["display_string"])
            self.assertEqual(_PROXY_URL, pairs["PI_CORPORATE_PROXY_URL"])
            self.assertEqual(_NO_PROXY, pairs["PI_CORPORATE_NO_PROXY"])
            self.assertEqual("true", pairs["CORPORATE_TRUST_ENABLED"])
            self.assertEqual(
                _SYSTEM_CA_BUNDLE,
                pairs["PI_CORPORATE_CA_PATH"],
            )

    def test_configured_run_dry_run_carries_mount_and_env(self) -> None:
        with _fake_repo(self.m, companion=self.companion, bundle=_VALID_BUNDLE):
            rc, out, _ = _run(
                self.m,
                [
                    "--output",
                    "json",
                    "run",
                    "--dry-run",
                    "--workspace",
                    "/work/project",
                ],
                _run_executor=_BombExecutor(),
                _container_inspector=_BombInspector(),
                _create_projection=_bomb_projection,
            )
            self.assertEqual(0, rc, out)
            data = _json_data(out)
            self.assertEqual("success", data["status"])
            args = data["data"]["run_args"]
            mount = _find_mount(_collect_mounts(args), _SYSTEM_CA_BUNDLE)
            self.assertIsNotNone(mount, "missing corporate trust mount")
            self.assertEqual("bind", mount["type"])
            self.assertIn("readonly", mount)
            env = _collect_env(args)
            for name in _PROXY_URL_NAMES:
                self.assertEqual(_PROXY_URL, env[name], name)
            self.assertEqual(_NO_PROXY, env["NO_PROXY"])
            self.assertEqual(_NO_PROXY, env["no_proxy"])


class TestDisabledCompatibilityAcceptanceRed(unittest.TestCase):
    """Disabled corporate settings preserve the default vectors."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = _load_mod()

    def test_disabled_build_dry_run_emits_no_corporate_args(self) -> None:
        for companion in (None, "[corporate-trust]\nenabled = false\n"):
            with self.subTest(companion=companion):
                with _fake_repo(self.m, companion=companion):
                    rc, out, _ = _run(
                        self.m,
                        ["--output", "json", "build", "--dry-run"],
                    )
                    self.assertEqual(0, rc, out)
                    data = _json_data(out)
                    _assert_no_corporate_build_args(
                        self,
                        data["data"]["display_string"],
                    )

    def test_disabled_run_dry_run_emits_no_mount_or_proxy(self) -> None:
        with _fake_repo(self.m):
            rc, out, _ = _run(
                self.m,
                [
                    "--output",
                    "json",
                    "run",
                    "--dry-run",
                    "--workspace",
                    "/work/project",
                ],
                _run_executor=_BombExecutor(),
                _container_inspector=_BombInspector(),
                _create_projection=_bomb_projection,
            )
            self.assertEqual(0, rc, out)
            data = _json_data(out)
            args = data["data"]["run_args"]
            self.assertIsNone(
                _find_mount(_collect_mounts(args), _SYSTEM_CA_BUNDLE),
                "disabled trust must not mount the system bundle",
            )
            _assert_no_proxy_run_env(self, _collect_env(args))


class TestCustomInventoryCompanionAcceptanceRed(unittest.TestCase):
    """A custom inventory resolves its companion beside itself, never the
    repository root."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = _load_mod()

    def test_custom_companion_resolved_beside_custom_inventory(self) -> None:
        with (
            tempfile.TemporaryDirectory() as inv_dir,
            tempfile.TemporaryDirectory() as repo_dir,
        ):
            inv_path = Path(inv_dir) / "docker-constructor.toml"
            inv_path.write_text(_CANONICAL)
            shutil.copyfile(_REPO_ROOT / "Dockerfile", Path(inv_dir) / "Dockerfile")
            (Path(inv_dir) / "docker-constructor.local.toml").write_text(
                f'[network.proxy]\nurl = "{_PROXY_URL}"\n'
            )
            repo_path = Path(repo_dir)
            (repo_path / "docker-constructor.toml").write_text(_CANONICAL)
            with mock.patch.object(self.m, "_INSTALLATION_ROOT", repo_path):
                rc, out, _ = _run(
                    self.m,
                    [
                        "--output",
                        "json",
                        "--project-directory",
                        str(Path(inv_path).parent),
                        "build",
                        "--dry-run",
                    ],
                )
            self.assertEqual(0, rc, out)
            pairs = _build_arg_pairs(_json_data(out)["data"]["display_string"])
            self.assertEqual(_PROXY_URL, pairs["PI_CORPORATE_PROXY_URL"])

    def test_custom_inventory_does_not_fall_back_to_repo_root_companion(self) -> None:
        with (
            tempfile.TemporaryDirectory() as inv_dir,
            tempfile.TemporaryDirectory() as repo_dir,
        ):
            inv_path = Path(inv_dir) / "docker-constructor.toml"
            inv_path.write_text(_CANONICAL)
            shutil.copyfile(_REPO_ROOT / "Dockerfile", Path(inv_dir) / "Dockerfile")
            repo_path = Path(repo_dir)
            (repo_path / "docker-constructor.toml").write_text(_CANONICAL)
            (repo_path / "docker-constructor.local.toml").write_text(
                f'[network.proxy]\nurl = "{_PROXY_URL}"\n'
            )
            with mock.patch.object(self.m, "_INSTALLATION_ROOT", repo_path):
                rc, out, _ = _run(
                    self.m,
                    [
                        "--output",
                        "json",
                        "--project-directory",
                        str(Path(inv_path).parent),
                        "build",
                        "--dry-run",
                    ],
                )
            self.assertEqual(0, rc, out)
            pairs = _build_arg_pairs(_json_data(out)["data"]["display_string"])
            self.assertNotIn("PI_CORPORATE_PROXY_URL", pairs)


class TestBundleErrorHandlingAcceptanceRed(unittest.TestCase):
    """Enabled trust with an unusable bundle fails CONFIG before Docker."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = _load_mod()

    def test_missing_bundle_fails_build_before_docker(self) -> None:
        with _fake_repo(self.m, companion="[corporate-trust]\nenabled = true\n"):
            rc, out, _ = _run(
                self.m,
                ["--output", "json", "build", "--dry-run"],
            )
            self.assertEqual(3, rc)
            data = _json_data(out)
            self.assertEqual("config", data["status"])
            self.assertIn("corporate-ca-bundle.crt", data.get("message", ""))

    def test_malformed_bundle_fails_run_before_docker(self) -> None:
        with _fake_repo(
            self.m,
            companion="[corporate-trust]\nenabled = true\n",
            bundle="not a pem bundle\n",
        ):
            rc, out, _ = _run(
                self.m,
                [
                    "--output",
                    "json",
                    "run",
                    "--dry-run",
                    "--workspace",
                    "/work/project",
                ],
                _run_executor=_BombExecutor(),
                _container_inspector=_BombInspector(),
                _create_projection=_bomb_projection,
            )
            self.assertEqual(3, rc)
            data = _json_data(out)
            self.assertEqual("config", data["status"])
            self.assertIn("corporate-ca-bundle.crt", data.get("message", ""))

    def test_invalid_bundle_fails_verify_before_docker(self) -> None:
        calls: list[tuple] = []

        class _Runner:
            def run(self, argv, *, mode=None):
                calls.append(tuple(argv))
                raise AssertionError("docker must not run before CONFIG")

        with _fake_repo(self.m, companion="[corporate-trust]\nenabled = true\n"):
            rc, out, _ = _run(
                self.m,
                [
                    "--output",
                    "json",
                    "verify",
                    "--scope",
                    "runtime",
                    "--image",
                    "pi-cli-pi:latest",
                ],
                _process_runner=_Runner(),
            )
            self.assertEqual(3, rc)
            data = _json_data(out)
            self.assertEqual("config", data["status"])
            self.assertIn("corporate-ca-bundle.crt", data.get("message", ""))
        self.assertEqual([], calls)


class TestProxyUriRejectionAcceptanceRed(unittest.TestCase):
    """Invalid proxy URLs fail CONFIG before Docker for build and run."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = _load_mod()

    def test_invalid_proxy_urls_fail_build_before_docker(self) -> None:
        invalid = (
            'url = "http://user:pass@proxy.corp.example:3128"',
            'url = "ftp://proxy.corp.example:3128"',
            'url = "http://proxy.corp.example:3128/path"',
            'url = "http://proxy.corp.example"',
        )
        for url in invalid:
            with self.subTest(url=url):
                with _fake_repo(self.m, companion=f"[network.proxy]\n{url}\n"):
                    rc, out, _ = _run(
                        self.m,
                        ["--output", "json", "build", "--dry-run"],
                    )
                    self.assertEqual(3, rc)
                    data = _json_data(out)
                    self.assertEqual("config", data["status"])
                    self.assertIn(
                        "local.network.proxy.url",
                        data.get("message", ""),
                    )

    def test_invalid_proxy_url_fails_run_before_docker(self) -> None:
        with _fake_repo(
            self.m,
            companion='[network.proxy]\nurl = "http://user:pass@proxy:3128"\n',
        ):
            rc, out, _ = _run(
                self.m,
                [
                    "--output",
                    "json",
                    "run",
                    "--dry-run",
                    "--workspace",
                    "/work/project",
                ],
                _run_executor=_BombExecutor(),
                _container_inspector=_BombInspector(),
                _create_projection=_bomb_projection,
            )
            self.assertEqual(3, rc)
            data = _json_data(out)
            self.assertEqual("config", data["status"])
            self.assertIn("local.network.proxy.url", data.get("message", ""))


class TestExplicitOnlyNoProxyAcceptanceRed(unittest.TestCase):
    """NO_PROXY is emitted only when the bypass list is configured."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = _load_mod()

    def _run_dry_run(self, companion: str) -> dict[str, str]:
        with _fake_repo(self.m, companion=companion):
            rc, out, _ = _run(
                self.m,
                [
                    "--output",
                    "json",
                    "run",
                    "--dry-run",
                    "--workspace",
                    "/work/project",
                ],
                _run_executor=_BombExecutor(),
                _container_inspector=_BombInspector(),
                _create_projection=_bomb_projection,
            )
            self.assertEqual(0, rc, out)
            return _collect_env(_json_data(out)["data"]["run_args"])

    def test_proxy_without_no_proxy_emits_url_only(self) -> None:
        env = self._run_dry_run(f'[network.proxy]\nurl = "{_PROXY_URL}"\n')
        for name in _PROXY_URL_NAMES:
            self.assertEqual(_PROXY_URL, env[name], name)
        for name in _PROXY_BYPASS_NAMES:
            self.assertNotIn(name, env, name)

    def test_explicit_no_proxy_emitted_under_both_forms(self) -> None:
        env = self._run_dry_run(
            f'[network.proxy]\nurl = "{_PROXY_URL}"\nno_proxy = "{_NO_PROXY}"\n'
        )
        self.assertEqual(_NO_PROXY, env["NO_PROXY"])
        self.assertEqual(_NO_PROXY, env["no_proxy"])


class TestHostAccessIndependenceAcceptanceRed(unittest.TestCase):
    """A valid external proxy requires no host access and changes no
    host-access mapping."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = _load_mod()

    def test_external_proxy_without_host_access_plans_and_runs(self) -> None:
        companion = f'[network.proxy]\nurl = "{_PROXY_URL}"\n'
        with _fake_repo(self.m, companion=companion):
            rc, out, _ = _run(
                self.m,
                ["--output", "json", "build", "--dry-run"],
            )
            self.assertEqual(0, rc, out)
            pairs = _build_arg_pairs(_json_data(out)["data"]["display_string"])
            self.assertEqual(_PROXY_URL, pairs["PI_CORPORATE_PROXY_URL"])

            rc, out, _ = _run(
                self.m,
                [
                    "--output",
                    "json",
                    "run",
                    "--dry-run",
                    "--workspace",
                    "/work/project",
                ],
                _run_executor=_BombExecutor(),
                _container_inspector=_BombInspector(),
                _create_projection=_bomb_projection,
            )
            self.assertEqual(0, rc, out)
            args = _json_data(out)["data"]["run_args"]
            env = _collect_env(args)
            self.assertEqual(_PROXY_URL, env["HTTP_PROXY"])
            self.assertNotIn("HOST_ACCESS_ADDRESS", env)
            self.assertNotIn("HOST_PROXY_PORT", env)
            self.assertNotIn(
                "--add-host", args, "host-access mapping must not be emitted"
            )


if __name__ == "__main__":
    unittest.main()

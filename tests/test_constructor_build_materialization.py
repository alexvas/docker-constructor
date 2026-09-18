from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from unittest.mock import patch
from dataclasses import replace
from pathlib import Path

from tests.build_test_support import (
    digest_valid_selected_artifacts, fake_pi_materialization, fixture_directory,
    publish_digest_valid_artifacts,
)

from docker.versioning.build_snapshot import MaterializedSnapshot
from docker.versioning.build_materialization import (
    HostNetworkPolicy, MaterializationError, SelectedBuildArtifact,
    UrllibStreamingTransport, materialize_artifact, select_build_artifacts,
)
from docker.versioning.digest_identity import DigestIdentity
from docker.versioning.effective import resolve_build_projection
from docker.versioning.inventory import load_inventory
from docker.versioning.build_orchestration import (
    BuildRequest, PublishResult, execute_build, orchestrate_build, plan_build,
)
from docker.networking import ProcessResult
from docker.versioning.dispatch_types import ExitKind

ROOT = Path(__file__).resolve().parents[1]


class RecordingTransport:
    def __init__(self, chunks=(), error: Exception | None = None):
        self.chunks, self.error, self.calls = chunks, error, []
    def stream(self, url):
        self.calls.append(url)
        for chunk in self.chunks:
            yield chunk
        if self.error:
            raise self.error


def selection():
    inventory = load_inventory(ROOT / "docker-constructor.toml")
    return resolve_build_projection(inventory.build, {}, platform="linux-amd64")


class TestBuildArtifactSelection(unittest.TestCase):
    def test_exact_effective_linux_amd64_pairs(self):
        selected = select_build_artifacts(selection())
        self.assertEqual([
            ("rustup", "https://static.rust-lang.org/rustup/archive/1.29.0/x86_64-unknown-linux-gnu/rustup-init", "4acc9acc76d5079515b46346a485974457b5a79893cfb01112423c89aeb5aa10"),
            ("uv", "https://github.com/astral-sh/uv/releases/download/0.12.5/uv-x86_64-unknown-linux-gnu.tar.gz", "68a509da24b06b4223a1c0175fb5eb5bc79342b76cbeff0cfe51ac3f5b17b6b2"),
            ("rtk", "https://github.com/rtk-ai/rtk/releases/download/v0.45.0/rtk_amd64.deb", "0ba496b2531cd4357edfb2ac2fe2eb19ec99eb9ba46401dffa8459e1cfbd0061"),
            ("fd", "https://github.com/sharkdp/fd/releases/download/v10.4.2/fd_10.4.2_amd64.deb", "0e44eb5fca93f09bc6f5430b90acdf44c8e069d0a903700aeb4820629337b67b"),
        ], [(x.name, x.url, x.identity.hex_digest()) for x in selected])

    def test_rustup_is_pinned_to_reviewed_immutable_archive_source(self):
        inventory = load_inventory(ROOT / "docker-constructor.toml")
        rustup = inventory.stages.toolchain.rust.rustup["linux-amd64"]
        source = inventory.stages.toolchain.rust.rustup_source
        self.assertEqual("https://static.rust-lang.org/rustup/archive/1.29.0/x86_64-unknown-linux-gnu/rustup-init", rustup.url)
        self.assertEqual("https://static.rust-lang.org/rustup/archive/1.29.0/x86_64-unknown-linux-gnu/rustup-init.sha256", source.checksum_url)
        self.assertNotIn("/rustup/dist/", rustup.url)
        self.assertNotIn("/rustup/dist/", source.checksum_url)
        self.assertEqual("4acc9acc76d5079515b46346a485974457b5a79893cfb01112423c89aeb5aa10", rustup.sha256)

    def test_rejects_every_unsupported_platform(self):
        with self.assertRaisesRegex(MaterializationError, "unsupported"):
            select_build_artifacts(replace(selection(), platform="linux-arm64"))


class TestStreamingMaterializer(unittest.TestCase):
    def selected(self, payload=b"payload"):
        return SelectedBuildArtifact("uv", "https://secret.example/path?token=nope", DigestIdentity.from_hex("sha256", hashlib.sha256(payload).hexdigest()))

    def test_streamed_miss_and_verified_hit(self):
        with tempfile.TemporaryDirectory() as td:
            transport = RecordingTransport((b"pay", b"load"))
            cache = Path(td) / "cache"; cache.mkdir(mode=0o700)
            path = materialize_artifact(self.selected(), constructor_project_root=td, cache_root=cache, transport=transport)
            self.assertEqual(b"payload", path.read_bytes())
            self.assertEqual(0o444, path.stat().st_mode & 0o777)
            bomb = RecordingTransport(error=AssertionError("network on hit"))
            self.assertEqual(path, materialize_artifact(self.selected(), constructor_project_root=td, cache_root=cache, transport=bomb))
            self.assertEqual([], bomb.calls)

    def test_digest_mismatch_and_interruption_clean_temporary_files(self):
        for transport in (RecordingTransport((b"wrong",)), RecordingTransport((b"pay",), MaterializationError("interrupted"))):
            with self.subTest(transport=transport), tempfile.TemporaryDirectory() as td:
                cache = Path(td) / "cache"; cache.mkdir(mode=0o700)
                with self.assertRaises(MaterializationError):
                    materialize_artifact(self.selected(), constructor_project_root=td, cache_root=cache, transport=transport)
                from docker.versioning.project_state import resolve_project_state
                state = resolve_project_state(td, cache_root=cache)
                self.assertEqual([], list((state.build_artifacts_root / "tmp").iterdir()))
                self.assertFalse(any((state.build_artifacts_root / "blobs").rglob("*.blob")))

    def test_atomic_publication_destination_absent_until_complete(self):
        with tempfile.TemporaryDirectory() as td:
            selected = self.selected()
            cache = Path(td) / "cache"; cache.mkdir(mode=0o700)
            from docker.versioning.project_state import resolve_project_state
            state = resolve_project_state(td, cache_root=cache)
            destination = state.build_artifacts_root / "blobs/sha256" / (selected.identity.hex_digest() + ".blob")
            class Observing:
                def stream(self, url):
                    self_outer.assertFalse(destination.exists())
                    yield b"payload"
                    self_outer.assertFalse(destination.exists())
            self_outer = self
            materialize_artifact(selected, constructor_project_root=td, cache_root=cache, transport=Observing())
            self.assertTrue(destination.exists())

    def test_materialization_mutates_only_the_external_namespace(self):
        with tempfile.TemporaryDirectory() as td:
            checkout = Path(td) / "proj"; checkout.mkdir()
            cache = Path(td) / "cache"; cache.mkdir(mode=0o700); os.chmod(cache, 0o700)
            path = materialize_artifact(
                self.selected(), constructor_project_root=checkout, cache_root=cache,
                transport=RecordingTransport((b"pay", b"load")),
            )
            # The project checkout receives no generated entry at all.
            self.assertEqual([], [p.name for p in checkout.iterdir()])
            self.assertFalse((checkout / ".docker-cache").exists())
            self.assertFalse((checkout / ".docker-generated").exists())
            # The blob lives inside the external namespace, never the checkout.
            from docker.versioning.project_state import resolve_project_state
            state = resolve_project_state(checkout, cache_root=cache)
            self.assertTrue(path.is_relative_to(state.namespace))
            self.assertTrue(path.is_relative_to(state.build_artifacts_root))
            self.assertFalse(path.is_relative_to(checkout))

    def test_new_publication_marks_once_and_cache_hit_keeps_timestamp(self):
        with tempfile.TemporaryDirectory() as td:
            checkout = Path(td) / "proj"; checkout.mkdir()
            cache = Path(td) / "cache"; cache.mkdir(mode=0o700)
            selected = self.selected()
            transport = RecordingTransport(chunks=(b"payload",))
            from docker.versioning.build_cache import acquire_constructor_project_build_lock, prepare_build_cache
            with acquire_constructor_project_build_lock(checkout, cache_root=cache) as lock, patch(
                "docker.versioning.build_cache.time.time", return_value=123,
            ):
                materialize_artifact(selected, constructor_project_root=checkout, cache_root=cache,
                                     transport=transport, lock=lock)
            paths = prepare_build_cache(checkout, cache_root=cache)
            marker = paths.markers_root / f"sha256:{selected.identity.hex_digest()}.json"
            self.assertEqual({"verified_at": 123}, __import__("json").loads(marker.read_text()))
            with acquire_constructor_project_build_lock(checkout, cache_root=cache) as lock, patch(
                "docker.versioning.build_cache.time.time", return_value=456,
            ):
                materialize_artifact(selected, constructor_project_root=checkout, cache_root=cache,
                                     transport=RecordingTransport(error=AssertionError()), lock=lock)
            self.assertEqual({"verified_at": 123}, __import__("json").loads(marker.read_text()))

    def test_failed_or_mismatched_download_creates_neither_blob_nor_marker(self):
        with tempfile.TemporaryDirectory() as td:
            checkout = Path(td) / "proj"; checkout.mkdir()
            cache = Path(td) / "cache"; cache.mkdir(mode=0o700)
            selected = self.selected()
            from docker.versioning.build_cache import acquire_constructor_project_build_lock, build_blob_path, prepare_build_cache
            for transport in (RecordingTransport(error=InterruptedError()), RecordingTransport(chunks=(b"wrong",))):
                with acquire_constructor_project_build_lock(checkout, cache_root=cache) as lock:
                    with self.assertRaises(MaterializationError):
                        materialize_artifact(selected, constructor_project_root=checkout, cache_root=cache,
                                             transport=transport, lock=lock)
                paths = prepare_build_cache(checkout, cache_root=cache)
                self.assertFalse(build_blob_path(paths.blobs_root, selected.identity).exists())
                self.assertFalse((paths.markers_root / f"sha256:{selected.identity.hex_digest()}.json").exists())

    def test_external_namespace_verified_hit_reuses_blob_without_download(self):
        with tempfile.TemporaryDirectory() as td:
            checkout = Path(td) / "proj"; checkout.mkdir()
            cache = Path(td) / "cache"; cache.mkdir(mode=0o700); os.chmod(cache, 0o700)
            selected = self.selected()
            from docker.versioning.build_cache import build_blob_path, prepare_build_cache
            paths = prepare_build_cache(checkout, cache_root=cache)
            destination = build_blob_path(paths.blobs_root, selected.identity)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"payload")
            os.chmod(destination, 0o444)
            bomb = RecordingTransport(error=AssertionError("network on verified hit"))
            path = materialize_artifact(
                selected, constructor_project_root=checkout, cache_root=cache, transport=bomb,
            )
            self.assertEqual(destination, path)
            self.assertEqual([], bomb.calls)
            self.assertEqual(b"payload", path.read_bytes())


def _fixture_snapshot(*_args, **_kwargs):
    path = fixture_directory("fixture-snapshot-")
    return MaterializedSnapshot(path, path / "manifest.json")


def setUpModule():
    global _snapshot_patcher
    _snapshot_patcher = patch("docker.versioning.build_orchestration.create_artifact_snapshot", side_effect=_fixture_snapshot)
    _snapshot_patcher.start()


def tearDownModule():
    _snapshot_patcher.stop()


class TestMaterializationOrchestration(unittest.TestCase):
    def test_injected_docker_runner_does_not_skip_materialization(self):
        effects = []
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"; repo.mkdir()
            cache = Path(td) / "cache"; cache.mkdir(mode=0o700)
            inventory = repo / "docker-constructor.toml"
            inventory.write_bytes((ROOT / "docker-constructor.toml").read_bytes())
            (repo / "docker-constructor.local.toml").write_text(
                f'[cache]\ndir = "{cache}"\n'
            )
            (repo / "Dockerfile").write_text("FROM scratch\n")
            def materialize(*args, **kwargs):
                effects.append("materialize")
                return publish_digest_valid_artifacts(*args, **kwargs)
            def publish(*args, **kwargs):
                effects.append("publish")
                return PublishResult("/tmp/effective.toml")
            class Docker:
                def run(self, argv):
                    effects.append("docker")
                    return ProcessResult(argv, 0, "", "")
            with patch(
                "docker.versioning.build_orchestration.select_build_artifacts",
                side_effect=digest_valid_selected_artifacts,
            ):
                result = orchestrate_build(BuildRequest(
                    inventory_path=str(inventory), repo_root=str(repo), project_root=str(repo),
                    confirmed=True, runner=Docker(),
                    _materialize_artifacts=materialize,
                    _named_context_supported=lambda: True,
                    _materialize_pi=fake_pi_materialization,
                    _publish_projection=publish,
                ))
        self.assertEqual(ExitKind.SUCCESS, result.exit_kind)
        self.assertEqual(["materialize", "publish", "docker"], effects)

    def test_disappearing_ca_is_redacted_and_prevents_all_execution_effects(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            inventory = root / "docker-constructor.toml"
            inventory.write_bytes((ROOT / "docker-constructor.toml").read_bytes())
            (root / "docker-constructor.local.toml").write_text(
                "[corporate-trust]\nenabled = true\n"
            )
            (root / "Dockerfile").write_text("FROM scratch\n")
            local = root / ".docker-local"
            local.mkdir()
            bundle = local / "corporate-ca-bundle.crt"
            bundle.write_bytes((ROOT / ".docker-local/corporate-ca-bundle.crt").read_bytes())
            effects = []
            class Docker:
                def run(self, argv):
                    effects.append("docker")
                    raise AssertionError("Docker must not run")
            def materialize(*args, **kwargs):
                effects.append("materialize")
                raise AssertionError("materialization must not run")
            def publish(*args, **kwargs):
                effects.append("publish")
                raise AssertionError("publication must not run")
            request = BuildRequest(
                inventory_path=str(inventory), repo_root=str(root), project_root=str(root), confirmed=True,
                runner=Docker(), _materialize_artifacts=materialize,
                _named_context_supported=lambda: True, _publish_projection=publish,
            )
            plan = plan_build(request)
            self.assertEqual(ExitKind.SUCCESS, plan.exit_kind, plan.message)
            detail = f"private CA vanished at {bundle}"
            with patch(
                "docker.versioning.build_materialization.ssl.create_default_context",
                side_effect=OSError(detail),
            ):
                result = execute_build(plan, request)
            self.assertEqual(ExitKind.OPERATIONAL, result.exit_kind)
            self.assertEqual([], effects)
            self.assertIn("transport configuration failed", result.message or "")
            self.assertNotIn(os.fspath(bundle), result.message or "")
            self.assertNotIn("private CA vanished", result.message or "")

    def test_failure_prevents_publication_and_docker(self):
        effects = []
        class Docker:
            def run(self, argv):
                effects.append("docker")
                raise AssertionError("Docker must not run")
        def fail(*args, **kwargs):
            effects.append("materialize")
            raise MaterializationError("integrity check failed")
        def publish(*args, **kwargs):
            effects.append("publish")
            raise AssertionError("reference must not be published")
        result = orchestrate_build(BuildRequest(
            inventory_path=str(ROOT / "docker-constructor.toml"),
            repo_root=str(ROOT), project_root=str(ROOT), confirmed=True, runner=Docker(),
            _materialize_artifacts=fail, _named_context_supported=lambda: True,
            _materialize_pi=fake_pi_materialization,
            _publish_projection=publish,
        ))
        self.assertEqual(ExitKind.OPERATIONAL, result.exit_kind)
        self.assertEqual(["materialize"], effects)
        self.assertIsNone(result.publish_result)


class TestHostTransportPolicy(unittest.TestCase):
    def test_enabled_credential_free_proxy_is_applied(self):
        transport = UrllibStreamingTransport(HostNetworkPolicy(proxy_url="http://proxy.example:3128"))
        proxy = next(h for h in transport._opener.handlers if h.__class__.__name__ == "ProxyHandler")
        self.assertEqual("http://proxy.example:3128", proxy.proxies["https"])

    def test_disabled_policy_has_no_constructor_overrides(self):
        with patch.dict(os.environ, {}, clear=True):
            transport = UrllibStreamingTransport(HostNetworkPolicy())
        proxies = [h for h in transport._opener.handlers if h.__class__.__name__ == "ProxyHandler"]
        self.assertTrue(not proxies or proxies[0].proxies == {})

    def test_enabled_replacement_ca_is_applied(self):
        bundle = Path("/private/corporate-ca.pem")
        sentinel = object()
        with patch("docker.versioning.build_materialization.ssl.create_default_context", return_value=sentinel) as create:
            with patch("docker.versioning.build_materialization.urllib.request.build_opener") as build:
                UrllibStreamingTransport(HostNetworkPolicy(ca_bundle=bundle))
        create.assert_called_once_with(cafile=os.fspath(bundle))
        self.assertTrue(any(getattr(h, "_context", None) is sentinel for h in build.call_args.args))

    def test_invalid_proxy_policy_fails_before_materialization_or_docker(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            inventory = root / "docker-constructor.toml"
            inventory.write_bytes((ROOT / "docker-constructor.toml").read_bytes())
            (root / "docker-constructor.local.toml").write_text(
                '[network.proxy]\nurl = "http://user:secret@proxy.example:3128"\n'
            )
            effects = []
            result = orchestrate_build(BuildRequest(
                inventory_path=str(inventory), repo_root=str(root), project_root=str(root), confirmed=True,
                runner=type("Docker", (), {"run": lambda self, argv: effects.append("docker")})(),
                _materialize_artifacts=lambda *a, **k: effects.append("network"),
                _materialize_pi=fake_pi_materialization,
            ))
            self.assertEqual(ExitKind.CONFIG, result.exit_kind)
            self.assertEqual([], effects)
            self.assertNotIn("secret", result.message or "")

    def test_transport_diagnostic_redacts_url(self):
        class FailingOpener:
            def open(self, url): raise OSError(f"cannot reach {url}")
        transport = UrllibStreamingTransport()
        transport._opener = FailingOpener()
        with self.assertRaises(MaterializationError) as caught:
            list(transport.stream("https://user:secret@example.test/private?token=x"))
        text = str(caught.exception)
        for secret in ("user", "secret", "example.test", "private", "token"):
            self.assertNotIn(secret, text)

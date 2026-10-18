"""Phase 4 named-context contracts; no daemon, network, or host cache needed."""

from __future__ import annotations

import hashlib
import ntpath
import os
import posixpath
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docker.versioning import build_orchestration
from docker.versioning.build_materialization import SelectedBuildArtifact
from docker.versioning.build_orchestration import (
    BuildRequest,
    orchestrate_build,
    plan_build,
)
from docker.versioning.build_snapshot import (
    MaterializedSnapshot,
    cleanup_artifact_snapshot,
    create_artifact_snapshot,
)
from docker.versioning.digest_identity import DigestIdentity
from docker.versioning.rendering import (
    BuildRenderInputs,
    Materialized,
    NoDerivedEnvironment,
    Prospective,
    is_platform_native_path,
    render_build_vector,
    render_prospective_build_display,
)
from docker.versioning.effective import resolve_build_projection
from docker.versioning.inventory import load_inventory
from tests.build_test_support import fake_pi_materialization
from tests.privilege_helpers import differing_uid_read_status

ROOT = Path(__file__).resolve().parents[1]


def projection():
    return resolve_build_projection(
        load_inventory(ROOT / "docker-constructor.toml").build,
        {},
        platform="linux-amd64",
    )


class TestPhase4Lifecycle(unittest.TestCase):
    def test_private_parent_owner_import_and_immutable_finalization(self):
        with tempfile.TemporaryDirectory() as outer:
            parent = Path(outer) / "private"
            parent.mkdir(0o700)
            checkout = parent / "checkout"
            checkout.mkdir(0o700)
            selected, blobs = [], []
            for name, data in (
                ("rustup", b"r"),
                ("uv", b"u"),
                ("rtk", b"t"),
                ("fd", b"f"),
            ):
                digest = DigestIdentity.from_hex(
                    "sha256", hashlib.sha256(data).hexdigest()
                )
                blob = checkout / f"{name}.blob"
                blob.write_bytes(data)
                blob.chmod(0o444)
                selected.append(
                    SelectedBuildArtifact(name, "https://never.exposed/", digest)
                )
                blobs.append(blob)
            snapshot = create_artifact_snapshot(
                selected, blobs, constructor_project_root=checkout
            )
            self.assertEqual(0o700, parent.stat().st_mode & 0o777)
            self.assertEqual(0o700, checkout.stat().st_mode & 0o777)
            self.assertTrue(snapshot.path.is_dir())  # host owner can import/traverse
            self.assertEqual(0, snapshot.path.stat().st_mode & 0o222)
            self.assertTrue(
                all(p.stat().st_mode & 0o222 == 0 for p in snapshot.path.iterdir())
            )
            payload = snapshot.path / "rustup-init"
            with self.assertRaises(PermissionError):
                with payload.open("wb") as output:
                    output.write(b"mutate")
            blobs[0].unlink()
            self.assertEqual(b"r", payload.read_bytes())
            cleanup_artifact_snapshot(snapshot)
            self.assertFalse(snapshot.path.exists())

    def test_differing_uid_cannot_traverse_private_snapshot(self):
        # Narrowly delegated to sudo runuser: the Python process stays on the
        # invoking user and only the ``test -r`` probe runs as the differing UID.
        with tempfile.TemporaryDirectory() as outer:
            root = Path(outer)
            root.chmod(0o700)
            self.assertNotEqual(0, differing_uid_read_status(root))


class TestPhase4Plans(unittest.TestCase):
    def _inputs(self, context):
        return BuildRenderInputs(
            ".", projection(), "runtime", "image", "linux/amd64", named_context=context
        )

    def test_executable_rejects_prospective_and_accepts_materialized(self):
        with self.assertRaisesRegex(Exception, "materialized"):
            render_build_vector(self._inputs(Prospective()))
        path = os.fspath(Path.cwd() / "native" / "snapshot")
        args = render_build_vector(
            self._inputs(Materialized(path, NoDerivedEnvironment()))
        )
        self.assertIn("--build-context", args)
        self.assertIn(f"constructor-artifacts={path}", args)

    def test_dry_run_shape_display_and_platform_neutral_bytes(self):
        context = Prospective()
        self.assertEqual("constructor-artifacts", context.name)
        self.assertIsNone(context.path)
        self.assertEqual({"state": "prospective"}, context.attestation)
        text = render_prospective_build_display(self._inputs(context))
        self.assertIn("Planned build (not executable)", text)
        self.assertIn(
            "--build-context constructor-artifacts=<prospective:not-materialized>", text
        )
        self.assertNotIn("_URL=", text)
        posix_context = Prospective()
        windows_context = Prospective()
        self.assertIsNone(posix_context.path)
        self.assertIsNone(windows_context.path)
        posix = render_prospective_build_display(
            self._inputs(posix_context), path_semantics=posixpath
        )
        windows = render_prospective_build_display(
            self._inputs(windows_context), path_semantics=ntpath
        )
        self.assertEqual(
            {
                "name": posix_context.name,
                "state": "prospective",
                "path": posix_context.path,
                "attestation": posix_context.attestation,
            },
            {
                "name": windows_context.name,
                "state": "prospective",
                "path": windows_context.path,
                "attestation": windows_context.attestation,
            },
        )
        self.assertEqual(posix.encode(), windows.encode())
        self.assertNotIn("C:\\", posix)
        self.assertNotIn("/tmp/materialized", windows)

    def test_materialized_paths_follow_selected_host_semantics(self):
        self.assertTrue(is_platform_native_path("/var/tmp/snapshot", posixpath))
        self.assertFalse(is_platform_native_path("var/tmp/snapshot", posixpath))
        self.assertTrue(is_platform_native_path(r"C:\\snapshot", ntpath))
        self.assertTrue(is_platform_native_path(r"\\\\host\\share\\snapshot", ntpath))
        self.assertFalse(is_platform_native_path(r"snapshot", ntpath))
        with self.assertRaises(ValueError):
            Materialized("relative/snapshot", NoDerivedEnvironment)

    def test_dry_plan_has_no_argv_or_side_effects(self):
        plan = plan_build(
            BuildRequest(
                inventory_path=str(ROOT / "docker-constructor.toml"),
                repo_root=str(ROOT),
                project_root=str(ROOT),
                dry_run=True,
            )
        )
        self.assertEqual((), plan.build_args)
        self.assertIsInstance(plan.render_inputs.named_context, Prospective)  # type: ignore[union-attr]


class TestPhase4Capability(unittest.TestCase):
    def test_buildx_presence_does_not_substitute_for_build_flag_support(self):
        completed = __import__("subprocess").CompletedProcess(
            ["docker", "build", "--help"],
            0,
            "usage: docker build",
            "",
        )
        with patch(
            "docker.versioning.build_orchestration.subprocess.run",
            return_value=completed,
        ) as run:
            self.assertFalse(build_orchestration._default_named_context_supported())
        self.assertEqual(["docker", "build", "--help"], run.call_args.args[0])

    def test_missing_named_context_fails_before_materialization_or_docker(self):
        effects = []
        result = orchestrate_build(
            BuildRequest(
                inventory_path=str(ROOT / "docker-constructor.toml"),
                repo_root=str(ROOT),
                project_root=str(ROOT),
                confirmed=True,
                _named_context_supported=lambda: False,
                _materialize_artifacts=lambda *_a, **_kw: effects.append("download"),
                _materialize_pi=fake_pi_materialization,
            )
        )
        self.assertEqual([], effects)
        self.assertIn("named-context", result.message or "")


class TestPhase4Cleanup(unittest.TestCase):
    def _snapshot(self, root: Path) -> MaterializedSnapshot:
        path = root / "snapshot"
        path.mkdir()
        return MaterializedSnapshot(path, path / "manifest.json")

    def _request(self, root: Path, *, publish=None, runner=None):
        return BuildRequest(
            inventory_path=str(ROOT / "docker-constructor.toml"),
            repo_root=str(ROOT),
            project_root=str(ROOT),
            confirmed=True,
            _named_context_supported=lambda: True,
            _materialize_artifacts=lambda *_a, **_kw: tuple(
                root / f"{name}.blob" for name in ("rustup", "uv", "rtk", "fd")
            ),
            _materialize_pi=fake_pi_materialization,
            _publish_projection=publish,
            runner=runner,
        )

    def test_cleanup_runs_when_publication_raises_unexpected_exception(self):
        with tempfile.TemporaryDirectory() as td:
            snapshot = self._snapshot(Path(td))
            with patch(
                "docker.versioning.build_orchestration.create_artifact_snapshot",
                return_value=snapshot,
            ):
                result = orchestrate_build(
                    self._request(
                        Path(td),
                        publish=lambda *_a, **_kw: (_ for _ in ()).throw(
                            RuntimeError("boom")
                        ),
                    )
                )
            self.assertIn("boom", result.message or "")
            self.assertFalse(snapshot.path.exists())

    def test_cleanup_runs_when_runner_is_interrupted(self):
        class Interrupted:
            def run(self, _argv):
                raise KeyboardInterrupt()

        with tempfile.TemporaryDirectory() as td:
            snapshot = self._snapshot(Path(td))
            with patch(
                "docker.versioning.build_orchestration.create_artifact_snapshot",
                return_value=snapshot,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    orchestrate_build(
                        self._request(
                            Path(td),
                            publish=lambda *_a, **_kw: object(),
                            runner=Interrupted(),
                        )
                    )
            self.assertFalse(snapshot.path.exists())


class TestPhase4DockerfileContracts(unittest.TestCase):
    def test_prebuilt_stages_use_only_named_context_inputs(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        for filename, arg in (
            ("rustup-init", "RUSTUP"),
            ("uv.tar.gz", "UV"),
            ("rtk.deb", "RTK"),
            ("fd.deb", "FD"),
        ):
            self.assertIn(f"COPY --from=constructor-artifacts", dockerfile)
            self.assertIn(filename, dockerfile)
            self.assertIn(f"ARG {arg}_SHA256", dockerfile)
            self.assertNotIn(f"ARG {arg}_URL", dockerfile)
        for url in ("RTK_URL", "FD_URL", "RUSTUP_URL", "UV_URL"):
            self.assertNotIn(url, dockerfile)

    def test_remapped_dev_reads_only_imported_buildkit_copies(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        # The host cache/snapshot name never appears as a path in a stage; it
        # crosses the UID boundary only through BuildKit's named context.
        self.assertNotIn(".docker-cache", dockerfile)
        self.assertNotIn(".docker-generated/build-artifacts", dockerfile)
        self.assertIn(
            "COPY --from=constructor-artifacts --chown=dev:dev --chmod=0555 rustup-init",
            dockerfile,
        )
        self.assertIn(
            "COPY --from=constructor-artifacts --chown=dev:dev --chmod=0444 uv.tar.gz",
            dockerfile,
        )
        self.assertNotIn("chmod +x /tmp/rustup-init", dockerfile)

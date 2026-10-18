"""Permission, ownership, immutability, and no-repair coverage for external state."""

from __future__ import annotations
import hashlib, os, stat, tempfile, types, unittest
from pathlib import Path
from unittest import mock
from docker.versioning.build_cache import (
    BuildCacheError,
    acquire_constructor_project_build_lock,
    maintain_uncommitted_blobs,
    prepare_build_cache,
    publish_uncommitted_blob,
    publish_verified_blob,
    _open_namespace_fd,
    _verify_published_blob,
)
from docker.versioning.digest_identity import DigestIdentity
from docker.versioning.project_state import resolve_project_state
from tests.privilege_helpers import docker_dev_ids, sudo_chown_tree

_REAL_INVENTORY = (
    Path(__file__).resolve().parents[1] / "docker-constructor.toml"
).read_text()


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def foreign_fstat(target):
    real = os.fstat
    target = str(target)

    def fake(fd, *a, **kw):
        value = real(fd, *a, **kw)
        try:
            name = os.path.normpath(os.readlink(f"/proc/self/fd/{fd}"))
        except OSError:
            name = ""
        return (
            types.SimpleNamespace(
                st_mode=value.st_mode, st_uid=os.geteuid() + 1, st_gid=value.st_gid
            )
            if name == target
            else value
        )

    return fake


def _recording_executor():
    class _Executor:
        def __init__(self):
            self.calls: list[tuple[str, ...]] = []

        def run(self, argv, *, interactive=False):
            self.calls.append(tuple(argv))
            from docker.launcher import ProcessResult

            return ProcessResult(argv=tuple(argv), return_code=0, stdout="", stderr="")

    return _Executor()


def _no_containers_inspector():
    class _Inspector:
        def list_names(self):
            return set()

    return _Inspector()


def _external_projection_factory(projection, *, parent_dir):
    projection_path = Path(parent_dir) / "runtime-projection.toml"
    projection_path.write_text("generated-by-permission-test = true\n")

    class _Handle:
        path = str(projection_path)
        content_hash = "permission-test"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            try:
                projection_path.unlink()
            except FileNotFoundError:
                pass
            return False

    return _Handle()


class ExternalPermissions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.cache = self.base / "cache"
        self.cache.mkdir(mode=0o700)
        self.project = self.base / "project"
        self.workspace = self.base / "workspace"
        self.extra = self.base / "extra"
        for p in (self.project, self.workspace, self.extra):
            p.mkdir()
        self.state = resolve_project_state(self.project, cache_root=self.cache)

    def publish(self, data=b"verified"):
        identity = DigestIdentity.from_hex("sha256", hashlib.sha256(data).hexdigest())
        return identity, publish_verified_blob(
            identity, data, constructor_project_root=self.project, cache_root=self.cache
        )

    def test_missing_namespace_diagnostic_identifies_project_state_namespace(self):
        missing = self.base / "missing-namespace"
        with self.assertRaisesRegex(
            BuildCacheError,
            r"project-state namespace .* does not exist",
        ):
            _open_namespace_fd(missing)

    def test_private_dirs_invoking_user_ownership_and_immutable_blobs(self):
        paths = prepare_build_cache(self.project, cache_root=self.cache)
        _, blob = self.publish()
        for path in (
            self.state.namespace,
            self.state.build_artifacts_root,
            paths.blobs_root,
            paths.tmp_root,
            paths.generated_root,
            paths.markers_root,
        ):
            self.assertEqual(mode(path), 0o700)
            self.assertEqual(path.stat().st_uid, os.geteuid())
        self.assertEqual(mode(blob), 0o444)
        self.assertEqual(blob.stat().st_uid, os.geteuid())
        self.assertEqual(mode(blob) & 0o222, 0)

    def test_exact_0444_blob_denies_ordinary_write_and_truncation(self):
        _, blob = self.publish(b"immutable")
        self.assertEqual(mode(blob), 0o444)
        with self.assertRaises(PermissionError):
            os.open(blob, os.O_WRONLY)
        with self.assertRaises(PermissionError):
            open(blob, "w").close()
        self.assertEqual(blob.read_bytes(), b"immutable")

    def test_symlink_and_foreign_owned_blob_entries_are_rejected(self):
        identity, blob = self.publish()
        paths = prepare_build_cache(self.project, cache_root=self.cache)
        outside = self.base / "outside"
        outside.write_bytes(b"verified")
        blob.unlink()
        blob.symlink_to(outside)
        with self.assertRaises(BuildCacheError):
            _verify_published_blob(identity, paths)
        blob.unlink()
        blob.write_bytes(b"verified")
        os.chmod(blob, 0o444)
        with mock.patch("os.fstat", foreign_fstat(blob)):
            with self.assertRaises(BuildCacheError):
                _verify_published_blob(identity, paths)

    def test_descriptor_relative_revalidation_rejects_replacement_race(self):
        identity, blob = self.publish()
        paths = prepare_build_cache(self.project, cache_root=self.cache)
        outside = self.base / "outside"
        outside.write_bytes(b"verified")
        real_open = os.open
        replaced = False

        def replace_before_open(name, flags, *args, **kwargs):
            nonlocal replaced
            if name == blob.name and not replaced:
                replaced = True
                blob.unlink()
                blob.symlink_to(outside)
            return real_open(name, flags, *args, **kwargs)

        with mock.patch("os.open", side_effect=replace_before_open):
            with self.assertRaises(BuildCacheError):
                _verify_published_blob(identity, paths)
        self.assertTrue(replaced)
        self.assertEqual(outside.read_bytes(), b"verified")

    def test_inaccessible_constructor_project_ancestor_fails_without_repair(self):
        parent = self.base / "blocked"
        parent.mkdir()
        project = parent / "project"
        project.mkdir()
        os.chmod(parent, 0o000)
        self.addCleanup(os.chmod, parent, 0o700)
        with self.assertRaises(BuildCacheError):
            prepare_build_cache(project, cache_root=self.cache)
        self.assertEqual(mode(parent), 0o000)
        self.assertFalse((project / ".docker-cache").exists())
        self.assertFalse((project / ".docker-generated").exists())

    def test_project_workspace_home_and_unrelated_paths_are_never_repaired(self):
        unrelated = self.base / "unrelated"
        unrelated.mkdir(mode=0o751)
        watched = (
            self.base,
            self.project,
            self.workspace,
            self.extra,
            self.cache.parent,
            unrelated,
        )
        before = {p: (mode(p), p.stat().st_uid) for p in watched}
        prepare_build_cache(self.project, cache_root=self.cache)
        self.publish()
        self.assertEqual(before, {p: (mode(p), p.stat().st_uid) for p in watched})
        self.assertFalse((self.project / ".docker-cache").exists())
        self.assertFalse((self.workspace / ".docker-generated").exists())

    def test_foreign_external_namespace_is_rejected_without_any_repair(self):
        before = (mode(self.state.namespace), self.state.namespace.stat().st_uid)
        with mock.patch("os.fstat", foreign_fstat(self.state.namespace)):
            with self.assertRaises(BuildCacheError):
                prepare_build_cache(self.project, cache_root=self.cache)
        self.assertEqual(
            before, (mode(self.state.namespace), self.state.namespace.stat().st_uid)
        )

    def test_permissive_algorithm_dir_rejected_without_repair(self):
        identity, blob = self.publish(b"algorithm-mode")
        paths = prepare_build_cache(self.project, cache_root=self.cache)
        algo_dir = paths.blobs_root / identity.algorithm
        algo_dir.chmod(0o755)
        entries = sorted(p.name for p in algo_dir.iterdir())
        with self.assertRaises(BuildCacheError):
            publish_verified_blob(
                identity,
                b"algorithm-mode",
                constructor_project_root=self.project,
                cache_root=self.cache,
            )
        with self.assertRaises(BuildCacheError):
            _verify_published_blob(identity, paths)
        self.assertEqual(mode(algo_dir), 0o755)  # not repaired
        self.assertEqual(sorted(p.name for p in algo_dir.iterdir()), entries)
        self.assertEqual(blob.read_bytes(), b"algorithm-mode")
        self.assertEqual(mode(blob), 0o444)

    def test_other_invalid_algorithm_modes_rejected_without_repair(self):
        identity, blob = self.publish(b"other-mode")
        paths = prepare_build_cache(self.project, cache_root=self.cache)
        algo_dir = paths.blobs_root / identity.algorithm
        self.addCleanup(os.chmod, algo_dir, 0o700)
        for bad in (0o777, 0o600, 0o711):
            algo_dir.chmod(bad)
            with self.assertRaises(BuildCacheError):
                _verify_published_blob(identity, paths)
            self.assertEqual(mode(algo_dir), bad)
        self.assertEqual(blob.read_bytes(), b"other-mode")

    def test_remove_blob_and_marker_rejects_permissive_algorithm_dir(self):
        identity = DigestIdentity.from_hex(
            "sha256", hashlib.sha256(b"remove-mode").hexdigest()
        )
        paths = prepare_build_cache(self.project, cache_root=self.cache)
        marker = (
            paths.markers_root / f"{identity.algorithm}:{identity.hex_digest()}.json"
        )
        with acquire_constructor_project_build_lock(
            self.project, cache_root=self.cache
        ) as lock:
            blob = publish_uncommitted_blob(
                identity,
                b"remove-mode",
                constructor_project_root=self.project,
                lock=lock,
                verified_at=0,
                cache_root=self.cache,
            )
            algo_dir = paths.blobs_root / identity.algorithm
            algo_dir.chmod(0o755)
            entries = sorted(p.name for p in algo_dir.iterdir())
            marker_before = marker.read_bytes()
            with self.assertRaises(BuildCacheError):
                maintain_uncommitted_blobs(
                    self.project, lock=lock, cache_root=self.cache
                )
            self.assertEqual(sorted(p.name for p in algo_dir.iterdir()), entries)
            self.assertEqual(mode(algo_dir), 0o755)  # not repaired
            self.assertTrue(blob.exists())
            self.assertEqual(blob.read_bytes(), b"remove-mode")
            self.assertEqual(marker.read_bytes(), marker_before)

    def test_foreign_owned_project_still_creates_invoking_user_owned_state(self):
        # Unit coverage for the docker-dev scenario without chown privileges:
        # a foreign-owned constructor project is never chowned/repaired, and
        # external state stays invoking-user-owned with private modes.  This
        # mock is NOT the docker-dev integration scenario (see below).
        real_stat = os.stat
        target = str(self.project.resolve())
        seen: list[bool] = []

        def foreign_project_stat(path, *args, **kwargs):
            value = real_stat(path, *args, **kwargs)
            try:
                key = os.path.abspath(os.fspath(path))
            except TypeError:
                return value
            if key == target:
                seen.append(True)
                return types.SimpleNamespace(
                    st_mode=value.st_mode,
                    st_uid=os.geteuid() + 1,
                    st_gid=value.st_gid,
                    st_ino=value.st_ino,
                    st_dev=value.st_dev,
                    st_size=value.st_size,
                    st_nlink=value.st_nlink,
                    st_mtime=value.st_mtime,
                )
            return value

        with mock.patch("os.stat", side_effect=foreign_project_stat):
            state = resolve_project_state(self.project, cache_root=self.cache)
            paths = prepare_build_cache(self.project, cache_root=self.cache)
        self.assertTrue(seen, "mock must observe the constructor project stat")
        for p in (
            state.namespace,
            state.build_artifacts_root,
            state.generated_root,
            state.runtime_root,
            state.evidence_root,
            state.transactions_root,
            paths.blobs_root,
            paths.tmp_root,
            paths.markers_root,
        ):
            self.assertEqual(p.stat().st_uid, os.geteuid(), p)
            self.assertEqual(mode(p), 0o700, p)
        self.assertFalse((self.project / ".docker-cache").exists())
        self.assertFalse((self.project / ".docker-generated").exists())


class RestrictiveUmaskCreation(unittest.TestCase):
    """Newly created project-state directories are exactly 0700 despite the umask."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def test_new_state_dirs_are_exactly_0700_under_owner_stripping_umask(self):
        project = self.base / "project"
        project.mkdir()
        cache = self.base / "cache"
        cache.mkdir(mode=0o700)
        old = os.umask(0o777)  # strips owner rwx, group, and other permissions
        try:
            state = resolve_project_state(project, cache_root=cache)
            paths = prepare_build_cache(project, cache_root=cache)
        finally:
            os.umask(old)
        for p in (
            cache / "projects",
            state.namespace,
            state.build_artifacts_root,
            state.generated_root,
            state.runtime_root,
            state.evidence_root,
            state.transactions_root,
            paths.blobs_root,
            paths.tmp_root,
            paths.markers_root,
        ):
            self.assertEqual(mode(p), 0o700, p)
            self.assertEqual(p.stat().st_uid, os.geteuid(), p)
        # Metadata is immutable and owner-only regardless of the umask.
        self.assertEqual(mode(state.namespace / "project.json"), 0o600)


class ConstructorProjectForeignOwnership(unittest.TestCase):
    """Real docker-dev ownership transfer; external state stays private."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.work = self.base / "work"
        self.work.mkdir(mode=0o755)
        self.project = self.work / "constructor"
        self.project.mkdir()
        self.primary = self.work / "primary-workspace"
        self.primary.mkdir()
        self.extra = self.work / "extra-workspace"
        self.extra.mkdir()
        self.cache = self.base / "cache"
        self.cache.mkdir(mode=0o700)

    def _docker_dev_ids(self):
        return docker_dev_ids()

    def test_foreign_owned_constructor_project_keeps_external_state_private(self):
        uid, gid = self._docker_dev_ids()
        # Capture the original invoking-user identity before ownership transfer
        # so cleanup can restore it after the test, even on assertion failure.
        invoking_uid = os.geteuid()
        invoking_gid = os.getegid()
        inventory = self.project / "docker-constructor.toml"
        inventory.write_text(_REAL_INVENTORY)
        # Grant the invoking user read + traverse before ownership transfer.
        os.chmod(self.project, 0o755)
        for root, dirs, files in os.walk(self.project):
            for name in dirs:
                os.chmod(os.path.join(root, name), 0o755)
            for name in files:
                os.chmod(os.path.join(root, name), 0o644)
        # Namespace creation and import run below as the invoking user; only
        # the project ownership transfer is delegated to sudo. Register the
        # ownership-restoring cleanup before the transfer so it still runs if a
        # later assertion fails (addCleanup is LIFO, so this runs before the
        # TemporaryDirectory cleanup).
        self.addCleanup(sudo_chown_tree, self.project, invoking_uid, invoking_gid)
        sudo_chown_tree(self.project, uid, gid)

        watched = (self.work, self.project, self.primary, self.extra)
        before = {
            p: (p.stat().st_uid, p.stat().st_gid, stat.S_IMODE(p.stat().st_mode))
            for p in watched
        }

        from docker.launcher import WorkspaceSelection, RunRequest, orchestrate_run
        from docker.versioning.build_orchestration import ExitKind

        executor = _recording_executor()
        with mock.patch(
            "docker.launcher.artifact_cache.materialize_selected_artifacts",
            return_value={},
        ):
            result = orchestrate_run(
                RunRequest(
                    inventory_path=str(inventory),
                    image="pi-cli-pi:latest",
                    selection=WorkspaceSelection(
                        workspace=str(self.project),
                        extra_workspaces=(str(self.primary), str(self.extra)),
                    ),
                    pi_home_host="/home/user/.pi",
                    repo_root=str(self.project),
                    dry_run=False,
                    executor=executor,
                    inspector=_no_containers_inspector(),
                    _create_projection=_external_projection_factory,
                    _constructor_cache_root=str(self.cache),
                    project_root=Path(str(inventory)).resolve().parent,
                )
            )
        self.assertEqual(
            ExitKind.SUCCESS, result.exit_kind, getattr(result, "message", "")
        )
        self.assertEqual(len(executor.calls), 1)

        state = resolve_project_state(self.project, cache_root=self.cache)
        # External project state is invoking-user-owned with private modes.
        self.assertEqual(state.namespace.stat().st_uid, os.geteuid())
        self.assertEqual(mode(state.namespace), 0o700)
        self.assertEqual(mode(state.namespace / "project.json"), 0o600)
        for path in (
            state.build_artifacts_root,
            state.generated_root,
            state.runtime_root,
            state.evidence_root,
            state.transactions_root,
        ):
            self.assertEqual(path.stat().st_uid, os.geteuid(), path)
            self.assertEqual(mode(path), 0o700, path)

        # Exactly one namespace (the constructor project's); none for workspaces.
        projects = self.cache / "projects"
        self.assertEqual(
            sorted(p.name for p in projects.iterdir()), [state.namespace.name]
        )

        # No checkout-local state in the constructor project or either workspace.
        for root in (self.project, self.primary, self.extra):
            self.assertFalse((root / ".docker-cache").exists(), root)
            self.assertFalse((root / ".docker-generated").exists(), root)
        self.assertEqual(
            sorted(p.name for p in self.project.iterdir()), ["docker-constructor.toml"]
        )
        self.assertEqual(list(self.primary.iterdir()), [])
        self.assertEqual(list(self.extra.iterdir()), [])

        # Ownership and modes of the project, workspaces, and ancestors unchanged.
        after = {
            p: (p.stat().st_uid, p.stat().st_gid, stat.S_IMODE(p.stat().st_mode))
            for p in watched
        }
        self.assertEqual(before, after)

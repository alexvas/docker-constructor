from __future__ import annotations

import errno
import hashlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docker.versioning.build_materialization import SelectedBuildArtifact, materialize_artifact
from docker.versioning.build_snapshot import (
    SnapshotError,
    cleanup_artifact_snapshot,
    create_artifact_snapshot,
)
from docker.versioning import build_snapshot as build_snapshot_module
from docker.versioning.digest_identity import DigestIdentity
from tests.privilege_helpers import docker_dev_ids, sudo_chown, sudo_maintain_tree


class TestArtifactSnapshot(unittest.TestCase):
    def _selected(self, root: Path):
        result = []
        for name, payload in (("rustup", b"r"), ("uv", b"u"), ("rtk", b"t"), ("fd", b"f")):
            blob = root / f"{name}.blob"
            blob.write_bytes(payload)
            blob.chmod(0o444)
            result.append((SelectedBuildArtifact(name, "https://not-exposed.invalid/", DigestIdentity.from_hex("sha256", hashlib.sha256(payload).hexdigest())), blob))
        return result

    def test_canonical_manifest_stable_names_and_narrow_exposure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pairs = self._selected(root)
            one = create_artifact_snapshot((x[0] for x in pairs), (x[1] for x in pairs), constructor_project_root=root)
            two = create_artifact_snapshot((x[0] for x in pairs), (x[1] for x in pairs), constructor_project_root=root)
            self.assertEqual(one.manifest.read_bytes(), two.manifest.read_bytes())
            self.assertEqual({"rustup-init", "uv.tar.gz", "rtk.deb", "fd.deb", "manifest.json"}, {p.name for p in one.path.iterdir()})
            self.assertNotIn(".docker-cache", one.manifest.read_text())
            cleanup_artifact_snapshot(one); cleanup_artifact_snapshot(two)

    def test_prefers_hardlink_and_survives_cache_unlink(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); pairs = self._selected(root)
            snapshot = create_artifact_snapshot((x[0] for x in pairs), (x[1] for x in pairs), constructor_project_root=root)
            source = pairs[0][1]; imported = snapshot.path / "rustup-init"
            self.assertEqual(source.stat().st_ino, imported.stat().st_ino)
            source.unlink()
            self.assertEqual(b"r", imported.read_bytes())
            cleanup_artifact_snapshot(snapshot)

    def test_copy_fallback_rechecks_digest(self):
        # Resolve the external namespace before making snapshot hard links fail;
        # project metadata publication itself uses a no-clobber hard link.
        # EXDEV is the one hard-link failure that may fall back to copying; the
        # copied payload must be descriptor-valid and the source left untouched.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); pairs = self._selected(root)
            from docker.versioning.project_state import resolve_project_state
            cache = root / "cache"; cache.mkdir(mode=0o700)
            state = resolve_project_state(root, cache_root=cache)
            source = pairs[0][1]
            content = source.read_bytes()
            before = (source.stat().st_uid, source.stat().st_mode & 0o777,
                      source.stat().st_ino, content, hashlib.sha256(content).hexdigest())
            with patch("docker.versioning.build_snapshot.os.link", side_effect=OSError(errno.EXDEV, "cross-device")):
                snapshot = create_artifact_snapshot((x[0] for x in pairs), (x[1] for x in pairs), constructor_project_root=root, project_state=state)
            imported = snapshot.path / "rustup-init"
            self.assertNotEqual(source.stat().st_ino, imported.stat().st_ino)
            self.assertTrue(imported.is_file())
            self.assertEqual(0o444, imported.stat().st_mode & 0o777)
            self.assertEqual(content, imported.read_bytes())
            self.assertEqual(hashlib.sha256(imported.read_bytes()).hexdigest(), before[4])
            st = source.stat()
            self.assertEqual(st.st_uid, before[0])
            self.assertEqual(st.st_mode & 0o777, before[1])
            self.assertEqual(st.st_ino, before[2])
            self.assertEqual(source.read_bytes(), before[3])
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before[4])
            cleanup_artifact_snapshot(snapshot)

    def test_copy_fallback_payloads_are_finalized_to_0444(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); pairs = self._selected(root)
            from docker.versioning.project_state import resolve_project_state
            cache = root / "cache"; cache.mkdir(mode=0o700)
            state = resolve_project_state(root, cache_root=cache)
            with patch("docker.versioning.build_snapshot.os.link", side_effect=OSError(errno.EXDEV, "cross-device")):
                snapshot = create_artifact_snapshot((x[0] for x in pairs), (x[1] for x in pairs), constructor_project_root=root, project_state=state)
            for name in ("rustup-init", "uv.tar.gz", "rtk.deb", "fd.deb", "manifest.json"):
                self.assertEqual(0o444, (snapshot.path / name).stat().st_mode & 0o777, name)
            cleanup_artifact_snapshot(snapshot)

    def test_hard_linked_payloads_are_never_chmodded_during_finalization_or_cleanup(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); pairs = self._selected(root)
            from docker.versioning.project_state import resolve_project_state
            cache = root / "cache"; cache.mkdir(mode=0o700)
            state = resolve_project_state(root, cache_root=cache)
            before = {blob: (blob.stat().st_uid, blob.stat().st_mode, blob.stat().st_ino, blob.read_bytes())
                      for _, blob in pairs}
            real_chmod = os.chmod
            calls: list[str] = []
            def spy(path, mode, *args, **kwargs):
                calls.append(os.fspath(path))
                return real_chmod(path, mode, *args, **kwargs)
            with patch("docker.versioning.build_snapshot.os.chmod", side_effect=spy):
                snapshot = create_artifact_snapshot((x[0] for x in pairs), (x[1] for x in pairs), constructor_project_root=root, project_state=state)
                cleanup_artifact_snapshot(snapshot)
            hard_linked = {"rustup-init", "uv.tar.gz", "rtk.deb", "fd.deb"}
            for call in calls:
                name = os.path.basename(call)
                self.assertNotIn(name, hard_linked, f"chmod must never touch hard-linked payload {name}")
            # Blob owner, mode, inode, content, and digest remain unchanged.
            for _, blob in pairs:
                st = blob.stat()
                self.assertEqual(st.st_uid, before[blob][0])
                self.assertEqual(st.st_mode, before[blob][1])
                self.assertEqual(st.st_ino, before[blob][2])
                self.assertEqual(blob.read_bytes(), before[blob][3])

    def test_nested_file_sharing_hard_link_basename_is_still_finalized(self):
        # A nested snapshot-owned file whose basename collides with a hard-linked
        # payload must still be finalized to 0444: only the exact root-relative
        # hard-linked path is excluded from chmod, never the basename alone.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); pairs = self._selected(root)
            from docker.versioning.project_state import resolve_project_state
            cache = root / "cache"; cache.mkdir(mode=0o700)
            state = resolve_project_state(root, cache_root=cache)
            real_mkdtemp = tempfile.mkdtemp
            def mkdtemp_with_nested(prefix, dir):
                staging = real_mkdtemp(prefix=prefix, dir=dir)
                nested = Path(staging) / "nested"
                nested.mkdir()
                (nested / "rustup-init").write_bytes(b"nested-own-payload")
                return staging
            with patch("docker.versioning.build_snapshot.tempfile.mkdtemp", side_effect=mkdtemp_with_nested):
                snapshot = create_artifact_snapshot(
                    (x[0] for x in pairs), (x[1] for x in pairs), constructor_project_root=root, project_state=state)
            nested = snapshot.path / "nested" / "rustup-init"
            linked = snapshot.path / "rustup-init"
            self.assertEqual(0o444, nested.stat().st_mode & 0o777)
            self.assertEqual(0, nested.stat().st_mode & 0o222)
            # Only the exact hard-linked relative path shares the blob inode.
            self.assertEqual(pairs[0][1].stat().st_ino, linked.stat().st_ino)
            self.assertNotEqual(pairs[0][1].stat().st_ino, nested.stat().st_ino)
            self.assertEqual(0o444, linked.stat().st_mode & 0o777)
            cleanup_artifact_snapshot(snapshot)

    def test_unsafe_mode_hard_link_is_rejected_without_repairing_source(self):
        # A readable source that is not already 0444 must be rejected after a
        # successful os.link(), never silently chmodded on the shared inode.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); pairs = self._selected(root)
            from docker.versioning.project_state import resolve_project_state
            cache = root / "cache"; cache.mkdir(mode=0o700)
            state = resolve_project_state(root, cache_root=cache)
            unsafe = pairs[0][1]
            unsafe.chmod(0o644)
            with self.assertRaises(SnapshotError):
                create_artifact_snapshot(
                    (x[0] for x in pairs), (x[1] for x in pairs), constructor_project_root=root, project_state=state)
            self.assertEqual(0o644, unsafe.stat().st_mode & 0o777)

    def test_foreign_owner_hard_link_is_rejected_without_adoption(self):
        # A readable foreign-owned source is rejected during source validation,
        # before any link or copy; it is never adopted, chmodded, or repaired.
        # os.link is only ever attempted for invoking-user-owned sources, so no
        # EPERM patch is required: validation rejects the foreign blob first.
        # Only the ownership transfer to docker-dev is delegated to sudo; the
        # snapshot validation itself runs as the invoking user.
        uid, gid = docker_dev_ids()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); pairs = self._selected(root)
            from docker.versioning.project_state import resolve_project_state
            cache = root / "cache"; cache.mkdir(mode=0o700)
            state = resolve_project_state(root, cache_root=cache)
            foreign = pairs[0][1]
            foreign.chmod(0o444)
            sudo_chown(foreign, uid, gid)
            content = foreign.read_bytes()
            before = (foreign.stat().st_uid, foreign.stat().st_mode & 0o777,
                      foreign.stat().st_ino, content, hashlib.sha256(content).hexdigest())
            real_link = os.link
            linked_sources: list[str] = []
            def spy_link(src, dst, *args, **kwargs):
                linked_sources.append(os.fspath(src))
                return real_link(src, dst, *args, **kwargs)
            with patch("docker.versioning.build_snapshot.os.link", side_effect=spy_link), \
                 patch("docker.versioning.build_snapshot._copy_source_fd_to_destination") as copy:
                with self.assertRaises(SnapshotError):
                    create_artifact_snapshot(
                        (x[0] for x in pairs), (x[1] for x in pairs), constructor_project_root=root, project_state=state)
            copy.assert_not_called()
            self.assertNotIn(os.fspath(foreign), linked_sources)
            st = foreign.stat()
            self.assertEqual(st.st_uid, before[0])
            self.assertEqual(st.st_mode & 0o777, before[1])
            self.assertEqual(st.st_ino, before[2])
            self.assertEqual(foreign.read_bytes(), before[3])
            self.assertEqual(hashlib.sha256(foreign.read_bytes()).hexdigest(), before[4])

    def test_protected_hardlink_eperm_fails_closed_without_copy(self):
        # A valid source on a host that rejects hard links with EPERM (a
        # protected-hardlink policy) must fail closed and never fall back to
        # copying.  This runs unprivileged on every host.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); pairs = self._selected(root)
            from docker.versioning.project_state import resolve_project_state
            cache = root / "cache"; cache.mkdir(mode=0o700)
            state = resolve_project_state(root, cache_root=cache)
            before = {blob: (blob.stat().st_uid, blob.stat().st_mode & 0o777, blob.stat().st_ino, blob.read_bytes())
                      for _, blob in pairs}
            with patch("docker.versioning.build_snapshot.os.link", side_effect=OSError(errno.EPERM, "Operation not permitted")), \
                 patch("docker.versioning.build_snapshot._copy_source_fd_to_destination") as copy:
                with self.assertRaises(SnapshotError):
                    create_artifact_snapshot(
                        (x[0] for x in pairs), (x[1] for x in pairs), constructor_project_root=root, project_state=state)
            copy.assert_not_called()
            for _, blob in pairs:
                st = blob.stat()
                self.assertEqual(st.st_uid, before[blob][0])
                self.assertEqual(st.st_mode & 0o777, before[blob][1])
                self.assertEqual(st.st_ino, before[blob][2])
                self.assertEqual(blob.read_bytes(), before[blob][3])

    def test_source_swap_between_validation_and_link_is_rejected(self):
        # A pathname replacement between source validation and os.link() must
        # be rejected by inode identity comparison, even when the replacement
        # is a safe invoking-user-owned 0444 regular file.  The originally
        # validated inode and the replacement must both survive untouched.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); pairs = self._selected(root)
            from docker.versioning.project_state import resolve_project_state
            cache = root / "cache"; cache.mkdir(mode=0o700)
            state = resolve_project_state(root, cache_root=cache)
            source = pairs[0][1]  # rustup blob, processed after fd and rtk
            original_content = source.read_bytes()
            original = (source.stat().st_dev, source.stat().st_ino,
                        source.stat().st_uid, source.stat().st_mode & 0o777)
            real_link = os.link
            swapped = source.with_name(source.name + ".swapped")
            replacement_inode: list[tuple[int, int]] = []

            def swap_then_link(src, dst, *args, **kwargs):
                src_path = Path(os.fspath(src))
                if src_path == source:
                    os.rename(src_path, swapped)
                    src_path.write_bytes(b"replacement-not-accepted")
                    src_path.chmod(0o444)
                    st = src_path.stat()
                    replacement_inode.append((st.st_dev, st.st_ino))
                return real_link(src, dst, *args, **kwargs)

            with patch("docker.versioning.build_snapshot.os.link", side_effect=swap_then_link):
                with self.assertRaises(SnapshotError):
                    create_artifact_snapshot(
                        (x[0] for x in pairs), (x[1] for x in pairs),
                        constructor_project_root=root, project_state=state)
            # No transaction snapshot remains in the external namespace.
            self.assertEqual([], list(state.transactions_root.glob("transaction-*")))
            # The originally validated inode survives, untouched, under its
            # renamed pathname.
            self.assertTrue(swapped.exists())
            self.assertEqual(original_content, swapped.read_bytes())
            st = swapped.stat()
            self.assertEqual((st.st_dev, st.st_ino), original[:2])
            self.assertEqual(st.st_uid, original[2])
            self.assertEqual(st.st_mode & 0o777, original[3])
            # The replacement was not accepted, deleted, or mutated.
            self.assertEqual(b"replacement-not-accepted", source.read_bytes())
            st = source.stat()
            self.assertEqual((st.st_dev, st.st_ino), replacement_inode[0])
            self.assertEqual(st.st_uid, os.geteuid())
            self.assertEqual(st.st_mode & 0o777, 0o444)

    def test_post_finalization_validation_failure_uses_permission_aware_cleanup(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); pairs = self._selected(root)
            with patch("docker.versioning.build_snapshot.validate_host_owner_traversal", side_effect=RuntimeError("traversal failed")):
                with self.assertRaisesRegex(RuntimeError, "traversal failed"):
                    create_artifact_snapshot((x[0] for x in pairs), (x[1] for x in pairs), constructor_project_root=root)
            generated = root / ".docker-generated/build-artifacts"
            self.assertEqual([], list(generated.glob("transaction-*")))

    def test_finalized_files_and_directories_are_not_writable_and_cleanup_is_unconditional(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); pairs = self._selected(root)
            snapshot = create_artifact_snapshot((x[0] for x in pairs), (x[1] for x in pairs), constructor_project_root=root)
            self.assertEqual(0o444, (snapshot.path / "fd.deb").stat().st_mode & 0o777)
            self.assertEqual(0, snapshot.path.stat().st_mode & 0o222)
            cleanup_artifact_snapshot(snapshot)
            self.assertFalse(snapshot.path.exists())
            # Snapshot payloads are hard links when possible. Cleanup must not
            # chmod those links because that would mutate the cached inode and
            # make the next build report an unsafe/corrupt cache hit.
            self.assertTrue(all((blob.stat().st_mode & 0o777) == 0o444 for _, blob in pairs))


class SnapshotCleanupRecoveryFaultTests(unittest.TestCase):
    """Fault injection for cleanup's permission-recovery branch.

    These tests call the real :func:`cleanup_artifact_snapshot` on a real
    readonly snapshot tree so the initial ``shutil.rmtree`` raises
    ``PermissionError`` and the domain-owned recovery path actually runs.  No
    test replaces the whole cleanup helper.
    """

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._temp.name)
        self.addCleanup(self._temp.cleanup)

    def _readonly_tree(self, *, nested_file: bool = True):
        tree = self.root / "transaction-readonly"
        sub = tree / "sub"
        sub.mkdir(parents=True)
        blob = self.root / "cached-blob"
        blob.write_bytes(b"payload")
        blob.chmod(0o444)
        os.link(blob, tree / "payload")
        manifest = tree / "manifest.json"
        manifest.write_bytes(b"{}")
        manifest.chmod(0o444)
        if nested_file:
            nested = sub / "nested"
            nested.write_bytes(b"nested")
            nested.chmod(0o444)
        sub.chmod(0o555)
        tree.chmod(0o555)
        self.addCleanup(self._release_readonly, tree)
        return tree, blob

    def _release_readonly(self, tree: Path) -> None:
        for entry in sorted(tree.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if entry.is_dir() and not entry.is_symlink():
                try:
                    os.chmod(entry, 0o700)
                except OSError:
                    pass
        try:
            os.chmod(tree, 0o700)
        except OSError:
            pass
        shutil.rmtree(tree, ignore_errors=True)

    def test_recovery_removes_readonly_tree_and_keeps_hard_link_mode(self):
        tree, blob = self._readonly_tree()
        before = (blob.stat().st_ino, blob.stat().st_mode & 0o777, blob.read_bytes())
        removed: list[Path] = []
        real_rmtree = shutil.rmtree

        def spy(path, *args, **kwargs):
            removed.append(Path(path))
            return real_rmtree(path, *args, **kwargs)

        with patch("docker.versioning.build_snapshot.shutil.rmtree", side_effect=spy):
            cleanup_artifact_snapshot(tree)
        # The initial attempt fails and the explicit recovery removal succeeds.
        self.assertEqual(2, len(removed))
        self.assertFalse(tree.exists())
        self.assertEqual(before[0], blob.stat().st_ino)
        self.assertEqual(before[1], blob.stat().st_mode & 0o777)
        self.assertEqual(before[2], blob.read_bytes())

    def test_directory_chmod_permission_failure_stays_observable(self):
        # An empty readonly child can still be removed once the parent is
        # writable, so the injected chmod failure is the only ordinary failure;
        # it must not be discarded just because a later action succeeded.
        tree, _ = self._readonly_tree(nested_file=False)
        target = tree / "sub"
        fault = PermissionError(errno.EACCES, "chmod denied")
        real_chmod = os.chmod
        attempted: list[Path] = []

        def chmod_spy(path, mode, *args, **kwargs):
            attempted.append(Path(path))
            if Path(path) == target:
                raise fault
            return real_chmod(path, mode, *args, **kwargs)

        with patch("docker.versioning.build_snapshot.os.chmod", side_effect=chmod_spy):
            with self.assertRaises(SnapshotError) as ctx:
                cleanup_artifact_snapshot(tree)
        self.assertFalse(tree.exists())
        self.assertIn(tree, attempted)
        cause = ctx.exception.__cause__
        self.assertIsInstance(cause, PermissionError)
        self.assertIn(fault, getattr(cause, "_transaction_secondary", []))

    def test_directory_chmod_io_failure_stays_observable(self):
        tree, _ = self._readonly_tree(nested_file=False)
        target = tree / "sub"
        fault = OSError(errno.EIO, "chmod io failure")
        real_chmod = os.chmod

        def chmod_spy(path, mode, *args, **kwargs):
            if Path(path) == target:
                raise fault
            return real_chmod(path, mode, *args, **kwargs)

        with patch("docker.versioning.build_snapshot.os.chmod", side_effect=chmod_spy):
            with self.assertRaises(SnapshotError) as ctx:
                cleanup_artifact_snapshot(tree)
        cause = ctx.exception.__cause__
        self.assertIn(fault, getattr(cause, "_transaction_secondary", []))

    def test_directory_chmod_unexpected_defect_is_authoritative(self):
        tree, _ = self._readonly_tree(nested_file=False)
        target = tree / "sub"
        defect = RuntimeError("chmod defect")
        real_chmod = os.chmod
        attempted: list[Path] = []

        def chmod_spy(path, mode, *args, **kwargs):
            attempted.append(Path(path))
            if Path(path) == target:
                raise defect
            return real_chmod(path, mode, *args, **kwargs)

        with patch("docker.versioning.build_snapshot.os.chmod", side_effect=chmod_spy):
            with self.assertRaises(RuntimeError) as ctx:
                cleanup_artifact_snapshot(tree)
        self.assertIs(ctx.exception, defect)
        # The remaining chmod and the final removal still ran.
        self.assertIn(tree, attempted)
        self.assertFalse(tree.exists())
        secondary = getattr(defect, "_transaction_secondary", [])
        self.assertTrue(any(isinstance(item, PermissionError) for item in secondary))

    def test_directory_chmod_interruption_is_authoritative_and_cleanup_continues(self):
        tree, _ = self._readonly_tree(nested_file=False)
        target = tree / "sub"
        interruption = KeyboardInterrupt("chmod interrupted")
        real_chmod = os.chmod
        attempted: list[Path] = []

        def chmod_spy(path, mode, *args, **kwargs):
            attempted.append(Path(path))
            if Path(path) == target:
                raise interruption
            return real_chmod(path, mode, *args, **kwargs)

        with patch("docker.versioning.build_snapshot.os.chmod", side_effect=chmod_spy):
            with self.assertRaises(KeyboardInterrupt) as ctx:
                cleanup_artifact_snapshot(tree)
        self.assertIs(ctx.exception, interruption)
        self.assertIn(tree, attempted)
        self.assertFalse(tree.exists())

    def test_final_removal_failure_is_observable(self):
        tree, _ = self._readonly_tree()
        removal_failure = OSError(errno.EIO, "final removal failed")
        real_rmtree = shutil.rmtree
        calls: list[Path] = []

        def spy(path, *args, **kwargs):
            calls.append(Path(path))
            if len(calls) == 2:
                raise removal_failure
            return real_rmtree(path, *args, **kwargs)

        with patch("docker.versioning.build_snapshot.shutil.rmtree", side_effect=spy):
            with self.assertRaises(SnapshotError) as ctx:
                cleanup_artifact_snapshot(tree)
        self.assertTrue(tree.exists())
        cause = ctx.exception.__cause__
        self.assertIsInstance(cause, PermissionError)
        self.assertIn(removal_failure, getattr(cause, "_transaction_secondary", []))

    def test_final_removal_idempotent_absence_is_tolerated(self):
        tree, _ = self._readonly_tree()
        real_rmtree = shutil.rmtree
        calls: list[Path] = []

        def spy(path, *args, **kwargs):
            calls.append(Path(path))
            if len(calls) == 2:
                real_rmtree(path, *args, **kwargs)
                raise FileNotFoundError(
                    errno.ENOENT, "snapshot already removed", os.fspath(path))
            return real_rmtree(path, *args, **kwargs)

        with patch("docker.versioning.build_snapshot.shutil.rmtree", side_effect=spy):
            cleanup_artifact_snapshot(tree)
        self.assertFalse(tree.exists())

    def test_multiple_recovery_failures_are_all_retained(self):
        tree, _ = self._readonly_tree()
        chmod_failure = OSError(errno.EIO, "chmod top denied")
        removal_failure = OSError(errno.EIO, "removal failed")
        real_chmod = os.chmod
        real_rmtree = shutil.rmtree
        calls: list[Path] = []

        def chmod_spy(path, mode, *args, **kwargs):
            if Path(path) == tree:
                raise chmod_failure
            return real_chmod(path, mode, *args, **kwargs)

        def rmtree_spy(path, *args, **kwargs):
            calls.append(Path(path))
            if len(calls) == 2:
                raise removal_failure
            return real_rmtree(path, *args, **kwargs)

        with patch("docker.versioning.build_snapshot.os.chmod", side_effect=chmod_spy), \
                patch("docker.versioning.build_snapshot.shutil.rmtree", side_effect=rmtree_spy):
            with self.assertRaises(SnapshotError) as ctx:
                cleanup_artifact_snapshot(tree)
        cause = ctx.exception.__cause__
        self.assertIsInstance(cause, PermissionError)
        secondary = getattr(cause, "_transaction_secondary", [])
        self.assertIn(chmod_failure, secondary)
        self.assertIn(removal_failure, secondary)

    def test_initial_non_permission_removal_failure_is_exposed_raw(self):
        tree, _ = self._readonly_tree()
        failure = OSError(errno.EIO, "initial removal io failure")
        real_rmtree = shutil.rmtree
        calls: list[Path] = []

        def spy(path, *args, **kwargs):
            calls.append(Path(path))
            raise failure

        with patch("docker.versioning.build_snapshot.shutil.rmtree", side_effect=spy):
            with self.assertRaises(OSError) as ctx:
                cleanup_artifact_snapshot(tree)
        self.assertIs(ctx.exception, failure)
        self.assertNotIsInstance(ctx.exception, SnapshotError)
        # A non-permission failure does not trigger directory recovery.
        self.assertEqual(1, len(calls))

    def test_recovery_chmods_only_snapshot_directories(self):
        tree, blob = self._readonly_tree()
        inode_before = blob.stat().st_ino
        attempted: list[Path] = []
        real_chmod = os.chmod

        def chmod_spy(path, mode, *args, **kwargs):
            attempted.append(Path(path))
            return real_chmod(path, mode, *args, **kwargs)

        with patch("docker.versioning.build_snapshot.os.chmod", side_effect=chmod_spy):
            cleanup_artifact_snapshot(tree)
        self.assertEqual({tree, tree / "sub"}, set(attempted))
        self.assertEqual(0o444, blob.stat().st_mode & 0o777)
        self.assertEqual(inode_before, blob.stat().st_ino)

    def _force_remove(self, path: Path) -> None:
        # Uses os.walk rather than Path.rglob so it is safe to call while
        # ``Path.rglob`` is patched by a test.
        for dirpath, dirnames, _filenames in os.walk(path, topdown=False):
            for name in dirnames:
                os.chmod(Path(dirpath) / name, 0o700)
        os.chmod(path, 0o700)
        shutil.rmtree(path)

    def test_vanished_snapshot_root_before_chmod_is_idempotent_absence(self):
        # A concurrent cleanup removes the whole snapshot after enumeration but
        # before any permission restoration.  Both the root chmod and the final
        # removal then report absence, which is an accepted cleanup outcome.
        tree, blob = self._readonly_tree()
        real_collect = build_snapshot_module._collect_snapshot_directories

        def vanishing_collector(path, discovered):
            real_collect(path, discovered)
            self._force_remove(path)

        with patch(
            "docker.versioning.build_snapshot._collect_snapshot_directories",
            side_effect=vanishing_collector,
        ):
            cleanup_artifact_snapshot(tree)
        self.assertFalse(tree.exists())
        self.assertEqual(0o444, blob.stat().st_mode & 0o777)

    def test_vanished_child_directory_before_chmod_is_idempotent_absence(self):
        # Only the discovered child vanishes; the root remains and is removed.
        tree, blob = self._readonly_tree(nested_file=False)
        real_collect = build_snapshot_module._collect_snapshot_directories

        def vanishing_child_collector(path, discovered):
            real_collect(path, discovered)
            os.chmod(path, 0o700)
            os.rmdir(discovered[0])

        with patch(
            "docker.versioning.build_snapshot._collect_snapshot_directories",
            side_effect=vanishing_child_collector,
        ):
            cleanup_artifact_snapshot(tree)
        self.assertFalse(tree.exists())
        self.assertEqual(0o444, blob.stat().st_mode & 0o777)

    def test_enumeration_absence_is_idempotent(self):
        # A concurrent cleanup removes the snapshot and enumeration observes
        # the vanished entry.  Absence is accepted and the later root chmod and
        # final removal also report absence without surfacing a domain error.
        tree, blob = self._readonly_tree()

        def absent_rglob(entry_path, pattern):
            self._force_remove(tree)
            raise FileNotFoundError(errno.ENOENT, "entry vanished", os.fspath(entry_path))

        with patch.object(Path, "rglob", absent_rglob):
            cleanup_artifact_snapshot(tree)
        self.assertFalse(tree.exists())
        self.assertEqual(0o444, blob.stat().st_mode & 0o777)

    def test_enumeration_defect_is_authoritative_and_cleanup_continues(self):
        # Enumeration yields one directory and then raises an unexpected defect;
        # the discovered directory, the root, and the final removal still run.
        tree, blob = self._readonly_tree()
        defect = RuntimeError("enumeration defect")
        chmodded: list[Path] = []
        real_chmod = os.chmod

        def rglob_spy(self, pattern):
            def iterator():
                yield tree / "sub"
                raise defect

            return iterator()

        def chmod_spy(path, mode, *args, **kwargs):
            chmodded.append(Path(path))
            return real_chmod(path, mode, *args, **kwargs)

        with patch.object(Path, "rglob", rglob_spy), \
                patch("docker.versioning.build_snapshot.os.chmod", side_effect=chmod_spy):
            with self.assertRaises(RuntimeError) as ctx:
                cleanup_artifact_snapshot(tree)
        self.assertIs(ctx.exception, defect)
        self.assertIn(tree / "sub", chmodded)
        self.assertIn(tree, chmodded)
        self.assertFalse(tree.exists())
        secondary = getattr(defect, "_transaction_secondary", [])
        self.assertTrue(any(isinstance(item, PermissionError) for item in secondary))
        self.assertEqual(0o444, blob.stat().st_mode & 0o777)

    def test_enumeration_retains_every_directory_discovered_before_failure(self):
        tree, _ = self._readonly_tree(nested_file=False)
        os.chmod(tree, 0o700)
        second = tree / "second"
        second.mkdir()
        data = second / "data"
        data.write_bytes(b"x")
        data.chmod(0o444)
        second.chmod(0o555)
        tree.chmod(0o555)
        defect = RuntimeError("enumeration defect")
        chmodded: list[Path] = []
        real_chmod = os.chmod

        def rglob_spy(self, pattern):
            def iterator():
                yield tree / "sub"
                yield second
                raise defect

            return iterator()

        def chmod_spy(path, mode, *args, **kwargs):
            chmodded.append(Path(path))
            return real_chmod(path, mode, *args, **kwargs)

        with patch.object(Path, "rglob", rglob_spy), \
                patch("docker.versioning.build_snapshot.os.chmod", side_effect=chmod_spy):
            with self.assertRaises(RuntimeError) as ctx:
                cleanup_artifact_snapshot(tree)
        self.assertIs(ctx.exception, defect)
        self.assertIn(tree / "sub", chmodded)
        self.assertIn(second, chmodded)
        self.assertFalse(tree.exists())

    def test_enumeration_interruption_is_authoritative_and_cleanup_continues(self):
        tree, blob = self._readonly_tree()
        interruption = KeyboardInterrupt("enumeration interrupted")
        chmodded: list[Path] = []
        real_chmod = os.chmod

        def rglob_spy(self, pattern):
            def iterator():
                yield tree / "sub"
                raise interruption

            return iterator()

        def chmod_spy(path, mode, *args, **kwargs):
            chmodded.append(Path(path))
            return real_chmod(path, mode, *args, **kwargs)

        with patch.object(Path, "rglob", rglob_spy), \
                patch("docker.versioning.build_snapshot.os.chmod", side_effect=chmod_spy):
            with self.assertRaises(KeyboardInterrupt) as ctx:
                cleanup_artifact_snapshot(tree)
        self.assertIs(ctx.exception, interruption)
        self.assertIn(tree / "sub", chmodded)
        self.assertIn(tree, chmodded)
        self.assertFalse(tree.exists())
        self.assertEqual(0o444, blob.stat().st_mode & 0o777)


class TwoBuildOwnershipMaintenanceRegression(unittest.TestCase):
    """Task 4.2: blob reuse survives project-scoped ownership maintenance."""

    def _artifact(self, name, payload):
        return SelectedBuildArtifact(
            name, f"https://{name}.example.invalid/{name}",
            DigestIdentity.from_hex("sha256", hashlib.sha256(payload).hexdigest()),
        )

    def test_two_builds_reuse_blobs_after_project_ownership_maintenance(self):
        artifacts = [
            (self._artifact(name, payload), payload)
            for name, payload in (
                ("rustup", b"rustup-payload"), ("uv", b"uv-payload"),
                ("rtk", b"rtk-payload"), ("fd", b"fd-payload"),
            )
        ]
        class Transport:
            def __init__(self, table):
                self.table, self.calls = table, []
            def stream(self, url):
                self.calls.append(url)
                if url not in self.table:
                    raise AssertionError(f"unexpected network access: {url}")
                yield self.table[url]

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            work = base / "work"; work.mkdir(mode=0o755)
            project = work / "constructor"; project.mkdir()
            primary = work / "primary-workspace"; primary.mkdir()
            extra = work / "extra-workspace"; extra.mkdir()
            cache = base / "cache"; cache.mkdir(mode=0o700)
            from docker.versioning.project_state import resolve_project_state
            state = resolve_project_state(project, cache_root=cache)

            # First build: streamed misses, immutable snapshot, cleanup.
            first = Transport({a.url: p for a, p in artifacts})
            blobs1 = tuple(materialize_artifact(
                a, constructor_project_root=project, cache_root=cache, project_state=state,
                transport=first,
            ) for a, _ in artifacts)
            self.assertEqual(len(artifacts), len(first.calls))
            snap1 = create_artifact_snapshot(
                (a for a, _ in artifacts), blobs1, constructor_project_root=project, project_state=state)
            cleanup_artifact_snapshot(snap1)
            before = {blob: (blob.stat().st_uid, blob.stat().st_mode, blob.stat().st_ino, blob.read_bytes())
                      for blob in blobs1}

            # External host event between invocations: recursive docker-dev
            # ownership transfer plus group read/write/traverse permission,
            # delegated to sudo. Only the project/workspace trees are
            # maintained; the cache and its blobs stay invoking-user-owned.
            uid, gid = docker_dev_ids()
            for root in (project, primary, extra):
                sudo_maintain_tree(root, uid, gid)

            # Second build as the original invoking user: a download-free cache
            # hit whose blobs are byte-, inode-, mode-, and owner-identical.
            second = Transport({})
            blobs2 = tuple(materialize_artifact(
                a, constructor_project_root=project, cache_root=cache, project_state=state,
                transport=second,
            ) for a, _ in artifacts)
            self.assertEqual([], second.calls)
            self.assertEqual(blobs2, blobs1)
            for blob in blobs1:
                st = blob.stat()
                self.assertEqual(st.st_uid, os.geteuid())
                self.assertEqual(st.st_mode & 0o777, 0o444)
                self.assertEqual(st.st_ino, before[blob][2])
                self.assertEqual(blob.read_bytes(), before[blob][3])
            snap2 = create_artifact_snapshot(
                (a for a, _ in artifacts), blobs2, constructor_project_root=project, project_state=state)
            cleanup_artifact_snapshot(snap2)


if __name__ == "__main__":
    unittest.main()

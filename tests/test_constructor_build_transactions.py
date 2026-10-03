"""Phase 2 RED/GREEN contracts for serialized build-cache state."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
import time
from unittest import mock

from docker.versioning.build_cache import (
    UNCOMMITTED_TTL_SECONDS,
    BuildCacheError,
    BuildTransactionError,
    acquire_constructor_project_build_lock,
    build_blob_path,
    commit_build_set,
    maintain_uncommitted_blobs,
    mark_uncommitted_blob,
    prepare_build_cache,
    publish_uncommitted_blob,
    publish_verified_blob,
    recover_abandoned_snapshots,
)
from docker.versioning.digest_identity import DigestIdentity


def _acquire_pristine_lock(checkout: str, start: multiprocessing.Event, results: multiprocessing.Queue) -> None:
    """Child-process helper for concurrent first-build lock bootstrap."""
    start.wait()
    try:
        with acquire_constructor_project_build_lock(checkout):
            results.put("acquired")
            time.sleep(0.2)
    except BuildTransactionError as exc:
        results.put(str(exc))
    except BaseException as exc:
        results.put(f"unexpected: {type(exc).__name__}: {exc}")


def _newest_generation_blobs(paths) -> list[str]:
    """Return the canonical blob keys of the newest immutable generation."""
    candidates = sorted(
        entry.name
        for entry in paths.persistent_root.iterdir()
        if entry.name.startswith("committed-build-")
    )
    return json.loads((paths.persistent_root / candidates[-1]).read_text())["blobs"]


def _generation_names(paths) -> list[str]:
    return sorted(
        entry.name
        for entry in paths.persistent_root.iterdir()
        if entry.name.startswith("committed-build-")
    )


class BuildTransactionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.checkout = Path(self.tmp.name) / "checkout"
        self.checkout.mkdir()

    def blob(self, payload: bytes) -> DigestIdentity:
        identity = DigestIdentity.from_hex("sha256", hashlib.sha256(payload).hexdigest())
        publish_verified_blob(identity, payload, constructor_project_root=self.checkout)
        return identity

    def test_simultaneous_pristine_checkout_bootstrap_serializes_builds(self) -> None:
        checkout = Path(self.tmp.name) / "pristine-checkout"
        checkout.mkdir()
        context = multiprocessing.get_context("fork")
        start = context.Event()
        results = context.Queue()
        workers = [
            context.Process(target=_acquire_pristine_lock, args=(str(checkout), start, results))
            for _ in range(4)
        ]
        for worker in workers:
            worker.start()
        start.set()
        for worker in workers:
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive(), "lock contender did not terminate")
        outcomes = [results.get(timeout=1) for _ in workers]
        self.assertIn("acquired", outcomes)
        self.assertTrue(
            all(value == "acquired" or "active build" in value for value in outcomes),
            outcomes,
        )

    def test_competing_process_is_rejected_before_any_mutation(self) -> None:
        with acquire_constructor_project_build_lock(self.checkout):
            command = (
                "from docker.versioning.build_cache import acquire_constructor_project_build_lock; "
                f"acquire_constructor_project_build_lock({str(self.checkout)!r})"
            )
            result = subprocess.run([sys.executable, "-c", command], text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("active build", result.stderr)
        self.assertFalse((prepare_build_cache(self.checkout).generated_root / "other").exists())

    def test_hard_linked_lock_is_rejected_without_chmodding_link_target(self) -> None:
        cache = Path(self.tmp.name) / "cache"
        cache.mkdir(mode=0o700)
        paths = prepare_build_cache(self.checkout, cache_root=cache)
        unrelated = self.checkout / "unrelated-owner-file"
        unrelated.write_bytes(b"must retain mode")
        unrelated.chmod(0o644)
        lock_path = paths.persistent_root / "build.lock"
        lock_path.hardlink_to(unrelated)
        with self.assertRaisesRegex(BuildTransactionError, "unsafe constructor-project build lock"):
            acquire_constructor_project_build_lock(self.checkout, cache_root=cache)
        self.assertEqual(0o644, unrelated.stat().st_mode & 0o777)
        self.assertEqual(2, unrelated.stat().st_nlink)

    def test_competing_lock_does_not_repair_owner_lock_mode(self) -> None:
        paths = prepare_build_cache(self.checkout)
        lock_path = paths.persistent_root / "build.lock"
        with acquire_constructor_project_build_lock(self.checkout):
            lock_path.chmod(0o644)
            command = (
                "from docker.versioning.build_cache import acquire_constructor_project_build_lock; "
                f"acquire_constructor_project_build_lock({str(self.checkout)!r})"
            )
            result = subprocess.run([sys.executable, "-c", command], text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("active build", result.stderr)
            self.assertEqual(0o644, lock_path.stat().st_mode & 0o777)
        with acquire_constructor_project_build_lock(self.checkout):
            self.assertEqual(0o600, lock_path.stat().st_mode & 0o777)

    def test_competing_lock_does_not_repair_unsafe_cache_state(self) -> None:
        paths = prepare_build_cache(self.checkout)
        with acquire_constructor_project_build_lock(self.checkout):
            paths.persistent_root.chmod(0o777)
            command = (
                "from docker.versioning.build_cache import acquire_constructor_project_build_lock; "
                f"acquire_constructor_project_build_lock({str(self.checkout)!r})"
            )
            result = subprocess.run([sys.executable, "-c", command], text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("active build", result.stderr)
            self.assertEqual(0o777, paths.persistent_root.stat().st_mode & 0o777)
            self.assertFalse((paths.persistent_root / "committed-build.json").exists())
            self.assertFalse(any(paths.markers_root.iterdir()))
            self.assertFalse(any(paths.generated_root.iterdir()))

    def test_lock_releases_after_exception_and_normal_exit(self) -> None:
        with self.assertRaises(RuntimeError):
            with acquire_constructor_project_build_lock(self.checkout):
                raise RuntimeError("interrupted owner")
        with acquire_constructor_project_build_lock(self.checkout):
            pass
        with acquire_constructor_project_build_lock(self.checkout):
            pass

    def test_recovery_needs_live_lock_and_preserves_blobs(self) -> None:
        identity = self.blob(b"reusable")
        paths = prepare_build_cache(self.checkout)
        abandoned = paths.generated_root / "abandoned"
        abandoned.mkdir()
        (abandoned / "snapshot").write_text("partial")
        with self.assertRaises(TypeError):
            recover_abandoned_snapshots(self.checkout)  # type: ignore[call-arg]
        # A later transaction acquires the live checkout lock before recovery.
        with acquire_constructor_project_build_lock(self.checkout) as lock:
            recover_abandoned_snapshots(self.checkout, lock=lock)
        self.assertFalse(abandoned.exists())
        self.assertTrue(build_blob_path(paths.blobs_root, identity).exists())

    def test_manifest_is_atomic_and_commit_precedes_superseded_delete(self) -> None:
        old, new = self.blob(b"old"), self.blob(b"new")
        paths = prepare_build_cache(self.checkout)
        with acquire_constructor_project_build_lock(self.checkout) as lock:
            mark_uncommitted_blob(old, self.checkout, lock=lock, verified_at=0)
            commit_build_set(self.checkout, {old}, lock=lock)
            old_path = build_blob_path(paths.blobs_root, old)
            self.assertTrue(old_path.exists())
            commit_build_set(self.checkout, {new}, lock=lock)
        self.assertEqual([f"sha256:{new.hex_digest()}"], _newest_generation_blobs(paths))
        self.assertFalse(old_path.exists())
        self.assertTrue(build_blob_path(paths.blobs_root, new).exists())
        # A successful cleanup returns to exactly one stable generation.
        self.assertEqual(len(_generation_names(paths)), 1)

    def test_failed_build_keeps_prior_live_set_and_shared_xdg_is_untouched(self) -> None:
        committed, failed = self.blob(b"committed"), self.blob(b"failed")
        xdg = Path(self.tmp.name) / "xdg-runtime-artifact"
        xdg.write_bytes(b"unrelated")
        paths = prepare_build_cache(self.checkout)
        with acquire_constructor_project_build_lock(self.checkout) as lock:
            commit_build_set(self.checkout, {committed}, lock=lock)
            mark_uncommitted_blob(failed, self.checkout, lock=lock, verified_at=1)
            # No commit models Docker failure/interruption.
            maintain_uncommitted_blobs(self.checkout, lock=lock, now=2)
        self.assertTrue(build_blob_path(paths.blobs_root, committed).exists())
        self.assertTrue(build_blob_path(paths.blobs_root, failed).exists())
        self.assertEqual(b"unrelated", xdg.read_bytes())

    def test_interrupted_publication_leaves_marker_for_missing_blob_recovery(self) -> None:
        payload = b"interrupted-before-publish"
        identity = DigestIdentity.from_hex("sha256", hashlib.sha256(payload).hexdigest())
        paths = prepare_build_cache(self.checkout)
        with acquire_constructor_project_build_lock(self.checkout) as lock:
            with mock.patch(
                "docker.versioning.build_cache.publish_verified_blob",
                side_effect=KeyboardInterrupt(),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    from docker.versioning.build_cache import publish_uncommitted_blob
                    publish_uncommitted_blob(identity, payload, constructor_project_root=self.checkout, lock=lock)
            self.assertTrue((paths.markers_root / f"sha256:{identity.hex_digest()}.json").exists())
            self.assertFalse(build_blob_path(paths.blobs_root, identity).exists())
            maintain_uncommitted_blobs(self.checkout, lock=lock, now=0)
        self.assertFalse((paths.markers_root / f"sha256:{identity.hex_digest()}.json").exists())

    def test_interruption_after_publication_retains_expiring_marker(self) -> None:
        payload = b"interrupted-after-publish"
        identity = DigestIdentity.from_hex("sha256", hashlib.sha256(payload).hexdigest())
        paths = prepare_build_cache(self.checkout)
        original = publish_verified_blob

        def publish_then_interrupt(*args, **kwargs):
            original(*args, **kwargs)
            raise KeyboardInterrupt()

        with acquire_constructor_project_build_lock(self.checkout) as lock:
            with mock.patch(
                "docker.versioning.build_cache.publish_verified_blob",
                side_effect=publish_then_interrupt,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    from docker.versioning.build_cache import publish_uncommitted_blob
                    publish_uncommitted_blob(
                        identity, payload, constructor_project_root=self.checkout, lock=lock, verified_at=100,
                    )
            self.assertTrue(build_blob_path(paths.blobs_root, identity).exists())
            self.assertTrue((paths.markers_root / f"sha256:{identity.hex_digest()}.json").exists())
            maintain_uncommitted_blobs(
                self.checkout, lock=lock, now=100 + UNCOMMITTED_TTL_SECONDS + 1,
            )
        self.assertFalse(build_blob_path(paths.blobs_root, identity).exists())
        self.assertFalse((paths.markers_root / f"sha256:{identity.hex_digest()}.json").exists())

    def test_post_publication_failure_preserves_marker_until_maintenance(self) -> None:
        payload = b"post-publication-failure"
        identity = DigestIdentity.from_hex("sha256", hashlib.sha256(payload).hexdigest())
        paths = prepare_build_cache(self.checkout)
        original = publish_verified_blob

        def publish_then_fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("final verification interrupted")

        with acquire_constructor_project_build_lock(self.checkout) as lock:
            with mock.patch(
                "docker.versioning.build_cache.publish_verified_blob",
                side_effect=publish_then_fail,
            ):
                with self.assertRaisesRegex(RuntimeError, "final verification interrupted"):
                    from docker.versioning.build_cache import publish_uncommitted_blob
                    publish_uncommitted_blob(
                        identity, payload, constructor_project_root=self.checkout, lock=lock, verified_at=100,
                    )
            marker = paths.markers_root / f"sha256:{identity.hex_digest()}.json"
            self.assertTrue(build_blob_path(paths.blobs_root, identity).exists())
            self.assertTrue(marker.exists())
            maintain_uncommitted_blobs(self.checkout, lock=lock, now=101)
            self.assertTrue(marker.exists())
            blob = build_blob_path(paths.blobs_root, identity)
            blob.chmod(0o644)
            blob.write_bytes(b"corrupt after publication")
            blob.chmod(0o444)
            maintain_uncommitted_blobs(self.checkout, lock=lock, now=102)
        self.assertFalse(blob.exists())
        self.assertFalse(marker.exists())

    def test_mismatched_republication_preserves_existing_blob_and_marker(self) -> None:
        payload = b"original-publication"
        identity = DigestIdentity.from_hex("sha256", hashlib.sha256(payload).hexdigest())
        paths = prepare_build_cache(self.checkout)
        with acquire_constructor_project_build_lock(self.checkout) as lock:
            publish_uncommitted_blob(identity, payload, constructor_project_root=self.checkout, lock=lock, verified_at=100)
        blob = build_blob_path(paths.blobs_root, identity)
        marker = paths.markers_root / f"sha256:{identity.hex_digest()}.json"
        original_blob = blob.read_bytes()
        original_marker = marker.read_bytes()
        with acquire_constructor_project_build_lock(self.checkout) as lock:
            with self.assertRaisesRegex(Exception, "digest mismatch"):
                publish_uncommitted_blob(identity, b"mismatched", constructor_project_root=self.checkout, lock=lock, verified_at=200)
        self.assertEqual(original_blob, blob.read_bytes())
        self.assertEqual(original_marker, marker.read_bytes())

    def test_transaction_publication_marks_blob_and_failed_blob_expires(self) -> None:
        payload = b"failed-download"
        identity = DigestIdentity.from_hex("sha256", hashlib.sha256(payload).hexdigest())
        paths = prepare_build_cache(self.checkout)
        with acquire_constructor_project_build_lock(self.checkout) as lock:
            publish_uncommitted_blob(identity, payload, constructor_project_root=self.checkout, lock=lock, verified_at=100)
            self.assertEqual(
                {"verified_at": 100},
                json.loads((paths.markers_root / f"sha256:{identity.hex_digest()}.json").read_text()),
            )
            # No commit models a failed/interrupted transaction.
        with acquire_constructor_project_build_lock(self.checkout) as lock:
            maintain_uncommitted_blobs(self.checkout, lock=lock, now=100 + UNCOMMITTED_TTL_SECONDS + 1)
        self.assertFalse(build_blob_path(paths.blobs_root, identity).exists())

    def test_corrupt_stale_marker_never_deletes_committed_blob(self) -> None:
        identity = self.blob(b"committed-marker-immunity")
        paths = prepare_build_cache(self.checkout)
        marker = paths.markers_root / f"sha256:{identity.hex_digest()}.json"
        with acquire_constructor_project_build_lock(self.checkout) as lock:
            commit_build_set(self.checkout, {identity}, lock=lock)
        marker.write_text('{"verified_at":NaN}')
        marker.chmod(0o600)
        with acquire_constructor_project_build_lock(self.checkout) as lock:
            maintain_uncommitted_blobs(self.checkout, lock=lock, now=1)
        self.assertFalse(marker.exists())
        self.assertTrue(build_blob_path(paths.blobs_root, identity).exists())

    def test_exact_ttl_boundary_and_committed_immunity(self) -> None:
        expired, committed = self.blob(b"expired"), self.blob(b"committed")
        paths = prepare_build_cache(self.checkout)
        with acquire_constructor_project_build_lock(self.checkout) as lock:
            mark_uncommitted_blob(expired, self.checkout, lock=lock, verified_at=100)
            mark_uncommitted_blob(committed, self.checkout, lock=lock, verified_at=0)
            commit_build_set(self.checkout, {committed}, lock=lock)
            maintain_uncommitted_blobs(
                self.checkout, lock=lock, now=100 + UNCOMMITTED_TTL_SECONDS - 1,
            )
            self.assertTrue(build_blob_path(paths.blobs_root, expired).exists())
            # Exactly 2,592,000 seconds old is NOT expired: the blob survives
            # at the boundary and only expires once it is older than the TTL.
            maintain_uncommitted_blobs(
                self.checkout, lock=lock, now=100 + UNCOMMITTED_TTL_SECONDS,
            )
            self.assertTrue(build_blob_path(paths.blobs_root, expired).exists())
            self.assertTrue((paths.markers_root / f"sha256:{expired.hex_digest()}.json").exists())
            maintain_uncommitted_blobs(
                self.checkout, lock=lock, now=100 + UNCOMMITTED_TTL_SECONDS + 1,
            )
        self.assertFalse(build_blob_path(paths.blobs_root, expired).exists())
        self.assertFalse((paths.markers_root / f"sha256:{expired.hex_digest()}.json").exists())
        self.assertTrue(build_blob_path(paths.blobs_root, committed).exists())

    def test_invalid_commit_blob_preserves_previous_manifest_and_blobs(self) -> None:
        previous = self.blob(b"previous")
        paths = prepare_build_cache(self.checkout)
        with acquire_constructor_project_build_lock(self.checkout) as lock:
            commit_build_set(self.checkout, {previous}, lock=lock)
        generations_before = _generation_names(paths)
        cases = ("missing", "altered", "writable")
        for case in cases:
            with self.subTest(case=case):
                payload = f"{case}-commit".encode()
                candidate = DigestIdentity.from_hex("sha256", hashlib.sha256(payload).hexdigest())
                if case != "missing":
                    candidate_path = publish_verified_blob(candidate, payload, constructor_project_root=self.checkout)
                    if case == "altered":
                        candidate_path.chmod(0o644)
                        candidate_path.write_bytes(b"altered")
                        candidate_path.chmod(0o444)
                    else:
                        candidate_path.chmod(0o644)
                with acquire_constructor_project_build_lock(self.checkout) as lock:
                    with self.assertRaises(BuildTransactionError):
                        commit_build_set(self.checkout, {candidate}, lock=lock)
                self.assertEqual(generations_before, _generation_names(paths))
                self.assertEqual([f"sha256:{previous.hex_digest()}"], _newest_generation_blobs(paths))
                self.assertTrue(build_blob_path(paths.blobs_root, previous).exists())

    def test_corrupt_generation_fails_without_any_deletion(self) -> None:
        identity = self.blob(b"live")
        paths = prepare_build_cache(self.checkout)
        outside = Path(self.tmp.name) / "outside"
        outside.write_bytes(b"must not delete")
        corrupt_name = "committed-build-" + "0" * 19 + "1.json"
        invalid_keys = (
            "sha256:../../outside",
            "md5:" + "0" * 32,
            "sha256:abc",
            "SHA256:" + identity.hex_digest(),
            "sha256:" + identity.hex_digest().upper(),
        )
        for key in invalid_keys:
            with self.subTest(key=key):
                original = json.dumps({"version": 1, "blobs": [key]})
                (paths.persistent_root / corrupt_name).write_text(original)
                with acquire_constructor_project_build_lock(self.checkout) as lock:
                    with self.assertRaises(BuildCacheError):
                        commit_build_set(self.checkout, {identity}, lock=lock)
                self.assertEqual(original, (paths.persistent_root / corrupt_name).read_text())
                self.assertTrue(outside.exists())
                self.assertTrue(build_blob_path(paths.blobs_root, identity).exists())
                (paths.persistent_root / corrupt_name).unlink()

    def test_marker_creation_rejects_non_finite_timestamps(self) -> None:
        identity = self.blob(b"timestamp")
        for timestamp in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(timestamp=timestamp):
                with acquire_constructor_project_build_lock(self.checkout) as lock:
                    with self.assertRaises(BuildCacheError):
                        mark_uncommitted_blob(
                            identity, self.checkout, lock=lock, verified_at=timestamp,
                        )

    def test_non_finite_marker_and_clock_values_are_rejected(self) -> None:
        for timestamp in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(marker_timestamp=timestamp):
                payload = f"marker-{timestamp}".encode()
                identity = self.blob(payload)
                paths = prepare_build_cache(self.checkout)
                marker = paths.markers_root / f"sha256:{identity.hex_digest()}.json"
                marker.write_text(f'{{"verified_at":{timestamp}}}')
                marker.chmod(0o600)
                with acquire_constructor_project_build_lock(self.checkout) as lock:
                    maintain_uncommitted_blobs(self.checkout, lock=lock, now=1)
                self.assertFalse(marker.exists())
                self.assertFalse(build_blob_path(paths.blobs_root, identity).exists())

        huge_identity = self.blob(b"huge-marker")
        huge_paths = prepare_build_cache(self.checkout)
        huge_marker = huge_paths.markers_root / f"sha256:{huge_identity.hex_digest()}.json"
        huge_marker.write_text('{"verified_at":' + '1' + ('0' * 4000) + '}')
        huge_marker.chmod(0o600)
        with acquire_constructor_project_build_lock(self.checkout) as lock:
            maintain_uncommitted_blobs(self.checkout, lock=lock, now=1)
        self.assertFalse(huge_marker.exists())
        self.assertFalse(build_blob_path(huge_paths.blobs_root, huge_identity).exists())

        identity = self.blob(b"expired-with-invalid-clock")
        paths = prepare_build_cache(self.checkout)
        for clock in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(clock=clock):
                with acquire_constructor_project_build_lock(self.checkout) as lock:
                    mark_uncommitted_blob(identity, self.checkout, lock=lock, verified_at=0)
                    with self.assertRaises(BuildCacheError):
                        maintain_uncommitted_blobs(self.checkout, lock=lock, now=clock)
                self.assertTrue(build_blob_path(paths.blobs_root, identity).exists())
                self.assertTrue((paths.markers_root / f"sha256:{identity.hex_digest()}.json").exists())

    def test_valid_marker_with_missing_or_corrupt_safe_blob_is_removed(self) -> None:
        cases = ("missing", "altered")
        for case in cases:
            with self.subTest(case=case):
                payload = f"{case}-blob".encode()
                identity = self.blob(payload)
                paths = prepare_build_cache(self.checkout)
                blob = build_blob_path(paths.blobs_root, identity)
                with acquire_constructor_project_build_lock(self.checkout) as lock:
                    mark_uncommitted_blob(identity, self.checkout, lock=lock, verified_at=0)
                    if case == "missing":
                        blob.unlink()
                    elif case == "altered":
                        blob.chmod(0o644)
                        blob.write_bytes(b"altered")
                        blob.chmod(0o444)
                    maintain_uncommitted_blobs(self.checkout, lock=lock, now=1)
                self.assertFalse(blob.exists())
                self.assertFalse((paths.markers_root / f"sha256:{identity.hex_digest()}.json").exists())

    def test_unsafe_writable_blob_fails_closed(self) -> None:
        identity = self.blob(b"writable-blob")
        paths = prepare_build_cache(self.checkout)
        blob = build_blob_path(paths.blobs_root, identity)
        with acquire_constructor_project_build_lock(self.checkout) as lock:
            mark_uncommitted_blob(identity, self.checkout, lock=lock, verified_at=0)
            blob.chmod(0o644)
            with self.assertRaises(BuildTransactionError):
                maintain_uncommitted_blobs(self.checkout, lock=lock, now=1)
        self.assertTrue(blob.exists())
        self.assertTrue(
            (paths.markers_root / f"sha256:{identity.hex_digest()}.json").exists()
        )

    def test_corrupt_marker_removes_partial_blob_immediately(self) -> None:
        identity = self.blob(b"partial")
        paths = prepare_build_cache(self.checkout)
        marker = paths.markers_root / f"sha256:{identity.hex_digest()}.json"
        marker.write_text("not-json")
        marker.chmod(0o600)
        with acquire_constructor_project_build_lock(self.checkout) as lock:
            maintain_uncommitted_blobs(self.checkout, lock=lock, now=0)
        self.assertFalse(marker.exists())
        self.assertFalse(build_blob_path(paths.blobs_root, identity).exists())


class ExternalTransactionIsolationTest(unittest.TestCase):
    """Revised Phase 2 contract: canonical identity, cross-project isolation,
    legacy-state neutrality, and global runtime/versioning cache immunity."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.cache = self.base / "cache"
        self.cache.mkdir(mode=0o700)
        os.chmod(self.cache, 0o700)
        self.project = self.base / "a" / "proj"
        self.project.mkdir(parents=True)
        self.paths = prepare_build_cache(self.project, cache_root=self.cache)

    def identity(self, payload: bytes) -> DigestIdentity:
        return DigestIdentity.from_hex("sha256", hashlib.sha256(payload).hexdigest())

    def blob(self, payload: bytes, *, checkout: Path) -> DigestIdentity:
        identity = self.identity(payload)
        publish_verified_blob(identity, payload, constructor_project_root=checkout, cache_root=self.cache)
        return identity

    def second_project(self, name: str = "proj") -> Path:
        other = self.base / "b" / name
        other.mkdir(parents=True)
        return other

    # ── 2.1 canonical lock identity ─────────────────────────────────────

    def test_canonical_alias_contender_is_rejected_under_one_lock(self) -> None:
        alias = self.base / "proj-link"
        alias.symlink_to(self.project, target_is_directory=True)
        with acquire_constructor_project_build_lock(self.project, cache_root=self.cache):
            with self.assertRaisesRegex(BuildTransactionError, "active build"):
                acquire_constructor_project_build_lock(alias, cache_root=self.cache)
        # The alias never created its own namespace or mutated the project.
        self.assertFalse((self.project / ".docker-cache").exists())
        self.assertFalse((self.project / ".docker-generated").exists())

    def test_relative_and_absolute_paths_share_one_critical_section(self) -> None:
        previous = os.getcwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(self.base)
        relative = os.path.relpath(self.project, self.base)
        with acquire_constructor_project_build_lock(relative, cache_root=self.cache):
            with self.assertRaisesRegex(BuildTransactionError, "active build"):
                acquire_constructor_project_build_lock(self.project, cache_root=self.cache)

    def test_same_basename_projects_hold_independent_locks(self) -> None:
        other = self.second_project()
        other_paths = prepare_build_cache(other, cache_root=self.cache)
        self.assertNotEqual(other_paths.namespace_root, self.paths.namespace_root)
        self.assertTrue(other_paths.namespace_root.name.startswith("proj-"))
        # Both namespaces serialize independently.
        with acquire_constructor_project_build_lock(self.project, cache_root=self.cache):
            with acquire_constructor_project_build_lock(other, cache_root=self.cache):
                pass
        with acquire_constructor_project_build_lock(other, cache_root=self.cache):
            with acquire_constructor_project_build_lock(self.project, cache_root=self.cache):
                pass

    # ── 2.2 abandoned-snapshot recovery ─────────────────────────────────

    def test_recovery_keeps_verified_blobs_uncommitted(self) -> None:
        identity = self.blob(b"reusable-after-recovery", checkout=self.project)
        with acquire_constructor_project_build_lock(self.project, cache_root=self.cache) as lock:
            mark_uncommitted_blob(identity, self.project, lock=lock, verified_at=0, cache_root=self.cache)
            snapshot = self.paths.generated_root / "abandoned"
            snapshot.mkdir()
            (snapshot / "partial").write_text("partial")
            recover_abandoned_snapshots(self.project, lock=lock, cache_root=self.cache)
            self.assertFalse(snapshot.exists())
            self.assertTrue(build_blob_path(self.paths.blobs_root, identity).exists())
            self.assertTrue((self.paths.markers_root / f"sha256:{identity.hex_digest()}.json").exists())
            self.assertFalse((self.paths.persistent_root / "committed-build.json").exists())

    def test_recovery_ignores_legacy_and_other_namespace_snapshots(self) -> None:
        legacy = self.project / ".docker-cache" / "snapshots" / "legacy-snapshot"
        legacy.mkdir(parents=True)
        (legacy / "keep.txt").write_text("legacy")
        other = self.second_project()
        other_paths = prepare_build_cache(other, cache_root=self.cache)
        other_snapshot = other_paths.generated_root / "other-snapshot"
        other_snapshot.mkdir()
        (other_snapshot / "keep.txt").write_text("other")
        with acquire_constructor_project_build_lock(self.project, cache_root=self.cache) as lock:
            recover_abandoned_snapshots(self.project, lock=lock, cache_root=self.cache)
        self.assertEqual((legacy / "keep.txt").read_text(), "legacy")
        self.assertEqual((other_snapshot / "keep.txt").read_text(), "other")
        self.assertTrue((self.project / ".docker-cache").is_dir())

    # ── 2.3 committed-build manifest ────────────────────────────────────

    def test_commit_removes_every_superseded_blob_immediately(self) -> None:
        old_one = self.blob(b"old-one", checkout=self.project)
        old_two = self.blob(b"old-two", checkout=self.project)
        new = self.blob(b"new", checkout=self.project)
        with acquire_constructor_project_build_lock(self.project, cache_root=self.cache) as lock:
            mark_uncommitted_blob(old_one, self.project, lock=lock, verified_at=0, cache_root=self.cache)
            mark_uncommitted_blob(old_two, self.project, lock=lock, verified_at=0, cache_root=self.cache)
            commit_build_set(self.project, {old_one, old_two}, lock=lock, cache_root=self.cache)
            mark_uncommitted_blob(new, self.project, lock=lock, verified_at=0, cache_root=self.cache)
            commit_build_set(self.project, {new}, lock=lock, cache_root=self.cache)
        manifest = _newest_generation_blobs(self.paths)
        self.assertEqual(manifest, [f"sha256:{new.hex_digest()}"])
        self.assertFalse(build_blob_path(self.paths.blobs_root, old_one).exists())
        self.assertFalse(build_blob_path(self.paths.blobs_root, old_two).exists())
        self.assertTrue(build_blob_path(self.paths.blobs_root, new).exists())

    def test_commit_and_gc_leave_other_namespaces_and_global_caches_untouched(self) -> None:
        other = self.second_project()
        other_paths = prepare_build_cache(other, cache_root=self.cache)
        other_blob = self.blob(b"other-committed", checkout=other)
        with acquire_constructor_project_build_lock(other, cache_root=self.cache) as lock:
            mark_uncommitted_blob(other_blob, other, lock=lock, verified_at=0, cache_root=self.cache)
            commit_build_set(other, {other_blob}, lock=lock, cache_root=self.cache)

        runtime = self.cache / "runtime-artifacts" / "blobs"
        runtime.mkdir(parents=True)
        (runtime / "runtime-blob").write_text("runtime")
        versioning = self.cache / "versioning"
        versioning.mkdir()
        (versioning / "discovery.json").write_text("versioning")

        committed = self.blob(b"committed", checkout=self.project)
        stale = self.blob(b"stale", checkout=self.project)
        replacement = self.blob(b"replacement", checkout=self.project)
        with acquire_constructor_project_build_lock(self.project, cache_root=self.cache) as lock:
            mark_uncommitted_blob(committed, self.project, lock=lock, verified_at=0, cache_root=self.cache)
            commit_build_set(self.project, {committed}, lock=lock, cache_root=self.cache)
            mark_uncommitted_blob(stale, self.project, lock=lock, verified_at=0, cache_root=self.cache)
            mark_uncommitted_blob(replacement, self.project, lock=lock, verified_at=0, cache_root=self.cache)
            commit_build_set(self.project, {replacement}, lock=lock, cache_root=self.cache)
            # At exactly the TTL the uncommitted blob is retained; it is
            # collected only once it is older than the TTL.
            maintain_uncommitted_blobs(
                self.project, lock=lock, now=UNCOMMITTED_TTL_SECONDS, cache_root=self.cache,
            )
            self.assertTrue(build_blob_path(self.paths.blobs_root, stale).exists())
            maintain_uncommitted_blobs(
                self.project, lock=lock, now=UNCOMMITTED_TTL_SECONDS + 1, cache_root=self.cache,
            )
            self.assertFalse(build_blob_path(self.paths.blobs_root, stale).exists())

        # Superseded project blob is gone; the replacement survives.
        self.assertFalse(build_blob_path(self.paths.blobs_root, committed).exists())
        self.assertTrue(build_blob_path(self.paths.blobs_root, replacement).exists())
        # Another project namespace and global runtime/versioning caches are untouched.
        self.assertTrue(build_blob_path(other_paths.blobs_root, other_blob).exists())
        self.assertEqual(
            _newest_generation_blobs(other_paths),
            [f"sha256:{other_blob.hex_digest()}"],
        )
        self.assertEqual((runtime / "runtime-blob").read_text(), "runtime")
        self.assertEqual((versioning / "discovery.json").read_text(), "versioning")

    # ── 2.4 markers / fixed-policy GC ───────────────────────────────────

    def test_legacy_checkout_state_is_neither_adopted_nor_deleted(self) -> None:
        legacy = self.project / ".docker-cache"
        legacy_manifest = legacy / "committed-build.json"
        legacy_manifest.parent.mkdir(parents=True)
        legacy_manifest.write_text('{"blobs": ["sha256:" + "0" * 64]}')
        legacy_blob = legacy / "blobs" / "sha256" / ("0" * 64 + ".blob")
        legacy_blob.parent.mkdir(parents=True)
        legacy_blob.write_bytes(b"legacy-blob")
        legacy_snapshot = legacy / "snapshots" / "stale"
        legacy_snapshot.mkdir(parents=True)
        (legacy_snapshot / "partial").write_text("partial")
        before = {
            legacy_manifest: legacy_manifest.read_bytes(),
            legacy_blob: legacy_blob.read_bytes(),
            legacy_snapshot / "partial": (legacy_snapshot / "partial").read_bytes(),
        }

        identity = self.blob(b"external-transaction", checkout=self.project)
        with acquire_constructor_project_build_lock(self.project, cache_root=self.cache) as lock:
            mark_uncommitted_blob(identity, self.project, lock=lock, verified_at=0, cache_root=self.cache)
            commit_build_set(self.project, {identity}, lock=lock, cache_root=self.cache)
            recover_abandoned_snapshots(self.project, lock=lock, cache_root=self.cache)
            maintain_uncommitted_blobs(self.project, lock=lock, now=0, cache_root=self.cache)

        for path, payload in before.items():
            self.assertTrue(path.exists(), path)
            self.assertEqual(path.read_bytes(), payload, path)
        self.assertEqual(
            _newest_generation_blobs(self.paths),
            [f"sha256:{identity.hex_digest()}"],
        )


if __name__ == "__main__":
    unittest.main()

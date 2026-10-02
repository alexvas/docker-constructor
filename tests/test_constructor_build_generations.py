"""Phase 3 RED contracts — immutable monotonically numbered build generations.

These tests pin the build-domain generation surface added by Phase 3:

* canonical fixed-width ``committed-build-<20 digits>.json`` naming and
  numeric/lexical ordering, overflow, malformed names, and legacy exclusion;
* a build-owned closed manifest schema over the canonical JSON codec;
* zero/one/two-generation classification with fail-closed ambiguity;
* discovery-time generation-directory synchronization before authority;
* durable no-clobber publication of ``max(valid) + 1`` under the checkout lock.

The module under test does not exist until the Phase 3 GREEN tasks.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from docker.transactions.capabilities import DirectoryCapability
from docker.transactions.errors import CapabilityError, TransactionError
from docker.versioning.build_generations import (
    BUILD_LOCK_NAME,
    BUILD_LOCK_NAMESPACE,
    GENERATION_MODE,
    LEGACY_MANIFEST_NAME,
    MANIFEST_VERSION,
    MAX_GENERATION,
    BuildGeneration,
    BuildGenerationError,
    BuildManifest,
    GenerationState,
    acquire_build_generation_lock,
    canonical_blob_key,
    discover_generations,
    format_generation_name,
    inspect_generations,
    is_generation_candidate,
    parse_generation_name,
    publish_generation,
)
from docker.versioning.digest_identity import DigestIdentity
from tests.transactions_test_support import InjectedOps


def identity(payload: bytes) -> DigestIdentity:
    return DigestIdentity.from_hex("sha256", hashlib.sha256(payload).hexdigest())


class GenerationNameTests(unittest.TestCase):
    def test_format_uses_exact_twenty_digit_suffix(self) -> None:
        self.assertEqual(
            format_generation_name(1),
            "committed-build-00000000000000000001.json",
        )
        self.assertEqual(
            format_generation_name(10**19),
            "committed-build-10000000000000000000.json",
        )
        self.assertEqual(
            format_generation_name(MAX_GENERATION),
            "committed-build-99999999999999999999.json",
        )

    def test_format_rejects_zero_negative_and_overflow(self) -> None:
        for number in (0, -1, MAX_GENERATION + 1, 10**20, True, "1", 1.0):
            with self.subTest(number=number):
                with self.assertRaises(BuildGenerationError):
                    format_generation_name(number)

    def test_numeric_and_lexical_order_agree(self) -> None:
        numbers = [1, 2, 9, 10, 11, 99, 100, 999, MAX_GENERATION]
        names = [format_generation_name(number) for number in numbers]
        self.assertEqual(names, sorted(names))

    def test_parse_returns_number_for_canonical_names(self) -> None:
        for number in (1, 2, 19, 20, 10**19, MAX_GENERATION):
            with self.subTest(number=number):
                self.assertEqual(
                    parse_generation_name(format_generation_name(number)), number
                )

    def test_parse_returns_none_for_malformed_generation_names(self) -> None:
        malformed = (
            "committed-build-.json",
            "committed-build-00000000000000000000.json",  # zero
            "committed-build-0000000000000000001.json",  # 19 digits
            "committed-build-000000000000000000001.json",  # 21 digits
            "committed-build-0000000000000000000a.json",  # non-digit
            "committed-build-00000000000000000001",  # missing suffix
            "committed-build-00000000000000000001.JSON",  # wrong suffix case
            "committed-build-100000000000000000000.json",  # 21-digit overflow
            "committed-build-٩٩٩٩٩٩٩٩٩٩٩٩٩٩٩٩٩٩٩٩.json",  # non-ASCII digits
            "committed-build-00000000000000000001.json.bak",
        )
        for name in malformed:
            with self.subTest(name=name):
                self.assertIsNone(parse_generation_name(name))
                # A malformed candidate still belongs to the generation
                # namespace and must fail closed rather than be ignored.
                self.assertTrue(is_generation_candidate(name))

    def test_parse_ignores_legacy_and_unrelated_names(self) -> None:
        for name in (
            LEGACY_MANIFEST_NAME,
            "build.lock",
            "blobs",
            "tmp",
            "markers",
            ".transaction-0123",
            "committed-build",
            "other-00000000000000000001.json",
        ):
            with self.subTest(name=name):
                self.assertIsNone(parse_generation_name(name))
                self.assertFalse(is_generation_candidate(name))


class ManifestSchemaTests(unittest.TestCase):
    def test_round_trip_and_deterministic_bytes(self) -> None:
        first = identity(b"first")
        second = identity(b"second")
        manifest = BuildManifest.from_blobs([first, second])
        expected_keys = tuple(
            sorted((canonical_blob_key(first), canonical_blob_key(second)))
        )
        self.assertEqual(manifest.keys, expected_keys)
        expected = json.dumps(
            {"version": MANIFEST_VERSION, "blobs": list(expected_keys)},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        self.assertEqual(manifest.encode(), expected)
        self.assertEqual(BuildManifest.decode(manifest.encode()), manifest)
        self.assertEqual(manifest.keys, tuple(sorted(manifest.keys)))

    def test_from_blobs_deduplicates_and_sorts(self) -> None:
        first = identity(b"first")
        second = identity(b"second")
        manifest = BuildManifest.from_blobs([second, first, second])
        self.assertEqual(
            manifest.blobs, tuple(sorted({first, second}, key=canonical_blob_key))
        )

    def test_from_blobs_rejects_non_identities(self) -> None:
        with self.assertRaises(BuildGenerationError):
            BuildManifest.from_blobs(["sha256:" + "0" * 64])

    def test_from_blobs_rejects_non_iterable(self) -> None:
        with self.assertRaises(BuildGenerationError):
            BuildManifest.from_blobs(5)  # type: ignore[arg-type]

    def test_unknown_version_is_rejected(self) -> None:
        for version in (0, 2, -1, True, "1", None):
            with self.subTest(version=version):
                data = json.dumps({"version": version, "blobs": []}).encode()
                with self.assertRaises(BuildGenerationError):
                    BuildManifest.decode(data)

    def test_unknown_fields_are_rejected(self) -> None:
        for value in (
            {"version": MANIFEST_VERSION, "blobs": [], "extra": True},
            {"version": MANIFEST_VERSION, "blobs": [], "schema": 1},
        ):
            with self.subTest(value=value):
                with self.assertRaises(BuildGenerationError):
                    BuildManifest.decode(json.dumps(value).encode())

    def test_missing_fields_are_rejected(self) -> None:
        for value in (
            {"version": MANIFEST_VERSION},
            {"blobs": []},
            {},
        ):
            with self.subTest(value=value):
                with self.assertRaises(BuildGenerationError):
                    BuildManifest.decode(json.dumps(value).encode())

    def test_non_object_is_rejected(self) -> None:
        for value in ([], "text", 1, None, True):
            with self.subTest(value=value):
                with self.assertRaises(BuildGenerationError):
                    BuildManifest.decode(json.dumps(value).encode())

    def test_non_list_blobs_is_rejected(self) -> None:
        for value in ({"a": 1}, "sha256:" + "0" * 64, 5, None, True):
            with self.subTest(value=value):
                with self.assertRaises(BuildGenerationError):
                    BuildManifest.decode(
                        json.dumps({"version": MANIFEST_VERSION, "blobs": value}).encode()
                    )

    def test_duplicate_blob_identities_are_rejected(self) -> None:
        key = canonical_blob_key(identity(b"dup"))
        data = json.dumps({"version": MANIFEST_VERSION, "blobs": [key, key]}).encode()
        with self.assertRaises(BuildGenerationError):
            BuildManifest.decode(data)

    def test_out_of_order_blob_lists_are_rejected(self) -> None:
        ordered = sorted(
            (
                canonical_blob_key(identity(b"first")),
                canonical_blob_key(identity(b"second")),
            )
        )
        canonical = json.dumps(
            {"version": MANIFEST_VERSION, "blobs": ordered}
        ).encode()
        self.assertEqual(BuildManifest.decode(canonical).keys, tuple(ordered))
        out_of_order = json.dumps(
            {"version": MANIFEST_VERSION, "blobs": list(reversed(ordered))}
        ).encode()
        with self.assertRaises(BuildGenerationError):
            BuildManifest.decode(out_of_order)

    def test_noncanonical_or_unsafe_keys_are_rejected(self) -> None:
        live = identity(b"live")
        invalid = (
            "sha256:../../outside",
            "md5:" + "0" * 32,
            "sha256:abc",
            "SHA256:" + live.hex_digest(),
            "sha256:" + live.hex_digest().upper(),
            "@/etc/passwd",
            "sha256:" + live.hex_digest() + ":extra",
            1,
            None,
            True,
        )
        for key in invalid:
            with self.subTest(key=key):
                data = json.dumps(
                    {"version": MANIFEST_VERSION, "blobs": [key]}
                ).encode()
                with self.assertRaises(BuildGenerationError):
                    BuildManifest.decode(data)

    def test_non_finite_json_is_rejected_by_the_codec(self) -> None:
        for token in ("NaN", "Infinity", "-Infinity", "1e400"):
            with self.subTest(token=token):
                data = (
                    '{"version":' + token + ',"blobs":[]}'
                ).encode()
                with self.assertRaises(BuildGenerationError):
                    BuildManifest.decode(data)

    def test_duplicate_version_fields_are_rejected(self) -> None:
        for raw in (
            b'{"version":999,"version":1,"blobs":[]}',
            b'{"version":1,"version":1,"blobs":[]}',
            b'{"version":1,"blobs":[],"version":1}',
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(BuildGenerationError):
                    BuildManifest.decode(raw)

    def test_duplicate_blobs_fields_are_rejected(self) -> None:
        safe = canonical_blob_key(identity(b"safe"))
        for raw in (
            b'{"version":1,"blobs":[],"blobs":[]}',
            b'{"version":1,"blobs":["sha256:../../outside"],"blobs":[]}',
            (
                '{"version":1,"blobs":["'
                + safe
                + '"],"blobs":[]}'
            ).encode(),
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(BuildGenerationError):
                    BuildManifest.decode(raw)


class _GenerationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.ops = InjectedOps()
        self.directory = DirectoryCapability.from_path(self.ops, self.root)
        self.addCleanup(self._close_directory)
        self.lock = acquire_build_generation_lock(self.ops, self.directory)
        self.addCleanup(self._close_lock)
        self.ops.reset()

    def _close_directory(self) -> None:
        try:
            if not self.directory.closed:
                self.directory.close()
        except OSError:
            pass

    def _close_lock(self) -> None:
        try:
            if not self.lock.closed:
                self.lock.close()
        except OSError:
            pass

    def path(self, name: str) -> Path:
        return Path(self.root) / name

    def write_generation(
        self,
        number: int,
        blobs=(),
        *,
        name: str | None = None,
        data: bytes | None = None,
        mode: int = GENERATION_MODE,
    ) -> str:
        name = name if name is not None else format_generation_name(number)
        if data is None:
            data = BuildManifest.from_blobs(blobs).encode()
        target = self.path(name)
        target.write_bytes(data)
        os.chmod(target, mode)
        return name

    def write_raw(self, name: str, data: bytes, *, mode: int = GENERATION_MODE) -> str:
        target = self.path(name)
        target.write_bytes(data)
        os.chmod(target, mode)
        return name


class GenerationStateTests(_GenerationTestCase):
    def test_zero_generations_is_empty(self) -> None:
        inventory = discover_generations(self.ops, self.directory, lock=self.lock)
        self.assertIs(inventory.state, GenerationState.EMPTY)
        self.assertEqual(inventory.generations, ())
        self.assertIsNone(inventory.current)
        self.assertIsNone(inventory.previous)

    def test_one_generation_is_stable(self) -> None:
        live = identity(b"live")
        self.write_generation(3, [live])
        inventory = discover_generations(self.ops, self.directory, lock=self.lock)
        self.assertIs(inventory.state, GenerationState.STABLE)
        self.assertEqual(len(inventory.generations), 1)
        self.assertEqual(inventory.current.number, 3)
        self.assertEqual(inventory.current.manifest.blobs, (live,))
        self.assertIsNone(inventory.previous)

    def test_two_generations_is_recoverable(self) -> None:
        first = identity(b"first")
        second = identity(b"second")
        self.write_generation(1, [first])
        self.write_generation(2, [second])
        inventory = discover_generations(self.ops, self.directory, lock=self.lock)
        self.assertIs(inventory.state, GenerationState.RECOVERABLE)
        self.assertEqual(inventory.previous.number, 1)
        self.assertEqual(inventory.current.number, 2)

    def test_more_than_two_generations_fail_closed_without_mutation(self) -> None:
        for number in (1, 2, 3):
            self.write_generation(number)
        with self.assertRaises(BuildGenerationError):
            discover_generations(self.ops, self.directory, lock=self.lock)
        for number in (1, 2, 3):
            self.assertTrue(self.path(format_generation_name(number)).exists())
        self.assertEqual(self.ops.counts.get("unlinkat", 0), 0)

    def test_non_consecutive_generations_fail_closed_without_mutation(self) -> None:
        first = self.write_generation(1, [identity(b"first")])
        second = self.write_generation(3, [identity(b"third")])
        first_bytes = self.path(first).read_bytes()
        second_bytes = self.path(second).read_bytes()
        self.ops.reset()
        with self.assertRaises(BuildGenerationError):
            inspect_generations(self.ops, self.directory)
        self.assertEqual(self.path(first).read_bytes(), first_bytes)
        self.assertEqual(self.path(second).read_bytes(), second_bytes)
        self.assertEqual(self.ops.counts.get("unlinkat", 0), 0)

    def test_discovery_rejects_non_consecutive_generations_before_sync(self) -> None:
        first = self.write_generation(1, [identity(b"first")])
        second = self.write_generation(3, [identity(b"third")])
        first_bytes = self.path(first).read_bytes()
        second_bytes = self.path(second).read_bytes()
        self.ops.reset()
        with self.assertRaises(BuildGenerationError):
            discover_generations(self.ops, self.directory, lock=self.lock)
        self.assertEqual(self.ops.counts.get("fsync", 0), 0)
        self.assertEqual(self.path(first).read_bytes(), first_bytes)
        self.assertEqual(self.path(second).read_bytes(), second_bytes)
        self.assertEqual(self.ops.counts.get("unlinkat", 0), 0)

    def test_corrupt_manifest_fails_closed_without_mutation(self) -> None:
        name = self.write_raw(format_generation_name(1), b"{not json")
        with self.assertRaises(BuildGenerationError):
            discover_generations(self.ops, self.directory, lock=self.lock)
        self.assertEqual(self.path(name).read_bytes(), b"{not json")
        self.assertEqual(self.ops.counts.get("unlinkat", 0), 0)

    def test_duplicate_manifest_fields_fail_closed_before_synchronization(self) -> None:
        samples = (
            b'{"version":999,"version":1,"blobs":[]}',
            b'{"version":1,"blobs":["sha256:../../outside"],"blobs":[]}',
        )
        for raw in samples:
            with self.subTest(raw=raw):
                name = self.write_raw(format_generation_name(1), raw)
                self.ops.reset()
                with self.assertRaises(BuildGenerationError):
                    discover_generations(self.ops, self.directory, lock=self.lock)
                self.assertEqual(self.ops.counts.get("fsync", 0), 0)
                self.assertEqual(self.path(name).read_bytes(), raw)
                os.unlink(self.path(name))

    def test_unsafe_entry_fails_closed_without_mutation(self) -> None:
        outside = identity(b"outside")
        target = self.path("outside.json")
        target.write_bytes(BuildManifest.from_blobs([outside]).encode())
        os.chmod(target, GENERATION_MODE)
        link = self.path(format_generation_name(1))
        link.symlink_to(target)
        with self.assertRaises(BuildGenerationError):
            discover_generations(self.ops, self.directory, lock=self.lock)
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_bytes(), BuildManifest.from_blobs([outside]).encode())

    def test_wrong_mode_generation_fails_closed(self) -> None:
        self.write_generation(1, [identity(b"live")], mode=0o644)
        with self.assertRaises(BuildGenerationError):
            discover_generations(self.ops, self.directory, lock=self.lock)

    def test_multiply_linked_generation_fails_closed(self) -> None:
        name = self.write_generation(1, [identity(b"live")])
        os.link(self.path(name), self.path("hardlink.json"))
        with self.assertRaises(BuildGenerationError):
            discover_generations(self.ops, self.directory, lock=self.lock)

    def test_malformed_generation_name_fails_closed(self) -> None:
        malformed = self.write_raw("committed-build-abc.json", b"{}")
        with self.assertRaises(BuildGenerationError):
            discover_generations(self.ops, self.directory, lock=self.lock)
        self.assertTrue(self.path(malformed).exists())

    def test_legacy_manifest_is_ignored_and_not_inspected(self) -> None:
        outside = self.path("outside.json")
        outside.write_bytes(BuildManifest.from_blobs([identity(b"legacy")]).encode())
        os.chmod(outside, GENERATION_MODE)
        legacy = self.path(LEGACY_MANIFEST_NAME)
        legacy.symlink_to(outside)
        self.ops.reset()
        inventory = discover_generations(self.ops, self.directory, lock=self.lock)
        self.assertIs(inventory.state, GenerationState.EMPTY)
        self.assertNotIn(LEGACY_MANIFEST_NAME, self.ops.names("openat"))
        self.assertTrue(legacy.is_symlink())

    def test_inspection_grants_no_authority_without_synchronization(self) -> None:
        self.write_generation(1, [identity(b"live")])
        self.ops.reset()
        inventory = inspect_generations(self.ops, self.directory)
        self.assertIs(inventory.state, GenerationState.STABLE)
        self.assertEqual(self.ops.counts.get("fsync", 0), 0)

    def test_discovery_synchronizes_the_generation_directory(self) -> None:
        self.write_generation(1, [identity(b"live")])
        self.ops.reset()
        discover_generations(self.ops, self.directory, lock=self.lock)
        self.assertGreaterEqual(self.ops.counts.get("fsync", 0), 1)
        self.assertIn((self.directory.fd,), self.ops.arg_pairs("fsync"))

    def test_discovery_sync_failure_preserves_state(self) -> None:
        name = self.write_generation(1, [identity(b"live")])
        self.ops.failures["fsync"] = OSError(errno.EIO, "injected generation sync failure")
        with self.assertRaises(BuildGenerationError):
            discover_generations(self.ops, self.directory, lock=self.lock)
        self.assertTrue(self.path(name).exists())

    def test_discovery_requires_a_live_matching_lock(self) -> None:
        self.lock.close()
        with self.assertRaises(CapabilityError):
            discover_generations(self.ops, self.directory, lock=self.lock)

    def test_discovery_rejects_a_lock_for_another_namespace(self) -> None:
        from docker.transactions.locking import LockCapability, LockPolicy

        self.lock.close()
        other = LockCapability.acquire(
            self.ops,
            self.directory,
            BUILD_LOCK_NAME,
            namespace=BUILD_LOCK_NAMESPACE + "-other",
            policy=LockPolicy.FAIL_FAST,
        )
        self.addCleanup(other.close)
        with self.assertRaises(CapabilityError):
            discover_generations(self.ops, self.directory, lock=other)


class GenerationPublicationTests(_GenerationTestCase):
    def test_publishes_next_generation_from_empty(self) -> None:
        live = identity(b"live")
        generation = publish_generation(
            self.ops, self.directory, [live], lock=self.lock
        )
        self.assertIsInstance(generation, BuildGeneration)
        self.assertEqual(generation.number, 1)
        self.assertEqual(generation.name, format_generation_name(1))
        self.assertEqual(generation.manifest.blobs, (live,))
        target = self.path(generation.name)
        self.assertTrue(target.exists())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), GENERATION_MODE)
        self.assertEqual(target.read_bytes(), generation.manifest.encode())
        inventory = discover_generations(self.ops, self.directory, lock=self.lock)
        self.assertIs(inventory.state, GenerationState.STABLE)
        self.assertEqual(inventory.current.manifest.blobs, (live,))

    def test_publishes_max_plus_one_without_modifying_previous(self) -> None:
        first = publish_generation(
            self.ops, self.directory, [identity(b"one")], lock=self.lock
        )
        first_bytes = self.path(first.name).read_bytes()
        second = publish_generation(
            self.ops, self.directory, [identity(b"two")], lock=self.lock
        )
        self.assertEqual(second.number, first.number + 1)
        self.assertEqual(self.path(first.name).read_bytes(), first_bytes)

    def test_publication_is_durable_before_success(self) -> None:
        publish_generation(self.ops, self.directory, [identity(b"live")], lock=self.lock)
        # Discovery sync, file sync, and generation-directory sync.
        self.assertEqual(self.ops.counts.get("fsync"), 3)
        order = self.ops.order
        self.assertEqual(order[-1], "fsync")
        self.assertLess(order.index("linkat"), len(order) - 1)

    def test_overflow_fails_before_any_mutation(self) -> None:
        self.write_generation(MAX_GENERATION)
        self.ops.reset()
        with self.assertRaises(BuildGenerationError):
            publish_generation(self.ops, self.directory, [identity(b"x")], lock=self.lock)
        self.assertEqual(self.ops.counts.get("linkat", 0), 0)
        self.assertEqual(self.ops.counts.get("unlinkat", 0), 0)
        self.assertEqual(self.ops.counts.get("fsync", 0), 0)
        self.assertTrue(self.path(format_generation_name(MAX_GENERATION)).exists())

    def test_publication_rejects_non_consecutive_generations_before_mutation(self) -> None:
        first = self.write_generation(1, [identity(b"first")])
        second = self.write_generation(3, [identity(b"third")])
        self.ops.reset()
        with self.assertRaises(BuildGenerationError):
            publish_generation(
                self.ops, self.directory, [identity(b"new")], lock=self.lock
            )
        self.assertEqual(self.ops.counts.get("fsync", 0), 0)
        self.assertEqual(self.ops.counts.get("linkat", 0), 0)
        self.assertEqual(self.ops.counts.get("unlinkat", 0), 0)
        for name in self.ops.names("openat"):
            self.assertFalse(str(name).startswith(".transaction-"))
        self.assertFalse(self.path(format_generation_name(2)).exists())
        self.assertFalse(self.path(format_generation_name(4)).exists())
        self.assertTrue(self.path(first).exists())
        self.assertTrue(self.path(second).exists())

    def test_refuses_ambiguous_two_generation_state(self) -> None:
        self.write_generation(1)
        self.write_generation(2)
        with self.assertRaises(BuildGenerationError):
            publish_generation(self.ops, self.directory, [identity(b"x")], lock=self.lock)
        self.assertFalse(self.path(format_generation_name(3)).exists())

    def test_allocation_failure_publishes_nothing(self) -> None:
        self.ops.failures["openat"] = OSError(errno.EIO, "injected allocation failure")
        with self.assertRaises(BuildGenerationError):
            publish_generation(self.ops, self.directory, [identity(b"x")], lock=self.lock)
        self.assertFalse(self.path(format_generation_name(1)).exists())

    def test_write_failure_publishes_nothing(self) -> None:
        self.ops.failures["write"] = OSError(errno.EIO, "injected write failure")
        with self.assertRaises(BuildGenerationError):
            publish_generation(self.ops, self.directory, [identity(b"x")], lock=self.lock)
        self.assertFalse(self.path(format_generation_name(1)).exists())

    def test_file_fsync_failure_publishes_nothing(self) -> None:
        # Call 1 is the preliminary discovery sync; call 2 is the file sync.
        self.ops.failures["fsync"] = lambda count: (
            OSError(errno.EIO, "injected file fsync failure") if count == 2 else None
        )
        with self.assertRaises(BuildGenerationError):
            publish_generation(self.ops, self.directory, [identity(b"x")], lock=self.lock)
        self.assertFalse(self.path(format_generation_name(1)).exists())

    def test_no_clobber_commit_failure_publishes_nothing(self) -> None:
        self.ops.failures["linkat"] = OSError(errno.EIO, "injected commit failure")
        with self.assertRaises(BuildGenerationError):
            publish_generation(self.ops, self.directory, [identity(b"x")], lock=self.lock)
        self.assertFalse(self.path(format_generation_name(1)).exists())

    def test_directory_fsync_failure_is_completed_by_discovery(self) -> None:
        # Call 1 is discovery; call 3 is the generation-directory sync.
        self.ops.failures["fsync"] = lambda count: (
            OSError(errno.EIO, "injected directory fsync failure") if count == 3 else None
        )
        with self.assertRaises(BuildGenerationError):
            publish_generation(self.ops, self.directory, [identity(b"live")], lock=self.lock)
        visible = self.path(format_generation_name(1))
        self.assertTrue(visible.exists())
        # The visible generation is not authoritative until discovery syncs.
        self.ops.failures.clear()
        inventory = discover_generations(self.ops, self.directory, lock=self.lock)
        self.assertIs(inventory.state, GenerationState.STABLE)
        self.assertEqual(inventory.current.number, 1)

    def test_directory_fsync_failure_blocks_authority_until_sync(self) -> None:
        self.ops.failures["fsync"] = lambda count: (
            OSError(errno.EIO, "injected directory fsync failure") if count == 3 else None
        )
        with self.assertRaises(BuildGenerationError):
            publish_generation(self.ops, self.directory, [identity(b"live")], lock=self.lock)
        self.ops.failures["fsync"] = OSError(errno.EIO, "injected discovery sync failure")
        with self.assertRaises(BuildGenerationError):
            discover_generations(self.ops, self.directory, lock=self.lock)
        self.assertTrue(self.path(format_generation_name(1)).exists())

    def test_visible_generation_is_not_allocated_without_discovery_sync(self) -> None:
        self.ops.failures["fsync"] = lambda count: (
            OSError(errno.EIO, "injected directory fsync failure") if count == 3 else None
        )
        with self.assertRaises(BuildGenerationError):
            publish_generation(self.ops, self.directory, [identity(b"one")], lock=self.lock)
        self.assertTrue(self.path(format_generation_name(1)).exists())
        self.assertFalse(self.path(format_generation_name(2)).exists())
        self.ops.reset()
        self.ops.failures["fsync"] = lambda count: (
            OSError(errno.EIO, "injected discovery fsync failure") if count == 1 else None
        )
        with self.assertRaises(BuildGenerationError):
            publish_generation(self.ops, self.directory, [identity(b"two")], lock=self.lock)
        # The failing sync is the discovery-time generation-directory sync, so
        # no temporary allocation or publication may have happened.
        self.assertEqual(self.ops.arg_pairs("fsync"), [(self.directory.fd,)])
        self.assertFalse(self.path(format_generation_name(2)).exists())
        self.assertEqual(self.ops.counts.get("linkat", 0), 0)
        self.assertEqual(self.ops.counts.get("unlinkat", 0), 0)

    def test_visible_generation_is_completed_by_discovery_sync(self) -> None:
        self.ops.failures["fsync"] = lambda count: (
            OSError(errno.EIO, "injected directory fsync failure") if count == 3 else None
        )
        with self.assertRaises(BuildGenerationError):
            publish_generation(self.ops, self.directory, [identity(b"one")], lock=self.lock)
        self.assertTrue(self.path(format_generation_name(1)).exists())
        self.ops.reset()
        self.ops.failures.clear()
        second = publish_generation(
            self.ops, self.directory, [identity(b"two")], lock=self.lock
        )
        self.assertEqual(second.number, 2)
        self.assertTrue(self.path(format_generation_name(2)).exists())
        # The first sync is discovery of the generation directory, and it
        # precedes temporary allocation, writing, and the no-clobber commit.
        self.assertEqual(self.ops.arg_pairs("fsync")[0], (self.directory.fd,))
        first_fsync = self._first_index("fsync")
        self.assertLess(first_fsync, self._first_index("write"))
        self.assertLess(first_fsync, self._first_index("linkat"))
        self.assertLess(first_fsync, self._first_temp_allocation_index())

    def _first_index(self, method: str) -> int:
        for index, (name, _) in enumerate(self.ops.calls):
            if name == method:
                return index
        self.fail(f"no {method!r} call recorded")

    def _first_temp_allocation_index(self) -> int:
        for index, (name, args) in enumerate(self.ops.calls):
            if (
                name == "openat"
                and isinstance(args[1], str)
                and args[1].startswith(".transaction-")
            ):
                return index
        self.fail("no temporary allocation recorded")

    def test_publication_requires_a_live_matching_lock(self) -> None:
        self.lock.close()
        with self.assertRaises(CapabilityError):
            publish_generation(
                self.ops, self.directory, [identity(b"x")], lock=self.lock
            )

    def test_publication_leaves_legacy_and_unrelated_entries_untouched(self) -> None:
        legacy = self.path(LEGACY_MANIFEST_NAME)
        legacy.write_bytes(b'{"blobs":[]}')
        os.chmod(legacy, GENERATION_MODE)
        unrelated = self.path("notes.txt")
        unrelated.write_bytes(b"keep me")
        publish_generation(self.ops, self.directory, [identity(b"x")], lock=self.lock)
        self.assertEqual(legacy.read_bytes(), b'{"blobs":[]}')
        self.assertEqual(unrelated.read_bytes(), b"keep me")


if __name__ == "__main__":
    unittest.main()

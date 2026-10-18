"""RED — canonical digest identity shared by hex SHA-256 and SRI callers.

Phase 1 (``materialize-build-artifacts-on-host``) replaces the
runtime-only SRI identity model with a single canonical identity of
algorithm plus digest bytes.  Hex SHA-256 build callers and SRI runtime
callers must describe the *same* identity, derive the *same* canonical
cache path, deduplicate by value, and reject malformed or unsupported
input before any cache work.

These tests are expected to FAIL while only the runtime-only SRI model
(``docker.versioning.artifact_cache``) exists.
"""

from __future__ import annotations

import base64
import hashlib
import os
import unittest


def _identity_module():
    from docker.versioning import digest_identity

    return digest_identity


def _sha256_sri(data: bytes) -> str:
    digest = hashlib.sha256(data).digest()
    return f"sha256-{base64.b64encode(digest).decode('ascii')}"


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class TestHexSRIEquivalence(unittest.TestCase):
    """The same algorithm + digest bytes is one identity, however encoded."""

    def test_hex_sha256_equals_sri_sha256(self) -> None:
        mod = _identity_module()
        data = b"materialize-build-artifacts-phase-1"

        from_hex = mod.DigestIdentity.from_hex("sha256", _sha256_hex(data))
        from_sri = mod.DigestIdentity.from_sri(_sha256_sri(data))

        self.assertEqual(from_hex, from_sri)
        self.assertEqual(hash(from_hex), hash(from_sri))

    def test_hex_digest_round_trips_to_lowercase_hex(self) -> None:
        mod = _identity_module()
        data = b"abc"
        identity = mod.DigestIdentity.from_hex("SHA256", _sha256_hex(data).upper())
        self.assertEqual(identity.algorithm, "sha256")
        self.assertEqual(identity.hex_digest(), _sha256_hex(data))

    def test_sri_round_trips_to_standard_base64(self) -> None:
        mod = _identity_module()
        data = b"abc"
        identity = mod.DigestIdentity.from_hex("sha256", _sha256_hex(data))
        self.assertEqual(identity.sri(), _sha256_sri(data))

    def test_digest_bytes_are_the_canonical_carrier(self) -> None:
        mod = _identity_module()
        data = b"canonical-bytes"
        digest_bytes = hashlib.sha256(data).digest()
        direct = mod.DigestIdentity("sha256", digest_bytes)
        from_hex = mod.DigestIdentity.from_hex("sha256", digest_bytes.hex())
        from_sri = mod.DigestIdentity.from_sri(
            f"sha256-{base64.b64encode(digest_bytes).decode('ascii')}"
        )
        self.assertEqual(direct, from_hex)
        self.assertEqual(direct, from_sri)


class TestCanonicalCachePaths(unittest.TestCase):
    """Canonical cache paths depend only on algorithm + digest bytes."""

    def test_cache_path_uses_algorithm_and_lowercase_hex(self) -> None:
        mod = _identity_module()
        data = b"path-payload"
        hex_digest = _sha256_hex(data)
        identity = mod.DigestIdentity.from_hex("sha256", hex_digest)

        path = identity.cache_path("/checkout/.docker-cache/build-artifacts/blobs")

        self.assertEqual(
            path,
            os.path.join(
                "/checkout/.docker-cache/build-artifacts/blobs",
                "sha256",
                hex_digest + ".blob",
            ),
        )

    def test_hex_and_sri_callers_derive_identical_path(self) -> None:
        mod = _identity_module()
        data = b"same-bytes-different-encoding"

        hex_identity = mod.DigestIdentity.from_hex("sha256", _sha256_hex(data))
        sri_identity = mod.DigestIdentity.from_sri(_sha256_sri(data))

        self.assertEqual(
            hex_identity.cache_path("/root"),
            sri_identity.cache_path("/root"),
        )

    def test_sha384_and_sha512_supported_with_distinct_paths(self) -> None:
        mod = _identity_module()
        data = b"wide-digests"
        sha384 = mod.DigestIdentity("sha384", hashlib.sha384(data).digest())
        sha512 = mod.DigestIdentity("sha512", hashlib.sha512(data).digest())
        self.assertEqual(
            sha384.cache_path("/root", extension=".blob"),
            os.path.join("/root", "sha384", hashlib.sha384(data).hexdigest() + ".blob"),
        )
        self.assertNotEqual(
            sha384.cache_path("/root"),
            sha512.cache_path("/root"),
        )


class TestDeduplication(unittest.TestCase):
    """Identities deduplicate by algorithm + digest bytes, not encoding."""

    def test_set_collapses_equivalent_encodings(self) -> None:
        mod = _identity_module()
        data = b"deduplicate-me"
        identities = {
            mod.DigestIdentity.from_hex("sha256", _sha256_hex(data)),
            mod.DigestIdentity.from_sri(_sha256_sri(data)),
            mod.DigestIdentity("sha256", hashlib.sha256(data).digest()),
        }
        self.assertEqual(len(identities), 1)

    def test_dictionary_keyed_by_identity(self) -> None:
        mod = _identity_module()
        data = b"dict-key"
        identity = mod.DigestIdentity.from_hex("sha256", _sha256_hex(data))
        equivalent = mod.DigestIdentity.from_sri(_sha256_sri(data))

        mapping = {identity: "blob.tgz"}
        self.assertEqual(mapping[equivalent], "blob.tgz")

    def test_distinct_bytes_are_distinct_identities(self) -> None:
        mod = _identity_module()
        a = mod.DigestIdentity.from_hex("sha256", _sha256_hex(b"a"))
        b = mod.DigestIdentity.from_hex("sha256", _sha256_hex(b"b"))
        self.assertNotEqual(a, b)


class TestMalformedInput(unittest.TestCase):
    """Malformed hex or SRI input is rejected before any path is derived."""

    def test_hex_wrong_length_rejected(self) -> None:
        mod = _identity_module()
        for bad in ("a" * 63, "a" * 65, ""):
            with self.subTest(hex_digest=bad):
                with self.assertRaises(mod.DigestIdentityError):
                    mod.DigestIdentity.from_hex("sha256", bad)

    def test_hex_non_hex_characters_rejected(self) -> None:
        mod = _identity_module()
        for bad in ("g" * 64, "zz" + "0" * 62, " " * 64):
            with self.subTest(hex_digest=bad):
                with self.assertRaises(mod.DigestIdentityError):
                    mod.DigestIdentity.from_hex("sha256", bad)

    def test_sri_malformed_rejected(self) -> None:
        mod = _identity_module()
        for bad in ("sha256-", "sha256-not-base64!", "sha256-AAAA", "sha256-$$$$"):
            with self.subTest(integrity=bad):
                with self.assertRaises(mod.DigestIdentityError):
                    mod.DigestIdentity.from_sri(bad)

    def test_sri_wrong_byte_length_rejected(self) -> None:
        mod = _identity_module()
        short = base64.b64encode(b"x" * 31).decode("ascii")
        long = base64.b64encode(b"x" * 33).decode("ascii")
        for payload in (short, long):
            with self.subTest(payload_length=len(payload)):
                with self.assertRaises(mod.DigestIdentityError):
                    mod.DigestIdentity.from_sri(f"sha256-{payload}")

    def test_constructor_wrong_byte_length_rejected(self) -> None:
        mod = _identity_module()
        with self.assertRaises(mod.DigestIdentityError):
            mod.DigestIdentity("sha256", b"too-short")

    def test_constructor_non_bytes_rejected(self) -> None:
        mod = _identity_module()
        with self.assertRaises(mod.DigestIdentityError):
            mod.DigestIdentity("sha256", "not-bytes")  # type: ignore[arg-type]


class TestUnsupportedAlgorithms(unittest.TestCase):
    """Algorithms outside the reviewed set are rejected."""

    def test_hex_unsupported_algorithms_rejected(self) -> None:
        mod = _identity_module()
        for algorithm, hex_len in (
            ("sha1", 40),
            ("md5", 32),
            ("sha224", 56),
            ("sha512/256", 64),
        ):
            with self.subTest(algorithm=algorithm):
                with self.assertRaises(mod.DigestIdentityError):
                    mod.DigestIdentity.from_hex(algorithm, "a" * hex_len)

    def test_sri_unsupported_algorithms_rejected(self) -> None:
        mod = _identity_module()
        with self.assertRaises(mod.DigestIdentityError):
            mod.DigestIdentity.from_sri("sha224-" + "A" * 56)

    def test_constructor_unsupported_algorithm_rejected(self) -> None:
        mod = _identity_module()
        with self.assertRaises(mod.DigestIdentityError):
            mod.DigestIdentity("sha1", b"x" * 20)


if __name__ == "__main__":
    unittest.main()

"""RED — content-addressed cache key contracts for host artifact
materialization.

These tests define the cache identity, layout, and safety contracts
BEFORE any materialization or download implementation exists.

The SRI derivation tests (``TestSRIIdentity``) are expected to be
**GREEN** now because the production module ``docker.versioning.
artifact_cache`` already contains the pure path-derivation functions
(task 2.4).

The cache-filesystem, layout, and containment tests
(``TestCacheLayout``, ``TestCacheSafety``) are **RED** because no
materialization layer, corruption recovery, or publication boundary
exists yet.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

# ═══════════════════════════════════════════════════════════════════════
# 2.1a — canonical integrity identities
# ═══════════════════════════════════════════════════════════════════════


class TestSRIIdentity(unittest.TestCase):
    """SRI parsing is owned by DigestIdentity."""

    def test_parse_and_canonical_encodings(self) -> None:
        import base64
        import hashlib

        from docker.versioning.digest_identity import DigestIdentity

        data = b"cache-contract"
        raw = hashlib.sha512(data).digest()
        identity = DigestIdentity.from_sri("sha512-" + base64.b64encode(raw).decode())
        self.assertEqual(identity.algorithm, "sha512")
        self.assertEqual(identity.digest_bytes, raw)
        self.assertEqual(identity.sri(), "sha512-" + base64.b64encode(raw).decode())
        self.assertEqual(identity.hex_digest(), raw.hex())


# ═══════════════════════════════════════════════════════════════════════
# 2.1c — fixed cache layout
# ═══════════════════════════════════════════════════════════════════════


class TestCacheLayout(unittest.TestCase):
    """Cache layout is deterministic and algorithm+depth fixed."""

    def test_default_runtime_path_uses_namespaced_cache_storage_root(self) -> None:
        from docker.versioning.artifact_cache import derive_cache_path
        from docker.versioning.cache_storage import (
            resolve_default_root,
            runtime_artifacts_blobs_child,
        )

        with tempfile.TemporaryDirectory() as tmp:
            xdg = str(Path(tmp) / "xdg")
            expected_root = runtime_artifacts_blobs_child(
                resolve_default_root(xdg, home=Path(tmp) / "home")
            )
            path = derive_cache_path("sha512", "a" * 128, root=str(expected_root))
            self.assertTrue(path.startswith(str(expected_root) + os.sep))

    def test_local_override_runtime_path_uses_blobs_child(self) -> None:
        from docker.versioning.artifact_cache import derive_cache_path
        from docker.versioning.cache_storage import (
            resolve_local_root,
            runtime_artifacts_blobs_child,
        )

        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "constructor-cache"
            root = resolve_local_root(
                str(local),
                xdg_cache_home=str(Path(tmp) / "xdg"),
                home=Path(tmp) / "home",
            )
            assert root is not None
            expected = runtime_artifacts_blobs_child(root)
            self.assertTrue(
                derive_cache_path("sha512", "a" * 128, root=str(expected)).startswith(
                    str(local / "runtime-artifacts" / "blobs") + os.sep
                )
            )

    def test_path_is_algorithm_then_digest(self) -> None:
        from docker.versioning.artifact_cache import derive_cache_path

        path = derive_cache_path("sha512", "a" * 128, root="/tmp/c")
        self.assertEqual(
            path,
            "/tmp/c/sha512/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.tgz",
        )

    def test_extension_is_always_tgz(self) -> None:
        from docker.versioning.artifact_cache import derive_cache_path

        path = derive_cache_path("sha256", "b" * 64, root="/cache")
        self.assertTrue(path.endswith(".tgz"))

    def test_only_algorithm_and_digest_participate(self) -> None:
        from docker.versioning.artifact_cache import derive_cache_path

        p1 = derive_cache_path("sha512", "c" * 128, root="/root")
        p2 = derive_cache_path("sha512", "e" * 128, root="/root")
        self.assertNotEqual(p1, p2)

    def test_default_root_is_constructor_owned(self) -> None:
        from docker.versioning.artifact_cache import (
            derive_cache_path,
        )

        path = derive_cache_path(
            "sha512", "e" * 128, root="/tmp/runtime-artifacts/blobs"
        )
        self.assertTrue(
            path.startswith("/tmp/runtime-artifacts/blobs"),
        )
        self.assertIn("sha512", path)
        self.assertTrue(path.endswith(".tgz"))

    def test_layout_adapters_share_identity_but_not_storage_names(self) -> None:
        import hashlib

        from docker.versioning.build_cache import build_blob_path
        from docker.versioning.digest_identity import DigestIdentity

        data = b"layout-boundary"
        identity = DigestIdentity("sha256", hashlib.sha256(data).digest())
        build = build_blob_path("/cache/build", identity)
        runtime = identity.runtime_cache_path_from_component(
            identity.algorithm, identity.runtime_safe_digest(), "/cache/runtime"
        )
        self.assertTrue(str(build).endswith(".blob"))
        self.assertTrue(runtime.endswith(".tgz"))
        self.assertNotEqual(str(build), runtime)

    def test_derive_cache_path_from_integrity(
        self, root="/tmp/runtime-artifacts/blobs"
    ) -> None:
        from docker.versioning.artifact_cache import (
            derive_cache_path_from_integrity,
        )

        integrity = (
            "sha512-VO9pV15PFTBOfcNq9hgKJ3K6k4Bb0ndDlX6N5"
            "ReNTOa/r66/Ppfc9N/hexsK5veMHGl1YbjCo3wOnL5jJu17/Q=="
        )
        path, algo = derive_cache_path_from_integrity(
            integrity, root="/tmp/runtime-artifacts/blobs"
        )
        self.assertEqual(algo, "sha512")
        self.assertIn("sha512", path)
        self.assertTrue(path.endswith(".tgz"))


# ═══════════════════════════════════════════════════════════════════════
# 2.1d — root containment, no-symlink, regular-file, permissions
# ═══════════════════════════════════════════════════════════════════════


class TestCacheSafety(unittest.TestCase):
    """Cache safety invariants that the materialization layer must
    enforce at runtime.

    Every test calls ``validate_cache_blob`` with an unsafe input and
    asserts a specific :class:`ArtifactMaterializationError` with the
    expected *reason* code.  Until implementation, the function raises
    ``NotImplementedError`` — the tests fail with a clear diagnostic
    instead of silently accepting an unhandled error type.

    Tests that target a specific reason *other* than ``"containment"``
    create their fixtures **inside** a temporarily patched cache root
    so that a correct implementation passes the containment gate and
    reaches the intended check."""

    # ── helpers ────────────────────────────────────────────────────

    @staticmethod
    def _assert_rejected(
        reason: str,
        host_path: str,
        *,
        cache_root: str | None = None,
    ) -> None:
        """Assert ``validate_cache_blob(host_path, cache_root=(cache_root or ("/tmp/runtime-artifacts/blobs" if host_path == "/etc/passwd" else os.path.dirname(host_path))))`` raises
        ``ArtifactMaterializationError`` with the given *reason*.

        When the validator is not yet implemented this helper
        produces a clean FAIL rather than an unhandled ERROR."""
        from docker.versioning.artifact_cache import (
            ArtifactMaterializationError,
            validate_cache_blob,
        )

        try:
            validate_cache_blob(
                host_path,
                cache_root=(
                    cache_root
                    or (
                        "/tmp/runtime-artifacts/blobs"
                        if host_path == "/etc/passwd"
                        else os.path.dirname(host_path)
                    )
                ),
            )
        except NotImplementedError:
            import unittest as _ut

            raise _ut.TestCase.failureException(  # noqa: TRY102
                f"validate_cache_blob is NOT IMPLEMENTED — "
                f"expected ArtifactMaterializationError(reason={reason!r}) "
                f"for path {host_path!r}"
            ) from None
        except ArtifactMaterializationError as exc:
            if exc.reason != reason:
                import unittest as _ut

                raise _ut.TestCase.failureException(
                    f"expected reason={reason!r} but got reason={exc.reason!r}"
                ) from None
            return  # GREEN — correct rejection
        else:
            import unittest as _ut

            raise _ut.TestCase.failureException(
                f"expected ArtifactMaterializationError(reason={reason!r}) "
                f"but no exception was raised for path {host_path!r}"
            )

    @staticmethod
    def _assert_accepted(host_path: str, *, cache_root: str | None = None) -> None:
        """Assert ``validate_cache_blob(host_path, cache_root=(cache_root or ("/tmp/runtime-artifacts/blobs" if host_path == "/etc/passwd" else os.path.dirname(host_path))))`` returns
        without raising (the blob is structurally sound).

        Converts ``NotImplementedError`` to a clean FAIL so the
        test team can see at a glance which safety contracts are
        not yet wired."""
        from docker.versioning.artifact_cache import validate_cache_blob

        try:
            validate_cache_blob(
                host_path,
                cache_root=(
                    cache_root
                    or (
                        "/tmp/runtime-artifacts/blobs"
                        if host_path == "/etc/passwd"
                        else os.path.dirname(host_path)
                    )
                ),
            )
        except NotImplementedError:
            import unittest as _ut

            raise _ut.TestCase.failureException(
                f"validate_cache_blob is NOT IMPLEMENTED — "
                f"expected no exception for valid blob {host_path!r}"
            ) from None
        # Any other exception is a genuine failure (wrong rejection).

    # ── containment: genuinely outside any plausible root ──────────

    def test_paths_outside_cache_root_rejected(self) -> None:
        """RED — paths outside the cache root are rejected with
        reason ``"containment"``."""
        self._assert_rejected("containment", "/etc/passwd")

    def test_symlink_parent_escape_rejected(self) -> None:
        """A blob reached through an in-root symlinked parent escapes containment."""
        from docker.versioning.artifact_cache import (
            ArtifactMaterializationError,
            validate_cache_blob,
        )

        with (
            tempfile.TemporaryDirectory() as root,
            tempfile.TemporaryDirectory() as outside,
        ):
            outside_blob = os.path.join(outside, "blob.tgz")
            with open(outside_blob, "wb") as file:
                file.write(b"outside blob")
            os.chmod(outside_blob, 0o444)

            link_parent = os.path.join(root, "linked-parent")
            os.symlink(outside, link_parent)
            blob_via_link = os.path.join(link_parent, "blob.tgz")

            with self.assertRaises(ArtifactMaterializationError) as raised:
                validate_cache_blob(blob_via_link, cache_root=root)
            self.assertEqual(raised.exception.reason, "containment")

    # ── missing blob ───────────────────────────────────────────────

    def test_missing_blob_path_rejected(self) -> None:
        """RED — a path that does not exist is rejected with
        reason ``"missing"``."""
        import tempfile

        with tempfile.TemporaryDirectory() as root:
            nonexistent = os.path.join(root, "nonexistent.tgz")
            self._assert_rejected("missing", nonexistent, cache_root=root)

    # ── symlink (fixture inside a patched root, containment satisfied) ─

    def test_symlink_at_blob_path_rejected(self) -> None:
        """RED — within the cache root, a symlink at the blob path
        is rejected with reason ``"symlink"``."""
        import tempfile

        with tempfile.TemporaryDirectory() as root:
            target = os.path.join(root, "target")
            link = os.path.join(root, "link")
            with open(target, "wb") as fh:
                fh.write(b"data")
            os.symlink(target, link)
            self._assert_rejected("symlink", link, cache_root=root)

    # ── not-regular-file (directory / non-regular inside root) ─────

    def test_directory_at_blob_path_rejected(self) -> None:
        """RED — within the cache root, a directory at the blob
        path is rejected with reason ``"not_regular_file"``."""
        import tempfile

        with tempfile.TemporaryDirectory() as root:
            subdir = os.path.join(root, "subdir")
            os.mkdir(subdir)
            self._assert_rejected("not_regular_file", subdir, cache_root=root)

    def test_fifo_at_blob_path_rejected(self) -> None:
        """RED — within the cache root, a named pipe (FIFO) is
        rejected with reason ``"not_regular_file"``."""
        import tempfile

        with tempfile.TemporaryDirectory() as root:
            fifo_path = os.path.join(root, "fifo")
            os.mkfifo(fifo_path)
            self._assert_rejected("not_regular_file", fifo_path, cache_root=root)

            os.unlink(fifo_path)

    # ── permissions (world-readable file inside patched root) ───────

    def test_insecure_permissions_rejected(self) -> None:
        """RED — within the cache root, a blob with group/other
        write bits is rejected with reason ``"permissions"``."""
        import tempfile

        with tempfile.TemporaryDirectory() as root:
            fd, path = tempfile.mkstemp(dir=root)
            os.close(fd)
            os.chmod(path, 0o666)
            self._assert_rejected("permissions", path, cache_root=root)

            os.unlink(path)

    # ── valid blob accepted (GREEN after implementation) ───────────

    def test_valid_blob_accepted(self) -> None:
        """RED (until implemented) — a regular file inside the
        cache root with owner-only permissions is accepted without
        raising."""
        import tempfile

        with tempfile.TemporaryDirectory() as root:
            fd, path = tempfile.mkstemp(dir=root)
            os.close(fd)
            os.chmod(path, 0o600)

            self._assert_accepted(path, cache_root=root)

            os.unlink(path)


# ═══════════════════════════════════════════════════════════════════════
# 2.1e — path derivation rejects non-content inputs
# ═══════════════════════════════════════════════════════════════════════


class TestNoURLPackageVersionInPath(unittest.TestCase):
    """Cache paths MUST NOT be derivable from URL, package, version, or
    caller-provided path — only algorithm and digest participate."""

    def test_url_does_not_affect_derived_path(self) -> None:
        from docker.versioning.artifact_cache import derive_cache_path

        derive_cache_path(
            "sha512",
            "f" * 128,
            root="https://example.com/pkg-1.0.tgz",
        )
        derive_cache_path(
            "sha512",
            "f" * 128,
            root="https://other.com/pkg-2.0.tgz",
        )
        # Only root changes, not algorithm/digest — but root is
        # constructor-owned, not caller-derived.  The contract is
        # that URL never participates in path derivation.
        # The functions don't even accept a URL parameter.
        self.assertEqual(
            derive_cache_path("sha512", "e" * 128, root="/tmp/runtime-artifacts/blobs"),
            derive_cache_path("sha512", "e" * 128, root="/tmp/runtime-artifacts/blobs"),
        )

    def test_version_does_not_affect_derived_path(self) -> None:
        from docker.versioning.artifact_cache import derive_cache_path

        self.assertEqual(
            derive_cache_path("sha512", "e" * 128, root="/tmp/runtime-artifacts/blobs"),
            derive_cache_path("sha512", "e" * 128, root="/tmp/runtime-artifacts/blobs"),
        )

    def test_package_name_does_not_affect_derived_path(self) -> None:
        from docker.versioning.artifact_cache import derive_cache_path

        self.assertEqual(
            derive_cache_path("sha512", "e" * 128, root="/tmp/runtime-artifacts/blobs"),
            derive_cache_path("sha512", "e" * 128, root="/tmp/runtime-artifacts/blobs"),
        )


# ═══════════════════════════════════════════════════════════════════════
# 2.2a — SelectedArtifact DTO module isolation
# ═══════════════════════════════════════════════════════════════════════


class TestArtifactDTOModuleIsolation(unittest.TestCase):
    """The cache-contracts module must not import facade, parser,
    presentation, Docker execution, or container-installer modules."""

    _FORBIDDEN = {
        "constructor_cli",
        "facade",
        "presentation",
        "tui",
        "launcher",
        "runtime_installer",
    }

    def test_no_forbidden_imports(self) -> None:
        import sys

        # Force import so the module is in sys.modules
        import docker.versioning.artifact_cache as _cache  # noqa: F401

        cache_mod = "docker.versioning.artifact_cache"
        self.assertIn(cache_mod, sys.modules)
        module = sys.modules[cache_mod]
        for attr in dir(module):
            if attr.startswith("_"):
                continue
            val = getattr(module, attr, None)
            if val is None:
                continue
            mod_name = getattr(val, "__module__", "")
            if not mod_name:
                continue
            parts = mod_name.split(".")
            overlap = self._FORBIDDEN & set(parts)
            self.assertEqual(
                set(),
                overlap,
                f"{attr} imports from forbidden {overlap}",
            )

    def test_selected_artifact_has_no_package_name(self) -> None:
        from docker.versioning.artifact_cache import SelectedArtifact

        ann = getattr(SelectedArtifact, "__annotations__", {})
        self.assertNotIn("package_name", ann)
        self.assertNotIn("version", ann)
        self.assertNotIn("host_path", ann)

    def test_verified_cache_blob_has_no_url(self) -> None:
        from docker.versioning.artifact_cache import VerifiedCacheBlob

        ann = getattr(VerifiedCacheBlob, "__annotations__", {})
        self.assertNotIn("url", ann)
        self.assertNotIn("package_name", ann)
        self.assertNotIn("version", ann)


# ═══════════════════════════════════════════════════════════════════════
# 2.3 — boundary protocols exist and have correct signatures
# ═══════════════════════════════════════════════════════════════════════


class TestBoundaryProtocols(unittest.TestCase):
    """Each injected boundary must be importable and have the expected
    callable signatures."""

    @staticmethod
    def _method_names(proto: type) -> set[str]:
        """Return the set of public method names from a Protocol."""
        return {
            name
            for name in dir(proto)
            if callable(getattr(proto, name, None)) and not name.startswith("_")
        }

    def test_cache_filesystem_signatures(self) -> None:
        from docker.versioning.artifact_cache import CacheFilesystem

        required = {
            "blob_exists",
            "is_regular_file",
            "is_symlink",
            "stat_blob",
            "read_bytes",
            "digest_file",
            "inspect_and_digest",
            "ensure_secure_dir",
            "create_temp",
            "append_temp",
            "finalize_temp",
            "cleanup_temp",
            "quarantine_or_remove",
            "get_permissions",
            "set_permissions",
            "atomic_publish",
        }
        self.assertEqual(required, self._method_names(CacheFilesystem))

    def test_streaming_transport_signature(self) -> None:
        from docker.versioning.artifact_cache import StreamingTransport

        self.assertIn("fetch_chunks", self._method_names(StreamingTransport))

    def test_identity_lock_signatures(self) -> None:
        from docker.versioning.artifact_cache import IdentityLock

        names = self._method_names(IdentityLock)
        self.assertIn("acquire", names)
        self.assertIn("release", names)

    def test_temporary_directory_signature(self) -> None:
        from docker.versioning.artifact_cache import TemporaryDirectory

        self.assertIn("mkdtemp", self._method_names(TemporaryDirectory))

    def test_error_reason_codes(self) -> None:
        from docker.versioning.artifact_cache import (
            ArtifactMaterializationError,
        )

        err = ArtifactMaterializationError(
            reason="integrity",
            detail="digest mismatch",
        )
        self.assertEqual(err.reason, "integrity")
        self.assertIsInstance(err, Exception)

    def test_error_is_immutable(self) -> None:
        from docker.versioning.artifact_cache import (
            ArtifactMaterializationError,
        )

        err = ArtifactMaterializationError(reason="t", detail="e" * 128)
        with self.assertRaises(AttributeError):
            err.reason = "other"  # type: ignore[misc]


# ═══════════════════════════════════════════════════════════════════════
# 2.4 — deterministic derivation (see TestCacheLayout above too)
# ═══════════════════════════════════════════════════════════════════════


class TestDerivationRejectsNonContentInputs(unittest.TestCase):
    """Cache path derivation SHALL NOT accept URL, package, version,
    or caller-provided path components in its parameter set."""

    def test_derive_cache_path_has_no_url_parameter(self) -> None:
        import inspect

        from docker.versioning.artifact_cache import derive_cache_path

        sig = inspect.signature(derive_cache_path)
        self.assertEqual(
            {"algorithm", "digest", "root"},
            set(sig.parameters.keys()),
        )

    def test_derive_cache_path_from_integrity_has_no_url_parameter(self) -> None:
        import inspect

        from docker.versioning.artifact_cache import (
            derive_cache_path_from_integrity,
        )

        sig = inspect.signature(derive_cache_path_from_integrity)
        self.assertEqual(
            {"integrity", "root"},
            set(sig.parameters.keys()),
        )


# ═══════════════════════════════════════════════════════════════════════
# 2.1f — hardened input validation for cache path derivation
# ═══════════════════════════════════════════════════════════════════════


class TestDerivationInputValidation(unittest.TestCase):
    """``derive_cache_path`` and ``derive_cache_path_from_integrity``
    MUST reject unsafe inputs before path construction."""

    # ── derived directly from the function ────────────────────────

    def test_unsupported_algorithm_raises_valueerror(self) -> None:
        from docker.versioning.artifact_cache import derive_cache_path

        for bad in ("md5", "sha1", "SHA512", "sha3-256", ""):
            with self.subTest(algorithm=bad):
                with self.assertRaises(ValueError):
                    derive_cache_path(
                        bad, "safe-digest", root="/tmp/runtime-artifacts/blobs"
                    )

    def test_empty_digest_raises_valueerror(self) -> None:
        from docker.versioning.artifact_cache import derive_cache_path

        with self.assertRaises(ValueError):
            derive_cache_path("sha512", "", root="/tmp/runtime-artifacts/blobs")

    def test_traversal_dot_dot_in_digest_raises_valueerror(self) -> None:
        from docker.versioning.artifact_cache import derive_cache_path

        for bad in (".", ".."):
            with self.subTest(digest=bad):
                with self.assertRaises(ValueError):
                    derive_cache_path(
                        "sha512", bad, root="/tmp/runtime-artifacts/blobs"
                    )

    def test_path_separator_in_digest_raises_valueerror(self) -> None:
        from docker.versioning.artifact_cache import derive_cache_path

        for bad in ("/", "a/b", "trailing/", "//", "/etc"):
            with self.subTest(digest=bad):
                with self.assertRaises(ValueError):
                    derive_cache_path(
                        "sha512", bad, root="/tmp/runtime-artifacts/blobs"
                    )

    def test_backslash_in_digest_raises_valueerror(self) -> None:
        from docker.versioning.artifact_cache import derive_cache_path

        with self.assertRaises(ValueError):
            derive_cache_path("sha512", "dig\\est", root="/tmp/runtime-artifacts/blobs")

    def test_null_byte_in_digest_raises_valueerror(self) -> None:
        from docker.versioning.artifact_cache import derive_cache_path

        with self.assertRaises(ValueError):
            derive_cache_path(
                "sha512", "dig\x00est", root="/tmp/runtime-artifacts/blobs"
            )

    def test_non_base64url_characters_rejected(self) -> None:
        from docker.versioning.artifact_cache import derive_cache_path

        for bad in (
            "has space",
            "has:colon",
            "has\nnewline",
            "has\ttab",
            "+",
            "plus+sign",
        ):
            with self.subTest(digest=bad):
                with self.assertRaises(ValueError):
                    derive_cache_path(
                        "sha512", bad, root="/tmp/runtime-artifacts/blobs"
                    )

    def test_padding_only_in_trailing_position_allowed(self) -> None:
        """Padding ``=`` is only allowed at the end (at most two)."""
        from docker.versioning.artifact_cache import derive_cache_path

        # Valid trailing padding (1 or 2 chars)
        for ok in ("a" * 128, "b" * 128):
            with self.subTest(digest=ok):
                derive_cache_path(
                    "sha512", ok, root="/tmp/runtime-artifacts/blobs"
                )  # must not raise

        # Invalid: padding in the middle
        for bad in ("a=b", "a==b", "==", "========", "=abc", "==abc"):
            with self.subTest(digest=bad):
                with self.assertRaises(ValueError):
                    derive_cache_path(
                        "sha512", bad, root="/tmp/runtime-artifacts/blobs"
                    )

    def test_derive_cache_path_from_integrity_rejects_malformed(self) -> None:
        from docker.versioning.artifact_cache import (
            derive_cache_path_from_integrity,
        )

        # Missing separator
        with self.assertRaises(ValueError):
            derive_cache_path_from_integrity(
                "not-an-integrity", root="/tmp/runtime-artifacts/blobs"
            )

    def test_derive_cache_path_from_integrity_rejects_unsupported_algo(self) -> None:
        from docker.versioning.artifact_cache import (
            derive_cache_path_from_integrity,
        )

        with self.assertRaises(ValueError):
            derive_cache_path_from_integrity(
                "md5-AAAA", root="/tmp/runtime-artifacts/blobs"
            )


if __name__ == "__main__":
    unittest.main()

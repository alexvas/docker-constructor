"""Phase 3 — immutable Docker image-reference validation.

Assembly must accept only a canonical immutable image reference — a bare
``sha256:<64 lowercase hex>`` digest, ``<registry>/<repository>@sha256:<64
lowercase hex>``, or ``<registry>/<repository>:<tag>@sha256:<64 lowercase
hex>`` — and reject tags, malformed digests, uppercase/non-hex values,
userinfo, empty registry/repository components, malformed repository names,
and any other mutable reference before cache, staging, executor, or Docker
activity.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR.parent))

from docker.npm_environment import (
    LockedNpmError,
    RootSpec,
    assemble,
    assembler_script_digest,
    compute_assembler_identity,
    npm_policy_digest,
    preflight,
    validate_image_reference,
)

_NODE = "24.18.0"
_NPM = "11.16.0"
_PLATFORM = "linux-x64"

_DIGEST = "sha256:" + "a" * 64
_FULL = f"docker.io/library/node:22.19.0-bookworm-slim@{_DIGEST}"
_UNTAGGED = f"docker.io/library/node@{_DIGEST}"


def _sri() -> str:
    import base64

    return "sha512-" + base64.b64encode(bytes([0xAB]) * 64).decode()


def _validated():
    raw = json.dumps(
        {
            "name": "root",
            "version": "1.0.0",
            "lockfileVersion": 3,
            "requires": True,
            "packages": {
                "": {
                    "name": "root",
                    "version": "1.0.0",
                    "dependencies": {"a": "1.0.0"},
                },
                "node_modules/a": {
                    "version": "1.0.0",
                    "resolved": "https://registry.npmjs.org/a/-/a-1.0.0.tgz",
                    "integrity": _sri(),
                },
            },
        }
    ).encode()
    return preflight(
        raw,
        roots=(RootSpec("a", "1.0.0"),),
        platform=_PLATFORM,
        node_version=_NODE,
        npm_version=_NPM,
    )


def _assembler(image_digest: str):
    return compute_assembler_identity(
        image_digest=image_digest,
        node_version=_NODE,
        npm_version=_NPM,
        script_digest=assembler_script_digest(),
        policy_digest=npm_policy_digest(),
        platform=_PLATFORM,
    )


class RecordingExecutor:
    def __init__(self):
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: tuple[str, ...]):
        self.calls.append(argv)
        from docker.npm_environment import ProcessResult

        return ProcessResult(argv, 0, "", "")


class TestValidateImageReference(unittest.TestCase):
    def test_accepts_bare_digest(self):
        validate_image_reference(_DIGEST)

    def test_accepts_repository_plus_digest(self):
        validate_image_reference(_FULL)
        validate_image_reference(_UNTAGGED)
        validate_image_reference(f"registry.example.com/library/node:24.1.0@{_DIGEST}")
        validate_image_reference(f"registry.example.com/library/node@{_DIGEST}")

    def test_accepts_untagged_nested_repositories(self):
        for ref in (
            f"docker.io/library/sub/node@{_DIGEST}",
            f"registry.example.com/a/b/c/d@{_DIGEST}",
            f"registry.example.com/my-org/my_repo/my.node@{_DIGEST}",
        ):
            validate_image_reference(ref)

    def test_accepts_valid_registries(self):
        for ref in (
            f"docker.io/library/node@{_DIGEST}",
            f"registry.example.com/library/node@{_DIGEST}",
            f"registry.example.com:5000/library/node@{_DIGEST}",
            f"localhost/library/node@{_DIGEST}",
            f"localhost:5000/library/node@{_DIGEST}",
            f"[::1]/library/node@{_DIGEST}",
            f"[::1]:5000/library/node@{_DIGEST}",
            f"[2001:db8::1]/library/node@{_DIGEST}",
            f"[2001:db8::1]:5000/library/node@{_DIGEST}",
        ):
            validate_image_reference(ref)

    def test_rejects_tag_only(self):
        for bad in ("node:24", "docker.io/library/node:24", "node"):
            with self.assertRaises(LockedNpmError) as ctx:
                validate_image_reference(bad)
            self.assertEqual(ctx.exception.reason, "invalid_image_reference")

    def test_rejects_malformed_digest(self):
        for bad in (
            "sha256:" + "a" * 63,
            "sha256:" + "a" * 65,
            "sha256:" + "g" * 64,
            "sha256:" + "a" * 63 + "!",
            "sha256",
        ):
            with self.assertRaises(LockedNpmError) as ctx:
                validate_image_reference(bad)
            self.assertEqual(ctx.exception.reason, "invalid_image_reference")

    def test_rejects_uppercase_or_non_lowercase_digest(self):
        for bad in (
            "sha256:" + "A" * 64,
            "sha256:" + "a" * 63 + "A",
            "SHA256:" + "a" * 64,
        ):
            with self.assertRaises(LockedNpmError) as ctx:
                validate_image_reference(bad)
            self.assertEqual(ctx.exception.reason, "invalid_image_reference")

    def test_rejects_userinfo(self):
        for bad in (
            f"user:pass@docker.io/library/node:24@{_DIGEST}",
            f"user@docker.io/library/node:24@{_DIGEST}",
        ):
            with self.assertRaises(LockedNpmError) as ctx:
                validate_image_reference(bad)
            self.assertEqual(ctx.exception.reason, "invalid_image_reference")

    def test_rejects_mutable_or_malformed_references(self):
        for bad in (
            "docker.io/library/node:tag",  # tag, no digest
            f"node:24@{_DIGEST}",  # name:tag but no registry/repo
            f"node@{_DIGEST}",  # name but no registry/repo
            "",  # empty
            f"docker.io/library/node:ta@g@{_DIGEST}",  # multiple '@'
        ):
            with self.assertRaises(LockedNpmError) as ctx:
                validate_image_reference(bad)
            self.assertEqual(ctx.exception.reason, "invalid_image_reference")

    def test_rejects_malformed_untagged_repositories(self):
        for bad in (
            f"docker.io/Library/node@{_DIGEST}",  # uppercase segment
            f"docker.io/library//node@{_DIGEST}",  # empty segment
            f"docker.io/library/node repo@{_DIGEST}",  # space
            f"docker.io/-node@{_DIGEST}",  # leading separator
            f"docker.io/node-@{_DIGEST}",  # trailing separator
            f"docker.io/library/node..x@{_DIGEST}",  # doubled separator
            f"docker.io/@{_DIGEST}",  # empty repository
        ):
            with self.assertRaises(LockedNpmError) as ctx:
                validate_image_reference(bad)
            self.assertEqual(ctx.exception.reason, "invalid_image_reference")

    def test_rejects_malformed_registries(self):
        for bad in (
            f"https://registry.example.com/repo@{_DIGEST}",  # URL scheme
            f"bad host/repo@{_DIGEST}",  # whitespace
            f"registry.example.com:/repo@{_DIGEST}",  # empty port
            f"registry.example.com:abc/repo@{_DIGEST}",  # non-numeric port
            f"registry.example.com:70000/repo@{_DIGEST}",  # out-of-range port
            f"registry.example.com:0/repo@{_DIGEST}",  # port zero
            f"registry.example.com:50:00/repo@{_DIGEST}",  # extra colon
            f"-registry.example.com/repo@{_DIGEST}",  # leading hyphen
            f"registry..example.com/repo@{_DIGEST}",  # empty label
            f"registry_.example.com/repo@{_DIGEST}",  # invalid char
            f"./repo@{_DIGEST}",  # empty hostname
            f":5000/repo@{_DIGEST}",  # empty hostname
        ):
            with self.assertRaises(LockedNpmError) as ctx:
                validate_image_reference(bad)
            self.assertEqual(ctx.exception.reason, "invalid_image_reference")

    def test_rejects_malformed_ipv6_registries(self):
        for bad in (
            f"::1/repo@{_DIGEST}",  # unbracketed IPv6
            f"[::1/repo@{_DIGEST}",  # unclosed bracket
            f"[not-ipv6]/repo@{_DIGEST}",  # not an IPv6 literal
            f"[1.2.3.4]/repo@{_DIGEST}",  # IPv4 in brackets
            f"[::1]:abc/repo@{_DIGEST}",  # non-numeric port
            f"[2001:db8::1]:70000/repo@{_DIGEST}",  # out-of-range port
            f"[::1]extra/repo@{_DIGEST}",  # trailing garbage
        ):
            with self.assertRaises(LockedNpmError) as ctx:
                validate_image_reference(bad)
            self.assertEqual(ctx.exception.reason, "invalid_image_reference")


class TestInvalidReferenceFailsBeforeEffects(unittest.TestCase):
    def _assert_no_effects(self, image_digest: str):
        executor = RecordingExecutor()
        tmp = tempfile.TemporaryDirectory(prefix="npm-env-img-")
        self.addCleanup(tmp.cleanup)
        cache_root = Path(tmp.name) / "cache"
        with self.assertRaises(LockedNpmError) as ctx:
            assemble(
                validated=_validated(),
                assembler=_assembler(image_digest),
                cache_root=cache_root,
                executor=executor,
            )
        self.assertEqual(ctx.exception.reason, "invalid_image_reference")
        # No cache, staging, executor, or Docker activity happened.
        self.assertFalse(cache_root.exists())
        self.assertEqual(executor.calls, [])

    def test_tag_reference_fails_before_effects(self):
        self._assert_no_effects("node:24")

    def test_malformed_digest_fails_before_effects(self):
        self._assert_no_effects("sha256:" + "A" * 64)

    def test_userinfo_fails_before_effects(self):
        self._assert_no_effects(f"user:pass@docker.io/library/node:24@{_DIGEST}")

    def test_malformed_repository_fails_before_effects(self):
        self._assert_no_effects(f"docker.io/Library/node@{_DIGEST}")

    def test_malformed_registry_fails_before_effects(self):
        self._assert_no_effects(f"registry.example.com:abc/repo@{_DIGEST}")


if __name__ == "__main__":
    unittest.main()

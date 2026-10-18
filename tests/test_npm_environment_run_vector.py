"""Phase 3 — deterministic Docker run vector (task 3.1).

Docker-backed assembly must accept only a preflight-produced
:class:`ValidatedAssemblyInput`, reject changed lock bytes, roots, platform,
or reviewed tool versions before any effect, and render a run vector with a
pinned image digest, numeric UID/GID, private HOME, read-only inputs, opaque
cache, writable staging, and no consumer mounts.
"""

from __future__ import annotations

import dataclasses
import json
import sys
import unittest
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR.parent))

from docker.npm_environment import (
    DockerRunVector,
    LockedNpmError,
    RootSpec,
    assembler_script_bytes,
    assembler_script_digest,
    compute_assembler_identity,
    npm_policy_digest,
    npm_policy_env,
    preflight,
    render_docker_argv,
    render_run_vector,
)

_IMAGE = "sha256:" + "a" * 64
_NODE = "24.18.0"
_NPM = "11.16.0"
_PLATFORM = "linux-x64"

_STAGING = Path("/tmp/npm-env-staging-3-1")
_NPM_CACHE = Path("/tmp/npm-env-cache-3-1")


def _sri() -> str:
    import base64

    return "sha512-" + base64.b64encode(bytes([0xAB]) * 64).decode()


def _url(name: str, version: str) -> str:
    stem = name.rsplit("/", 1)[-1]
    return f"https://registry.npmjs.org/{name}/-/{stem}-{version}.tgz"


def _lock(roots: dict[str, str]) -> bytes:
    pkg_nodes = {
        f"node_modules/{name}": {
            "version": version,
            "resolved": _url(name, version),
            "integrity": _sri(),
        }
        for name, version in roots.items()
    }
    return json.dumps(
        {
            "name": "root",
            "version": "1.0.0",
            "lockfileVersion": 3,
            "requires": True,
            "packages": {
                "": {"name": "root", "version": "1.0.0", "dependencies": roots},
                **pkg_nodes,
            },
        }
    ).encode()


def _validated(roots: dict[str, str]):
    raw = _lock(roots)
    validated = preflight(
        raw,
        roots=tuple(RootSpec(n, v) for n, v in sorted(roots.items())),
        platform=_PLATFORM,
        node_version=_NODE,
        npm_version=_NPM,
    )
    return raw, validated


def _assembler(**overrides: str):
    kwargs = {
        "image_digest": _IMAGE,
        "node_version": _NODE,
        "npm_version": _NPM,
        "script_digest": assembler_script_digest(),
        "policy_digest": npm_policy_digest(),
        "platform": _PLATFORM,
    }
    kwargs.update(overrides)
    return compute_assembler_identity(**kwargs)


def _vector(*, validated=None, assembler=None, **overrides):
    if validated is None:
        _raw, validated = _validated({"a": "1.0.0"})
    if assembler is None:
        assembler = _assembler()
    kwargs = {
        "validated": validated,
        "assembler": assembler,
        "staging": _STAGING,
        "npm_cache": _NPM_CACHE,
        "uid": 1000,
        "gid": 1000,
        "name": "npm-assembler-test",
    }
    kwargs.update(overrides)
    return render_run_vector(**kwargs)


class TestBindingRechecks(unittest.TestCase):
    def test_changed_lock_bytes_rejected(self):
        _raw, validated = _validated({"a": "1.0.0"})
        changed = dataclasses.replace(validated, lockfile_bytes=b'{"x": 1}')
        with self.assertRaises(LockedNpmError) as ctx:
            _vector(validated=changed)
        self.assertEqual(ctx.exception.reason, "lockfile_bytes_mismatch")

    def test_changed_roots_rejected(self):
        _raw, validated = _validated({"a": "1.0.0", "b": "1.0.0"})
        changed = dataclasses.replace(validated, roots=tuple(reversed(validated.roots)))
        with self.assertRaises(LockedNpmError) as ctx:
            _vector(validated=changed)
        self.assertEqual(ctx.exception.reason, "validated_input_mismatch")

    def test_changed_platform_rejected(self):
        _raw, validated = _validated({"a": "1.0.0"})
        changed = dataclasses.replace(validated, platform="darwin-arm64")
        with self.assertRaises(LockedNpmError) as ctx:
            _vector(validated=changed)
        self.assertEqual(ctx.exception.reason, "platform_mismatch")

    def test_changed_node_version_rejected(self):
        _raw, validated = _validated({"a": "1.0.0"})
        changed = dataclasses.replace(validated, node_version="20.0.0")
        with self.assertRaises(LockedNpmError) as ctx:
            _vector(validated=changed)
        self.assertEqual(ctx.exception.reason, "node_version_mismatch")

    def test_changed_npm_version_rejected(self):
        _raw, validated = _validated({"a": "1.0.0"})
        changed = dataclasses.replace(validated, npm_version="10.0.0")
        with self.assertRaises(LockedNpmError) as ctx:
            _vector(validated=changed)
        self.assertEqual(ctx.exception.reason, "npm_version_mismatch")

    def test_script_digest_mismatch_rejected(self):
        _raw, validated = _validated({"a": "1.0.0"})
        assembler = _assembler(script_digest="forged-script-digest")
        with self.assertRaises(LockedNpmError) as ctx:
            _vector(validated=validated, assembler=assembler)
        self.assertEqual(ctx.exception.reason, "script_digest_mismatch")

    def test_policy_digest_mismatch_rejected(self):
        _raw, validated = _validated({"a": "1.0.0"})
        assembler = _assembler(policy_digest="forged-policy-digest")
        with self.assertRaises(LockedNpmError) as ctx:
            _vector(validated=validated, assembler=assembler)
        self.assertEqual(ctx.exception.reason, "policy_digest_mismatch")


class TestRunVectorShape(unittest.TestCase):
    def test_pinned_image_digest(self):
        self.assertEqual(_vector().image, _IMAGE)

    def test_numeric_uid_gid(self):
        self.assertEqual(_vector(uid=1234, gid=5678).user, "1234:5678")

    def test_private_home(self):
        home = _vector().home
        self.assertIsInstance(home, str)
        self.assertTrue(home.startswith("/"))
        self.assertNotEqual(home, "/")
        self.assertNotEqual(home, "/root")

    def test_explicit_environment(self):
        env = dict(_vector().env)
        self.assertEqual(
            set(env),
            {
                "HOME",
                "npm_config_cache",
                "REVIEWED_NODE_VERSION",
                "REVIEWED_NPM_VERSION",
            }
            | {name for name, _value in npm_policy_env()},
        )
        self.assertEqual(env["REVIEWED_NODE_VERSION"], _NODE)
        self.assertEqual(env["REVIEWED_NPM_VERSION"], _NPM)
        # The four reviewed npm fetch limits are rendered deterministically.
        for name, value in npm_policy_env():
            self.assertEqual(env[name], value, name)

    def test_read_only_inputs(self):
        mounts = _vector().mounts
        lock = [m for m in mounts if m.container.endswith("package-lock.json")]
        self.assertEqual(len(lock), 1)
        self.assertEqual(lock[0].mode, "ro")

    def test_opaque_cache_mount(self):
        mounts = _vector().mounts
        cache = [m for m in mounts if m.host == str(_NPM_CACHE)]
        self.assertEqual(len(cache), 1)
        self.assertEqual(cache[0].mode, "rw")
        self.assertEqual(cache[0].container, "/cache")

    def test_writable_staging_mount(self):
        mounts = _vector().mounts
        staging = [m for m in mounts if m.host == str(_STAGING)]
        self.assertEqual(len(staging), 1)
        self.assertEqual(staging[0].mode, "rw")
        self.assertEqual(staging[0].container, "/work")

    def test_no_consumer_mounts(self):
        mounts = _vector().mounts
        self.assertEqual(len(mounts), 3)
        allowed = {str(_STAGING), str(_NPM_CACHE), str(_STAGING / "package-lock.json")}
        for mount in mounts:
            self.assertIn(mount.host, allowed, mount.host)

    def test_command_is_embedded_script(self):
        command = _vector().command
        self.assertEqual(command[0], "/bin/sh")
        self.assertEqual(command[1], "-c")
        self.assertEqual(command[2], assembler_script_bytes().decode("utf-8"))

    def test_render_docker_argv(self):
        vector = _vector()
        argv = render_docker_argv(vector)
        self.assertEqual(argv[0], "docker")
        self.assertEqual(argv[1], "run")
        self.assertIn("--rm", argv)
        self.assertIn("--user", argv)
        self.assertIn("--name", argv)
        self.assertIn("--workdir", argv)
        self.assertIn(vector.image, argv)
        # The executable command bytes match the displayed script bytes.
        self.assertIn(assembler_script_bytes().decode("utf-8"), argv)

    def test_vector_is_immutable(self):
        vector = _vector()
        with self.assertRaises((dataclasses.FrozenInstanceError, AttributeError)):
            vector.image = "sha256:" + "b" * 64  # type: ignore[misc]

    def test_vector_type_exposes_no_output_identity(self):
        fields = {f.name for f in dataclasses.fields(DockerRunVector)}
        for banned in ("output_identity", "tree_digest", "evidence_digest"):
            self.assertNotIn(banned, fields)


if __name__ == "__main__":
    unittest.main()

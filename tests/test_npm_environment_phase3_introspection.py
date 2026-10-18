"""Phase 3 — execution-boundary introspection (task 3.7).

The displayed (structured) run vector and the executable Docker argv must
carry identical environment, mounts, image, and script bytes; every ambient
or consumer-specific input must be removed.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import sys
import unittest
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR.parent))

# Repository/test path setup must run before these imports.
import docker.npm_environment.assembler as assembler_module  # noqa: E402
import docker.npm_environment.run_vector as run_vector_module  # noqa: E402
from docker.npm_environment import (  # noqa: E402
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
_STAGING = Path("/tmp/npm-env-staging-3-7")
_CACHE = Path("/tmp/npm-env-cache-3-7")


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


def _assembler():
    return compute_assembler_identity(
        image_digest=_IMAGE,
        node_version=_NODE,
        npm_version=_NPM,
        script_digest=assembler_script_digest(),
        policy_digest=npm_policy_digest(),
        platform=_PLATFORM,
    )


def _vector():
    return render_run_vector(
        validated=_validated(),
        assembler=_assembler(),
        staging=_STAGING,
        npm_cache=_CACHE,
        uid=1000,
        gid=1000,
        name="npm-assembler-introspect",
    )


class TestDisplayedMatchesExecutable(unittest.TestCase):
    def test_argv_embeds_identical_env_mounts_image_and_script(self):
        vector = _vector()
        argv = render_docker_argv(vector)
        self.assertIn(vector.image, argv)
        for key, value in vector.env:
            self.assertIn(f"{key}={value}", argv)
        for mount in vector.mounts:
            self.assertIn(f"{mount.host}:{mount.container}:{mount.mode}", argv)
        self.assertIn(assembler_script_bytes().decode("utf-8"), argv)

    def test_env_is_closed_and_explicit(self):
        vector = _vector()
        keys = [k for k, _v in vector.env]
        self.assertEqual(
            keys,
            [
                "HOME",
                "npm_config_cache",
                "REVIEWED_NODE_VERSION",
                "REVIEWED_NPM_VERSION",
            ]
            + [name for name, _value in npm_policy_env()],
        )

    def test_script_digest_is_sha256_of_script_bytes(self):
        self.assertEqual(
            assembler_script_digest(),
            hashlib.sha256(assembler_script_bytes()).hexdigest(),
        )

    def test_no_ambient_environment_read(self):
        for module in (assembler_module, run_vector_module):
            src = inspect.getsource(module)
            self.assertNotIn("os.environ", src, module.__name__)
            self.assertNotIn("os.getenv", src, module.__name__)
            self.assertNotIn("os.environb", src, module.__name__)

    def test_no_consumer_specific_inputs(self):
        vector = _vector()
        argv = render_docker_argv(vector)
        self.assertNotIn("--env-file", argv)
        self.assertNotIn("--network", argv)
        # Every volume is one of the three narrow mounts.
        self.assertEqual(len(vector.mounts), 3)
        for token in argv:
            if token.startswith("/") and ":" in token:
                host = token.split(":", 1)[0]
                self.assertIn(host, {m.host for m in vector.mounts}, token)


if __name__ == "__main__":
    unittest.main()

"""Phase 1 — reviewed assembler limit policy and identity.

The standalone npm assembler runs under one fixed reviewed policy: finite
npm request/retry limits (timeout, retry count, minimum and maximum retry
delay) plus a reviewed total assembly duration reserved for Phase 3
deadline enforcement.  Every limit is represented canonically in the
policy digest, so changing any one of them changes assembler identity and
invalidates outputs assembled under the prior policy.  The npm
request/retry settings are rendered only into the assembler container
environment — never into command displays, evidence, or output trees — and
no user-facing override is accepted.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import sys
import tempfile
import unittest
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR.parent))

from docker.npm_environment import (
    ASSEMBLY_TOTAL_TIMEOUT_SECONDS,
    NPM_CI_FLAGS,
    NPM_REQUEST_TIMEOUT_MS,
    NPM_RETRY_COUNT,
    NPM_RETRY_MAX_TIMEOUT_MS,
    NPM_RETRY_MIN_TIMEOUT_MS,
    CorporateNetworkPolicy,
    RootSpec,
    assemble,
    assembler_script_digest,
    compute_assembler_identity,
    compute_assembler_input_identity,
    npm_policy,
    npm_policy_digest,
    npm_policy_env,
    preflight,
    prepare_assembler_namespace,
    publish_environment,
    redact_docker_argv,
    redact_run_vector,
    render_docker_argv,
    render_run_vector,
    verify_output,
)

_IMAGE = "sha256:" + "a" * 64
_NODE = "24.18.0"
_NPM = "11.16.0"
_PLATFORM = "linux-x64"
_STAGING = Path("/tmp/npm-env-staging-policy")
_NPM_CACHE = Path("/tmp/npm-env-cache-policy")

_PROXY = "http://proxy.example.test:3128"
_TRUST_BUNDLE = "/corp/trust/bundle.crt"

#: The five reviewed limit keys carried by the canonical policy payload.
_LIMIT_KEYS = (
    "request_timeout_ms",
    "retry_count",
    "retry_min_timeout_ms",
    "retry_max_timeout_ms",
    "total_timeout_seconds",
)


def _sri() -> str:
    import base64

    return "sha512-" + base64.b64encode(bytes([0xAB]) * 64).decode()


def _lock(roots: dict[str, str]) -> bytes:
    pkg_nodes = {
        f"node_modules/{name}": {
            "version": version,
            "resolved": f"https://registry.npmjs.org/{name}/-/{name}-{version}.tgz",
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


def _validated(roots: dict[str, str] | None = None):
    roots = roots or {"a": "1.0.0"}
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


def _vector(*, corporate_network: CorporateNetworkPolicy | None = None):
    _raw, validated = _validated()
    return render_run_vector(
        validated=validated,
        assembler=_assembler(),
        staging=_STAGING,
        npm_cache=_NPM_CACHE,
        uid=1000,
        gid=1000,
        name="npm-assembler-policy",
        corporate_network=corporate_network,
    )


def _canonical_digest(obj: object) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _write_tree(root: Path) -> None:
    (root / "node_modules" / "a").mkdir(parents=True)
    (root / "node_modules" / "a" / "package.json").write_text(
        json.dumps({"name": "a", "version": "1.0.0"})
    )
    root.joinpath("package.json").write_text(
        json.dumps({"name": "root", "version": "1.0.0"})
    )


class TestReviewedFiniteLimits(unittest.TestCase):
    """Task 1.1 — explicit finite reviewed policy constants."""

    def test_request_timeout_is_finite_positive_milliseconds(self):
        self.assertIsInstance(NPM_REQUEST_TIMEOUT_MS, int)
        self.assertNotIsInstance(NPM_REQUEST_TIMEOUT_MS, bool)
        self.assertGreater(NPM_REQUEST_TIMEOUT_MS, 0)

    def test_retry_count_is_finite_nonnegative(self):
        self.assertIsInstance(NPM_RETRY_COUNT, int)
        self.assertNotIsInstance(NPM_RETRY_COUNT, bool)
        self.assertGreaterEqual(NPM_RETRY_COUNT, 0)

    def test_retry_min_max_delays_are_finite_and_ordered(self):
        self.assertIsInstance(NPM_RETRY_MIN_TIMEOUT_MS, int)
        self.assertIsInstance(NPM_RETRY_MAX_TIMEOUT_MS, int)
        self.assertGreater(NPM_RETRY_MIN_TIMEOUT_MS, 0)
        self.assertLessEqual(
            NPM_RETRY_MIN_TIMEOUT_MS,
            NPM_RETRY_MAX_TIMEOUT_MS,
        )

    def test_total_assembly_duration_is_finite_positive_seconds(self):
        self.assertIsInstance(ASSEMBLY_TOTAL_TIMEOUT_SECONDS, int)
        self.assertNotIsInstance(ASSEMBLY_TOTAL_TIMEOUT_SECONDS, bool)
        self.assertGreater(ASSEMBLY_TOTAL_TIMEOUT_SECONDS, 0)

    def test_policy_payload_binds_flags_and_every_limit(self):
        policy = npm_policy()
        self.assertEqual(policy["flags"], list(NPM_CI_FLAGS))
        for key in _LIMIT_KEYS:
            self.assertIn(key, policy)
            self.assertIsInstance(policy[key], int)
            self.assertNotIsInstance(policy[key], bool)


class TestPolicyIdentity(unittest.TestCase):
    """Task 1.2 — canonical representation and identity invalidation."""

    def test_digest_is_canonical_sha256_of_policy_payload(self):
        # Each limit is represented canonically: the digest is exactly the
        # SHA-256 of the key-sorted policy payload.
        self.assertEqual(npm_policy_digest(), _canonical_digest(npm_policy()))
        self.assertEqual(len(npm_policy_digest()), 64)

    def test_each_limit_changes_policy_digest(self):
        base = npm_policy_digest()
        for key in _LIMIT_KEYS:
            changed = dict(npm_policy())
            changed[key] = changed[key] + 1
            self.assertNotEqual(base, _canonical_digest(changed), key)

    def test_each_limit_changes_assembler_identity(self):
        base = _assembler()
        for key in _LIMIT_KEYS:
            changed = dict(npm_policy())
            changed[key] = changed[key] + 1
            self.assertNotEqual(
                base.digest,
                _assembler(policy_digest=_canonical_digest(changed)).digest,
                key,
            )

    def test_prior_policy_output_does_not_validate_under_changed_policy(self):
        tmp = tempfile.TemporaryDirectory(prefix="npm-env-policy-")
        self.addCleanup(tmp.cleanup)
        cache_root = Path(tmp.name) / "cache"
        cache_root.mkdir()

        _raw, validated = _validated()
        assembler = _assembler()
        namespace = prepare_assembler_namespace(cache_root, assembler.digest)
        input_identity = compute_assembler_input_identity(validated, assembler)

        tree = Path(tmp.name) / "tree"
        tree.mkdir()
        _write_tree(tree)
        published = publish_environment(
            validated=validated,
            tree_root=tree,
            namespace=namespace,
            input_identity=input_identity,
        )

        # A changed limit (retry count) changes the policy digest and hence
        # the assembler and input identities; the prior output must not
        # validate under the changed policy.
        changed = dict(npm_policy())
        changed["retry_count"] = changed["retry_count"] + 1
        new_assembler = _assembler(policy_digest=_canonical_digest(changed))
        new_input_identity = compute_assembler_input_identity(
            validated,
            new_assembler,
        )
        self.assertNotEqual(
            new_input_identity.digest,
            input_identity.digest,
        )
        self.assertIsNone(
            verify_output(
                namespace,
                published.output_identity,
                input_identity=new_input_identity,
            )
        )


class TestPolicyRendering(unittest.TestCase):
    """Task 1.3 — limits reach only the assembler; no user override."""

    def test_npm_limits_reach_the_assembler_environment(self):
        vector = _vector()
        env = dict(vector.env)
        for name, value in npm_policy_env():
            self.assertEqual(env.get(name), value, name)
        # The reviewed total assembly duration is not rendered as an npm
        # environment variable; only the four npm request/retry settings are.
        self.assertEqual(len(npm_policy_env()), 4)

    def test_npm_limits_render_into_executable_argv(self):
        argv = render_docker_argv(_vector())
        for name, value in npm_policy_env():
            self.assertIn(f"--env", argv)
            self.assertIn(f"{name}={value}", argv)

    def test_npm_limits_are_policy_not_local_network_config(self):
        # The fixed npm limits are never redacted away; the local corporate
        # proxy/trust configuration is.
        vector = _vector(
            corporate_network=CorporateNetworkPolicy(
                proxy_url=_PROXY,
                corporate_trust_bundle=_TRUST_BUNDLE,
            )
        )
        argv = render_docker_argv(vector)
        for name, value in npm_policy_env():
            self.assertIn(f"{name}={value}", argv)

        redacted_vector = redact_run_vector(
            vector,
            (_PROXY, _TRUST_BUNDLE),
        )
        redacted_argv = redact_docker_argv(argv, (_PROXY, _TRUST_BUNDLE))
        for secret in (_PROXY, _TRUST_BUNDLE):
            self.assertNotIn(secret, json.dumps(list(redacted_vector.env)))
            self.assertNotIn(secret, json.dumps(list(redacted_argv)))
        # The fixed limits survive redaction as policy constants.
        for name, value in npm_policy_env():
            self.assertIn((name, value), redacted_vector.env)

    def test_output_tree_is_free_of_network_configuration(self):
        tmp = tempfile.TemporaryDirectory(prefix="npm-env-policy-tree-")
        self.addCleanup(tmp.cleanup)
        cache_root = Path(tmp.name) / "cache"
        cache_root.mkdir()

        _raw, validated = _validated()
        assembler = _assembler()
        namespace = prepare_assembler_namespace(cache_root, assembler.digest)
        input_identity = compute_assembler_input_identity(validated, assembler)

        tree = Path(tmp.name) / "tree"
        tree.mkdir()
        _write_tree(tree)
        published = publish_environment(
            validated=validated,
            tree_root=tree,
            namespace=namespace,
            input_identity=input_identity,
        )

        for path in published.environment_root.rglob("*"):
            if path.is_file():
                for token in (_PROXY, _TRUST_BUNDLE, "npm_config_fetch_timeout"):
                    self.assertNotIn(token.encode(), path.read_bytes())

    def test_no_user_facing_override_is_accepted(self):
        import docker.npm_environment.assembler as assembler_module

        # The limits are fixed module-level constants, never read from the
        # ambient environment.
        src = inspect.getsource(assembler_module)
        self.assertNotIn("os.environ", src)
        self.assertNotIn("os.getenv", src)
        self.assertNotIn("os.environb", src)

        # No assembler boundary accepts a limit override parameter.
        for func in (
            compute_assembler_identity,
            preflight,
            render_run_vector,
            assemble,
        ):
            params = set(inspect.signature(func).parameters)
            self.assertFalse(
                params
                & {
                    "timeout",
                    "retry",
                    "deadline",
                    "duration",
                    "fetch_timeout",
                    "total_timeout",
                },
                f"{func.__name__} exposes a limit override",
            )

        # Neither the reviewed inventory nor its TOML surface declares a
        # user-facing npm limit field.
        import docker.versioning.inventory as inventory_module
        import docker.versioning.model as model_module

        toml = Path(__file__).resolve().parents[1] / "docker-constructor.toml"
        toml_text = toml.read_text()
        for key in (
            "fetch_timeout",
            "fetch_timeout_ms",
            "npm_retry",
            "npm_timeout",
            "assembly_timeout",
            "total_timeout",
        ):
            self.assertNotIn(key, inspect.getsource(model_module))
            self.assertNotIn(key, inspect.getsource(inventory_module))
            self.assertNotIn(key, toml_text)


if __name__ == "__main__":
    unittest.main()

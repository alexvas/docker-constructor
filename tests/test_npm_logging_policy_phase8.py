"""Phase 8 — accepted npm ``loglevel=http`` production policy.

Task 8.10 applies the Phase 6 accepted decision atomically: the canonical
reviewed npm policy and exact invocation gain ``--loglevel=http``, which
changes the policy digest, the assembler script digest, the assembler
evidence identity, and the assembled-output/cache identity, so no output
assembled under the prior policy can be reused.  No rejection branch that
preserves the prior command or identity remains.
"""
from __future__ import annotations

import base64
import hashlib
import json
import unittest

from docker.npm_environment import (
    NPM_CI_FLAGS,
    LockedNpmError,
    assembler_script_bytes,
    assembler_script_digest,
    compute_assembler_identity,
    compute_assembler_input_identity,
    evidence_body_digest,
    npm_policy,
    npm_policy_digest,
    npm_policy_flags,
    preflight,
    recheck_assembler_bindings,
    RootSpec,
)
from docker.npm_environment.assembler import ASSEMBLER_SCRIPT, NPM_CI_COMMAND
from docker.npm_environment.evidence import AssemblerEvidenceBody

_IMAGE = "sha256:" + "a" * 64
_NODE = "24.18.0"
_NPM = "11.16.0"
_PLATFORM = "linux-x64"

_PRIOR_FLAGS = (
    "--ignore-scripts",
    "--no-bin-links",
    "--no-audit",
    "--no-fund",
)


def _canonical_digest(policy: dict) -> str:
    payload = json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _validated():
    lock = json.dumps(
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
                    "integrity": "sha512-"
                    + base64.b64encode(bytes([0xAB]) * 64).decode(),
                },
            },
        }
    ).encode()
    return preflight(
        lock,
        roots=(RootSpec("a", "1.0.0"),),
        platform=_PLATFORM,
        node_version=_NODE,
        npm_version=_NPM,
    )


def _assembler(policy_digest: str, script_digest: str):
    return compute_assembler_identity(
        image_digest=_IMAGE,
        node_version=_NODE,
        npm_version=_NPM,
        script_digest=script_digest,
        policy_digest=policy_digest,
        platform=_PLATFORM,
    )


class TestAcceptedNpmLoggingPolicy(unittest.TestCase):
    def test_command_and_script_include_accepted_loglevel(self):
        self.assertIn("--loglevel=http", NPM_CI_FLAGS)
        self.assertIn("--loglevel=http", NPM_CI_COMMAND)
        self.assertIn("--loglevel=http", ASSEMBLER_SCRIPT)
        self.assertIn("--loglevel=http", npm_policy_flags())
        self.assertIn("--loglevel=http", npm_policy()["flags"])

    def test_policy_digest_binds_accepted_flag(self):
        self.assertEqual(npm_policy_digest(), _canonical_digest(npm_policy()))
        prior = dict(npm_policy())
        prior["flags"] = list(_PRIOR_FLAGS)
        self.assertNotEqual(npm_policy_digest(), _canonical_digest(prior))

    def test_script_digest_binds_accepted_command(self):
        self.assertEqual(
            assembler_script_digest(),
            hashlib.sha256(assembler_script_bytes()).hexdigest(),
        )

    def test_evidence_identity_binds_accepted_flags(self):
        validated = _validated()
        assembler = _assembler(npm_policy_digest(), assembler_script_digest())
        input_identity = compute_assembler_input_identity(validated, assembler)
        accepted = AssemblerEvidenceBody(
            input_identity=input_identity,
            tree_digest="t" * 64,
            tree_entries=(),
            packages=(),
            omitted_optionals=(),
            integrity_less=(),
            root_metadata=(),
            npm_policy_flags=npm_policy_flags(),
        )
        prior = AssemblerEvidenceBody(
            input_identity=input_identity,
            tree_digest="t" * 64,
            tree_entries=(),
            packages=(),
            omitted_optionals=(),
            integrity_less=(),
            root_metadata=(),
            npm_policy_flags=_PRIOR_FLAGS,
        )
        self.assertNotEqual(evidence_body_digest(accepted), evidence_body_digest(prior))

    def test_prior_policy_assembler_identity_is_rejected(self):
        prior_policy_digest = _canonical_digest(
            {**npm_policy(), "flags": list(_PRIOR_FLAGS)}
        )
        prior = _assembler(prior_policy_digest, assembler_script_digest())
        with self.assertRaises(LockedNpmError):
            recheck_assembler_bindings(prior)
        current = _assembler(npm_policy_digest(), assembler_script_digest())
        recheck_assembler_bindings(current)

    def test_prior_policy_input_and_cache_identity_differ(self):
        validated = _validated()
        prior_policy_digest = _canonical_digest(
            {**npm_policy(), "flags": list(_PRIOR_FLAGS)}
        )
        prior = _assembler(prior_policy_digest, assembler_script_digest())
        current = _assembler(npm_policy_digest(), assembler_script_digest())
        self.assertNotEqual(prior.digest, current.digest)
        self.assertNotEqual(
            compute_assembler_input_identity(validated, prior).digest,
            compute_assembler_input_identity(validated, current).digest,
        )


if __name__ == "__main__":
    unittest.main()

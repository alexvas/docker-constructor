"""Phase 6 — public-boundary introspection (task 6.6).

The dependent consumer boundary is verified against the public API surface
and the result/evidence DTO field names: no npm-cache, staging, executor,
mount, or consumer-specific field may leak into the public result contract.
"""

from __future__ import annotations

import dataclasses
import unittest


class TestPublicResultBoundary(unittest.TestCase):
    def test_public_api_exposes_the_consumer_boundary(self) -> None:
        import docker.npm_environment as pkg

        for name in (
            "preflight",
            "assemble_environment",
            "AssemblyResult",
            "AssemblerEvidence",
            "AssemblerInputIdentity",
            "AssembledOutputIdentity",
        ):
            self.assertIn(name, pkg.__all__)
            self.assertTrue(hasattr(pkg, name), name)

    def test_result_and_evidence_do_not_leak_cache_or_runtime_internals(self) -> None:
        import docker.npm_environment as pkg

        for dto in (pkg.AssemblyResult, pkg.AssemblerEvidence, pkg.AssemblerEvidenceBody):
            fields = {f.name for f in dataclasses.fields(dto)}
            lowered = " ".join(fields).lower()
            for forbidden in (
                "npm_cache",
                "namespace",
                "staging",
                "executor",
                "mount",
                "docker",
            ):
                self.assertNotIn(forbidden, lowered, fields)


if __name__ == "__main__":
    unittest.main()

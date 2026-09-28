"""Phase 8 RED capture — canonical fetch rendering must not restore secrets.

Disables only the incremental secret guard so the recognizer's
``canonical_unsafe`` flag is never set.  The collector then accepts a canonical
fetch rendering whose method, status, attempt, or cache clause was copied from
the source line without projection, so a configured secret redacted from the
selected diagnostic is restored in ``fetch_text`` and grouped/presented.
Everything else stays real.  Run from anywhere:

    python3 -B openspec/changes/add-configurable-network-url-display/verification/phase8_canonical_secret_red.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))

from docker.versioning import npm_fetch as nf  # noqa: E402


def _reverted_feed(self, text):
    """Previous behaviour: trust canonicalization without a secret check."""
    return None


nf._SecretFieldMatcher.feed = _reverted_feed

import tests.test_npm_diagnostic_collection_phase8 as p8  # noqa: E402
import tests.test_presentation_wiring_phase7 as p7  # noqa: E402

loader = unittest.TestLoader()
suite = unittest.TestSuite(
    [
        loader.loadTestsFromTestCase(p8.TestFetchCanonicalizationSecretSafety),
        loader.loadTestsFromTestCase(p7.TestFetchCanonicalizationSecretSafety),
    ]
)
result = unittest.TextTestRunner(verbosity=2).run(suite)
print(
    "RED SUMMARY (canonical secret):",
    "failures=", len(result.failures),
    "errors=", len(result.errors),
)

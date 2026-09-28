"""Phase 8 RED capture -- no raw authority/user-information buffer.

Reinstalls the retired authority retention on top of the current code without
touching the sources on disk: every authority character (including the
``user:password@`` user-information prefix) is appended to a raw
``_legacy_authority`` list, so a credential-bearing authority substring
survives between :meth:`NpmFetchRecognizer.feed` calls again.  The per-chunk
retention regressions then fail while everything else stays real.

Run from anywhere:

    python3 -B openspec/changes/add-configurable-network-url-display/verification/phase8_authority_buffer_red.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))

from docker.versioning import npm_fetch as nf  # noqa: E402

# -- legacy raw authority buffer -----------------------------------------
_orig_init = nf.NpmFetchRecognizer.__init__


def _init(self, secrets=()):
    _orig_init(self, secrets)
    self._legacy_authority = []


nf.NpmFetchRecognizer.__init__ = _init

_orig_consume = nf.NpmFetchRecognizer._consume_url_authority


def _consume(self, character):
    # Every authority character is retained verbatim, including the
    # credentials before ``@`` -- the behaviour removed from the shipped code.
    self._legacy_authority.append(character)
    return _orig_consume(self, character)


nf.NpmFetchRecognizer._consume_url_authority = _consume

import tests.test_npm_diagnostic_collection_phase8 as p8  # noqa: E402
import tests.test_npm_fetch_phase4 as p4  # noqa: E402

loader = unittest.TestLoader()
suite = unittest.TestSuite()
suite.addTests(
    loader.loadTestsFromName(
        "tests.test_npm_diagnostic_collection_phase8."
        "TestIncrementalFetchRecognition."
        "test_parser_state_never_retains_credentials_query_or_fragment"
    )
)
suite.addTests(
    loader.loadTestsFromName(
        "tests.test_npm_fetch_phase4."
        "TestIncrementalFetchRecognizerEquivalence."
        "test_recognizer_state_never_retains_credentials_per_chunk"
    )
)
result = unittest.TextTestRunner(verbosity=2).run(suite)
print(
    "RED SUMMARY (authority buffer):",
    "failures=", len(result.failures),
    "errors=", len(result.errors),
)

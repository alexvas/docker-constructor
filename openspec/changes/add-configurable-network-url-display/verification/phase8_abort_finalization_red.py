"""Phase 8 RED capture — fail-closed abort finalization for ``host-path``.

Reverts only the abort policy of the selected ``host-path`` projector: an
unterminated line still resolves its trailing token with a clean EOF
(``projector.finish()``) instead of ``projector.finish(abort=abort)``.  A line
finalized at reader failure or cancellation could therefore resolve a trailing
URL token that the streaming sanitizer had withheld, so the finalized live
text and retained tail were no longer fail-closed.  The record-newline
handling is kept, so this capture isolates the abort defect.  Run from
anywhere:

    python3 -B openspec/changes/add-configurable-network-url-display/verification/phase8_abort_finalization_red.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))

from docker.versioning import npm_diagnostic_stream as nds  # noqa: E402

_orig_finish = nds.NpmDiagnosticStream.finish


def _reverted_finish(self, *, abort=False):
    """Previous abort policy: every projector finalizes with a clean EOF."""
    return _orig_finish(self, abort=False)


nds.NpmDiagnosticStream.finish = _reverted_finish

import tests.test_npm_diagnostic_collection_phase8 as p8  # noqa: E402
import tests.test_presentation_wiring_phase7 as p7  # noqa: E402

loader = unittest.TestLoader()
suite = unittest.TestSuite(
    [
        loader.loadTestsFromTestCase(p8.TestAbortFailClosedHostPathFinalization),
        loader.loadTestsFromTestCase(p8.TestCollectionAbortFinalization),
        loader.loadTestsFromTestCase(p7.TestPresentationIsolation),
    ]
)
result = unittest.TextTestRunner(verbosity=2).run(suite)
print(
    "RED SUMMARY (abort finalization):",
    "failures=", len(result.failures),
    "errors=", len(result.errors),
)

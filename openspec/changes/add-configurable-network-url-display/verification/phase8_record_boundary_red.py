"""Phase 8 RED capture — record-newline versus EOF finalization for ``host-path``.

Reverts only the record-boundary policy of the selected ``host-path``
projector: the source line is fed as if it ended at EOF, dropping the record
newline before the selected projector sees it.  A newline-terminated partial
secret or scheme prefix then failed closed in the finalized live text while a
real record boundary should have resolved it as ordinary text.  The abort
policy is kept, so this capture isolates the record-boundary defect.  Run from
anywhere:

    python3 -B openspec/changes/add-configurable-network-url-display/verification/phase8_record_boundary_red.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))

from docker.versioning import npm_diagnostic_stream as nds  # noqa: E402

_orig_emit_selected = nds.NpmDiagnosticStream._emit_selected_prefix


def _reverted_emit_selected(self, source_segment):
    """Previous record policy: never give the selected projector the newline."""
    if self._network_url_display is nds.NetworkUrlDisplay.HOST_PATH:
        stripped = (
            source_segment[:-1]
            if source_segment.endswith("\n")
            else source_segment
        )
        return _orig_emit_selected(self, stripped)
    return _orig_emit_selected(self, source_segment)


nds.NpmDiagnosticStream._emit_selected_prefix = _reverted_emit_selected

import tests.test_npm_diagnostic_collection_phase8 as p8  # noqa: E402

loader = unittest.TestLoader()
suite = unittest.TestSuite(
    [
        loader.loadTestsFromTestCase(
            p8.TestHostPathRecordBoundaryFinalization
        ),
    ]
)
result = unittest.TextTestRunner(verbosity=2).run(suite)
print(
    "RED SUMMARY (record boundary):",
    "failures=", len(result.failures),
    "errors=", len(result.errors),
)

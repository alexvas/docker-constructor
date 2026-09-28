"""Phase 7 RED capture A — collection wiring.

Reverts only ``NpmDiagnosticStream._select_presentation`` to the pre-Phase-7
behaviour (no mode-selected local text, no fetch identity).  Routing stays the
real independent fan-out.  Run from anywhere:

    python3 -B openspec/changes/add-configurable-network-url-display/verification/phase7_wiring_red.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))

from docker.versioning import npm_diagnostic_stream as nds  # noqa: E402


def _reverted_select(self, projected, overflowed, *, newline_terminated=False):
    return projected, None, None


nds.NpmDiagnosticStream._select_presentation = _reverted_select

import tests.test_presentation_wiring_phase7 as t  # noqa: E402

suite = unittest.TestLoader().loadTestsFromModule(t)
result = unittest.TextTestRunner(verbosity=1).run(suite)
print(
    "RED SUMMARY (wiring):",
    "failures=", len(result.failures),
    "errors=", len(result.errors),
)

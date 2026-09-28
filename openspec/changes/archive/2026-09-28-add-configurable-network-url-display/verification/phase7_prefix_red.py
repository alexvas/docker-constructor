"""Phase 7 RED capture C — mode-selected committed prefixes.

Reverts only the committed-prefix display selection to the previous
behaviour: ``host-path`` and ``exact`` released their safe (redacted)
projection as the live prefix and carried no selected local fragment, so a
partial diagnostic rendered redacted until its newline.  Run from anywhere:

    python3 -B openspec/changes/add-configurable-network-url-display/verification/phase7_prefix_red.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))

from docker.npm_environment.streaming import StreamChunk  # noqa: E402
from docker.versioning import npm_diagnostic_stream as nds  # noqa: E402
from docker.versioning.model import NetworkUrlDisplay  # noqa: E402


def _reverted_retain_only(self, source_segment):
    """Previous prefix behaviour: retain only, release no local fragment."""
    mode = self._network_url_display
    if mode is NetworkUrlDisplay.HOST_PATH:
        assert self._retained_projector is not None
        for retained in self._retained_projector.feed_text(source_segment):
            self._append_tail(retained)
        self._drain_retained_facts()
    elif mode is NetworkUrlDisplay.EXACT:
        self._append_tail(source_segment)
    return []


def _reverted_emit_prefix(self, segment, *, retain):
    """Previous prefix behaviour: the safe projection was the local text."""
    self._line_chars.append(segment)
    if retain:
        self._append_tail(segment)
    return StreamChunk(
        self._stream,
        segment,
        overflowed=self._line_overflowed,
        finalized=False,
        local_text=segment,
    )


nds.NpmDiagnosticStream._emit_selected_prefix = _reverted_retain_only
nds.NpmDiagnosticStream._emit_prefix = _reverted_emit_prefix

import tests.test_npm_diagnostic_collection_phase8 as p8  # noqa: E402
import tests.test_presentation_wiring_phase7 as p7  # noqa: E402
import tests.test_constructor_pi_assembly as pa  # noqa: E402

loader = unittest.TestLoader()
suite = unittest.TestSuite(
    [
        loader.loadTestsFromTestCase(p8.TestCommittedPrefixDisplayModes),
        loader.loadTestsFromTestCase(p7.TestCommittedPrefixRouting),
        loader.loadTestsFromTestCase(pa.TestFacadeProvisionalPrefixRoute),
    ]
)
result = unittest.TextTestRunner(verbosity=2).run(suite)
print(
    "RED SUMMARY (prefix):",
    "failures=", len(result.failures),
    "errors=", len(result.errors),
)

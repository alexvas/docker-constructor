"""Phase 8 RED capture -- no full source buffer in redacted/host-path.

Reinstalls the retired retention behaviour on top of the current code without
touching the sources on disk:

* the collector keeps a complete terminal-safe ``_source_line_chars`` buffer
  for every line;
* ``host-path`` finalization reprojects that complete source line with a second
  projector instead of using the output the selected projector already
  produced incrementally;
* the recognizer retains the complete fed source text.

The new regression class then fails: the collector owns a full source buffer,
the recognizer state contains credentials/query/fragment text, and the
"no complete source line" contract is violated.  Everything else stays real.
Run from anywhere:

    python3 -B openspec/changes/add-configurable-network-url-display/verification/phase8_incremental_fetch_red.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))

from docker.npm_environment.streaming import _OverflowMarker  # noqa: E402
from docker.versioning import npm_diagnostic_stream as nds  # noqa: E402
from docker.versioning.diagnostic_projection import (  # noqa: E402
    DiagnosticProjector,
)
from docker.versioning.npm_fetch import NpmFetchRecognizer  # noqa: E402

# -- legacy collector full-source buffer ---------------------------------
_orig_init = nds.NpmDiagnosticStream.__init__


def _init(self, *args, **kwargs):
    _orig_init(self, *args, **kwargs)
    self._source_line_chars = []


nds.NpmDiagnosticStream.__init__ = _init

_orig_emit = nds.NpmDiagnosticStream._emit_neutralized


def _emit(self, text):
    if not isinstance(text, _OverflowMarker):
        self._source_line_chars.append(text[:-1] if text.endswith("\n") else text)
    return _orig_emit(self, text)


nds.NpmDiagnosticStream._emit_neutralized = _emit

# -- legacy host-path full-line reprojection ------------------------------
_orig_finish = nds.NpmDiagnosticStream.finish


def _finish(self, *, abort=False):
    self._legacy_abort = abort
    return _orig_finish(self, abort=abort)


nds.NpmDiagnosticStream.finish = _finish

_orig_local = nds.NpmDiagnosticStream._selected_local_text


def _local(self, projected, *, newline_terminated):
    if self._network_url_display is nds.NetworkUrlDisplay.HOST_PATH:
        source = "".join(self._source_line_chars)
        projector = DiagnosticProjector(
            self._secrets, url_formatter=nds._host_path_formatter
        )
        rendered = "".join(projector.feed_text(source))
        if newline_terminated:
            rendered += "".join(projector.feed_text("\n"))
        rendered += "".join(
            projector.finish(abort=getattr(self, "_legacy_abort", False))
        )
        if newline_terminated and rendered.endswith("\n"):
            rendered = rendered[:-1]
        return rendered
    return _orig_local(self, projected, newline_terminated=newline_terminated)


nds.NpmDiagnosticStream._selected_local_text = _local

# -- legacy recognizer full-source retention ------------------------------
_orig_feed = NpmFetchRecognizer.feed


def _feed(self, text):
    self._legacy_source = getattr(self, "_legacy_source", "") + text
    return _orig_feed(self, text)


NpmFetchRecognizer.feed = _feed

import tests.test_npm_diagnostic_collection_phase8 as p8  # noqa: E402

loader = unittest.TestLoader()
suite = loader.loadTestsFromTestCase(p8.TestIncrementalFetchRecognition)
result = unittest.TextTestRunner(verbosity=2).run(suite)
print(
    "RED SUMMARY (incremental fetch):",
    "failures=", len(result.failures),
    "errors=", len(result.errors),
)

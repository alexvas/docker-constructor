"""Phase 8 RED capture — combined internal + SDK dispatching (this fix).

Reinstates only the pre-fix delivery model: internal prefixes/envelopes and
SDK diagnostics share one lossy ``SinkDispatcher``, and provisional prefixes
enter that queue too.  The collector, the presentation actor, the SDK routing,
and the callback budgets stay real.  Run from anywhere:

    python3 -B openspec/changes/add-configurable-network-url-display/verification/phase8_combined_sink_red.py

The regression must fail: a one-slot SDK queue plus a callback blocked on the
first finalized record delays (then drops) the queued internal prefixes, so
the internal renderer never sees a later record's provisional snapshot and
the prefix buffer is never reset at the dropped record boundary.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))

from docker.npm_environment import streaming as npm_streaming  # noqa: E402
from docker.versioning import pi_assembly as pa  # noqa: E402


class _LegacyCombinedSink:
    """One shared, lossy dispatcher target for internal + SDK delivery."""

    def __init__(self, direct, dispatched):
        # ``direct`` is folded into the dispatched target and the collector is
        # given no direct channel, so every chunk -- including a provisional
        # prefix -- enters the one dispatcher queue.
        self.direct = None
        if direct is None:
            self.dispatched = dispatched
        else:
            def combined(chunk):
                direct(chunk)
                dispatched(chunk)

            self.dispatched = combined

    def should_dispatch(self, chunk):  # every chunk enters the lossy queue
        return True


# ``pi_assembly`` imported the class by name, so patch both references.
npm_streaming.SplitDiagnosticSink = _LegacyCombinedSink
pa.SplitDiagnosticSink = _LegacyCombinedSink

import tests.test_constructor_pi_assembly as p  # noqa: E402

loader = unittest.TestLoader()
suite = unittest.TestSuite(
    [
        loader.loadTestsFromName(
            "TestFacadeProvisionalPrefixRoute."
            "test_combined_sink_saturation_keeps_internal_records_prompt_and_bounded",
            p,
        ),
    ]
)
result = unittest.TextTestRunner(verbosity=2).run(suite)
print(
    "RED SUMMARY (combined sink):",
    "failures=", len(result.failures),
    "errors=", len(result.errors),
)

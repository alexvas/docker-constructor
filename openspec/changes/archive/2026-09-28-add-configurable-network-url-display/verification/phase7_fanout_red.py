"""Phase 7 RED capture B — independent fan-out (this fix).

Reverts only the routing to the previous exclusive behaviour: when a nominal
internal presentation actor exists, the SDK channel is suppressed; overflow is
mutually exclusive too.  The collection wiring stays real.  Run from anywhere:

    python3 -B openspec/changes/add-configurable-network-url-display/verification/phase7_fanout_red.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))

from docker.versioning import pi_assembly as pa  # noqa: E402
from docker.versioning.host_progress import (  # noqa: E402
    HostDiagnosticPrefix,
    HostDiagnosticStream,
    HostPhase,
    HostStep,
    emit,
)
from docker.versioning.pi_assembly import (  # noqa: E402
    overflow_diagnostic_for,
    presentation_envelope_for,
    structured_diagnostic_for,
)


def _exclusive_route(chunk, *, internal_sink, sdk_sink, logical_resource):
    if internal_sink is not None:
        try:
            internal_sink.admit_diagnostic(
                presentation_envelope_for(chunk, logical_resource=logical_resource)
            )
        except Exception:
            pass
        return
    if sdk_sink is not None:
        emit(
            sdk_sink,
            structured_diagnostic_for(chunk, logical_resource=logical_resource),
        )


def _exclusive_overflow(chunk, *, internal_sink, sdk_sink, logical_resource):
    if internal_sink is not None:
        prefix = HostDiagnosticPrefix(
            phase=HostPhase.LOCKED_ASSEMBLY,
            step=HostStep.NPM_EXECUTION,
            stream=HostDiagnosticStream(chunk.stream),
            text=(
                chunk.local_text
                if chunk.local_text is not None
                else chunk.text
            ),
            logical_resource=logical_resource,
            finalized=True,
            overflowed=True,
        )
        try:
            internal_sink.admit_prefix(prefix)
        except Exception:
            pass
        return
    if sdk_sink is not None:
        emit(
            sdk_sink,
            overflow_diagnostic_for(chunk, logical_resource=logical_resource),
        )


pa.route_finalized_diagnostic = _exclusive_route
pa.route_overflow_diagnostic = _exclusive_overflow

import tests.test_presentation_wiring_phase7 as t  # noqa: E402

t.route_finalized_diagnostic = _exclusive_route
t.route_overflow_diagnostic = _exclusive_overflow

loader = unittest.TestLoader()
suite = unittest.TestSuite(
    [
        loader.loadTestsFromTestCase(t.TestFinalizedDiagnosticRouting),
    ]
)
result = unittest.TextTestRunner(verbosity=2).run(suite)
print(
    "RED SUMMARY (fan-out):",
    "failures=", len(result.failures),
    "errors=", len(result.errors),
)

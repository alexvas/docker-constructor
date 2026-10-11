"""Build P6's heading-complete evidence index; fail on missing test references.

One-off evidence tooling, not tests/CI. Maps each delta requirement/scenario
explicitly to reviewed executable suites/classes; does not infer test success.
"""
from pathlib import Path
import ast

ROOT = Path(__file__).resolve().parents[4]
HERE = Path(__file__).resolve().parent

# Short aliases resolve to real test classes or complete executable suites.
REFERENCES = {
    'schemes': 'test_diagnostic_recognizer_phase2.py::SupportedSchemeTests',
    'encoded': 'test_diagnostic_recognizer_phase2.py::EncodedSchemeTests',
    'provenance': 'test_diagnostic_recognizer_phase2.py::SourceRangeProvenanceTests',
    'work': 'test_diagnostic_recognizer_phase2.py::WorkStateRegressionTests',
    'continuation': 'test_diagnostic_recognizer_phase3.py::Phase3Tests',
    'unknown': 'test_diagnostic_policy_phase4.py::UnknownTextPolicyTests',
    'markers': 'test_diagnostic_policy_phase4.py::PolicyMarkerTests',
    'decoded': 'test_diagnostic_recognizer_phase2.py::UrlDecodedSecretTests',
    'gates': 'test_diagnostic_policy_phase4.py::FactGateRouteTests',
    'identity': 'test_diagnostic_policy_phase4.py::CanonicalIdentityTests',
    'channels': 'test_diagnostic_channels_phase5.py::TestCollectorChannelModes',
    'groups': 'test_diagnostic_channels_phase5.py::TestGroupingPolicy',
    'fetch': 'test_diagnostic_channels_phase5.py::TestFetchSidePaths',
    'fetchgrammar': 'test_npm_fetch_phase4.py::TestFetchGrammarRecognition',
    'fetchreject': 'test_npm_fetch_phase4.py::TestFetchGrammarRejection',
    'fetchkey': 'test_npm_fetch_phase4.py::TestFetchGroupIdentity',
    'fetchunits': 'test_npm_fetch_phase4.py::TestFetchNumericBoundaries',
    'interactivefetch': 'test_host_presentation_phase5.py::TestInteractiveRedactedFetchGrouping',
    'hostfetch': 'test_host_presentation_phase5.py::TestInteractiveHostPathFetchGrouping',
    'exact': 'test_host_presentation_phase5.py::TestExactPresentation',
    'numeric': 'test_host_diagnostic_grouping_phase7.py',
    'fingerprints': 'test_host_diagnostic_identity_phase7.py',
    'tail': 'test_retained_tail_phase6.py',
    'binding': 'test_retained_tail_phase6.py::TestAssemblerRequestBinding',
    'invariance': 'test_retained_tail_phase6.py::TestPresentationOnlyInvariance',
    'collector': 'test_npm_diagnostic_collection_phase8.py',
    'selection': 'test_host_presentation_phase9.py::TestPresentationSelection',
    'terminal': 'test_host_activity_monitor.py::TestTerminalLifecycle',
    'heartbeat': 'test_host_activity_monitor.py',
    'interactive': 'test_host_presentation_phase9.py::TestInteractiveState',
    'lines': 'test_host_presentation_phase9.py::TestLinesState',
    'inbox': 'test_host_presentation_phase9.py::TestOrderedInbox',
    'close': 'test_host_presentation_phase9.py::TestCapacityIndependentSessionClose',
    'width': 'test_presentation_wiring_phase7.py::TestRealOutputIntegrationMatrix',
    'wiring': 'test_presentation_wiring_phase7.py',
    'exceptions': 'test_host_diagnostic_projection.py::TestExceptionTypeChain',
    'structured': 'test_host_diagnostic_projection.py::TestStructuredHostProjection',
    'hostpolicy': 'test_host_presentation_phase9.py::TestHostnamePolicy',
    'report': 'test_host_presentation_phase9.py::TestFailureReport',
    'protocol': 'test_constructor_host_progress.py',
}

# Requirement-body coverage supplements the per-scenario rows below. Inherited
# lifecycle/presentation obligations are explicitly covered by current runs,
# not by obsolete UNKNOWN-hiding gates.
REQUIREMENTS = {
    'Integrate host progress through typed presentation-neutral events': 'protocol heartbeat inbox close wiring channels',
    'Apply one safe network diagnostic presentation policy': 'structured exceptions unknown markers continuation channels',
    'Recognize only supported diagnostic URL schemes': 'schemes unknown gates groups',
    'Detect encoded diagnostic URL boundaries conservatively': 'encoded provenance continuation',
    'Continue ambiguous diagnostic candidates across input boundaries': 'continuation',
    'Avoid quadratic diagnostic candidate scanning': 'work continuation',
    'Preserve secret recognition across URL boundaries': 'continuation markers',
    'Match decoded secrets only inside supported URLs': 'decoded gates',
    'Render secret-aware diagnostic URL components': 'markers channels',
    'Commit recognized URL projection irreversibly': 'continuation channels',
    'Separate rendering markers from validated URL facts': 'gates identity groups',
    'Retain bounded redacted assembler diagnostics': 'binding invariance tail collector numeric fetchgrammar fetchreject fetchkey interactivefetch hostfetch exact inbox close wiring channels',
}
SCENARIOS = {
    'Text output is noninteractive': 'selection tail',
    'Host phase completes or fails': 'terminal wiring',
    'Hostname policy is presentation-only': 'hostpolicy channels invariance',
    'Diagnostic interrupts transient progress': 'interactive numeric wiring',
    'Hidden URL identity is presentation-safe': 'fingerprints groups fetchkey',
    'Numeric updates remain durable in line mode': 'numeric lines',
    'Diagnostic presentation capacity is exhausted': 'inbox wiring',
    'Injected caller omits presentation': 'terminal selection protocol',
    'Supported transcripts fit independently of the hard limit': 'inbox',
    'Control admission reaches the hard safety limit': 'inbox close',
    'Full inbox closes normally': 'inbox close',
    'Presentation sink fails': 'terminal wiring close',
    'Renderer raises an exception': 'close wiring',
    'Renderer stalls indefinitely': 'close wiring',
    'Final host failure report has one writer': 'wiring tail',
    'Transient diagnostic exceeds terminal width': 'width',
    'Delivered heartbeats do not determine execution timeout': 'heartbeat wiring',
    'SDK caller receives diagnostics under a revealing local mode': 'channels',
    'Hostname display is disabled': 'structured report',
    'Hostname display is enabled': 'structured report hostpolicy',
    'Host-path failure context uses the selected retained tail': 'tail channels',
    'Nested exception graph is deep or cyclic': 'exceptions',
    'Unknown schemes remain captured text': 'unknown channels fetch',
    'Unknown text participates in normal grouping': 'groups',
    'Exact unknown text and SDK projection are separate': 'channels unknown',
    'Secret-bearing host is display-only masked text': 'markers gates channels',
    'Secret-bearing path preserves safe hostname only': 'markers gates channels',
    'Credential secret does not erase permitted host facts': 'markers gates',
    'Early URL projection is retained without replacement': 'continuation channels',
    'Crossing-secret prefix preserves local and SDK source order': 'continuation',
    'Exact remains revealing locally with safe SDK projection': 'channels',
    'Exact mode has no captured third-party diagnostic': 'structured',
    'Known scheme is not proof of validity': 'schemes markers gates',
    'Unknown scheme remains ordinary text': 'unknown decoded fetch',
    'Supported scheme begins inside longer text': 'schemes unknown continuation',
    'Unknown source differences remain text identity': 'groups',
    'Earlier encoded candidate precedes a literal candidate': 'encoded provenance continuation',
    'Encoded known scheme uses the same presentation policy': 'encoded identity markers',
    'Depth-four separator preserves legacy coverage': 'encoded gates',
    'Mixed depth and separately encoded escapes remain recognized': 'encoded provenance',
    'Separator is split across chunks': 'encoded continuation',
    'Single slash does not complete the separator': 'schemes continuation',
    'Unsupported encoded scheme remains text': 'encoded',
    'Separator prefix ends a feed': 'continuation',
    'Separator is complete but token is not terminated': 'continuation schemes',
    'Completed candidate precedes ambiguous suffix': 'continuation',
    'Ordinary scheme-like word ends cleanly': 'continuation collector',
    'Chunk partition does not change projection': 'continuation channels',
    'Long uninterrupted non-URL diagnostic': 'work continuation',
    'Long candidate recovers after overflow': 'continuation collector',
    'Cumulative work includes commits and resets': 'work continuation',
    'Retained raw text has one owner': 'continuation tail',
    'Secret overlaps a supported scheme prefix': 'continuation',
    'Secret crosses the end of a URL': 'continuation',
    'Secret is inside a URL authority': 'markers gates',
    'Secret commitment is not delayed to a URL terminator': 'continuation',
    'Standalone secret replacement preserves surrounding text': 'markers channels',
    'Encoded secret in a URL path': 'decoded markers gates',
    'Encoded text is not a supported URL': 'decoded unknown',
    'Decoding depth does not grow at finalization': 'decoded gates encoded',
    'Credential secret permits safe host and path': 'markers gates',
    'Host secret preserves only independently safe path': 'markers channels',
    'Path secret replaces the whole displayed path': 'markers channels',
    'Redacted URL begins before authority is complete': 'continuation',
    'Crossing-secret delay is mandatory and narrowly scoped': 'continuation',
    'Finalization preserves a committed redacted URL': 'continuation',
    'Earlier unresolved crossing secret delays marker delivery': 'continuation',
    'Mismatching continuation releases ordinary prefix before URL marker': 'continuation',
    'Clean EOF resolves an incomplete crossing secret as a non-match': 'continuation',
    'Overflow cannot duplicate a committed redacted URL': 'continuation collector',
    'Host and path commit separately': 'continuation',
    'Failure after host commitment preserves host output': 'continuation collector',
    'Partial separator still follows previous finalization': 'continuation',
    'Masked host has no metadata identity': 'gates groups',
    'Secret prefix must not manufacture a valid URL': 'unknown gates continuation',
    'Facts from unrelated URLs remain available': 'gates',
    'Presentation selector reaches collection': 'binding invariance',
    'Direct caller omits the presentation selector': 'binding tail',
    'Direct caller supplies an invalid presentation selector': 'binding',
    'Long-running npm installation emits output': 'collector wiring heartbeat',
    'Repeated retry status': 'lines wiring numeric tail',
    'Interactive repeated diagnostic updates one slot': 'interactive numeric',
    'Interactive numeric diagnostic updates one slot': 'interactive numeric',
    'Numeric diagnostic values remain separate in lines mode': 'lines numeric',
    'Hidden URLs remain distinct without disclosure': 'identity fingerprints groups fetchkey',
    'Coalescing window ends without another diagnostic': 'lines wiring',
    'Diagnostic mailbox saturates': 'inbox wiring',
    'Fixed bounded control capacity is exhausted': 'inbox close',
    'Coalescing terminates with healthy presentation lifecycle': 'terminal close wiring',
    'Presentation cannot finish within its budget': 'close wiring',
    'Retained context repeats earlier live output': 'tail',
    'Structured caller executes assembly': 'selection tail',
    'URL spans diagnostic chunks': 'continuation collector channels',
    'Arbitrarily long URL has no terminating delimiter': 'continuation collector',
    'Arbitrarily long diagnostic has no newline': 'collector',
    'Stream terminates with an ambiguous sensitive prefix': 'continuation collector tail',
    'Diagnostic contains network configuration': 'channels tail',
    'Research successful-fetch fixture is recognized end to end': 'fetchgrammar collector interactivefetch hostfetch exact',
    'Similar npm forms remain ordinary diagnostics': 'fetchreject collector',
    'Redacted interactive fetches accumulate': 'interactivefetch',
    'Attempt identity changes': 'fetchkey interactivefetch',
    'Unsupported latency unit is received': 'fetchreject fetchunits',
    'Host-path latency variants accumulate': 'hostfetch',
    'Host-path resources differ': 'hostfetch fetchkey',
    'Exact output preserves content but not terminal effects': 'exact channels tail',
    'Revealing modes do not alter SDK or evidence': 'channels invariance',
    'Presentation-only requests remain semantically equivalent': 'invariance tail',
}


def main():
    for ref in REFERENCES.values():
        file, _, cls = ref.partition('::')
        tree = ast.parse((ROOT / 'tests' / file).read_text())
        if cls:
            node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls)
            assert any(isinstance(n, ast.FunctionDef) and n.name.startswith('test_') for n in node.body)
    lines = [
        '# P6 — requirement/scenario-to-evidence matrix', '',
        'Every heading in both current delta specs is indexed below. References are',
        'reviewed executable suites/classes, not a claim that a checkbox proves behavior.',
        'All listed tests run in `phase6_validation.log` (mandated full suite); local',
        'configured discovery also runs in `phase6_unittest.log`. Final statuses and',
        'limitations are reconciled in `phase-6.md`.', '',
        'Predecessor gates: `phase-1.md`, `phase-1a.md` (including immutability fix),',
        '`phase-2.md` (including all maintenance/depth corrections), `phase-3.md`',
        '(including delayed activation/crossing-prefix EOF), `phase-4.md`, and',
        '`phase-5.md` (including unsupported fetch URLs with literal secrets).',
        'Old UNKNOWN-hiding, arbitrary-suffix batch equivalence and feed-only counters',
        'are not reused as proof of revised behavior. Historical RED logs remain intact.', '',
    ]
    counts = {'Requirement': 0, 'Scenario': 0}
    seen = set()
    for spec in sorted((HERE.parent / 'specs').glob('*/spec.md')):
        lines += [f'## {spec.parent.name}', '', '| Kind / source line / obligation | Executable evidence |', '| --- | --- |']
        for number, text in enumerate(spec.read_text().splitlines(), 1):
            if text.startswith('### Requirement: '):
                kind, title = 'Requirement', text.removeprefix('### Requirement: ')
                keys = REQUIREMENTS[title]
            elif text.startswith('#### Scenario: '):
                kind, title = 'Scenario', text.removeprefix('#### Scenario: ')
                keys = SCENARIOS[title]
                seen.add(title)
            else:
                continue
            counts[kind] += 1
            refs = '; '.join(f'`tests/{REFERENCES[key]}`' for key in keys.split())
            lines.append(f'| {kind}, L{number}: {title} | {refs} |')
        lines.append('')
    assert seen == set(SCENARIOS), set(SCENARIOS) - seen
    lines += [
        '## Phase deliverables and acceptance-specific evidence', '',
        '| Tasks | Evidence / interpretation |', '| --- | --- |',
        '| 1.1–1.9 | `phase-1.md` task table; caller inventory; immutable recognition contract; current full-suite revalidation |',
        '| 1A.1–1A.10 | `phase-1a.md` task table; `tests/test_aho_corasick_core.py`; `phase6_core_audit.log` |',
        '| 2.1–2.11 | `phase-2.md` corrections/task table; phase2 tests; `phase6_recognizer_audit.log` |',
        '| 3.1–3.14 | `phase-3.md` task table; 41 feed-by-feed tests; `phase6_ownership_work.log`, `phase6_differential.log` |',
        '| 4.1–4.10 | `phase-4.md` task table; policy/fact-route tests and API documentation |',
        '| 5.1–5.9 | `phase-5.md` task table; channel/grouping/fetch tests; user documentation; literal-secret unknown-fetch regression |',
        '| 6.1–6.2 | This heading-complete matrix; `phase6_checks.md`; no artificial acceptance RED |',
        '| 6.3 | `phase6_experiment.py`; before/after logs + JSON; unchanged fixture inputs; `phase6_performance.md` |',
        '| 6.4–6.5 | Full pytest/type/configured-discovery logs and actual statuses |',
        '| 6.6–6.7 | `phase6_scope.md`, final-state audits and reconciliation in `phase-6.md` |',
        '| 6.8–6.9 | Final code-state hashes/check logs and acceptance decision in `phase-6.md` |', '',
        'At initial indexing, outstanding work was the P6 experiment, current-run',
        'audit/scope reconciliation and final acceptance decision; no missing behavior',
        'was established merely from those missing artifacts. See the final gate for',
        'their resolution. This matrix is an evidence index, not whole-pipeline',
        'linearity proof. Recognition bounds are fixed-registration/depth, output-sensitive.',
        '', f"Coverage: {counts['Requirement']} requirements; {counts['Scenario']} scenarios.",
    ]
    (HERE / 'phase6_matrix.md').write_text('\n'.join(lines) + '\n')
    print('Validated test references and indexed all headings:', counts)


if __name__ == '__main__':
    main()

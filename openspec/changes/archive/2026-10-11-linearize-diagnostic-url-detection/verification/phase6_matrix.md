# P6 — requirement/scenario-to-evidence matrix

Every heading in both current delta specs is indexed below. References are
reviewed executable suites/classes, not a claim that a checkbox proves behavior.
All listed tests run in `phase6_validation.log` (mandated full suite); local
configured discovery also runs in `phase6_unittest.log`. Final statuses and
limitations are reconciled in `phase-6.md`.

Predecessor gates: `phase-1.md`, `phase-1a.md` (including immutability fix),
`phase-2.md` (including all maintenance/depth corrections), `phase-3.md`
(including delayed activation/crossing-prefix EOF), `phase-4.md`, and
`phase-5.md` (including unsupported fetch URLs with literal secrets).
Old UNKNOWN-hiding, arbitrary-suffix batch equivalence and feed-only counters
are not reused as proof of revised behavior. Historical RED logs remain intact.

## docker-build-output

| Kind / source line / obligation | Executable evidence |
| --- | --- |
| Requirement, L5: Integrate host progress through typed presentation-neutral events | `tests/test_constructor_host_progress.py`; `tests/test_host_activity_monitor.py`; `tests/test_host_presentation_phase9.py::TestOrderedInbox`; `tests/test_host_presentation_phase9.py::TestCapacityIndependentSessionClose`; `tests/test_presentation_wiring_phase7.py`; `tests/test_diagnostic_channels_phase5.py::TestCollectorChannelModes` |
| Scenario, L32: Text output is noninteractive | `tests/test_host_presentation_phase9.py::TestPresentationSelection`; `tests/test_retained_tail_phase6.py` |
| Scenario, L38: Host phase completes or fails | `tests/test_host_activity_monitor.py::TestTerminalLifecycle`; `tests/test_presentation_wiring_phase7.py` |
| Scenario, L46: Hostname policy is presentation-only | `tests/test_host_presentation_phase9.py::TestHostnamePolicy`; `tests/test_diagnostic_channels_phase5.py::TestCollectorChannelModes`; `tests/test_retained_tail_phase6.py::TestPresentationOnlyInvariance` |
| Scenario, L52: Diagnostic interrupts transient progress | `tests/test_host_presentation_phase9.py::TestInteractiveState`; `tests/test_host_diagnostic_grouping_phase7.py`; `tests/test_presentation_wiring_phase7.py` |
| Scenario, L64: Hidden URL identity is presentation-safe | `tests/test_host_diagnostic_identity_phase7.py`; `tests/test_diagnostic_channels_phase5.py::TestGroupingPolicy`; `tests/test_npm_fetch_phase4.py::TestFetchGroupIdentity` |
| Scenario, L71: Numeric updates remain durable in line mode | `tests/test_host_diagnostic_grouping_phase7.py`; `tests/test_host_presentation_phase9.py::TestLinesState` |
| Scenario, L76: Diagnostic presentation capacity is exhausted | `tests/test_host_presentation_phase9.py::TestOrderedInbox`; `tests/test_presentation_wiring_phase7.py` |
| Scenario, L86: Injected caller omits presentation | `tests/test_host_activity_monitor.py::TestTerminalLifecycle`; `tests/test_host_presentation_phase9.py::TestPresentationSelection`; `tests/test_constructor_host_progress.py` |
| Scenario, L91: Supported transcripts fit independently of the hard limit | `tests/test_host_presentation_phase9.py::TestOrderedInbox` |
| Scenario, L97: Control admission reaches the hard safety limit | `tests/test_host_presentation_phase9.py::TestOrderedInbox`; `tests/test_host_presentation_phase9.py::TestCapacityIndependentSessionClose` |
| Scenario, L106: Full inbox closes normally | `tests/test_host_presentation_phase9.py::TestOrderedInbox`; `tests/test_host_presentation_phase9.py::TestCapacityIndependentSessionClose` |
| Scenario, L113: Presentation sink fails | `tests/test_host_activity_monitor.py::TestTerminalLifecycle`; `tests/test_presentation_wiring_phase7.py`; `tests/test_host_presentation_phase9.py::TestCapacityIndependentSessionClose` |
| Scenario, L120: Renderer raises an exception | `tests/test_host_presentation_phase9.py::TestCapacityIndependentSessionClose`; `tests/test_presentation_wiring_phase7.py` |
| Scenario, L126: Renderer stalls indefinitely | `tests/test_host_presentation_phase9.py::TestCapacityIndependentSessionClose`; `tests/test_presentation_wiring_phase7.py` |
| Scenario, L134: Final host failure report has one writer | `tests/test_presentation_wiring_phase7.py`; `tests/test_retained_tail_phase6.py` |
| Scenario, L141: Transient diagnostic exceeds terminal width | `tests/test_presentation_wiring_phase7.py::TestRealOutputIntegrationMatrix` |
| Scenario, L147: Delivered heartbeats do not determine execution timeout | `tests/test_host_activity_monitor.py`; `tests/test_presentation_wiring_phase7.py` |
| Scenario, L153: SDK caller receives diagnostics under a revealing local mode | `tests/test_diagnostic_channels_phase5.py::TestCollectorChannelModes` |
| Requirement, L158: Apply one safe network diagnostic presentation policy | `tests/test_host_diagnostic_projection.py::TestStructuredHostProjection`; `tests/test_host_diagnostic_projection.py::TestExceptionTypeChain`; `tests/test_diagnostic_policy_phase4.py::UnknownTextPolicyTests`; `tests/test_diagnostic_policy_phase4.py::PolicyMarkerTests`; `tests/test_diagnostic_recognizer_phase3.py::Phase3Tests`; `tests/test_diagnostic_channels_phase5.py::TestCollectorChannelModes` |
| Scenario, L163: Hostname display is disabled | `tests/test_host_diagnostic_projection.py::TestStructuredHostProjection`; `tests/test_host_presentation_phase9.py::TestFailureReport` |
| Scenario, L168: Hostname display is enabled | `tests/test_host_diagnostic_projection.py::TestStructuredHostProjection`; `tests/test_host_presentation_phase9.py::TestFailureReport`; `tests/test_host_presentation_phase9.py::TestHostnamePolicy` |
| Scenario, L173: Host-path failure context uses the selected retained tail | `tests/test_retained_tail_phase6.py`; `tests/test_diagnostic_channels_phase5.py::TestCollectorChannelModes` |
| Scenario, L179: Nested exception graph is deep or cyclic | `tests/test_host_diagnostic_projection.py::TestExceptionTypeChain` |
| Scenario, L183: Unknown schemes remain captured text | `tests/test_diagnostic_policy_phase4.py::UnknownTextPolicyTests`; `tests/test_diagnostic_channels_phase5.py::TestCollectorChannelModes`; `tests/test_diagnostic_channels_phase5.py::TestFetchSidePaths` |
| Scenario, L188: Unknown text participates in normal grouping | `tests/test_diagnostic_channels_phase5.py::TestGroupingPolicy` |
| Scenario, L193: Exact unknown text and SDK projection are separate | `tests/test_diagnostic_channels_phase5.py::TestCollectorChannelModes`; `tests/test_diagnostic_policy_phase4.py::UnknownTextPolicyTests` |
| Scenario, L199: Secret-bearing host is display-only masked text | `tests/test_diagnostic_policy_phase4.py::PolicyMarkerTests`; `tests/test_diagnostic_policy_phase4.py::FactGateRouteTests`; `tests/test_diagnostic_channels_phase5.py::TestCollectorChannelModes` |
| Scenario, L205: Secret-bearing path preserves safe hostname only | `tests/test_diagnostic_policy_phase4.py::PolicyMarkerTests`; `tests/test_diagnostic_policy_phase4.py::FactGateRouteTests`; `tests/test_diagnostic_channels_phase5.py::TestCollectorChannelModes` |
| Scenario, L210: Credential secret does not erase permitted host facts | `tests/test_diagnostic_policy_phase4.py::PolicyMarkerTests`; `tests/test_diagnostic_policy_phase4.py::FactGateRouteTests` |
| Scenario, L215: Early URL projection is retained without replacement | `tests/test_diagnostic_recognizer_phase3.py::Phase3Tests`; `tests/test_diagnostic_channels_phase5.py::TestCollectorChannelModes` |
| Scenario, L222: Crossing-secret prefix preserves local and SDK source order | `tests/test_diagnostic_recognizer_phase3.py::Phase3Tests` |
| Scenario, L231: Exact remains revealing locally with safe SDK projection | `tests/test_diagnostic_channels_phase5.py::TestCollectorChannelModes` |
| Scenario, L236: Exact mode has no captured third-party diagnostic | `tests/test_host_diagnostic_projection.py::TestStructuredHostProjection` |

## locked-npm-environment-assembly

| Kind / source line / obligation | Executable evidence |
| --- | --- |
| Requirement, L5: Recognize only supported diagnostic URL schemes | `tests/test_diagnostic_recognizer_phase2.py::SupportedSchemeTests`; `tests/test_diagnostic_policy_phase4.py::UnknownTextPolicyTests`; `tests/test_diagnostic_policy_phase4.py::FactGateRouteTests`; `tests/test_diagnostic_channels_phase5.py::TestGroupingPolicy` |
| Scenario, L8: Known scheme is not proof of validity | `tests/test_diagnostic_recognizer_phase2.py::SupportedSchemeTests`; `tests/test_diagnostic_policy_phase4.py::PolicyMarkerTests`; `tests/test_diagnostic_policy_phase4.py::FactGateRouteTests` |
| Scenario, L14: Unknown scheme remains ordinary text | `tests/test_diagnostic_policy_phase4.py::UnknownTextPolicyTests`; `tests/test_diagnostic_recognizer_phase2.py::UrlDecodedSecretTests`; `tests/test_diagnostic_channels_phase5.py::TestFetchSidePaths` |
| Scenario, L20: Supported scheme begins inside longer text | `tests/test_diagnostic_recognizer_phase2.py::SupportedSchemeTests`; `tests/test_diagnostic_policy_phase4.py::UnknownTextPolicyTests`; `tests/test_diagnostic_recognizer_phase3.py::Phase3Tests` |
| Scenario, L25: Unknown source differences remain text identity | `tests/test_diagnostic_channels_phase5.py::TestGroupingPolicy` |
| Requirement, L30: Detect encoded diagnostic URL boundaries conservatively | `tests/test_diagnostic_recognizer_phase2.py::EncodedSchemeTests`; `tests/test_diagnostic_recognizer_phase2.py::SourceRangeProvenanceTests`; `tests/test_diagnostic_recognizer_phase3.py::Phase3Tests` |
| Scenario, L33: Earlier encoded candidate precedes a literal candidate | `tests/test_diagnostic_recognizer_phase2.py::EncodedSchemeTests`; `tests/test_diagnostic_recognizer_phase2.py::SourceRangeProvenanceTests`; `tests/test_diagnostic_recognizer_phase3.py::Phase3Tests` |
| Scenario, L39: Encoded known scheme uses the same presentation policy | `tests/test_diagnostic_recognizer_phase2.py::EncodedSchemeTests`; `tests/test_diagnostic_policy_phase4.py::CanonicalIdentityTests`; `tests/test_diagnostic_policy_phase4.py::PolicyMarkerTests` |
| Scenario, L44: Depth-four separator preserves legacy coverage | `tests/test_diagnostic_recognizer_phase2.py::EncodedSchemeTests`; `tests/test_diagnostic_policy_phase4.py::FactGateRouteTests` |
| Scenario, L49: Mixed depth and separately encoded escapes remain recognized | `tests/test_diagnostic_recognizer_phase2.py::EncodedSchemeTests`; `tests/test_diagnostic_recognizer_phase2.py::SourceRangeProvenanceTests` |
| Scenario, L54: Separator is split across chunks | `tests/test_diagnostic_recognizer_phase2.py::EncodedSchemeTests`; `tests/test_diagnostic_recognizer_phase3.py::Phase3Tests` |
| Scenario, L60: Single slash does not complete the separator | `tests/test_diagnostic_recognizer_phase2.py::SupportedSchemeTests`; `tests/test_diagnostic_recognizer_phase3.py::Phase3Tests` |
| Scenario, L65: Unsupported encoded scheme remains text | `tests/test_diagnostic_recognizer_phase2.py::EncodedSchemeTests` |
| Requirement, L70: Continue ambiguous diagnostic candidates across input boundaries | `tests/test_diagnostic_recognizer_phase3.py::Phase3Tests` |
| Scenario, L73: Separator prefix ends a feed | `tests/test_diagnostic_recognizer_phase3.py::Phase3Tests` |
| Scenario, L78: Separator is complete but token is not terminated | `tests/test_diagnostic_recognizer_phase3.py::Phase3Tests`; `tests/test_diagnostic_recognizer_phase2.py::SupportedSchemeTests` |
| Scenario, L83: Completed candidate precedes ambiguous suffix | `tests/test_diagnostic_recognizer_phase3.py::Phase3Tests` |
| Scenario, L87: Ordinary scheme-like word ends cleanly | `tests/test_diagnostic_recognizer_phase3.py::Phase3Tests`; `tests/test_npm_diagnostic_collection_phase8.py` |
| Scenario, L91: Chunk partition does not change projection | `tests/test_diagnostic_recognizer_phase3.py::Phase3Tests`; `tests/test_diagnostic_channels_phase5.py::TestCollectorChannelModes` |
| Requirement, L96: Avoid quadratic diagnostic candidate scanning | `tests/test_diagnostic_recognizer_phase2.py::WorkStateRegressionTests`; `tests/test_diagnostic_recognizer_phase3.py::Phase3Tests` |
| Scenario, L99: Long uninterrupted non-URL diagnostic | `tests/test_diagnostic_recognizer_phase2.py::WorkStateRegressionTests`; `tests/test_diagnostic_recognizer_phase3.py::Phase3Tests` |
| Scenario, L104: Long candidate recovers after overflow | `tests/test_diagnostic_recognizer_phase3.py::Phase3Tests`; `tests/test_npm_diagnostic_collection_phase8.py` |
| Scenario, L109: Cumulative work includes commits and resets | `tests/test_diagnostic_recognizer_phase2.py::WorkStateRegressionTests`; `tests/test_diagnostic_recognizer_phase3.py::Phase3Tests` |
| Scenario, L115: Retained raw text has one owner | `tests/test_diagnostic_recognizer_phase3.py::Phase3Tests`; `tests/test_retained_tail_phase6.py` |
| Requirement, L120: Preserve secret recognition across URL boundaries | `tests/test_diagnostic_recognizer_phase3.py::Phase3Tests`; `tests/test_diagnostic_policy_phase4.py::PolicyMarkerTests` |
| Scenario, L123: Secret overlaps a supported scheme prefix | `tests/test_diagnostic_recognizer_phase3.py::Phase3Tests` |
| Scenario, L129: Secret crosses the end of a URL | `tests/test_diagnostic_recognizer_phase3.py::Phase3Tests` |
| Scenario, L135: Secret is inside a URL authority | `tests/test_diagnostic_policy_phase4.py::PolicyMarkerTests`; `tests/test_diagnostic_policy_phase4.py::FactGateRouteTests` |
| Scenario, L140: Secret commitment is not delayed to a URL terminator | `tests/test_diagnostic_recognizer_phase3.py::Phase3Tests` |
| Scenario, L145: Standalone secret replacement preserves surrounding text | `tests/test_diagnostic_policy_phase4.py::PolicyMarkerTests`; `tests/test_diagnostic_channels_phase5.py::TestCollectorChannelModes` |
| Requirement, L150: Match decoded secrets only inside supported URLs | `tests/test_diagnostic_recognizer_phase2.py::UrlDecodedSecretTests`; `tests/test_diagnostic_policy_phase4.py::FactGateRouteTests` |
| Scenario, L153: Encoded secret in a URL path | `tests/test_diagnostic_recognizer_phase2.py::UrlDecodedSecretTests`; `tests/test_diagnostic_policy_phase4.py::PolicyMarkerTests`; `tests/test_diagnostic_policy_phase4.py::FactGateRouteTests` |
| Scenario, L158: Encoded text is not a supported URL | `tests/test_diagnostic_recognizer_phase2.py::UrlDecodedSecretTests`; `tests/test_diagnostic_policy_phase4.py::UnknownTextPolicyTests` |
| Scenario, L162: Decoding depth does not grow at finalization | `tests/test_diagnostic_recognizer_phase2.py::UrlDecodedSecretTests`; `tests/test_diagnostic_policy_phase4.py::FactGateRouteTests`; `tests/test_diagnostic_recognizer_phase2.py::EncodedSchemeTests` |
| Requirement, L166: Render secret-aware diagnostic URL components | `tests/test_diagnostic_policy_phase4.py::PolicyMarkerTests`; `tests/test_diagnostic_channels_phase5.py::TestCollectorChannelModes` |
| Scenario, L169: Credential secret permits safe host and path | `tests/test_diagnostic_policy_phase4.py::PolicyMarkerTests`; `tests/test_diagnostic_policy_phase4.py::FactGateRouteTests` |
| Scenario, L174: Host secret preserves only independently safe path | `tests/test_diagnostic_policy_phase4.py::PolicyMarkerTests`; `tests/test_diagnostic_channels_phase5.py::TestCollectorChannelModes` |
| Scenario, L179: Path secret replaces the whole displayed path | `tests/test_diagnostic_policy_phase4.py::PolicyMarkerTests`; `tests/test_diagnostic_channels_phase5.py::TestCollectorChannelModes` |
| Requirement, L183: Commit recognized URL projection irreversibly | `tests/test_diagnostic_recognizer_phase3.py::Phase3Tests`; `tests/test_diagnostic_channels_phase5.py::TestCollectorChannelModes` |
| Scenario, L186: Redacted URL begins before authority is complete | `tests/test_diagnostic_recognizer_phase3.py::Phase3Tests` |
| Scenario, L192: Crossing-secret delay is mandatory and narrowly scoped | `tests/test_diagnostic_recognizer_phase3.py::Phase3Tests` |
| Scenario, L201: Finalization preserves a committed redacted URL | `tests/test_diagnostic_recognizer_phase3.py::Phase3Tests` |
| Scenario, L206: Earlier unresolved crossing secret delays marker delivery | `tests/test_diagnostic_recognizer_phase3.py::Phase3Tests` |
| Scenario, L214: Mismatching continuation releases ordinary prefix before URL marker | `tests/test_diagnostic_recognizer_phase3.py::Phase3Tests` |
| Scenario, L220: Clean EOF resolves an incomplete crossing secret as a non-match | `tests/test_diagnostic_recognizer_phase3.py::Phase3Tests` |
| Scenario, L228: Overflow cannot duplicate a committed redacted URL | `tests/test_diagnostic_recognizer_phase3.py::Phase3Tests`; `tests/test_npm_diagnostic_collection_phase8.py` |
| Scenario, L234: Host and path commit separately | `tests/test_diagnostic_recognizer_phase3.py::Phase3Tests` |
| Scenario, L240: Failure after host commitment preserves host output | `tests/test_diagnostic_recognizer_phase3.py::Phase3Tests`; `tests/test_npm_diagnostic_collection_phase8.py` |
| Scenario, L246: Partial separator still follows previous finalization | `tests/test_diagnostic_recognizer_phase3.py::Phase3Tests` |
| Requirement, L251: Separate rendering markers from validated URL facts | `tests/test_diagnostic_policy_phase4.py::FactGateRouteTests`; `tests/test_diagnostic_policy_phase4.py::CanonicalIdentityTests`; `tests/test_diagnostic_channels_phase5.py::TestGroupingPolicy` |
| Scenario, L254: Masked host has no metadata identity | `tests/test_diagnostic_policy_phase4.py::FactGateRouteTests`; `tests/test_diagnostic_channels_phase5.py::TestGroupingPolicy` |
| Scenario, L259: Secret prefix must not manufacture a valid URL | `tests/test_diagnostic_policy_phase4.py::UnknownTextPolicyTests`; `tests/test_diagnostic_policy_phase4.py::FactGateRouteTests`; `tests/test_diagnostic_recognizer_phase3.py::Phase3Tests` |
| Scenario, L265: Facts from unrelated URLs remain available | `tests/test_diagnostic_policy_phase4.py::FactGateRouteTests` |
| Requirement, L271: Retain bounded redacted assembler diagnostics | `tests/test_retained_tail_phase6.py::TestAssemblerRequestBinding`; `tests/test_retained_tail_phase6.py::TestPresentationOnlyInvariance`; `tests/test_retained_tail_phase6.py`; `tests/test_npm_diagnostic_collection_phase8.py`; `tests/test_host_diagnostic_grouping_phase7.py`; `tests/test_npm_fetch_phase4.py::TestFetchGrammarRecognition`; `tests/test_npm_fetch_phase4.py::TestFetchGrammarRejection`; `tests/test_npm_fetch_phase4.py::TestFetchGroupIdentity`; `tests/test_host_presentation_phase5.py::TestInteractiveRedactedFetchGrouping`; `tests/test_host_presentation_phase5.py::TestInteractiveHostPathFetchGrouping`; `tests/test_host_presentation_phase5.py::TestExactPresentation`; `tests/test_host_presentation_phase9.py::TestOrderedInbox`; `tests/test_host_presentation_phase9.py::TestCapacityIndependentSessionClose`; `tests/test_presentation_wiring_phase7.py`; `tests/test_diagnostic_channels_phase5.py::TestCollectorChannelModes` |
| Scenario, L309: Presentation selector reaches collection | `tests/test_retained_tail_phase6.py::TestAssemblerRequestBinding`; `tests/test_retained_tail_phase6.py::TestPresentationOnlyInvariance` |
| Scenario, L315: Direct caller omits the presentation selector | `tests/test_retained_tail_phase6.py::TestAssemblerRequestBinding`; `tests/test_retained_tail_phase6.py` |
| Scenario, L320: Direct caller supplies an invalid presentation selector | `tests/test_retained_tail_phase6.py::TestAssemblerRequestBinding` |
| Scenario, L324: Long-running npm installation emits output | `tests/test_npm_diagnostic_collection_phase8.py`; `tests/test_presentation_wiring_phase7.py`; `tests/test_host_activity_monitor.py` |
| Scenario, L330: Repeated retry status | `tests/test_host_presentation_phase9.py::TestLinesState`; `tests/test_presentation_wiring_phase7.py`; `tests/test_host_diagnostic_grouping_phase7.py`; `tests/test_retained_tail_phase6.py` |
| Scenario, L340: Interactive repeated diagnostic updates one slot | `tests/test_host_presentation_phase9.py::TestInteractiveState`; `tests/test_host_diagnostic_grouping_phase7.py` |
| Scenario, L350: Interactive numeric diagnostic updates one slot | `tests/test_host_presentation_phase9.py::TestInteractiveState`; `tests/test_host_diagnostic_grouping_phase7.py` |
| Scenario, L361: Numeric diagnostic values remain separate in lines mode | `tests/test_host_presentation_phase9.py::TestLinesState`; `tests/test_host_diagnostic_grouping_phase7.py` |
| Scenario, L366: Hidden URLs remain distinct without disclosure | `tests/test_diagnostic_policy_phase4.py::CanonicalIdentityTests`; `tests/test_host_diagnostic_identity_phase7.py`; `tests/test_diagnostic_channels_phase5.py::TestGroupingPolicy`; `tests/test_npm_fetch_phase4.py::TestFetchGroupIdentity` |
| Scenario, L374: Coalescing window ends without another diagnostic | `tests/test_host_presentation_phase9.py::TestLinesState`; `tests/test_presentation_wiring_phase7.py` |
| Scenario, L379: Diagnostic mailbox saturates | `tests/test_host_presentation_phase9.py::TestOrderedInbox`; `tests/test_presentation_wiring_phase7.py` |
| Scenario, L388: Fixed bounded control capacity is exhausted | `tests/test_host_presentation_phase9.py::TestOrderedInbox`; `tests/test_host_presentation_phase9.py::TestCapacityIndependentSessionClose` |
| Scenario, L395: Coalescing terminates with healthy presentation lifecycle | `tests/test_host_activity_monitor.py::TestTerminalLifecycle`; `tests/test_host_presentation_phase9.py::TestCapacityIndependentSessionClose`; `tests/test_presentation_wiring_phase7.py` |
| Scenario, L405: Presentation cannot finish within its budget | `tests/test_host_presentation_phase9.py::TestCapacityIndependentSessionClose`; `tests/test_presentation_wiring_phase7.py` |
| Scenario, L413: Retained context repeats earlier live output | `tests/test_retained_tail_phase6.py` |
| Scenario, L420: Structured caller executes assembly | `tests/test_host_presentation_phase9.py::TestPresentationSelection`; `tests/test_retained_tail_phase6.py` |
| Scenario, L425: URL spans diagnostic chunks | `tests/test_diagnostic_recognizer_phase3.py::Phase3Tests`; `tests/test_npm_diagnostic_collection_phase8.py`; `tests/test_diagnostic_channels_phase5.py::TestCollectorChannelModes` |
| Scenario, L432: Arbitrarily long URL has no terminating delimiter | `tests/test_diagnostic_recognizer_phase3.py::Phase3Tests`; `tests/test_npm_diagnostic_collection_phase8.py` |
| Scenario, L441: Arbitrarily long diagnostic has no newline | `tests/test_npm_diagnostic_collection_phase8.py` |
| Scenario, L451: Stream terminates with an ambiguous sensitive prefix | `tests/test_diagnostic_recognizer_phase3.py::Phase3Tests`; `tests/test_npm_diagnostic_collection_phase8.py`; `tests/test_retained_tail_phase6.py` |
| Scenario, L459: Diagnostic contains network configuration | `tests/test_diagnostic_channels_phase5.py::TestCollectorChannelModes`; `tests/test_retained_tail_phase6.py` |
| Scenario, L465: Research successful-fetch fixture is recognized end to end | `tests/test_npm_fetch_phase4.py::TestFetchGrammarRecognition`; `tests/test_npm_diagnostic_collection_phase8.py`; `tests/test_host_presentation_phase5.py::TestInteractiveRedactedFetchGrouping`; `tests/test_host_presentation_phase5.py::TestInteractiveHostPathFetchGrouping`; `tests/test_host_presentation_phase5.py::TestExactPresentation` |
| Scenario, L472: Similar npm forms remain ordinary diagnostics | `tests/test_npm_fetch_phase4.py::TestFetchGrammarRejection`; `tests/test_npm_diagnostic_collection_phase8.py` |
| Scenario, L477: Redacted interactive fetches accumulate | `tests/test_host_presentation_phase5.py::TestInteractiveRedactedFetchGrouping` |
| Scenario, L482: Attempt identity changes | `tests/test_npm_fetch_phase4.py::TestFetchGroupIdentity`; `tests/test_host_presentation_phase5.py::TestInteractiveRedactedFetchGrouping` |
| Scenario, L487: Unsupported latency unit is received | `tests/test_npm_fetch_phase4.py::TestFetchGrammarRejection`; `tests/test_npm_fetch_phase4.py::TestFetchNumericBoundaries` |
| Scenario, L491: Host-path latency variants accumulate | `tests/test_host_presentation_phase5.py::TestInteractiveHostPathFetchGrouping` |
| Scenario, L496: Host-path resources differ | `tests/test_host_presentation_phase5.py::TestInteractiveHostPathFetchGrouping`; `tests/test_npm_fetch_phase4.py::TestFetchGroupIdentity` |
| Scenario, L501: Exact output preserves content but not terminal effects | `tests/test_host_presentation_phase5.py::TestExactPresentation`; `tests/test_diagnostic_channels_phase5.py::TestCollectorChannelModes`; `tests/test_retained_tail_phase6.py` |
| Scenario, L506: Revealing modes do not alter SDK or evidence | `tests/test_diagnostic_channels_phase5.py::TestCollectorChannelModes`; `tests/test_retained_tail_phase6.py::TestPresentationOnlyInvariance` |
| Scenario, L511: Presentation-only requests remain semantically equivalent | `tests/test_retained_tail_phase6.py::TestPresentationOnlyInvariance`; `tests/test_retained_tail_phase6.py` |

## Phase deliverables and acceptance-specific evidence

| Tasks | Evidence / interpretation |
| --- | --- |
| 1.1–1.9 | `phase-1.md` task table; caller inventory; immutable recognition contract; current full-suite revalidation |
| 1A.1–1A.10 | `phase-1a.md` task table; `tests/test_aho_corasick_core.py`; `phase6_core_audit.log` |
| 2.1–2.11 | `phase-2.md` corrections/task table; phase2 tests; `phase6_recognizer_audit.log` |
| 3.1–3.14 | `phase-3.md` task table; 41 feed-by-feed tests; `phase6_ownership_work.log`, `phase6_differential.log` |
| 4.1–4.10 | `phase-4.md` task table; policy/fact-route tests and API documentation |
| 5.1–5.9 | `phase-5.md` task table; channel/grouping/fetch tests; user documentation; literal-secret unknown-fetch regression |
| 6.1–6.2 | This heading-complete matrix; `phase6_checks.md`; no artificial acceptance RED |
| 6.3 | `phase6_experiment.py`; before/after logs + JSON; unchanged fixture inputs; `phase6_performance.md` |
| 6.4–6.5 | Full pytest/type/configured-discovery logs and actual statuses |
| 6.6–6.7 | `phase6_scope.md`, final-state audits and reconciliation in `phase-6.md` |
| 6.8–6.9 | Final code-state hashes/check logs and acceptance decision in `phase-6.md` |

At initial indexing, outstanding work was the P6 experiment, current-run
audit/scope reconciliation and final acceptance decision; no missing behavior
was established merely from those missing artifacts. See the final gate for
their resolution. This matrix is an evidence index, not whole-pipeline
linearity proof. Recognition bounds are fixed-registration/depth, output-sensitive.

Coverage: 12 requirements; 107 scenarios.

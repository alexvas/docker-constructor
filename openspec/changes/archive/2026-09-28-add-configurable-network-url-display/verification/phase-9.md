# Phase 9 verification evidence

## 9.1 pre-fix cross-capability acceptance

Command:

```text
python -m unittest tests.test_network_url_display_acceptance -v
```

Result at initial implementation: **PASS**, but subsequent review found that two rows were not sufficient evidence for their names:

1. `noninteractive_lines_all_policies` exercised redacted line rendering plus policy-agnostic mailbox ordering, but did not assert `host-path` or `exact` durable line rendering.
2. `simultaneous_sdk_all_policies` tested SDK projection and retained-tail behavior independently, but did not fan one diagnostic out to active local and SDK sinks in the same production routing call.

These were acceptance-coverage gaps, not production defects.

## 9.2 initial full-project check

Commands and results:

```text
scripts/check-types
```

**PASS** — `All checks passed!`

```text
scripts/check-dockerfile
```

**BLOCKED** — exited 127 because Docker is unavailable; the pinned Hadolint container could not run.

```text
python -m unittest discover -s tests -p 'test_*.py'
```

**FAIL** — 4,451 tests ran with 1 failure and 13 skips. The failure was `test_constructor_host_access_launch_red.TestHostAccessPlanningRed.test_enabled_missing_or_malformed_local_state_fails_before_effects`: its secret-value assertion searched the complete error string for `42`, which occurred coincidentally in the randomized temporary-directory name `tmpwyg_l42q`, rather than in disclosed configuration content.

```text
docker build --check .
```

**BLOCKED** — the build attempt failed before execution because the `docker` executable is unavailable.

Task 9.2 is complete as a RED inventory: every required full-project gate was attempted and its pre-release result was recorded. The actionable test regression is the path-sensitive confidentiality assertion above; Docker-backed lint and build remain environment blockers for the GREEN/final validation tasks.

## 9.3 cross-capability release fixes

Review of the initial matrix exposed the two acceptance-coverage gaps recorded above. No production change was required. The corrected matrix now points to:

- `tests.test_presentation_wiring_phase7.TestRealOutputIntegrationMatrix.test_noninteractive_lines_render_selected_representation_in_every_mode`
- `tests.test_presentation_wiring_phase7.TestFinalizedDiagnosticRouting.test_all_policies_simultaneously_fan_out_selected_local_and_safe_sdk_output`

The policy-agnostic mailbox-ordering reference and independently exercised SDK/retained-tail references were removed from those two matrix rows.

Re-verification command:

```text
python -m unittest tests.test_network_url_display_acceptance -v
```

Result: **PASS** — the matrix test resolved every corrected test ID exactly once and all rows passed.

## 9.4 regression fix and available GREEN checks

Fixed the pre-release test regression without weakening its confidentiality assertion: the assertion now removes the required companion path from the diagnostic before checking that the rejected value is absent. This avoids accidental matches in randomized temporary-directory names while continuing to reject value disclosure in the actual error content.

Commands and results:

```text
scripts/check-types
```

**PASS** — `All checks passed!`

```text
python -m unittest discover -s tests -p 'test_*.py'
```

**PASS** — 4,451 tests ran with 13 skips.

```text
npx pi-green-loop check --affected tests/test_constructor_host_access_launch_red.py
```

**PASS** — configured typecheck and complete unittest checks both passed.

Host-provided Docker-backed results:

```text
scripts/check-dockerfile
```

**PASS** — exited successfully with no output.

```text
./docker/docker-constructor.py build -y
```

**PASS** — cache reuse, artifact/release acquisition, locked assembly, derived validation, and Docker transition succeeded; BuildKit completed all 67 steps and exported `pi-cli-pi:latest`; command exited 0.

Task 9.4 is complete: typecheck, static/lint checks, the complete test suite, and the canonical image build all pass.

## 9.5 requirement and scenario traceability

All task references below are completed checklist tasks. The complete 4,451-test run above executes every cited suite.

| Delta requirement / scenarios | Completed tasks | Automated or observable verification |
|---|---|---|
| **docker-build-output — Report host-side build materialization progress**: Pi dependency assembly is long-running; Silent npm execution remains alive; Host download has transport progress but no diagnostic silence; Transport progress does not reset applicable diagnostic silence; Build advances to Docker; Structured build output; Noninteractive line mode; Noninteractive default mode; Host materialization fails | 1.3, 1.7, 5.4, 5.7, 6.6–6.7, 6.11, 7.3, 7.8 | `tests.test_host_activity_monitor`, `tests.test_host_download_observability`, `tests.test_constructor_pi_assembly`, `tests.test_constructor_build_output`, `tests.test_host_presentation_phase9`, `tests.test_retained_tail_phase6`, the all-policy durable-lines test `TestRealOutputIntegrationMatrix.test_noninteractive_lines_render_selected_representation_in_every_mode`, and the release acceptance rows `text_failure_all_policies` and `json_failure_all_policies` |
| **docker-build-output — Integrate host progress through typed presentation-neutral events**: Text output is noninteractive; Host phase completes or fails; Hostname policy is presentation-only; Diagnostic interrupts transient progress; Hidden URL identity is presentation-safe; Numeric updates remain durable in line mode; Diagnostic presentation capacity is exhausted; Injected caller omits presentation; Supported transcripts fit independently of the hard limit; Control admission reaches the hard safety limit; Full inbox closes normally; Presentation sink fails; Renderer raises an exception; Renderer stalls indefinitely; Final host failure report has one writer; Transient diagnostic exceeds terminal width; Delivered heartbeats do not determine execution timeout; SDK caller receives diagnostics under a revealing local mode | 1.3–1.9, 3.4–3.7, 5.1–5.10, 6.5, 6.10, 7.1–7.12 | `tests.test_host_presentation_phase9`, `tests.test_host_diagnostic_grouping_phase7`, `tests.test_host_diagnostic_identity_phase7`, `tests.test_constructor_pi_assembly`, `tests.test_presentation_wiring_phase7`, the all-policy durable-lines test `TestRealOutputIntegrationMatrix.test_noninteractive_lines_render_selected_representation_in_every_mode`, and the simultaneous production fan-out test `TestFinalizedDiagnosticRouting.test_all_policies_simultaneously_fan_out_selected_local_and_safe_sdk_output` |
| **docker-build-output — Configure host output through the local companion**: Local output settings are absent; Local output settings are valid; Local output settings are invalid; Output policy remains host-only; Presentation selection does not alter assembly semantics | 1.1–1.12, 6.8, 6.12, 6.14, 8.1–8.6 | `tests.test_output_configuration`, `tests.test_local_project_configuration_phase2`, `tests.test_configuration_document_validation_phase1`, `tests.test_host_diagnostic_projection`, `tests.test_retained_tail_phase6.TestPresentationOnlyInvariance`, documentation assertions in `tests.test_host_observability_acceptance_phase10`, and successful parsing/build of this project's `host-path` companion |
| **docker-build-output — Apply one safe network diagnostic presentation policy**: Hostname display is disabled; Hostname display is enabled; Host-path failure context uses the selected retained tail; Nested exception graph is deep or cyclic; Exact mode has no captured third-party diagnostic | 3.1–3.8, 6.1–6.7, 6.9–6.11, 8.1–8.6 | `tests.test_host_path_projection`, `tests.test_host_failure_output_regression`, `tests.test_retained_tail_phase6`, `tests.test_constructor_pi_assembly`, and release acceptance rows `text_failure_all_policies` and `json_failure_all_policies` |
| **locked-npm-environment-assembly — Retain bounded redacted assembler diagnostics**: Presentation selector reaches collection; Direct caller omits the presentation selector; Direct caller supplies an invalid presentation selector; Long-running npm installation emits output | 1.3–1.9, 6.1–6.15 | `tests.test_constructor_pi_assembly`, `tests.test_npm_environment_streaming`, `tests.test_retained_tail_phase6`, and `tests.test_presentation_wiring_phase7` |
| Same requirement: Repeated retry status; Interactive repeated diagnostic updates one slot; Interactive numeric diagnostic updates one slot; Numeric diagnostic values remain separate in lines mode; Hidden URLs remain distinct without disclosure; Coalescing window ends without another diagnostic | 3.4–3.7, 5.1–5.10 | `tests.test_host_diagnostic_grouping_phase7`, `tests.test_host_diagnostic_identity_phase7`, `tests.test_host_presentation_phase5`, and `tests.test_host_presentation_phase9` |
| Same requirement: Diagnostic mailbox saturates; Fixed bounded control capacity is exhausted; Coalescing terminates with healthy presentation lifecycle; Presentation cannot finish within its budget | 7.1–7.3, 7.6, 7.8, 7.10–7.12 | `tests.test_host_presentation_phase9`, `tests.test_presentation_wiring_phase7`, and `tests.test_constructor_pi_assembly` saturation, close/drain, blocked-renderer, and cancellation cases |
| Same requirement: Retained context repeats earlier live output; Structured caller executes assembly | 6.6–6.7, 6.11, 7.3, 7.8 | `tests.test_retained_tail_phase6.TestFailureChannelMatrix`, `tests.test_retained_tail_phase6.TestFailureContextChannels`, and release acceptance rows `text_failure_all_policies` and `json_failure_all_policies` |
| Same requirement: URL spans diagnostic chunks; Arbitrarily long URL has no terminating delimiter; Arbitrarily long diagnostic has no newline; Stream terminates with an ambiguous sensitive prefix; Diagnostic contains network configuration | 2.1–2.11, 3.2–3.7, 7.7 | `tests.test_npm_diagnostic_collection_phase8`, `tests.test_npm_environment_streaming`, `tests.test_host_path_projection`, and `tests.test_presentation_wiring_phase7` incremental, overflow, recovery, EOF, cancellation, and confidentiality cases |
| Same requirement: Research successful-fetch fixture is recognized end to end; Similar npm forms remain ordinary diagnostics; Redacted interactive fetches accumulate; Attempt identity changes; Unsupported latency unit is received; Host-path latency variants accumulate; Host-path resources differ | 4.1–4.7, 5.1–5.7, 7.4, 7.7 | `tests.test_npm_fetch_phase4`, `tests.test_npm_http_logging_research`, `tests.test_host_presentation_phase5`, `tests.test_presentation_wiring_phase7.TestRealOutputIntegrationMatrix`, and release acceptance rows `interactive_text_all_policies` and `noninteractive_lines_all_policies` |
| Same requirement: Exact output preserves content but not terminal effects; Revealing modes do not alter SDK or evidence; Presentation-only requests remain semantically equivalent | 2.1–2.11, 5.5, 5.8, 6.5, 6.8, 6.10, 6.12–6.14, 7.4, 7.7 | `tests.test_npm_environment_streaming` terminal neutralization cases, `tests.test_retained_tail_phase6.TestPresentationOnlyInvariance`, `tests.test_presentation_wiring_phase7.TestRealOutputIntegrationMatrix`, `TestFinalizedDiagnosticRouting.test_all_policies_simultaneously_fan_out_selected_local_and_safe_sdk_output`, and release acceptance rows `success_all_policies`, `ordinary_failure_all_policies`, `timeout_all_policies`, and `interruption_all_policies` |

Coverage result: every delta requirement and every named scenario is represented above by completed implementation tasks and at least one passing automated suite or explicit successful build/configuration observation. No uncovered requirement was found.

## 9.6 final boundary and dependency audit

Audit commands included repository-wide selector/legacy-claim searches, an AST scan of identity/evidence/projection DTO fields, targeted boundary suites, and `git diff --check`.

Results:

- **SDK/evidence disclosure:** the external DTO remains URL-free/path-free in every mode; revealing-mode integration tests passed. No identity, evidence, reviewed/effective projection, cache-semantic, rendering/container-input DTO contains `network_url_display`.
- **Retention:** with and without an SDK sink, targeted tests prove exactly one selected local retained representation; SDK projection remains transient.
- **Semantic identity:** all three selector values retain identical assembler/input/output identity, cache reuse, evidence, argv, lifecycle, cleanup, publication, and primary results.
- **Compatibility:** no production/document/example occurrence of `show_network_hosts` remains, and no alias or migration path was found. Closed-schema rejection remains tested.
- **Bounds and concurrency:** targeted projector, line/token bound, saturation, cancellation, and selected-tail tests passed; no new unbounded collection was found.
- **Dependencies:** selector references are limited to local parsing, facade/host materialization and assembly request propagation, collector selection, and local presentation. No late presentation type enters lower semantic identity/evidence modules; the dependency-neutral fetch identity remains separate.
- **Stale claims:** repository search found no claim that `network_url_display` cannot traverse host materialization or assembler requests.
- **Diff hygiene:** `git diff --check` passed.

Targeted audit verification:

```text
python -m unittest tests.test_host_diagnostic_projection tests.test_host_path_projection tests.test_host_diagnostic_identity_phase7 tests.test_retained_tail_phase6.TestSingleRepresentationMatrix tests.test_retained_tail_phase6.TestPresentationOnlyInvariance tests.test_presentation_wiring_phase7.TestRealOutputIntegrationMatrix tests.test_output_configuration -v
```

**PASS** — 212 tests ran.

## 9.7 final clean validation

Tool versions:

```text
Python 3.14.7
ty 0.0.73
OpenSpec 1.13.0
pi-green-loop 0.2.0
```

Final focused and full-project commands/results:

```text
python -m unittest tests.test_network_url_display_acceptance -v
```

**PASS** — 1 release-matrix test ran all required policy/channel/result rows.

```text
npx pi-green-loop check
```

**PASS** — configured `ty` typecheck passed and the complete unittest suite passed.

```text
git diff --check
```

**PASS**.

Docker-capable host checks, run as fresh commands after the release regression fix:

```text
scripts/check-dockerfile
./docker/docker-constructor.py build -y
```

**PASS** — Hadolint exited successfully; the canonical cached build completed all 67 BuildKit steps, exported `pi-cli-pi:latest`, and exited 0.

### Corrected acceptance review rerun

Commands and results after adding the two missing integration cases:

```text
python -m unittest tests.test_network_url_display_acceptance -v
```

**PASS** — 1 matrix test; every referenced test ID resolved exactly once and passed.

```text
python -m unittest tests.test_presentation_wiring_phase7 -v
```

**PASS** — 44 focused tests, including all-policy durable `lines` rendering and same-execution local/SDK fan-out.

```text
npx pi-green-loop check
```

**PASS** — typecheck passed and the complete unit-test suite passed.

## 9.8 strict OpenSpec validation

```text
openspec validate add-configurable-network-url-display --strict
```

**PASS** — `Change 'add-configurable-network-url-display' is valid`.

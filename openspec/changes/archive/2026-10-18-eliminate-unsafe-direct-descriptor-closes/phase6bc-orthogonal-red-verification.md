# Phase 6BC orthogonal result migration — RED evidence

## 6BC.6 Exact value surface and validation

Added `tests/test_process_orthogonal_results.py` (97 cases). It binds exact frozen fields/annotations/defaults, exact enum members, every phase/stream combination, kind/control-flow pairing (including InterruptedError), explicit error identity, independent action progress flags, strict tuple/context transport, and rejection of obsolete fields and CleanupResult aliases. No descriptors or worker threads are acquired by these record tests.

Command:

```text
.venv/bin/python -m pytest tests/test_process_orthogonal_results.py -n 8 --dist loadfile --tb=short -q
```

Full stdout/stderr and runner status: `/tmp/docker-constructor-6bc-orthogonal-surface-red.3SFJEx.log`.

Result: **87 failed, 10 passed; runner exit status 1**. Expected RED, not successful implementation verification. Failures expose absent FailureInfo/FailureKind/FailurePhase/LifecycleResult, obsolete ActionResult/ProcessResult fields and retained CleanupResult. Existing ProcessStream/StreamOutcome and some rejection paths are passing characterization only. No GREEN migration or whole-phase acceptance is claimed.

## 6BC.7 Collection claim and launch boundaries

Added `tests/test_process_single_collection.py` (42 cases): repeated/mixed and four racing method pairs, no loser callback/wait work, validation-before-claim, ordinary/unexpected release value results in both pipe positions, terminal interrupted attempts without snapshots, immutable results across blocked late release, same prepared owner after factory/transfer/reader setup failure, and validation/preparation-before-acquisition. Controlled race and release workers are resumed and joined in finally blocks. Fake byte streams acquire no OS descriptors; process pipe release remains solely through the existing OwnedStream authorities.

Final command:

```text
.venv/bin/python -m pytest tests/test_process_single_collection.py -n 8 --dist loadfile --tb=short -q
```

Full log/status: `/tmp/docker-constructor-6bc-single-collection-red-final.vxcL2o.log`.

Result: **36 failed, 6 passed; runner exit status 1** (expected RED, unsuccessful implementation verification). Earlier 32-case run: `/tmp/docker-constructor-6bc-single-collection-red.XjNAlQ.log`, 29 failed / 3 passed, exit 1. Missing contracts include rejection instead of queued/repeated collection, value returns instead of raises, new lifecycle records, prepared-owner failure returns and absence of interruption process_result attachments. The six passing checks characterize argument validation and early preparation only.

## 6BC.8 Failure context and shared authority

Added `tests/test_process_failure_context.py` (52 cases): both release/join positions, original ordinary/interrupted primaries, sole and competing cleanup failures, repeat observation, wait context, deterministic competing reader order, same-object read/join reuse, launch control flow, first/later coordinator-wait interruptions, the publication-before-notification gap, InterruptedError release policy and context exit after value delivery. Callback/reporter/fallback/action interruption coverage is supplied by the 6BC.9 module as part of the combined gate. Every controlled barrier is released and test worker joined in finally.

Final isolated log: `/tmp/docker-constructor-6bc-failure-context-red-final.xNvNSi.log`: **48 failed, 4 passed; runner exit 1**, expected RED. An earlier invocation had a test-module import error (`/tmp/docker-constructor-6bc-failure-context-red.nClPEM.log`); that test error was fixed before the final run and is not RED contract evidence.

## 6BC.9 Strict actions and single-invocation reporting

Added `tests/test_process_strict_actions_and_reporting.py` (72 cases in its final form): explicit action kinds independent of Python error type, void start/cancel validation, cumulative observations, helper-local versus parent context, malformed observations retaining actual control flow, all non-Exception interruption families through callback/reporter/fallback in both streams, first interruption versus later terminal reporting interruptions, diagnostic-only ordinary callback isolation outside locks, fixed-safe defaults/nonrecursive fallback, completion-before-registration and completion/dispatch races, prompt EOF before sibling read failure and a real descriptor-backed stalled reader retaining its owner until its actual stop barrier.

Isolated intermediate logs: `/tmp/docker-constructor-6bc-actions-reporting-red.mHeZ9o.log` (64 failed, exit 1) and `/tmp/docker-constructor-6bc-actions-reporting-red-final.rCJzht.log` (66 failed, exit 1). Six additional first-versus-later interruption cases are included in the final combined gate below. Real child pipes are acquired and released only through OwnedProcess/its existing OwnedStream authorities; the blocked reader is resumed in finally and cleanup completion is observed.

## 6BC.10 Partial adapter RED and migration inventory

Added `tests/test_npm_environment_orthogonal_projection.py` (166 cases): all 18 valid phase/stream prefix pairs across both non-control-flow kinds, two exception types and timeout states; returned captured values and original owner identity; timeout/release versus timeout/join authority; local READ versus JOIN/RELEASE/ACTION projection; captured/streaming interruption without generic snapshot, domain retained_capture and actual CLI reporting; and missing captured streams with sole independent release.

Log: `/tmp/docker-constructor-6bc-adapter-projection-red.pV9Z9j.log`: **166 failed; runner exit 1**, expected RED. These are returned-value projection tests using resource-free owner doubles, except missing-stream cases using descriptor-less byte-stream fixtures. Existing full integration/order/fd-reuse suites still require migration/verification; **6BC.10 is not complete**.

Inventory commands:

```text
rg -l 'ActionResult\(|CleanupResult|process_result|reader_failure_stream|callback_errors|\.cleanup\.' docker tests docs --glob '*.py' --glob '*.md'
rg -l 'DeadlineSupervisor|terminate_and_reap|_adopt_stream_owners|_make_streaming_reader|collect_streams|reap_process|_finalize_setup_failure|_close_stream_owners' docker tests docs
rg -n 'ActionResult\(' docker docs tests/test_process*.py
```

Production action producers are docker.process coordinator observations and execution._ContainerRemovalAction. Legacy test producers include test_process_stop_policy.Action/auxiliary fixture, test_process_captured_action_faults helper and test_process_stop_policy_faults. The process guide contains an action observation example requiring migration. New typed fixtures live in the strict-action module.

Direct process/result migration candidates include docker/process.py, npm execution/lifecycle/streaming, docs/process-lifecycle.md and docs/npm-environment-assembler.md; the process unit/contract/lifecycle/stop-policy/failure-source/failure-snapshot/failure-delivery/cross-owner/captured-action/callback/stream-outcome/adapter-compatibility suites; npm phase6b/6bc captured/streaming and phase3 deadline/phase4 lifecycle tests; and API/import baselines in test_process_contract and test_descriptor_close_convergence.

Standalone collector/export/helper review additionally reaches npm_environment/__init__.py, test_npm_environment_streaming, test_npm_diagnostic_collection_phase8, test_presentation_wiring_phase7, test_retained_tail_phase6, test_output_configuration, test_bound_pi_assembly_execution_phase6_host, test_locked_assembly_observability_phase8 and test_constructor_pi_assembly. Broad matches in launcher/CLI/build orchestration and constructor acceptance/output/networking tests are audit candidates, not permission to change unrelated domain result types or retained_capture consumers. The public collect_streams boundary requires a user decision; see phase6bc-public-collector-scope-question.md. No export or public collector has been removed.

## Final combined RED gate

```text
.venv/bin/python -m pytest tests/test_process_orthogonal_results.py tests/test_process_single_collection.py tests/test_process_failure_context.py tests/test_process_strict_actions_and_reporting.py tests/test_npm_environment_orthogonal_projection.py -n 8 --dist loadfile --tb=short -q
```

Full stdout/stderr and runner status: `/tmp/docker-constructor-6bc-orthogonal-red-combined.R5sjXA.log`.

Result: **409 failed, 20 passed (429 cases); runner exit status 1**. This demonstrates unmet approved contracts, not successful implementation verification. No GREEN task, integration acceptance or Phase 6BC completion is claimed.

## Static check and pause

`/tmp/docker-constructor-6bc-red-static.sISrFK.log` records successful `ty check docker --python-version 3.14 --output-format concise`, `git diff --check`, strict OpenSpec validation and apply-instructions retrieval (each exit 0). The CLI remains ready: 126/158 overall tasks complete, Phase 6BC 9/24. Only RED 6BC.6–9 were checked this session; 6BC.10 remains partial and all GREEN/docs/audit/validation obligations remain pending. Full-suite acceptance has not been run or claimed for this RED-only increment. Implementation is paused for the public collect_streams scope decision documented separately. No production changes, commits or index operations were made in this resume.

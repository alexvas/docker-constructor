# Tasks

Every task requires its named deliverable and verification. Section 0 records the already delivered, separately verified minimal repairs requested by the user. Groups 1–5 remain the full improvement plan: extend and verify that baseline, retain its passing regressions, and demonstrate RED only for genuinely unmet contracts rather than recreating fixed leaks. Within each remaining implementation group proceed RED → GREEN → INTROSPECT → VALIDATE; record passing characterization separately from newly failing contracts. Preserve complete stdout/stderr and runner exit statuses in this change's verification evidence. Stop for planning updates if another resource family or incompatible boundary appears. Original Phase 7 task states remain unchanged. Minimal-patch logs do not replace final validation after the remaining changes.

## 0. Delivered minimal repairs

**Completed baseline, not full change acceptance.** Evidence, complete logs and limitations are in [minimal-fix-verification.md](minimal-fix-verification.md); the corresponding 29 regressions are in `tests/test_network_resource_minimal_cleanup.py`.

- [x] 0.1 Release the buffered metadata HTTPError response through one `OwnedStream`, including read failure, preserving status/body and the existing handler boundary; verify once-only release, operation-plus-close precedence and interruption identity in the buffered HTTPError regression cases. Normal-response migration and uniform sole-close-to-503 mapping are not included.
- [x] 0.2 Release streaming acquisition HTTPError resources through one `OwnedStream` before propagation; verify exact acquisition/interruption authority and one underlying close in the streaming HTTPError regression cases, without changing the successful-response native context manager.
- [x] 0.3 Explicitly finalize the acquired artifact chunk iterator when it has a callable close, including externally retained generators after processing failure; use shared cleanup to preserve the active write failure over ordinary iterator-close failure and retain plain-iterator compatibility. Verify retained-iterator cases and the existing materialization suite.
- [x] 0.4 Prepare the private probe listener adapter and sole `OwnedStream` before server acquisition, bind before address/thread setup, reject a second sequential active start and clean failed startup without shutdown/join of a never-started thread; verify preparation, server/thread construction, start failure and start-plus-close regressions. Full serving readiness and start-then-raise races remain pending.
- [x] 0.5 Make minimal sequential probe stop detach saved references before fallible actions and attempt shutdown, owner release and started-thread join through one accumulator; verify shared precedence, all independent attempts, no repeated/reentrant listener release and real loopback listener/worker release. Concurrent/incomplete outcome retention and worker-safe deferred release are not included.
- [x] 0.6 Add 29 focused regression cases for the delivered repairs in `tests/test_network_resource_minimal_cleanup.py`; verify they pass in the complete suite, covering operation-plus-close, close-only/interruption, repeat/reentrant cleanup, independent actions, retained iteration and real listener release. Record unsuccessful development runs separately rather than presenting them as passing acceptance.
- [x] 0.7 Regenerate the existing `eliminate-unsafe-direct-descriptor-closes/phase6bd-process-inventory.json` for the minimal patch's changed source hashes/dependency edges; verify the unchanged generated-inventory equality test passes without modifying original specifications, completion states or historical acceptance.
- [x] 0.8 Verify the minimal patch with the full eight-worker pytest command, typecheck, diff check and strict validation of both changes; preserve successful statuses and complete logs in `minimal-fix-verification.md`: 7862 passed, 13 skipped, 11 warnings and 17753 subtests passed. Explicit unittest discovery and full expanded-plan acceptance are not claimed.

## 1. Buffered HTTP response ownership

### RED

- [ ] 1.1 Capture buffered request signatures and passing characterization for methods, URLs, headers/auth, timeout, successful/error status-body-header values, no-cache/cache consumers, normal OSError-to-503 mapping and raw failures inside the HTTPError handler; record named tests and results in `verification.md` before further production changes, using the delivered minimal patch as the current baseline.
- [ ] 1.2 Reuse the completed retained-HTTPError regressions as passing characterization of the repaired preflight case; extend them with success/error/no-body and acquisition-failure ownership cases, demonstrating only unmet contracts RED and requiring exactly one underlying release per acquired body, no fabricated close and unchanged returned data.
- [ ] 1.3 Add preparation-before-request and operation-plus-close, sole close, unexpected defect, interruption, repeated/reentrant/concurrent finalization matrices for both response paths; prove unmet contracts RED, including exact selected identity, secondary retention, owner rejection leaving caller resources untouched and no second fileno owner.

### GREEN

- [ ] 1.4 Implement a private prepared HTTP response release adapter under `docker/versioning/` with one pre-acquisition `OwnedStream`, non-calling resource binding and `CleanupFailures`; verify task 1.3 with no foundation public API change or use of process-private transfer functions.
- [ ] 1.5 Migrate buffered success and HTTPError paths to that owner, preserving the characterized error-handler distinction and mapping a newly observed sole ordinary release failure to the established 503 response; verify tasks 1.1–1.3, no raw-note leakage, no retry and no independently releasing native response context manager.

### INTROSPECT / VALIDATE

- [ ] 1.6 Add origin/AST assertions for preparation before urlopen, caught error-response binding before read, the concrete terminal owner-to-adapter-to-response release chain and unchanged signatures; verify negative missing-binding, double/native-context release, aliased borrowed close and raw-fileno fixtures fail while valid ownership passes.
- [ ] 1.7 Add concise buffered/error-response lifecycle guidance and a link to the executable regression in the lifecycle docs; run link/API checks and focused transport/provider/cache tests, preserving complete logs and no-drift evidence in `verification.md` before group 2.

## 2. Streaming HTTP responses and artifact consumption

**Depends on:** group 1's prepared HTTP owner and passing verification.

### RED

- [ ] 2.1 Characterize lazy acquisition, request configuration, chunk sizes/order/bytes, EOF, streaming HTTP errors and existing artifact transport-error/temporary-cleanup/publication behavior; verify passing baseline tests and record their names/results separately from new RED cases.
- [ ] 2.2 Add response-ownership tests for EOF, acquisition HTTPError with/without body, read failure, explicit generator close/throw, first/later interruption, sole release failure and repeat/reentrant cleanup; demonstrate missing accounting or precedence RED, including Python GeneratorExit semantics and exact error identity.
- [ ] 2.3 Add externally retained iterator tests for consumer chunk-type, hash, write and cancellation failures combined with iterator/response/temp/lock cleanup failures; reuse the delivered write/iterator-close regressions and demonstrate remaining unmet matrix cases RED, requiring every safe independent cleanup once, no artifact publication, operation authority and compatibility with plain injected iterators lacking close.

### GREEN

- [ ] 2.4 Migrate `HttpStreamingTransport.fetch_chunks` to the prepared response owner, accounting for HTTPError resources before exact propagation and retaining lazy bounded streaming; verify tasks 2.1–2.2 without buffering, extra requests, retries or raw socket ownership.
- [ ] 2.5 Verify and extend the delivered artifact-consumer finalization against the full task 2.3 matrix under shared precedence, leaving non-closeable compatible iterators unchanged; require retained-reference cleanup without GC, skipped temporary cleanup or finalize/publication after failure.

### INTROSPECT / VALIDATE

- [ ] 2.6 Extend concrete iterator/response origin proofs and positive/negative fixtures for unowned error bodies, omitted consumer finalization, aliased iterator close, reset owners and implicit double release; update streaming lifecycle guidance with checked executable links and verify all fixtures/docs pass.
- [ ] 2.7 Run focused streaming transport, artifact-cache/materialization, integrity, publication, progress and temporary-cleanup suites plus loopback response EOF/early-termination tests; verify fd/resource release independently of doubles and record complete results/no-drift in `verification.md` before group 3.

## 3. Probe listener startup and coordinated shutdown

**Depends on:** groups 1–2 verification; reuse the existing filesystem ownership/precedence contract, not HTTP policy.

### RED

- [ ] 3.1 Capture probe/diagnosis signatures and passing token, bind, gateway-order, Docker-mode, command, persistence, successful stop and facade-error behavior; verify baseline tests and record the existing five-second worker-join allowance before edits.
- [ ] 3.2 Add acquisition-ledger failures for owner/lock preparation, server construction, address inspection, thread construction/start/readiness, and a worker starting then failing before serving-loop entry; demonstrate unmet cases RED with one listener owner, no request before preparation, no deadlock on a never-entered loop and no join of a never-started worker.
- [ ] 3.3 Add stop/diagnosis operation-plus-release, sole release, interruption, unexpected-defect and independent-cleanup matrices; prove RED for skipped actions, raw-finally masking or repeat close, requiring exact shared precedence and one safe attempt per action.
- [ ] 3.4 Add deterministic repeated/concurrent/reentrant stop and start/stop races, blocked winning release, bounded worker-wait expiry and eventual worker-safe settlement; prove missing retention RED, rejecting claimed-as-complete outcomes, deadline resets, dropped owners and a second start while an earlier generation remains owned. Ensure all controlled workers/resources are released in test cleanup.

### GREEN

- [ ] 3.5 Extend the delivered private listener adapter, prepared `OwnedStream` and immediate returned-server binding to account for every partial startup outcome and actual serving-loop readiness/exit; enforce active-start rejection across lifecycle races and verify task 3.2 without new filesystem APIs or raw socket/fileno owners.
- [ ] 3.6 Implement one per-generation stop request, tracked worker-safe release, shared five-second worker-wait deadline, retained winning outcome and eventual worker-exit finalization through the same owner; verify tasks 3.3–3.4, including controlled RuntimeError on incomplete observation, no new budgets/retries/late output and independent safe cleanup after failures.
- [ ] 3.7 Protect `diagnose_gateway` teardown using shared failure precedence without changing gateway/CLI policy; verify task 3.3's active-diagnosis failures remain exact over ordinary cleanup failures and tasks 3.1's compatibility comparisons pass.

### INTROSPECT / VALIDATE

- [ ] 3.8 Add structural listener factory/binding/server_close alias, readiness-barrier, deadline-guard and winning-outcome proofs, rejecting premature close, skipped join/independent cleanup and discarded incomplete ownership; document supported stop/incomplete behavior with checked executable test links and verify positive/negative fixtures pass.
- [ ] 3.9 Run networking/facade and fault/race suites plus Docker-free loopback token exchange, listener/port release, safe unrelated-fd reuse and worker-completion tests; require bounded supported behavior, no surviving completed-lifecycle worker and recorded no-drift results in `verification.md`.

## 4. Integrated network-resource enforcement and inventory

**Depends on:** groups 1–3.

### RED / GREEN

- [ ] 4.1 Extend complete-production-tree fixtures under `tests/enforcement/` to cover aliased/keyword urlopen, error-response delivery, iterator finalization, server factories and assigned server_close callables in new modules; prove previously missed cases RED and extend origin resolution so every fixture passes without a historical consumer-function allowlist.
- [ ] 4.2 Add ownership mutation fixtures for preparation gaps, fake owner/class names, native-context double release, raw-fileno double ownership, reset-per-cleanup owners, unsafe live-worker close, lost concurrent failures and false settlement; verify all are rejected by the actual integrated gate and valid concrete adapters/plain non-descriptor iterators remain accepted.

### INTROSPECT / VALIDATE

- [ ] 4.3 Generate a source-located network-resource ledger covering each acquisition, error-response binding, iterator finalizer, listener/response release delegate, worker safety barrier and retained cleanup outcome; add regeneration-equality tests and record the three-category release classification and ownership proof for every row in `verification.md`.
- [ ] 4.4 Add executable API/dependency assertions for unchanged public transport/networking fields/signatures, bounded filesystem surface, explicit foundation imports, no HTTP/server domain imports in filesystem and no process-owner dependency introduced by this work; verify assertions and isolated import checks pass.
- [ ] 4.5 Regenerate `eliminate-unsafe-direct-descriptor-closes/phase6bd-process-inventory.json` again after the remaining implementation changes using its existing generator as derived source-hash/DAG evidence; verify its unchanged equality assertion passes and record differences here without modifying original specifications, checkboxes or historical acceptance, or treating regeneration as Phase 7 completion.
- [ ] 4.6 Run combined groups 1–3 characterization and enforcement/docs/API tests; record no drift except the specified lifecycle corrections, complete logs/statuses and original-change follow-up requirements in this change's `verification.md` before final validation.

## 5. Final validation

**Depends on:** all preceding deliverables and their verification.

- [ ] 5.1 Run `.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q`; preserve full stdout/stderr, exit status, totals, skips, warnings and duration in `verification.md`, requiring success without exclusions or warning suppression.
- [ ] 5.2 Run `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'`; preserve full output, exit status, total tests, skips and duration in `verification.md`, requiring success independently of pytest.
- [ ] 5.3 Run `ty check docker --python-version 3.14 --output-format concise` and `git diff --check`; record full outputs and individual successful exit statuses in `verification.md`.
- [ ] 5.4 Run `openspec validate own-http-response-and-probe-server-lifecycles --strict` and validate `eliminate-unsafe-direct-descriptor-closes --strict` after derived-inventory maintenance; require both successful statuses before accepting this implementation, with no automatic archive, commit or original Phase 7 completion.

## Workflow follow-up

- Review and implement the remaining improvements before resuming the original change's blocked Phase 7; the delivered minimal repairs do not establish the full convergence guarantees.
- After implementation, explicitly resume the original apply workflow to rerun its complete-tree audit and release gates and record updated blocker evidence. Keep its existing requirements and task completion criteria intact.
- Archive either change only when explicitly requested and its own completion criteria are satisfied.

# Phase 6BC value-result migration — incremental GREEN evidence

## Scope decision

The user clarified that deleting public `collect_streams` was not planned. Keep its standalone arbitrary-byte diagnostic API and package export, and remove only obsolete process-lifecycle coordination from subprocess adapters. The earlier public-collector question is resolved, not an implementation blocker. No planning requirements have been rewritten during apply.

## RED 6BC.10

The 166 adapter result/prefix/timeout/interruption/missing-stream cases and producer/helper/API inventory are recorded in `phase6bc-orthogonal-red-verification.md`. The public collector ambiguity is resolved as above. Its original RED log is `/tmp/docker-constructor-6bc-adapter-projection-red.pV9Z9j.log` (166 failed, runner exit 1); it is not successful implementation verification. Preserve existing removal/staging/real-pipe compatibility cases for their unaffected invariants; their complete migrated acceptance is still required in 6BC.15–18/23–24.

## Generic GREEN 6BC.11–14

Implemented exact immutable FailureInfo/FailureKind/FailurePhase, strict ActionResult transport, LifecycleResult and ProcessResult.lifecycle. The private occurrence ledger retains shared CleanupFailures priority selection without complete()-time exception notes, records boundary context and publishes authority first, deduplicating exact error identities and preserving winning occurrence context on reused objects. No generic CleanupResult alias, parallel authority fields or exception output snapshots remain.

Capture/stream return ordinary and unexpected failures as values. Once preparation is usable, failed factory/setup returns the original owner; earlier preparation may raise. Control flow propagates after bounded independent cleanup without output snapshots. Collection uses one prepared nonblocking claim latch, not serial subscriptions; callback validation precedes the claim. Context exit distinguishes claimed from actually delivered collection so an active body before delivery still participates in priority selection. Actual interruption delivery marks the owner delivered before propagation, preventing new post-delivery policy requests.

Typed chunk/end callbacks retain prompt independent EOF, stable ABORTED/READ_FAILED, per-stream once-only attempts, pre-registration/racing completion and no replay. Callback/reporter/fallback interruptions publish their actual context immediately through the shared ledger; ordinary diagnostic failures stay outside lifecycle failure storage. Policy coordination defects cannot bypass independent reap/releases. A refused cleanup-observation attribute cannot replace the first interruption or lose retained owners.

All generic action producers and generic/auxiliary/captured test fixtures now return explicit typed failures. Valid helper handoff preserves kind/error while rebasing to ACTION/None. Canonical validation rejects malformed observations; field-access interruptions are protected, actual supplied control flow is retained, independent cleanup continues and invalid transport is never normalized into legacy input. Progress remains coordinator-private; final LifecycleResult has no nested action error stores. Repeated raised observer defects are bounded by precedence class, while cumulative valid failures retain exact identity and do not disappear on later success.

Migrated all `tests/test_process*.py` contracts, including historical snapshot/subscription tests rather than preserving their obsolete API expectations. Existing matrix dimensions are retained: the main lifecycle module still has 117 cases, configured-policy faults 109, captured-action faults 79, reader-source 19, and adapter compatibility 79. Assertions now inspect typed returned values or lifecycle observation on control flow, verify absence of exception output attributes, require second-call rejection and preserve sole release, independent cleanup, exact identity, budgets and real-descriptor barriers. The existing unrelated descriptor test now uses an explicitly prevalidated OwnedDescriptor rather than a raw finally close.

## Verified gates (overlapping, not additive)

All pytest commands use `.venv/bin/python -m pytest ... -n 8 --dist loadfile --tb=short -q`; complete stdout/stderr and runner status are in each log.

- New orthogonal surface/collection/context/action/reporting/adapter gate: `/tmp/docker-constructor-6bc-value-green.pVaAd0.log`, **429 passed, exit 0**.
- Captured-action, value-failure and API/executable-doc gate: `/tmp/docker-constructor-6bc-captured-action-green.LT0N3h.log`, **126 passed, exit 0**.
- Migrated identity/context/source gate: `/tmp/docker-constructor-6bc-migrated-context-green.OEzBZe.log`, **362 passed, exit 0**.
- Configured policy/API/source/compatibility: `/tmp/docker-constructor-6bc-policy-migration.Wxrczo.log`, **182 passed, exit 0**.
- Readiness/action/fd-reuse faults: `/tmp/docker-constructor-6bc-policy-fault-green.btGdvC.log`, **109 passed, exit 0**.
- Protected core + strict producer/fixture gate: `/tmp/docker-constructor-6bc-core-protected-green.YrkMX8.log`, **836 passed, exit 0**.
- Migrated complete generic lifecycle module: `/tmp/docker-constructor-6bc-lifecycle-migration.9FrRvZ.log`, **117 passed, exit 0**.
- All generic process test modules: `/tmp/docker-constructor-6bc-generic-migration.FOvDUk.log`, **906 passed, exit 0**.
- Latest all-generic plus orthogonal adapter projection gate, including added latch/context/refused-attribute regressions: `/tmp/docker-constructor-6bc-generic-latch-green.FAkUIc.log`, **1095 passed, exit 0**. Exact command: `.venv/bin/python -m pytest tests/test_process*.py tests/test_npm_environment_orthogonal_projection.py -n 8 --dist loadfile --tb=short -q`.
- Static gate: `/tmp/docker-constructor-6bc-generic-static.wYYiKt.log`, **exit 0** for each of `ty check docker --python-version 3.14 --output-format concise`, `git diff --check`, and strict OpenSpec validation.

## Unsuccessful intermediate verification retained

- `/tmp/docker-constructor-6bc-legacy-migration.NUNafQ.log`: **544 failed, 953 passed, 24 warnings, 1 collection error, 411 subtests passed; runner exit 2**, including an interrupted xdist worker. This was unsuccessful. Legacy generic producers/consumers and the removed CleanupResult import caused failures; the generic modules and producer fixtures have since been migrated and verified above. Npm compatibility migration remains pending; this old broad run is not final acceptance.
- `/tmp/docker-constructor-6bc-api-green.sztH2d.log`: **1 failed, 34 passed, exit 1**. Transfer/reader barrier AST normalization and bounded wait-only observation proof were corrected, with new negative mutations and subsequent passing API gates. No ownership assertion was waived.
- An intermediate typecheck reported 21 missing `_ledger` attribute annotations; explicit private ledger/status annotations were added and the static gate above passes.

## Remaining obligations

Npm captured/streaming integration fixtures, timeout/removal/staging compatibility, obsolete private coordinator removal, final assembler docs and full API/audit/full-suite gates remain pending. Production adapter code and process-guide examples already consume the new values, but scoped successes do not by themselves complete 6BC.15–24. No full-phase or full-suite acceptance is claimed. No commits or index operations were performed.

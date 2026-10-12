# Binding Implementation Contract

**Prerequisite:** `eliminate-unsafe-direct-descriptor-closes` must finish and pass its release gates before implementation starts here. Artifact readiness alone does not satisfy that dependency. Consume its generic launch-selected policy/stage/drain/managed-action API without reimplementing or expanding it; pause if that contract is insufficient. Every main/control pipe has one internal OwnedStream, and no wrapper independently signals/waits/reaps the main child or releases its streams.

This change plans the Docker operation wrapper, prelaunch cancellation, cooperative whole-container control, Docker stage mapping, separate local/container outcomes and their tests/documentation. These features are not implemented yet; all tasks below remain pending. Generic acceptance logs are prerequisite API evidence only, not acceptance of this Docker behavior.

Groups 2 → 3 → 4 follow RED → GREEN → INTROSPECT → VALIDATE in order. Group 1 is the dependency/passing-characterization gate; group 5 is integrated acceptance only. Keep shared precedence, first-interruption identity, locked pre-delivery publication, once-only logical stages, bounded command attempts and at-most-once releases and retained incomplete ownership. Do not change consumer signatures, persistent representations, normal output/identities, URL-display policy or the prior total shutdown cap beyond the specifically authorized cancellation/container-stop/final-tail corrections.

## 1. Prerequisite and compatibility gate

- [ ] 1.1 Verify and record that the prerequisite's full implementation, generic policy/API/managed-action examples, behavior-preserving adapters, convergence and release checks are complete and accepted. Check actual implementation/evidence, not just artifact existence or baseline 6BB.1–6BB.13; stop here if unmet. Record the exact available signatures and ownership/dependency contract for later groups.
- [ ] 1.2 Record passing consumer characterization for signatures, list/LifecycleOutcome/error shapes, main/helper factories/missing streams, decoding/tail limits, URL-display/abort redaction, Docker identity/absence evidence, existing staging/cache guarantees and path-specific total shutdown bounds. Pin old terminate-CLI-first order as baseline only; preserve complete logs/statuses before new RED tests.

- [ ] 1.3 Run scripts/collect-docker-cancellation-evidence.py manually on each supported host Docker version; preserve its command logs, timings, versions, summary/exit status and cleanup report. This protocol evidence collection may precede the implementation prerequisite gate and does not prove wrapper acceptance. Verify CID timing, stop-before-start, removal-before-start, concurrent start/removal and scoped label recovery; retain failures rather than presenting simulation as daemon evidence.

## 2. Docker operation composition and prelaunch cancellation

### RED

- [ ] 2.1 Add prelaunch shutdown tests requiring irreversible cancelled-before-launch status, no generic launch/factory/control commands or pipes, repeat-launch suppression and independent cleanup of any prepared wrapper resources. Require cancelled-not-success/nonexistent-container semantics through unchanged consumer interfaces; prove RED.
- [ ] 2.2 Add barrier-controlled acceptance/shutdown races, pending owner handoff, failed-launch cleanup observation and test-controlled blocked acquisition. Require a short state lock never held across launch/readiness/callbacks, exactly one main owner, exactly-once request forwarding and pending-not-complete observation without retries/raw control; prove RED and release/join all controlled workers in finally.

### GREEN

- [ ] 2.3 Implement the Docker domain composition object and irreversible prelaunch gate using a short state lock for state/pending request/owner handoff. Consume OwnedProcess.launch without adding generic prepare/start or subclass shutdown overrides; verify 2.1–2.2, both race winners, failed-launch observation and no independent main signal/wait/reap/pipe release.
- [ ] 2.4 Document wrapper construction, cancelled-before-launch behavior, atomic acceptance, pending/failed owner handoff and blocked-factory limits in the assembler guide with executable examples. Verify examples and distinguish preliminary pending observation from terminal cleanup delivery.

### INTROSPECT

- [ ] 2.5 Add structural/dependency proofs rejecting lock-across-launch, lost/reset pending cancellation, second main owner/retry, generic two-phase launch additions and raw main-process/pipe authority. Verify wrapper → generic process → filesystem direction and unchanged consumer signatures.

### VALIDATE

- [ ] 2.6 Run composition/cancellation/race/failed-launch/example/proof suites with the prerequisite's relevant launch/precedence tests. Retain complete logs/statuses and verify no surviving supported workers on completed cleanup before group 3.

## 3. Whole-container stop policy and distinct stage timeouts

### RED

- [ ] 3.1 Add deterministic command/order/clock tests for T_soft covering docker stop plus natural original-CLI waiting, and T_hard covering docker rm -f plus waiting before local signals. Require soft error/expiry → hard stage, hard error/expiry → generic local fallback, immediate advancement on nonretryable errors or exhausted attempts, successful or causally qualified absent final removal without unnecessary hard sleeping, with uncertain absence advancing bounded stages but never confirming cleanup, at-most-once stages and all autonomous/explicit triggers; prove RED.
- [ ] 3.2 Add helper-client preparation/launch/transfer/close/join/reap/stall fault tests and invalid/nonfinite/nonpositive/over-total phase settings. Require one internal stream owner per acquired pipe, default local policy for helpers, stage-and-parent caps, daemon stop-grace conversion, preserved local reserve and no clock restart per command/post-command wait/concurrent caller; prove RED with managed real-subprocess fixtures where applicable.

- [ ] 3.8 Add RED fake-clock/jitter/barrier tests for private missing/empty/partial/stale CID files, 10-second maximum observation capped by soft/total budgets, 10 ms doubling delays with ±10% jitter, sequential three-attempt transient launch/daemon/lost-response retries and no retry for interruption/defect/permanent failure. Test shared counters, pending helper cleanup blocking another attempt, reserved recovery/local time, exact-ID idempotence and managed/PID/operation-token discovery including stopped containers, PID reuse and unrelated operations.

- [ ] 3.10 Add policy-level creation-race RED tests before group-3 implementation: cancel while CID/Create is pending; deliver ID before expiry; create before/during scoped recovery; and create after an empty sweep or terminal delivery. Use barriers, a fake clock and injected jitter to prove stop-before-start is not cancellation, exact-ID removal handles either Start/removal race winner, uncertain settlement stays unconfirmed and shared attempt counters/deadlines/local reserve are preserved. Release/join all controlled workers. Consumer field/error projection is separately verified in group 4.

### GREEN

- [ ] 3.3 Implement domain-local whole-container stop/force-removal/evidence policy on the prerequisite's narrow stage API. Keep Docker argv/identity/grace conversion outside the generic mechanism and local signalling/reaping inside it; verify 3.1–3.2, no early terminate after soft expiry or successful force removal with hard waiting time left. Treat uncertain absence as a stage failure/transition observation, not cleanup confirmation; preserve once-only logical stages, bounded shared attempts and all budgets while verifying CID observation/recovery under 3.8–3.9.
- [ ] 3.4 Integrate separately owned stop/removal helper clients through the prerequisite captured/default-local adapters with capped stage/parent deadlines. Retain failed/pending helper cleanup handles without recursive container policies or close fallback; verify 3.2 accounting and independent generic cleanup after Docker-control failures.
- [ ] 3.9 Implement fresh protected --cidfile preparation/observation, validated identity handoff, bounded classified retries and exact-label recovery through separately owned default-policy helpers. Verify group-3 RED contracts 3.8 and 3.10, shared per-command/ID counters across recovery, no second main acquisition, no overlap with unresolved helper cleanup, and unconfirmed cleanup when discovery or removal cannot establish settlement.
- [ ] 3.5 Document soft/hard/local/total budgets, clock start before commands, immediate-error transitions, final removal and helper ownership with runnable policy examples. Include CID observation, 10-second cap, backoff/jitter, three-attempt classification and exact-label recovery. Verify examples/order/defaults against 3.1–3.2 and 3.8 and preserve existing policy-identity rules when reviewed execution limits change.

### INTROSPECT

- [ ] 3.6 Prove the domain policy consumes existing generic configuration without new generic Docker imports/API machinery or independent main lifecycle control. Add evasion fixtures for premature local signals, missing hard waiting, late timer start, fresh helper grace, over-total phase allocation and recursive helper policy; require rejection and alias-resolved owner evidence.

### VALIDATE

- [ ] 3.7 Run domain policy/control-client/order/clock/fault/proof/example tests with the prerequisite's staged-policy real-pipe gates. Preserve full logs/statuses, measured/reviewed total bounds and once-only logical-stage, bounded command-attempt and exactly-once resource ledgers including all policy-level creation races in 3.10 before group 4.

## 4. Consumer integration, final output and separate outcomes

### RED

- [ ] 4.1 Add local-complete/container-unconfirmed and daemon-unavailable/ambiguous stop/removal reporting tests through existing results/lists/LifecycleOutcome/errors. Include active ValueError/domain Exception versus cleanup defects, first/later interruptions and the exact publication-before-phase-notification gap; require exact identity, retained secondary diagnostics once, staging cleanup and no late mutation/output/user callbacks/new control requests; prove RED.
- [ ] 4.8 After group 3 passes, add consumer-integration RED coverage of the policy races already proved by 3.10, using barriers, a fake clock and injected jitter. Deliver ID before expiry and require exact-ID stop/removal; expire observation and create a labeled container before/during recovery; also allow creation after an empty sweep or terminal delivery and require unconfirmed cleanup. Prove stop-before-start cannot confirm cancellation, successful exact-ID removal handles either race winner, and shared bounded retries preserve budgets, local reserve, authoritative failures and staging cleanup without late requests/mutation. Prove RED and release/join all controlled workers in finally.
- [ ] 4.2 Add real-subprocess Docker-command fixtures for large final stdout/stderr, cooperative and forced stop, slow control operations, callback backpressure, partial readers, descendant-held pipes, reader failure and deadline races. Require selected bounded tails, safe abort finalization, actual reader-stop-before-release, unrelated-fd safety and no supported workers on completed local cleanup; prove RED independently of descriptor-less doubles.

### GREEN

- [ ] 4.3 Integrate streaming execution/hooks with the composition object and policy selected at launch, preserving normal signatures/output/tails/redaction and staging/cache authority. Verify 1.2 and 4.1–4.2 across all shutdown triggers; reuse generic readers/cleanup rather than duplicating them, and allow only documented cancellation/final-tail corrections.
- [ ] 4.4 Retain separate local-resource and confirmed/unconfirmed container stop/removal outcomes and project bounded sanitized controlled diagnostics through existing consumer interfaces. Verify 4.1 including prelaunch cancellation, unresolved acquisition, local-complete/remote-unknown, interruption authority, late helper observations and no retroactive success. Verify 4.8 and 3.8–3.9: qualify absence using creation-settlement, exact ID and causally subsequent evidence, never infer settlement from CLI exit/reaping or elapsed time, and explicitly report unconfirmed cleanup when bounded available evidence is insufficient.
- [ ] 4.5 Update assembler/process-guide integration examples with the cooperative sequence, separate status, final abort-redacted drain and late observation rules. Document the CLI-launch/daemon-creation gap, why early absent evidence and local reaping cannot prove cleanup, and bounded CID observation/retries/scoped label recovery and unconfirmed reporting without orphan-prevention guarantees. Execute examples and cross-reference checks; verify documented staging cleanup and normal/failure output against 4.1–4.2 and 4.8.

### INTROSPECT

- [ ] 4.6 Add adapter signature/field/AST/import proofs for composition, launch-selected policy and separately owned default-local helpers. Reject duplicated coordinator/reader state, raw-fileno double ownership, main raw control, unsanitized generic-note projection, runtime-installer dependency and generic filesystem/process domain leakage.

### VALIDATE

- [ ] 4.7 Run focused npm streaming/execution/captured-helper/deadline/redaction/tail/host/cooperative real-pipe suites plus dependency generic gates. Preserve complete logs/statuses and compare normal behavior, approved cancellation tails, stage order/caps, delayed-creation race evidence from 4.8, local/container status, cache/staging guarantees and worker completion before integrated acceptance.

## 5. Integrated ownership and release checks

- [ ] 5.1 Extend complete-tree main/control-client alias-resolved ownership/convergence and executable guidance checks with wrapper gate/handoff, policy stage clocks, separate outcomes and retained helpers. Reject fake owner names/imports, lost owners, raw closes, premature release/false completion and domain/reverse imports; preserve the prerequisite's three-category release and generic API proofs.
- [ ] 5.2 Run `.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q`, preserving complete stdout/stderr and runner exit status. Record counts/skips/warnings/duration and cooperative fault/retention/no-drift evidence in this change's verification.md; require exit 0 without excluding failures or claiming prerequisite logs prove Docker acceptance.
- [ ] 5.3 Run `ty check docker --python-version 3.14 --output-format concise` and the configured `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'` check; preserve complete logs/statuses and require successful results for the final implementation.
- [ ] 5.4 Run `git diff --check` and strict validation of this change; record successful outputs in verification.md and confirm MODIFIED requirements contain complete prerequisite/main blocks with all unaffected scenarios retained.
- [ ] 5.5 Record the final dependency DAG, preserved generic API/consumer signatures, generated main/helper resource ledger and approved behavior differences. Verify no generic implementation expansion, new persistent representation/runtime packaging requirement, late public output or false remote completion.

## Workflow follow-up

- Sync/archive the completed prerequisite before archiving this dependent delta, so its MODIFIED pipe-ownership requirement exists in main specs. Do not infer inter-change completion from CLI artifact readiness.
- Review and archive this change only after its implementation and release checks are complete; archive is not part of this implementation checklist.

# Implementation Contract

This checklist is the binding implementation contract for `add-durable-filesystem-transactions`. A task is complete only when its stated production change or evidence exists and its stated verification passes. Every phase SHALL proceed in `RED → GREEN → INTROSPECT → VALIDATE` order; no task in a later stage of a phase may be marked complete while an earlier stage in that phase remains incomplete. A phase MAY depend only on lower-numbered phases named in its `Depends on` line. Independent phases MAY proceed in parallel.

```text
Phase 1 ──┬──▶ Phase 3 ──▶ Phase 4 ──▶ Phase 5 ──┐
          ├──▶ Phase 6 ──────────────────────────┤
Phase 2 ──┼──▶ Phase 3                           ├──▶ Phase 9 ─────▶ Phase 9A ──▶ Phase 10
          ├──▶ Phase 7 ──────────────────────────┤
          └──▶ Phase 8 ──────────────────────────┘
Phase 1 ─────▶ Phase 7
Phase 1 ─────▶ Phase 8
```

## Phase 1. L0–L2 Regular-File Substrate

**Depends on:** none
**Deliverables:** production `PosixFileOps`; live descriptor capabilities; validated read; atomic no-clobber; durable no-clobber; durable replacement; durable unlink; canonical JSON codec; deterministic fault injection and exception-lifecycle guarantees.

### RED

- [x] 1.1 Add L0 tests injecting partial writes and failures from openat/read/write/fstat/fsync/linkat/renameat/unlinkat/chmod/close; verify the focused suite fails because production `PosixFileOps` does not exist.
- [x] 1.2 Add L1 tests proving directory and regular-file capabilities retain live descriptors, accept only canonical basenames, reject released and cross-directory capabilities, and never reconstruct paths; verify the focused suite fails before capability implementation.
- [x] 1.3 Add validated-read tests for symlink, non-regular, foreign-owned, multiply linked, and forbidden-mode leaves through one retained no-follow descriptor; verify every unsafe case fails without target mutation.
- [x] 1.4 Add atomic-no-clobber tests for complete bytes, final mode before visibility, typed final `DESTINATION_EXISTS`, destination preservation, temporary cleanup, and absence of a parent-directory durability claim; verify the tests fail before the contract exists.
- [x] 1.5 Add temporary-allocation tests proving each `EEXIST` chooses a fresh name, collided entries remain unread and untouched, three total failed attempts produce allocation-stage failure, and no temporary collision becomes `DESTINATION_EXISTS`.
- [x] 1.6 Add durable-no-clobber fault tests proving file fsync precedes publication, parent fsync precedes success, final collision is a typed outcome, and post-publication parent-fsync failure remains failure despite a visible destination.
- [x] 1.7 Add durable-replacement fault tests proving complete writes, file fsync, safe-destination validation, replacement, parent fsync, and temporary cleanup occur in order; verify post-replacement parent-fsync failure remains failure despite a visible replacement.
- [x] 1.8 Add durable-unlink tests for present and declared-absent entries plus open/unlink/fsync/close failures; verify only the declared absent case is idempotent.
- [x] 1.9 Add exception-lifecycle tests proving raw `OSError` subclass/errno/chaining remain observable, `KeyboardInterrupt` and cancellation pass through unchanged, and cleanup/close failures never replace an existing primary failure.
- [x] 1.10 Add canonical JSON codec tests for deterministic UTF-8 bytes, stable mapping order, generic decoding, and rejection of non-finite or unsupported values; verify no generic envelope, schema/version interpretation, or path/deletion authority exists.

### GREEN

- [x] 1.11 Implement production `PosixFileOps` over descriptor-relative POSIX calls; verify task 1.1 passes without an in-memory path VFS or exception normalization.
- [x] 1.12 Implement validated directory and regular-file capabilities with live-descriptor and basename-only authority; verify tasks 1.2–1.3 pass.
- [x] 1.13 Implement atomic no-clobber publication with three-attempt temporary allocation and typed final collision; verify tasks 1.4–1.5 pass.
- [x] 1.14 Implement durable no-clobber and durable replacement as distinct operations with mandatory file/directory boundaries; verify tasks 1.6–1.7 pass without a durability boolean.
- [x] 1.15 Implement durable unlink and primary-error-preserving cleanup/close handling; verify tasks 1.8–1.9 pass.
- [x] 1.16 Implement the canonical JSON codec without an envelope API or domain validation; verify task 1.10 passes.

### INTROSPECT

- [x] 1.17 Review L0–L2 for descriptor leaks, leaf following, ancestor repair, pathname reconstruction, cross-filesystem promotion, partial writes, collision conflation, false durability, destination clobbering, interruption translation, cleanup masking, generic VFS growth, and domain authority leakage; resolve every finding and record the review outside specification files.

### VALIDATE

- [x] 1.18 Run the complete Phase 1 security and fault-injection suite across every open/write/fsync/link/replace/unlink/chmod/close boundary; record that all outcomes preserve the selected atomic/durable contract, unrelated entries, primary exceptions, and owned-resource cleanup.

## Phase 2. Owner-Private Advisory Locks

**Depends on:** none
**Deliverables:** validated owner-private advisory locks; mandatory `BLOCK`/`FAIL_FAST` policy; namespace-bound live capability; deterministic contention and lifecycle behavior.

### RED

- [x] 2.1 Add lock-entry tests for symlink, non-regular, foreign-owned, multiply linked, repairable owner-readable or owner-writable wrong modes, a missing lock created under restrictive umask, pre-existing owner-inaccessible modes, and bootstrap-race entries; verify repair occurs only after exclusive acquisition, including exact `0600` repair of a known-new umask-restricted inode, while pre-existing inaccessible and unsafe entries and unrelated ancestors remain untouched.
- [x] 2.2 Add process-level tests for same-namespace exclusion, different-namespace concurrency, explicit `BLOCK` waiting, and explicit `FAIL_FAST` rejection; verify no implicit contention policy is accepted.
- [x] 2.3 Add capability tests proving released, cross-namespace, wrong-root, and non-live lock capabilities fail before protected mutation.
- [x] 2.4 Add lifecycle tests for success, ordinary exception, interruption/cancellation, validation failure, contention failure, unlock failure, and close failure; verify primary exceptions remain authoritative.

### GREEN

- [x] 2.5 Implement secure lock preparation and descriptor validation: atomically create a missing lock with `O_CREAT|O_EXCL`, repair a known-new inode and any owner-readable or owner-writable safe single-link regular file to exactly `0600` only after exclusive acquisition and full validation, and fail closed without mutation for a pre-existing lock whose owner has neither read nor write access; verify task 2.1 passes.
- [x] 2.6 Implement mandatory `BLOCK` and `FAIL_FAST` acquisition with namespace-bound live capabilities; verify tasks 2.2–2.3 pass.
- [x] 2.7 Implement unconditional release with primary-error-preserving unlock/close handling; verify task 2.4 passes.

### INTROSPECT

- [x] 2.8 Review locking for pathname/descriptor TOCTOU, lock replacement, descriptor inheritance, capability reuse, lock-order inversion, ancestor mutation, pre-lock permission repair, restrictive-mode handling, implicit policy, interruption conversion, and cleanup masking; resolve every finding and record the review outside specification files.

### VALIDATE

- [x] 2.9 Run deterministic multiprocessing and lock-security suites; record same-namespace exclusion, different-namespace concurrency, exact contention behavior, post-acquisition repair of owner-accessible safe modes and known-new restrictive-umask creations, fail-closed preservation of pre-existing owner-inaccessible and unsafe entries, and release on every outcome.

## Phase 3. Immutable Build Generations

**Depends on:** Phase 1, Phase 2
**Deliverables:** build-owned closed manifest schema; canonical fixed-width generations; zero/one/two-state classification; durable no-clobber publication; discovery-time generation-directory synchronization; fail-closed ambiguous-state handling.

### RED

- [x] 3.1 Add generation-name tests for exact 20-digit nonzero suffixes, numeric/lexical ordering, overflow, malformed generation names, and exclusion of legacy `committed-build.json` from discovery; verify malformed generation names grant no authority and the legacy name is not inspected.
- [x] 3.2 Add build-owned manifest tests for explicit schema version, unknown versions/fields, canonical unique digest identities, deterministic codec bytes, duplicates, unsafe values, and unsafe entries; verify no shared envelope performs validation.
- [x] 3.3 Add state-classification tests for zero generations, one stable generation, two pending-cleanup generations, more than two generations, and corrupt generations; verify ambiguous state causes no mutation. Add restart tests where a complete generation is visible after publication but the original parent-directory fsync failed; verify discovery does not accept authority or permit cleanup until a fresh generation-directory fsync succeeds.
- [x] 3.4 Add publication fault tests for allocation, write, file fsync, no-clobber commit, and directory fsync; verify authority changes only after the durable contract completes and a visible post-publication/pre-fsync failure is completed only by successful discovery-time directory synchronization.

### GREEN

- [x] 3.5 Implement build-domain generation parsing and closed manifest validation over the canonical JSON codec; verify tasks 3.1–3.2 pass.
- [x] 3.6 Implement zero/one/two-generation classification, fail-closed corrupt/excess-generation handling, exclusion of legacy `committed-build.json` from discovery, and mandatory generation-directory fsync before discovered authority is accepted; verify task 3.3 passes.
- [x] 3.7 Implement `max + 1` allocation under the checkout lock and durable no-clobber generation publication; verify task 3.4 passes.

### INTROSPECT

- [x] 3.8 Review generation handling for counter ambiguity, overflow, replacement, authority before initial or discovery-time directory fsync, visible publication after failed fsync, permissive JSON, generic-envelope leakage, malformed-state mutation, and lock-capability mismatch; resolve every finding and record the review.

### VALIDATE

- [x] 3.9 Run the complete generation schema/state/publication and restart suite with all injected durability failures; record that no discovered generation grants authority until the generation directory has been successfully synchronized and each result is zero-generation initial state, one stable generation, two recoverable generations, or fail-closed ambiguity.

## Phase 4. Sequential Cleanup and Recovery

**Depends on:** Phase 3
**Deliverables:** durable authoritative-generation marker reconciliation; bounded `previous - current` cleanup; all-candidate attempts; durable blob/marker absence; aggregate failures; durable predecessor removal after candidate-directory synchronization; deterministic restart recovery.

### RED

- [x] 4.1 Add candidate-set tests proving cleanup is exactly canonical `previous - current` and no current-generation identity is deletable.
- [x] 4.2 Add partial-failure tests proving every candidate is attempted; blob unlink, marker unlink, per-directory post-batch fsync, and shared marker-directory post-batch fsync failures are aggregated; successful deletions are still synchronized after another candidate fails; and the predecessor remains after any unsuccessful durability step.
- [x] 4.3 Add completion, ordering, and fsync-count tests proving markers for every authoritative-generation blob are removed only after generation authority is durable and the shared marker directory is fsynced once before superseded cleanup. Inject marker unlink and marker-directory fsync failures; verify they preserve any existing predecessor and block superseded cleanup. Prove candidate removals are grouped by parent directory, each affected existing blob directory and the shared marker directory are fsynced exactly once after their complete batches rather than once per candidate, every candidate-cleanup failure retains the predecessor, and the predecessor is unlinked only after all candidate batches are durable. Cover retries that find an entire batch already absent after earlier unlinks succeeded but directory fsync failed; require one fresh fsync of that directory before batch completion. Separately prove that generation-directory fsync failure after successful predecessor unlink is reported as failure without claiming that the predecessor remains visible.
- [x] 4.4 Add interruption tests at authoritative-generation marker unlink and marker-directory fsync, every candidate blob/marker deletion, candidate-directory fsync, predecessor unlink, and generation-directory fsync boundary. Verify marker reconciliation recovery fsyncs the marker directory even when all authoritative markers are already absent and preserves any existing predecessor on failure. For failure or interruption after predecessor unlink but before its directory fsync completes, verify restart may observe either one stable generation or two recoverable generations; require discovery-time generation-directory synchronization before accepting the one-generation state, or idempotent cleanup before reducing the two-generation state, without unsafe deletion or loss of still-required recovery evidence.

### GREEN

- [x] 4.5 Implement build-domain candidate derivation through validated canonical identities and existing contained blob/marker authority; verify task 4.1 passes.
- [x] 4.6 Implement post-authority marker reconciliation for every current-generation blob with one marker-directory fsync before superseded cleanup, including recovery when all markers are already absent; on failure preserve any existing predecessor and stop before superseded cleanup. Then implement all-candidate cleanup with blob/marker removals grouped by containing directory, one post-batch fsync per affected existing blob directory and one for the shared marker directory, required batch synchronization even when recovery finds all assigned targets already absent, aggregate diagnostics, and predecessor preservation; verify tasks 4.2–4.3 pass.
- [x] 4.7 Implement durable predecessor removal only after every candidate's durable absence and restart recovery under the checkout lock; verify tasks 4.3–4.4 pass.

### INTROSPECT

- [x] 4.8 Review cleanup for early evidence removal, non-durable blob/marker unlink, missing or per-candidate redundant directory fsync, incorrect batch boundaries, stop-on-first-error behavior, current-blob deletion, marker authority leakage, path interpretation below L3, error masking, and retries that mistake visible absence for durable absence; resolve every finding and record the review.

### VALIDATE

- [x] 4.9 Run exhaustive cleanup/recovery fault injection across authoritative-generation marker unlink and marker-directory fsync, superseded blob unlink, superseded marker unlink, each unique blob-directory post-batch fsync, the superseded-marker post-batch fsync, predecessor unlink, and generation-directory fsync. Record that authoritative-marker failure leaves the generation committed, preserves any existing predecessor, blocks cleanup and build work, and is recovered before either may proceed. Record that fsync count scales with unique affected directories rather than candidate count, no current blob is deleted, all candidates are attempted, and every candidate-cleanup durability failure retains the predecessor. Explicitly test and report generation-directory fsync failure after successful predecessor unlink without requiring predecessor retention; after restart and discovery synchronization, accept either one stable generation or two generations that complete idempotent recovery, with successful completion leaving one stable generation durably.

## Phase 5. Build-Cache and Orchestration Integration

**Depends on:** Phase 4
**Deliverables:** migrated checkout build lock and control state; immutable-generation commit; pre-build recovery; post-build cleanup; `OPERATIONAL`/4 mapping; preserved marker, TTL, snapshot, blob, and XDG policies.

### RED

- [x] 5.1 Add parity tests for checkout-wide `FAIL_FAST`, competing-build rejection before mutation, canonical project/cache binding, and release on every outcome.
- [x] 5.2 Add parity tests for marker retention before generation commit, durable batch removal of markers for blobs admitted to the authoritative generation, exact fixed TTL for blobs that remain uncommitted, snapshot recovery/cleanup, content-addressed blob verification/publication, shared-XDG non-interaction, and zero constructor-project mutation.
- [x] 5.3 Add orchestration tests proving discovery-time authoritative-marker reconciliation and two-generation recovery run before materialization, snapshot work, Docker execution, or superseded cleanup and block all such side effects on reconciliation failure. Cover restart with all current-generation markers already absent and require marker-directory fsync before progress.
- [x] 5.4 Add result-mapping tests proving authoritative-marker reconciliation and pre/post-build cleanup failures preserve existing build-domain diagnostics, causes, and interruption behavior while mapping ordinary failure to `ExitKind.OPERATIONAL` and process exit code `4`.
- [x] 5.5 Add post-commit failure tests proving authoritative-marker unlink/fsync failure does not roll back a successful image or newest generation, preserves any existing predecessor, blocks superseded cleanup/build work, and returns operational/4; add snapshot-cleanup-failure coverage proving newly materialized blobs retain uncommitted markers and remain subject to the fixed 30-day TTL when no later generation references them.

### GREEN

- [x] 5.6 Migrate the checkout build lock to the shared `FAIL_FAST` capability without changing namespace or diagnostics; verify task 5.1 passes.
- [x] 5.7 Migrate build control reads/replacements/unlinks through L2 adapters that preserve existing build exception types/messages, raw causes, chaining, and interruption passthrough; verify task 5.4 passes.
- [x] 5.8 Replace mutable `committed-build.json` with generation publication, connect durable authoritative-marker reconciliation, then connect normal superseded cleanup; verify a successful build returns to one stable generation with no marker for any committed blob.
- [x] 5.9 Connect authoritative-marker reconciliation and generation recovery before superseded cleanup and every build side effect; map incomplete marker reconciliation or cleanup to `ExitKind.OPERATIONAL` and process exit code `4`; verify tasks 5.3–5.5 pass.
- [x] 5.10 Remove superseded local build lock and exact duplicate control-file helpers while retaining specialized blob, marker, TTL, and snapshot code; verify task 5.2 passes.

### INTROSPECT

- [x] 5.11 Review build integration for cleanup before commit, image rollback claims, hidden legacy adoption, widened deletion authority, changed dry-run effects, TTL drift, snapshot ordering, blob-publisher migration, L2 exception leakage, interruption conversion, and release masking; resolve every finding and record the review.

### VALIDATE

- [x] 5.12 Run build transaction, persistence, materialization, orchestration, CLI, and acceptance suites for first build, changed build, recovery, partial deletion, post-commit failure, corrupt generation state, ignored legacy manifest names, and excess generations; record expected image, manifest, marker/TTL, cache, diagnostic, and exit-code outcomes.

## Phase 6. Project Metadata and Effective Build Projection

**Depends on:** Phase 1
**Deliverables:** project identity metadata on durable no-clobber; effective build projection on durable replacement; preserved domain validation/errors; unchanged user-directed effective-inventory output.

### RED

- [x] 6.1 Add project-metadata tests for exact bytes, `0600` mode, three-attempt temporary allocation, typed final collision, concurrent-winner verification, unsafe-entry rejection, file/directory durability, and existing `ProjectStateError` message/cause behavior.
- [x] 6.2 Add effective-build-projection tests for descriptor-relative containment, safe destination replacement, TOML validation, `0600` mode, file/directory durability, temporary cleanup, existing `EffectiveInventoryOutputError` mappings, observable raw `OSError`/chaining, and interruption passthrough.
- [x] 6.3 Add boundary tests proving user-directed `write_effective_inventory` retains its current atomic-output and failure behavior and is not migrated to L2 durability.

### GREEN

- [x] 6.4 Migrate project identity metadata to durable no-clobber and map only final destination collision to concurrent-winner verification; verify task 6.1 passes.
- [x] 6.5 Migrate only the effective build projection to durable replacement through a rendering-domain adapter; verify task 6.2 passes.
- [x] 6.6 Preserve `write_effective_inventory` unchanged; verify task 6.3 passes and no shared durable API is imported by that path.

### INTROSPECT

- [x] 6.7 Review both migrations for temp/final collision conflation, pathname reconstruction, domain-error drift, raw-cause loss, interruption conversion, false durability, cleanup masking, and accidental user-output migration; resolve every finding and record the review.

### VALIDATE

- [x] 6.8 Run project-state, project-root, effective-build-projection, rendering, path-security, and failure-injection suites; record durable/no-clobber/replace parity and unchanged user-output behavior.

## Phase 7. Runtime Lock and Projection Migration

**Depends on:** Phase 1, Phase 2
**Deliverables:** runtime-artifact shared `BLOCK` lock; existing `Filesystem` compatibility/domain facade over L0; atomic no-clobber runtime projection; specialized content-addressed blob publisher retained.

### RED

- [x] 7.1 Add runtime-lock tests for digest scope, same-identity blocking/recheck, different-identity concurrency, valid-hit fast path, unsafe entries, and release/error parity.
- [x] 7.2 Add runtime-projection tests for complete bytes, `0444` mode before visibility, typed collision mapped to the existing `EffectiveConfigError` text, raw non-collision failures, symlink/path rejection, interruption passthrough, lifecycle cleanup, and no parent-directory durability claim.
- [x] 7.3 Add facade tests proving the existing runtime `Filesystem` retains path generation, runtime-root validation, lifecycle ownership, and injection seams while secure publication uses descriptor-relative L0 without path reconstruction.
- [x] 7.4 Add specialization tests proving runtime artifact SRI validation, content-addressed publication, collision/revalidation, quarantine, and cleanup remain outside L2 regular-file authority.

### GREEN

- [x] 7.5 Replace the runtime artifact lock with the shared `BLOCK` adapter while preserving namespace and diagnostics; verify task 7.1 passes.
- [x] 7.6 Make the runtime `Filesystem` a compatibility/domain facade over production `PosixFileOps`; verify task 7.3 passes without merging other filesystem interfaces.
- [x] 7.7 Migrate runtime projection publication to L2 atomic no-clobber while retaining serialization, identity, lifecycle, errors, and cleanup in its adapter; verify task 7.2 passes.
- [x] 7.8 Retain the runtime content-addressed blob publisher as an L3 protocol while reusing the highest compatible L2/L1/L0 mechanics for individual operations; justify every descent below L2 and verify task 7.4 passes without moving digest, collision, quarantine, or lifecycle authority below L3.

### INTROSPECT

- [x] 7.9 Review runtime migration for widened locking, changed waiting, weakened SRI, blob-authority leakage, false projection durability, build-generation coupling, interruption conversion, cleanup masking, path reconstruction, and forced filesystem-interface merging; resolve every finding and record the review.

### VALIDATE

- [x] 7.10 Run runtime materializer, projection, launcher, cache-security, corrupt-cache, lifecycle, and multiprocessing suites; record lock parity, projection atomicity, facade compatibility, and specialized blob behavior.

## Phase 8. npm-Environment Lock Migration

**Depends on:** Phase 1, Phase 2
**Deliverables:** one shared npm input-identity `BLOCK` capability; compatible leaf-mechanic reuse; unchanged immutable-tree, advisory-index, quarantine, cleanup, evidence, and cancellation protocols.

### RED

- [x] 8.1 Add npm lock tests for input-identity scope, same-identity blocking across lookup/assembly/publication, different-identity concurrency, post-lock lookup, and release parity.
- [x] 8.2 Add lock-entry tests for symlink, non-regular, foreign-owned, hard-linked, wrong-mode, bootstrap-race, and prepare/reopen replacement cases without target or ancestor mutation.
- [x] 8.3 Add specialization tests for immutable-output collision, recursive fsync/sealing, evidence authority, advisory-index best-effort failure, corrupt-output quarantine, cancellation, workspace cleanup, and primary-error preservation.
- [x] 8.4 Add leaf-mechanic tests proving npm selects the highest compatible L2/L1/L0 contract for each reused operation, justifies every descent below L2, and preserves npm-specific modes, paths, errors, and tree commit boundaries without making a shared layer a generic tree publisher.

### GREEN

- [x] 8.5 Migrate npm storage lock preparation and publication coordination to one shared `BLOCK` capability; verify tasks 8.1–8.2 pass.
- [x] 8.6 Reuse compatible L2 leaf contracts for npm manifest/evidence mechanics where their complete semantics fit, otherwise descend to L1/L0 with an explicit mismatch justification; verify task 8.4 passes without moving schema, tree-commit, or evidence authority below npm L3.
- [x] 8.7 Remove only proven duplicate npm lock/leaf mechanics while retaining tree rename, recursive fsync/sealing, advisory index, quarantine, recursive cleanup, evidence, and cancellation code; verify task 8.3 passes.

### INTROSPECT

- [x] 8.8 Review npm migration for split namespaces, cross-identity serialization, evidence leakage, changed waiting, generic-tree abstraction, advisory-index durability promotion, quarantine authority, cancellation masking, build-generation coupling, exception drift, and incompatible helper reuse; resolve every finding and record the review.

### VALIDATE

- [x] 8.9 Run npm preflight, execution, publication, storage, evidence, validation, concurrency, cancellation, and smoke suites; record unchanged tree/index/quarantine protocols and shared-lock behavior.

## Phase 9. L0–L3 Boundary and Consumer Inventory

**Depends on:** Phase 5, Phase 6, Phase 7, Phase 8
**Deliverables:** checked production filesystem-I/O matrix; required initial direct-L2 adoption set; documented L3 specialized list and highest-compatible-layer choices; enforced architecture boundaries; coherent downstream-change dependencies.

### RED

- [ ] 9.1 Add architecture/introspection tests rejecting generic `atomic_write`, durability booleans, a project-wide path VFS, pathname reconstruction from capabilities, shared envelope APIs, generic tree/content-addressed authority, direct L2 exception leakage, and domain path/deletion interpretation below L3.
- [ ] 9.2 Add consumer-boundary tests proving the required initial direct-L2 adoption set is build control/generations, project metadata, effective build projection, and runtime projection. Prove this set is a migration obligation rather than a permission boundary: specialized L3 protocols may compose compatible L2 leaf contracts while retaining domain authority, and future metadata records consume primitives only from their owning change.
- [ ] 9.3 Add specialization-boundary tests proving blobs, npm trees, snapshots/confinement, quarantine/recursive cleanup, advisory indexes, and user/evidence outputs retain L3 identity, commit, collision, recovery, error, retention, and lifecycle authority.
- [ ] 9.4 Add downstream-plan checks proving Pi extensions own sync-lock-journal/settings-sidecar schemas and recovery while metadata owns its cache-record schema, with no generic envelope or shared recovery contract.

### GREEN

- [ ] 9.5 Classify every production filesystem writer in the L0–L3 matrix by its owning layer and every reused operation by the highest compatible shared layer; verify no writer is unclassified and every descent from L2 to L1/L0 has a semantic mismatch justification.
- [ ] 9.6 Remove or document every remaining direct production `flock` and exact duplicate required-adoption regular-file helper; verify specialized direct syscalls remain only where no higher shared contract fits the domain protocol.
- [ ] 9.7 Reconcile downstream Pi-extension and metadata planning artifacts with the implemented lock, regular-file, codec, and domain-schema boundaries; verify task 9.4 passes without implementing those changes.

### INTROSPECT

- [ ] 9.8 Review the complete dependency direction for cycles, unnecessary descent below a compatible higher layer, domain-authority leakage into shared layers, generic APIs, false durability, exception-boundary drift, undocumented writers, abstractions with one consumer and no security benefit, and accidental specialized-protocol migration; resolve every finding and record the final matrix outside specification files.

### VALIDATE

- [ ] 9.9 Run architecture, introspection, projection, cache, npm, snapshot, confinement, evidence, and user-output parity suites; record the required direct-L2 adoption set, specialized L3 list, highest-compatible-layer decisions, justified direct syscalls, and absence of generic envelope/VFS/transaction authority.

## Phase 9A. Primary-Preserving Cleanup Unification

**Depends on:** Phase 9
**Deliverables:** one internal cleanup-failure accumulator with mandatory keyword-only ordinary policy and one `complete()` terminal operation; deterministic interruption/unexpected/primary/ordinary precedence; behavior-preserving shared and consumer migration; separately tested hardening of cleanup failures currently lost or masking; explicit exclusions and downstream metadata obligation.

### RED

- [ ] 9A.1 Add accumulator truth-table tests for no failure; an original `Exception` primary; an original non-`Exception` `BaseException` primary; one and multiple ordinary failures with and without a primary; one and multiple unexpected defects; one and multiple cleanup interruptions; and all mixed-precedence combinations. Prove an original process-control primary remains authoritative over cleanup defects and later interruptions, while deterministic secondary ordering, displaced-`Exception`-primary preservation, and exactly-once continuation hold through every remaining independent action.
- [ ] 9A.2 Add API-contract tests requiring `run(action, *, ordinary=...)`, rejecting positional or invalid ordinary policies, proving action return values grant no authority, and rejecting `run()` after completion or a second `complete()` without duplicating diagnostics.
- [ ] 9A.3 Add attachment tests proving exception objects are retained without duplicate identity when supported and bounded `BaseException.add_note()` fallback keeps secondary diagnostics observable when object attachment is unavailable.
- [ ] 9A.4 Audit capability adoption/path walks, lock unlock/close, L2 validated read/publication/unlink/discard, build lock/storage release, project-state/rendering/runtime projection adapters, runtime-artifact locking, and npm identity-lock release before asserting parity. Classify each site as behavior-preserving or intentional hardening and add tests for its current and required precedence, action ordering, continuation, exactly-once ownership, exception identity, raw cause, stage, and domain mapping. At minimum, add regression tests proving (a) `locking._release_descriptor` no longer lets cleanup failure displace an original non-`Exception` primary and (b) `regular._discard` keeps an original interruption authoritative and still attempts owned temporary unlink exactly once after close raises an unexpected exception or interruption. Require every cleanup failure to remain observable as secondary diagnostics and record any additional mismatch as explicit hardening rather than parity.
- [ ] 9A.5 Add fault-injection tests for specialized build-blob publication/state opening/verification, snapshot construction and invalid-hard-link cleanup, failed build materialization publication, and npm owned staging/tree cleanup. At every write/validate/unlink/rmtree/close boundary, prove `FileNotFoundError` is ignored only by a domain action whose contract declares idempotent absence, while permission, I/O, partial-cleanup, unexpected defects, and interruption remain observable without skipping later independent cleanup.

### GREEN

- [ ] 9A.6 Implement internal `docker.transactions.cleanup.CleanupFailures` with state for original primary, ordinary failures, unexpected defects, and interruptions. Classify an original non-`Exception` `BaseException` as the first authoritative interruption; make `run()` attempt each action exactly once without raising action failures; and make sole terminal method `complete()` apply `interruption > unexpected > original Exception primary > ordinary`, preserve displaced failures, and return only an unopposed first ordinary failure for caller-owned mapping.
- [ ] 9A.7 Strengthen `attach_secondary` with identity de-duplication and bounded `BaseException.add_note()` fallback when exception-object attachment is unavailable; retain authoritative exception identity and existing `TransactionError.secondary` behavior.
- [ ] 9A.8 Migrate shared capabilities, locking, and regular-file contracts, then the matching build, project-state, rendering, runtime, and npm adapters according to the task 9A.4 classification. Preserve existing cleanup precedence, ordering, continuation, exactly-once ownership, raw causes, stages, exception identity, and domain mappings only at sites proven conforming. Intentionally harden every audited mismatch, including `_release_descriptor` authoritative-original-interruption preservation and `_discard` authoritative-original-interruption plus continuation to owned temporary unlink after unexpected close failure or interruption; add no unrecorded behavior change. Do not top-level-export the accumulator.
- [ ] 9A.9 Harden the task 9A.5 specialized L3 paths through domain-owned actions plus the shared accumulator. Replace broad suppression only where tests prove non-absence cleanup failures were lost or masked; preserve idempotent `FileNotFoundError` handling for owned temporary entries, invalid hard-link destinations, snapshots, failed materializations, and npm staging trees.
- [ ] 9A.10 Update `revalidate-update-metadata` planning tasks to require the shared cleanup accumulator for metadata-owned descriptor cleanup while retaining metadata-owned schema, path, removal, idempotent-absence, and error-mapping authority; do not modify metadata production code in this phase.

### INTROSPECT

- [ ] 9A.11 Review every migrated call site for independent-action validity, deterministic ordering, accidental retry, duplicate attachment, swallowed programmer defects, interruption conversion, broad `FileNotFoundError` suppression, domain/path/deletion leakage, and changed exception identity. Confirm npm process lifecycle/streaming, activity-monitor policy, user/presentation outputs, and existing metadata cache storage remain excluded; resolve every finding and record the review outside specification files.

### VALIDATE

- [ ] 9A.12 Run the complete accumulator fault matrix plus L0-L3 lifecycle, locking, build cache/cleanup/snapshot/materialization/orchestration, project-state/rendering/projection, runtime artifact, npm publication/tree, architecture, type, lint, and diff checks. Record migrated and excluded sites, unchanged domain contracts, corrected masking/loss cases, and exact idempotent-absence decisions outside specification files.

## Phase 10. Rollout and Complete Validation

**Depends on:** Phase 9A
**Deliverables:** explicit one-time development-cache cutover; no runtime inspection or migration of the legacy manifest; complete automated and acceptance evidence; archive-ready change.

### RED

- [ ] 10.1 Add documentation/acceptance tests requiring an exact project-resolved instruction for deleting the old development build cache and rejecting broad or approximate `rm -rf` guidance.
- [ ] 10.2 Add final legacy-name tests proving `committed-build.json` is not inspected and does not affect generation-state classification, and that no runtime migration, adoption, rejection, or deletion path exists.
- [ ] 10.3 Add final integration assertions for operational exit code `4`, successful-image/no-rollback messaging, aggregate cleanup diagnostics, and recovery-before-build ordering.

### GREEN

- [ ] 10.4 Publish the one-time developer cache-removal instruction at the approved project documentation location; verify task 10.1 passes.
- [ ] 10.5 Complete any final integration wiring required by tasks 10.2–10.3 without broadening the approved L0–L3 contracts; verify both tasks pass.

### INTROSPECT

- [ ] 10.6 Review the complete change for unfinished compatibility paths, undocumented behavior changes, stale journal/envelope assumptions, task/spec/design divergence, unchecked cleanup authority, and unrecorded verification evidence; resolve every finding before final validation.

### VALIDATE

- [ ] 10.7 Run formatting, type checks, lint, complete unit/integration/acceptance suites, strict OpenSpec validation, and `git diff --check`; record every command and result outside specification artifacts.
- [ ] 10.8 Execute the documented old-cache cutover in the development environment, rerun the representative first-build/change-build/recovery flow, and record that the change is ready for archive.

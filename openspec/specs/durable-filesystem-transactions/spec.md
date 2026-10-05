# durable-filesystem-transactions Specification

## Purpose

Define reusable security and recovery guarantees for Constructor processes that coordinate and durably mutate owner-private filesystem state.

## Requirements

### Requirement: Coordinate owner-private state through validated file locks
A side-effecting Constructor operation that adopts the shared transaction substrate SHALL acquire an advisory lock at its domain-selected scope before entering its critical section. Lock preparation and acquisition SHALL reject symlinks, non-regular files, foreign-owned files, and multiply linked files without mutating their targets. A missing lock entry SHALL be created atomically and exclusively, and a lock entry created by the current acquisition SHALL be repaired to exactly `0600` only after the exclusive acquisition and full validation, even when a restrictive process umask stripped its owner access bits. A pre-existing safe owner-owned single-link regular lock that grants its owner read or write access SHALL be exclusively acquired before its mode is repaired to exactly `0600`. A pre-existing lock whose mode grants its owner neither read nor write access SHALL fail closed without changing its mode because an unprivileged process cannot lock it before repair, and the created-entry repair SHALL NOT apply to it. Each consumer SHALL explicitly select blocking or nonblocking contention behavior, and only a live lock capability for the same namespace SHALL authorize protected mutation.

#### Scenario: Rejecting an unsafe lock entry
- **WHEN** a lock path resolves to a symlink, non-regular entry, foreign-owned file, or multiply linked file
- **THEN** acquisition SHALL fail without chmodding, replacing, or otherwise mutating that entry or its target

#### Scenario: Handling a safe lock with the wrong mode
- **WHEN** a pre-existing safe owner-owned single-link regular lock grants its owner read or write access but its mode is not `0600`
- **THEN** acquisition SHALL obtain the exclusive lock before repairing its mode to exactly `0600`
- **WHEN** a missing lock entry is created by the current acquisition and a restrictive umask strips its owner access bits
- **THEN** acquisition SHALL obtain the exclusive lock and fully validate the created inode before repairing its mode to exactly `0600`
- **WHEN** a pre-existing safe lock grants its owner neither read nor write access
- **THEN** acquisition SHALL fail without changing its mode
- **AND** the created-entry repair SHALL NOT apply to a pre-existing entry

#### Scenario: Preserving consumer contention policy
- **WHEN** two operations contend for the same lock namespace
- **THEN** the shared mechanism SHALL apply the consuming domain's declared blocking or nonblocking policy
- **AND** SHALL permit at most one holder to enter the protected critical section

#### Scenario: Rejecting a stale or unrelated capability
- **WHEN** protected mutation receives a released lock capability or one issued for another namespace
- **THEN** it SHALL fail before changing protected state

### Requirement: Publish regular files through explicit atomic and durable contracts
Shared regular-file publication SHALL expose distinct contracts rather than one configurable generic write. Atomic no-clobber publication SHALL make complete bytes visible at a new destination without replacing an existing entry and SHALL establish the final mode before visibility, but SHALL NOT claim persistence after power loss. Durable no-clobber publication SHALL additionally flush the complete file before publication and flush the parent directory before reporting success. Durable replacement SHALL flush complete private sibling state, replace only an absent or validated owned regular-file destination, and flush the parent directory before reporting success. Shared durable removal SHALL unlink one validated owned entry and flush the parent directory before reporting success. Validated reads SHALL reject unsafe type, ownership, link count, or mode without following a leaf symlink.

#### Scenario: Publishing an atomic no-clobber file
- **WHEN** atomic no-clobber publication reports success
- **THEN** readers SHALL observe the complete file with its final mode rather than partial bytes or an intermediate mode
- **AND** an entry that already existed at the destination SHALL not have been replaced
- **AND** the operation SHALL NOT claim that the destination directory entry is durable after power loss

#### Scenario: Publishing a durable no-clobber file
- **WHEN** durable no-clobber publication reports success
- **THEN** readers SHALL observe the complete file without replacement of an existing destination
- **AND** the file contents and containing-directory entry SHALL have crossed their required durability boundaries

#### Scenario: Completing durable replacement
- **WHEN** durable replacement reports success
- **THEN** readers SHALL observe the complete replacement rather than partial bytes
- **AND** the replacement and containing-directory entry SHALL have crossed their required durability boundaries

#### Scenario: Failing during publication
- **WHEN** writing, mode establishment, flushing, replacement, no-clobber publication, or directory synchronization required by the selected contract fails
- **THEN** the operation SHALL report failure
- **AND** SHALL remove any still-owned temporary entry without modifying an unrelated path

#### Scenario: Reading unsafe control state
- **WHEN** a control-state path is a symlink, non-regular entry, foreign-owned entry, multiply linked entry, or has a mode forbidden by its consumer
- **THEN** the read SHALL fail without following or repairing it

#### Scenario: Removing owned state durably
- **WHEN** durable removal reports success for an owned entry
- **THEN** the entry SHALL be absent
- **AND** its containing directory SHALL have crossed the required durability boundary

#### Scenario: Applying the explicit regular-file contracts
- **WHEN** shared publication is adopted by existing consumers
- **THEN** runtime projection publication SHALL use atomic no-clobber semantics
- **AND** project identity metadata and build generation manifests SHALL use durable no-clobber semantics
- **AND** the effective build projection SHALL use durable replacement semantics

### Requirement: Preserve authoritative failures across resource cleanup
The shared substrate SHALL provide one internal cleanup-failure accumulator for independent resource-release actions. Each action SHALL be attempted exactly once in caller-selected order even when an earlier cleanup action raises. Every caller SHALL explicitly declare, through a mandatory keyword-only policy, which `Exception` types are ordinary cleanup failures. The accumulator SHALL NOT derive paths, remove entries or trees, retry actions, define rollback, map domain errors, or decide whether absence is idempotent.

The accumulator SHALL retain the original primary failure, ordinary cleanup failures, unexpected cleanup defects, and process-control interruptions as separate classes. An original primary that is a non-`Exception` `BaseException` SHALL be classified as the first process-control interruption and SHALL remain authoritative over every cleanup defect or later interruption. Its sole terminal operation SHALL otherwise apply precedence in this order: process-control interruption, unexpected cleanup defect, original `Exception` primary, then ordinary cleanup failure. Any higher-precedence failure that displaces an existing `Exception` primary SHALL retain that primary and all other failures as bounded secondary diagnostics. When exception-object attachment is unavailable, secondary diagnostics SHALL remain observable through bounded exception notes without changing the authoritative exception's identity.

#### Scenario: Preserving an existing primary failure
- **WHEN** an operation has an existing primary failure
- **AND** one or more independent cleanup actions raise declared ordinary failures
- **THEN** every cleanup action SHALL still be attempted exactly once
- **AND** the original primary object SHALL remain authoritative
- **AND** every ordinary cleanup failure SHALL remain observable as secondary diagnostic context

#### Scenario: Reporting cleanup failure without an existing primary
- **WHEN** no primary failure exists
- **AND** one or more cleanup actions raise declared ordinary failures
- **THEN** every cleanup action SHALL still be attempted exactly once
- **AND** completion SHALL return the first ordinary failure for caller-owned mapping or raising
- **AND** later ordinary failures SHALL be attached to it as secondary diagnostics

#### Scenario: Preserving an original process-control interruption
- **WHEN** the original primary is `KeyboardInterrupt`, cancellation, or another non-`Exception` `BaseException`
- **AND** cleanup actions raise unexpected defects, later interruptions, or ordinary failures
- **THEN** every independent cleanup action SHALL still be attempted exactly once
- **AND** completion SHALL raise the original primary object unchanged
- **AND** every cleanup failure SHALL remain observable as secondary diagnostics

#### Scenario: Propagating interruption first raised during cleanup
- **WHEN** the original primary is absent or is an `Exception`
- **AND** any cleanup action raises `KeyboardInterrupt`, cancellation, or another non-`Exception` `BaseException`
- **THEN** every remaining independent cleanup action SHALL still be attempted exactly once
- **AND** completion SHALL raise the first cleanup interruption object unchanged
- **AND** any displaced original `Exception` primary, unexpected defects, later interruptions, and ordinary cleanup failures SHALL remain observable as secondary diagnostics

#### Scenario: Surfacing an unexpected cleanup defect
- **WHEN** a cleanup action raises an `Exception` not declared ordinary for that action
- **THEN** every remaining independent cleanup action SHALL still be attempted exactly once
- **AND** completion SHALL raise the first unexpected exception object unchanged unless a process-control interruption has higher precedence
- **AND** any displaced original primary and other cleanup failures SHALL remain observable as secondary diagnostics

#### Scenario: Carrying secondary diagnostics across an exception remap
- **WHEN** a domain adapter replaces a transaction or capability wrapper with its raw operational cause as the authoritative exception
- **THEN** when the raw cause can retain exception objects, every secondary exception object already retained by the wrapper SHALL also remain observable on it in source order without duplicate identity, including across repeated carries
- **AND** when the raw cause cannot retain exception objects, its secondary diagnostics SHALL instead remain observable as bounded textual notes without any guarantee of exception-object retention or identity de-duplication across repeated carries
- **AND** the wrapper SHALL remain unchanged rather than having its diagnostics consumed
- **AND** `carry_secondary_diagnostics` itself SHALL NOT change the raw cause's identity, `__cause__`, `__context__`, or unrelated notes
- **AND** subsequent propagation MAY update normal Python exception chaining exactly as it did before the remap migration
- **AND** the adapter SHALL use the shared diagnostic-carry contract rather than reading shared storage or constructing an arbitrary secondary list

#### Scenario: Treating owned absence as a domain decision
- **WHEN** a domain-owned cleanup action unlinks an entry or recursively removes a tree
- **THEN** that domain action MAY treat `FileNotFoundError` as successful idempotent absence when its complete contract permits
- **AND** the shared accumulator SHALL NOT globally suppress `FileNotFoundError` or any other filesystem error
- **AND** permission, I/O, partial-cleanup, and unexpected-state failures SHALL remain subject to the declared cleanup policy

### Requirement: Recover immutable build generations and deferred cleanup
Committed build state SHALL be published as immutable, no-clobber, fixed-width monotonically numbered manifest generations under the checkout-wide exclusive lock. The newest valid generation SHALL be authoritative. At most one immediately previous generation MAY coexist as durable evidence that superseded-blob cleanup remains incomplete.

#### Scenario: Publishing a new authoritative generation
- **WHEN** a build and its transaction-snapshot cleanup complete successfully
- **THEN** the constructor SHALL durably publish the next manifest generation without modifying or replacing an existing generation
- **AND** that generation SHALL become authoritative only after its containing directory is durably synchronized

#### Scenario: Recovering a visible generation after publication synchronization failure
- **WHEN** discovery finds a complete generation that may have become visible before a required parent-directory synchronization failed
- **THEN** the constructor SHALL successfully synchronize the generation directory under the checkout-wide lock before accepting the newest generation as authoritative
- **AND** failure SHALL preserve every generation and blob and prevent cleanup and build work

#### Scenario: Clearing markers for committed blobs
- **WHEN** a generation becomes authoritative or discovery finds an authoritative generation
- **THEN** the constructor SHALL remove uncommitted markers for every blob in that generation as one batch after generation authority is durable
- **AND** SHALL synchronize the shared marker directory once, including when recovery finds all applicable markers already absent
- **AND** failure SHALL leave the authoritative generation committed, preserve any existing predecessor, and prevent superseded cleanup and build work

#### Scenario: Completing superseded-blob cleanup
- **WHEN** current and previous manifest generations coexist
- **AND** authoritative-generation marker reconciliation has completed durably
- **THEN** the constructor SHALL attempt every canonical blob identity present in the previous generation and absent from the current generation
- **AND** SHALL batch candidate removals by containing directory rather than synchronize once per candidate
- **AND** SHALL synchronize each affected existing blob directory once after its complete removal batch and the shared marker directory once after all marker removals
- **AND** each candidate SHALL be complete only after its blob and marker are absent and the applicable post-batch synchronizations have succeeded
- **AND** SHALL durably remove the previous manifest only after every candidate is durably absent

#### Scenario: Reporting aggregate cleanup failure
- **WHEN** one or more superseded blobs cannot be removed
- **THEN** the constructor SHALL still attempt every remaining candidate
- **AND** SHALL preserve the previous manifest for recovery
- **AND** SHALL report every candidate failure as an operational failure with process exit code 4

#### Scenario: Recovering before another build
- **WHEN** a later invocation finds current and previous manifest generations
- **THEN** it SHALL repeat idempotent cleanup under the checkout-wide lock before starting build work
- **AND** it SHALL synchronize each relevant existing blob directory and the shared marker directory once per recovery batch even when all assigned candidate entries are already absent
- **AND** already absent candidates SHALL count as successfully cleaned only after the applicable batch synchronizations establish their durable absence
- **AND** it SHALL not start the build unless cleanup and durable previous-manifest removal complete successfully

#### Scenario: Failing closed on ambiguous generation state
- **WHEN** build state contains a malformed generation, more than two generations, or a noncanonical generation name
- **THEN** the constructor SHALL fail without deleting blobs or generation manifests and without starting a build

#### Scenario: Ignoring the legacy mutable manifest name
- **WHEN** `committed-build.json` exists after the immutable-generation cutover
- **THEN** generation-state discovery SHALL NOT inspect, adopt, reject, or delete it
- **AND** SHALL classify authority solely from canonical immutable generation entries

#### Scenario: Failing cleanup after a successful image build
- **WHEN** a new generation is committed after a successful image build but its post-commit cleanup fails
- **THEN** the image and newest generation SHALL remain committed without rollback
- **AND** the constructor command SHALL return an operational failure with process exit code 4

### Requirement: Preserve domain ownership and concurrency boundaries
Adoption of the shared substrate SHALL NOT broaden a consumer's lock scope, cache ownership, deletion authority, or filesystem traversal permissions. Domain adapters SHALL validate and derive every authoritative identity and cleanup target before invoking a shared primitive.

#### Scenario: Migrating identity-scoped caches
- **WHEN** runtime-artifact or npm-environment publication adopts shared locking
- **THEN** different identities SHALL remain independently concurrent
- **AND** contention for the same identity SHALL retain its existing blocking policy

#### Scenario: Migrating checkout build transactions
- **WHEN** checkout build state adopts immutable generations and shared primitives
- **THEN** its checkout-wide lock SHALL remain nonblocking
- **AND** build-specific manifest validation, marker retention, snapshot recovery, and blob-deletion authority SHALL remain confined to the checkout build domain

#### Scenario: Rejecting an unvalidated cleanup target
- **WHEN** a manifest or domain adapter presents a malformed, noncanonical, escaping, or otherwise unowned cleanup identity
- **THEN** recovery SHALL fail without deleting any target

#### Scenario: Preserving specialized publication protocols
- **WHEN** a consumer publishes a content-addressed build/runtime blob, an immutable npm output tree, a snapshot or confined build context, quarantine state, a recursively cleaned tree, an advisory index, or a user/evidence output
- **THEN** it SHALL reuse the highest shared layer whose complete contract matches each operation and SHALL descend to a lower layer only when no higher contract is semantically sufficient
- **AND** a specialized L3 protocol MAY compose compatible L2 leaf contracts without making L2 the owner of that consumer's identity, commit, collision, recovery, retention, error, or lifecycle semantics
- **AND** any L1/L0 reuse SHALL likewise leave the complete domain protocol and its failure policy under the consumer's authority

### Requirement: Expose bounded extension seams
The shared substrate SHALL permit later domain-owned adapters to compose lock, regular-file, and canonical JSON primitives without weakening their guarantees. The canonical JSON codec SHALL provide deterministic bytes and generic JSON decoding only; it SHALL NOT interpret schema or protocol versions, closed fields, identities, paths, commit state, recovery state, or deletion authority. The base mechanism SHALL NOT claim a generic envelope schema, generic single-authority recovery, atomic multi-file replacement, or automatic reconciliation of externally mutable user state.

#### Scenario: Requesting stronger transaction semantics
- **WHEN** a consumer requires mutable-state recovery, all-or-nothing replacement of multiple authoritative files, or reconciliation with concurrent user edits
- **THEN** it SHALL supply an explicit domain-owned adapter, complete closed versioned schema, and recovery contract
- **AND** SHALL NOT represent the base durable-I/O, canonical JSON, or immutable-generation mechanism as providing those guarantees

#### Scenario: Defining domain envelopes
- **WHEN** build generations, multi-target synchronization, settings reconciliation, or metadata caching persists a versioned record
- **THEN** the owning domain SHALL define and validate the record's complete schema and version policy
- **AND** the shared codec SHALL treat that record only as a generic JSON value and deterministic byte sequence

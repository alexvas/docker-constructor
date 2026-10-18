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

### Requirement: Converge descriptor-owning cleanup on explicit lifecycle guarantees
Descriptor-owning consumers SHALL use the shared lightweight lifecycle, mark ownership terminal before release and attempt each close at most once. Cleanup SHALL preserve active failures over ordinary close failures and attempt every independent release through one accumulator. Direct closes SHALL be restricted to primitives, terminal shared-owner internals or verified handoffs.

#### Scenario: Enforce shared ownership and direct-close categories
- **WHEN** a consumer adopts a descriptor
- **THEN** it SHALL represent that descriptor through the shared lightweight lifecycle and mark ownership terminal before its sole release attempt
- **AND** one aggregate cleanup accumulator SHALL attempt all independent releases even if an earlier release fails
- **AND** ordinary close failures SHALL remain secondary diagnostic context to an active operation failure
- **WHEN** production code invokes a direct descriptor close
- **THEN** it SHALL be a low-level POSIX adapter primitive, an internal release of `OwnedDescriptor` or an equivalent shared owner after ownership is terminal, or a handoff with a terminal parent and a child already tracked for failure cleanup

#### Scenario: Release fails while an operation failure is active
- **WHEN** an owned descriptor must be released while an operation or domain failure is already active
- **AND** release reports an ordinary close failure
- **THEN** the active failure SHALL remain authoritative
- **AND** the close failure SHALL remain observable as secondary diagnostic context
- **AND** the descriptor SHALL NOT receive another close attempt

#### Scenario: One of several independent releases fails
- **WHEN** a lifecycle owns several independent descriptors
- **AND** releasing one descriptor fails
- **THEN** every other descriptor SHALL still receive exactly one release attempt
- **AND** final failure selection SHALL follow the shared cleanup precedence

#### Scenario: Transferring parent authority after opening a child
- **WHEN** descriptor-relative traversal has opened a child and releasing the retained parent fails
- **THEN** the parent release SHALL NOT be retried
- **AND** the child SHALL receive exactly one release attempt
- **AND** the child SHALL neither leak nor be returned

#### Scenario: Releasing one descriptor after successful work
- **WHEN** a consumer successfully completes work and owns one descriptor requiring release
- **THEN** it SHALL release through the shared owned-descriptor lifecycle
- **AND** a close failure SHALL be the sole reported failure
- **AND** the release attempt SHALL remain terminal

#### Scenario: Shared owner performs its internal release
- **WHEN** `OwnedDescriptor.close()` or an equivalent shared owner releases its adopted descriptor
- **THEN** it SHALL make ownership terminal before invoking its injected close operation
- **AND** it MAY invoke that close operation directly
- **AND** a repeated owner release SHALL NOT invoke the operation again

#### Scenario: File-object adoption does not hide descriptor ownership
- **WHEN** a consumer obtains a raw descriptor from `tempfile.mkstemp()` or wraps a raw descriptor with `os.fdopen()`
- **THEN** the descriptor SHALL transfer immediately to the shared owned-descriptor lifecycle
- **AND** a file object SHALL use `closefd=False` with that external owner as the sole descriptor-release authority
- **AND** file-object flush or close failure SHALL remain primary over an ordinary descriptor-release failure
- **AND** both release attempts SHALL remain terminal and at most once

#### Scenario: Close aliases cannot evade lifecycle classification
- **WHEN** a production descriptor close is reached through an import alias, assigned callable, renamed backend object, or injected descriptor-operation variable
- **THEN** the convergence gate SHALL resolve and classify its descriptor-close origin
- **AND** SHALL apply the same primitive, shared-owner, or handoff requirements as for a directly spelled call
- **AND** an unresolved close-like call SHALL require proof that it is not a descriptor release

#### Scenario: Retaining another correct direct close
- **WHEN** a low-level POSIX adapter delegates its close primitive
- **THEN** it MAY invoke the raw close primitive directly and SHALL preserve the raw failure contract of that boundary
- **WHEN** an ownership handoff releases a terminal parent after its child is already tracked
- **THEN** its direct close MAY remain the operation that triggers the surrounding failure cleanup
- **AND** the parent SHALL not be retried and the child SHALL not leak

### Requirement: Give fd-owning streams one synchronized release authority
The foundation SHALL provide `OwnedStream` in `docker.filesystem.streams` for already-open fd-owning file objects. Consumers SHALL retain one owner per stream for its entire lifecycle. Release SHALL use a synchronized terminal claim and shared cleanup precedence, with no second integer-fd owner. Borrowed access SHALL NOT grant release authority.

#### Scenario: Preserve the bounded foundation surface
- **WHEN** the stream ownership API is introduced
- **THEN** it SHALL expose only labeled adoption, borrowed live stream access, terminal-state inspection, explicit close, and context-manager cleanup
- **AND** it SHALL NOT acquire subprocess, reader, deadline, redaction, path, advisory-lock, publication, durability, or domain-error policy
- **AND** its underlying close SHALL remain a shared-owner internal release, not a fourth direct-close exemption
- **AND** it SHALL NOT authorize consumer `fdopen(closefd=True)` or per-cleanup owner reconstruction

#### Scenario: Concurrent or reentrant stream cleanup
- **WHEN** repeated, concurrent, or reentrant cleanup targets the same owned stream
- **THEN** the owner SHALL synchronize and mark its release claim terminal before invoking the underlying close outside the claim lock
- **AND** exactly one caller SHALL invoke the underlying close, including when it fails or is interrupted
- **AND** a losing caller SHALL NOT retry, close the raw descriptor, or treat a terminal claim as proof that the winning close has completed
- **AND** the consuming cleanup coordinator SHALL retain the winning outcome before claiming cleanup completion
- **AND** a bounded return before completion SHALL explicitly expose incomplete cleanup with retained ownership and outcome observation

#### Scenario: Adopt a stream without double ownership
- **WHEN** a factory has already created a file object that owns its descriptor
- **THEN** the consumer SHALL adopt the file object into the shared stream owner before fallible use
- **AND** SHALL NOT independently adopt or close its `fileno()` descriptor
- **AND** failure during multi-stream adoption SHALL account for every acquired resource under shared precedence and release it once when safe
- **AND** resources still used by readers SHALL retain their owners until those readers stop

#### Scenario: Reject a stream before ownership transfer
- **WHEN** labeled stream adoption rejects an invalid label or stream interface, or fails while preparing synchronization
- **THEN** ownership SHALL remain with the caller
- **AND** no stream close SHALL be attempted and the stream SHALL remain untouched
- **AND** the rejection SHALL NOT hide a close interruption or defect by attempting release

#### Scenario: Close failure or interruption is terminal
- **WHEN** an owned stream close fails ordinarily, raises an unexpected defect, or is interrupted
- **THEN** ownership SHALL remain terminal and subsequent cleanup SHALL issue no further underlying close
- **AND** independently safe owners SHALL still receive their release attempts
- **AND** a pipe still accessed by a reader SHALL retain its owner until safe release
- **AND** authoritative failure selection SHALL follow shared cleanup precedence without wrapping process-control objects

### Requirement: Acquire subprocess resources through one prepared lifecycle
A reusable process lifecycle SHALL own launch, captured output streams, internally controlled readers, and coordinated cleanup. Fallible ownership and cleanup-continuation preparation SHALL precede launch. Every acquired pipe SHALL have one prepared release authority before fallible use. Process coordination SHALL remain outside the filesystem foundation and SHALL NOT acquire domain cleanup or diagnostic policy.

#### Scenario: Preparation fails before launch
- **WHEN** validation, owner preparation, lock/event creation, or cleanup-continuation preparation/startup fails
- **THEN** no subprocess SHALL be launched and no pipe SHALL be acquired
- **AND** any already-prepared idle workers SHALL undergo cleanup
- **AND** the authoritative failure SHALL retain its identity under shared precedence

#### Scenario: Process launch fails after preparation
- **WHEN** the subprocess factory fails without returning a launched child
- **THEN** preparation SHALL NOT leave idle cleanup workers running or pipe ownership stranded
- **AND** cleanup failures SHALL participate in shared precedence

#### Scenario: Launch succeeds but setup fails
- **WHEN** a subprocess has launched and first or second stream transfer, reader configuration/startup, supervisor startup, or a launch callback fails
- **THEN** every acquired pipe SHALL remain accounted for by its designated prepared release authority
- **AND** both pipe positions SHALL be handled independently without raw consumer-close fallback, constructor retry as release authority, or self-closing rejected adoption
- **AND** safe pipe release and bounded child termination/reaping SHALL still be attempted
- **AND** incomplete cleanup SHALL retain responsibility and expose observation access even when launch returns no owner

#### Scenario: Separate process coordination from domain policy
- **WHEN** a consumer uses the reusable process lifecycle
- **THEN** the mechanism SHALL depend on lightweight filesystem ownership rather than domain packages
- **AND** filesystem ownership SHALL NOT depend on process coordination
- **AND** Docker removal, domain errors, staging, cache, publication, and redaction SHALL remain consumer responsibilities
- **AND** the supported scope SHALL be one direct child and captured stdout/stderr, not process groups/trees, stdin, PTY, or arbitrary externally supplied reader threads

### Requirement: Configure stop policy without delegating ownership
The process lifecycle SHALL accept a narrow stop policy selected and prepared at launch. All shutdown triggers SHALL use that policy through one coordinator. Local waiting/reaping, reader barriers, stream release and outcome publication SHALL remain coordinator responsibilities. The default local policy SHALL retain its supported behavior; domain commands and identities SHALL remain outside the generic mechanism.

#### Scenario: Policy preparation fails before acquisition
- **WHEN** policy validation or preparation fails
- **THEN** the main child SHALL NOT be launched and no main pipe SHALL be acquired
- **AND** any prepared idle workers SHALL undergo independent cleanup with shared precedence

#### Scenario: Every shutdown trigger selects the same policy
- **WHEN** explicit cancellation, deadline, context exit, reader failure or post-launch setup failure initiates shutdown
- **THEN** the launch-configured policy SHALL participate in the same coordination and retained outcomes
- **AND** it SHALL NOT replace shutdown, obtain raw process or pipe authority, or independently wait/reap the child
- **AND** natural completion SHALL be distinguished from cancellation so unnecessary external stop requests are not issued for normally completed helper clients

#### Scenario: Managed stop request stalls or fails
- **WHEN** a supported managed stop operation stalls, fails or exceeds its allocated stage time
- **THEN** observing that operation SHALL NOT block generic local fallback, independently safe release or bounded outcome delivery
- **AND** its locally acquired resources SHALL retain explicit owners and bounded cleanup observation
- **AND** the protocol SHALL use nonblocking initiation/observation rather than arbitrary blocking whole-shutdown callbacks

### Requirement: Bound configured stop-policy stages and local cleanup
The process lifecycle SHALL bound policy-defined stages and reserve local-cleanup time within one monotonic total deadline. A stage cap SHALL include managed-action execution and configured child waiting. The policy SHALL define its stage selection/order without prescribing Docker commands or a fixed soft/hard sequence. Default local shutdown, safe reader-stop barriers and at-most-once release SHALL remain supported.

#### Scenario: Policy selects a bounded final drain
- **WHEN** a configured policy selects draining while natural child completion is awaited
- **THEN** supported internal readers SHALL continue bounded draining until completion or the allocated expiry
- **AND** reader cancellation SHALL follow that configured drain boundary, not merely the stop request
- **AND** tails/spooling SHALL remain bounded without requiring consumer callback progress
- **AND** failed readers SHALL NOT block remaining actions or sibling cleanup

#### Scenario: Configured stage fails or expires
- **WHEN** a managed action or its configured child wait fails or reaches the stage limit
- **THEN** the coordinator SHALL follow the selected policy's next-stage or local-fallback transition
- **AND** action execution and subsequent waiting SHALL share that stage cap under the remaining total deadline
- **AND** explicit failure SHALL NOT require sleeping out the unused allowance
- **AND** a new stage, helper or concurrent shutdown SHALL NOT reset the total deadline or enlarge configured caps

#### Scenario: Local cleanup uses its retained allowance
- **WHEN** the selected policy permits local fallback or finalization
- **THEN** coordinator-owned terminate/kill/reap, managed-resource cleanup and reader finalization SHALL use the reserved remaining allowance
- **AND** incomplete local cleanup SHALL retain the same owners and observable outcomes
- **AND** policies without external stages SHALL retain the default local shutdown behavior

#### Scenario: Stage configuration cannot fit the total budget
- **WHEN** configured stage caps or the local-cleanup reserve are nonpositive/nonfinite, or their sum exceeds the supported total budget
- **THEN** policy validation SHALL fail before main acquisition
- **AND** valid waits SHALL be bounded by their configured stage cap and the remaining total deadline
- **AND** no particular number or domain-specific naming of external stages SHALL be required

#### Scenario: Managed auxiliary children retain explicit ownership
- **WHEN** a configured managed action acquires an auxiliary child
- **THEN** that child SHALL use its own prepared process lifecycle with the default local stop policy and one stream owner per pipe
- **AND** helper execution and cleanup SHALL be capped by the parent's remaining budget
- **AND** pending or failed helper cleanup SHALL remain owned and observable without a raw-close fallback

#### Scenario: Late control outcomes remain observation only
- **WHEN** an already-started control operation finishes after result delivery
- **THEN** its outcome SHALL be retained without mutating delivered results/errors or beginning user-facing callbacks/output
- **AND** observation SHALL NOT restart budgets, repeat requests or initiate a new external policy action after delivery

### Requirement: Preserve precedence throughout process cleanup
All process setup and independent cleanup failures SHALL participate in shared cleanup precedence with explicit ordinary-error policies. Join, release, and reaping failures SHALL NOT skip independent actions. Existing process-control interruptions SHALL retain identity over later defects or interruptions; a later interruption SHALL outrank an ordinary setup failure. Repeated or concurrent cleanup SHALL share one retained outcome without retries or duplicate diagnostics.

#### Scenario: Second transfer interrupts after an ordinary first failure
- **WHEN** the first stream transfer fails ordinarily and the second raises a process-control interruption
- **THEN** the second interruption SHALL be authoritative with its exact identity preserved
- **AND** the first failure SHALL be retained secondarily
- **AND** both acquired pipes SHALL remain accounted for and the child SHALL undergo bounded reaping

#### Scenario: Operation interruption precedes cleanup failures
- **WHEN** the active operation is interrupted and join, stream release, or reaping subsequently fails
- **THEN** the original interruption SHALL remain authoritative with its exact identity preserved
- **AND** all independent cleanup actions SHALL still be attempted and their failures retained once

#### Scenario: Active operation exceptions are not cleanup defects
- **WHEN** an active operation or domain Exception is supplied to shutdown
- **THEN** it SHALL be retained as the original operation primary through shared cleanup precedence, not classified as an unexpected cleanup action
- **AND** a subsequent unexpected cleanup defect SHALL outrank that ordinary operation primary while retaining its exact identity secondarily

#### Scenario: Reader join OSError is an unexpected cleanup defect
- **WHEN** a reader join raises OSError while an ordinary operation failure such as ValueError is active
- **THEN** the join SHALL retain `ordinary=()` classification and its OSError SHALL be an unexpected cleanup defect, not an ordinary release failure
- **AND** absent an interruption or an earlier unexpected defect, that exact join exception SHALL outrank the ordinary operation failure, retaining the displaced failure and other cleanup failures once
- **AND** if KeyboardInterrupt was already active, that exact interruption SHALL remain authoritative with the join failure retained secondarily
- **AND** sibling joins, independently safe releases and bounded child reaping SHALL still be attempted; the failed join SHALL NOT authorize release beneath a live reader
- **AND** repeated cleanup SHALL NOT retry pipe release or duplicate retained diagnostics

#### Scenario: Concurrent operation interruptions are merged
- **WHEN** a later caller supplies an interruption before the existing shutdown outcome is delivered
- **THEN** the first recorded interruption SHALL retain exact authoritative identity
- **AND** the later interruption SHALL be retained secondarily once without restarting policy actions or cleanup

#### Scenario: Primary arrives between outcome publication and notification
- **WHEN** deadline or policy cleanup publishes an outcome before phase notification or first delivery
- **AND** an active operation primary is merged during that interval
- **THEN** the undelivered snapshot SHALL reflect the current ledger under the coordination lock immediately before delivery
- **AND** completion state and first-interruption precedence SHALL be preserved without changing previously delivered snapshots

#### Scenario: Join is interrupted during setup cleanup
- **WHEN** joining a started reader raises an interruption or unexpected defect
- **THEN** sibling cleanup and bounded child termination/reaping SHALL still be attempted
- **AND** the reader's stream SHALL NOT be released while the reader can still access it
- **AND** failure selection SHALL include the join failure under shared precedence

#### Scenario: Cleanup is requested repeatedly or concurrently
- **WHEN** deadline, cancellation, context exit, or explicit shutdown targets the same process lifecycle
- **THEN** callers SHALL share the existing cleanup coordination, monotonic budgets, owners, and winning outcomes
- **AND** they SHALL NOT re-adopt a pipe, retry its close, restart shutdown budgets, or duplicate retained failures

### Requirement: Retain safe ownership during incomplete process shutdown
Process shutdown SHALL bound caller waiting and distinguish completed cleanup from `shutdown_incomplete`. A pipe still used by a reader SHALL retain its owner until safe release. Incomplete cleanup SHALL provide retained status/outcome observation and bounded waiting. Completion SHALL require child reaping, reader-stop confirmation, terminal pipe release attempts, and published cleanup outcomes, not successful release of every resource.

#### Scenario: Reader exceeds its shutdown budget
- **WHEN** cancellation and bounded shutdown leave a descriptor-backed reader alive
- **THEN** shutdown SHALL report incomplete cleanup rather than success
- **AND** that reader's pipe SHALL remain owned and SHALL NOT be closed beneath the reader
- **AND** independently safe releases and child termination/reaping SHALL proceed
- **AND** prepared per-process continuation SHALL retain responsibility without relying on garbage collection or a global ownership registry

#### Scenario: Reader later stops
- **WHEN** a retained reader actually stops after incomplete shutdown was reported
- **THEN** its designated owner SHALL receive exactly one release attempt
- **AND** the retained cleanup result SHALL expose completion and any late failure without duplicate diagnostics
- **AND** no late domain output or user-facing callback SHALL begin

#### Scenario: Cleanup observation times out
- **WHEN** bounded waiting for cleanup expires and a reader still has not stopped
- **THEN** status SHALL remain incomplete and the same owner SHALL remain retained
- **AND** observation SHALL NOT retry close, restart shutdown, or claim completion
- **AND** a reader that never stops SHALL NOT be promised eventual resource release

#### Scenario: Context exit cannot finish cleanup
- **WHEN** context exit reaches its shutdown budget without completing cleanup
- **THEN** without an active operation failure it SHALL report a controlled incomplete-cleanup failure with observation access
- **AND** with an active failure it SHALL preserve shared precedence and attach incomplete-cleanup observation to the authoritative failure
- **AND** incomplete-shutdown timeout SHALL be ordinary and SHALL NOT mask an existing interruption

#### Scenario: Collection returns operation and cleanup failures as values
- **WHEN** the sole capture or stream attempt encounters ordinary failures or unexpected defects
- **THEN** it SHALL return an immutable ProcessResult containing bounded stdout/stderr bytes, known returncode, timed_out, lifecycle: LifecycleResult and ordered immutable stream_outcomes
- **AND** lifecycle.failures SHALL contain typed FailureInfo entries with selected authority first and exact exception identity, rather than raising that ordinary/defect authority
- **AND** publication SHALL synchronize copied bytes, selected failures and outcomes without releasing a pipe beneath a live reader
- **AND** neither collection nor handoff SHALL attach error.process_result, add a generic interruption snapshot or require a new owner.result accessor

#### Scenario: Snapshot identifies an authoritative reader through unified metadata
- **WHEN** capture or stream selects its authoritative exception under shared cleanup precedence
- **THEN** ProcessResult.lifecycle.failures[0], when present, SHALL describe that exact winning occurrence, with phase=READ and its stream only for a local reader authority
- **AND** a winning join/release/wait/action defect or coordinator interruption SHALL retain its own phase rather than READ even when stream_outcomes contains READ_FAILED
- **AND** concurrent reader failures SHALL identify the selected reader rather than infer it from stream ordering; exception-object reuse by a non-reader action SHALL NOT mislabel its winning defect
- **AND** generic exception identity/type and previously delivered snapshots SHALL remain unchanged, without a wrapper, unbounded event/ownership log, traceback/text heuristics or late metadata replacement

#### Scenario: Failed collection snapshot remains unchanged
- **WHEN** capture or stream returns a ProcessResult while cleanup remains incomplete
- **THEN** lifecycle.complete SHALL be False and copied output SHALL NOT share mutable reader buffers
- **AND** later output, cleanup failures or completion SHALL NOT mutate the delivered value, attach snapshots to exceptions or emit late diagnostics
- **AND** eventual cleanup SHALL remain observable through the same owner's cleanup status and bounded waiting
- **AND** raw streaming tails SHALL NOT be replayed into callbacks or treated as a transcript of delivered chunks

#### Scenario: Launch fails before collection begins
- **WHEN** validation or preparation fails before a usable prepared owner and failure ledger exist
- **THEN** the exception SHALL NOT be required to carry process_result and no owner SHALL be fabricated
- **WHEN** ordinary launch/setup failure occurs after that usable prepared-owner boundary exists
- **THEN** launch SHALL perform bounded independent cleanup and return that same failed owner; its sole collection SHALL return available bytes, known returncode and typed SETUP failures, without a fabricated child or streams
- **WHEN** control flow interrupts launch or collection
- **THEN** the exact first interruption SHALL propagate after bounded independent cleanup without error.process_result or a promised generic output snapshot; a returned owner SHALL NOT be promised for interrupted launch

#### Scenario: Late release fails after result delivery
- **WHEN** deferred stream release fails after the operation result or exception has already been delivered
- **THEN** the late failure SHALL remain observable in the retained cleanup result
- **AND** it SHALL NOT retroactively replace or mutate the delivered result or authoritative exception

### Requirement: Describe selected process failure authority with immutable metadata
Process coordination SHALL return one immutable aggregate sequence of typed failures in LifecycleResult, with selected authority first. Each FailureInfo SHALL contain the exact error, classification and owner-relative context. ActionResult SHALL be observation transport rather than a nested duplicate failure store. No result or failure record SHALL grant resource ownership.

#### Scenario: Declare the unified metadata surface
- **WHEN** the process failure contract is exposed
- **THEN** FailureKind SHALL have exactly ORDINARY, UNEXPECTED and INTERRUPTION, and FailurePhase SHALL have exactly SETUP, READ, JOIN, RELEASE, WAIT, ACTION, CALLBACK, REPORTER and COORDINATION
- **AND** frozen FailureInfo SHALL have exactly error: BaseException, kind: FailureKind, phase: FailurePhase and stream: ProcessStream | None = None
- **AND** frozen ActionResult SHALL have exactly done: bool, cleanup_complete: bool and failures: tuple[FailureInfo, ...] = ()
- **AND** frozen LifecycleResult SHALL have exactly complete: bool and failures: tuple[FailureInfo, ...] = (), replacing generic CleanupResult
- **AND** frozen ProcessResult SHALL have stdout: bytes, stderr: bytes, returncode: int | None, timed_out: bool, lifecycle: LifecycleResult and stream_outcomes: tuple[tuple[ProcessStream, StreamOutcome], ...] = ()
- **AND** redundant .error/.failure_info/error_kind/reader_failure_stream/callback_errors fields, ProcessResult.cleanup and nested action results SHALL be removed without aliases or legacy conversion modes
- **AND** FailureInfo SHALL NOT copy exception text/traceback/output or retain an owner, timestamp, event log or raw resource; domain LifecycleOutcome fields and capture input signature SHALL remain unchanged

#### Scenario: Publish metadata of the winning occurrence
- **WHEN** shared cleanup precedence selects authority
- **THEN** its FailureInfo SHALL be published atomically with that exact exception and immutable result snapshot
- **AND** ORDINARY SHALL describe an ordinary operation or cleanup failure while their distinct ranks remain in the existing ledger; UNEXPECTED SHALL describe an unexpected defect and INTERRUPTION a control-flow interruption
- **AND** pipe release OSError SHALL remain ORDINARY, reader join OSError SHALL remain UNEXPECTED under ordinary=(), and the first interruption SHALL retain authority and identity
- **AND** metadata SHALL follow the selected occurrence even when one exception object is reused across different actions; equal-rank competing readers SHALL retain the selected reader's context
- **AND** no adapter SHALL reconstruct kind or phase from exception type alone, text, traceback, READ_FAILED or private owner state

#### Scenario: Validate phase and stream context
- **WHEN** failure metadata is constructed or consumed
- **THEN** its kind, phase and optional stream SHALL have the declared enum types
- **AND** READ, CALLBACK and REPORTER SHALL require a stream; SETUP, JOIN and RELEASE SHALL permit a known stream or None; WAIT, ACTION and COORDINATION SHALL require None
- **AND** phase SHALL identify the recorded owner-relative boundary, not an inferred inner traceback location
- **AND** invalid metadata SHALL be rejected without granting authority, retrying release or skipping independent cleanup

#### Scenario: Separate completion from success and late observation
- **WHEN** LifecycleResult is observed through shutdown, cleanup_status, wait_cleanup or a returned ProcessResult
- **THEN** complete SHALL describe settlement of owned local resources independently of failure presence; empty failures SHALL mean no selected failure
- **AND** failures SHALL contain selected authority first and deterministic secondary order, retaining each exception identity once with its selected occurrence context
- **AND** incomplete collection reporting SHALL retain existing authority and ordinary COORDINATION context when a controlled incomplete error is needed; observation timeout alone SHALL NOT imply a new failure
- **AND** late observations SHALL be fresh immutable LifecycleResult values without mutating delivered records, generic exception diagnostics, restarting budgets or redoing collection

#### Scenario: Preserve helper classification at the parent action boundary
- **WHEN** a separately owned helper delivers a failure to a managed action
- **THEN** the helper's returned ProcessResult.lifecycle.failures SHALL retain original phase/stream and kind, including prepared-owner launch/setup failure
- **AND** parent ActionResult.failures SHALL transfer those exact error objects and selected kinds with phase=ACTION and stream=None, without type-based reclassification or attaching results to exceptions
- **AND** an unexpected helper join OSError SHALL remain UNEXPECTED and an ordinary helper operation failure SHALL remain ordinary parent cleanup, not become the parent's original operation primary
- **AND** helper READ/STDOUT SHALL NOT masquerade as a main READ/STDOUT failure
- **AND** retained secondaries, first-interruption identity, independent cleanup and bounded late observation SHALL remain intact without new public domain result fields

#### Scenario: Require strict typed action transport without compatibility inference
- **WHEN** ManagedAction.observe returns ActionResult
- **THEN** done SHALL mean request settlement and cleanup_complete SHALL independently mean owned action-resource settlement; neither flag SHALL imply absence of failures
- **AND** failures SHALL be a tuple of explicit valid FailureInfo entries with BaseException error, phase=ACTION and stream=None; bare errors, old fields and missing classification SHALL be protocol violations, not instructions for inference
- **AND** the coordinator SHALL consume failures into the single LifecycleResult sequence while retaining progress internally, not nest duplicate action/error lists in the final result
- **AND** an Exception SHALL NOT be labelled INTERRUPTION and a control-flow BaseException SHALL NOT be labelled ORDINARY or UNEXPECTED
- **AND** all production producers, helper handoffs, fixtures, examples and API assertions SHALL migrate to this strict contract without legacy mode, normalization fallback, compatibility alias or shim
- **AND** exceptions raised directly by action methods SHALL retain classification at the coordinator's known operation boundary under its declared ordinary-error policy; that rule SHALL NOT be used to infer metadata for returned error data
- **AND** malformed observations SHALL become unexpected ACTION validation defects through shared cleanup rather than bypassing independent actions
- **AND** an actual control-flow BaseException already supplied by the observation SHALL first be retained with exact identity as INTERRUPTION/ACTION/None, so invalid metadata cannot discard or demote it; other supplied exceptions SHALL remain secondary without fabricated interruption authority
- **AND** repeated/cumulative action observations SHALL NOT duplicate retained failures or erase accumulated authority when the action later reports success

### Requirement: Retain typed per-stream outcomes for one collection
The generic process lifecycle SHALL permit exactly one collection attempt, capture or stream, and retain typed terminal outcomes. The sole stream call SHALL observe completion safely even before registration. ProcessResult SHALL carry immutable stream_outcomes. Repeated or racing collections SHALL fail as usage errors without output replay or lifecycle work.

#### Scenario: Distinguish stream collection from resource cleanup
- **WHEN** a captured stream reaches clean EOF, fails during reading or has collection forcibly discontinued
- **THEN** its outcome SHALL transition once from StreamOutcome.PENDING to EOF, READ_FAILED or ABORTED respectively, and SHALL NOT later be revised
- **AND** a sibling failure SHALL NOT replace an already established EOF
- **AND** a callback failure SHALL NOT be classified as READ_FAILED
- **AND** ABORTED SHALL NOT imply child exit, reader-stop confirmation or safe pipe release
- **AND** retained readers SHALL continue to use the existing safety barriers and owners

#### Scenario: Deliver typed chunks and prompt independent completion
- **WHEN** a caller invokes stream with a chunk callback and optional on_stream_end callback
- **THEN** ProcessStream SHALL provide exactly STDOUT and STDERR, and StreamOutcome SHALL provide exactly PENDING, EOF, READ_FAILED and ABORTED
- **AND** the chunk callback SHALL receive `(ProcessStream, bytes)` rather than string stream names
- **AND** on_stream_end SHALL receive `(ProcessStream, StreamOutcome)` with terminal outcomes only, never PENDING
- **AND** each end notification SHALL follow that invocation's last assigned chunk for its stream
- **AND** clean EOF notification SHALL NOT wait for the sibling stream to finish
- **AND** callbacks SHALL run on that invocation's coordinating thread outside state locks and SHALL receive no pipe or release authority

#### Scenario: Completion precedes or races with registration
- **WHEN** stream is invoked with on_stream_end after a stream has completed or concurrently with its completion
- **THEN** registration and retained-outcome delivery SHALL be synchronized to make exactly one terminal notification attempt per stream for that invocation before it returns or raises
- **AND** previously established EOF, READ_FAILED or ABORTED outcomes SHALL remain available to the new callback
- **AND** no separate subscription method or parallel string callback channel SHALL be required

#### Scenario: Reject repeated and racing collection attempts
- **WHEN** any validated capture or stream attempt has claimed the owner, including one that failed or was interrupted
- **THEN** every second capture/stream call, mixed-method call or racing loser SHALL raise RuntimeError before callbacks, reporting, replay, reader creation or cleanup initiation
- **AND** callback argument validation SHALL precede the atomic claim; invalid callback configuration SHALL NOT consume the sole attempt
- **AND** no outcome-only subscription compatibility path SHALL remain; late cleanup SHALL be observed only through cleanup_status/wait_cleanup without callback registration

#### Scenario: Completion callback fails or collection is interrupted
- **WHEN** an ordinary chunk or end callback exception occurs
- **THEN** it SHALL trigger one diagnostic reporting attempt without retrying the callback, retaining callback_errors in results, erasing the stream outcome or being classified as a read/cleanup failure
- **AND** control-flow interruptions SHALL remain coordinator responsibilities: existing first-interruption precedence SHALL apply to the sole collection, with exact propagation after independent cleanup and no generic interruption output snapshot
- **WHEN** collection is forcibly discontinued before an acquired stream has a terminal outcome
- **THEN** its outcome SHALL become ABORTED before operation delivery, without waiting indefinitely for its reader
- **AND** remaining terminal notification attempts SHALL precede return or authoritative exception delivery
- **AND** late reader completion SHALL NOT revise ABORTED or initiate an automatic callback

#### Scenario: Process result snapshots retain per-stream outcomes
- **WHEN** capture or stream returns its sole ProcessResult
- **THEN** existing ProcessResult SHALL contain `stream_outcomes: tuple[tuple[ProcessStream, StreamOutcome], ...]`, with each stream once in STDOUT/STDERR order
- **AND** acquired streams SHALL have terminal outcomes in the delivered snapshot
- **AND** outcome copying SHALL be synchronized with collection delivery and retained callback accounting
- **AND** late cleanup SHALL NOT mutate that value or attach a result to any exception; repeated subscriptions SHALL be rejected
- **AND** PENDING SHALL describe only an outcome not yet established, not reader execution or resource release

### Requirement: Report callback failures through injected diagnostic policy
The generic process lifecycle SHALL accept an optional prompt-returning diagnostic reporter at launch, validated before acquisition, for ordinary chunk/end callback errors in the sole stream invocation. Reporter policy SHALL remain separate from resource ownership and control-flow propagation. ProcessResult SHALL NOT contain callback_errors or replacement callback-error stores; error.process_result and generic interruption snapshots SHALL NOT be created.

#### Scenario: Select diagnostic policy without importing domain policy
- **WHEN** an ordinary callback exception is reported
- **THEN** an injected reporter SHALL receive the exception object and bounded callback-kind/stream context outside state locks on the invocation's coordinating thread
- **AND** the generic mechanism SHALL NOT eagerly render exception text, traceback, raw notes or output bytes into public logs
- **AND** the injected policy MAY deliberately log raw exceptions or mask secrets/URLs; domain masking knowledge SHALL remain outside docker.process
- **AND** without an injected reporter only a fixed safe callback-failure message SHALL be emitted
- **AND** reporting SHALL NOT retry callbacks, alter terminal outcomes, grant pipe/process authority or change operation failure precedence

#### Scenario: Reporter or fallback fails ordinarily
- **WHEN** an ordinary Exception escapes the reporter
- **THEN** an independent nonrecursive fallback SHALL attempt one fixed safe diagnostic without original/reporter exception text, repr, traceback, notes or output
- **AND** the failing reporter SHALL NOT be called to report itself
- **AND** ordinary fallback failure SHALL be best-effort and SHALL NOT recursively report, mask operation authority or prevent independent safe cleanup/notification attempts
- **AND** reporter/fallback implementations SHALL return promptly without a promise to forcibly bound arbitrary blocking logging handlers

#### Scenario: Reporting or callback raises control flow
- **WHEN** a callback, reporter or fallback raises KeyboardInterrupt, SystemExit, GeneratorExit or another control-flow BaseException
- **THEN** the coordinator SHALL preserve its identity and apply interruption precedence rather than swallow it or delegate propagation policy to the reporter
- **AND** independently safe cleanup and remaining terminal notification attempts SHALL precede delivery without callback retries

#### Scenario: Propagate interruption without a generic output snapshot
- **WHEN** launch or the sole collection encounters control-flow BaseException, including from callback/reporter/fallback
- **THEN** the first interruption SHALL propagate with exact identity after bounded independent cleanup attempts
- **AND** no generic output snapshot, error.process_result or new owner.result accessor SHALL be promised; cleanup observation SHALL remain available if the caller already received the owner
- **AND** InterruptedError SHALL remain an OSError subject to the operation's declared ordinary-error policy, not automatically control flow
- **AND** ordinary callback/reporting isolation SHALL NOT swallow control flow, and context exit SHALL NOT rethrow ordinary/defect failures already delivered as collection result data

### Requirement: Regulate new descriptor-owning code through documented filesystem wrappers
New descriptor-owning code SHALL select a filesystem lifecycle wrapper and verify its ownership paths. The project SHALL publish that contract in `docs/filesystem-lifecycle.md`, reference it from root `AGENTS.md`, OpenSpec project context, and README links, and enforce it through complete-production-tree convergence checks.

#### Scenario: Enforce guidance beyond historical migrations
- **WHEN** a new production module or aliased resource operation is added
- **THEN** convergence checks SHALL classify its ownership without relying only on the historical migration function list
- **AND** project guidance SHALL NOT authorize unowned descriptor release or extend foundation domain authority

#### Scenario: Select a wrapper for a new resource
- **WHEN** new production code acquires an integer fd or an already fd-owning stream
- **THEN** it SHALL immediately use `OwnedDescriptor`, the applicable `DirectoryDescriptor` capability, or `OwnedStream` respectively, with constructor inputs prepared before acquisition
- **AND** raw-fd file-object wrappers SHALL retain `closefd=False` with a live external descriptor owner
- **AND** every transfer SHALL explicitly identify the receiving ownership responsibility
- **AND** subprocess consumers SHALL use the prepared unified process lifecycle rather than compose independent launch/adoption/reader/shutdown machinery
- **AND** the process lifecycle SHALL still use one filesystem stream owner per acquired pipe, without broadening direct-close permissions
- **AND** external cooperative stop policy SHALL be configured at launch without creating a second local shutdown owner
- **AND** local resource completion SHALL NOT be treated as evidence that externally controlled work stopped

#### Scenario: Verify a new ownership path
- **WHEN** a new acquisition, transfer, wrapper, or cleanup branch is introduced
- **THEN** its tests SHALL cover operation-plus-close failure, sole close failure, interruption, and repeat cleanup, plus independent cleanup, handoff, or concurrency where applicable
- **AND** alias-resolved convergence checks SHALL reject bare consumer release, duplicate ownership, and missing immediate adoption
- **AND** unresolved close-like calls SHALL fail closed until proved non-descriptor

#### Scenario: Keep guidance aligned with executable APIs
- **WHEN** filesystem ownership documentation or APIs change
- **THEN** documented examples SHALL execute against the supported API and cross-references SHALL resolve
- **AND** public-surface and dependency assertions SHALL permit only the bounded declared lifecycle additions

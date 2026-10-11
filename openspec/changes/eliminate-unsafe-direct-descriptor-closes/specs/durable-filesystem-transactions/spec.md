## ADDED Requirements

### Requirement: Converge descriptor-owning cleanup on explicit lifecycle guarantees
A descriptor-owning consumer SHALL represent each adopted descriptor through the shared lightweight descriptor lifecycle, SHALL mark ownership terminal before each release attempt, and SHALL issue at most one close attempt for each owned descriptor. It SHALL preserve an active operation failure over an ordinary close failure while retaining that close failure as secondary diagnostic context. When several descriptors are independently owned, one aggregate cleanup accumulator SHALL attempt every release even when an earlier release fails. A direct descriptor-close call SHALL remain limited to low-level POSIX adapters, the internal release operation of `OwnedDescriptor` or an equivalent shared owner after ownership is terminal, and ownership-handoff operations whose releasing parent is already terminal and whose opened child is already tracked for failure cleanup.

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
- **AND** the consuming cleanup coordinator SHALL retain the winning outcome before returning

#### Scenario: Adopt a stream without double ownership
- **WHEN** a factory has already created a file object that owns its descriptor
- **THEN** the consumer SHALL adopt the file object into the shared stream owner before fallible use
- **AND** SHALL NOT independently adopt or close its `fileno()` descriptor
- **AND** failure during multi-stream adoption SHALL release all acquired resources once under shared precedence

#### Scenario: Close failure or interruption is terminal
- **WHEN** an owned stream close fails ordinarily, raises an unexpected defect, or is interrupted
- **THEN** ownership SHALL remain terminal and subsequent cleanup SHALL issue no further underlying close
- **AND** independent owners SHALL still receive their release attempts
- **AND** authoritative failure selection SHALL follow shared cleanup precedence without wrapping process-control objects

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

#### Scenario: Verify a new ownership path
- **WHEN** a new acquisition, transfer, wrapper, or cleanup branch is introduced
- **THEN** its tests SHALL cover operation-plus-close failure, sole close failure, interruption, and repeat cleanup, plus independent cleanup, handoff, or concurrency where applicable
- **AND** alias-resolved convergence checks SHALL reject bare consumer release, duplicate ownership, and missing immediate adoption
- **AND** unresolved close-like calls SHALL fail closed until proved non-descriptor

#### Scenario: Keep guidance aligned with executable APIs
- **WHEN** filesystem ownership documentation or APIs change
- **THEN** documented examples SHALL execute against the supported API and cross-references SHALL resolve
- **AND** public-surface and dependency assertions SHALL permit only the bounded declared lifecycle additions

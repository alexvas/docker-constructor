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

## MODIFIED Requirements

### Requirement: Bind descriptor construction authority
`DescriptorError` and `UnsafeDescriptorError` SHALL retain `(stage, message, *, cause=None)` and cause chaining. Raw-fd construction and adoption SHALL remain inside the runtime ownership implementation or its test seams. Application acquisition SHALL return an owner without exposing an unowned fd. Constructors SHALL validate arguments before transfer; directory adoption SHALL consume accepted ownership and either return a validated sole owner or release once on validation failure.

#### Scenario: Constructing an owned descriptor directly
- **WHEN** a permitted implementation or test caller constructs `OwnedDescriptor` with descriptor operations, a non-negative integer fd, and a non-empty label
- **THEN** the new object SHALL become the descriptor's sole owner without issuing a filesystem validation operation
- **AND** the construction seam SHALL NOT authorize application raw-fd acquisition or transfer

#### Scenario: Rejecting invalid owned-descriptor arguments
- **WHEN** `OwnedDescriptor` receives a negative or non-integer fd or an empty label
- **THEN** construction SHALL fail before ownership transfers
- **AND** SHALL issue no close or other descriptor operation

#### Scenario: Rejecting direct directory construction
- **WHEN** a caller invokes `DirectoryDescriptor` directly rather than an authorized factory or adoption seam
- **THEN** construction SHALL raise `TypeError` before ownership transfers or a descriptor operation is issued

#### Scenario: Adoption validation fails
- **WHEN** `DirectoryDescriptor.adopt()` has accepted ownership and directory-type or requested effective-owner validation fails
- **THEN** the validation failure SHALL remain authoritative over an ordinary close failure
- **AND** the adopted descriptor SHALL receive exactly one release attempt and SHALL not be returned

#### Scenario: Application acquires a descriptor-backed resource
- **WHEN** an application caller opens or creates a resource
- **THEN** the supported factory SHALL return ownership rather than a raw integer fd
- **AND** fallible label, backend and synchronization preparation SHALL precede acquisition
- **AND** a post-acquisition failure SHALL retain one release authority under shared precedence

### Requirement: Expose the stable descriptor capability API
The independently installable ownership library SHALL expose explicit filesystem and process entry points with documented contracts. Existing error and cleanup semantics SHALL remain stable. Application APIs SHALL return owners and offer operations and borrowed capabilities, not raw descriptors. Raw constructor, fd access and detach seams SHALL be permitted inside the runtime implementation/tests, not exposed to consumers.

#### Scenario: Consumer imports one foundation component
- **WHEN** a consumer imports a named API from a public ownership entry point or an approved compatibility facade
- **THEN** the import SHALL resolve with its documented name and signature
- **AND** SHALL NOT load application domains or the architecture analyzer through the runtime package initializer
- **AND** imports SHALL resolve from the installed library distribution without depending on the application's source tree

#### Scenario: Descriptor lifecycle API is inspected
- **WHEN** a caller inspects the capability API
- **THEN** it SHALL find documented owning, borrowing, acquisition and operation contracts
- **AND** existing directory basename safety and child-operation semantics SHALL remain supported
- **AND** compatibility seams exposing raw descriptors SHALL NOT count as permitted application use
- **AND** the application's raw-primitive argument/return prohibition SHALL NOT be applied to calls between runtime implementation functions

## ADDED Requirements

### Requirement: Operate on descriptors without exposing their numbers
Owners and borrowed capabilities SHALL provide bounded read, write and stat operations and the low-level operations required by the migrated consumers. Each operation SHALL verify live authority before accessing the descriptor. These mechanisms SHALL NOT assume hashing, recursion, publication sequencing, advisory-lock policy, process coordination or domain-error authority.

#### Scenario: Read or stat through a live capability
- **WHEN** an application invokes a supported descriptor operation
- **THEN** the capability SHALL perform it without returning or exposing the underlying fd
- **AND** partial reads or writes and ordinary errors SHALL preserve their documented low-level semantics

#### Scenario: Operation follows release or transfer
- **WHEN** a released or transferred authority or its borrowed view is used
- **THEN** access SHALL fail before issuing a filesystem operation

#### Scenario: Application needs a Python file-object wrapper
- **WHEN** a raw descriptor must support buffered file-object operations
- **THEN** wrapper construction SHALL occur inside the runtime ownership boundary with the descriptor owner as sole release authority
- **AND** application callers SHALL receive a restricted capability rather than a raw owning stream or a fileno escape
- **AND** wrapper flush or close failure SHALL preserve operation-primary precedence while descriptor release remains terminal

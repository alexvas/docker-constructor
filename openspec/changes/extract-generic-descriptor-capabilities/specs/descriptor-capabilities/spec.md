## Purpose

Define lightweight, domain-neutral ownership and safety guarantees for live filesystem descriptors used across otherwise independent Constructor subsystems.

## ADDED Requirements

### Requirement: Own each descriptor through one explicit lifecycle
A descriptor capability SHALL exclusively own each adopted live descriptor until it either transfers that ownership or attempts release. Release SHALL mark ownership terminal before invoking the underlying close operation, SHALL attempt close at most once, and SHALL NOT retry after any close outcome. Access through a released or transferred capability SHALL fail before issuing a filesystem operation.

#### Scenario: Close reports an error
- **WHEN** release invokes close and close reports an error
- **THEN** the capability SHALL propagate that error according to the active failure policy
- **AND** a later release SHALL NOT invoke close again

#### Scenario: Ownership is transferred
- **WHEN** a capability explicitly transfers its descriptor to another owner
- **THEN** the source capability SHALL become terminal without closing the descriptor
- **AND** subsequent access or release through the source SHALL issue no descriptor operation

### Requirement: Bind descriptor construction authority
`DescriptorError` and `UnsafeDescriptorError` SHALL use the design-specified `(stage, message, *, cause=None)` constructor contract, retain the stage and cause, and chain a supplied cause. `OwnedDescriptor` SHALL be directly constructible through the design-specified `(ops, fd, *, label)` test and ownership seam. It SHALL accept ownership only after validating a non-negative integer descriptor and non-empty label, and SHALL perform no filesystem validation during construction. `DirectoryDescriptor` SHALL reject direct construction before accepting ownership and SHALL grant directory authority only through `open_secure_path()` or `adopt()`. After ordinary argument validation, `adopt()` SHALL consume the supplied descriptor and either return its sole validated owner or release it exactly once if directory or effective-owner validation fails.

#### Scenario: Constructing an owned descriptor directly
- **WHEN** a caller constructs `OwnedDescriptor` with descriptor operations, a non-negative integer fd, and a non-empty label
- **THEN** the new object SHALL become the descriptor's sole owner without issuing a filesystem validation operation
- **AND** SHALL provide the supported seam for lifecycle tests

#### Scenario: Rejecting invalid owned-descriptor arguments
- **WHEN** `OwnedDescriptor` receives a negative or non-integer fd or an empty label
- **THEN** construction SHALL fail before ownership transfers
- **AND** SHALL issue no close or other descriptor operation

#### Scenario: Rejecting direct directory construction
- **WHEN** a caller invokes `DirectoryDescriptor` directly rather than `open_secure_path()` or `adopt()`
- **THEN** construction SHALL raise `TypeError` before ownership transfers or a descriptor operation is issued

#### Scenario: Adoption validation fails
- **WHEN** `DirectoryDescriptor.adopt()` has accepted ownership and directory-type or requested effective-owner validation fails
- **THEN** the validation failure SHALL remain authoritative over an ordinary close failure
- **AND** the adopted descriptor SHALL receive exactly one release attempt and SHALL not be returned

### Requirement: Preserve failure precedence during descriptor release
A capability used as a cleanup boundary SHALL preserve an active operation failure when release raises an ordinary declared close failure and SHALL retain that close failure as secondary diagnostic context. A cleanup interruption or unexpected defect SHALL follow the shared cleanup precedence, and independent owned descriptors SHALL each receive one release attempt even when another release fails.

#### Scenario: Ordinary close fails during exception propagation
- **WHEN** an operation has already failed and release raises an ordinary close failure
- **THEN** the operation failure SHALL remain authoritative
- **AND** the close failure SHALL remain observable as secondary diagnostic context

#### Scenario: One of several independent releases fails
- **WHEN** multiple descriptors require cleanup and one release fails
- **THEN** every remaining independent descriptor SHALL still receive exactly one release attempt
- **AND** the final failure SHALL follow deterministic shared cleanup precedence

### Requirement: Open and validate directories without following control-path symlinks
The generic capability boundary SHALL support opening an absolute directory path by walking components descriptor-relatively without following symlinks, validating the final descriptor as a directory and, when requested, as owned by the invoking effective user. Intermediate and child descriptor handoff SHALL neither leak a newly opened descriptor nor retry an ambiguously failed close.

#### Scenario: Secure path walk succeeds
- **WHEN** every component of an absolute path is a real directory and the final directory satisfies the requested ownership policy
- **THEN** the operation SHALL return one live capability owning the validated final descriptor
- **AND** SHALL retain no intermediate descriptor

#### Scenario: Parent release fails after opening a child
- **WHEN** a secure walk opens a child and release of the retained parent fails
- **THEN** the child SHALL not be returned
- **AND** the child SHALL receive exactly one release attempt without retrying the parent release

#### Scenario: Unsafe path component is encountered
- **WHEN** a walked component is a symlink or not a directory, or the final directory violates the requested ownership policy
- **THEN** the operation SHALL fail without following or mutating the unsafe entry
- **AND** SHALL release every descriptor it still owns according to the shared failure precedence

### Requirement: Restrict child directory operations to one basename
A directory capability SHALL expose only single-component child names to its generic child-directory operations. It SHALL support no-follow child open, exclusive child creation, and create-or-open with explicit mode and ownership validation, while leaving path derivation and domain policy to the caller.

#### Scenario: Unsafe child name is supplied
- **WHEN** a child name is empty, special, absolute, contains a separator, or otherwise names more than one path component
- **THEN** the capability SHALL reject it before issuing a filesystem operation

#### Scenario: Private child is created
- **WHEN** an allowed missing child is created with an explicit owner-private mode
- **THEN** the returned capability SHALL own a no-follow descriptor for the new directory
- **AND** the directory SHALL be validated before authority is returned

### Requirement: Expose the stable descriptor capability API
The generic foundation SHALL be available through the explicit modules `docker.filesystem.cleanup`, `docker.filesystem.operations`, and `docker.filesystem.descriptors`. The cleanup module SHALL expose `CleanupFailures`, `attach_secondary`, and `carry_secondary_diagnostics`; the operations module SHALL expose `DescriptorOps` and `PosixDescriptorOps`; and the descriptors module SHALL expose `DescriptorError`, `UnsafeDescriptorError`, `OwnedDescriptor`, and `DirectoryDescriptor` with the signatures fixed by the change design. The `docker.filesystem` package initializer SHALL perform no aggregate imports or re-exports.

#### Scenario: Consumer imports one foundation component
- **WHEN** a consumer imports a named API from one `docker.filesystem` submodule
- **THEN** the import SHALL resolve with the design-specified name and signature
- **AND** SHALL NOT load `docker.transactions` or an unrelated filesystem submodule through the package initializer

#### Scenario: Descriptor lifecycle API is inspected
- **WHEN** a caller inspects `OwnedDescriptor` and `DirectoryDescriptor`
- **THEN** it SHALL find `close`, `detach`, `open_secure_path`, `adopt`, `child_basename`, `open_directory`, `create_directory`, `open_or_create_directory`, `stat_child`, `list_names`, `unlink_child`, and `remove_child_directory` with the design-specified parameter contracts

### Requirement: Keep multi-entry protocols outside descriptor capabilities
The generic descriptor capability SHALL NOT define recursive tree removal, cache-root selection, namespace layout, retries, idempotent-absence policy, locking, regular-file publication, durability, or domain error mapping. Callers SHALL own sequencing across multiple entries and translate generic failures into their domain contracts.

#### Scenario: A caller removes a directory tree
- **WHEN** a domain needs recursive tree removal
- **THEN** it SHALL compose basename-only descriptor operations under its own traversal and absence policy
- **AND** the generic capability SHALL grant no tree-transaction or recovery authority

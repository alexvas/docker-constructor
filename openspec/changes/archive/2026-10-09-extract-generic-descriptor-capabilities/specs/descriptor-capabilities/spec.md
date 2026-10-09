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

### Requirement: Present a uniform transaction L1 error boundary
Public transaction L1 capability operations SHALL translate operational filesystem failures from directory open, descriptor validation, regular-file read, and descriptor release into typed transaction errors with a stable operation stage and the original failure retained as the direct cause. Capability misuse SHALL remain a capability error, unsafe filesystem objects SHALL retain their dedicated safety classification, and process-control exceptions SHALL propagate unchanged. Translation SHALL preserve ownership-transfer rules, irreversible close attempts, active-primary precedence, and secondary cleanup diagnostics.

#### Scenario: Directory acquisition fails at any walk position
- **WHEN** opening the filesystem root, an intermediate component, the final directory, or a direct path reports an operational filesystem failure other than a no-follow symlink rejection
- **THEN** the transaction L1 boundary SHALL raise the same typed transaction error classification and operation stage regardless of walk position
- **AND** SHALL retain the original filesystem failure as its direct cause

#### Scenario: Directory acquisition rejects a symlink or non-directory
- **WHEN** a no-follow directory open reports `ELOOP` or `ENOTDIR` for a symlink or non-directory component
- **THEN** transaction L1 SHALL preserve its capability-safety rejection classification rather than treating it as an operational open failure
- **AND** SHALL retain the original filesystem failure as the direct exception cause without requiring a separate capability-error cause attribute

#### Scenario: Caller-owned descriptor validation cannot stat
- **WHEN** transaction L1 validates a caller-provided descriptor and its stat operation fails
- **THEN** it SHALL raise a typed transaction validation error caused by the original filesystem failure
- **AND** SHALL leave the descriptor caller-owned and issue no close operation

#### Scenario: Validated file read fails
- **WHEN** reading through a live regular-file capability reports an operational filesystem failure
- **THEN** transaction L1 SHALL raise a typed transaction read error caused by that failure

#### Scenario: Sole descriptor release fails
- **WHEN** closing a live transaction capability is the only active operation and release reports an operational filesystem failure
- **THEN** transaction L1 SHALL raise a typed transaction close error caused by that failure
- **AND** SHALL keep the release attempt terminal so a later close issues no operation

#### Scenario: Downstream cleanup handles typed capability close failure
- **WHEN** a transaction consumer releases an adopted capability through cleanup accumulation, a direct close handler, a deliberate suppression boundary, or an error translator
- **THEN** it SHALL recognize only a close-stage transaction error as the ordinary capability-close failure without swallowing unrelated transaction failures
- **AND** it SHALL preserve that typed close-stage error rather than unwrap its raw cause merely to reproduce historical raw-descriptor behavior
- **AND** when no other failure is active, the typed close-stage error SHALL propagate unchanged with the original filesystem failure available through both its stored cause and direct exception cause
- **AND** when another failure is active, that failure SHALL remain authoritative and the typed close-stage error SHALL remain observable as secondary diagnostic context

#### Scenario: Cleanup follows generated-directory ownership state
- **WHEN** effective-build publication opens its generated directory but transaction capability adoption fails
- **THEN** the still caller-owned raw descriptor SHALL receive exactly one direct release attempt and an ordinary release failure MAY remain a raw `OSError`
- **WHEN** generated-directory adoption succeeds and later release reports an operational filesystem failure
- **THEN** publication SHALL release only through the owning capability
- **AND** a sole release failure SHALL propagate as `TransactionError(STAGE_CLOSE)` with the original filesystem failure as its stored and direct exception cause
- **AND** an active publication failure SHALL remain authoritative with that typed close-stage error attached as secondary diagnostic context

### Requirement: Preserve typed operational failures across consumer boundaries
L2 adapters, cleanup accumulators, lock wrappers, and domain consumers SHALL preserve a typed transaction or lock failure when no domain translation is required. When translation to a domain error is required, the domain error SHALL chain directly from the typed failure so its stage, original cause, and secondary diagnostics remain reachable together. A consumer MAY inspect the raw cause to determine absence, errno, safety classification, or another documented domain result, but SHALL NOT replace the typed failure with that cause solely to reproduce historical raw-descriptor behavior. A raw operational error MAY remain authoritative before capability adoption, at an injected POSIX boundary, or where a separately documented public contract requires the exact raw exception type.

#### Scenario: Typed operational failure requires no domain translation
- **WHEN** an L2 adapter, lock wrapper, cleanup accumulator, or domain consumer receives a typed operational failure and its contract does not require another error type
- **THEN** it SHALL propagate or aggregate that typed failure rather than its raw cause
- **AND** the operation stage, original cause, and secondary diagnostics SHALL remain reachable through that typed failure

#### Scenario: Typed operational failure requires a domain error
- **WHEN** a consumer maps a typed operational failure to a domain error
- **THEN** the domain error SHALL chain directly from the typed failure
- **AND** SHALL NOT bypass the typed failure by chaining from its raw cause

#### Scenario: Consumer inspects a cause for policy
- **WHEN** a consumer examines a typed failure's cause to recognize absence, errno, a safety outcome, or another documented domain result
- **THEN** that inspection MAY select the required control-flow or domain outcome
- **AND** inspection alone SHALL NOT authorize replacing the typed failure with its cause

#### Scenario: Cleanup classifies a typed release failure
- **WHEN** a cleanup accumulator receives a typed capability-close or lock-release failure after successful adoption
- **THEN** it SHALL classify that typed failure directly under its declared cleanup policy
- **AND** SHALL NOT unwrap it merely to make it match `OSError`

#### Scenario: Exact raw exception type is a required contract
- **WHEN** a consumer intentionally replaces a typed failure with its raw cause
- **THEN** a separately documented public contract and focused regression test SHALL require the exact raw exception type
- **AND** an implementation-history compatibility claim alone SHALL NOT satisfy that requirement

#### Scenario: L2 validated read has a sole close failure
- **WHEN** a validated read succeeds but releasing its file capability raises an already typed close error
- **THEN** L2 SHALL propagate that transaction error unchanged rather than wrapping it again
- **AND** the original filesystem failure SHALL remain both its stored cause and direct exception cause

#### Scenario: L2 validated read and close both fail
- **WHEN** a validated read fails and releasing its file capability also raises a typed close error
- **THEN** the read failure SHALL remain authoritative
- **AND** the close failure SHALL remain observable as secondary diagnostic context

#### Scenario: Directory context release fails while another failure is active
- **WHEN** an operation failure is already active and directory-capability context cleanup reports an ordinary filesystem failure
- **THEN** the active failure SHALL remain authoritative
- **AND** the translated close failure and its original cause SHALL remain observable as secondary diagnostic context

#### Scenario: Capability contract is violated
- **WHEN** a caller uses a released capability, supplies an unsafe basename, or combines capabilities from different directory authorities
- **THEN** transaction L1 SHALL raise its capability-misuse error rather than classify the condition as an operational filesystem failure

#### Scenario: Process-control exception crosses L1
- **WHEN** an L1 operation or cleanup raises a process-control exception
- **THEN** the exception SHALL propagate unchanged rather than being wrapped as a transaction error

### Requirement: Keep multi-entry protocols outside descriptor capabilities
The generic descriptor capability SHALL NOT define recursive tree removal, cache-root selection, namespace layout, retries, idempotent-absence policy, locking, regular-file publication, durability, or domain error mapping. Callers SHALL own sequencing across multiple entries and translate generic failures into their domain contracts.

#### Scenario: A caller removes a directory tree
- **WHEN** a domain needs recursive tree removal
- **THEN** it SHALL compose basename-only descriptor operations under its own traversal and absence policy
- **AND** the generic capability SHALL grant no tree-transaction or recovery authority

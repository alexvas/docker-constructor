## ADDED Requirements

### Requirement: Preserve npm file-operation failures across descriptor release
Assembler lockfile-input writing and canonical tree-file hashing SHALL own each opened regular-file descriptor through an explicit at-most-once lifecycle. An ordinary descriptor-close failure SHALL NOT replace an active write, stat, type-validation, read, hashing, or npm domain failure, and every descriptor SHALL be released once when it cannot be returned. Existing lockfile bytes, tree manifests, digests, no-follow checks, domain reasons, and diagnostic wording SHALL remain unchanged.

#### Scenario: Lockfile writing and close both fail
- **WHEN** writing the exact lockfile input fails
- **AND** releasing its descriptor reports an ordinary close failure
- **THEN** the original write `OSError` SHALL remain authoritative without conversion to `LockedNpmError`
- **AND** the close failure SHALL remain observable as secondary diagnostic context
- **AND** the descriptor SHALL not be closed again

#### Scenario: Opening a tree-file entry fails
- **WHEN** the no-follow open of a tree-file entry reports `OSError`
- **THEN** `_hash_file_entry()` SHALL preserve the existing `LockedNpmError` with reason `tree_type_mismatch`
- **AND** the original `OSError` SHALL remain its cause

#### Scenario: Tree-file raw operation and close both fail
- **WHEN** tree-file stat or read reports `OSError`, or hashing reports another operation exception
- **AND** releasing the file descriptor reports an ordinary close failure
- **THEN** the exact stat, read, or hashing exception SHALL propagate unchanged without conversion to `LockedNpmError`
- **AND** the close failure SHALL remain observable as secondary diagnostic context
- **AND** canonical traversal, manifest bytes, and tree digest semantics SHALL remain unchanged

#### Scenario: Unsafe tree-file type and close both fail
- **WHEN** tree-file type validation rejects an unsafe entry with `LockedNpmError`
- **AND** releasing the file descriptor reports an ordinary close failure
- **THEN** that exact `LockedNpmError` SHALL remain authoritative
- **AND** the close failure SHALL remain observable as secondary diagnostic context

#### Scenario: Sole npm file release fails
- **WHEN** lockfile writing or tree-file hashing otherwise succeeds
- **AND** its sole descriptor release fails
- **THEN** the exact close `OSError` SHALL propagate unchanged without conversion to `LockedNpmError`
- **AND** the release attempt SHALL remain terminal

### Requirement: Own subprocess pipes across all assembly cleanup paths
Assembler streaming execution and captured cleanup clients SHALL use one unified process lifecycle for launch, internally controlled readers, and shutdown. Each stdout/stderr pipe SHALL retain one filesystem stream owner across all cleanup paths, with terminal at-most-once release and shared precedence. Consumers SHALL NOT separately adopt or close raw pipe descriptor numbers. Domain cleanup and diagnostic policy SHALL remain in assembler adapters.

#### Scenario: Preserve subprocess compatibility
- **WHEN** subprocess stream ownership is migrated
- **THEN** existing consumer input parameters, domain result fields, output tails, decoding, npm identities, controlled public errors, redaction, and configured shutdown bounds SHALL remain unchanged
- **AND** the sole authorized return-type change SHALL be `run_captured(...) -> tuple[OwnedProcess | None, LifecycleOutcome]`, replacing its returned raw `Popen | None` with the generic owner
- **AND** terminal at-most-once release and shared cleanup precedence SHALL be required corrections rather than baseline defects to preserve

#### Scenario: Captured client returns its owner and outcome snapshot
- **WHEN** `run_captured(argv, *, policy)` finishes execution or reports a capture/cleanup failure
- **THEN** it SHALL return the same `OwnedProcess` alongside the existing `LifecycleOutcome` snapshot when an owner is available
- **AND** a launch/setup failure carrying generic cleanup observation access SHALL return that same owner rather than reconstruct ownership
- **AND** the first element SHALL be `None` when no owner/cleanup observation handle is available
- **AND** `LifecycleOutcome` SHALL retain its `return_code`, `errors`, `stdout`, and `stderr` fields
- **AND** a returned owner SHALL NOT imply successful acquisition or execution, nor expose raw pipes for compatibility

#### Scenario: Captured cleanup continues after outcome delivery
- **WHEN** captured local cleanup is incomplete at delivery
- **THEN** the existing outcome error channel SHALL report bounded sanitized incomplete cleanup
- **AND** the returned owner SHALL provide status and bounded eventual cleanup observation
- **AND** later cleanup failures SHALL NOT mutate the delivered `LifecycleOutcome` or begin public callbacks/diagnostics
- **AND** the Docker removal caller SHALL retain its existing exit-code, container-absence and error-list interpretation

#### Scenario: A reader failure triggers multiple cleanup hooks
- **WHEN** a stream reader fails and reader-failure cleanup is followed by exception-path or final cleanup
- **THEN** all hooks SHALL reuse the same stdout/stderr owners
- **AND** each underlying stream close SHALL be attempted once, not retried because file objects are normally idempotent
- **AND** an ordinary close failure SHALL remain secondary to the active reader failure
- **AND** shared higher-precedence interruption or unexpected-defect behavior SHALL retain the displaced failure diagnostically

#### Scenario: Deadline and interruption race to release streams
- **WHEN** deadline cleanup races with caller interruption or finalization
- **THEN** release claims SHALL be synchronized and no stream SHALL receive a second underlying close
- **AND** the process cleanup coordinator SHALL retain the winning outcome before claiming completion, without duplicating close diagnostics
- **AND** a bounded return before completion SHALL report incomplete cleanup with retained responsibility
- **AND** process-control objects SHALL retain identity under shared precedence
- **AND** diagnostics exposed to users SHALL remain bounded and sanitized

#### Scenario: One stream fails to close
- **WHEN** stdout or stderr close fails ordinarily, unexpectedly, or by interruption
- **THEN** the other independently safe stream SHALL still receive its release attempt
- **AND** a stream still used by a reader SHALL retain its owner for release after reader shutdown
- **AND** process termination, removal where applicable, and bounded reaping SHALL not be skipped
- **AND** repeated cleanup SHALL issue no raw-close fallback or stream-close retry

#### Scenario: Post-launch setup fails
- **WHEN** stream adoption, reader setup, supervisor setup, or a launch callback fails after a successful process launch
- **THEN** all acquired streams SHALL be accounted for through prepared ownership and released once when safe under shared precedence
- **AND** adoption failure tests SHALL inject failures before transfer rather than close rejected streams themselves
- **AND** the launched child SHALL undergo the established bounded process cleanup
- **AND** no not-yet-adopted stream SHALL be stranded by failure while adopting its sibling

#### Scenario: Ownership preparation fails
- **WHEN** stream-owner synchronization or process cleanup preparation fails before execution
- **THEN** no Docker client subprocess SHALL be launched
- **AND** the failure SHALL remain controlled by the existing assembler boundary

#### Scenario: Captured factory omits a required stream
- **WHEN** an injected captured-client factory returns missing stdout, stderr or both despite requested `PIPE` capture
- **THEN** the adapter SHALL report a controlled setup failure through `LifecycleOutcome.errors` rather than successful execution with empty output
- **AND** every present pipe SHALL receive one release attempt when safe and the child SHALL undergo bounded cleanup
- **AND** normal factory doubles SHALL provide both streams as standard successful `Popen(..., stdout=PIPE, stderr=PIPE)` does
- **AND** generic missing-stream rejection SHALL remain unchanged without fabricated replacement streams or an expanded generic API

#### Scenario: Later adoption interruption takes precedence
- **WHEN** the first pipe transfer fails ordinarily and the second raises a process-control interruption
- **THEN** the exact interruption SHALL be authoritative with the first failure retained secondarily
- **AND** every acquired pipe SHALL remain accounted for and bounded child cleanup SHALL proceed

#### Scenario: Setup cleanup join fails
- **WHEN** joining a started captured-client reader raises an interruption or unexpected defect
- **THEN** independent pipe cleanup and child termination/reaping SHALL still be attempted
- **AND** a descriptor-backed pipe SHALL NOT be released while its reader remains able to use it
- **AND** the returned lifecycle outcome SHALL preserve shared precedence and bounded sanitized diagnostics

#### Scenario: Real pipe readers remain safe during bounded shutdown
- **WHEN** a descendant holds a pipe write end open, a reader fails, or cancellation requires forced shutdown
- **THEN** subprocess-layer coordination SHALL stop descriptor readers safely within the supported shutdown bounds before final underlying stream release
- **AND** it SHALL NOT close a raw descriptor number while a worker can still use it or block cleanup on a buffered-reader lock
- **AND** output draining, retained tails, decoding, abort redaction, and supported worker completion guarantees SHALL remain intact
- **AND** real-pipe tests SHALL verify fd-reuse safety and bounded completion, independently of descriptor-less test doubles

#### Scenario: Shutdown cannot complete within bounds
- **WHEN** a reader exceeds bounded shutdown during streaming execution or a captured cleanup client
- **THEN** the adapter SHALL report incomplete cleanup through its established controlled error/outcome interface rather than successful execution
- **AND** retained process ownership SHALL safely continue cleanup independently of the caller retaining a generic handle
- **AND** the diagnostic SHALL be bounded and sanitized without raw exception notes leaking URL or secret content
- **AND** Docker removal and staging cleanup SHALL still follow the established domain policy
- **AND** late cleanup outcomes SHALL be available internally without changing a delivered primary result, invoking user-facing domain callbacks, or emitting late public diagnostics

#### Scenario: Preserve domain removal ordering
- **WHEN** assembly timeout, interruption, reader failure or executor failure requires named-container cleanup
- **THEN** the adapter SHALL preserve the characterized terminate-CLI/remove/reap ordering and existing shutdown bounds
- **AND** removal failure SHALL NOT skip coordinated child or pipe cleanup
- **AND** the generic lifecycle SHALL NOT derive container identities or contain Docker command policy

#### Scenario: Audit subprocess stream origins
- **WHEN** a subprocess factory, `PIPE`, stream attribute, or stream-close callable is reached through an alias
- **THEN** the convergence ledger SHALL track its ownership origin and classify the underlying close as a shared-owner internal release only after terminal synchronized ownership is proved
- **AND** it SHALL reject per-cleanup owner reconstruction, duplicate raw-fd ownership, and blanket non-descriptor exemptions for subprocess streams
- **AND** unified process ownership SHALL be proved through its internal pipe-owner/reader-stop ledger rather than accepted by class name or import alone

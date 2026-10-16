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

#### Scenario: Derive deterministic secondary process-cleanup note prefixes
- **WHEN** the npm process adapter projects an already-reportable secondary process-cleanup FailureInfo into public exception notes
- **THEN** it SHALL use exactly `{subject} {operation} failed` as the prefix, replacing legacy labels and the previously approved single `stream cleanup failed` prefix
- **AND** stream STDOUT, STDERR and None SHALL map respectively to subject `stdout`, `stderr` and `process`
- **AND** phase SETUP, READ, JOIN, RELEASE, WAIT, ACTION, CALLBACK, REPORTER and COORDINATION SHALL map respectively to operation `setup`, `read`, `join`, `release`, `wait`, `action`, `callback`, `reporter` and `coordination`
- **AND** kind, timeout state and exception identity/type/text SHALL NOT affect the prefix; the declared mapping SHALL NOT depend on enum representation
- **AND** helper failures rebased to ACTION/None SHALL use `process action failed`, never a main-reader or guessed helper-stream label
- **AND** error details SHALL be formatted separately through existing bounded secret/URL sanitization; prefix selection SHALL NOT add diagnostics, retain ordinary callback errors or format control flow into ordinary notes
- **AND** this wording-only exception SHALL NOT change shared precedence, authoritative primary types/messages, generic exception identity, domain result fields, retained failures, output/tails or cleanup order/bounds/safety
- **AND** notes SHALL remain bounded, secret/URL-sanitized and once-only, without mutation of earlier delivered diagnostics or new late public callbacks
- **AND** no additional generic cleanup-provenance API or inference from timeout, exception type/text, traceback or private owner state SHALL be used to reconstruct legacy source labels
- **AND** authoritative reader projection through FailureInfo with phase=READ and stream, non-process file-operation diagnostics, callback reporter/fallback messages and incomplete-cleanup observation SHALL retain their separate contracts

#### Scenario: Captured client returns its owner and outcome snapshot
- **WHEN** `run_captured(argv, *, policy)` finishes execution or reports a capture/cleanup failure
- **THEN** it SHALL return the same `OwnedProcess` alongside the existing `LifecycleOutcome` snapshot when an owner is available
- **AND** a launch/setup failure carrying generic cleanup observation access SHALL return that same owner rather than reconstruct ownership
- **AND** the first element SHALL be `None` when no owner/cleanup observation handle is available
- **AND** `LifecycleOutcome` SHALL retain its `return_code`, `errors`, `stdout`, and `stderr` fields
- **AND** a returned owner SHALL NOT imply successful acquisition or execution, nor expose raw pipes for compatibility

#### Scenario: Project returned generic value failures into captured fields
- **WHEN** the sole generic capture returns ProcessResult containing ordinary or unexpected failures
- **THEN** run_captured SHALL project its bounded bytes/known returncode into existing LifecycleOutcome fields and lifecycle.failures entries' error objects into the existing error channel
- **AND** it SHALL use the returned value, not error.process_result, private owner output or repeated collection
- **AND** decoding, masking and Docker exit/absence interpretation SHALL remain domain responsibilities
- **AND** ordinary validation/preparation failure before a usable owner SHALL retain no-result mapping; ordinary launch/setup failure after that boundary SHALL return the same failed owner and its sole collection result
- **AND** control flow SHALL propagate with exact identity after cleanup rather than be swallowed into ordinary outcome errors

#### Scenario: Streaming failure retains domain-safe output
- **WHEN** streaming collection returns bounded raw ProcessResult data with lifecycle failures
- **THEN** the adapter SHALL preserve existing incremental redaction, per-stream finalization and abort-safe retained diagnostic tails
- **AND** it SHALL NOT replay raw snapshot tails into the sink, expose unsanitized generic bytes or initiate late public callbacks

#### Scenario: Project the authoritative reader error without disguising cleanup defects
- **WHEN** generic collection returns a selected ProcessResult.lifecycle.failures[0] identifying a local READ authority and its stream
- **THEN** the adapter SHALL preserve the existing StreamReaderFailure domain projection for a non-control-flow reader exception under the existing reader mapping, with that stream identity and bounded sanitized diagnostic detail
- **AND** JOIN or RELEASE with that stream, parent ACTION, or READ_FAILED alone SHALL NOT be treated as a local reader authority
- **AND** classification SHALL come from FailureInfo.kind and the error from FailureInfo.error, not exception type/text, traceback, private ledger state or removed parallel fields
- **AND** control-flow propagation and generic exception identity SHALL retain their existing contracts

#### Scenario: Preserve selected authority while projecting a domain timeout
- **WHEN** a timed-out generic invocation selects an ORDINARY cleanup failure such as pipe-close OSError
- **THEN** AssemblyTimeoutError SHALL remain the domain primary and the ordinary cleanup error SHALL appear once as a bounded sanitized secondary note
- **WHEN** the selected failure is UNEXPECTED, including reader join OSError, or INTERRUPTION
- **THEN** timed_out SHALL NOT replace its selected authority with AssemblyTimeoutError; the existing local-reader domain mapping SHALL still apply only where phase=READ and the authority is not control flow
- **AND** an unexpected join defect and a control-flow interruption SHALL retain exact generic identity, including an interruption arriving after deadline cleanup began
- **AND** generic precedence, primary error types/messages, result fields, output, redaction and shutdown bounds SHALL NOT be weakened to pass the release/join paired regression

#### Scenario: Transfer helper classification without changing stream ownership context
- **WHEN** captured helper failure participates in main cleanup
- **THEN** the adapter SHALL transfer returned FailureInfo entries into parent ActionResult.failures preserving error identity/kind and setting phase=ACTION/stream=None, without bare-error or legacy inference
- **AND** helper-local phase/stream SHALL remain available in the helper's immutable snapshot without being interpreted as main-reader context
- **AND** both capture and prepared-owner launch/setup failures SHALL use returned generic values, not error.process_result, later mutable status, repeated collection or Python-type reclassification
- **AND** public LifecycleOutcome fields and the captured return/input contracts SHALL remain unchanged, with bounded private handoff data permitted

#### Scenario: Preserve domain interruption diagnostics without a generic snapshot
- **WHEN** control flow interrupts assembly launch or the sole collection
- **THEN** the exact interruption SHALL propagate after protected cleanup without requiring generic error.process_result or an owner.result property
- **AND** the domain collector SHALL preserve its existing bounded sanitized retained_capture for CLI interruption reports
- **AND** cleanup diagnostics SHALL remain reportable through retained lifecycle/cleanup context without exposing raw generic notes or losing independent removal/staging cleanup

#### Scenario: Finalize each diagnostic stream from its retained outcome
- **WHEN** the streaming adapter receives typed ProcessStream chunks and terminal StreamOutcome notifications from the generic owner
- **THEN** it SHALL map STDOUT/STDERR to existing domain stream tags without changing public diagnostic identities
- **AND** it SHALL finalize each DiagnosticStream independently with abort=False only for EOF, and abort=True for READ_FAILED or ABORTED
- **AND** stdout reaching clean EOF before a later stderr read failure SHALL retain its clean finalization and already selected tail rather than be retroactively aborted
- **AND** prompt clean EOF finalization SHALL NOT await sibling completion
- **AND** callback failures SHALL remain distinct from generic read failures

#### Scenario: Domain adapter collects once and observes late cleanup separately
- **WHEN** an adapter obtains an OwnedProcess
- **THEN** it SHALL make at most one capture or stream attempt, without replaying raw tails into a completed collector
- **AND** a second/mixed/racing collection SHALL be rejected before callback registration, reporting or lifecycle work
- **AND** immutable stream_outcomes in the returned ProcessResult and late LifecycleResult observations through cleanup_status/wait_cleanup SHALL provide supported observations without private reader/output access
- **AND** late cleanup SHALL NOT initiate new domain sink callbacks or diagnostics

#### Scenario: Adapter supplies sanitized callback diagnostics
- **WHEN** the streaming adapter delegates chunk/end callbacks to OwnedProcess
- **THEN** ordinary callback errors SHALL be routed through a launch-injected prompt-returning reporter using the existing bounded domain-local secret/URL sanitization policy
- **AND** generic callback_errors SHALL NOT be projected into domain results or replaced with a new domain callback-error field
- **AND** ordinary reporter failure SHALL use an independent fixed-safe-message fallback without raw exception text/traceback or recursive reporting
- **AND** control-flow propagation SHALL remain with the generic coordinator, not the diagnostic reporter
- **AND** interrupted collection SHALL not be retried; no later outcome-only subscription or generic interruption snapshot SHALL be required

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

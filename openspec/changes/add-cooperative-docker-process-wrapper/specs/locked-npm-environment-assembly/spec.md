## MODIFIED Requirements

### Requirement: Own subprocess pipes across all assembly cleanup paths
Assembler streaming execution and captured cleanup clients SHALL use one unified process lifecycle for launch, internally controlled readers, and shutdown. Each stdout/stderr pipe SHALL retain one filesystem stream owner across all cleanup paths, with terminal at-most-once release and shared precedence. Consumers SHALL NOT separately adopt or close raw pipe descriptor numbers. Domain cleanup and diagnostic policy SHALL remain in assembler adapters.

#### Scenario: Preserve subprocess compatibility
- **WHEN** the Docker execution wrapper is introduced after shared process ownership
- **THEN** existing consumer signatures, process-result shapes, output tails, decoding, npm identities, controlled public errors, redaction, and configured total shutdown bounds SHALL remain unchanged
- **AND** terminal at-most-once release and shared cleanup precedence SHALL remain mandatory
- **AND** cooperative whole-container shutdown and separate unconfirmed-container reporting SHALL be authorized changes to the old terminate-CLI-first order
- **AND** cancelled-operation tails SHALL permit additional bounded final output only under unchanged decoding, sanitization and abort-finalization rules

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
- **AND** the launched child SHALL undergo the configured cooperative policy and bounded local cleanup
- **AND** no not-yet-adopted stream SHALL be stranded by failure while adopting its sibling

#### Scenario: Ownership preparation fails
- **WHEN** stream-owner synchronization or process cleanup preparation fails before execution
- **THEN** no Docker client subprocess SHALL be launched
- **AND** the failure SHALL remain controlled by the existing assembler boundary

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
- **AND** Docker removal and staging cleanup SHALL still follow the configured domain policy
- **AND** late cleanup outcomes SHALL be available internally without changing a delivered primary result, invoking user-facing domain callbacks, or emitting late public diagnostics

#### Scenario: Replace prerequisite removal ordering cooperatively
- **WHEN** assembly timeout, interruption, reader failure or executor failure requires named-container cleanup
- **THEN** the adapter SHALL use container stop/soft waiting/force removal/hard waiting/local fallback rather than the prerequisite's terminate-CLI-first order
- **AND** the prerequisite's characterized total shutdown upper bound SHALL NOT increase
- **AND** Docker control failure SHALL NOT skip coordinated child or pipe cleanup
- **AND** the generic lifecycle SHALL NOT derive container identities or contain Docker command policy

#### Scenario: Audit subprocess stream origins
- **WHEN** a subprocess factory, PIPE, stream attribute, or stream-close callable is reached through an alias
- **THEN** the convergence ledger SHALL track its ownership origin and classify the underlying close as a shared-owner internal release only after terminal synchronized ownership is proved
- **AND** it SHALL reject per-cleanup owner reconstruction, duplicate raw-fd ownership, and blanket non-descriptor exemptions for subprocess streams
- **AND** unified process ownership SHALL be proved through its internal pipe-owner/reader-stop ledger rather than accepted by class name or import alone

### Requirement: Bound assembler network and process execution
The assembler SHALL apply deterministic reviewed limits to npm registry request duration, retry count, retry delay, and total Docker-backed assembly duration. These limits SHALL be part of the fixed assembler policy identity. Exhausting npm request or retry limits SHALL produce a structured assembler failure containing bounded, redacted npm diagnostics rather than waiting indefinitely. Exceeding the constructor-owned total assembly deadline SHALL produce a timeout-specific structured operational failure. Container shutdown SHALL use separate soft and hard stage timeouts plus reserved local cleanup within the existing total shutdown bound, without restarting a timer for control commands or subsequent CLI waiting.

#### Scenario: Registry request remains unresponsive
- **WHEN** a locked package request does not complete within the reviewed npm request and retry limits
- **THEN** npm assembly SHALL terminate finitely with a structured assembler failure containing bounded, redacted npm diagnostics
- **AND** SHALL NOT require Constructor to classify the npm failure as network-specific
- **AND** SHALL NOT publish the partial environment

#### Scenario: Total assembly deadline expires
- **WHEN** the assembler container remains active beyond the reviewed total assembly deadline
- **THEN** the constructor SHALL request cooperative whole-container stop, force-remove as required and boundedly finalize the local CLI under the configured staged policy
- **AND** SHALL report an actionable timeout failure

#### Scenario: Fixed limits affect assembler identity
- **WHEN** any reviewed npm request, retry, or total execution limit changes
- **THEN** the assembler policy identity SHALL change
- **AND** an output assembled under the prior policy SHALL NOT validate as an output of the new policy

### Requirement: Clean failed assembler executions without discarding reusable cache
On timeout, cancellation, interruption, executor failure, or nonzero exit, the assembler SHALL request cooperative stop and ensure removal of its named container under the staged shutdown policy, and remove mutable staging. It SHALL preserve prior immutable published environments and the shared opaque npm download cache. Abandoned same-input staging SHALL be detected under the existing private input-identity coordination boundary and handled without adopting it as valid output. Unconfirmed daemon-side cleanup SHALL be reported separately from local resource completion through controlled diagnostics.

#### Scenario: User interrupts assembly
- **WHEN** the invoking user interrupts a running assembler container
- **THEN** container cleanup SHALL use the cooperative stop/removal policy and mutable staging SHALL be removed
- **AND** the interruption SHALL propagate to the caller with ordinary unconfirmed-cleanup failures retained secondarily

#### Scenario: Assembly fails after downloads
- **WHEN** npm downloads package bytes but assembly subsequently fails or times out
- **THEN** no partial environment SHALL be published
- **AND** safe opaque npm cache entries and prior published environments SHALL remain available

#### Scenario: Same-input staging is abandoned
- **WHEN** locked assembly encounters mutable staging left by an execution that no longer owns the coordinated operation
- **THEN** it SHALL fail closed or securely replace that staging under the input-identity lock
- **AND** SHALL NOT treat abandoned staging as evidence of a completed environment

## ADDED Requirements

### Requirement: Preserve Docker cancellation before process acquisition
The Docker operation SHALL exist before process acquisition and retain irreversible cancellation. Launch acceptance and cancellation SHALL be atomic. Cancellation winning that transition SHALL prevent process acquisition; cancellation after acceptance SHALL remain pending until owner handoff or failed-launch cleanup observation. This SHALL NOT require a generic two-phase launch API or a second local shutdown coordinator.

#### Scenario: Shutdown precedes wrapper launch
- **WHEN** shutdown is requested before the wrapper accepts launch
- **THEN** cancellation SHALL be retained and subsequent launch SHALL NOT call the generic launch entry point or any subprocess factory
- **AND** no main/control pipe or Docker stop/removal client SHALL be acquired
- **AND** any already-prepared wrapper resources SHALL receive independent cleanup
- **AND** the operation SHALL be reported as cancelled before launch, not successfully executed or a confirmed stopped container

#### Scenario: Shutdown races with accepted launch
- **WHEN** wrapper launch acceptance wins the atomic race and shutdown arrives while generic launch is in flight
- **THEN** the wrapper SHALL retain the request and forward it once when the owner becomes available
- **AND** it SHALL NOT hold its state lock across generic launch, factory execution, readiness waits or callbacks
- **AND** it SHALL NOT independently close pipes or wait/reap the main child
- **AND** an unresolved launch SHALL remain explicitly pending rather than cleanup complete

#### Scenario: Cancelled or in-flight launch is requested again
- **WHEN** launch or shutdown is requested repeatedly or concurrently on the same wrapper
- **THEN** cancellation SHALL NOT be cleared and no second main owner SHALL be acquired
- **AND** an accepted launch that fails SHALL expose/retain its generic cleanup observation where applicable, without acquisition retry or reconstructed release authority
- **AND** bounded observation of unresolved launch SHALL NOT claim terminal cleanup or guarantee forcible completion of an arbitrary blocked factory

### Requirement: Stop attached Docker containers through composition
Docker execution SHALL compose a domain operation and launch-configured cooperative stop policy with the generic process owner. The policy SHALL target the whole attached docker run container, not a PID. It SHALL request docker stop, await natural local CLI completion and ensure removal through docker rm -f, using force removal as fallback. Local CLI signalling SHALL follow hard-stage control failure or expiry, not soft-stage expiry alone.

#### Scenario: Container stops cooperatively
- **WHEN** cancellation requests stop for the identified container and the bounded docker stop operation succeeds
- **THEN** the wrapper SHALL await natural local Docker CLI completion and delegate reaping to its generic owner without first signalling it
- **AND** supported readers SHALL retain bounded final stdout/stderr under active abort redaction
- **AND** the domain policy SHALL ensure final removal or report unconfirmed cleanup, treating characterized already-absent evidence idempotently only when creation for this execution is known to be settled and the evidence is causally subsequent

#### Scenario: Soft stop fails or its CLI waiting expires
- **WHEN** Docker stop fails nonretryably, exhausts its permitted attempts, or its request/natural CLI waiting reaches the soft-stage timeout
- **THEN** the policy SHALL enter the once-only docker rm -f hard stage with its distinct timeout
- **AND** successful force removal SHALL be followed by natural local CLI waiting within the remaining hard-stage allowance before local signalling
- **AND** soft-stage failure/expiry alone SHALL NOT trigger immediate local terminate/kill

#### Scenario: Hard removal fails or its CLI waiting expires
- **WHEN** Docker force removal fails nonretryably, exhausts its permitted attempts, or hard-stage command/natural CLI waiting reaches its timeout
- **THEN** the generic owner SHALL perform bounded local terminate/kill/reap using the reserved local-cleanup allowance
- **AND** removal failure SHALL NOT skip local cleanup, reader-stop confirmation or safe independent releases
- **AND** nonretryable failure or exhausted attempts SHALL advance immediately rather than consume the rest of its stage by sleeping

#### Scenario: Distinct stage timeouts include command execution
- **WHEN** the configured soft or hard Docker stage starts
- **THEN** its timer SHALL begin before CID observation, recovery discovery or control-client initiation as applicable, and cover those actions plus subsequent original CLI waiting
- **AND** daemon-side Docker stop grace SHALL be distinguished from the soft-stage timeout
- **AND** soft plus hard allowances plus local-cleanup reserve SHALL fit the total supported bound and SHALL be validated before main launch
- **AND** no helper, post-command wait or concurrent caller SHALL reset those timers
- **AND** if soft stop and CLI exit already completed, final removal SHALL remain bounded without an unnecessary full hard-stage CLI wait

#### Scenario: Autonomous triggers and repeated callers share control requests
- **WHEN** deadline, context exit, setup/reader failure or explicit cancellation initiates or joins Docker shutdown
- **THEN** the policy selected before main launch SHALL cover that trigger through the same coordinator
- **AND** each logical stop/removal stage SHALL be entered once, with shared bounded sequential attempt counters rather than one command attempt per caller
- **AND** helper Docker clients SHALL have separate generic owners with default local policies and stage/parent-capped remaining budgets
- **AND** the wrapper SHALL NOT override generic shutdown or independently close, join or reap the main CLI

### Requirement: Observe creation with a bounded private CID file
Attached Docker execution SHALL use a fresh private --cidfile and accept only a complete validated ID. Cancellation without an ID SHALL observe it for at most 10 seconds within existing phase/total budgets, preserving recovery and local-cleanup time. Retry delays SHALL begin at 10 ms, double, apply ±10% jitter and be clamped to the deadline. Container output SHALL NOT be parsed as identity.

#### Scenario: CID arrives during cancellation
- **WHEN** a missing, empty or partially written CID file becomes a complete valid ID before the observation deadline
- **THEN** pending cancellation SHALL target that exact ID through the shared staged policy
- **AND** the wrapper SHALL distinguish creation acknowledgement from startup and stop confirmation
- **AND** CID observation SHALL NOT block independent coordinator progress, require a new full grace or retry main acquisition

#### Scenario: CID observation expires
- **WHEN** no valid ID is available at min(10 seconds, remaining soft-stage allowance, available total allowance)
- **THEN** observation SHALL end and hard-stage scoped discovery SHALL use the retained recovery allowance
- **AND** no stale file, PID or container stdout SHALL substitute for trustworthy identity
- **AND** private CID resources SHALL follow existing staging cleanup without a new persistent schema

### Requirement: Retry control and recover lost containers within existing budgets
Logical shutdown stages SHALL be shared and entered once. Classified transient launch/daemon failures SHALL permit at most three sequential attempts per command/exact ID, including the initial attempt. Discovery SHALL have the same attempt bound. Retries SHALL use 10 ms doubling delays with ±10% jitter inside existing deadlines. Labels SHALL scope recovery to managed status, controller PID and a unique operation token.

#### Scenario: Transient failure or lost response
- **WHEN** a control attempt cannot launch transiently or encounters a transient daemon communication failure or lost response
- **THEN** another attempt SHALL require remaining attempt and phase/total allowance and settlement of the previous helper's local cleanup
- **AND** each acquired helper SHALL retain its own default-policy process/stream owners
- **AND** concurrent callers and recovery paths SHALL share counters for the same command/target
- **AND** interruptions, unexpected defects, permanent failures and exhausted deadlines SHALL NOT be retried
- **AND** successful or characterized already-absent removal of a known created ID SHALL NOT require another attempt

#### Scenario: Recover identity after the CID timeout
- **WHEN** cancellation cannot obtain trustworthy identity within its bounded CID observation
- **THEN** the policy SHALL list all containers including created/stopped ones using all exact filters io.docker-constructor.managed=true, io.docker-constructor.controller-pid=<current PID> and io.docker-constructor.operation-id=<fresh unique token>
- **AND** it SHALL remove only validated full IDs returned by that scoped discovery within the hard-stage/total deadline, retaining local-cleanup time
- **AND** unrelated operations or reused PIDs SHALL NOT authorize deletion
- **AND** discovery/removal failure or possible creation after the sweep SHALL leave cleanup unconfirmed
- **AND** no retry, discovery or removal SHALL reset clocks or begin after terminal delivery

### Requirement: Distinguish local cleanup from confirmed container cleanup
The adapter SHALL retain separate local-resource completion and daemon-side container stop/removal outcomes. Local CLI exit SHALL NOT prove container cleanup. Daemon unavailability or unconfirmed cleanup SHALL produce a bounded sanitized controlled domain failure through existing consumer result/error shapes. Active operation failures and interruptions SHALL retain shared precedence with cleanup diagnostics secondary where applicable.

#### Scenario: CLI is cleaned up but the daemon cannot confirm stop
- **WHEN** the local child/readers/pipes are cleaned up but container stop/removal cannot be confirmed
- **THEN** local cleanup SHALL be independently observable as complete while container cleanup remains unconfirmed
- **AND** the adapter SHALL report a controlled sanitized container-cleanup failure, not successful remote termination
- **AND** an active operation failure or interruption SHALL retain its exact authoritative identity over ordinary control failures with the unconfirmed diagnostic retained secondarily
- **AND** staging cleanup SHALL still run

#### Scenario: Cancellation precedes daemon-side container creation
- **WHEN** cancellation arrives after CLI launch while daemon creation or its acknowledgement remains pending
- **THEN** the wrapper SHALL boundedly observe its private CID file, then recover by exact labels if no trustworthy ID is available
- **AND** early absent observations, local CLI exit/reaping, elapsed time and an empty recovery list SHALL NOT prove settlement of a pending creation request
- **AND** successful removal of the known created container ID SHALL establish removal of that object even if ContainerStart races with removal
- **AND** stop success before ContainerStart SHALL NOT alone establish cancellation
- **AND** insufficient causal evidence within the existing budget SHALL yield unconfirmed cleanup with bounded sanitized diagnostics, unchanged failure precedence and staging cleanup
- **AND** delayed creation/evidence after delivery SHALL NOT initiate new requests or mutate delivered outcomes

#### Scenario: Container-control result arrives after delivery
- **WHEN** an already-started Docker control command finishes after the shutdown result/error was delivered
- **THEN** retained internal observation SHALL expose its late outcome
- **AND** the delivered local/container outcome SHALL NOT be mutated or retroactively reported as success
- **AND** no new domain stop/removal request, public output or user-facing callback SHALL begin after delivery

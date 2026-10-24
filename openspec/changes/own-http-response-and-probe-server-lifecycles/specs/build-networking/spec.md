# Spec Delta

## ADDED Requirements

### Requirement: Own the isolated probe across startup failure
Gateway diagnosis SHALL launch its temporary probe as one explicitly owned direct child process and wait at most ten seconds for validated readiness. Any subsequent startup-failure cleanup SHALL use the separate bounded teardown budget. Every acquired child and parent-side communication resource SHALL remain accounted for on partial startup, failure and interruption; no gateway request SHALL precede readiness.

#### Scenario: Child acquisition fails
- **WHEN** preparing or launching the child fails before a child is acquired
- **THEN** no gateway request SHALL use that probe
- **AND** no nonexistent child SHALL be signalled or reaped and no fabricated pipe SHALL be released

#### Scenario: Startup fails after child acquisition
- **WHEN** listener binding, readiness communication or parent-side setup fails after launch
- **THEN** diagnosis SHALL report startup failure and request cleanup of the actual acquired child through the same owner
- **AND** all independently safe parent-side cleanup SHALL still be attempted under shared failure precedence

#### Scenario: Readiness is absent or invalid
- **WHEN** the child exits before readiness, readiness exceeds ten seconds, or the readiness message is malformed, oversized, duplicated or contains an invalid port
- **THEN** startup SHALL fail without issuing a gateway request
- **AND** the acquired child and communication resources SHALL still be cleaned up

### Requirement: Limit the probe to one diagnostic lifecycle
One controlling caller SHALL manage one probe instance for the full gateway-candidate sequence. Each instance SHALL support at most one start attempt. Parallel probes, restart and concurrent or reentrant probe lifecycle calls are outside the supported contract; sequential repeated stop SHALL remain safe.

#### Scenario: Multiple gateway candidates are checked
- **WHEN** a diagnosis checks several gateway candidates sequentially
- **THEN** the same temporary probe child SHALL remain available for the entire sequence
- **AND** the first HTTP request SHALL NOT automatically end its lifetime

#### Scenario: Start is requested again
- **WHEN** another start is requested on an instance whose start was already attempted, whether active, failed or stopped
- **THEN** it SHALL reject the request before acquiring another child
- **AND** SHALL NOT overwrite existing cleanup responsibility

### Requirement: Attempt bounded best-effort probe teardown
After diagnosis completes, fails or is interrupted, the parent SHALL request SIGKILL for a still-live probe child without SIGTERM or graceful draining and attempt reap/communication cleanup within one five-second budget. Ordinary teardown failure or incompleteness SHALL NOT invalidate completed gateway checks. Generic owner cleanup SHALL remain honestly incomplete until its completion conditions are met.

#### Scenario: Ordinary stop succeeds
- **WHEN** gateway checks finish and the parent stops its still-live probe
- **THEN** SIGKILL SHALL be requested through the child's sole owner without a terminate grace period
- **AND** the parent SHALL attempt reap and parent-side resource settlement within the same cleanup budget, reaping an already exited child without a redundant signal
- **AND** confirmed child exit SHALL establish listener/connection reclamation, while incomplete cleanup SHALL follow the warning/retention contract
- **AND** the expected kill return code SHALL NOT alone turn successful checks into a diagnosis failure
- **AND** subsequent sequential stop SHALL NOT restart signalling, waiting or release, obtain another budget or emit a repeated warning

#### Scenario: A request is stalled
- **WHEN** stop is requested while the probe is idle, parsing a silent/incomplete/trickling request or writing a response
- **THEN** parent termination SHALL NOT depend on a cooperative server-loop exit or successful delivery of the response
- **AND** the parent's foreground cleanup attempt SHALL remain bounded even if actual child exit or communication settlement cannot be confirmed

#### Scenario: Cleanup fails or is interrupted
- **WHEN** kill, waiting or parent communication cleanup fails or is interrupted
- **THEN** every independently safe cleanup action SHALL still be attempted under shared precedence
- **AND** first interruption, unexpected defect, original ordinary operation failure and ordinary cleanup failure SHALL retain their precedence and exact selected identity before the networking domain projection
- **AND** ordinary cleanup-only failures SHALL be projected to the warning contract rather than discard completed checks
- **AND** active diagnosis failure SHALL remain authoritative over ordinary stop failure
- **AND** cleanup interruptions and unexpected defects SHALL NOT be downgraded to nonfatal warnings

#### Scenario: Completion cannot be confirmed
- **WHEN** reap or parent-side resource settlement cannot be confirmed within the cleanup budget
- **THEN** the foreground caller SHALL stop waiting without requiring watchdog expiry or final resource settlement
- **AND** completed gateway checks SHALL still produce their unchanged diagnosis result, with at most one fixed-size sanitized cleanup warning submission
- **AND** any active startup/check failure SHALL remain a failure with ordinary cleanup failures secondary
- **AND** the same owners, collector references and existing generic continuation SHALL retain responsibility without GC dependence, new budgets, independent signalling/reaping or ambiguous-close retries
- **AND** the owner SHALL NOT claim complete cleanup merely because the diagnosis was returned
- **AND** SIGKILL SHALL NOT be represented as an unconditional prompt-exit guarantee for uninterruptible kernel waits

#### Scenario: Probe exits before requested teardown
- **WHEN** a child exit is observed before the parent requests teardown, including during gateway checks
- **THEN** diagnosis SHALL fail rather than silently publish a partial successful result
- **AND** remaining cleanup SHALL still attempt reap and account for parent communication resources under the same best-effort warning/retention rules

### Requirement: Report probe cleanup uncertainty without changing completed results
Ordinary teardown failure or incompleteness SHALL trigger at most one fixed WARNING log submission per lifecycle by the controlling diagnosis thread before return or failure propagation, without changing public signatures or DTO fields. Submission SHALL be best effort: reporting Exception failures SHALL NOT alter the diagnosis. Background cleanup SHALL NOT present warnings or mutate delivered outcomes.

#### Scenario: Checks finish but ordinary cleanup fails
- **WHEN** checks have completed and ordinary kill, wait or resource-release failure prevents clean teardown
- **THEN** the diagnosis SHALL retain its gateway results and selected address
- **AND** one fixed-size cleanup record SHALL be submitted without raw exceptions, child output, URLs or tokens and without direct writes to probe stdout/readiness
- **AND** the warning attempt SHALL be marked before submission, preventing retry after a reporting failure or repeated stop
- **AND** ordinary failure to deliver the warning SHALL NOT discard the diagnosis
- **AND** sequential repeated stop or late cleanup SHALL NOT duplicate the warning or revise the delivered result

#### Scenario: Warning delivery fails or is disabled
- **WHEN** warning lookup/submission raises an Exception or application logging suppresses the record
- **THEN** the selected diagnosis result or pending failure SHALL remain unchanged without warning retries or fallback output
- **AND** reporting control-flow interruptions SHALL NOT be swallowed and SHALL follow shared first-interruption precedence
- **AND** no handler, queue, reporting thread or public sink parameter SHALL be introduced by networking

#### Scenario: Application logging handler blocks
- **WHEN** application-configured synchronous logging takes longer than the cleanup observation budget
- **THEN** only warning size and attempt count SHALL be described as bounded, not handler execution time
- **AND** the five-second budget SHALL apply to process cleanup observation, not a hard total diagnosis-return deadline including arbitrary logging handlers
- **AND** networking SHALL NOT create asynchronous warning delivery; application logging configuration SHALL remain responsible for whether, where and when an already submitted record is displayed

#### Scenario: Cleanup settles after the foreground call
- **WHEN** a retained collector or continuation finishes after diagnosis return or failure propagation
- **THEN** it SHALL retain internal cleanup observations without a warning callback or presentation sink
- **AND** it SHALL NOT submit new domain cleanup logs or present CLI output, mutate delivered DTOs/errors or claim generic settlement before its actual completion conditions are met

### Requirement: Limit idle and accepted-connection inactivity
The probe SHALL limit listener/idle waits to one second and explicitly set a three-second read/write inactivity timeout on each accepted connection before HTTP request I/O. An idle or request inactivity timeout SHALL NOT end the whole probe lifecycle. These timeouts SHALL NOT be represented as an absolute request or process lifetime; the independent watchdog and external kill remain required.

#### Scenario: No client connects
- **WHEN** the listener's idle wait times out
- **THEN** the probe SHALL remain available for later gateway-candidate requests without resetting its watchdog

#### Scenario: An accepted client stalls
- **WHEN** a connected client sends no further request data or stops reading the response
- **THEN** the connection's blocking network I/O SHALL use its own three-second inactivity timeout rather than inherit an assumed listener timeout
- **AND** timed-out request resources SHALL be released through their existing owner while the probe remains available to later clients

#### Scenario: A client trickles data
- **WHEN** a client sends data often enough to avoid inactivity expiry
- **THEN** the probe SHALL NOT claim that the three-second timeout bounds the total request
- **AND** the absolute watchdog deadline SHALL remain unchanged and external SIGKILL SHALL remain the primary stop mechanism

#### Scenario: Configured client timeout exceeds server inactivity limit
- **WHEN** a probe uses a client timeout above three seconds within its unchanged configurable 1–300-second range
- **THEN** the fixed three-second accepted-connection read/write inactivity limit SHALL remain independent of, and may expire before, that client timeout
- **AND** a live request lasting more than three seconds SHALL still be eligible to succeed when individual inactivity gaps stay below three seconds
- **AND** a server-observed gap exceeding three seconds SHALL end that accepted connection even if the client timeout is longer, while the probe remains available for a subsequent request
- **AND** this intentional slow-client restriction SHALL NOT be represented as complete preservation of previous slow-request behavior or a total candidate deadline
- **AND** existing client retry behavior SHALL remain unchanged; connection closure SHALL NOT imply that the entire candidate immediately ends

### Requirement: Expire a probe whose parent does not stop it
The probe child SHALL arm a backup watchdog before listener acquisition with one absolute monotonic lifetime of 1200 seconds. Expiry SHALL terminate the whole child independently of parent cleanup requests; requests and readiness SHALL NOT extend the deadline. This backup SHALL NOT replace external parent termination or promise execution when the entire child cannot be scheduled.

#### Scenario: Parent cleanup does not happen
- **WHEN** the watchdog deadline expires without parental stop
- **THEN** the watchdog SHALL terminate the whole child, not only its own thread
- **AND** actual child exit SHALL allow the operating system to reclaim its listener and connections without Python finalizers

#### Scenario: Watchdog expires during diagnosis
- **WHEN** expiry is observed while diagnosis is still using the probe
- **THEN** diagnosis SHALL report failure and attempt remaining parent-side cleanup under the bounded best-effort contract
- **AND** expiry SHALL NOT be counted as a successful gateway result or expected parental teardown
- **AND** the maximum lifetime SHALL NOT be claimed to bound arbitrary Docker image/startup delays

### Requirement: Preserve process ownership and existing stop defaults
Probe process and captured-pipe management SHALL use the shared process owner with immediate kill selected at launch. The new local stop mode SHALL be opt-in; existing consumers SHALL retain terminate-then-kill defaults. All modes SHALL retain shared shutdown budgets, safe pipe release, failure precedence and incomplete-cleanup responsibility, without independent borrowed-child control.

#### Scenario: Immediate kill is selected
- **WHEN** the probe's launch-selected local stop policy is exercised by shutdown, cancellation, setup failure or execution deadline
- **THEN** the shared owner SHALL use immediate kill rather than a terminate-first path
- **AND** the parent SHALL NOT independently signal, wait for, reap or release the owner's borrowed child or pipes

#### Scenario: Existing callers do not select the new mode
- **WHEN** an existing process consumer launches without selecting immediate kill
- **THEN** its signal order, configured stages, budgets, result fields and failure behavior SHALL remain unchanged
- **AND** invalid mode selection SHALL be rejected before child acquisition

### Requirement: Preserve gateway diagnosis policy during lifecycle correction
Probe isolation SHALL preserve the networking service API, token response, bind policy, gateway ordering, Docker-mode detection, probe commands, persistence boundaries and normal diagnostics. HTTP/probe/watchdog policy SHALL remain outside the filesystem and generic process foundation. Image builds and non-gateway host-access modes SHALL NOT gain diagnosis side effects.

#### Scenario: Existing gateway behavior is retained
- **WHEN** successful or failed gateway diagnosis uses the isolated probe
- **THEN** candidate order, token exchange, selected address and persistence SHALL remain unchanged except the specified startup/lifetime failures, fixed accepted-connection inactivity restriction and nonfatal ordinary cleanup warnings
- **AND** real loopback tests SHALL verify readiness, repeated token requests, forced exit/reap and resource release without Docker or an external network

#### Scenario: Compatible injected probe has no health observation
- **WHEN** a test or embedding caller injects a compatible probe exposing only port, token, start and stop
- **THEN** diagnosis SHALL preserve those interactions and its public factory parameter/DTO fields without requiring production process state
- **AND** absent optional health observation SHALL mean unknown, not confirmed health, a fabricated failure or a cleanup warning

#### Scenario: Injected probe reports a known health failure
- **WHEN** a health-capable injected probe reports a known failure at a pre-candidate, post-candidate or pre-teardown observation
- **THEN** diagnosis SHALL fail rather than return partial success and SHALL still attempt bounded teardown
- **AND** an optional health attribute that is present but non-callable SHALL be rejected as an invalid injection rather than treated as absent
- **AND** no pre-teardown health observation SHALL be repeated after the parent has requested its expected kill

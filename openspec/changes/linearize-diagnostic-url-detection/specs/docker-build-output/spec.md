# Spec Delta

## MODIFIED Requirements

### Requirement: Integrate host progress through typed presentation-neutral events
Coalescing, refresh, and finalization scenarios in this requirement describe healthy presentation; overload or presentation failure MAY cause safe repeated groups or unavailable display as specified by the degradation scenarios below. This qualification SHALL NOT weaken event safety, bounded capture, authoritative activity facts, or primary-result isolation.

The constructor SHALL convey host-materialization lifecycle and live assembler diagnostics through immutable typed event interfaces between the facade, build orchestration, Pi materialization, artifact acquisition, and assembler execution. Existing lifecycle events SHALL retain fixed phase and started/succeeded/failed classifications. A separate operational activity event hierarchy SHALL represent step transitions, observed transport progress, diagnostics, and heartbeats without changing lifecycle-event semantics. Each step start SHALL carry a closed declaration of whether that operation expects a diagnostic stream, and heartbeat facts SHALL omit diagnostic-silence duration when it does not.

Before materialization starts, the facade SHALL fix one `network_url_display` value for the operation and propagate the same typed enum through host materialization, Pi assembly, and assembler execution requests into collector configuration before collection starts. The collection boundary SHALL derive terminal-safe source and safe projected representations only as needed for the selected local mode and external delivery, while feeding exactly one bounded retained tail with the selected local representation. Non-selected local representations and transient external SDK representations SHALL NOT populate another retained tail.

External structured diagnostic events SHALL identify their fixed phase, step, stdout/stderr source, closed classification, optional safe logical resource, already-redacted URL-free text, zero or more normalized hostnames from which every other URL component has already been removed, and an ordered tuple of ephemeral session-keyed opaque URL fingerprints for safe presentation identity. External SDK events SHALL always use that safe representation. The facade SHALL use the same immutable policy for local live presentation and the single retained representation used by text and JSON failure formatting; this selection SHALL NOT alter activity observation, lifecycle facts, evidence, or semantic identities.

Activity events SHALL use an activity-independent monotonic heartbeat cadence beginning at 3 seconds with subsequent intervals no greater than 1 second and a separate monotonic diagnostic-silence duration reset only by stdout/stderr and exposed exactly from 120 seconds, SHALL carry optional closed last-activity kind and whole monotonic age in seconds together only after diagnostic or transport-progress activity is at least one second old, SHALL reject last-activity ages below one second, SHALL expose no wall-clock activity timestamp, and SHALL carry a remaining deadline only when one exists.

The facade SHALL own output-mode selection and rendering; domain modules SHALL NOT print directly or depend on terminal presentation details. The sink SHALL remain optional for SDK and injected callers, and a sink failure SHALL NOT replace or change the build result.

The internal facade path SHALL use one bounded ordered inbox and one presentation actor, with independently bounded admission budgets for control and telemetry. Control capacity SHALL be the fixed 1024-event `CONTROL_CAPACITY` hard safety limit with substantial headroom, not a calculation from the exact current build transcript or module inventory; exact supported event order and multiplicity SHALL be tested independently.

Short mutex-protected admission is permitted; producers SHALL NOT wait for queue capacity, rendering, I/O, or consumer acknowledgement. On the internal enqueue path, no rendering, flushing, arbitrary callback invocation, completion wait, or join SHALL occur under inbox or producer serialization locks. External SDK callback serialization and isolation SHALL retain their existing contract.

Diagnostic saturation SHALL NOT consume control capacity. Control events SHALL preserve admission order; accepted diagnostic events SHALL preserve that order unless explicitly discarded on presentation failure. Optional supersession of replaceable telemetry SHALL NOT move a newer observation before intervening control events or cross an operation terminal/restart boundary.

Orchestration SHALL finish diagnostic readers and sanitizer finalization, drain any upstream dispatcher, and stop/join heartbeat production before admitting the corresponding operation terminal event. Session close SHALL atomically end acceptance and wake the actor independently of queue capacity; it SHALL NOT require admission of a shutdown message. Normal completion SHALL drain accepted events, finalize display, signal completion, and join the worker before BuildKit or return. Control-admission failure SHALL explicitly disable presentation and wake its consumer without waiting under producer locks; it SHALL NOT silently discard control as ordinary telemetry or change the primary result.

All presentation completion waits and joins SHALL share one five-second monotonic budget, which repeated cleanup calls SHALL NOT restart. Renderer exceptions SHALL disable further output and discard pending display state while allowing completion. If terminal I/O stalls beyond the budget, the facade SHALL cancel further presentation and continue without an unbounded wait/join or synchronous retry to the failed stream. A daemon worker and an already-started write MAY outlive that degraded return; after unblocking, the worker SHALL check cancellation before any further renderer operation. No clean native-output handoff, final-message visibility, or worker-termination guarantee applies to indefinitely blocked I/O. Healthy and returning-error paths SHALL still terminate the worker, and no rendering SHALL occur after successful completion acknowledgement.

During a live session the actor SHALL be the sole host terminal writer, including the facade-prepared final host failure report. The normal CLI result printer SHALL NOT write that report a second time or retry it after presentation failure. Structured result data SHALL remain available independently of rendering. Without a live session, ordinary text/JSON result formatting SHALL remain responsible for the report; JSON SHALL create no presentation inbox or worker.

External hostname and URL-fingerprint facts SHALL derive only from safely validated known-scheme URLs as classified by `locked-npm-environment-assembly`. Unknown-scheme candidates SHALL remain hidden without metadata and SHALL NOT independently distinguish otherwise equal diagnostic groups.

#### Scenario: Text output is noninteractive
- **WHEN** text output is redirected or otherwise runs without interactive presentation
- **AND** `lines` was not explicitly configured
- **THEN** the facade SHALL create no live progress or diagnostic sink
- **AND** failures SHALL retain bounded diagnostics under the selected network URL display policy without direct domain output

#### Scenario: Host phase completes or fails
- **WHEN** a host-materialization phase starts
- **THEN** its started event SHALL be observable before its potentially blocking work
- **AND** heartbeat production SHALL atomically enter terminal state, reject later activity, and stop/join before exactly one matching succeeded or failed event is delivered
- **AND** no heartbeat SHALL be delivered after that terminal event
- **AND** operational events SHALL NOT replace or alter that lifecycle pair
- **AND** no later host phase or Docker build SHALL start after a failed event

#### Scenario: Hostname policy is presentation-only
- **WHEN** the same collected diagnostic is presented under different local `network_url_display` modes
- **THEN** external SDK facades SHALL receive identical sanitized URL-free text and normalized-host facts
- **AND** internal CLI presentation MAY select redacted, host-path, or terminal-safe source representations
- **AND** activity observation, lifecycle facts, evidence, and semantic identities SHALL remain identical

#### Scenario: Diagnostic interrupts transient progress
- **WHEN** a diagnostic of any closed classification arrives while healthy interactive transient progress is active
- **THEN** the facade SHALL make its first admitted occurrence immediately visible only in one mutable diagnostic slot, without a durable write
- **AND** identical admitted repeats SHALL update only that slot with the latest admitted-occurrence count on the next TUI refresh
- **AND** an otherwise identical diagnostic whose one changed field is a single strict maximal numeric token as defined by `locked-npm-environment-assembly` SHALL replace that slot without a repetition suffix and reset the exact-repeat count
- **AND** a signed, incomplete, malformed dot-separated, or identifier-embedded numeric-looking sequence SHALL NOT be matched by substring and SHALL instead finalize the current group
- **AND** diagnostic activity SHALL already have been observed before mailbox admission and presentation grouping
- **AND** warning or error classification SHALL NOT bypass the mutable slot or force an immediate durable write
- **AND** a different diagnostic or terminal event SHALL atomically freeze the latest admitted-occurrence count, clear the transient region, perform exactly one durable write of the finalized diagnostic, clear the slot, and restore only still-applicable transient status
- **AND** the different or terminal event SHALL render only after that ordered durable finalization
- **AND** concurrent activity SHALL NOT interleave terminal fragments, duplicate the durable diagnostic, or lose the latest admitted-occurrence count

#### Scenario: Hidden URL identity is presentation-safe
- **WHEN** two ordinary diagnostics outside a recognized successful-fetch group render the same URL-free safe text but their ordered hidden known-scheme URL identities differ
- **THEN** exact-repeat and interactive numeric-update presentation SHALL treat them as different groups
- **AND** recognized successful-fetch grouping SHALL instead use the selected `redacted` or `host-path` group key
- **AND** normalized hostnames SHALL remain available only for facade hostname-display policy rather than as an additional post-render coalescing-key field
- **AND** no URL fingerprint or session key SHALL be rendered, persisted, retained in a failure tail, included in evidence, or reused across presentation sessions

#### Scenario: Numeric updates remain durable in line mode
- **WHEN** `redacted` or `host-path` `lines` presentation receives otherwise identical diagnostics outside a recognized successful-fetch group whose one changed field is a numeric token
- **THEN** every changed numeric value SHALL remain a separate durable line
- **AND** only fully identical diagnostics SHALL use the existing exact-repeat window and canonical suffix

#### Scenario: Diagnostic presentation capacity is exhausted
- **WHEN** diagnostic production exhausts the bounded best-effort presentation capacity
- **THEN** further ordinary diagnostics MAY be dropped without blocking the producer
- **AND** lifecycle, step-transition, terminal, timeout, cancellation, and final-report events SHALL remain admissible within their independently reserved protocol bound
- **AND** the facade SHALL preserve ordering across all admitted events
- **AND** a repetition suffix SHALL count only admitted occurrences and SHALL NOT claim an exact producer-side total
- **AND** functioning presentation SHALL present a bounded non-coalesced omission notice with a subsequent admitted diagnostic/control event or normal close
- **AND** exact counts SHALL be shown only when known; saturation or uncertainty SHALL use a lower bound or generic omission notice
- **AND** presentation SHALL NOT claim to reconstruct every lost occurrence or an exact original transcript

#### Scenario: Injected caller omits presentation
- **WHEN** an SDK or injected materializer or executor does not provide the optional event sink
- **THEN** its existing non-presenting behavior SHALL remain compatible
- **AND** domain execution SHALL NOT print directly

#### Scenario: Supported transcripts fit independently of the hard limit
- **WHEN** a healthy supported build produces its realistic success transcript or a supported failure transcript including the facade-owned final report while the presentation consumer is stalled
- **THEN** every control event SHALL fit within the fixed 1024-event capacity
- **AND** the expected transcript SHALL be constructed and asserted independently of `CONTROL_CAPACITY`
- **AND** adding, removing, or replacing supported operations SHALL NOT require changing the hard limit while the resulting transcript remains within it

#### Scenario: Control admission reaches the hard safety limit
- **WHEN** all 1024 control slots are occupied and another lifecycle, terminal, or final-report event is admitted
- **THEN** the adapter SHALL explicitly disable presentation and wake the actor without consuming another queue slot
- **AND** producers SHALL NOT wait for output, acknowledgement, or worker termination
- **AND** close SHALL remain available and no acknowledgement of the unadmitted event SHALL be awaited
- **AND** completion waiting SHALL stay within the shared five-second budget
- **AND** the exhaustion SHALL be treated as runaway or unsupported control production
- **AND** it SHALL NOT mask or change success, timeout, interruption, assembler failure, or Docker result

#### Scenario: Full inbox closes normally
- **WHEN** all producers have finished and the presentation inbox is full
- **THEN** close SHALL end acceptance and wake the consumer without requiring a free slot
- **AND** events accepted before close SHALL drain in order on the healthy path
- **AND** later admissions SHALL be rejected and repeated close SHALL be idempotent
- **AND** pending diagnostics and omission information SHALL finalize before completion acknowledgement

#### Scenario: Presentation sink fails
- **WHEN** an injected presentation sink raises or the internal presentation path fails
- **THEN** the failure SHALL remain secondary to the primary operation result
- **AND** external SDK callback isolation and optional-sink behavior SHALL remain compatible
- **AND** internal producer callbacks SHALL NOT render or wait for presentation completion under serialization locks
- **AND** presentation completion waiting SHALL use only the shared five-second budget, with no wait for an unadmitted event

#### Scenario: Renderer raises an exception
- **WHEN** the presentation renderer raises while writing host output
- **THEN** the actor SHALL disable subsequent rendering and discard pending display state without retrying the failed stream
- **AND** it SHALL continue consuming or discarding accepted events so normal close can complete
- **AND** the primary result and bounded retained diagnostics SHALL remain unchanged

#### Scenario: Renderer stalls indefinitely
- **WHEN** a host terminal write or flush remains blocked while the build continues or completes
- **THEN** producers SHALL continue independently and presentation memory SHALL remain bounded
- **AND** the facade SHALL spend at most one shared five-second budget on presentation completion, including repeated cleanup calls
- **AND** on expiry it SHALL cancel further presentation and continue without an unbounded join or synchronous fallback write
- **AND** an already-started write MAY finish later, but no subsequent renderer operation SHALL begin after cancellation is observed
- **AND** worker absence and clean terminal handoff SHALL NOT be required on this degraded path

#### Scenario: Final host failure report has one writer
- **WHEN** host execution fails during an authorized live session
- **THEN** the facade SHALL submit its safe final failure report to the actor before close
- **AND** the normal CLI result printer SHALL NOT repeat that report
- **AND** retained diagnostics MAY repeat earlier live output with an explicit retained-context warning
- **AND** a failed report admission or rendering attempt SHALL preserve the primary structured result without synchronous retry to the failed stream

#### Scenario: Transient diagnostic exceeds terminal width
- **WHEN** a healthy interactive renderer receives a diagnostic wider than the terminal, including wide Unicode characters
- **THEN** transient output SHALL fit within one physical row without automatic wrapping
- **AND** replacement, finalization, and normal BuildKit handoff SHALL leave no stale transient rows
- **AND** durable diagnostic text SHALL remain complete

#### Scenario: Delivered heartbeats do not determine execution timeout
- **WHEN** presentation drops diagnostics, supersedes heartbeats, or processes a backlog of consecutive heartbeats
- **THEN** diagnostic silence SHALL still derive from collector-observed stdout/stderr activity and authoritative monotonic heartbeat facts
- **AND** presentation SHALL NOT infer or trigger execution timeout from consumed heartbeat counts
- **AND** the existing executor-owned total deadline SHALL remain unchanged

#### Scenario: SDK caller receives diagnostics under a revealing local mode
- **WHEN** an SDK or injected caller supplies an external event sink while local output selects `host-path` or `exact`
- **THEN** structured diagnostic events delivered to that sink SHALL remain redacted and URL-free
- **AND** the local display policy SHALL NOT expose original URL content through the SDK event contract

### Requirement: Apply one safe network diagnostic presentation policy
Host-pipeline live and failure diagnostics SHALL identify reviewed resources by safe logical asset names and MAY identify named assembler containers by the fixed `npm-assembler-<digest-prefix>` form. The closed `network_url_display` policy SHALL control additional network detail. `redacted` SHALL omit source URL detail. `host-path` MAY expose only a normalized hostname and safe path and SHALL omit scheme, port, user information, query, fragment, proxy details, credentials, and exception messages. `exact` MAY preserve terminal-safe source diagnostic content only where such third-party content was captured under the contracts of `locked-npm-environment-assembly`; it SHALL NOT synthesize unavailable source content or expose arbitrary transport exception messages. Nested transport failures MAY report at most four unique exception type names traversed through reason, cause, or context relationships, with cycles terminated. Terminal-control neutralization and output bounds SHALL apply in every mode.

Known/unknown scheme classification SHALL follow `locked-npm-environment-assembly`. Unknown-scheme candidates SHALL be hidden in `redacted` and `host-path` without normalized hostname, host/path or URL-fingerprint facts. Captured local `exact` source content SHALL retain its existing behavior; simultaneous external SDK diagnostics SHALL remain safe.

#### Scenario: Hostname display is disabled
- **WHEN** a reviewed asset download fails under the default `redacted` output policy
- **THEN** the failure SHALL identify the logical asset and bounded exception-type chain
- **AND** SHALL expose no source or proxy hostname, URL component, credential, or exception message

#### Scenario: Hostname display is enabled
- **WHEN** a diagnostic with an approved safe host/path fact is presented under `host-path`
- **THEN** the failure MAY include the normalized source hostname and safe path
- **AND** SHALL expose no other source-URL component, proxy detail, credential, or exception message

#### Scenario: Host-path failure context uses the selected retained tail
- **WHEN** assembly fails after `host-path` diagnostics have entered the mode-selected bounded retained tail
- **THEN** text failure context and JSON `host_failure.tail` SHALL render their retained diagnostics with the same safe normalized hostname/path representation and original retained order
- **AND** no source-safe or redacted duplicate tail SHALL be retained or emitted
- **AND** simultaneous external SDK diagnostics SHALL remain URL-free and SHALL contain no path

#### Scenario: Nested exception graph is deep or cyclic
- **WHEN** a transport exception exposes more than four unique related types or a relationship cycle
- **THEN** presentation SHALL terminate deterministically after at most four unique type names

#### Scenario: Unknown URLs are hidden without host presentation
- **WHEN** captured diagnostics contain an unknown-scheme URL with credentials and a private path under `redacted` or `host-path`
- **THEN** live output and text/JSON retained failure context SHALL hide the candidate
- **AND** SDK events SHALL carry no hostname or URL fingerprint from that candidate
- **AND** no host/path fact SHALL be available for local rendering

#### Scenario: Unknown URLs do not add a grouping identity
- **WHEN** ordinary diagnostics differ only in unknown-scheme URL content and otherwise have identical safe rendered text and event identity
- **THEN** existing safe repeat grouping SHALL treat them as equal
- **AND** validated known-scheme URL fingerprints SHALL continue to distinguish groups when they differ

#### Scenario: Exact unknown URL remains local source content
- **WHEN** local `exact` diagnostics contain an unknown-scheme URL and an external SDK sink is also attached
- **THEN** captured local text and failure tails SHALL preserve source content within existing terminal-safety and bounds
- **AND** SDK text SHALL hide the candidate without hostname, host/path or fingerprint disclosure

#### Scenario: Exact mode has no captured third-party diagnostic
- **WHEN** a structured host transport failure provides no captured terminal-safe source diagnostic
- **THEN** `exact` SHALL retain the safe logical asset and bounded exception-type presentation
- **AND** SHALL NOT synthesize or expose a raw URL, credential, proxy detail, or exception message

# Docker Build Output

## Purpose

Define Docker build-progress visibility, structured-output isolation, failure reporting, and final build summaries.

## Requirements

### Requirement: Stream build progress in text mode
The constructor SHALL expose Docker build stdout and stderr to the host terminal as they are produced when an actual build runs in text output mode. It SHALL preserve the selected native `--progress auto`, `plain`, or `tty` argument and SHALL NOT buffer the complete build before displaying it.

#### Scenario: Long-running text build
- **WHEN** a user runs `docker-constructor.py build` in text output mode and Docker emits BuildKit progress before exiting
- **THEN** that progress is visible on the host before the Docker process exits

#### Scenario: Explicit progress style
- **WHEN** a user runs a text-mode build with `--progress plain` or `--progress tty`
- **THEN** Docker receives the selected progress style and controls its native presentation

### Requirement: Preserve structured output isolation
The constructor SHALL keep JSON-mode stdout as one valid JSON document. Docker output from a JSON-mode build SHALL be captured rather than streamed into the facade's stdout, and a failed build SHALL retain its captured diagnostic and original exit code in the structured result.

#### Scenario: Successful JSON build
- **WHEN** a user runs a successful build with `--output json`
- **THEN** stdout contains valid constructor JSON without interleaved Docker progress

#### Scenario: Failed JSON build
- **WHEN** Docker emits an error and exits nonzero during a build with `--output json`
- **THEN** the constructor returns an operational failure containing the captured diagnostic and preserving the Docker exit code

### Requirement: Do not duplicate streamed diagnostics
After a streamed text-mode build fails, the constructor SHALL report the failed operation and Docker exit code without replaying output that Docker already wrote to the terminal.

#### Scenario: Streamed build failure
- **WHEN** Docker writes a failure diagnostic during a text-mode build and exits nonzero
- **THEN** the diagnostic is shown once through the inherited stream and the final constructor result does not repeat it

### Requirement: Render an appropriate final build result
After a successful executed text-mode build, the constructor SHALL print a concise success summary instead of the complete Docker build vector. The complete vector SHALL remain available for dry runs, JSON output, and verbose diagnostics.

#### Scenario: Successful executed text build
- **WHEN** an actual text-mode build completes successfully
- **THEN** the constructor prints a concise success summary without printing the full build command as the primary result

#### Scenario: Dry-run build
- **WHEN** a user requests a build dry run
- **THEN** the constructor prints the complete planned Docker build vector and does not execute Docker

#### Scenario: Verbose build diagnostics
- **WHEN** a user requests verbose diagnostics for a build
- **THEN** the complete Docker build vector remains available after rendering

### Requirement: Report host-side build materialization progress
Live display cadence and coalescing guarantees in this requirement apply to healthy, promptly serviced presentation. Live delivery and deduplication SHALL be best effort under saturation, state reset, or presentation failure; repeated diagnostics or unavailable live output are permitted in those cases. Sanitization, capture bounds, authoritative domain heartbeat/deadline facts, and primary-result isolation SHALL remain mandatory. Presentation SHALL NOT infer diagnostic silence or execution timeout from the number of consecutive consumed heartbeat events.

Before the main Docker build starts, the constructor SHALL report the current host-side build-materialization phase when text output is presented interactively, including Pi release acquisition, reviewed build-artifact acquisition, locked dependency assembly, derived-environment validation, publication, and transition to the Docker build. It SHALL expose observable operational steps within those phases, including coordination-lock wait, cache lookup, assembler-container startup, `npm ci`, validation, and publication. A long-running operation SHALL become visible before it completes and SHALL report elapsed time; when diagnostic or transport-progress activity has been observed for at least one second it SHALL identify the last observed activity kind and whole monotonic age in seconds, and when a fixed deadline applies it SHALL report remaining time. Before any qualifying activity is observed, or while its age is less than one second, it SHALL omit last-activity reporting without delaying heartbeat emission. It SHALL produce the first heartbeat 3 seconds after the operation starts and subsequent heartbeat facts at intervals no greater than 1 second, regardless of diagnostic or transport-progress activity. Separately, an operation that declares an expected diagnostic stream SHALL identify the duration specifically as diagnostic silence exactly at 2 minutes without stdout or stderr and SHALL continue updating that duration in subsequent heartbeat facts. An operation without an expected diagnostic stream, including host artifact/release downloads, SHALL omit diagnostic silence entirely. Transport progress SHALL update last activity but SHALL NOT reset applicable diagnostic silence or the heartbeat schedule. Diagnostic silence SHALL NOT be represented as proof that execution or networking is inactive, and neither silence nor human-readable npm output SHALL be represented as proof that npm is currently downloading.

Presented host progress, live diagnostics, rendered retained diagnostics, and JSON `host_failure.tail` SHALL obey the same facade-owned `network_url_display` policy. JSON execution SHALL create no live presentation sink and SHALL keep stdout one valid JSON document. External SDK diagnostic events SHALL remain bounded, redacted, and URL-free regardless of display policy. Verification evidence, assembler evidence, assembler identity, assembler-input identity, and assembled-output identity SHALL remain independent of the display selector. The configured heartbeat presentation SHALL be one of `interactive`, `lines`, or `off`: `interactive` SHALL use replaceable terminal progress only for interactive text output, render the first status at 3 seconds, and refresh it at intervals no greater than 1 second; `lines` SHALL use durable newline-terminated text events, render the first status at 3 seconds, emit each ordinary status line exactly 30 seconds after the preceding durable status, emit a distinct diagnostic-silence transition exactly at 120 seconds, and restart its 30-second line interval from that transition; `off` SHALL suppress heartbeat presentation without suppressing domain heartbeat-event production, phase terminal states, or actionable warnings and errors. JSON execution SHALL create no live presentation sink. Noninteractive text execution SHALL create a live host-event sink only when `lines` is explicitly configured; otherwise it SHALL retain only bounded diagnostics for failure reporting.

#### Scenario: Pi dependency assembly is long-running
- **WHEN** an interactive text-mode build spends time assembling locked Pi dependencies before the main Docker build
- **THEN** the terminal SHALL identify the current Pi assembly step before it completes
- **AND** SHALL report elapsed time, diagnostic-silence duration, last observed activity kind/age, and remaining deadline as applicable
- **AND** assembler warnings, errors, retries, timeouts, and status diagnostics selected by `network_url_display` SHALL be visible as they are produced

#### Scenario: Silent npm execution remains alive
- **WHEN** locked npm execution produces no stdout or stderr through the 2-minute diagnostic-silence threshold
- **THEN** interactive heartbeat output SHALL first appear at 3 seconds and refresh at least once per second
- **AND** exactly at 120 seconds it SHALL describe diagnostic silence and the whole-second age of the last observed diagnostic activity from one second onward without claiming that npm or its network is inactive
- **AND** a sub-second last-activity age SHALL be omitted without delaying the heartbeat
- **AND** SHALL NOT state that npm is downloading or has network activity unless a separate authoritative observation exists
- **AND** SHALL identify the safe assembler container name and safe cancellation action when the silence becomes prolonged

#### Scenario: Host download has transport progress but no diagnostic silence
- **WHEN** a long-running host artifact or release download yields transport bytes
- **THEN** transport progress SHALL update cumulative bytes and last activity
- **AND** the first and subsequent heartbeats SHALL retain their operation-based cadence
- **AND** diagnostic silence SHALL be absent because the operation has no expected diagnostic stream

#### Scenario: Transport progress does not reset applicable diagnostic silence
- **WHEN** an operation with an expected diagnostic stream observes transport progress after its latest stdout or stderr
- **THEN** transport progress SHALL update last activity
- **AND** SHALL NOT reset or relabel the separately tracked diagnostic silence
- **AND** SHALL NOT alter the heartbeat schedule

#### Scenario: Build advances to Docker
- **WHEN** host artifact and Pi materialization complete successfully and presentation remains healthy
- **THEN** the constructor SHALL drain accepted host events, finalize and clear host progress, and terminate/join the presentation worker before native Docker build progress
- **AND** if presentation stalls, the constructor SHALL continue after the shared five-second presentation completion budget without promising clean terminal handoff

#### Scenario: Structured build output
- **WHEN** a build runs with JSON output
- **THEN** host-materialization progress and assembler output SHALL NOT be written into structured stdout
- **AND** stdout SHALL remain one valid constructor JSON document
- **AND** a failure's bounded `host_failure.tail` SHALL use the same resolved network URL display policy as text failure context

#### Scenario: Noninteractive line mode
- **WHEN** a noninteractive text build explicitly configures heartbeat presentation as `lines`
- **THEN** the first status SHALL be emitted at 3 seconds as a durable newline-terminated line under the selected network URL display policy
- **AND** each ordinary status line SHALL be emitted exactly 30 seconds after the preceding durable status
- **AND** a distinct diagnostic-silence line SHALL be emitted exactly at 120 seconds and restart the 30-second ordinary-line interval
- **AND** terminal control sequences SHALL NOT be emitted

#### Scenario: Noninteractive default mode
- **WHEN** text output is noninteractive and `lines` was not explicitly configured
- **THEN** the facade SHALL create no live host progress or diagnostic sink
- **AND** failures SHALL retain bounded diagnostics under the selected network URL display policy

#### Scenario: Host materialization fails
- **WHEN** any host materialization operation fails before the main Docker build starts
- **THEN** output SHALL identify the failed phase and logical operation
- **AND** SHALL include the existing bounded diagnostic tail selected by `network_url_display` once in the final report when it is nonempty, explicitly labeled as retained context that may repeat live output
- **AND** SHALL NOT filter that tail using live-delivery history or substitute a failure summary for an empty tail
- **AND** an empty tail SHALL produce no diagnostic section
- **AND** report visibility is best effort if presentation has failed, but the structured failure result SHALL remain intact
- **AND** the main Docker build SHALL NOT execute

### Requirement: Integrate host progress through typed presentation-neutral events
Coalescing, refresh, and finalization scenarios in this requirement describe healthy presentation; overload or presentation failure MAY cause safe repeated groups or unavailable display as specified by the degradation scenarios below. This qualification SHALL NOT weaken event safety, bounded capture, authoritative activity facts, or primary-result isolation.

The constructor SHALL convey host-materialization lifecycle and live assembler diagnostics through immutable typed event interfaces between the facade, build orchestration, Pi materialization, artifact acquisition, and assembler execution. Existing lifecycle events SHALL retain fixed phase and started/succeeded/failed classifications. A separate operational activity event hierarchy SHALL represent step transitions, observed transport progress, diagnostics, and heartbeats without changing lifecycle-event semantics. Each step start SHALL carry a closed declaration of whether that operation expects a diagnostic stream, and heartbeat facts SHALL omit diagnostic-silence duration when it does not. Before materialization starts, the facade SHALL fix one `network_url_display` value for the operation and propagate the same typed enum through host materialization, Pi assembly, and assembler execution requests into collector configuration before collection starts. The collection boundary SHALL derive terminal-safe source and safe projected representations only as needed for the selected local mode and external delivery, while feeding exactly one bounded retained tail with the selected local representation. Non-selected local representations and transient external SDK representations SHALL NOT populate another retained tail. External structured diagnostic events SHALL identify their fixed phase, step, stdout/stderr source, closed classification, optional safe logical resource, already-redacted URL-free text, zero or more normalized hostnames from which every other URL component has already been removed, and an ordered tuple of ephemeral session-keyed opaque URL fingerprints for safe presentation identity. External SDK events SHALL always use that safe representation. The facade SHALL use the same immutable policy for local live presentation and the single retained representation used by text and JSON failure formatting; this selection SHALL NOT alter activity observation, lifecycle facts, evidence, or semantic identities. Activity events SHALL use an activity-independent monotonic heartbeat cadence beginning at 3 seconds with subsequent intervals no greater than 1 second and a separate monotonic diagnostic-silence duration reset only by stdout/stderr and exposed exactly from 120 seconds, SHALL carry optional closed last-activity kind and whole monotonic age in seconds together only after diagnostic or transport-progress activity is at least one second old, SHALL reject last-activity ages below one second, SHALL expose no wall-clock activity timestamp, and SHALL carry a remaining deadline only when one exists. The facade SHALL own output-mode selection and rendering; domain modules SHALL NOT print directly or depend on terminal presentation details. The sink SHALL remain optional for SDK and injected callers, and a sink failure SHALL NOT replace or change the build result. The internal facade path SHALL use one bounded ordered inbox and one presentation actor, with independently bounded admission budgets for control and telemetry. Control capacity SHALL be the fixed 1024-event `CONTROL_CAPACITY` hard safety limit with substantial headroom, not a calculation from the exact current build transcript or module inventory; exact supported event order and multiplicity SHALL be tested independently. Short mutex-protected admission is permitted; producers SHALL NOT wait for queue capacity, rendering, I/O, or consumer acknowledgement. On the internal enqueue path, no rendering, flushing, arbitrary callback invocation, completion wait, or join SHALL occur under inbox or producer serialization locks. External SDK callback serialization and isolation SHALL retain their existing contract. Diagnostic saturation SHALL NOT consume control capacity. Control events SHALL preserve admission order; accepted diagnostic events SHALL preserve that order unless explicitly discarded on presentation failure. Optional supersession of replaceable telemetry SHALL NOT move a newer observation before intervening control events or cross an operation terminal/restart boundary.

Orchestration SHALL finish diagnostic readers and sanitizer finalization, drain any upstream dispatcher, and stop/join heartbeat production before admitting the corresponding operation terminal event. Session close SHALL atomically end acceptance and wake the actor independently of queue capacity; it SHALL NOT require admission of a shutdown message. Normal completion SHALL drain accepted events, finalize display, signal completion, and join the worker before BuildKit or return. Control-admission failure SHALL explicitly disable presentation and wake its consumer without waiting under producer locks; it SHALL NOT silently discard control as ordinary telemetry or change the primary result.

All presentation completion waits and joins SHALL share one five-second monotonic budget, which repeated cleanup calls SHALL NOT restart. Renderer exceptions SHALL disable further output and discard pending display state while allowing completion. If terminal I/O stalls beyond the budget, the facade SHALL cancel further presentation and continue without an unbounded wait/join or synchronous retry to the failed stream. A daemon worker and an already-started write MAY outlive that degraded return; after unblocking, the worker SHALL check cancellation before any further renderer operation. No clean native-output handoff, final-message visibility, or worker-termination guarantee applies to indefinitely blocked I/O. Healthy and returning-error paths SHALL still terminate the worker, and no rendering SHALL occur after successful completion acknowledgement.

During a live session the actor SHALL be the sole host terminal writer, including the facade-prepared final host failure report. The normal CLI result printer SHALL NOT write that report a second time or retry it after presentation failure. Structured result data SHALL remain available independently of rendering. Without a live session, ordinary text/JSON result formatting SHALL remain responsible for the report; JSON SHALL create no presentation inbox or worker.

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
- **WHEN** two ordinary diagnostics outside a recognized successful-fetch group render the same URL-free safe text but their ordered hidden URL identities differ
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

### Requirement: Configure host output through the local companion
The host-only local companion defined by `local-project-configuration` SHALL accept an optional closed `[output]` table containing only `host_heartbeat` and `network_url_display`. `host_heartbeat` SHALL accept exactly `interactive`, `lines`, or `off` and SHALL default to `interactive` when the key or table is absent. `network_url_display` SHALL accept exactly `redacted`, `host-path`, or `exact` and SHALL default to `redacted` when the key or table is absent. Unknown output keys, unsupported values, incorrect value types, duplicate definitions, and placement of either setting outside local `[output]` SHALL be rejected with an actionable error that identifies the local configuration path without exposing unrelated local values. The former `show_network_hosts` field SHALL be an unknown key and SHALL NOT be accepted, migrated, or reinterpreted.

Output policy SHALL remain host-side presentation state. Neither setting SHALL be accepted in reviewed `docker-constructor.toml`, included in reviewed inventory serialization or update discovery, persisted as transient runtime state, or included in effective build or runtime dependency projections. The facade SHALL resolve `network_url_display` before materialization and MAY pass that immutable typed enum through host materialization, Pi assembly, and assembler execution requests solely to configure live diagnostic selection and the single retained tail before stream capture. This presentation-only field SHALL NOT be a semantic assembly input and SHALL be excluded from assembler/input/output identities, hashing and equality used for cache lookup or reuse, verification and assembler evidence, publication decisions, and primary-result semantics. `host_heartbeat` SHALL remain facade-local and SHALL NOT enter those requests. The companion, `[output]` table, and derived output policy SHALL NOT be copied or mounted into build or runtime containers. No command-line option or environment-variable alias SHALL be introduced for either setting.

#### Scenario: Local output settings are absent
- **WHEN** the resolved local companion omits `[output]` or either output key
- **THEN** `host_heartbeat` SHALL resolve to `interactive` and `network_url_display` SHALL resolve to `redacted` for each absent value
- **AND** the defaults SHALL affect only host-side live and retained presentation selection, including the presentation-only selector propagated to collector configuration

#### Scenario: Local output settings are valid
- **WHEN** local `[output]` contains one of `interactive`, `lines`, or `off` for `host_heartbeat` and one of `redacted`, `host-path`, or `exact` for `network_url_display`
- **THEN** local configuration validation SHALL accept the values exactly
- **AND** facade presentation SHALL receive the resulting immutable policy
- **AND** the typed `network_url_display` value MAY propagate through host-side materialization and assembly requests to collector configuration for live and retained diagnostic selection
- **AND** domain heartbeat and external safe diagnostic event production SHALL remain independent of those values

#### Scenario: Local output settings are invalid
- **WHEN** local configuration contains an unknown `[output]` key including `show_network_hosts`, an unsupported heartbeat or network URL display value, a wrong value type, a duplicate definition, or either setting outside `[output]`
- **THEN** validation SHALL reject it before build, materialization, transport, assembler, or Docker effects
- **AND** the error SHALL safely identify the local configuration path and invalid field

#### Scenario: Output policy remains host-only
- **WHEN** reviewed inventory serialization, update discovery, effective build or runtime projection, build-container construction, or runtime launch is produced
- **THEN** `host_heartbeat`, `network_url_display`, the `[output]` table, and the local companion SHALL be absent
- **AND** host-side requests MAY carry only the typed `network_url_display` presentation field as specified above
- **AND** no CLI option or environment-variable alias SHALL provide another source for the settings

#### Scenario: Presentation selection does not alter assembly semantics
- **WHEN** three otherwise equivalent host materialization and assembly requests differ only by `network_url_display = redacted`, `host-path`, or `exact`
- **THEN** their assembler identity, assembler-input identity, assembled-output identity, cache lookup and reuse, and verification and assembler evidence SHALL be identical
- **AND** their npm and Docker argv, network behavior, timeouts, lifecycle events, publication decisions, cleanup, and primary result SHALL be identical
- **AND** only live diagnostic presentation and retained failure content MAY differ

### Requirement: Apply one safe network diagnostic presentation policy
Host-pipeline live and failure diagnostics SHALL identify reviewed resources by safe logical asset names and MAY identify named assembler containers by the fixed `npm-assembler-<digest-prefix>` form. The closed `network_url_display` policy SHALL control additional network detail. `redacted` SHALL omit source URL detail. `host-path` MAY expose only a normalized hostname and safe path and SHALL omit scheme, port, user information, query, fragment, proxy details, credentials, and exception messages. `exact` MAY preserve terminal-safe source diagnostic content only where such third-party content was captured under the contracts of `locked-npm-environment-assembly`; it SHALL NOT synthesize unavailable source content or expose arbitrary transport exception messages. Nested transport failures MAY report at most four unique exception type names traversed through reason, cause, or context relationships, with cycles terminated. Terminal-control neutralization and output bounds SHALL apply in every mode.

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

#### Scenario: Exact mode has no captured third-party diagnostic
- **WHEN** a structured host transport failure provides no captured terminal-safe source diagnostic
- **THEN** `exact` SHALL retain the safe logical asset and bounded exception-type presentation
- **AND** SHALL NOT synthesize or expose a raw URL, credential, proxy detail, or exception message

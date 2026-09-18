## MODIFIED Requirements

### Requirement: Retain bounded redacted assembler diagnostics
The assembler SHALL make ongoing execution observable to an authorized text-mode caller while retaining only the existing bounded diagnostic tail. Before display, return, persistence, or inclusion in a failure report, diagnostics SHALL redact resolved proxy endpoints, trust paths, caller-supplied secrets, and disallowed URL components. Diagnostic collection SHALL NOT grow without bound for a long-running process. Presentation SHALL surface each first safe npm warning, error, retry, timeout, or status line that is admitted to the bounded presentation mailbox. It SHALL coalesce every sequence of identical admitted final safe rendered diagnostic lines regardless of warning, error, retry, status, or timeout classification and SHALL count admitted occurrences including the first. Under mailbox saturation, ordinary diagnostic admission SHALL be best-effort, and a rendered repetition count SHALL NOT claim to include occurrences dropped before admission. Warnings and errors SHALL NOT bypass coalescing or be forced into individual ungrouped live writes. Typed Constructor lifecycle/control events SHALL NOT be treated as diagnostic lines or coalesced. In `lines` mode it SHALL use a fixed one-second monotonic window, emit the first diagnostic immediately, and emit `<diagnostic> (repeated N times)` for a group total `N >= 2` at window end or immediately before a different diagnostic or terminal event; a single-occurrence group SHALL emit no summary. In interactive mode it SHALL maintain one mutable diagnostic slot, initially without a suffix, increment the group total for every identical repeat, and update the slot at the next at-most-one-second TUI refresh using the same canonical suffix. The interactive group SHALL remain mutable until a different diagnostic or terminal event finalizes it; a later identical diagnostic SHALL start a new group. Before either retained or live output leaves the collector, the decoded stream SHALL undergo secret redaction followed by boundary-safe URL sanitization and normalized-host extraction. Pending sanitizer state for an ambiguous URL or secret candidate SHALL be capped at 8 KiB, and diagnostic-line assembly SHALL be capped at 64 KiB of decoded UTF-8 text per unterminated line. The collector SHALL continue draining after either limit is reached. A candidate exceeding its limit SHALL emit only `[sanitized oversized token]` and discard its remaining content until a safe token boundary; a diagnostic line exceeding its limit SHALL emit only `[sanitized oversized diagnostic]` and discard its remaining content until newline or stream termination. Processing SHALL resume after that safe boundary. At EOF, reader failure, or cancellation, an unresolved URL or secret candidate SHALL emit only `[sanitized incomplete token]`, never ordinary candidate text, and any pending line SHALL be finalized only through the same bounded sanitizer. The original ordering and occurrences of the resulting URL-free sanitized text and fixed safe replacement markers SHALL enter the existing byte-bounded per-stream tail without grouping; only the structured live branch SHALL later be grouped for presentation. Existing tail byte limits and truncation semantics SHALL remain unchanged. Merely secret-redacted pre-projection text SHALL NOT appear in returned, persisted, attached, or failure representations. One facade presentation worker SHALL exclusively own coalescing state, deadline calculation, and renderer calls while reading a bounded thread-safe event mailbox with independently bounded reliable-control and best-effort telemetry capacity. Diagnostic saturation SHALL NOT evict or consume capacity reserved for lifecycle, terminal, timeout, cancellation, or barrier events. Dropped diagnostics SHALL be represented by a bounded, non-coalesced omission notice before the next admitted diagnostic or terminal event; when the omission count itself saturates, the notice SHALL state a lower bound rather than an exact count. While `GuardedHostEventSink` serialization is held, producers SHALL only perform failure-isolated non-blocking admission of immutable events and SHALL NOT render, flush, wait for barrier acknowledgement, or stop/join presentation machinery. A step-terminal barrier SHALL be attempted only after heartbeat and stream-reader producers are stopped and joined. When admitted, it SHALL cause the worker to finalize the latest admitted-occurrence count before terminal rendering and SHALL be acknowledged asynchronously after the producer callback has returned. If reliable admission fails, including for a terminal or shutdown barrier, a capacity-independent emergency signal SHALL wake and stop the worker without consuming a mailbox slot; the callback SHALL return the admission failure without waiting, and the facade SHALL NOT await acknowledgement for the unadmitted barrier. Only after the guarded callback returns SHALL the facade await the distinct worker-stopped acknowledgement and join the worker. The worker SHALL persist across successful host steps and SHALL be shut down and joined before BuildKit, host-pipeline failure return, timeout return, or cancellation return. Renderer failure SHALL disable rendering and discard pending presentation state without changing the primary result while the worker continues servicing admitted control barriers. Reliable-lane exhaustion SHALL instead discard pending presentation, stop the worker through the emergency path, and leave the primary result unchanged. No render SHALL occur after session-shutdown or worker-stopped acknowledgement.

#### Scenario: Long-running npm installation emits output
- **WHEN** npm writes stdout or stderr during authorized interactive text-mode assembly
- **THEN** safe redacted warnings, errors, retries, timeouts, and status output SHALL become visible before process completion
- **AND** each received stdout or stderr chunk SHALL count as diagnostic activity for silence tracking and last-activity age
- **AND** the constructor SHALL retain only the existing bounded redacted diagnostic tail

#### Scenario: Repeated retry status
- **WHEN** npm emits identical diagnostic lines of any closed diagnostic classification in `lines` mode within one monotonic second
- **THEN** presentation SHALL emit the first admitted line immediately and count each admitted occurrence including the first
- **AND** a group total of at least two admitted occurrences SHALL emit `<diagnostic> (repeated N times)` at window end or before a different diagnostic or terminal event
- **AND** a single-occurrence group SHALL emit no summary
- **AND** a different diagnostic SHALL follow the flushed summary so observable order is preserved
- **AND** identical warnings, errors, and timeout diagnostics SHALL use the same coalescing rule
- **AND** typed Constructor lifecycle/control events SHALL remain individual and uncoalesced
- **AND** retained failure diagnostics SHALL receive the original ungrouped ordering and occurrences after secret redaction and URL sanitization

#### Scenario: Interactive repeated diagnostic updates one slot
- **WHEN** interactive presentation receives identical repeated diagnostic lines of any closed diagnostic classification
- **THEN** the first occurrence SHALL populate one mutable diagnostic slot without a suffix
- **AND** each admitted repeat SHALL increment the admitted-occurrence count
- **AND** the next TUI refresh, no later than one second afterward, SHALL replace that slot with `<diagnostic> (repeated N times)` for admitted total `N`
- **AND** neither the first occurrence nor any repeat SHALL create a durable line while the group remains mutable
- **AND** a different diagnostic or terminal event SHALL finalize the group with exactly one ordered durable write containing the latest total
- **AND** warning and error classification SHALL NOT change this mutable-first behavior
- **AND** a later identical diagnostic SHALL start a new group

#### Scenario: Coalescing window ends without another diagnostic
- **WHEN** a `lines`-mode diagnostic group with at least two total occurrences is pending and no later diagnostic arrives before the one-second window ends
- **THEN** the single facade presentation worker SHALL emit the canonical total-count summary when its mailbox wait reaches the window deadline
- **AND** assembler execution and facade presentation SHALL create no separate coalescing timer thread

#### Scenario: Diagnostic mailbox saturates
- **WHEN** diagnostic producers fill the bounded best-effort telemetry capacity
- **THEN** further ordinary diagnostics MAY be dropped without blocking a producer
- **AND** diagnostic saturation SHALL NOT drop, evict, or delay admission of lifecycle, terminal, timeout, cancellation, or barrier events within the protocol's reserved control bound
- **AND** repetition suffixes SHALL count only admitted occurrences and SHALL NOT claim exact producer-side multiplicity
- **AND** presentation SHALL emit one non-coalesced omission notice before the next admitted diagnostic or terminal event
- **AND** the notice SHALL report the bounded dropped count, or a lower bound when that counter saturates
- **AND** terminal processing SHALL remain ordered after every earlier admitted event

#### Scenario: Reliable lane cannot admit a barrier
- **WHEN** reserved reliable capacity is unexpectedly exhausted while admitting a terminal or shutdown barrier
- **THEN** the producer SHALL atomically signal capacity-independent emergency stop and return admission failure without blocking
- **AND** it SHALL NOT render, wait for barrier acknowledgement, wait for worker termination, or join the worker under `GuardedHostEventSink` serialization
- **AND** the worker SHALL wake, disable rendering, discard pending presentation and queued events, publish worker-stopped acknowledgement, and exit without awaiting the unadmitted barrier
- **AND** after the guarded callback returns, the facade SHALL skip acknowledgement of the unadmitted barrier, await worker-stopped acknowledgement, and join the worker
- **AND** no presentation worker SHALL remain alive when the facade continues to BuildKit or returns success, failure, timeout, or cancellation
- **AND** reliable-lane exhaustion SHALL NOT alter the primary result

#### Scenario: Coalescing terminates with presentation lifecycle
- **WHEN** a host step reaches its terminal event with a pending diagnostic group
- **THEN** heartbeat and stream-reader producers SHALL stop and join before enqueueing the terminal barrier
- **AND** the single presentation worker SHALL freeze and durably finalize the latest admitted-occurrence count and any pending omission notice before terminal rendering and acknowledgement
- **AND** the worker SHALL remain available for the next successful host step
- **WHEN** the host presentation session then ends before BuildKit, failure return, timeout return, or cancellation return
- **THEN** the facade SHALL attempt to enqueue a shutdown barrier through the prompt-returning guarded sink and return from that callback before waiting
- **AND** after successful admission it SHALL await barrier acknowledgement and join the presentation worker outside guarded serialization
- **AND** after failed reliable admission it SHALL instead skip barrier acknowledgement, await worker-stopped acknowledgement, and join the presentation worker outside guarded serialization
- **AND** no render SHALL occur after shutdown or worker-stopped acknowledgement
- **AND** presentation failure SHALL discard pending presentation state, continue control-barrier service, and SHALL NOT alter the primary operation result

#### Scenario: Structured caller executes assembly
- **WHEN** assembler execution has no authorized live text sink, including JSON mode or an SDK caller that omits presentation
- **THEN** assembler output SHALL NOT be interleaved with caller output
- **AND** a failure SHALL include the bounded redacted diagnostic tail

#### Scenario: URL spans diagnostic chunks
- **WHEN** a URL, credential, secret, or disallowed URL component spans decoder or input-chunk boundaries
- **THEN** the collector SHALL withhold an ambiguous token prefix only within the 8 KiB pending-sanitizer limit until it can be sanitized
- **AND** SHALL NOT flush an unresolved candidate as ordinary text
- **AND** no complete or partial disallowed URL component, credential, or secret SHALL enter the retained tail or structured live event
- **AND** existing retained-tail byte bounds and truncation behavior SHALL remain unchanged

#### Scenario: Arbitrarily long URL has no terminating delimiter
- **WHEN** an arbitrarily long URL-shaped candidate arrives across any number of chunks without a terminating delimiter
- **THEN** pending sanitizer state SHALL remain bounded at 8 KiB while input continues draining
- **AND** the collector SHALL emit only `[sanitized oversized token]` for the candidate and discard its remaining content until a safe token boundary
- **AND** processing SHALL recover after that boundary
- **AND** no candidate fragment SHALL enter a structured live event or retained tail

#### Scenario: Arbitrarily long diagnostic has no newline
- **WHEN** an arbitrarily long diagnostic arrives without a newline
- **THEN** diagnostic-line assembly SHALL remain bounded at 64 KiB while input continues draining
- **AND** the collector SHALL emit only `[sanitized oversized diagnostic]` for that diagnostic and discard its remaining content until newline or stream termination
- **AND** processing SHALL recover after a subsequent newline
- **AND** no discarded diagnostic fragment SHALL enter a structured live event or retained tail

#### Scenario: Stream terminates with an ambiguous sensitive prefix
- **WHEN** EOF, reader failure, or cancellation occurs while an ambiguous URL, encoded URL, credential, or secret prefix or an unterminated diagnostic line is pending
- **THEN** unresolved sensitive candidates SHALL be replaced only with `[sanitized incomplete token]` and SHALL NOT be flushed as ordinary text
- **AND** any pending diagnostic line SHALL be finalized only through bounded secret redaction and URL sanitization
- **AND** pending sanitizer and line-assembly state SHALL remain within their fixed limits
- **AND** no complete or partial URL, credential, or secret SHALL enter a structured live event or retained tail

#### Scenario: Diagnostic contains network configuration
- **WHEN** assembler output contains a configured proxy endpoint, trust path, credential, or disallowed URL component
- **THEN** every displayed, returned, persisted, attached, and failure-report representation SHALL receive only secret-redacted and URL-sanitized text
- **AND** structured diagnostics SHALL separate normalized source-host facts from URL-free text independently of output configuration
- **AND** only the facade SHALL apply hostname-display policy

## ADDED Requirements

### Requirement: Expose locked-assembly operational activity
Locked environment assembly SHALL expose presentation-neutral operational activity for coordination-lock wait, cache lookup and reuse, stale staging cleanup, named-container startup, `npm ci` execution, output validation, and publication. Waiting for the existing blocking coordination lock SHALL report elapsed wait without changing lock acquisition semantics. `npm ci` activity SHALL derive from its stdout and stderr rather than Docker network, CPU, or filesystem probes. The existing fixed total deadline and cleanup behavior SHALL remain unchanged.

#### Scenario: Assembly waits for coordination
- **WHEN** another execution holds the same input-identity coordination lock
- **THEN** activity SHALL identify coordination wait and elapsed duration
- **AND** SHALL NOT impose a new lock timeout or alter serialization semantics

#### Scenario: npm produces no diagnostics
- **WHEN** the named assembler container remains alive without stdout or stderr
- **THEN** activity SHALL identify elapsed execution, diagnostic silence, the last observed diagnostic activity age when one exists, safe container name, and remaining fixed deadline
- **AND** SHALL NOT claim that npm or its registry connection is inactive

#### Scenario: Cached environment is reusable
- **WHEN** locked assembly finds a fully verified cached environment after coordination
- **THEN** activity SHALL report cache reuse
- **AND** no container or npm activity SHALL be reported

#### Scenario: Assembly fails
- **WHEN** locked assembly times out or exits unsuccessfully
- **THEN** the failure report SHALL identify the active operational step
- **AND** SHALL reuse the existing bounded redacted diagnostic tail
- **AND** existing container and staging cleanup guarantees SHALL apply

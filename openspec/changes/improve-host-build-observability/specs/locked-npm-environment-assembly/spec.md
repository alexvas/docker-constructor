## MODIFIED Requirements

### Requirement: Retain bounded redacted assembler diagnostics
Live display, coalescing, and refresh scenarios below describe healthy, promptly serviced presentation. Deduplication and coalescing SHALL be best effort under overload, state reset, or renderer failure: extra groups/repeated diagnostics or absent live output are permitted on those degraded paths. Normal-path coalescing SHALL remain supported and tested. Sanitization, safe identity matching, capture bounds, primary execution results, and cleanup SHALL NOT be weakened by this qualification. The retained tail SHALL remain independent of live delivery and SHALL NOT be reconciled with renderer history. Every final failure report SHALL include its nonempty tail once under an explicit retained-context label warning that live output may be repeated; empty tails SHALL produce no diagnostic section.

The assembler SHALL make ongoing execution observable to an authorized text-mode caller while retaining only the existing bounded diagnostic tail. Before display, return, persistence, or inclusion in a failure report, diagnostics SHALL redact resolved proxy endpoints, trust paths, caller-supplied secrets, and disallowed URL components. Diagnostic collection SHALL NOT grow without bound for a long-running process. Presentation SHALL surface each first safe npm warning, error, retry, timeout, or status line that is admitted to the bounded presentation mailbox. It SHALL coalesce every sequence of identical admitted final safe rendered diagnostic lines regardless of warning, error, retry, status, or timeout classification and SHALL count admitted occurrences including the first. Under mailbox saturation, ordinary diagnostic admission SHALL be best-effort, and a rendered repetition count SHALL NOT claim to include occurrences dropped before admission. Warnings and errors SHALL NOT bypass coalescing or be forced into individual ungrouped live writes. Typed Constructor lifecycle/control events SHALL NOT be treated as diagnostic lines or coalesced. In `lines` mode it SHALL use a fixed one-second monotonic window, emit the first diagnostic immediately, and emit `<diagnostic> (repeated N times)` for a group total `N >= 2` at window end or immediately before a different diagnostic or terminal event; a single-occurrence group SHALL emit no summary. In interactive mode it SHALL maintain one mutable diagnostic slot, initially without a suffix, increment the group total for every identical repeat, and update the slot at the next at-most-one-second TUI refresh using the same canonical suffix. When consecutive diagnostics have identical presentation identity and text except for exactly one changed numeric token, the latest diagnostic SHALL replace that mutable slot without a repetition suffix and reset its exact-repeat count to one; numeric values need not be monotonic. Numeric matching SHALL examine the entire maximal numeric-looking sequence and SHALL NOT extract a valid-looking substring from an invalid sequence. A valid numeric token SHALL consist of one or more ASCII digits followed by zero or more `.` plus one-or-more-digit segments, with neither adjacent boundary an ASCII letter, digit, underscore, dot, plus, or minus. Thus `1`, `42`, `1.2`, and `10.20.30` are valid, while signed `-1`, `+1`, and `-1.2`, incomplete `.1` and `1.`, malformed `1..2`, and identifier-embedded `item1` are invalid in full and SHALL NOT participate in numeric grouping. Surrounding nonnumeric text SHALL be byte-identical. The interactive group SHALL remain mutable until an identity/template mismatch, omission notice, different diagnostic, or terminal event finalizes it; only its latest value SHALL be durably written, and a later identical diagnostic SHALL start a new group. `lines` mode SHALL NOT apply numeric-variant grouping and SHALL preserve each changed numeric value as a separate durable line. Before either retained or live output leaves the collector, the decoded stream SHALL undergo secret redaction followed by boundary-safe URL sanitization, normalized-host extraction, and ephemeral URL-identity derivation. Each complete URL SHALL contribute an ordered fixed-width fingerprint produced by a cryptographic keyed digest with a fresh random presentation-session key. Fingerprint input SHALL remove userinfo, query, and fragment, exclude proxy information, and preserve scheme, normalized hostname, path, and any explicitly supplied port including an explicit default port. Fingerprint order and multiplicity SHALL be preserved. Pending sanitizer state for an ambiguous URL or secret candidate SHALL be capped at 8 KiB, and diagnostic-line assembly SHALL be capped at 64 KiB of decoded UTF-8 text per unterminated line. The collector SHALL continue draining after either limit is reached. A candidate exceeding its limit SHALL emit only `[sanitized oversized token]` and discard its remaining content until a safe token boundary; a diagnostic line exceeding its limit SHALL emit only `[sanitized oversized diagnostic]` and discard its remaining content until newline or stream termination. Processing SHALL resume after that safe boundary. At EOF, reader failure, or cancellation, an unresolved URL or secret candidate SHALL emit only `[sanitized incomplete token]`, never ordinary candidate text, and any pending line SHALL be finalized only through the same bounded sanitizer. The original ordering and occurrences of the resulting URL-free sanitized text and fixed safe replacement markers SHALL enter the existing byte-bounded per-stream tail without grouping; only the structured live branch SHALL carry ephemeral fingerprints and later be grouped for presentation. Session keys and fingerprints SHALL never enter rendered text, retained tails, failure reports, persistence, evidence, or policy identity and SHALL never permit cross-session correlation. Existing tail byte limits and truncation semantics SHALL remain unchanged. Merely secret-redacted pre-projection text SHALL NOT appear in returned, persisted, attached, or failure representations. One facade presentation actor SHALL exclusively own coalescing, presentation deadlines, and host terminal writes through one ordered bounded inbox with independently bounded admission budgets for control and telemetry. Diagnostic saturation SHALL NOT consume reserved lifecycle/terminal/final-report capacity. On the internal enqueue path, short mutex-protected admission is permitted; producers SHALL NOT wait for queue capacity, I/O, rendering, or consumer acknowledgement, and no rendering, arbitrary callback invocation, completion wait, flush, or join SHALL occur under inbox/producer serialization locks. The known internal enqueue path SHALL avoid a redundant asynchronous dispatcher while arbitrary external SDK callbacks SHALL retain their existing isolation contract.

Dropped live diagnostics SHALL produce a bounded non-coalesced notice with a subsequent admitted diagnostic/control event or normal close when presentation is functioning. Counts SHALL be exact only when known; uncertainty or saturation SHALL use a lower bound or a generic omission notice. Loss accounting SHALL NOT reconstruct a transcript or alter retained tails.

A step-terminal event SHALL be admitted only after stream readers finish safe sanitizer finalization, any upstream dispatcher is drained, and heartbeat production is stopped/joined. On the healthy path the actor SHALL finalize its pending diagnostic group before rendering that terminal and SHALL persist across successful steps. Session close SHALL stop acceptance and wake the consumer independently of queue capacity, without an enqueued shutdown sentinel. Normal close SHALL drain accepted events, finalize display, acknowledge completion, and join before BuildKit or return, with no output after successful completion acknowledgement.

Control-admission failure SHALL explicitly disable presentation and wake its actor without consuming another slot or changing the primary result. Renderer exceptions SHALL discard pending display state and disable output without retrying the failed stream while the actor can still complete. All presentation completion waits and joins SHALL share one five-second monotonic budget; repeated cleanup SHALL NOT restart it. If renderer I/O remains blocked, expiry SHALL cancel further presentation and allow execution/return to continue without an unbounded wait or synchronous CLI fallback. A daemon worker or an in-flight write MAY outlive this degraded boundary, but after unblocking no further renderer operation SHALL begin after cancellation is observed. Domain cleanup, timeout, SDK data, and retained capture guarantees SHALL remain independent of this presentation-only degradation.

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

#### Scenario: Interactive numeric diagnostic updates one slot
- **WHEN** consecutive admitted interactive diagnostics have the same phase, step, stream, classification, logical resource, ordered URL-fingerprint tuple, and rendered text except for exactly one changed numeric token
- **THEN** the latest diagnostic SHALL replace the mutable slot without a repetition suffix
- **AND** its exact-repeat count SHALL reset to one, while an exact repeat of that latest value SHALL resume canonical repetition counting
- **AND** numeric monotonicity SHALL NOT be required
- **AND** matching SHALL consume the entire maximal numeric-looking sequence and SHALL NOT extract a valid-looking substring from a signed, incomplete, malformed dot-separated, or identifier-embedded sequence
- **AND** all surrounding nonnumeric text SHALL be byte-identical
- **AND** an identity/template mismatch or omission notice SHALL finalize the group
- **AND** finalization SHALL durably write only the latest interactive value
- **AND** every received stdout/stderr chunk SHALL already have reset diagnostic silence and updated latest diagnostic activity before mailbox admission or grouping

#### Scenario: Numeric diagnostic values remain separate in lines mode
- **WHEN** `lines` mode receives diagnostics that differ in exactly one numeric token
- **THEN** each changed numeric value SHALL be emitted as its own durable line
- **AND** only exact repeats SHALL use the existing one-second repetition window

#### Scenario: Hidden URLs remain distinct without disclosure
- **WHEN** diagnostics with the same rendered safe text contain different sanitized URL identities
- **THEN** their ordered session-keyed fingerprint tuples SHALL keep their exact-repeat and numeric-update groups distinct
- **AND** userinfo, query, fragment, and proxy information SHALL contribute nothing to fingerprint input
- **AND** an explicit port, including an explicit default port, SHALL remain part of fingerprint input while an absent default port SHALL NOT be synthesized
- **AND** no fingerprint or session key SHALL appear in user-visible or retained output, failure reports, persistence, or evidence

#### Scenario: Coalescing window ends without another diagnostic
- **WHEN** a `lines`-mode diagnostic group with at least two total occurrences is pending and no later diagnostic arrives before the one-second window ends
- **THEN** the single facade presentation worker SHALL emit the canonical total-count summary when its mailbox wait reaches the window deadline
- **AND** assembler execution and facade presentation SHALL create no separate coalescing timer thread

#### Scenario: Diagnostic mailbox saturates
- **WHEN** diagnostic producers fill the bounded best-effort telemetry capacity
- **THEN** further ordinary diagnostics MAY be dropped without blocking a producer
- **AND** diagnostic saturation SHALL NOT consume the fixed bounded control capacity used for lifecycle, terminal, timeout, cancellation, or final-report events
- **AND** repetition suffixes SHALL count only admitted occurrences and SHALL NOT claim exact producer-side multiplicity
- **AND** functioning presentation SHALL emit a bounded non-coalesced omission notice with a subsequent admitted diagnostic/control or normal close
- **AND** the notice SHALL state the count only when known, otherwise a lower bound or a generic loss warning
- **AND** terminal processing SHALL remain ordered after every earlier admitted event

#### Scenario: Fixed bounded control capacity is exhausted
- **WHEN** a terminal or final-report control event cannot be admitted within the fixed bounded control capacity
- **THEN** the adapter SHALL explicitly disable presentation and wake its actor without another queue slot
- **AND** no producer SHALL wait for rendering or an unadmitted event's acknowledgement
- **AND** close SHALL remain available and all presentation completion waiting SHALL share the five-second budget
- **AND** the primary success, failure, timeout, or cancellation result SHALL remain unchanged

#### Scenario: Coalescing terminates with healthy presentation lifecycle
- **WHEN** a host step completes with a pending diagnostic group
- **THEN** readers SHALL finish safe finalization, any upstream dispatcher SHALL drain, and heartbeat production SHALL stop/join before terminal admission
- **AND** the actor SHALL process earlier admitted events and finalize the group before terminal rendering
- **AND** it SHALL remain available for the next successful host step
- **WHEN** the session ends after any facade final failure report is submitted
- **THEN** close SHALL end acceptance without requiring a queue slot, even if the inbox is full
- **AND** the actor SHALL drain, finalize, acknowledge completion, and join before native output or return within the shared completion budget
- **AND** no output SHALL occur after successful completion acknowledgement

#### Scenario: Presentation cannot finish within its budget
- **WHEN** a renderer write or flush stalls during assembly output or session completion
- **THEN** readers SHALL continue draining and retaining safe bounded diagnostics independently of the actor
- **AND** the facade SHALL stop waiting after one shared five-second completion budget without restarting it on repeated cleanup
- **AND** it SHALL cancel subsequent rendering without retrying output synchronously to the same stream
- **AND** a daemon thread and its already-started write MAY outlive return, with cancellation checked before later renderer operations
- **AND** existing subprocess deadline and container/staging cleanup guarantees SHALL remain unchanged

#### Scenario: Retained context repeats earlier live output
- **WHEN** assembly fails after some or all retained diagnostics were already shown live
- **THEN** the final report SHALL still contain the existing nonempty bounded tail once with a retained-context repeat warning
- **AND** no delivery hash, occurrence receipt, or live-history filtering SHALL be required
- **AND** drops, earlier identical diagnostics, numeric replacement, and tail truncation SHALL NOT cause the report to hide retained context
- **AND** an empty structured tail SHALL remain empty rather than being replaced with the summary

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
- **AND** SHALL reuse the existing bounded redacted diagnostic tail once per final report, explicitly labeled as retained context that may repeat live output
- **AND** renderer failure MAY prevent visible output but SHALL NOT discard the structured failure or change the primary result
- **AND** existing container and staging cleanup guarantees SHALL apply

## MODIFIED Requirements

### Requirement: Retain bounded redacted assembler diagnostics
Live display, coalescing, and refresh scenarios below describe healthy, promptly serviced presentation. Deduplication and coalescing SHALL be best effort under overload, state reset, or renderer failure: extra groups/repeated diagnostics or absent live output are permitted on those degraded paths. Normal-path coalescing SHALL remain supported and tested. Terminal-control neutralization, mode-prescribed transformation, safe SDK identity matching, capture bounds, primary execution results, and cleanup SHALL NOT be weakened by this qualification. The retained tail SHALL remain independent of live delivery and SHALL NOT be reconciled with renderer history. Every final failure report SHALL include its nonempty tail once under an explicit retained-context label warning that live output may be repeated; empty tails SHALL produce no diagnostic section.

The assembler request/API SHALL accept a typed presentation-only `network_url_display: NetworkUrlDisplay` field with the closed values `redacted`, `host-path`, and `exact`. Facade-originated calls SHALL propagate the resolved value through host materialization, Pi assembly, and assembler execution requests without converting it to a string or boolean. Direct assembler, SDK, and injected callers that omit the field SHALL resolve it to `redacted`; every supplied value outside the closed enum SHALL be rejected before execution or stream collection. The assembler SHALL fix the resolved value in stream collector configuration before consuming stdout or stderr, and it SHALL NOT change during the operation.

Semantic assembler inputs SHALL describe execution and output content, including dependency roots and lockfile, image and tool versions, platform, and equivalent content-bearing inputs. The presentation-only `network_url_display` field SHALL NOT enter `AssemblerIdentity`, `AssemblerInputIdentity`, assembled-output identity, cache hashing or equality, verification or assembler evidence, or publication decisions, and SHALL NOT change npm or Docker argv, environment or network behavior, timeouts, lifecycle events, cleanup, or primary results.

The assembler SHALL make ongoing execution observable to an authorized text-mode caller while retaining exactly one bounded diagnostic tail whose representation is selected by `network_url_display`. The selector SHALL choose `redacted`, `host-path`, or `exact` consistently for CLI live text, durable `lines`, text failure context, and JSON `host_failure.tail`. It SHALL control only local live diagnostics and retained-tail representation. Non-selected local representations SHALL NOT enter another retained tail. External SDK events and all verification or assembler evidence SHALL receive only the safe URL-free representation. Diagnostic collection SHALL NOT grow without bound for a long-running process.

In `redacted` and `host-path`, presentation SHALL surface each first safe npm warning, error, retry, timeout, or status line that is admitted to the bounded presentation mailbox. Outside recognized successful-fetch groups, those modes SHALL coalesce every sequence of identical admitted final safe rendered diagnostic lines regardless of warning, error, retry, status, or timeout classification and SHALL count admitted occurrences including the first. `exact` SHALL surface each admitted line without coalescing. Under mailbox saturation, ordinary diagnostic admission SHALL be best-effort, and a rendered repetition count SHALL NOT claim to include occurrences dropped before admission. Warnings and errors SHALL NOT bypass coalescing or be forced into individual ungrouped live writes. Typed Constructor lifecycle/control events SHALL NOT be treated as diagnostic lines or coalesced.

In `lines` mode it SHALL use a fixed one-second monotonic window, emit the first diagnostic immediately, and emit `<diagnostic> (repeated N times)` for a group total `N >= 2` at window end or immediately before a different diagnostic or terminal event; a single-occurrence group SHALL emit no summary.

In interactive mode it SHALL maintain one mutable diagnostic slot, initially without a suffix, increment the group total for every identical repeat, and update the slot at the next at-most-one-second TUI refresh using the same canonical suffix. When consecutive diagnostics have identical presentation identity and text except for exactly one changed numeric token, the latest diagnostic SHALL replace that mutable slot without a repetition suffix and reset its exact-repeat count to one; numeric values need not be monotonic.

Numeric matching SHALL examine the entire maximal numeric-looking sequence and SHALL NOT extract a valid-looking substring from an invalid sequence. A valid numeric token SHALL consist of one or more ASCII digits followed by zero or more `.` plus one-or-more-digit segments, with neither adjacent boundary an ASCII letter, digit, underscore, dot, plus, or minus. Thus `1`, `42`, `1.2`, and `10.20.30` are valid, while signed `-1`, `+1`, and `-1.2`, incomplete `.1` and `1.`, malformed `1..2`, and identifier-embedded `item1` are invalid in full and SHALL NOT participate in numeric grouping. Surrounding nonnumeric text SHALL be byte-identical. The interactive group SHALL remain mutable until an identity/template mismatch, omission notice, different diagnostic, or terminal event finalizes it; only its latest value SHALL be durably written, and a later identical diagnostic SHALL start a new group. `lines` mode SHALL NOT apply numeric-variant grouping and SHALL preserve each changed numeric value as a separate durable line.

The decoded stream SHALL undergo one bounded decoding, streaming line-accounting, and terminal-control-neutralization pipeline. While an unterminated diagnostic remains within its line bound, the collector SHALL emit committed terminal-safe prefixes promptly without waiting for newline. The resolved immutable policy SHALL feed exactly one byte-bounded per-stream retained tail with the original ordering and occurrences of the selected representation without grouping: URL-free sanitized text for `redacted`, safely rendered normalized hostname/path text for `host-path`, or terminal-safe source text for `exact`. Existing tail byte limits and truncation semantics SHALL remain unchanged. In `redacted` and `host-path`, terminal-safe source content SHALL NOT be retained. In `exact`, no projected-safe or host-path tail SHALL be retained.

The safe projector SHALL perform secret redaction followed by boundary-safe URL sanitization, normalized-host extraction, safe host-path derivation, and ephemeral URL-identity derivation whenever required by the selected local mode or an external SDK event. Each complete URL SHALL contribute an ordered fixed-width fingerprint produced by a cryptographic keyed digest with a fresh random presentation-session key. Fingerprint input SHALL remove userinfo, query, and fragment, exclude proxy information, and preserve scheme, normalized hostname, path, and any explicitly supplied port including an explicit default port. Fingerprint order and multiplicity SHALL be preserved. Pending sanitizer state for an ambiguous URL or secret candidate SHALL be capped at 8 KiB, and diagnostic-line accounting SHALL be capped at 64 KiB of decoded source text per unterminated line. The collector SHALL continue draining after either limit is reached. A candidate exceeding its limit SHALL emit only `[sanitized oversized token]` and discard its remaining content until a safe token boundary. When a diagnostic line first exceeds its limit, the collector SHALL append exactly one `[sanitized oversized diagnostic]` marker after any previously emitted committed safe prefix, SHALL NOT retract that prefix, and SHALL discard the remaining source content until newline or stream termination. A subsequent newline SHALL preserve the record boundary and resume normal processing; stream termination SHALL NOT add another overflow marker. No discarded suffix SHALL enter a live event or retained tail. At EOF, reader failure, or cancellation, an unresolved URL or secret candidate in projected processing SHALL emit only `[sanitized incomplete token]`, never ordinary candidate text, and any pending projected line SHALL be finalized only through the same bounded sanitizer.

Safe host-path facts MAY enter the selected internal live representation and `host-path` retained tail but SHALL NOT enter the external SDK DTO, verification or assembler evidence, or semantic identities. A projected-safe external SDK event SHALL be transient with respect to retained diagnostics: producing it SHALL NOT create or populate another retained tail. Session keys and fingerprints SHALL never enter rendered text, retained tails, failure reports, persistence, evidence, or policy identity and SHALL never permit cross-session correlation. Source content SHALL appear only in `exact` CLI text and text/JSON failure tails; it SHALL NOT enter external SDK events, verification evidence, assembler evidence, or semantic identities.

A recognized successful npm HTTP fetch source line SHALL use the complete npm 11.16.0 grammar `npm http fetch <METHOD> <STATUS> <URL> <ASCII digits>ms [attempt #<ASCII digits>] [(cache <OUTCOME>)]`. The parser SHALL explicitly recognize and consume the `npm http fetch` prefix; no upstream prefix stripping SHALL be assumed. Its latency SHALL consist of one or more ASCII digits immediately followed by `ms`; the dedicated parser SHALL recognize this field independently of generic numeric-token boundary rules and SHALL reject other units as an unknown shape. A bare `<METHOD> ...` line, an unrelated or malformed prefix, and the distinct `npm http cache ...` form SHALL remain ordinary diagnostics. In `redacted` mode, consecutive recognized lines that differ only by URL and millisecond latency SHALL form one request-count group keyed by method, exact successful status, optional attempt number or its absence, and cache outcome. Its canonical text SHALL omit URL identity and latency and SHALL preserve `attempt #N` when present. In `host-path` mode, a complete URL SHALL render as normalized hostname plus terminal-safe path without scheme, userinfo, port, query, or fragment; consecutive recognized fetches SHALL share a request-count group only when method, exact status, optional attempt number or its absence, cache outcome, hostname, and safe path match while latency changes. Its canonical text SHALL omit latency and preserve `attempt #N` when present. Unsafe paths SHALL use `<redacted-path>`. For both modes, interactive presentation SHALL immediately show the first canonical line in one mutable slot and update it with ` — N requests` for the admitted total without waiting for another event; `lines` SHALL emit the first line immediately and the counted form at the existing one-second window or earlier boundary. Different group-key fields—including different attempt numbers or presence versus absence of an attempt—warnings, errors, retries, timeouts, malformed diagnostics, lifecycle events, and terminal events SHALL delimit a fetch group. Counts SHALL use `requests` without an `observed` qualifier.

In `exact` mode, the constructor SHALL preserve complete source diagnostic content, including URL components, proxy details, credentials, and caller-provided secrets, as closely as decoding, line framing, and existing bounds allow. `exact` SHALL disable application content redaction, diagnostic aggregation, and numeric-variant replacement. In `redacted` and `host-path`, every prescribed transformation SHALL be mandatory. Because callers may select `exact`, application content redaction as a whole SHALL be treated as a user-selected noise/presentation facility rather than a security boundary. ANSI CSI/OSC/DCS sequences and terminal-affecting controls SHALL become inert visible text, and terminal-control neutralization and output bounds SHALL remain mandatory in every mode.

One facade presentation actor SHALL exclusively own coalescing, presentation deadlines, and host terminal writes through one ordered bounded inbox with independently bounded admission budgets for control and telemetry. Diagnostic saturation SHALL NOT consume reserved lifecycle/terminal/final-report capacity. On the internal enqueue path, short mutex-protected admission is permitted; producers SHALL NOT wait for queue capacity, I/O, rendering, or consumer acknowledgement, and no rendering, arbitrary callback invocation, completion wait, flush, or join SHALL occur under inbox/producer serialization locks. The known internal enqueue path SHALL avoid a redundant asynchronous dispatcher while arbitrary external SDK callbacks SHALL retain their existing isolation contract.

Dropped live diagnostics SHALL produce a bounded non-coalesced notice with a subsequent admitted diagnostic/control event or normal close when presentation is functioning. Counts SHALL be exact only when known; uncertainty or saturation SHALL use a lower bound or a generic omission notice. Loss accounting SHALL NOT reconstruct a transcript or alter retained tails.

A step-terminal event SHALL be admitted only after stream readers finish safe sanitizer finalization, any upstream dispatcher is drained, and heartbeat production is stopped/joined. On the healthy path the actor SHALL finalize its pending diagnostic group before rendering that terminal and SHALL persist across successful steps. Session close SHALL stop acceptance and wake the consumer independently of queue capacity, without an enqueued shutdown sentinel. Normal close SHALL drain accepted events, finalize display, acknowledge completion, and join before BuildKit or return, with no output after successful completion acknowledgement.

Control-admission failure SHALL explicitly disable presentation and wake its actor without consuming another slot or changing the primary result. Renderer exceptions SHALL discard pending display state and disable output without retrying the failed stream while the actor can still complete. All presentation completion waits and joins SHALL share one five-second monotonic budget; repeated cleanup SHALL NOT restart it. If renderer I/O remains blocked, expiry SHALL cancel further presentation and allow execution/return to continue without an unbounded wait or synchronous CLI fallback. A daemon worker or an in-flight write MAY outlive this degraded boundary, but after unblocking no further renderer operation SHALL begin after cancellation is observed. Domain cleanup, timeout, SDK data, and retained capture guarantees SHALL remain independent of this presentation-only degradation.

#### Scenario: Presentation selector reaches collection
- **WHEN** the facade resolves a valid `network_url_display` value and starts host materialization and Pi assembly
- **THEN** the same typed enum value SHALL pass through the host materialization, Pi assembly, and assembler execution requests into stream collector configuration
- **AND** the collector SHALL fix it before consuming stdout or stderr
- **AND** no intermediate DTO SHALL represent it as a string or boolean

#### Scenario: Direct caller omits the presentation selector
- **WHEN** a direct assembler, SDK, or injected caller omits `network_url_display`
- **THEN** the assembler request SHALL resolve it to `redacted` before stream collection
- **AND** only the redacted live and retained representation SHALL be selected

#### Scenario: Direct caller supplies an invalid presentation selector
- **WHEN** a direct assembler, SDK, or injected caller supplies a value outside `redacted`, `host-path`, or `exact`
- **THEN** closed validation SHALL reject the request before npm, Docker, network, container, cache, or stream-collection effects

#### Scenario: Long-running npm installation emits output
- **WHEN** npm writes stdout or stderr during authorized interactive text-mode assembly
- **THEN** warnings, errors, retries, timeouts, and status output selected by `network_url_display` SHALL become visible before process completion
- **AND** each received stdout or stderr chunk SHALL count as diagnostic activity for silence tracking and last-activity age
- **AND** the constructor SHALL retain only the selected bounded diagnostic tail

#### Scenario: Repeated retry status
- **WHEN** npm emits identical diagnostic lines of any closed diagnostic classification in `redacted` or `host-path` `lines` mode within one monotonic second
- **THEN** presentation SHALL emit the first admitted line immediately and count each admitted occurrence including the first
- **AND** a group total of at least two admitted occurrences SHALL emit `<diagnostic> (repeated N times)` at window end or before a different diagnostic or terminal event
- **AND** a single-occurrence group SHALL emit no summary
- **AND** a different diagnostic SHALL follow the flushed summary so observable order is preserved
- **AND** identical warnings, errors, and timeout diagnostics SHALL use the same coalescing rule
- **AND** typed Constructor lifecycle/control events SHALL remain individual and uncoalesced
- **AND** retained failure diagnostics SHALL receive the original ungrouped ordering and occurrences after secret redaction and URL sanitization

#### Scenario: Interactive repeated diagnostic updates one slot
- **WHEN** interactive `redacted` or `host-path` presentation receives identical repeated diagnostic lines of any closed diagnostic classification outside a recognized successful-fetch group
- **THEN** the first occurrence SHALL populate one mutable diagnostic slot without a suffix
- **AND** each admitted repeat SHALL increment the admitted-occurrence count
- **AND** the next TUI refresh, no later than one second afterward, SHALL replace that slot with `<diagnostic> (repeated N times)` for admitted total `N`
- **AND** neither the first occurrence nor any repeat SHALL create a durable line while the group remains mutable
- **AND** a different diagnostic or terminal event SHALL finalize the group with exactly one ordered durable write containing the latest total
- **AND** warning and error classification SHALL NOT change this mutable-first behavior
- **AND** a later identical diagnostic SHALL start a new group

#### Scenario: Interactive numeric diagnostic updates one slot
- **WHEN** consecutive admitted interactive `redacted` or `host-path` diagnostics outside a recognized successful-fetch group have the same phase, step, stream, classification, logical resource, ordered URL-fingerprint tuple, and rendered text except for exactly one changed numeric token
- **THEN** the latest diagnostic SHALL replace the mutable slot without a repetition suffix
- **AND** its exact-repeat count SHALL reset to one, while an exact repeat of that latest value SHALL resume canonical repetition counting
- **AND** numeric monotonicity SHALL NOT be required
- **AND** matching SHALL consume the entire maximal numeric-looking sequence and SHALL NOT extract a valid-looking substring from a signed, incomplete, malformed dot-separated, or identifier-embedded sequence
- **AND** all surrounding nonnumeric text SHALL be byte-identical
- **AND** an identity/template mismatch or omission notice SHALL finalize the group
- **AND** finalization SHALL durably write only the latest interactive value
- **AND** every received stdout/stderr chunk SHALL already have reset diagnostic silence and updated latest diagnostic activity before mailbox admission or grouping

#### Scenario: Numeric diagnostic values remain separate in lines mode
- **WHEN** `redacted` or `host-path` `lines` mode receives diagnostics outside a recognized successful-fetch group that differ in exactly one numeric token
- **THEN** each changed numeric value SHALL be emitted as its own durable line
- **AND** only exact repeats SHALL use the existing one-second repetition window

#### Scenario: Hidden URLs remain distinct without disclosure
- **WHEN** ordinary diagnostics outside a recognized successful-fetch group have the same rendered safe text but contain different sanitized URL identities
- **THEN** their ordered session-keyed fingerprint tuples SHALL keep their exact-repeat and numeric-update groups distinct
- **AND** recognized successful-fetch grouping SHALL instead follow the selected `redacted` or `host-path` group key
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
- **AND** a failure SHALL include the bounded diagnostic tail selected for that channel and display policy

#### Scenario: URL spans diagnostic chunks
- **WHEN** a URL, credential, secret, or disallowed URL component spans decoder or input-chunk boundaries
- **THEN** the collector SHALL withhold an ambiguous token prefix only within the 8 KiB pending-sanitizer limit until it can be sanitized
- **AND** SHALL NOT flush an unresolved candidate as ordinary text
- **AND** no complete or partial disallowed URL component, credential, or secret SHALL enter a `redacted` or `host-path` retained tail or an external structured event
- **AND** existing retained-tail byte bounds and truncation behavior SHALL remain unchanged

#### Scenario: Arbitrarily long URL has no terminating delimiter
- **WHEN** an arbitrarily long URL-shaped candidate arrives across any number of chunks without a terminating delimiter
- **THEN** pending sanitizer state SHALL remain bounded at 8 KiB while input continues draining
- **AND** the collector SHALL emit only `[sanitized oversized token]` for the candidate and discard its remaining content until a safe token boundary
- **AND** processing SHALL recover after that boundary
- **AND** no candidate fragment SHALL enter an external structured event or a `redacted` or `host-path` retained tail

#### Scenario: Arbitrarily long diagnostic has no newline
- **WHEN** an arbitrarily long diagnostic arrives without a newline
- **THEN** diagnostic-line accounting SHALL remain bounded at 64 KiB while input continues draining
- **AND** committed terminal-safe prefixes within the bound SHALL remain eligible for prompt live delivery and selected-tail retention without waiting for newline
- **AND** when the diagnostic first exceeds the bound, the collector SHALL append exactly one `[sanitized oversized diagnostic]` marker without retracting any previously emitted committed safe prefix
- **AND** the collector SHALL discard the remaining source content until newline or stream termination
- **AND** a subsequent newline SHALL preserve the record boundary and processing SHALL recover after it
- **AND** stream termination SHALL NOT emit a second overflow marker
- **AND** no discarded suffix SHALL enter a structured live event or retained tail

#### Scenario: Stream terminates with an ambiguous sensitive prefix
- **WHEN** EOF, reader failure, or cancellation occurs while an ambiguous URL, encoded URL, credential, or secret prefix or an unterminated diagnostic line is pending
- **THEN** unresolved sensitive candidates SHALL be replaced only with `[sanitized incomplete token]` and SHALL NOT be flushed as ordinary text
- **AND** any pending diagnostic line SHALL be finalized only through bounded secret redaction and URL sanitization
- **AND** pending sanitizer and line-assembly state SHALL remain within their fixed limits
- **AND** no complete or partial URL, credential, or secret SHALL enter an external structured event or a `redacted` or `host-path` retained tail

#### Scenario: Diagnostic contains network configuration
- **WHEN** assembler output contains a configured proxy endpoint, trust path, credential, or disallowed URL component
- **THEN** `redacted` and `host-path` displayed, returned, SDK, evidence, and persisted safe representations SHALL apply every transformation prescribed by the selected channel and mode
- **AND** `exact` CLI text and text/JSON failure tails SHALL preserve source content except for terminal-control neutralization and bounds
- **AND** external SDK diagnostics SHALL remain secret-redacted and URL-free independently of local output configuration

#### Scenario: Research successful-fetch fixture is recognized end to end
- **WHEN** collection receives `npm http fetch GET 200 https://registry.npmjs.org/npm-http-research-fixture/-/npm-http-research-fixture-1.0.0.tgz 15ms (cache miss)` from `docs/research/npm-http-logging.md` and compatible complete raw fetch lines
- **THEN** the collector and parser SHALL recognize the full `npm http fetch` source-line grammar without an implicit prefix-stripping prerequisite
- **AND** `redacted` presentation SHALL aggregate compatible lines without URL or latency
- **AND** `host-path` presentation SHALL aggregate compatible lines only when their safe normalized hostname and path match
- **AND** `exact` presentation SHALL preserve every complete terminal-safe source line without aggregation

#### Scenario: Similar npm forms remain ordinary diagnostics
- **WHEN** collection receives the research line `npm http cache npm-http-research-fixture@https://registry.npmjs.org/npm-http-research-fixture/-/npm-http-research-fixture-1.0.0.tgz 0ms (cache hit)`, a bare `<METHOD> ...` form, an unrelated prefix, or a malformed `npm http fetch` near-match
- **THEN** the successful-fetch parser SHALL reject it conservatively
- **AND** presentation SHALL retain it as an ordinary diagnostic rather than a request-count group member

#### Scenario: Redacted interactive fetches accumulate
- **WHEN** interactive `redacted` presentation admits consecutive successful npm fetch lines that differ by URL and `[0-9]+ms` latency but share method, exact status, optional attempt number or its absence, and cache outcome
- **THEN** the first event SHALL immediately show the canonical `<redacted>` line without latency and with `attempt #N` when present
- **AND** every later member SHALL join the same request-count group and replace that slot in place with ` — N requests` using the admitted total

#### Scenario: Attempt identity changes
- **WHEN** otherwise compatible recognized successful npm fetch lines differ by attempt number or by presence versus absence of `attempt #N`
- **THEN** they SHALL enter separate request-count groups
- **AND** each canonical line SHALL preserve its own `attempt #N` when present

#### Scenario: Unsupported latency unit is received
- **WHEN** an otherwise similar npm HTTP line uses `mks`, `s`, `m`, `h`, or any latency unit other than `ms`
- **THEN** it SHALL remain an ordinary unaggregated diagnostic rather than a recognized successful-fetch group member

#### Scenario: Host-path latency variants accumulate
- **WHEN** interactive `host-path` presentation admits consecutive successful npm fetch lines for the same normalized hostname, safe path, and optional attempt number or its absence whose `[0-9]+ms` latency values differ
- **THEN** the first event SHALL immediately show the hostname/path canonical line without latency and with `attempt #N` when present
- **AND** each later member SHALL replace that slot in place with ` — N requests` using the admitted total

#### Scenario: Host-path resources differ
- **WHEN** `host-path` mode receives successful npm fetch diagnostics with different normalized hostnames or safe paths
- **THEN** each output SHALL identify its normalized hostname and safe path without scheme, userinfo, port, query, or fragment
- **AND** the diagnostics SHALL enter separate request-count groups

#### Scenario: Exact output preserves content but not terminal effects
- **WHEN** `exact` mode receives a bounded diagnostic containing URL credentials, query values, fragments, proxy details, caller-supplied secrets, or terminal controls
- **THEN** CLI live text and text/JSON failure tails SHALL preserve content while rendering terminal controls inert
- **AND** SHALL NOT aggregate or apply numeric-variant replacement to the diagnostic

#### Scenario: Revealing modes do not alter SDK or evidence
- **WHEN** `host-path` or `exact` is selected while diagnostics are delivered to an external SDK sink or recorded in verification or assembler evidence
- **THEN** those channels SHALL receive no source URL, path, credential, query, fragment, proxy detail, or caller-supplied secret
- **AND** evidence and assembler/input/output identities SHALL remain unchanged by the presentation selector

#### Scenario: Presentation-only requests remain semantically equivalent
- **WHEN** three otherwise equivalent assembler requests differ only by `network_url_display = redacted`, `host-path`, or `exact`
- **THEN** their assembler identity, assembler-input identity, assembled-output identity, cache lookup and reuse, evidence bytes or semantic evidence content, npm and Docker argv, publication, lifecycle events, and primary result SHALL be identical
- **AND** only local live diagnostics and the single selected retained tail MAY differ
- **AND** with or without an external SDK sink, exactly one local retained representation SHALL be stored and SDK events SHALL remain transient, redacted, and URL-free

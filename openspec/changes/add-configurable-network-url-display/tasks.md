# Implementation Contract

This checklist is the binding implementation contract for this change. A phase is complete only when all of its tasks are checked and its deliverables exist. Within every phase, work SHALL proceed in `RED → GREEN → INTROSPECT → VALIDATE` order. A phase MAY begin only after all phases named in its `Depends on` line are complete. Dependencies SHALL point only to earlier phases.

## 1. Local Output Policy

**Depends on:** none.

**Deliverables:** a closed `NetworkUrlDisplay` model; aggregate local-schema support for `[output].network_url_display`; typed presentation-only propagation from facade through every host-side assembly request to stream collector configuration; `redacted` defaults for local and direct callers; no semantic identity inputs; this project's local configuration migrated to `network_url_display = "host-path"`.

- [x] 1.1 **RED:** Add focused local parsing tests for `redacted`, `host-path`, and `exact`, and verify each test fails because the enum is not implemented.
- [x] 1.2 **RED:** Add focused local validation tests for the absent-value `redacted` default, non-string values, values outside the closed set, and ordinary rejection of undeclared `[output]` fields; verify each new case fails for the intended missing behavior.
- [x] 1.3 **RED:** Add propagation tests asserting one `NetworkUrlDisplay` enum field named `network_url_display` flows through facade resolution, host materialization request, Pi assembly request, assembler execution request, and stream collector configuration; verify the assertions fail at the first missing DTO boundary.
- [x] 1.4 **RED:** Add direct assembler, SDK, and injected-caller request tests for omitted-value `redacted` default and closed rejection of invalid values before effects; verify the missing request/API behavior fails.
- [x] 1.5 **RED:** Add request-model tests proving the presentation field is absent from reviewed/effective projections and cannot be supplied by CLI, environment, or container input; verify the new host-only boundary assertions fail before implementation.
- [x] 1.6 **GREEN:** Implement the closed `NetworkUrlDisplay` model and `[output]` schema composition; verify the parsing, default, type, value, and unknown-field tests from 1.1–1.2 pass.
- [x] 1.7 **GREEN:** Add the same enum-typed `network_url_display` field to each host materialization and assembly DTO and bind it immutably into collector configuration before stream capture; verify the end-to-end propagation tests from 1.3 pass without string or boolean conversion.
- [x] 1.8 **GREEN:** Implement `redacted` defaulting and closed validation at direct assembler, SDK, and injected entry points; verify the tests from 1.4 pass before npm, Docker, network, cache, container, or stream effects.
- [x] 1.9 **GREEN:** Preserve reviewed/effective/container and alias exclusions while permitting only host-side request propagation; verify the boundary tests from 1.5 pass.
- [x] 1.10 **GREEN:** Change this project's local configuration once from `show_network_hosts` to `network_url_display = "host-path"`; verify the project-local companion parses under the new closed schema without compatibility or generalized migration logic.
- [x] 1.11 **INTROSPECT:** Audit every intermediate DTO plus serialization, equality, hashing, cache-key, reviewed projection, and effective projection path touched by the selector; add a regression assertion for each discovered path and verify the enum reaches collection but never becomes a semantic input.
- [x] 1.12 **VALIDATE:** Run the focused local-configuration, aggregate-schema, propagation, direct-caller, projection, request-model, alias-exclusion, and DTO-type suites with this project's migrated local companion in place; verify its resolved policy is `host-path` and record the exact commands and passing results in the change verification evidence.

## 2. Terminal-Safe Diagnostic Framing

**Depends on:** Phase 1.

**Deliverables:** one bounded incremental decoder/line framer and terminal-control neutralizer shared by every display mode; deterministic inert output for controls and overflow.

- [ ] 2.1 **RED:** Add parameterized incremental tests for fragmented CSI, OSC, DCS/control strings, CR, backspace, NUL, and remaining C0/C1 controls; verify the new safety cases fail before the neutralizer exists.
- [ ] 2.2 **RED:** Add incremental tests for ordinary Unicode and tab preservation, incomplete control sequences, oversized control sequences, arbitrary chunk boundaries, and EOF finalization; verify each test fails for the intended missing behavior.
- [ ] 2.3 **RED:** Add line-framing tests for the 64 KiB unterminated-line bound, overflow marker, continued draining, recovery after newline, cancellation, and reader failure; verify the missing bounded behavior causes the focused tests to fail.
- [ ] 2.4 **GREEN:** Implement incremental terminal-control neutralization with visible inert escapes and fail-closed incomplete/oversized markers; verify the tests from 2.1–2.2 pass under every tested chunk split.
- [ ] 2.5 **GREEN:** Integrate the neutralizer with bounded UTF-8 decoding and line framing while preserving continued draining and recovery; verify the tests from 2.3 pass.
- [ ] 2.6 **INTROSPECT:** Review all mode-selectable and JSON paths for any route around neutralization or line bounds; add a regression test for each route found and verify no tested sequence can move the cursor, rewrite prior content, or alter title, clipboard, or screen state.
- [ ] 2.7 **VALIDATE:** Run the focused decoder, framer, neutralizer, stream-finalization, cancellation, and bounded-memory suites; record the exact commands and passing results in the change verification evidence.
- [ ] 2.8 **CORRECTIVE RED:** Restore and add incremental tests for prompt committed safe-prefix delivery without newline, exactly-at-limit output, prefix-plus-one-marker overflow, immediate overflow notification, suffix discard, stream termination without a duplicate marker, and newline recovery through both collector paths; verify the staged atomic-line behavior fails the restored no-newline and appended-marker expectations.
- [ ] 2.9 **CORRECTIVE GREEN:** Replace atomic whole-line buffering with bounded streaming source-line accounting that preserves terminal neutralization, mode-prescribed redaction or projection, prompt committed-prefix delivery, exactly one appended `[sanitized oversized diagnostic]`, suffix discard through newline or stream termination, and one selected bounded tail; verify the tests from 2.8 pass.
- [ ] 2.10 **CORRECTIVE INTROSPECT:** Audit internal and external live sinks, selected retained tails, transient SDK projection, text/JSON failure paths, cancellation, reader failure, and overflow during a pending control sequence; add regressions proving committed prefixes obey mode disclosure rules, pending ambiguous control content and discarded suffixes never escape, and complete-line parsing or grouping does not force stream capture to wait for newline.
- [ ] 2.11 **CORRECTIVE VALIDATE:** Run the focused decoder, streaming line-accounting, neutralizer, no-newline delivery, overflow, stream-finalization, cancellation, reader-failure, retained-tail, SDK-isolation, text/JSON failure, and consumer suites; restore the established no-newline regression and update change verification evidence to remove atomic-replacement claims and record exact passing commands and results.

## 3. Safe URL and Host-Path Projection

**Depends on:** Phase 2.

**Deliverables:** one bounded safe projector producing URL-free SDK text, normalized host facts, canonical safe host/path facts, and ephemeral URL identities without widening public DTOs.

- [ ] 3.1 **RED:** Add projector tests for normalized hostnames and canonical encoded paths, including scoped package paths; verify they fail because safe host/path projection is absent.
- [ ] 3.2 **RED:** Preserve passing confidentiality regressions proving the current projector excludes scheme, userinfo, explicit port, query, fragment, proxy detail, credentials, and caller secrets, then add assertions for the missing bounded safe host-path representation; verify only the new host-path assertions fail before implementation.
- [ ] 3.3 **RED:** Add boundary tests for percent-encoded controls, ambiguous or secret-bearing paths, the 8 KiB pending-token bound, incomplete tokens, oversized tokens, and `<redacted-path>` fallback; verify the intended markers and recovery assertions fail before implementation.
- [ ] 3.4 **RED:** Add API-shape tests proving plaintext paths and internal host/path facts cannot enter external SDK fields, verification or assembler evidence, or persistence while canonical URL paths remain part of keyed fingerprint input; preserve passing regressions that different paths produce different opaque fingerprints within one session and that no fingerprint exposes plaintext path, then verify only the missing internal host/path representation assertions fail.
- [ ] 3.5 **GREEN:** Implement normalized bounded host/path derivation and fail-closed path fallback in the safe projector; verify the tests from 3.1–3.3 pass.
- [ ] 3.6 **GREEN:** Keep plaintext host/path facts internal while preserving URL-free projected SDK text and existing path-sensitive opaque fingerprint identity; verify the API-shape, confidentiality, same-session different-path fingerprint, and no-plaintext-fingerprint tests from 3.2 and 3.4 pass.
- [ ] 3.7 **INTROSPECT:** Trace projected fields from collection through internal events, callbacks, evidence, persistence, logs, and exception formatting; add regression assertions for every boundary and verify only approved local presentation paths can observe safe paths.
- [ ] 3.8 **VALIDATE:** Run the focused URL sanitizer, projector, confidentiality, fingerprint, SDK-shape, evidence, and persistence suites; record the exact commands and passing results in the change verification evidence.

## 4. Successful npm Fetch Parsing and Identity

**Depends on:** Phase 3.

**Deliverables:** a conservative npm 11.16.0 successful-fetch parser; policy-specific group identities and canonical text; unknown forms preserved as ordinary diagnostics.

- [ ] 4.1 **RED:** Add parser tests for the complete source-line grammar `npm http fetch <METHOD> <STATUS> <URL> <ASCII digits>ms [attempt #<ASCII digits>] [(cache <OUTCOME>)]`, including the actual cache-miss line from `docs/research/npm-http-logging.md`, `0ms`, ordinary values, large integer values, optional attempts, and optional cache outcomes; verify extraction tests fail before the parser exists.
- [ ] 4.2 **RED:** Add rejection tests for unrelated or malformed prefixes, bare `<METHOD> ...` lines lacking `npm http fetch`, non-2xx statuses, retry/failure forms, missing URLs, malformed attempts, malformed latency, `mks`, `s`, `m`, `h`, and unknown npm variants; include the research fixture's distinct `npm http cache ...` line and verify every rejected form remains an ordinary diagnostic.
- [ ] 4.3 **RED:** Add identity tests proving `redacted` excludes URL and latency, `host-path` includes normalized hostname/path but excludes latency, and both modes include method, exact status, cache outcome, and attempt presence/value; verify policy-specific identities are unavailable before implementation.
- [ ] 4.4 **GREEN:** Implement explicit recognition and consumption of the `npm http fetch` prefix followed by conservative successful-fetch field parsing; verify the real-output acceptance and near-match rejection tests from 4.1–4.2 pass.
- [ ] 4.5 **GREEN:** Implement redacted and host-path fetch identities and canonical text, preserving `attempt #N` while omitting latency; verify the identity tests from 4.3 pass.
- [ ] 4.6 **INTROSPECT:** Fuzz or table-drive token boundaries, very large integers, optional-clause ordering, malformed near-matches, and URL sanitizer interaction; verify no rejected form is partially parsed or accidentally grouped.
- [ ] 4.7 **VALIDATE:** Run the focused npm parser, identity, canonical-rendering, URL-projection, and malformed-input suites; record the exact commands and passing results in the change verification evidence.

## 5. Policy-Aware Live Presentation

**Depends on:** Phases 1 and 4.

**Deliverables:** correct redacted and host-path request-count grouping; exact unaggregated presentation; unchanged ordinary repeat and numeric-variant behavior outside fetch groups.

- [ ] 5.1 **RED:** Add interactive redacted tests for immediate first-slot display, admitted totals including the first request, in-place `— N requests` updates, and no `observed` qualifier; verify the state transitions fail before fetch grouping is implemented.
- [ ] 5.2 **RED:** Add interactive host-path tests proving latency variants for one hostname/path aggregate while different hostnames, paths, attempts, methods, statuses, and cache outcomes delimit groups; verify the missing grouping behavior fails.
- [ ] 5.3 **RED:** Add boundary tests proving warning, error, retry, timeout, malformed diagnostic, omission notice, lifecycle event, and terminal event finalize the current fetch group in order; verify finalization assertions fail before implementation.
- [ ] 5.4 **RED:** Add `lines` tests for immediate first output, one counted summary at the fixed one-second window or earlier boundary, and a fresh group after deadline finalization; verify the durable-output assertions fail before implementation.
- [ ] 5.5 **RED:** Add exact-mode tests proving every admitted terminal-safe source line remains visible and exact-repeat, fetch, and numeric-variant aggregation are disabled; verify the source-preservation assertions fail before mode selection is implemented.
- [ ] 5.6 **GREEN:** Implement interactive redacted and host-path request-count state in the existing mutable diagnostic slot; verify the tests from 5.1–5.3 pass without durable intermediate duplicates.
- [ ] 5.7 **GREEN:** Implement the `lines` request-count window and ordered boundary flush; verify the tests from 5.4 pass.
- [ ] 5.8 **GREEN:** Implement exact live selection and aggregation bypass; verify the tests from 5.5 pass.
- [ ] 5.9 **INTROSPECT:** Re-run ordinary exact-repeat and numeric-variant cases outside recognized fetch groups in redacted and host-path modes, plus omission accounting under dropped admissions; add regressions for any interaction and verify counts never claim producer events dropped before admission.
- [ ] 5.10 **VALIDATE:** Run the focused interactive state, durable `lines`, grouping, ordinary-repeat, numeric-variant, exact-mode, and ordering suites; record the exact commands and passing results in the change verification evidence.

## 6. Single Mode-Selected Retained Tail and Channel Isolation

**Depends on:** Phases 1, 3, and 5.

**Deliverables:** exactly one bounded retained tail per stream selected from the assembler request before collection; matching text/JSON failure output; transient URL-free SDK delivery with and without a sink; no retained non-selected representation; semantic equivalence across selector values.

- [ ] 6.1 **RED:** Add an assembler-request retention test for `redacted` proving the tail contains ordered URL-free sanitized diagnostics and retains no source-safe or host-path representation; verify it fails before selected retention is implemented.
- [ ] 6.2 **RED:** Add an assembler-request retention test for `host-path` proving the tail contains ordered safe hostname/path diagnostics, including `<redacted-path>` fallback, and retains no source-safe or duplicate redacted tail; verify it fails before selected retention is implemented.
- [ ] 6.3 **RED:** Add an assembler-request retention test for `exact` proving the tail preserves bounded terminal-safe source URLs, credentials, proxy details, queries, fragments, and caller secrets while retaining no projected-safe or host-path tail; verify it fails before selected retention is implemented.
- [ ] 6.4 **RED:** Repeat the three assembler-request retention cases without an external SDK sink and assert exactly one selected representation is stored; verify the matrix fails before collector selection is implemented.
- [ ] 6.5 **RED:** Repeat the three assembler-request retention cases with an external SDK sink and assert exactly one local representation is stored while each SDK event is transient, redacted, URL-free, and path-free; verify the matrix fails before channel isolation is implemented.
- [ ] 6.6 **RED:** Add text failure-context tests for nonempty, empty, truncated, timeout, interruption, reader-failure, and already-shown-live tails in each request mode; verify mode-selected rendering and one-time retained-context labeling fail before implementation.
- [ ] 6.7 **RED:** Add JSON `host_failure.tail` tests for the same request-mode matrix, asserting one valid JSON document and no live sink; verify mode-selected JSON content fails before implementation.
- [ ] 6.8 **RED:** Construct three otherwise equivalent assembler requests differing only by `network_url_display`; assert identical assembler identity, assembler-input identity, assembled-output identity, cache lookup/reuse, evidence bytes or semantic content, npm/Docker argv, publication, lifecycle events, cleanup, and primary result; verify only the missing invariance assertions fail.
- [ ] 6.9 **GREEN:** Bind the request selector immutably before stream capture and feed exactly one byte-bounded retained buffer with the selected local representation; verify the retention matrices from 6.1–6.4 pass.
- [ ] 6.10 **GREEN:** Produce projected-safe SDK events transiently without retaining them separately; verify the with-sink matrix from 6.5 passes.
- [ ] 6.11 **GREEN:** Use the selected retained buffer for text failure context and JSON `host_failure.tail`; verify the channel tests from 6.6–6.7 pass.
- [ ] 6.12 **GREEN:** Exclude the presentation field from semantic execution and identity paths; verify the three-request invariance test from 6.8 passes without normalizing away the value needed by collector configuration.
- [ ] 6.13 **INTROSPECT:** Instrument or inspect retained-buffer construction and ownership for all three request modes, with and without an SDK sink; add assertions proving exactly one buffer receives diagnostic payloads and verify source content is never retained in non-exact modes.
- [ ] 6.14 **INTROSPECT:** Trace the three equivalent requests through execution, cache, evidence, publication, lifecycle, and result paths; add regressions for every observed selector dependency and verify local live/retained diagnostics are the only permitted differences.
- [ ] 6.15 **VALIDATE:** Run the focused request propagation, retention, text failure, JSON failure, SDK isolation, evidence, identity, cache, argv, publication, lifecycle, result, truncation, and bounded-memory suites; record the exact commands and passing results in the change verification evidence.

## 7. Presentation Concurrency and Failure Isolation

**Depends on:** Phases 5 and 6.

**Deliverables:** unchanged nonblocking producer and control guarantees under all display policies; bounded shutdown and primary-result isolation; high-volume acceptance coverage.

- [ ] 7.1 **RED:** Add saturation tests for diagnostic admission, omission notices, control-event headroom, and accepted-event ordering in every mode; verify any missing mode coverage fails before integration changes.
- [ ] 7.2 **RED:** Add renderer-exception and blocked-write tests for presentation disablement, no synchronous fallback, the shared five-second completion budget, and no post-cancellation renderer operation; verify missing guarantees fail.
- [ ] 7.3 **RED:** Add close/drain tests proving stream finalization, pending-group finalization, terminal ordering, persistent actor reuse across steps, and no output after successful completion acknowledgement; verify missing mode-specific behavior fails.
- [ ] 7.4 **RED:** Feed complete raw `npm http fetch ...` lines based on the successful cache-miss fixture in `docs/research/npm-http-logging.md` through decoding, collection, projection, parser, identity, and presentation; verify compatible lines aggregate without URL or latency in `redacted`, aggregate only by safe hostname/path in `host-path`, and remain complete terminal-safe unaggregated source lines in `exact`.
- [ ] 7.5 **RED:** Add bounded high-volume acceptance tests for redacted interactive counts, redacted `lines` summaries, host-path same-resource aggregation and resource separation, exact unaggregated output, and one selected retained tail; verify the end-to-end cases fail before final wiring.
- [ ] 7.6 **GREEN:** Integrate policy-aware events and retained-tail selection without changing mailbox admission or control capacity; verify the saturation and ordering tests from 7.1 pass.
- [ ] 7.7 **GREEN:** Wire complete raw npm lines through collection and mode-specific presentation; verify the real-output integration matrix from 7.4 passes.
- [ ] 7.8 **GREEN:** Isolate renderer failure and bounded completion from execution and retained capture; verify the tests from 7.2–7.3 pass.
- [ ] 7.9 **GREEN:** Complete high-volume mode wiring with bounded memory and nonblocking producers; verify the acceptance tests from 7.5 pass.
- [ ] 7.10 **INTROSPECT:** Audit locks, queue operations, renderer calls, callback invocation, joins, and cancellation checks; add a regression assertion for every blocking or ordering risk found and verify no producer waits for capacity, rendering, I/O, callback completion, or consumer acknowledgement.
- [ ] 7.11 **INTROSPECT:** Force presentation saturation and renderer failure during successful, failed, timed-out, and interrupted assembly; verify the primary result, domain cleanup, SDK delivery, and selected retained tail remain independent of presentation failure.
- [ ] 7.12 **VALIDATE:** Run the focused raw-npm integration, actor, mailbox, saturation, renderer-failure, blocked-I/O, close/drain, timeout, interruption, and high-volume acceptance suites; record the exact commands and passing results in the change verification evidence.

## 8. Documentation and Project Configuration

**Depends on:** Phases 1 and 6.

**Deliverables:** accurate mode, disclosure, channel, and safety documentation plus an updated local example, without generalized migration behavior.

- [ ] 8.1 **RED:** Add or update documentation/example assertions for the three closed values and the absent-value `redacted` default; verify the assertions fail against the current documents and example.
- [ ] 8.2 **RED:** Add documentation assertions for host-path disclosure, exact credential/secret disclosure, mandatory terminal safety and bounds, text/JSON channel behavior, one selected retained tail, typed presentation-only propagation, direct-caller `redacted` default, SDK/evidence/identity exclusions, and operator responsibility; verify each missing statement is reported.
- [ ] 8.3 **GREEN:** Update the local example for `network_url_display`; verify example checks pass without compatibility or generalized migration logic.
- [ ] 8.4 **GREEN:** Update host-build-output documentation with the required mode semantics, typed host-side propagation path, direct-caller `redacted` default, disclosures, channel matrix, retention model, semantic-identity exclusions, and safety boundaries; verify the documentation assertions from 8.1–8.2 pass.
- [ ] 8.5 **INTROSPECT:** Search examples and user documentation for stale boolean-setting guidance, claims of multiple retained tails, or claims that exact is secret-safe; resolve every match and verify remaining historical references are limited to the explicit one-time project configuration replacement.
- [ ] 8.6 **VALIDATE:** Run documentation tests and parse every shipped example; record the exact commands and passing results in the change verification evidence.

## 9. Release Integration

**Depends on:** Phases 7 and 8.

**Deliverables:** passing cross-capability acceptance matrix; no unresolved contract gaps; complete external verification evidence; strict OpenSpec validation.

- [ ] 9.1 **RED:** Add a cross-capability acceptance matrix covering all three policies across interactive text, noninteractive `lines`, text failure context, JSON failure tails, simultaneous SDK delivery, success, ordinary failure, timeout, and interruption; run it and record every unsatisfied integration case before making release fixes.
- [ ] 9.2 **RED:** Run the complete project typecheck, lint/static checks, unit/integration tests, and build once; record every pre-release failure before making release fixes.
- [ ] 9.3 **GREEN:** Fix only integration defects exposed by 9.1 while preserving the completed phase contracts; verify the entire cross-capability matrix passes.
- [ ] 9.4 **GREEN:** Fix regressions exposed by 9.2 without weakening tests or specifications; verify typecheck, lint/static checks, the complete test suite, and build all pass.
- [ ] 9.5 **INTROSPECT:** Trace every requirement and scenario in the delta specs to at least one completed task and automated test or explicit observable verification; document and close every uncovered requirement in the change verification evidence.
- [ ] 9.6 **INTROSPECT:** Review the final diff for forbidden SDK/evidence disclosure, non-selected retention, semantic identity changes, compatibility behavior, unbounded state, or late-to-early phase dependency violations; search for and remove every stale claim that `network_url_display` cannot traverse host materialization or assembler requests, and verify the selector appears in no identity, evidence, cache-semantic, or container-input path.
- [ ] 9.7 **VALIDATE:** Re-run all focused and full-project checks from clean state and record exact commands, versions, and results outside the specification artifacts.
- [ ] 9.8 **VALIDATE:** Run `openspec validate add-configurable-network-url-display --strict` and verify it passes before marking implementation complete.

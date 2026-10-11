# Design

## Context

See proposal.md for motivation and the agreed supported-scheme/secret contract. The current implementation combines persistent percent transducers with projector-owned pending text, but `IncrementalRecognizer.consume` rebuilds from a duplicate `_tail`, and projector prefix/secret/reset paths also re-feed retained suffixes. Previous feed-only work probes did not verify consumption or cross-instance cumulative work. P3 remains reopened.

The revised decision is not two independent filters: literal secrets can cross Text/URL boundaries, and URL starts can overlap secrets. Recognition is coordinated over source coordinates; secret replacement must not invent a different valid URL. Existing local `exact` content policy, one selected retained tail, 8 KiB pending limit, 64 KiB line limit and five-second presentation shutdown budget remain binding.

## Goals / Non-Goals

**Goals:**
- Recognize only the fixed supported schemes, including starts within longer strings, using bounded streaming states.
- Preserve overlapping URL/secret continuations and current prefix delivery without replay or duplicate raw buffers.
- Separate safe rendering markers from validated metadata; apply the same fact gates to normal, EOF, structured and fetch routes.
- Establish cumulative deterministic work and ownership evidence across commits and resets.

**Non-Goals:**
- Arbitrary-scheme conservative hiding, decoded secret matching outside recognized URLs, or secret rejection for token-boundary crossings.
- General URI parsing, new public candidate DTOs or persistence, selectable legacy fallback, or globally changed transport/hostname validation.
- Whole-pipeline linearity or delayed secret commitment until a delimiter.

## Decisions

### 1. Fixed scheme automaton with raw provenance

Build a case-insensitive fixed-prefix automaton for exactly the schemes in proposal.md and then the complete `://` separator. Failure/suffix transitions preserve all viable supported starts, so `TOKENhttps://host` exposes a start at `https`, not an UNKNOWN start at `TOKEN`. Earliest selection is among supported starts only. Ordinary text containing no supported start does not become an arbitrarily long scheme candidate.

Keep literal and encoded structural detection at the existing fixed depth. Use bounded percent transducers carrying inclusive raw start/exclusive raw end coordinates; virtual outputs are consumed immediately, not materialized as whole search layers. Effective separator depth four does not authorize a fourth scheme/whole-URL interpretation pass. Unsupported or invalid decoded schemes remain text unless another supported start exists inside them.

Internal typed outcomes distinguish no match, an ambiguous supported prefix and a detected URL awaiting its raw end. Coordinates are absolute Python character offsets, not UTF-8 byte or decoded offsets. A candidate is not proof of validity. Remove the arbitrary-scheme KNOWN/UNKNOWN recognition obligation; compatibility helpers, if temporarily retained, must not make UNKNOWN a detected URL or provide facts.

Alternative: arbitrary conservative schemes plus a whitelist only at metadata time. Rejected by the revised user contract. Searching fixed prefixes only at token starts is also rejected: it would lose the agreed embedded `https` example.

### 2. Parallel secret automata and overlapping states

Implement an internal dependency-free Aho–Corasick core in a separate prerequisite phase P1A. Compile registered non-empty literal patterns into a shared immutable trie with breadth-first failure links and output links; avoid duplicating complete inherited output lists at each node. Registration storage depends on configured pattern size, not stream length. Each stream owns a separate cursor containing the current node, character offset and cumulative work counters, never source payload. Per-symbol advancement emits pattern identities and exclusive Python-character match ends, including overlapping matches. Search work is O(N + emitted matches); count failure traversals and output-link work as well as direct transitions, retaining lifetime counters across cursor resets.

P1A lands core unit/differential tests, Unicode/raw-character coordinate tests, independent shared-trie cursors, partition invariance, adversarial suffix patterns, ownership/complexity documentation and its own successful gate before P2 integration. P2 adapts the core to fixed-scheme recognition and secret matching; percent provenance, leftmost/longest secret selection, component coverage and marker coordination belong to the integration, not the generic core. Preserve existing secret selection and marker ordering; the deliberate marker change is `<secret>` for standalone secret replacement.

Alternative: an external Aho–Corasick library. Not selected: the core is small, requires inspectable per-symbol continuation and deterministic counters, and a library would not replace the custom URL/secret/provenance coordinator. No new runtime dependency is approved; a future library substitution needs separate API, licensing, ownership and work verification.

Literal secret recognition reads the original stream across text/URL boundaries, including raw terminators. URL recognition continues independently while a secret is pending or matched. A secret match does not erase URL suffix/failure states. Scheme-prefix suffixes of registered secrets can be precomputed as state transitions for coordination, but they are not strings to append to another raw buffer. A `TOKENht` match leaves the live `ht` URL-prefix state able to accept new `tps://host` without replay.

Inside recognized URL spans, also match configured secrets against literal content and each of the first three permitted percent-decoded views, carrying source provenance back to component ranges. Outside recognized URLs only literal secret matching applies. The bounded scheme/separator lookahead must preserve the decoded secret state needed when URL recognition becomes definite; do not restart decoding the growing candidate on every feed. Overlapping decoded views must not produce duplicate replacements.

URL spans still end at raw terminators; decoded whitespace does not shorten a raw candidate. A literal secret spanning that terminator remains active across it. Rejecting secrets with spaces, full/partial scheme suffixes or cross-span matches is not part of this design.

Alternative: secret deletion followed by fresh URL scanning. Rejected because it both requires replay and can invent a new address. Independent per-span secret filters are rejected because they lose cross-span matches.

### 3. Coordinate coordinator, commits and single ownership

The projector owns the only retained source payload. The recognizers own registration structures, bounded pending escape units, automaton states and source coordinates. Neither a second raw `_tail` nor an input-sized decoded buffer/source map is allowed. Coordinate records for detected outcomes must be pruned with source commitment and remain bounded by the existing pending budget, not the stream's history.

The coordinator combines tentative Text/URL ranges and secret coverage. It emits independently safe text promptly, retains exactly the earliest still-required unresolved suffix, and commits completed secrets under the current streaming rules. It must not wait for the end of a URL merely to avoid a mid-token secret commit. URL continuation starting inside already-covered secret characters survives as recognition state even though those characters are never emitted as text again.

Consumption advances outcome/ownership frontiers; it is not a new batch scan of an arbitrary retained substring. Keep automaton failure and overlap states needed by future source characters rather than pruning every state whose start precedes the output frontier. Separate already-emitted/covered source ranges from live recognition eligibility so a continuation cannot duplicate a marker or resurrect committed text. If a low-level API does not support an arbitrary caller-created cut, reject it explicitly and document its supported coordinated commits; all existing projector commits must remain supported without delayed delivery.

Projector reset means discarding obsolete state at finalization or overflow recovery, never feeding retained source again. Ordinary prefix commits and secret replacement keep the persistent automata. Counters belong to the stream/projector lifetime and survive resets, so replacing an instance cannot hide work. Incremental buffer accounting encodes only new pieces and must not repeatedly join/copy growing tails.

### 4. Rendering and metadata are separate products

Safe secret replacement is `<secret>`, not deletion. Do not parse a string formed by removing a secret or a marker as a fresh URL.

| Local policy | Supported URL projection | Ordinary literal secret |
| --- | --- | --- |
| `redacted` | Emit one `<redacted>` as soon as the complete supported scheme and `://` are recognized, subject only to the crossing-secret prefix exception below; suppress the remaining raw URL | `<secret>` |
| `host-path` | Host/path only; secret-bearing host `<redacted-host>`; unsafe or secret-bearing path `/<redacted-path>` | `<secret>` |
| `exact` | Original terminal-safe source within existing bounds | Original source |

Rendering has its own irreversible commit frontier, separate from recognition completion and metadata eligibility. For example `feed("https://ho")` in redacted projection emits `<redacted>` immediately: the full separator, not a complete authority or validated URL, triggers coverage. Subsequent source is suppressed through the raw URL terminator without another URL marker. The coordinator still advances URL/secret automata; it may retain bounded projector-owned data or incremental validated component state required by metadata. Early display does not authorize early hostname/fingerprint facts. If bounded metadata processing is abandoned, emit no incomplete/overflow URL facts and never grow another payload buffer.

Exception: when a registered secret includes the URL separator and an earlier unresolved literal prefix of that secret crosses the recognized separator, redacted marker delivery must wait until the prefix resolves, preserving source order. With secret `Xhttps://host`, `feed("Xhttps://ho")` must return no output. Continuation `st done` yields `<secret><redacted> done`; continuation `r done` yields `X<redacted> done`. Clean EOF without continuation resolves the crossing literal prefix as a non-match because the secret never completed: output must be `X<redacted>`, without `<secret>` or an incomplete-token marker. Once the prefix resolves, emit exactly one irreversible URL marker without waiting for unrelated authority completion or a raw terminator. In all other cases immediate separator-triggered delivery remains mandatory. This exception changes neither retention limits, replay-free continuation nor metadata eligibility; existing finalization rules govern other unresolved uncommitted state, while this clean-EOF crossing-prefix outcome overrides incomplete-secret-prefix handling only for the described exception. Abort and overflow rules remain unchanged.

In host-path projection, commit components separately. Wait for the complete authority before deciding hostname: late userinfo `@`, an IPv6 literal or explicit port can change its interpretation. Emit the validated normalized hostname or `<redacted-host>` once authority validation and every secret overlap capable of covering it are settled. Wait for the path boundary and safety/canonicalization result, then emit the safe path or `/<redacted-path>`; query/fragment never enter output. A possible secret crossing authority/path or URL/text boundaries may delay only affected unsafe-to-commit output. Neither decoded separators nor early chunks are raw URL terminators. Already committed host text cannot later be replaced because an unrelated path/URL suffix is unfinished.

Local exact delivery bypasses content substitutions and URL-component waiting, retaining terminal safety, line bounds and selected-tail semantics. A simultaneous SDK projection has its own redacted coverage frontier and continues coordinated recognition without populating a second retained tail.

Credentials, scheme, port, query, fragment and proxy information never enter host-path rendering. A secret in credentials may coexist with a validated safe host/path. A masked host may coexist with an independently safe path: `https://TOKEN.example/public` renders `<redacted-host>/public`; if the path is also unsafe it renders `<redacted-host>/<redacted-path>`.

Rendering markers are not normalized hostname facts. Derive external hostname facts only from independently validated secret-free hosts. A secret-bearing host yields no normalized-host fact and no URL fingerprint. A secret-bearing path yields no fingerprint, though a safe hostname fact may remain. Safe host/path rendering with a masked host is an internal display product, not proof of a validated URL identity. Fingerprints otherwise retain the existing canonicalization, order/multiplicity, explicit-port and session-key rules; credentials/query/fragment are excluded.

A preceding adjacent secret affects metadata eligibility: `TOKENhttps://host/public` with secret `TOKEN` is represented with the retained `<secret>` prefix and must not be reparsed as a standalone valid `https` URL for facts. The supported URL suffix still must not leak in redacted SDK text. Likewise, overlapping `TOKENht`/`https` states exist to protect projection, not to authorize facts from secret-transformed source.

Apply these gates to drain completion, clean EOF, structured projection, and collector/fetch side paths. Unknown schemes without a supported embedded start remain text and provide no URL facts. They can differ in ordinary rendered text/grouping; do not claim that unknown URLs automatically coalesce. SDK projection always uses redacted policy independently of local mode, with the explicit unknown-scheme exception to the previous universal URL-free promise.

### 5. Finalization and recovery preserve the stream contract

Retain only still-needed source under the existing whole-pending 8 KiB UTF-8 bound, not an assumed read size. Chunk boundaries are not token boundaries. Maintain source-order output and no duplicate candidates/facts. The user-approved exception to historical marker sequencing is early irreversible URL/component commitment, in addition to `<secret>` replacement.

Before the full supported `://` is detected, keep the previous ambiguous-prefix EOF/abort/overflow rules and markers. Ordinary words still survive clean EOF. Once detected, subject only to the crossing-secret prefix delivery exception above, redacted projection commits exactly one `<redacted>` and suppresses its remainder through a raw terminator; clean EOF, abort and token overflow never replace or append an incomplete/oversized marker for that already-covered URL. Bounded metadata collection may be abandoned without changing its committed rendering or granting facts.

Host-path commits settled authority and path components independently. Clean EOF may complete and validate an uncommitted component. Abort or token overflow preserves every emitted component and applies the existing incomplete/oversized token marker only to the unresolved, uncommitted display remainder, once; it never repeats a covered component. If all display components are committed and only omitted query/fragment or metadata remains, no extra token marker is emitted. Incomplete components never become source text on abort. A complete secret never becomes an ordinary word merely because another recognizer is unfinished.

Overflow discard/recovery still observes raw boundaries without replay. If an active cross-boundary secret still needs coverage at recovery, preserve only its bounded recognition/coverage state so its suffix cannot leak; do not start a second payload tail. The ordinary ambiguous-secret oversized marker, diagnostic-line overflow marker and collector line bounds remain unchanged. Line overflow may append its own single diagnostic marker after earlier committed URL/component output; that is distinct from duplicating a token marker.

Alternative: defer secret commits to raw delimiters. Explicitly rejected: it changes prompt delivery, pending limits, EOF and overflow behavior.

### 6. Work accounting and acceptance

At fixed supported scheme set, encoding depth and registered secret set, each new source unit participates in bounded transition work. Failure transitions are amortized, decoder pending storage is fixed-depth, and commit processing retires coordinate outcomes rather than rescanning suffixes. Registration cost/state may depend on secret patterns; live search state must not grow with input history. Report output-sensitive match/coverage work rather than hiding it inside counters or resetting counters with instances.

Deterministic regressions must cover successive supported commits, secrets before/inside/after URLs, crossings at both URL boundaries, encoded overlaps, terminators immediately before/after commits, repeated overflow resets, and input doubling. Count every recognizer instance and transition path, including speculative work and finalization. Verify exact coordinates, one raw owner, no discarded suffix leakage and no duplicate candidates/facts. Assert output after each feed for redacted early separator commitment and host-path component commitment, not merely final concatenated text. Also verify the crossing-secret prefix exception with matching continuation, mismatching continuation and clean EOF without continuation: the source-order-dependent marker must wait until the earlier crossing secret prefix resolves; other URL markers must not be delayed by this exception, and resolution produces exactly one irreversible URL marker. Cover early output followed by clean EOF, abort, token overflow and diagnostic-line overflow, with facts delayed until eligible completion. Keep stress fixtures unchanged; do not use elapsed-time assertions or claim whole-pipeline linearity.

## Risks / Trade-offs

- [Unknown URLs now remain visible] → Document the SDK and local safe-mode contract change, test ordinary-text behavior and prevent fetch metadata bypasses.
- [Overlap state survives a commit but re-emits covered text] → Separate output coverage from recognition eligibility; test both URL-start and raw-terminator crossings at every chunk partition.
- [Decoded secrets alter source boundaries] → Track bounded provenance; never treat virtual terminators as raw boundaries or grant a fourth interpretation pass.
- [Rendering markers mistaken for facts] → Distinct internal rendering product and validated metadata gates, with empty-host/fingerprint assertions for secret-bearing hosts.
- [Replay hidden by resets or buffer ownership tests] → Stream-lifetime work counters and recursive payload/state audits, including reset-heavy scenarios.
- [Historical gates reused for changed behavior] → Reopen affected P1–P3 tasks and validate each changed predecessor before dependent phases.

## Migration Plan

Reconcile proposal, both delta specs and tasks before code changes. Preserve historical evidence under verification/; it is not acceptance of this revised contract. The phase DAG is `P1 → P1A → P2 → P3 → P4 → P5 → P6`, each with RED → GREEN → INTROSPECT → VALIDATE, phase-local tests/docs and separate full logs/statuses. P3 is explicitly reopened until all ownership, overlap, prefix-delivery and cumulative-work guarantees pass.

Run Python verification in the project virtual environment with eight loadfile workers; final acceptance requires `.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q`, preserved stdout/stderr, warnings and actual exit status, plus configured project checks. Prior successful gates may be reused only for unchanged obligations with an explicit evidence review, never by retaining a completion box alone.

Deployment is an ordinary code update after acceptance, without stored-data migration. Rollback reverts coordinated recognition and policy changes together. No new dependencies, configuration switches, persistence, argv, deadlines, scheduler or shutdown-budget changes. Sync/archive remains a separate explicit workflow.

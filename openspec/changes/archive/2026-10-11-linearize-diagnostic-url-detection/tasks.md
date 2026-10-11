# Tasks

## Binding execution contract

Proposal, design and delta specs define the revised supported-scheme and overlapping-secret contract. This checklist is binding: completion requires the stated behavior and evidence, not merely a checkbox. The phase DAG is `P1 → P1A → P2 → P3 → P4 → P5 → P6`, with RED → GREEN → INTROSPECT → VALIDATE in each phase and successful predecessor gates before dependent work.

The user-approved revision supersedes arbitrary UNKNOWN hiding, secret rejection for Text/URL crossings or scheme suffixes, independent per-span secret filters and unrestricted retained-suffix batch equivalence. Prior evidence remains historical. Affected P1–P3 tasks are reopened below; P3 must not close before current prefix delivery, one raw owner, overlapping continuation and cumulative linear recognition work are implemented and revalidated. The unchanged one-off baseline task 1.2 remains complete; other revised obligations need current evidence.

GREEN must not reduce fixtures, exclude failures, suppress warnings or delay secret commits to raw delimiters. Phase-local tests and documentation are mandatory. INTROSPECT must resolve ownership, correctness, complexity and scope findings before VALIDATE. A failed gate blocks dependent phases; reopen and revalidate defects in their owning phase.

Store phase-labelled command outputs, full logs, actual exit statuses, reviews and one-off profiles in verification/, never in requirement text. Tests must not measure recognizer elapsed time. Python verification uses the project virtual environment and eight loadfile workers; full-suite acceptance requires `.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q`, complete stdout/stderr, warnings and actual successful exit status. Applicable configured checks must also pass the final code state.

## 1. P1 — Revised recognition and projection contract

**Depends on:** none.
**Deliverables:** revised caller inventory, baseline, internal models and fixed scheme policy, contract tests/API documentation, P1 validation record.

### RED

- [x] 1.1 Reconcile the caller inventory for normal/EOF/structured projection, secret registration, collector/fetch facts and grouping; deliver ownership of every changed detection, marker and metadata dependency.
- [x] 1.2 Capture the one-off baseline experiment outside tests/CI; verify evidence records 1/2/4/8/16 KiB medians, capture profiles, oversized-tail timings, environment, commands, full logs and statuses without fixture changes.
- [x] 1.3 Revise contract tests for supported-only detection, raw inclusive starts/exclusive ends, end=None, ambiguous prefixes, embedded starts and unknown text; record which previous obligations are intentionally superseded.
- [x] 1.4 Record revised P1 RED results and unchanged passing obligations; verify failures map to the agreed contract rather than infrastructure.

### GREEN

- [x] 1.5 Revise immutable internal outcomes/models for supported URLs and source secret coverage; verify tests do not make detection prove validity or introduce a public/persisted DTO.
- [x] 1.6 Define the exact case-insensitive scheme set and detection-versus-interpretation depth; verify all listed schemes and unknown variants without changing transport/lock acceptance.

### INTROSPECT

- [x] 1.7 Document raw coordinates, outcome validity, supported consumption boundaries and marker/fact separation; verify API documentation agrees with the revised tests.
- [x] 1.8 Review the revised P1 diff and historical evidence applicability; deliver resolved findings or a blocking gate without reusing obsolete UNKNOWN-hiding results.

### VALIDATE

- [x] 1.9 Run P1 and affected existing tests with successful statuses; deliver the revised P1 gate linking full logs and each deliverable.

## 1A. P1A — Standalone Aho–Corasick streaming core

**Depends on:** successful revised P1 gate.
**Deliverables:** internal dependency-free trie with failure/output links, immutable registered patterns, independent stream cursors, deterministic counters, unit tests, API/complexity documentation and P1A validation record. This phase implements the generic matching core only; percent-decoding, URL semantics, secret selection and projector integration remain in P2/P3. Keep existing P1–P6 numbering and historical evidence labels unchanged.

### RED

- [x] 1A.1 Define core contract tests for empty pattern sets, registered non-empty patterns, duplicates, prefix/suffix patterns, overlapping and simultaneous matches, Unicode Python-character coordinates and independent streams sharing one compiled trie; verify expected source spans without introducing a public SDK API.
- [x] 1A.2 Add one-feed/every-split/single-character continuation tests plus deterministic transition/failure/output-link counters and state ownership checks; verify chunk boundaries require neither overlap text nor retained payload.
- [x] 1A.3 Add a bounded differential corpus against a simple literal-match oracle and adversarial suffix-heavy patterns; verify every match exactly once, shared compilation stability, output-sensitive work and live cursor state independent of stream length without timing assertions.
- [x] 1A.4 Record P1A RED with full logs and actual statuses; map failures to missing core behavior and record already-passing obligations honestly.

### GREEN

- [x] 1A.5 Implement the internal trie builder and breadth-first failure/output-link construction without a new runtime dependency; verify registration tests and storage bounded by configured patterns, avoiding copied transitive output lists that multiply registration storage.
- [x] 1A.6 Implement a per-symbol streaming cursor over immutable compiled state, reporting pattern identities and exclusive match ends in Python-character coordinates; verify overlapping outputs, independent cursors and no retained source string, chunk replay or input-sized history.
- [x] 1A.7 Implement cumulative counters for input transitions, failure traversals and emitted-match/output-link work, with explicit cursor reset semantics that preserve lifetime totals; verify search work is O(N + matches) and reset cannot hide work.

### INTROSPECT

- [x] 1A.8 Document registration versus live-state ownership, Unicode/coordinate semantics, cursor continuation/reset, output ordering and O(N + matches) search bounds; verify examples and deterministic tests match the API without claiming this core resolves URL/secret coordination.
- [x] 1A.9 Audit construction/search allocations, output-link handling and failure paths; deliver resolved findings for immutable shared registration, payload-free cursors, pattern-dependent storage and cumulative work, with no production regex/library fallback.

### VALIDATE

- [x] 1A.10 Run P1/P1A and applicable configured checks in the project environment with required eight-worker Python verification; preserve full logs/statuses and deliver a successful P1A gate before P2 integration. Historical recognition evidence does not substitute for this new gate.

## 2. P2 — Compact supported-scheme and secret recognition

**Depends on:** successful revised P1 and new P1A gates.
**Deliverables:** bounded percent transducers, fixed scheme automaton, literal/URL-decoded secret automata, overlap provenance, deterministic work/state tests and P2 review/validation.

### RED

- [x] 2.1 Add raw scheme tests for literal separators, negative single-slash/npm forms, unknown text, embedded starts, earliest supported selection and raw terminators; verify exact outcomes/coordinates.
- [x] 2.2 Add mixed/nested depth-zero-through-four separator tests, malformed/incomplete escapes and encoded scheme validity checks; verify no fourth interpretation or secret-decoding pass.
- [x] 2.3 Revise the bounded differential corpus for intentional supported-only changes; add literal secret selection, scheme-prefix suffix overlap and secrets crossing raw terminators with explicit expected coverage.
- [x] 2.4 Add deterministic state/work and raw-provenance assertions for scheme/secret automata; verify fixed registration-dependent bounds without input-sized search maps, variant tables or timing.
- [x] 2.5 Record revised P2 RED with each failure mapped to scheme, decoder, secret selection, decoded scope or overlap obligations and honestly record already-passing cases.

### GREEN

- [x] 2.6 Implement/reuse bounded percent transducers and provenance for supported recognition; verify raw and encoded tests without decoded search payload copies.
- [x] 2.7 Integrate the validated P1A core with fixed-scheme prefix/separator transitions and registered literal secret matching, retaining overlapping starts; implement selection/coverage outside the generic core and verify embedded schemes, leftmost/longest selection and no secret rejection for crossings/suffixes.
- [x] 2.8 Implement secret matching inside recognized URL views through three decoding passes and preserve continuation states across covered secret ranges; verify literal-only ordinary text and no suffix replay or duplicate coverage.

### INTROSPECT

- [x] 2.9 Document transition/state bounds at fixed depth, scheme set and registered secrets, including output-sensitive work and raw provenance; verify registration storage is distinguished from stream history.
- [x] 2.10 Audit P2 allocation, speculation, fallback and decoding scope; deliver findings proving no duplicate raw tail, decoded search buffer, input-sized search map, variant enumeration or regex fallback.

### VALIDATE

- [x] 2.11 Run revised P1/P1A/P2 and affected existing detection/secret tests; deliver successful logs and P2 corpus/work/state evidence before P3 integration.

## 3. P3 — Replay-free coordinated continuation and tail ownership

**Depends on:** successful revised P2 gate.
**Deliverables:** persistent overlap coordinator, one projector-owned payload, exact bounded retention, current prefix delivery, cumulative counters, EOF/abort/overflow behavior and P3 validation.

### RED

- [x] 3.1 Add partial supported-scheme/percent/separator and secret-overlap retention tests; verify exact earliest required source suffix and prompt independent prefix delivery.
- [x] 3.2 Add detected-but-unterminated and source-order tests, including TOKENht/https overlap, multiple completed candidates and commits immediately before/after raw terminators; verify no lost/duplicate outputs or facts.
- [x] 3.3 Add one-feed/every-split/single-byte partition tests for literals, encoded prefixes, secrets and crossings at both Text/URL boundaries; verify final text and ordered facts under existing limits.
- [x] 3.4 Add feed-by-feed early redacted URL and host-path authority/path commitment tests followed by clean EOF, abort, token overflow and line overflow, plus unchanged partial-separator rules, immediate secret commits and cross-boundary coverage; verify irreversible output, exact markers and no leaked suffix, including mandatory marker withholding until the earlier crossing secret prefix resolves, with secret Xhttps://host, first feed Xhttps://ho and matching st done, mismatching r done and clean EOF without continuation (X<redacted>, no secret or incomplete-token marker) outcomes, while retaining mandatory immediate URL delivery in other cases.
- [x] 3.5 Add lifetime work/state/ownership regressions across repeated supported commits and resets; count all recognizer instances/speculation and assert linear input-doubling growth, not merely feed work within one instance.
- [x] 3.6 Record revised P3 RED for replay, ownership, commitment and recovery defects; distinguish historical successful feed-only checks from the currently missing cumulative guarantees.

### GREEN

- [x] 3.7 Integrate persistent URL/secret coverage and continuation states into projector draining; remove recognizer `_tail` and consumption replay and projector retained-suffix resets; verify current prefix delivery and every existing projector commit remain supported without replay.
- [x] 3.8 Preserve incremental retained-buffer/byte accounting and add stream-lifetime recognition counters that survive resets; verify one raw owner and bounded storage/copying/encoding work.
- [x] 3.9 Integrate literal secret precedence, overlapping decoded URL coverage and absolute-to-buffer coordinate conversion; verify TOKENht continuations and cross-terminator secrets without delayed commitment or reconstructed payload.
- [x] 3.10 Implement clean EOF/abort and irreversible early rendering for coordinated outcomes; verify one early redacted marker subject only to mandatory source-order-preserving delay until an earlier unresolved separator-crossing secret prefix resolves, exactly one irreversible marker upon prefix resolution, clean-EOF crossing-secret non-match output without an incomplete marker while abort/overflow rules remain unchanged, settled host/path components, incomplete markers only on unresolved uncommitted display remainders, unchanged partial-separator rules and single percent-state finalization.
- [x] 3.11 Implement bounded overflow recovery preserving secret coverage and committed output without replay; verify no duplicate marker for covered redacted URLs, one marker only for an uncovered remainder, unchanged line-overflow behavior and no leaked suffix.

### INTROSPECT

- [x] 3.12 Document recognition, rendering and metadata frontiers, ownership, the narrowly scoped crossing-secret prefix delivery exception and early-output EOF/abort/overflow exceptions; verify component/secret settlement and that 8 KiB remains a whole-pending byte limit, not a read-size assumption.
- [x] 3.13 Correct the P3 audit against actual cumulative results; resolve suffix replay, duplicate payload and obsolete unrestricted batch-equivalence claims before closing the gate.

### VALIDATE

- [x] 3.14 Run revised P1–P3 including P1A, affected projection/lifecycle tests, the mandated full suite and configured checks; preserve full logs/statuses and close P3 only when ownership, delivery and cumulative-work guarantees agree.

## 4. P4 — Policy-specific rendering and secret-aware facts

**Depends on:** successful revised P3 gate.
**Deliverables:** policy markers, validated fact gates on all routes, metadata/identity coverage, documentation and P4 validation.

### RED

- [x] 4.1 Add unknown-text tests for delimiter/EOF/structured routes plus TOKENhttps secret-prefix cases; verify unknown text stays text and no facts arise by deleting/replacing secrets and reparsing.
- [x] 4.2 Add policy tests for credential secrets, secret-bearing hosts/paths, malformed supported URLs and encoded secrets; verify early irreversible redacted hiding, separate settled host/path delivery, `<redacted-host>`, `/<redacted-path>`, delayed validated facts and unchanged exact source.
- [x] 4.3 Add three-pass decoded-secret scope and fact assertions: credential-only permits otherwise safe facts, host secret suppresses hostname/fingerprint, path secret suppresses fingerprint but permits a safe hostname.
- [x] 4.4 Record P4 RED for marker, route and metadata obligations, distinguishing agreed compatibility changes from accidental recognition failures.

### GREEN

- [x] 4.5 Implement normal projection markers and distinct masked display products; verify markers are not normalized-host facts and standalone literal secrets render `<secret>`.
- [x] 4.6 Apply supported/secret coverage gates to normal, EOF and structured metadata; verify no transformed-source or unknown-fact bypass.
- [x] 4.7 Preserve eligible known URL canonicalization, explicit ports, fingerprint order/multiplicity and session isolation; verify no fingerprints from secret-bearing host/path or attached secret prefixes.

### INTROSPECT

- [x] 4.8 Document policy rendering versus validated metadata, decoded-secret scope and the unchanged exact exception; verify examples match executable assertions.
- [x] 4.9 Reconcile every inventoried metadata/fetch route; verify unknown RFC-valid successful-fetch URLs still match fetch grammar without URL-derived facts/grouping fields and shared validation is unchanged.

### VALIDATE

- [x] 4.10 Run revised P1–P4 and affected identity/metadata tests; deliver successful full logs and a compatibility record for the agreed policy changes.

## 5. P5 — Consumer channels and grouping

**Depends on:** successful P4 gate.
**Deliverables:** collector channel isolation, SDK supported-URL contract, selected retained rendering/grouping, docs and P5 validation.

### RED

- [x] 5.1 Add all-mode collector/tail tests with/without SDK delivery; verify exact source/secrets, supported URL hiding, unknown text, secret host/path markers and exactly one selected retained representation.
- [x] 5.2 Add ordinary/fetch grouping tests for unknown rendered text, eligible fingerprints and suppressed secret-bearing host/path identities; verify line/interactive rules explicitly.
- [x] 5.3 Add fetch side-path regressions for supported, unknown and secret-bearing URLs; verify unchanged parser grammar and consistent permitted facts/rendered group keys.
- [x] 5.4 Record P5 RED and honestly record already-passing channel isolation; map missing behavior to the revised policy rather than old UNKNOWN omission assumptions.

### GREEN

- [x] 5.5 Integrate coordinated projection into collector channels without another retained tail; verify early redacted SDK output independent of selected local mode, unchanged exact local delivery, the unknown-text exception and no retraction in selected tails.
- [x] 5.6 Align grouping and fetch display paths with masked rendering/fact gates; verify no hidden-host identity leak and unchanged eligible known identity distinctions.

### INTROSPECT

- [x] 5.7 Update user-facing scheme, secret-marker, host/path-marker and SDK policy documentation; verify examples and local exact behavior against tests.
- [x] 5.8 Review channel isolation and scope; deliver findings confirming unchanged configuration options, persistence, assembly identity, argv, deadlines and shutdown budgets.

### VALIDATE

- [x] 5.9 Run revised P1–P5 and affected collector/presentation acceptance tests; deliver successful full logs and evidence linking channel/grouping/docs obligations.

## 6. P6 — Final acceptance

**Depends on:** successful P5 gate.
**Deliverables:** revised requirement-to-evidence matrix, one-off comparison, final full-suite/check logs, scope review and acceptance decision.

### RED

- [x] 6.1 Map every revised requirement/scenario and phase deliverable to actual tests/evidence; record outstanding obligations without manufacturing failures or claiming old gates prove changed behavior.
- [x] 6.2 Discover applicable configured lint/type checks; record exact commands or justified absence without inventing checks or deferring earlier coverage.

### GREEN

- [x] 6.3 Perform one-off before/after recognition/capture experiments outside CI; deliver baseline-compatible single-feed/chunked medians, profiles and allocation/work evidence with unchanged stress fixtures.
- [x] 6.4 Run the complete mandated eight-worker loadfile pytest command; preserve full stdout/stderr, warnings, summary and actual zero runner status without exclusions.
- [x] 6.5 Run applicable configured checks; preserve full outputs and successful statuses, or retain the verified absence in 6.2.

### INTROSPECT

- [x] 6.6 Review final implementation scope; verify unchanged stress inputs, scheduler, AGENTS.md, shutdown budgets and unrelated execution semantics, with apply-time spec edits limited to completion boxes.
- [x] 6.7 Reconcile final acceptance evidence, cumulative work and raw ownership against the matrix; verify all findings resolved without claiming whole-pipeline linearity.

### VALIDATE

- [x] 6.8 Revalidate affected checks after final fixes; verify full-suite and applicable check logs reflect the final code state and successful statuses.
- [x] 6.9 Deliver final acceptance only after revised P1–P6 and added P1A gates pass; otherwise report blocked acceptance and outstanding obligations, keeping P3 reopened if its guarantees remain unverified.

## Workflow follow-up

- Review planning revisions and historical evidence before resuming apply.
- Sync/archive only on separate explicit request after successful acceptance.

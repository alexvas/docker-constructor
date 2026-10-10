# Design

## Context

See proposal.md for motivation. The affected pipeline currently locates a start with `_find_url_start`, locates a source terminator with `_find_terminator`, and parses a bounded candidate through `_candidate_url_layer`, `urllib.parse.urlsplit`, hostname/path validation and ephemeral identity derivation. The demonstrated hotspot is the unanchored `_URL_START_RE.search`, not an established quadratic defect in every downstream parser.

`DiagnosticProjector._drain`, `_unresolved_candidate` and `_is_complete_url_at_eof` depend on the old detection contract. Streaming input has ambiguous-prefix holding, 8 KiB pending bounds and overflow recovery; the collector separately bounds diagnostic lines and retains one selected tail. `normalized_url_host` is also used by npm fetch recognition. Changing shared hostname validation globally would unintentionally expand the scope.

Design is required because this change combines performance-sensitive parsing, source-coordinate mapping, stream finalization and intentional metadata compatibility changes.

## Goals / Non-Goals

**Goals:**
- Remove repeated suffix regex scans while making candidate extent and interpretation policy explicit.
- Share candidate classification across normal draining and clean-EOF finalization; do not let unknown metadata escape through a second path.
- Preserve existing raw token boundaries, secret precedence and bounded stream ownership.
- Establish linear raw-text recognition at fixed depth with bounded state, an algorithmic argument and repeatable evidence.

**Non-Goals:**
- A new general URI parser, a public SDK candidate API or a persisted URL-kind field.
- Global changes to `normalized_url_host`, fetch grammar, transport URL validation or scheme acceptance in dependency locks.
- Full equivalence for unknown metadata, or a claim of whole-pipeline linearity beyond incremental candidate recognition.

## Decisions

### 1. Internal immutable candidate, not a tuple of loosely related values

Introduce an internal record with `start`, exclusive `end: int | None`, and a closed `kind` (`KNOWN`, `UNKNOWN`). Offsets are character indices into the supplied raw string, not UTF-8 byte offsets or decoded-layer coordinates. `end=None` means no raw terminator has arrived; it does not mean a valid complete URL. No result means no complete separator-based candidate was detected; ambiguous suffix holding remains a separate operation.

Expose a closed recognition outcome separate from candidate kind: `NO_MATCH`, `MATCH(candidate)` or `NEED_MORE(retain_from)`. `KNOWN`/`UNKNOWN` classify only a detected candidate, not an unfinished separator. For `https:/`, return NEED_MORE with retention from the scheme start; for `https://host`, return MATCH with `end=None`, and retain the token until its raw terminator or finalization. Neither outcome proves URL validity.

Return the earliest detected candidate first, even if an ambiguous suffix follows it. Process it once and resume in source order, applying existing secret precedence. NEED_MORE applies to the remaining ambiguous suffix, retaining from the earliest still-viable scheme/escape start rather than just a trailing colon, slash or percent sign. A suspicious character without a viable candidate context does not itself mandate retention. NO_MATCH permits commitment only of text independently safe under secret and terminal handling.

Keep raw offsets in a documented stream coordinate space across incremental feeds, with explicit conversion when slicing the retained buffer. The classifier may retain bounded scheme/layer information needed to avoid reclassification; that is not an external data contract.

At clean EOF, existing completeness rules still distinguish a complete token from an unresolved prefix. Complete UNKNOWN tokens are redacted without parsing or facts; unresolved tokens use the existing incomplete marker. Abort continues to fail closed. Known malformed URLs never gain metadata simply because their scheme is in the whitelist.

Alternative: keep the integer API and separately repeat classification/end discovery. Rejected because multiple interpretation paths invite drift at EOF and add unnecessary scans.

### 2. Compact raw recognizer, not encoded-variant enumeration

Consume the raw search text through a fixed cascade of incremental percent-decoding transducers. Each transducer retains only an ordinary/percent/first-hex state and its bounded pending source ranges; emitted characters carry their originating raw start and exclusive end. Malformed escapes and final incomplete escapes must reproduce the existing detection decoder's literal behavior, including overlap/reprocessing, rather than disappearing. Search observers consume emissions immediately: no full decoded search strings, full-buffer source maps or tables of encoded variants are constructed.

Attach a small active-state scheme/separator recognizer to raw input and to each of the first three virtual decoded views. These observers implement the old conservative scheme-unit grammar, including literal or encoded colon and slashes. Together they cover separator spellings of effective depth four without blindly applying four decoded views to all scheme and URL semantics. The fourth structural level is the observers' bounded encoded-unit recognition, not a new whole-URL decoding pass. This compact transducer/observer composition represents the alternatives implicitly.

Track the earliest start reaching each equivalent state at the current position. Encoded units require branching: `%3A` can be a conservative scheme unit or a colon. Restart eligibility must also preserve starts within malformed percent text. Record completed candidates and select the minimum raw start across observers; the first completion need not have the earliest start. Locate the selected candidate's raw terminator once instead of scanning a suffix for every possible start.

Whitelist classification uses bounded scheme-prefix information and existing permitted decoding semantics; an oversized scheme becomes UNKNOWN without repeatedly copying a growing string. Detection still accepts arbitrary conservative unknown schemes. Preserve any bounded interpretation distinctions required by classification when merging states; start-index dominance alone is valid only for equivalent future recognition and classification states.

For fixed depth and fixed grammar, each raw character participates in a bounded number of transitions; transducers do not expand their emitted character count. Recognition is O(N) with bounded state independent of input length. Existing retained candidate text and subsequent URL parsing have their own unchanged bounds. Do not enumerate hundreds of millions of single-character spellings or billions of separator combinations.

Alternative: four literal searches. Rejected because they omit mixed depth, hex case and separately encoded escape characters. Alternative: materialize decoded layers and run a linear scanner. Correctness can be retained, but whole-buffer copies and source maps are unnecessary when emissions can carry provenance. Alternative: possessive regex or repeated backwards scheme scans. Rejected because they do not by themselves establish bounded work across all starts.

### 3. Separate detection depth from interpretation depth

The effective separator detection depth is four: the legacy regex recognizes one encoded structural layer after zero through three additional decoding passes. Preserve that language using the observers above; do not simply accept every fourth-decoded scheme. In particular, conservative encoded scheme units, encoded characters that become invalid scheme characters, and starts exposed at different views must retain their legacy detection behavior.

Carry raw source ranges through the transducers and choose the earliest raw candidate even when an earlier nested-encoded candidate is emitted later than a literal one. Keep candidate end at the existing raw terminator, not a boundary introduced only by percent-decoding. An encoded space must not expose a raw suffix by shortening the hidden span. Same-start matches must preserve raw extent and cannot approve malformed scheme content merely by choosing a deeper view.

Known URL interpretation, host/path validation and identity canonicalization retain their existing bounded decoding rules. Decode only bounded selected scheme/candidate content when classification or validated metadata requires it; UNKNOWN hiding does not require parsing a whole URL. A fourth detection level does not authorize a fourth whole-URL interpretation pass.

Test source-range propagation for multibyte raw prefixes, malformed and incomplete escapes and independently encoded escape characters. There is no new scheme-length rejection, regex fallback, whole-buffer search decoding or input-sized search mapping. Existing mapping/decode helpers used for interpretation or unrelated consumers are not removed indiscriminately.

### 4. Gate interpretation at the diagnostic projection boundary

For KNOWN candidates, retain existing safe URL parsing, hostname/path derivation, path fallback and keyed identity canonicalization. For UNKNOWN, emit the existing redaction marker for a completed candidate and derive no hostname, host/path or fingerprint. Do not call URL parsing merely to decide whether an unknown candidate deserves redaction.

Apply the same gate to `_sanitize_candidate`, clean EOF and relevant structured-diagnostic projection helpers. Audit callers of detection and metadata helpers rather than changing shared hostname validation indiscriminately. Npm fetch recognition must not manufacture unknown facts through a collector side path; known successful-fetch contracts otherwise remain unchanged.

The scheme policy is fixed, internal and case-insensitive. It introduces neither a user setting nor dependency/assembly execution semantics. SDK safe projection applies even when local `exact` is selected. Local `exact` keeps its authoritative existing content policy, including preserving caller secrets; terminal neutralization and bounds still apply.

Alternative: whitelist-only detection. Rejected because unknown URL-shaped tokens would leak into safe output. Alternative: interpret unknowns as before. Rejected by the agreed behavior change and unnecessary parsing cost.

### 5. Incremental continuation with exact tail retention

The recognizer retains its transducer and observer states across feeds. On NEED_MORE, commit only the safe prefix and retain the exact ambiguous raw suffix plus bounded recognition state. Feed new characters only: do not restart search on the growing retained suffix, reread stdout/stderr or create a fixed 4 KiB overlap. Retained raw content serves later transformation; it is not input to repeated recognition. Extend an already detected MATCH with `end=None` incrementally until a raw terminator or finalization. Emit a candidate and its metadata only once.

Eight KiB is the existing limit on the whole unresolved raw tail, not an assumed read size. Combine URL retention with existing secret-overlap holding by preserving the earliest necessary raw position. Preserve `_ambiguous_hold` semantics while reconciling its role with stateful recognition; it must not become an implicit route that restarts the recognizer. Buffer management must not repeatedly copy or re-encode the entire growing tail merely to append a byte or check its bound.

On overflow emit the existing oversized marker and discard through the next raw safe boundary; reset stale candidate/decoder states so recovery cannot reinterpret discarded content or duplicate a marker. On clean EOF finalize pending decoder emissions and retain the existing distinction between an ordinary bare scheme-like word, a complete candidate and an unresolved sensitive prefix. Abort retains the existing fail-closed rules. Do not turn a clean-EOF ordinary word into a redacted URL simply because an earlier feed returned NEED_MORE.

Recognition and raw termination accounting must be amortized linear across incremental input at fixed depth, including one-byte feeds. Verify with deterministic transition/work accounting and chunk-partition invariance, not timing assertions. This does not assert linearity of every downstream URL parser, secret matcher or renderer. Preserve existing oversized-tail fixtures, safe SDK processing in local `exact` and renderer shutdown budgets.

## Risks / Trade-offs

- [Encoded grammar overlap or incorrect earliest-start selection] → Small bounded differential corpus against the legacy start detector, explicit overlapping escapes and earlier-encoded/later-literal cases; compare only semantics intended to remain equivalent.
- [Raw source provenance or virtual-view semantics diverge] → Multibyte prefix, malformed/incomplete escape, encoded terminator and depth-four separator tests with exact source offsets and surrounding-text preservation; bounded differential comparisons against the old three-pass-plus-regex detector.
- [UNKNOWN metadata appears at EOF or through another helper] → Audit all interpretation entry points and assert empty facts for delimiter completion, clean EOF, structured projections and simultaneous SDK delivery.
- [Whitelist changes grouping] → Intentional compatibility change: otherwise equal unknown diagnostics can merge. Document and test this; preserve known identity order and multiplicity.
- [Known scheme mistaken for validated URL] → Keep malformed authority, unsafe path, secret-bearing host and fingerprint validation tests.
- [Time-based regressions are noisy] → Automated tests check correctness and bounded work deterministically; no recognizer timing measurements or timing-based assertions in automated tests or CI. One-off before/after profiling and scaling measurements during implementation belong only in separate evidence files, not in a recurring test or benchmark job.
- [Legacy long MODIFIED requirements trigger existing style constraints] → Preserve their full unrelated descriptions and scenarios as instructed; do not split requirements in this change merely to satisfy length guidance.

## Migration Plan

The implementation checklist in tasks.md is a binding contract with atomic tasks and explicit deliverables. Follow the phase DAG `P1 → P2 → P3 → P4 → P5 → P6`: recognition result contract, compact raw recognition, incremental tail retention/finalization, known/unknown projection, consumer channels/grouping, and final acceptance. Later phases depend only on earlier phases; each dependency includes the earlier phase's successful validation gate.

Every phase executes RED → GREEN → INTROSPECT → VALIDATE and lands its own tests and documentation. RED must identify the intended unmet obligation; an unrelated failure is not evidence, and an already-passing obligation must not be deliberately broken. P6 RED inventories outstanding acceptance evidence rather than manufacturing a failing test. A phase cannot close before all named deliverables and validation evidence exist. If later work reveals an earlier defect, reopen and revalidate its owning phase and affected dependents rather than creating a backwards dependency.

Keep command outputs, complete logs, exit statuses, reviews and one-off profiling in phase-labelled evidence under `docs/verification/linearize-diagnostic-url-detection/`, not in specifications. No recognizer timing measurements belong in automated tests or recurring CI jobs. Final acceptance requires the complete mandated eight-worker loadfile suite and applicable project checks for the final code state, without silent exclusions or warning suppression.

Deploy as an ordinary code update after acceptance; no stored data migration is needed. Rollback reverts the scanner and classification together, restoring previous metadata behavior and performance. Do not ship a selectable legacy regex fallback. Sync/archive requires a separate explicit workflow request.

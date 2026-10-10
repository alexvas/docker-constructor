# Tasks

## Binding execution contract

This checklist is the binding implementation and acceptance contract for this change. Proposal, design and delta specs define its behavior and scope; no task authorizes weakening those requirements. Each checkbox is one independently verifiable action. A checked task MUST have its stated result and evidence; checking a box is not itself evidence.

Every phase MUST proceed RED → GREEN → INTROSPECT → VALIDATE. RED establishes the phase's executable obligations and records the expected unmet behavior before implementation. An unrelated exception, collection error or environmental failure is not a valid expected RED. A pre-existing passing obligation MUST be recorded honestly, not deliberately broken. In the final acceptance phase, RED means outstanding acceptance obligations, not manufactured test failures.

GREEN MUST satisfy the established obligations without deleting coverage, shrinking stress fixtures, silently excluding failing tests or suppressing warnings. INTROSPECT MUST inspect correctness, ownership, complexity, scope and documentation and resolve identified defects. VALIDATE MUST verify the phase and all predecessor deliverables. A phase is complete only when every checkbox, named deliverable and validation gate is satisfied. A failed gate blocks dependent phases; rework must be revalidated before proceeding.

Evidence MUST live separately from planning specifications, under `docs/verification/linearize-diagnostic-url-detection/`, with phase-labelled records linking commands, full stdout/stderr logs, runner exit statuses and relevant profiles. Automated tests MUST NOT measure recognizer elapsed time or assert timing thresholds. Baseline and before/after timings are one-off implementation experiments, not recurring CI benchmarks. Python verification MUST use the project virtual environment and eight workers; complete-suite acceptance uses `.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q`. Preserve the complete log and actual runner exit status for every run; every nonzero exit is unsuccessful verification.

## Phase dependency DAG

```text
P1 → P2 → P3 → P4 → P5 → P6
```

Direct prerequisites are stated below. Transitive prerequisites remain binding. No earlier phase depends on later work. Each phase lands its own tests and documentation; P6 adds no deferred feature tests. If later validation finds an earlier defect, reopen the owning phase, repair and revalidate it, then revalidate affected dependent phases without introducing a backwards dependency.

## 1. P1 — Recognition result contract

**Depends on:** none.
**Deliverables:** affected-path inventory; baseline evidence; immutable candidate/outcome model; closed whitelist; contract tests; API documentation; P1 validation record.

### RED

- [ ] 1.1 Deliver the caller inventory covering detection, unresolved holding, EOF, structured projection, metadata and collector/fetch routes; verify each old integer-contract dependency and possible UNKNOWN fact bypass has an identified owner.
- [ ] 1.2 Capture the one-off baseline experiment outside tests/CI; verify the evidence records medians for 1/2/4/8/16 KiB uninterrupted and separated runs, per-mode capture profiles, existing oversized-tail timings, environment, commands, full logs and statuses without changing fixtures.
- [ ] 1.3 Add outcome/model contract tests; verify they express NO_MATCH/MATCH/NEED_MORE separately from KNOWN/UNKNOWN, raw coordinates, exclusive ends, end=None and every case-insensitive whitelist entry.
- [ ] 1.4 Record the pre-implementation P1 test run; verify failures are attributable to missing contract behavior and separately identify any already-passing obligations.

### GREEN

- [ ] 1.5 Implement the immutable candidate and closed outcome model; verify P1 model assertions pass without introducing a public SDK or persistence contract.
- [ ] 1.6 Implement the fixed case-insensitive scheme whitelist; verify all listed schemes and non-whitelist variants classify as specified without changing shared transport or lock validation.

### INTROSPECT

- [ ] 1.7 Document candidate validity, offset conversion, outcome ownership and classification semantics at the internal API; verify the documentation matches P1 tests and does not conflate incomplete separator and incomplete URL.
- [ ] 1.8 Review the P1 diff for scope and contract consistency; deliver a review record showing findings resolved or a blocking gate if they remain.

### VALIDATE

- [ ] 1.9 Run P1 contract tests and affected pre-existing tests; verify zero runner exit status and deliver a P1 validation record linking full logs and every P1 deliverable.

## 2. P2 — Compact raw recognition

**Depends on:** P1.
**Deliverables:** bounded percent transducers and structural observers; raw provenance; depth-four detection coverage; deterministic compatibility/complexity tests; algorithm documentation; P2 validation record.

### RED

- [ ] 2.1 Add raw recognition tests for literal separators, negative `:/`/`npm:` forms, arbitrary UNKNOWN lengths, raw end boundaries and earliest-source selection; verify assertions target P1 outcomes and source coordinates.
- [ ] 2.2 Add encoded recognition tests for depths zero through four, mixed depths, hex case, separately encoded escape characters and malformed/incomplete escapes; verify tests preserve three-pass-plus-encoded-separator detection semantics without granting a fourth interpretation pass.
- [ ] 2.3 Add a bounded deterministic differential corpus against the legacy detector; verify expected comparisons cover overlapping units, multibyte prefixes and earlier encoded versus later literal starts, excluding only explicitly intentional metadata differences.
- [ ] 2.4 Add deterministic work/state regression checks; verify they assert bounded structural work without elapsed-time measurements, input-sized search maps or encoded-variant enumeration.
- [ ] 2.5 Record the pre-implementation P2 test run; verify each expected failure maps to a missing raw-recognition obligation rather than unrelated infrastructure failure.

### GREEN

- [ ] 2.6 Implement bounded incremental percent transducers with raw source-range provenance; verify encoded and malformed-escape unit tests pass with bounded pending state and no full decoded search buffers.
- [ ] 2.7 Implement structural observers and equivalent-state dominance; verify conservative scheme grammar, overlapping encoded units and earliest raw selection pass the P2 corpus.
- [ ] 2.8 Implement selected-candidate raw boundary detection and bounded scheme classification; verify exact source extents and arbitrary UNKNOWN lengths without repeated backwards or suffix scans.

### INTROSPECT

- [ ] 2.9 Document the fixed-depth transition/state bound and detection-versus-interpretation depth distinction; verify the argument covers encoded branching and preserves classification distinctions when states merge.
- [ ] 2.10 Audit P2 allocation and fallback paths; deliver evidence that no input-sized decoded search copies, variant tables, legacy regex fallback or performance-only scheme limit remains.

### VALIDATE

- [ ] 2.11 Run P1/P2 tests and the affected pre-existing detection tests; verify zero exit status and deliver the P2 validation record with compatibility corpus and deterministic complexity results.

## 3. P3 — Incremental tail retention and finalization

**Depends on:** P2.
**Deliverables:** projector state continuation; exact bounded raw tail; incremental buffer/byte accounting; EOF/abort/overflow recovery; partition-invariance tests; continuation documentation; P3 validation record.

### RED

- [ ] 3.1 Add partial-separator continuation tests for scheme-colon, scheme-colon-slash and scheme-percent suffixes; verify retention starts at the earliest viable scheme/escape position and suspicious isolated characters do not cause unjustified retention.
- [ ] 3.2 Add detected-but-unterminated and ordering tests; verify MATCH with end=None differs from NEED_MORE and completed candidates preceding an ambiguous suffix are consumed once in source order.
- [ ] 3.3 Add chunk-partition tests using one feed, each split and single-byte feeds; verify expected final text and ordered metadata are invariant under existing overflow rules.
- [ ] 3.4 Add secret-overlap, clean-EOF, abort and overflow/recovery tests; verify ordinary words, incomplete markers, byte limits and discarded suffix behavior have explicit expectations.
- [ ] 3.5 Add deterministic incremental work/buffer tests; verify they detect restarts over growing tails and repeated copying/re-encoding without measuring elapsed time.
- [ ] 3.6 Record the pre-implementation P3 test run; verify failures correspond to missing continuation, finalization or accounting behavior.

### GREEN

- [ ] 3.7 Integrate persistent recognizer state and exact tail ownership into projector draining; verify continuation consumes only new input with no reread, fixed overlap or duplicate candidate emissions.
- [ ] 3.8 Implement incremental retained-buffer and byte-limit accounting; verify P3 work/buffer assertions pass without repeatedly processing the whole growing tail.
- [ ] 3.9 Integrate existing secret holding with URL retention and offset conversion; verify earliest required retention and safe-prefix commitment pass secret-overlap tests.
- [ ] 3.10 Implement clean-EOF and abort transitions for the new outcomes; verify ordinary-word preservation and existing complete/incomplete candidate finalization assertions pass.
- [ ] 3.11 Implement overflow discard and recognizer-state reset at recovery; verify one existing oversized marker, no leaked suffix and correct processing after the raw boundary.

### INTROSPECT

- [ ] 3.12 Document continuation, ownership, coordinate conversion and finalization transitions; verify documented 8 KiB retention is not presented as an assumed input read size.
- [ ] 3.13 Audit incremental recognition and buffer paths for amortized bounded work and duplicate processing; deliver a review record with all findings resolved before the P3 gate.

### VALIDATE

- [ ] 3.14 Run P1–P3 tests and affected existing projection/lifecycle tests; verify zero exit status and deliver the P3 record covering partition invariance, byte bounds, EOF/abort and recovery.

## 4. P4 — Known/unknown projection and metadata

**Depends on:** P3.
**Deliverables:** classification-gated diagnostic interpretation; UNKNOWN metadata omission on every projection route; preserved KNOWN validation/identity; projection tests and documentation; P4 validation record.

### RED

- [ ] 4.1 Add UNKNOWN projection tests for normal boundaries, structured diagnostics and clean EOF; verify hidden source and empty hostname, host/path and fingerprint facts are asserted on every inventoried route.
- [ ] 4.2 Add KNOWN validation and identity tests; verify malformed authority, unsafe path, secret-bearing host, encoded forms and identity order/multiplicity retain existing permitted behavior.
- [ ] 4.3 Add interpretation-depth and UNKNOWN parser-bypass checks; verify a depth-four detection match does not authorize deeper URL interpretation and UNKNOWN is not parsed merely to decide hiding.
- [ ] 4.4 Record the pre-implementation P4 test run; verify expected failures concern the agreed metadata-policy change rather than accidental detection regressions.

### GREEN

- [ ] 4.5 Gate ordinary diagnostic candidate interpretation on classification; verify UNKNOWN hiding and KNOWN validation tests pass with existing redaction/path markers.
- [ ] 4.6 Apply the same classification gate to structured and clean-EOF metadata paths; verify P4 route coverage proves no UNKNOWN fact bypass remains.
- [ ] 4.7 Preserve existing known URL canonicalization and interpretation depth; verify known fingerprint/host-path assertions and depth-bound checks pass without globally narrowing shared hostname helpers.

### INTROSPECT

- [ ] 4.8 Document UNKNOWN metadata omission and KNOWN validity requirements at affected projection APIs; verify examples agree with tests and authoritative local exact-mode behavior.
- [ ] 4.9 Reconcile the P1 caller inventory against implemented gates; deliver a route-by-route review showing all bypass findings resolved and unrelated fetch/transport grammar unchanged.

### VALIDATE

- [ ] 4.10 Run P1–P4 tests and affected existing metadata/identity tests; verify zero exit status and deliver the P4 record identifying only intentional UNKNOWN compatibility differences.

## 5. P5 — Consumer channels and grouping

**Depends on:** P4.
**Deliverables:** integrated collector/retained-tail behavior; mode-independent safe SDK events; compatible grouping; user-facing policy documentation; P5 validation record.

### RED

- [ ] 5.1 Add collector/tail channel tests for all local modes with and without SDK delivery; verify UNKNOWN omission, exact source preservation including secrets, terminal neutralization and one selected retained representation are asserted.
- [ ] 5.2 Add host grouping tests for otherwise identical UNKNOWN diagnostics and distinct validated KNOWN identities; verify line and interactive repeat/numeric grouping expectations are explicit.
- [ ] 5.3 Add collector/fetch route regression coverage; verify known successful-fetch behavior remains intact and no side path produces UNKNOWN facts.
- [ ] 5.4 Record the pre-implementation P5 test run; verify already-passing integrations are recorded honestly and any expected missing behavior is identified precisely.

### GREEN

- [ ] 5.5 Integrate outcome/classification behavior into affected collector channels; verify P5 channel assertions pass without adding a retained representation or changing execution semantics.
- [ ] 5.6 Align grouping and any affected collector side paths with UNKNOWN metadata omission; verify grouping and fetch regression assertions pass without weakening known identity distinctions.

### INTROSPECT

- [ ] 5.7 Update applicable user-facing diagnostic policy documentation; verify whitelist, UNKNOWN hiding/grouping and unchanged exact/SDK policies match executable examples.
- [ ] 5.8 Review channel isolation and scope across the P5 diff; deliver findings confirming no new configuration, persistence, assembly identity, argv, timeout or shutdown-budget changes.

### VALIDATE

- [ ] 5.9 Run P1–P5 tests and affected existing collector/presentation acceptance tests; verify zero exit status and deliver the P5 record linking channel, grouping and documentation deliverables.

## 6. P6 — Final acceptance

**Depends on:** P5.
**Deliverables:** requirement-to-test/evidence matrix; one-off before/after performance evidence; complete-suite and project-check records; final scope review; explicit acceptance decision.

### RED

- [ ] 6.1 Deliver the acceptance matrix mapping every delta requirement/scenario and phase deliverable to existing tests or evidence; verify uncovered or unverified obligations are explicitly recorded as outstanding without manufacturing failures.
- [ ] 6.2 Discover the project's applicable configured lint/type checks; verify the acceptance record lists exact commands or a justified absence, without inventing checks or deferring earlier feature coverage.

### GREEN

- [ ] 6.3 Perform the one-off before/after recognition/capture experiment outside tests and CI; deliver baseline-compatible medians, profiles and allocation/work evidence confirming hotspot removal with unchanged stress fixtures, reporting single-feed and chunked results separately.
- [ ] 6.4 Run the complete mandated eight-worker loadfile pytest command; verify zero runner exit status and preserve the full stdout/stderr log, warnings and summary without exclusions.
- [ ] 6.5 Run the applicable configured lint/type checks; verify each exit status is zero and preserve complete outputs, or retain the verified absence recorded in 6.2.

### INTROSPECT

- [ ] 6.6 Inspect the final implementation diff against the binding scope; deliver a review confirming unchanged stress inputs, scheduler, AGENTS.md, shutdown budget and unrelated execution semantics, with implementation-time spec edits limited to task completion boxes.
- [ ] 6.7 Reconcile final evidence with the acceptance matrix; verify every obligation has an actual result and all findings are resolved, without claiming whole-pipeline linearity from recognizer-only evidence.

### VALIDATE

- [ ] 6.8 Revalidate affected checks after any final review fix; verify the full suite and applicable project checks reflect the final code state with preserved logs and successful statuses.
- [ ] 6.9 Deliver the final acceptance decision; verify P1–P6 gates and deliverables are all satisfied before marking implementation complete, otherwise explicitly report blocked acceptance and the outstanding obligations.

## Workflow follow-up

- Review the planning artifacts before starting implementation.
- Review implementation and acceptance evidence before explicitly requesting sync/archive.
- Sync/archive delta specs only through the requested OpenSpec workflow; these steps are outside tracked implementation completion.

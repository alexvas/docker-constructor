# Revised P3 ownership and cumulative-work audit

**Status: PASS for P3.** Ownership/work and focused P3 checks pass; final full
suite and types pass after the separately authorized registry cleanup race fix.
See `registry_cleanup.md` for deterministic RED/GREEN and preserved failed-run
evidence. P4–P6 remain pending. The approved fixed specification mandates delayed
marker delivery for the narrowly defined earlier crossing-secret prefix and
ordinary-prefix release on its clean-EOF non-match. P4–P6 remain pending.

## Production paths

`DiagnosticProjector` uses `CoordinatedProjection` (`diagnostic_coordinator.py`)
and one persistent `IncrementalRecognizer`. Every new Python source character
is fed once, including discarded overflow input. `consume()` retires outcomes;
it never rebuilds cursors/decoders or replays an owned suffix. There is no
recognizer `_tail`, retained-suffix reset or production regex fallback. Tests
make the historical regex/source-map helpers raise, while exercising both
projection products and completion/metadata.

The coordinator deque is the only retained raw owner. It stores characters
and their once-computed UTF-8 widths. Pending bytes are a cached integer and
cover the whole deque, never a read-size assumption. Popping subtracts saved
widths. `_pending` is an inspection-only property, not another raw string.
Settled URL snapshots exist transiently for interpretation/validation, not
search. Component userinfo provenance uses bounded percent units and coordinate
visits, not an input-sized map; interpretation remains at three passes.

Host-path collection previously constructed two projectors. It now aliases
`_retained_projector` to `_projector` and drains an independent transient
redacted product from that same raw owner. There is still exactly one selected
retained tail. Exact local content follows the unchanged terminal-safe route.
Rendering products are not normalized-host/fingerprint facts.

## Frontiers and overlap

- Recognition coordinates/cursors survive raw ownership commits. Pending
  decoder units and registered-secret prefixes remain bounded by fixed depth
  and registration. Existing P2 commit watermarks filter delayed fully
  committed secret outcomes but preserve straddlers and continuation atoms.
- Completed standalone secrets commit `<secret>` without waiting for a URL
  terminator. `TOKENht` continuation grows into `https` without refeeding `ht`.
  An earlier unresolved secret prefix blocks only source-order-dependent
  delivery; it is not emitted as ordinary text ahead of a later match. This
  behavior is mandatory for the approved crossing-separator exception, not
  discretionary withholding. Once the prefix resolves, the marker is emitted
  without waiting for an unrelated authority boundary or token terminator.
- Ordinary literal selection preserves leftmost/longest non-overlap: same-start
  extensions enlarge already-covered secret coverage without another marker;
  differently-started overlaps inside that selected interval do not invent a
  second replacement. Failure-prefix settlement still sees the earliest
  eligible uncommitted suffix. URL component coverage considers all raw secret
  spans, including decoded views and crossings, independently of that selection.
- Redacted URL coverage commits once at its full separator. Host/path output
  commits secret-settled authority/path separately; userinfo/IPv6/port ambiguity
  must resolve first. A masked host retains the marker, not another original
  hostname payload. Metadata waits for eligible source completion/validation.
- Clean EOF flushes percent state once; abort grants no pending URL facts.
  Clean EOF resolves the earlier incomplete separator-crossing literal prefix
  as a non-match: `Xhttps://ho` with secret `Xhttps://host` yields `X<redacted>`.
  Existing complete secret records still apply; other incomplete prefixes and
  abort/overflow remain fail-closed. Early rendering is never retracted. Only uncovered/unsettled display receives
  incomplete/oversized token markers. Unsupported words remain ordinary text.
- Overflow abandons metadata and discards through a raw boundary while feeding
  existing recognizers. Crossing literal-secret state remains live. Synthetic
  line overflow is delivered independently, not swallowed as part of a URL.

## Accounting and results

RecognitionStats now exposes cumulative prefix inspection bounds, every core
cursor's failure/output work, and charged coordinate coverage iteration.
The coordinator iterates coordinate records for minima/intersections without
calling batch deduplication/sorting. Existing transition, emission, speculation,
record-transfer/ordering and retirement counters remain cumulative.
ProjectionWork separately measures raw encoding, appends, pops, settled
snapshot materialization and provenance visits. It reports one recognizer
instance; tests/audit independently intercept **all `_Pipeline` constructions**
to prove this is real, not reset-hidden accounting. Cursor and pending-state
ownership is inspected through recovery and at each feed.

`phase3_incremental_bounds_probe.py` counts all instances and all applicable
recognition maintenance fields, plus buffer/materialization/provenance work.
At fixed registration, observed work is:

| Scenario | Redacted | Host-path |
| --- | --- | --- |
| Ordinary x 3000 / 6000 | 186029 -> 372029 | 186029 -> 372029 |
| 100 / 200 overlapping secret / URL commits | 281129 -> 562229 | 282229 -> 564429 |
| 2 / 4 overflow-recovery cycles | 676833 -> 1353637 | 676885 -> 1353741 |
| 100 / 200 matching + mismatching crossing-prefix settlements | 261117 -> 522217 | 264417 -> 528817 |

Character feeds and one-feed execution have identical measured work and text.
The independent partition corpus checks complete text and ordered facts,
including original random-corpus sizes/seed/alphabets, under the new contract.
It now performs 33558 comparisons and compares ordered fingerprints as well as
text/hostname/host-path results using a shared ephemeral comparison key. Keys
and fingerprints are never printed or persisted in evidence.
No elapsed-time assertions or old arbitrary suffix batch-equivalence claims
are used. Registration cost/storage can depend on patterns; live search state
cannot depend on stream history. Output-coordinate storage is bounded by
pending commitment and is output-sensitive.

## Scope and resolved findings

Development full-suite failures found malformed-authority facts and encoded
userinfo misclassification. Shared port validation and streamed raw userinfo
provenance fixed those defects; tests/fixtures were not weakened. Query-only
secrets no longer mask path facts. Fixed-marker products are distinguished from
source substrings in secret assertions (`sec` occurs in the prescribed marker).
The initial audit treated the str-valued display Enum as a payload string;
that audit predicate now excludes only that typed configuration attribute.

Historical P3 evidence is under `historical-phase3/`. It does not prove revised
behavior. `phase3_test_migration.md` accounts for all old scenario families and
intentional specification changes before temporary test backups are removed.

This phase concerns P3 continuation/ownership/rendering/lifecycle integration,
not completion of P4/P5 metadata/helper/fetch-route or presentation acceptance
matrices. Those tasks stay pending. No whole-pipeline linearity, global URL-free
SDK guarantee for unknown schemes, or unrestricted suffix equivalence is claimed.
Stress payloads, 8/64 KiB limits, worker scheduling, AGENTS.md and shutdown budgets
are unchanged. Specification edits are completion boxes only.

## Resolved finding: early delivery versus an earlier unresolved source prefix

The former unconditional early-delivery blocker was resolved by explicit user
approval of the fixed specification, before this apply session. With secret
`Xhttps://host`, first feed `Xhttps://ho` must return no output. Matching
continuation requires `<secret><redacted> done`; a mismatching continuation
requires `X<redacted> done`; clean EOF requires `X<redacted>` without a secret
or incomplete-token marker. The marker is delivered as soon as the literal
prefix resolves, not deferred to a terminator. All other supported starts keep
mandatory immediate separator-triggered redacted delivery.

The new behavioral RED exposed the missing clean-EOF outcome (2 failed,
34 passed, exit 1). `_drain_coordinated` now resolves only that earlier crossing
literal prefix as a non-match at clean EOF, preserving any completed secret
records and original-source fact gates. Regression tests preserve incomplete
handling for partial separators and ordinary partial secrets, as well as
existing abort/overflow behavior. SDK tests cover all three selected local
modes using the same persistent recognizer; exact source output is unchanged.

Before the delayed-activation follow-up, P3 and the full suite passed; these
logs are historical. Current focused verification: P3 41 passed / 11 subtests,
exit 0. The first follow-up full suite failed due to registry-harness cleanup on a
closed event loop and was not accepted. After user-authorized deterministic
reproduction and repair, the final full suite has 6352 passed / 13 skipped /
11 warnings / 17535 subtests, exit 0. See `registry_cleanup.md`. Ownership/partition audits,
P1A/P2 audits and configured types also pass with full logs and statuses in
`phase-3.md`. No failures were excluded, no warnings suppressed and no existing
stress inputs reduced. This resolves P3 only, not later phase acceptance.


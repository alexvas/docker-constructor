# Phase 2 Verification Evidence — Supported-scheme and secret raw recognition

Change: `linearize-diagnostic-url-detection`
Schema: `spec-driven`
Phase: **P2 — Compact supported-scheme and secret recognition** (tasks 2.1–2.11)
Depends on: revised P1 gate (satisfied) and new P1A gate (satisfied)
Repository: `/home/aavasiljev/work/home/docker-constructor.git`

Phase 2 is the raw recognition engine: supported-only URL detection plus
literal and up-to-three-pass decoded secret matching, built on the P1A
Aho-Corasick core.  It does **not** rewire the projector (that is P3).

This record was revised after the reviewer required four corrections: (a)
replay-free `consume()` that retires committed outcomes instead of rebuilding
from a retained `_tail`; (b) scheme interpretation capped at three decoding
passes while separator depth four is retained; (c) promptly discarded decoded
secret speculation outside supported URLs; and (d) tests, audit and evidence
that cover all three.  The affected P2 boxes were reopened for these fixes and
are closed again only after the runs below pass.

It was revised a second time after the reviewer identified quadratic work that
the character-transition counters did not expose: (a) `retain_from()` rebuilt
the growing speculative deque after every character; (b) `_candidate_spans()`
rescanned forward to the terminator for every candidate; and (c)
`recognize_with_secrets` tested every decoded secret against every candidate
span. At that revision pruning used heaps (superseded below); candidate spans share
one cached token boundary, coverage uses merged intervals with a monotonic
sweep, `RecognitionStats` reports speculative-record visits, boundary
characters and coverage checks, and adversarial input-doubling regressions
were added.  The affected P2 boxes were reopened again and are closed only
after the runs below pass.

It was revised a third time after the reviewer found two remaining quadratic
maintenance paths: (a) `_SecretObserver.finalize_token()` re-heapified the whole
finalized collection at every URL boundary (15 150/60 300 record visits for
100/200 repeats of `https://host/%61` with secret `a`) while the counters
reported only the newly promoted records; and (b) `_LayerMatcher.retire()`
rebuilt the whole remaining candidate deque on every commit.  Finalized secret
matches at that revision lived in an end-ordered deque sorted from each new
batch (superseded below), per-layer candidates are recorded in raw-start order so commits
pop from the left, and `RecognitionStats` reports record transfers, ordering
work and retirement inspections.  The affected P2 boxes were reopened for
this revision and are closed only after the runs below pass.

## Fourth correction: capped prefixes and registration-bounded ordering

The previous prefix tracker still accepted fourth-pass scheme atoms, and
heap/sort maintenance was O(M log M) for a single long token (reported ordering
work 2100/4800 for 100/200 `a` occurrences). Those claims are superseded.
`_feed_prefix` now rejects extra decoded scheme atoms while still permitting
extra separator atoms. `%25252568` is NO_MATCH; later independent supported
prefixes retain their exact raw start. Three-pass schemes with four-pass
separators remain supported.

Secret maintenance now uses registration-many ordered queues and bounded-head
merging, as detailed below and in `phase2_allocation_audit.md`. Coverage keeps
the original recognized token start across commits so decoded straddlers are
not pruned by a moved display frontier. `CappedPrefixAndLongTokenTests` verifies
exact coverage, overlaps, encoded/literal input, terminator/EOF, character feeds
and successive consumption. Total long-token maintenance is 3336 -> 6636 for
100/200 encoded `a` units, and 11718 -> 23418 with `a`, `aa`, `aaa`.
Tasks 2.2, 2.4, 2.7–2.11 were reopened and closed only after the current logs
passed. Missing-module RED establishes test absence failures, not proof of
which earlier implementation bugs failed; the current regressions and audit
provide the behavior/work evidence.

## Pending-escape cap correction

`_LayerMatcher.prefix_state()` now applies the scheme cap before retaining
pending `%` or `%<hex>` atoms. At the capped layer only a viable separator
edge of an already permitted prefix can justify retention; a present hex
nibble must be compatible with that edge (`3` for colon, `2` for slash).
`%2525256` and partially encoded fourth-pass scheme prefixes are NO_MATCH.
Incomplete depth-four separator spellings after permitted three-pass schemes
still retain the exact scheme start. Negative every-split tests and positive
single-character-feed tests cover both paths and later supported-prefix
retention. Tasks 2.2, 2.7, 2.9–2.11 were reopened and reclosed after the refreshed
logs below passed. Secret maintenance bounds are unchanged.

## Pending-transducer decoded-secret pruning correction

A shallower decoded view can emit a secret before a deeper transducer emits
its first supported scheme atom. Pruning now includes the earliest pending
transducer raw provenance in its frontier, rather than discarding those
matches when no matcher has established a prefix yet. After EOF flush, pruning
runs again before promotion. Fixed-depth pending units bound this extra
speculation window; this introduces no payload replay or unbounded history.

`PendingTransducerSecretTests` checks `%%25368ttps://host` in batch (including
raw termination) and at every split: the URL starts at 0 and coverage includes
literal [0,1), literal [1,2), and depth-1 [1,4). Character-feed ordinary-input
probes bound each decoded observer's speculation and decoder pending state,
retire outcomes at each commit, and assert linear doubled-input work.
Tasks 2.8, 2.10 and 2.11 were reopened and reclosed after the refreshed logs
passed.

## Committed-secret delayed-emission correction

Each secret observer now retains a monotonic commit watermark. Delayed decoder
atoms still advance its cursor and bounded provenance, but completed matches
whose raw end is at or before that watermark are not enqueued. This prevents
[13,14) from reappearing after feeding `https://host/%`, consuming 14, and then
feeding `zz` or finalizing. The check is on the end, not the start: delayed `%z`
and encoded `%25` matches crossing the cut remain eligible. No decoder reset,
provenance deletion or suffix replay is introduced. Filtering work is counted.

`CommittedDecoderEmissionTests` covers delayed feed and EOF emissions, exact
cut boundaries, continued/EOF straddlers, encoded matches crossing commits,
retirement after coverage, monotonic counters and unchanged source-feed counts.
Tasks 2.8, 2.10 and 2.11 were reopened and reclosed after current validation.

## Deliverables and evidence map

| Deliverable | Location |
|---|---|
| Shared supported-scheme P1A automaton and ``scheme://`` prefix trie | `docker/versioning/diagnostic_recognizer.py` (`_SCHEME_AUTOMATON`, `_SCHEME_URL_TRIE`) |
| Bounded incremental percent transducers with raw start/end provenance | `diagnostic_recognizer.py` (`_Transducer`, `_decode_one_layer`) |
| Supported-scheme matcher, three-pass scheme cap and depth-four separator check | `diagnostic_recognizer.py` (`_LayerMatcher`, `_scheme_match_allowed`) |
| Replay-free incremental consumption with outcome retirement | `diagnostic_recognizer.py` (`_Pipeline.retire`, `IncrementalRecognizer.consume`) |
| Literal and decoded secret observers with bounded provenance and speculation | `diagnostic_recognizer.py` (`_SecretObserver`, `retain_from`, `retire`) |
| Raw / encoded / differential / work / secret / commit-crossing tests | `tests/test_diagnostic_recognizer_phase2.py` |
| Missing-module P2 RED run (full log + exit status) | `verification/phase2_red.log` (127 failed, 2 passed, exit 1) |
| P2 GREEN run | `verification/phase2_green.log` (118 passed, 6780 subtests, exit 0) |
| Algorithm / bounds documentation (task 2.9) | module docstring §Fixed-depth transition/state bounds |
| Allocation / speculation / fallback audit (task 2.10) | `phase2_allocation_audit.py` + `.md` + `.log` (exit 0) |
| P1/P1A/P2 + affected pre-existing validation run | `verification/phase2_validation.log` (743 passed, 14043 subtests, exit 0) |
| Applicable configured type check | `verification/phase2_types.log` (exit 0) |

---

## RED

### 2.1 Supported-only raw recognition obligations

`SupportedSchemeTests` binds literal separator detection at the raw start,
exclusive raw terminator ends, `end=None` for an unterminated candidate,
terminator variants (space/tab/newline/CR/`<`/`|`/`"`/control), the negative
`note:/section` and `npm:package` forms, every supported scheme, unknown-scheme
text (`custom`, `custom+transport`, `gopher`, `npm`, `httpx`, `ftp2`, `socks6`
and an arbitrarily long `customxxx…` scheme) as `NO_MATCH`, embedded supported
starts (`TOKENhttps://host`), earliest supported source selection, offset
handling, ordinary non-viable tokens, supported prefixes and started
separators (`NEED_MORE`), the trailing-token `retain_from` (including the
earliest supported restart inside a token), and multibyte character offsets.

### 2.2 Encoded recognition obligations — three scheme passes, depth-four separator

`EncodedSchemeTests` covers every supported scheme with scheme and separator
encoded together at depths zero through three, scheme-only and separator-only
encoding, mixed-depth separators (`:%2f/`, `%3A%2F/`, `%253A//`, `%3A//`),
separately encoded escapes (`%25%33%41%25%32%46/`), hex letter case, uppercase
and encoded-uppercase scheme spellings, encoded unknown schemes as `NO_MATCH`,
conservative encoded units that do not manufacture a scheme, malformed escapes,
incomplete trailing escapes (`NEED_MORE`) and an encoded separator without a
supported scheme.

The corrected obligations are explicit:

* `test_four_pass_encoded_scheme_with_literal_separator_is_not_detected` — a
  four-pass-encoded scheme is **not** a candidate even with a literal `://`;
* `test_four_pass_scheme_and_separator_is_not_detected` — a four-pass scheme
  and separator is **not** a candidate;
* `test_separator_encoded_at_every_depth_over_literal_scheme` — separator
  depths zero through four remain recognized;
* `test_depth_four_separator_over_permitted_encoded_scheme` — a literal or
  one/two/three-pass-encoded scheme followed by a depth-four separator remains
  recognized;
* `test_depth_five_separator_is_not_recognized` — no fourth whole-URL pass.

### 2.3 Bounded deterministic differential corpus, secrets and crossings

`DifferentialCorpusTests` compares `recognize` against an independent naive
oracle (`_oracle_start`).  The corrected oracle does **not** decode the whole
URL prefix four times: layers zero through two search the combined
``scheme://`` in the one-, two- and three-times-decoded views, while the last
layer searches the scheme in the three-times-decoded view (a fourth pass would
decode scheme characters) and locates only the separator in the
four-times-decoded view.  Coverage:

* a curated corpus of 47 raw/encoded/multibyte inputs, including depth-four and
  depth-five separator spellings, mixed depths, unknown schemes and a mangled
  scheme whose three-pass view differs from its four-pass view
  (`%1%66%74%70%2525253A…`);
* 4000 deterministic PRNG inputs of length 0–18 and 400 of length 20–59 over a
  fixed alphabet (`random.Random(20240607)`, `random.Random(987654321)`);
* 2000 deterministic supported spellings inserted after random prefixes, with
  independent scheme and separator encoding depths up to five
  (`random.Random(20240613)`).

`LiteralSecretTests` adds literal secret selection and cross-terminator
coverage; `SchemePrefixSuffixOverlapTests` adds scheme-prefix suffix overlap
and partition invariance with registered secrets.

### 2.4 Deterministic work/state and provenance obligations

`WorkStateRegressionTests` asserts, without any elapsed-time measurement:

* uninterrupted and separated scheme runs double their structural counters at
  most linearly (`layer_char_visits`, `emissions`, `atoms`), with
  `emissions > 0` so the comparison is not vacuous;
* a 100 000-character token, a 60 000-character escaped token and a long
  `https`-prefixed token keep `peak_pending_chars <= 4` and
  `peak_atom_queue <= 4`;
* batch recognition retains a constant number of matches (`<= 1` per layer)
  up to 5000 candidates;
* secret cursor work is non-zero only when secrets are registered; and
* scheme registration storage is bounded by `1 + sum(len(scheme))`.

`SourceRangeProvenanceTests` asserts inclusive/exclusive raw ranges for
literal, decoded, nested, malformed and incomplete emissions, the
observer-retained scheme/colon ranges, and unchanged candidate coordinates.

### 2.8 Commit-crossing obligations

`IncrementalContinuationTests` now includes explicit commit-crossing
regressions:

* `test_overlapping_secret_and_scheme_prefix_survive_a_commit` — feed
  `TOKENht` with secret `TOKENht`, `consume(7)`, then feed `tps://host`; the
  supported URL is recognized at the overlapping start `5` and the secret is
  not reported twice;
* `test_literal_secret_straddling_a_commit_is_reported_once` — a secret
  spanning the commit boundary is reported once;
* `test_literal_secret_spanning_a_commit_and_raw_terminator` — a secret
  crossing a raw terminator across a commit is reported once;
* `test_consumption_does_not_replay_input_processing` — `consume()` does not
  change `chars`, `layer_char_visits`, `emissions`, `transitions` or
  `secret_emissions`.

### 2.5 Pre-implementation P2 run

With `docker/versioning/diagnostic_recognizer.py` temporarily absent,
`phase2_red.log` records:

```
.venv/bin/python -m pytest tests/test_diagnostic_recognizer_phase2.py \
  -n 8 --dist loadfile --tb=short -q
127 failed, 2 passed in 5.13s
red exit status: 1
```

There are **no collection errors, import errors or environmental failures**.
Honest accounting: there are 118 test methods; 116 fail at method level with the
explicit `AssertionError: docker.versioning.diagnostic_recognizer must define
the P2 recognizer`, and 2 (`test_terminator_variants_end_the_candidate`,
`test_range_provenance_keeps_candidate_coordinates`) report `PASSED` only
because their `self.fail()` calls execute inside `subTest` blocks, whose 11
per-case failures are counted as `SUBFAILED` items in the summary.  Thus
`127 failed = 116 method failures + 11 subtest failures`; no test genuinely
passes without the module.

---

## GREEN

### 2.6 Bounded percent transducers and provenance

`_Transducer` is a fixed chain `L0 -> L1 -> L2 -> L3`.  Each node consumes one
layer character, decodes a complete `%XX` into one character and copies
incomplete/invalid escapes verbatim, emitting
`(character, source_start, source_end, from_token)`.  `source_start` is
inclusive and `source_end` exclusive: a decoded `%XX` spans all contributing
raw characters, a copied literal spans its one raw character, and
malformed/incomplete escapes preserve the literal ranges of their characters.
Every forwarded character increments `RecognitionStats.emissions`.  Pending
state is at most two characters per transducer.

### 2.7 Fixed-scheme transitions integrated with the P1A core

`_LayerMatcher` atomises each layer's characters (one literal or one complete
`%XX`) and feeds the atom to the shared, immutable
`AhoCorasick(SUPPORTED_SCHEMES)` cursor.  When the automaton reports a
supported scheme, the matcher verifies the following `:`, `/`, `/` atoms (each
possibly one more encoded level) and records the candidate's raw scheme start
and colon range.  Selection and coverage stay outside the generic core.

**Three scheme passes, depth-four separator.**  The last matcher carries a
scheme cap: its own atomisation is the *additional separator-decoding step*, so
a scheme match that consumes an atom produced by decoding a `%XX` there would
be a fourth scheme-decoding pass and is refused (`_scheme_match_allowed`).  A
scheme that is already literal in the last layer's three-pass-decoded input is
accepted, and the same decoded atoms remain available to complete a depth-four
separator.  This yields exactly: scheme depths 0–3 recognized, scheme depth 4
rejected, separator depths 0–4 recognized, and a permitted scheme followed by a
depth-four separator recognized.

### 2.8 Secret matching, bounded speculation and replay-free continuation

`_SecretObserver` feeds one decoded layer character at a time to a private P1A
cursor.  Literal secrets are matched on the raw stream (`depth == 0`); secrets
are matched on the first three decoded layers (`depth` 1–3) and reported only
when their raw span lies inside a recognised URL.  Decoded speculation is
bounded: after every character the pipeline computes the earliest offset from
which a supported URL could still cover text (the minimum of the viable prefix
starts and any detected candidate start in the current token) and discards
decoded matches that start before it; when the token contains no viable
prefix or candidate, all decoded matches are discarded.  When the token closes
at a raw terminator, the eligible in-token matches are promoted to final
outcomes (so a decoded secret ending exactly at the terminator is kept) while
ineligible ones are dropped.  Literal matching is
never pruned and spans all text and raw terminators.

`IncrementalRecognizer` no longer keeps a source `_tail` and no longer
rebuilds.  `consume(offset)` retires only already-recorded candidates whose
start is before the commit and secret matches whose end is at or before it,
while preserving every Aho-Corasick cursor, percent-decoder pending unit and
supported-prefix continuation state.  A URL prefix that overlaps committed
secret characters keeps growing, and a commit never re-feeds input.

**Bounded incremental pruning, boundary reuse and coverage sweep.**  The
reviewer's second-round findings are addressed as follows:

* `_SecretObserver` uses **one ordered speculative deque per registered
  pattern**, plus an end-ordered finalized deque. For a fixed-length pattern,
  emission starts and ends increase, including through decoded provenance.
  Pruning pops ineligible heads; retirement pops completed heads and leaves
  straddlers in place. Finalization merges only registration-many heads,
  without heaps, token-sized sorting, or historical rebuilding. Occupancy is
  accounted incrementally. All bucket inspections, queue operations and
  transfers are counted. At P registered patterns, maintenance costs
  O(P * (N + M + commits)), linear at fixed registration.
* `_LayerMatcher` records candidates in increasing raw-start order within its
  own layer (each layer owns a separate queue), so `retire()` pops committed
  candidates from the left instead of re-filtering the remaining deque on every
  commit.
* `_candidate_spans()` sorts candidates by start and caches the shared token
  terminator, rescanning only when a candidate begins at or past the previous
  terminator, so every raw character is examined at most once across all
  candidates and absolute/unterminated coordinates are unchanged.
* `_cover_secret_matches()` merges candidate spans into disjoint intervals and
  sweeps merged intervals and start-ordered secrets with a monotonic index, so
  no decoded secret is tested against every span.  Literal matches still pass
  through unfiltered.
* `RecognitionStats` gains `speculative_record_visits`,
  `secret_record_transfers`, `secret_ordering_work`,
  `candidate_retirement_visits`, `boundary_chars` and `coverage_checks`;
  `recognize_with_secrets_and_stats` exposes them for the batch secret path and
  they accumulate across `consume()`.

**Finalization and retirement regressions.**  `FinalizationWorkTests` processes
100 and 200 repeats of `https://host/%61` (secret `a`) in batch and incremental
form, asserting exact coverage (one depth-1 `%61` span per repeat) and linear
finalization maintenance (`1500 -> 3000` reported transfer+ordering work instead
of the former `15 150/60 300` uncounted heapify visits).
`SuccessiveConsumptionTests` feeds 50/100 completed candidates (literal,
percent-encoded and scheme-overlapping-secret variants) and consumes every
token boundary individually, asserting each candidate record retires exactly
once, the remaining starts stay ordered, and `candidate_retirement_visits` grows
linearly.

**GREEN run** — `phase2_green.log`:

```
118 passed, 6780 subtests passed in 10.28s
green exit status: 0
```

---

## INTROSPECT

### 2.9 Fixed-depth transition/state bound and depth distinction

Documented in the `diagnostic_recognizer` module docstring
(§Fixed-depth transition/state bounds):

* `_LAYERS` is fixed at four; a raw character is consumed by layer 0 and each
  character it decodes to by at most the remaining layers, so recognition is
  O(raw input) with a constant number of transitions per raw character.
* Every transducer retains fewer than three pending characters, every layer
  matcher queues at most three atoms and the supported-prefix tracker keeps one
  active trie node per distinct supported prefix; live state is independent of
  stream length.
* Registration storage depends only on the configured scheme set and secret
  set (the scheme automaton has 39 nodes for the fixed 17 schemes); no
  input-sized decoded search copy, per-character source map or encoded-variant
  table is built.
* Supported **scheme** characters are interpreted through at most three
  percent-decoding passes; the **separator** keeps effective depth four.  A
  depth-five separator is not recognised and detection never authorises a
  fourth whole-URL interpretation pass.
* Decoded secret observers retain only matches that can still belong to a
  supported URL; ordinary text never accumulates decoded speculation, and
  pruning/retirement are bounded per record rather than a per-character
  rebuild.

### 2.10 Allocation / speculation / fallback / decoding-scope audit

`phase2_allocation_audit.py` (one-off, timing-free; `phase2_allocation_audit.log`,
exit 0) and its summary `phase2_allocation_audit.md` record:

* imports only the allowlist; no `re`, no legacy detector helper and no
  variant table;
* scheme automaton `node_count = 39 <= 87`; secret automaton registers each
  non-empty secret once;
* layer provenance maps and escaped-atom sets stay `<= _MAX_SCHEME_LEN + 4`,
  atom buffers `< 3`, separator queues `<= 3`, secret provenance windows
  bounded by the longest secret, transducer pending `< 3`, peak pending
  characters `2 < 4`;
* the scheme/separator split: depths 0–3 with a depth-four separator detected,
  four-pass scheme candidates refused, last layer capped;
* 5000 repeats of ordinary `%54OKEN` retain zero decoded matches with
  `peak_retained_secrets <= 1`, while a decoded secret inside a supported URL
  (including one ending at a raw terminator) is retained; and
* the incremental recognizer has no `_tail`/`_rebuild`, an overlapping prefix
  survives `consume()` (start `5`), the committed secret is not reported twice;
* no pipeline or layer attribute holds an input-sized string or list, and
  structural work grows at most linearly when the input doubles;
* speculative matches use registration-many ordered deques (no heap or sort)
  and finalized matches use an end-ordered deque that is never heapified; the
  `https://host/` + 100 vs 200 `a` regression keeps pruning visits linear
  (`1039 -> 2039`), the counters survive `consume()`, and boundary scanning and
  coverage comparisons stay linear when the URL count doubles, including all
  candidates embedded in one token (`boundary_chars <= len(input) + 8`);
* finalization orders only each token's new batch: reported
  transfer+ordering work is linear (`1500 -> 3000`) for 100 vs 200 repeats of
  `https://host/%61` in both batch and incremental forms, and never revisits
  historical records;
* candidate retirement is incremental and output-sensitive: each per-layer
  candidate record retires exactly once (`candidate_retirement_visits ==`
  records retained before the commits) with linear total work (`200 -> 400` for
  50 vs 100 candidates), and each layer's candidate queue stays in raw-start
  order.

---

## VALIDATE

### 2.11 P2 validation record

**Command** (`phase2_validation.log`):

```
.venv/bin/python -m pytest \
  tests/test_diagnostic_recognition_contract.py \
  tests/test_aho_corasick_core.py \
  tests/test_diagnostic_recognizer_phase2.py \
  tests/test_host_diagnostic_projection.py \
  tests/test_host_path_projection.py \
  tests/test_host_diagnostic_grouping_phase7.py \
  tests/test_host_diagnostic_identity_phase7.py \
  tests/test_npm_diagnostic_collection_phase8.py \
  tests/test_npm_fetch_phase4.py \
  tests/test_npm_environment_streaming.py \
  tests/test_retained_tail_phase6.py \
  -n 8 --dist loadfile --tb=short -q
```

Result:

```
743 passed, 14043 subtests passed in 25.73s
validation exit status: 0
```

This includes the revised P1 contract tests, the P1A core tests, the new P2
tests, and every pre-existing suite that exercises detection, projection,
collection, fetch or the retained tail.  Zero exit status confirms the revised
recognizer introduces no regression on the currently integrated routes.

**Applicable configured type check** (`phase2_types.log`):

```
./scripts/check-types
All checks passed!
types exit status: 0
```

### Scope: projector integration stays in P3

P2 introduces the raw engine only; the projector is **not** rewired here.  The
committed projector baseline imports no recognizer, and the validation suite
above is green with it.  A pre-existing uncommitted Phase 3 projector
experiment that prematurely wired `IncrementalRecognizer` into projection was
invalidated (backed up at `/tmp/diagnostic_projection.phase3.bak` and reverted
to its committed baseline) because it encoded the superseded unknown-scheme
withholding policy.  That integration, and the revision of the affected
projection/collection tests, belongs to P3/P4 and is not claimed here.

The complete mandated eight-worker suite and the before/after performance
experiment remain P6 deliverables and are not claimed here.

### P2 deliverable check

| Task | Deliverable | Evidence | Status |
|---|---|---|---|
| 2.1 | Supported-only raw tests (separators, negatives, unknown text, embedded starts, earliest supported selection, raw terminators) | `…::SupportedSchemeTests`; `phase2_green.log` | done |
| 2.2 | Scheme passes ≤3 and separator depth ≤4; mixed/nested/escape tests; no fourth interpretation | `…::EncodedSchemeTests`; `phase2_green.log` | done |
| 2.3 | Bounded differential corpus vs corrected oracle; literal/decoded secret selection, suffix overlap, cross-terminator secrets | `…::DifferentialCorpusTests`, `…::LiteralSecretTests`, `…::SchemePrefixSuffixOverlapTests`; `phase2_green.log` | done |
| 2.4 | Deterministic state/work and raw-provenance assertions | `…::WorkStateRegressionTests`, `…::SourceRangeProvenanceTests`; `phase2_green.log` | done |
| 2.5 | Missing-module P2 RED run | `phase2_red.log` (127 failed, 2 passed, exit 1) | done |
| 2.6 | Bounded percent transducers and provenance without decoded payload copies | `diagnostic_recognizer.py::_Transducer`, `_decode_one_layer`; audit | done |
| 2.7 | P1A integration with fixed-scheme transitions and the three-pass scheme cap | `diagnostic_recognizer.py::_SCHEME_AUTOMATON`, `_LayerMatcher`; `phase2_green.log` | done |
| 2.8 | Replay-free `consume`, overlapping-prefix preservation, secret matching in URL views, bounded decoded speculation, incremental finalization/retirement | `diagnostic_recognizer.py::IncrementalRecognizer`, `_SecretObserver`, `_LayerMatcher`; `…::IncrementalContinuationTests`, `…::SecretRetentionWorkTests`, `…::SuccessiveConsumptionTests` | done |
| 2.9 | Fixed-depth bound / depth-distinction documentation | module docstring §Fixed-depth transition/state bounds; §2.9 | done |
| 2.10 | Allocation / speculation / fallback / decoding-scope, pruning/coverage and finalization/retirement work audit | `phase2_allocation_audit.py` + `.md` + `.log`; §2.10 | done, no blocking findings |
| 2.11 | Adversarial boundary/coverage, finalization and successive-consumption regressions and P1/P1A/P2 + affected pre-existing validation | `…::AdversarialCoverageWorkTests`, `…::FinalizationWorkTests`, `…::SuccessiveConsumptionTests`; `phase2_validation.log` (exit 0); `phase2_types.log` (exit 0) | done |

Phase 2 gate: **satisfied**.  P3 integration may proceed from this core.

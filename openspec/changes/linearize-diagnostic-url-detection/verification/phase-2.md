# Phase 2 Verification Evidence — Compact Raw Recognition

Change: `linearize-diagnostic-url-detection`
Schema: `spec-driven`
Phase: **P2 — Compact raw recognition** (tasks 2.1–2.11)
Depends on: P1 (satisfied)
Repository: `/home/aavasiljev/work/home/docker-constructor.git`
Pre-P2 revision: `87c9a64 Define the diagnostic URL recognition result contract`

Phase 2 adds the raw recognition engine only. It does not rewire the projector,
consumer channels or metadata gate — those are P3–P5. No pre-existing
executable path imports the new module yet, so P2 changes no existing behavior.

## Deliverables and evidence map

| Deliverable | Location |
|---|---|
| Bounded incremental percent transducers with raw provenance | `docker/versioning/diagnostic_recognizer.py` (`_Transducer`, `_decode_one_layer`) |
| Structural observers and equivalent-state dominance | `docker/versioning/diagnostic_recognizer.py` (`_MatchObserver`, `_PrefixObserver`, `_Pipeline`) |
| Selected-candidate raw boundary + bounded scheme classification | `docker/versioning/diagnostic_recognizer.py` (`_Pipeline.best_match`, `_find_terminator`, `_classify_raw_scheme`) |
| Raw / encoded / differential / work tests | `tests/test_diagnostic_recognizer_phase2.py` |
| Pre-implementation P2 run (full log + exit status) | `verification/phase2_red.log` (44 failed, exit 1) |
| P2 GREEN run | `verification/phase2_green.log` (64 passed, 4762 subtests, exit 0) |
| Algorithm documentation (task 2.9) | `diagnostic_recognizer.py` module docstring, §Algorithm/§Bounds (this record) |
| Allocation / fallback audit (task 2.10) | `verification/phase2_allocation_audit.md` |
| P1/P2 + affected pre-existing validation run | `verification/phase2_validation.log` (622 passed, 11624 subtests, exit 0) |
| Applicable configured type check | `verification/phase2_types.log` (exit 0) |

---

## RED

### 2.1 Raw recognition obligations

`RawRecognitionTests` binds literal separator detection at the raw start,
exclusive raw terminator ends, `end=None` for an unterminated candidate,
terminator variants (space/tab/newline/CR/`<`/`|`/`"`/control), the negative
`note:/section` and `npm:package` forms, arbitrarily long UNKNOWN schemes,
KNOWN/UNKNOWN classification, earliest-source selection for an earlier encoded
candidate versus a later literal one, offset handling, ordinary non-viable
trailing tokens (`NO_MATCH`), plain scheme words and started separators
(`NEED_MORE`), the trailing-token `retain_from`, and multibyte character
offsets.

### 2.2 Encoded recognition obligations

`EncodedRecognitionTests` covers literal depth zero, encoded depths one through
four, that a depth-five separator is **not** recognized (no fourth
interpretation pass), mixed-depth separators (`%253A%2F/`, `:%2f/`,
`%3A%2F/`), separately encoded escapes (`%25%33%41%25%32%46/`), hex letter
case, encoded KNOWN/UNKNOWN schemes, conservative encoded scheme units,
malformed escapes, incomplete trailing escapes (`NEED_MORE`) and a separator
without a scheme.

### 2.3 Bounded deterministic differential corpus

`DifferentialCorpusTests` compares `recognize` against the legacy
`_find_url_start` / `_find_terminator` / `_url_prefix_state` oracle for:

* a curated corpus of 44 raw/encoded/multibyte inputs, including overlapping
  units (`a%3A%3A%2F%2Fb`, `a%2F%2Fb://c`), earlier-encoded versus later-literal
  starts (`%2525253A%2525252F%2525252Fhost` before nothing / mixed lists) and
  multibyte prefixes (`éhttps://host`, `€x%3A%2F%2Fhost`);
* 4000 deterministic PRNG inputs of length 0–18 and 400 of length 20–59 over a
  fixed alphabet of scheme characters, escapes, separators, terminators and a
  multibyte character (`random.Random(20240607)` / `random.Random(987654321)`).

Every case agrees on the three outcomes and their raw coordinates, so no
metadata-policy exception was needed at P2; the only intentional P4
differences are metadata (UNKNOWN facts), which this phase does not derive.

### 2.4 Deterministic work/state obligations

`WorkStateRegressionTests` asserts, without any elapsed-time measurement:

* uninterrupted and separated scheme runs double their structural counters at
  most linearly (`layer_char_visits`, `emissions`, `atoms`, `transitions`),
  with `emissions > 0` asserted so the comparison is not vacuous;
* representative non-empty inputs report non-zero emissions and the empty
  input reports zero;
* a 100 000-character uninterrupted token and a 60 000-character escaped token
  keep `peak_pending_chars ≤ 4` and `peak_atom_queue ≤ 4`;
* the counters are structural only.

### 2.5 Pre-implementation P2 run

With `docker/versioning/diagnostic_recognizer.py` temporarily absent,
`phase2_red.log` records:

```
44 failed in 1.10s
red exit status: 1
```

Every failure is the explicit
`AssertionError: docker.versioning.diagnostic_recognizer must define the P2 raw
URL recognizer`; there were **no collection errors, import errors or
environmental failures**, and no pre-existing obligation was broken. The
recognizer is greenfield, so all 44 tests are new and all failed for the same
attributable reason (absent module).

---

## GREEN

### 2.6 Bounded incremental percent transducers with raw provenance

`_Transducer` is a fixed chain `L0 -> L1 -> L2 -> L3`. Each node consumes one
layer character at a time, decodes a complete `%XX` into one character and
copies incomplete/invalid escapes verbatim — matching the legacy
`_decode_percent_layer` — emitting `(character, source_start, source_end,
from_token)`. `source_start` is inclusive and `source_end` exclusive: a decoded
`%XX` covers all three raw characters (spanning the contributing ranges when
the escape came from an earlier layer), a copied literal covers its one raw
character, and malformed/incomplete escapes preserve the literal ranges of
their characters. Every forwarded character increments
`RecognitionStats.emissions`, so the transducer-work metric is real and
bounded, with no per-layer source map. Pending state is at most three
characters. Bounded scheme classification uses `_decode_one_layer` only on a
span capped by the derived `_CLASSIFY_BOUND`.

### 2.7 Structural observers and equivalent-state dominance

`_MatchObserver` groups layer characters into scheme atoms (`_literal_atom`,
`_escape_atom`) and runs the `FIRST UNIT* COLON SLASH SLASH` automaton in
constant state: it tracks the current run's unit count and the first
FIRST-capable raw source (`_run_first`), checks the triple at each colon atom
(which reproduces the legacy greedy-then-backtracking scheme run, including
`%3A`/`%2F` units that can be either scheme units or separators), and commits
each atom once. Every `_Atom` carries the inclusive `source_start` and
exclusive `source_end` of its layer characters; selection uses the start while
the observer retains `match_start_end` and `match_colon_end` as exact range
provenance for P3 incremental work. `_PrefixObserver` runs the legacy
`_url_prefix_state_single` state over the current raw token per layer.

### 2.8 Selected-candidate raw boundary and bounded scheme classification

`_Pipeline.best_match` selects the minimum raw start over the four observers
(recording the selected layer's colon raw source); `_find_terminator` locates
the raw terminator once, forward from that start, or reports `end=None`.
`_classify_raw_scheme` classifies the selected raw scheme span with bounded
repeated decoding against the P1 whitelist. Scheme length is never a
performance-only rejection: an arbitrarily long scheme is still a candidate
and classifies `UNKNOWN`.

**GREEN run** — `phase2_green.log`:

```
64 passed, 4762 subtests passed in 2.70s
green exit status: 0
```

Two INTROSPECT findings were found and fixed before the gate:

1. **Token-boundary escape leak (resolved).** The first differential run had
   49 mismatches, all of the form legacy `NO_MATCH` versus new `NEED_MORE`: a
   pending `%` at the end of a token was flushed by the transducers into the
   prefix observers of the *next* (empty) token after a terminator. Fix:
   `_Pipeline.feed` now flushes each transducer's token-pending characters
   (`_flush_token`) immediately *before* crossing a raw terminator, so a
   trailing `%` is attributed to the token that ended. After the fix the full
   deterministic corpus agrees with the legacy detector.
2. **Three over-strict self-test expectations (resolved).** `ordinary words
   only`, `GET 200 /index.html` and `npm://host` were asserted as `NO_MATCH` /
   not-a-candidate, but a trailing alphabetic token is a legacy scheme prefix
   (`NEED_MORE`) and `npm://host` is a real UNKNOWN candidate. The tests were
   corrected to match the authoritative legacy/contract behavior rather than
   the implementation being weakened.

---

## INTROSPECT

### 2.9 Fixed-depth transition/state bound and depth distinction

Documented in the `diagnostic_recognizer` module docstring
(§Fixed-depth transition/state bound, §Detection versus interpretation depth):

* `_LAYERS` is fixed at four; each raw character causes a constant number of
  transitions, each transducer retains ≤3 characters, each match observer
  queues ≤3 atoms and each prefix observer retains ≤2 characters, so
  recognition is O(N) with bounded state independent of input length.
* Encoded branching (`%3A` as scheme unit or colon, `%2F` as scheme unit or
  slash) is handled by checking the triple at every atom with constant-cost
  run-state updates; it never enumerates decoded variants.
* Detection depth is four (three decoding passes plus one encoded separator
  unit). Detection is separate from interpretation: the module only locates a
  raw extent and classifies the scheme, so a depth-four detection match never
  authorises an additional whole-URL interpretation pass.
* When several layers expose the same raw start, the selected layer's colon
  raw source is retained so the decoded scheme span is classified
  consistently; start-index dominance is used only because every layer runs
  the same conservative language, not as a substitute for classification
  state.

### 2.10 Allocation and fallback audit

`phase2_allocation_audit.md` records:

* no `import re`, no `_URL_START_RE`, no `_find_url_start`,
  `_decode_percent_layer` or `_url_prefix_state` reference (the only textual
  matches are docstring notes that name the legacy behavior being replaced);
* the module imports only `__future__`, `collections.namedtuple`,
  `dataclasses.dataclass` and the P1 `diagnostic_recognition` contract;
* no encoded-variant table (no `VARIANT` name);
* the only string-building sites are the transducer/observer pending buffers
  (≤3) and `_classify_raw_scheme` / `_decode_one_layer` on a scheme span that
  `_recognize` has already length-guarded against the derived `_CLASSIFY_BOUND`
  (`max(len(scheme) for scheme in KNOWN_SCHEMES) * (3 ** _LAYERS)` = 729)
  **before** slicing. There is no input-sized decoded search copy, source map
  or variant table, and no legacy regex fallback. (The original audit
  mistakenly relied on the classifier's own length check and on a fixed 256
  bound; see the revalidation sections below.)

---

## VALIDATE

### 2.11 P2 validation record

**Command** (`phase2_validation.log`):

```
.venv/bin/python -m pytest \
  tests/test_diagnostic_recognition_contract.py \
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
642 passed, 11904 subtests passed in 25.59s
validation exit status: 0
```

This includes the P1 contract tests, the new P2 tests, and every pre-existing
suite that exercises detection, projection, collection, fetch or the retained
tail. Zero exit status confirms the new recognizer introduces no regression on
the affected routes.

**Applicable configured type check** (`phase2_types.log`):

```
./scripts/check-types    # ty check --output-format concise docker tests/typing
All checks passed!
types exit status: 0
```

The complete mandated eight-worker suite and the before/after performance
experiment remain P6 deliverables and are not claimed here.

### P2 deliverable check

| Task | Deliverable | Evidence | Status |
|---|---|---|---|
| 2.1 | Raw recognition tests (separators, negatives, UNKNOWN lengths, raw ends, earliest source) | `tests/test_diagnostic_recognizer_phase2.py::RawRecognitionTests`; `phase2_green.log` | done |
| 2.2 | Encoded recognition tests (depths 0–4, mixed, hex case, escapes, malformed/incomplete) | `…::EncodedRecognitionTests`; `phase2_green.log` | done |
| 2.3 | Bounded deterministic differential corpus vs legacy detector | `…::DifferentialCorpusTests` (44 curated + 4400 PRNG); `phase2_green.log` | done |
| 2.4 | Deterministic work/state regression checks (no timing) | `…::WorkStateRegressionTests`; `phase2_green.log` | done |
| 2.5 | Pre-implementation P2 run | `phase2_red.log` (44 failed, exit 1) | done |
| 2.6 | Bounded incremental percent transducers with provenance | `diagnostic_recognizer.py::_Transducer`, `_decode_one_layer`; `phase2_green.log` | done |
| 2.7 | Structural observers and equivalent-state dominance | `diagnostic_recognizer.py::_MatchObserver`, `_PrefixObserver`, `_Pipeline`; `phase2_green.log` | done |
| 2.8 | Raw boundary detection and bounded scheme classification | `diagnostic_recognizer.py::_Pipeline.best_match`, `_find_terminator`, `_classify_raw_scheme`; `phase2_green.log` | done |
| 2.9 | Fixed-depth bound / depth distinction documentation | module docstring §Bounds/§Detection; §2.9 | done |
| 2.10 | Allocation / fallback audit | `phase2_allocation_audit.md`; `phase2_classification_{red,green}.log` | done, 1 finding fixed |
| 2.11 | P1/P2 + affected pre-existing validation | `phase2_validation.log` (exit 0); `phase2_types.log` (exit 0) | done |

> **Revalidation note.** The paragraphs above describe the original P2 close.
> A follow-up review found the unconditional `text[start:colon]` slice in
> `_recognize`; see **P2.10 fix revalidation** below for the corrected evidence.

---

## P2.10 fix revalidation (bounded classification allocation)

**Defect.** `_recognize` computed `kind = _classify_raw_scheme(text[start:colon])`,
which copied an arbitrarily long scheme into an input-sized string *before*
`_classify_raw_scheme` length-checked it. Detection was still correct, but the
allocation contract was violated and the 2.10 audit claim was wrong.

**Fix** (`docker/versioning/diagnostic_recognizer.py::_recognize`): the span
length is checked *before* slicing; only spans `≤ _CLASSIFY_BOUND` are copied.
Long schemes remain detectable `MATCH` candidates and are classified `UNKNOWN`.
The `_classify_raw_scheme` docstring now states the caller pre-guards the span.

**Deterministic allocation regression**
(`tests/test_diagnostic_recognizer_phase2.py::BoundedClassificationAllocationTests`,
no timing assertions):

* a `_CLASSIFY_BOUND * 20` scheme at a nonzero start offset returns `MATCH`
  with exact raw boundaries and `UNKNOWN`, and a `unittest.mock` spy proves
  `_classify_raw_scheme` is **not** called;
* a `_CLASSIFY_BOUND + 1` scheme likewise returns `MATCH`/`UNKNOWN` with the
  classifier **not** called;
* a scheme of exactly `_CLASSIFY_BOUND` calls the classifier exactly once with
  a `_CLASSIFY_BOUND`-character span.

### Second defect: the fixed bound did not cover supported spellings

**Defect.** The bound was a fixed `256`, but `KNOWN` is decided after up to
`_LAYERS` (4) percent-decoding passes and each pass can expand a character by
3x, so a supported spelling of a whitelist scheme can use up to `3 ** _LAYERS`
(81) raw characters per decoded character. The longest whitelist scheme is
`git+https` (9 characters), so a fully four-times-encoded known scheme is up to
`9 * 81 = 729` raw characters. In particular `https` encoded four times is a
405-character scheme that the 256 bound misclassified `UNKNOWN`.

**Fix.** `_CLASSIFY_BOUND` is now derived from the contract whitelist:

```python
_CLASSIFY_BOUND = max(len(scheme) for scheme in KNOWN_SCHEMES) * (3 ** _LAYERS)
```

i.e. `9 * 81 = 729`. The pre-slice guard is unchanged, so a longer
(unsupported) span is still never copied and is classified `UNKNOWN` as
`MATCH`. Detection depth, fetch grammar, projector integration and
interpretation policy are unchanged.

**Deterministic classification regression**
(`tests/test_diagnostic_recognizer_phase2.py::WhitelistClassificationTests`,
no timing assertions):

* every whitelist scheme at depths zero through four with uppercase and
  lowercase hex, verifying `MATCH`, `KNOWN` and exact raw boundaries;
* uppercase scheme spellings and mixed per-character depths;
* separately encoded escape characters (e.g. `%25%36%38` for `h` at depth 2);
* the 405-character four-times-encoded `https`, plus `_CLASSIFY_BOUND >= 405`.

**Evidence (reopened → fixed → verified):**

| Run | Command | Result | Log |
|---|---|---|---|
| Allocation RED (pre-fix) | `pytest …::BoundedClassificationAllocationTests -n 8 --dist loadfile -q` | 2 failed, exit 1 | `phase2_classification_red.log` |
| Allocation GREEN (post-fix) | same | 3 passed, exit 0 | `phase2_classification_green.log` |
| Whitelist RED (pre-fix bound 256) | `pytest …::WhitelistClassificationTests -n 8 --dist loadfile -q` | 37 failed, exit 1 | `phase2_whitelist_red.log` |
| Full P2 | `pytest tests/test_diagnostic_recognizer_phase2.py -n 8 --dist loadfile -q` | 53 passed, 4754 subtests, exit 0 | `phase2_green.log` |
| Types | `./scripts/check-types` | All checks passed, exit 0 | `phase2_types.log` |
| Validation | P1/P2 + affected suites, `-n 8 --dist loadfile -q` | 631 passed, 11896 subtests, exit 0 | `phase2_validation.log` |

Fetch grammar, projector integration and URL interpretation depth were not
touched.

### Third and fourth defects: vacuous emissions counter and missing range provenance

**Defect 3.** `RecognitionStats.emissions` was never incremented, so the
transducer-work metric was always zero and its linear-growth check compared two
zeros. `_Transducer._emit` now increments `stats.emissions` for every forwarded
character (decoded `%XX`, copied literal and flushed pending characters).

**Defect 4.** Emitted characters carried only a raw source start, but the P2
design requires an inclusive raw start and exclusive raw end. `_Transducer` now
threads `(source_start, source_end)` through `_Pipeline` into `_MatchObserver`;
a decoded `%XX` covers all three raw characters (spanning contributing ranges
for nested escapes), a copied literal covers one raw character, and
malformed/incomplete escapes preserve literal ranges. Selection uses the range
start; the exclusive end is retained as `match_start_end` / `match_colon_end`.
Returned candidate coordinates and the legacy differential corpus are
unchanged.

**Deterministic regressions (no timing):**

* `WorkStateRegressionTests` asserts non-zero `emissions` for representative
  non-empty inputs, zero for the empty input, and linear growth for long runs.
* `SourceRangeProvenanceTests` asserts literal one-character ranges, decoded
  three-character `%XX` ranges, literal ranges for malformed (`%zz`, `%4G`) and
  trailing incomplete (`%4`) escapes, nested spans covering all contributing
  raw characters, observer-retained range ends, and unchanged candidate
  coordinates.

**Evidence (reopened → fixed → verified):**

| Run | Command | Result | Log |
|---|---|---|---|
| Emissions RED (pre-fix) | `pytest …::WorkStateRegressionTests -n 8 --dist loadfile -q` | 7 failed, exit 1 | `phase2_emissions_red.log` |
| Range RED (pre-fix) | `pytest …::SourceRangeProvenanceTests -n 8 --dist loadfile -q` | 3 failed, exit 1 | `phase2_ranges_red.log` |
| Full P2 | `pytest tests/test_diagnostic_recognizer_phase2.py -n 8 --dist loadfile -q` | 64 passed, 4762 subtests, exit 0 | `phase2_green.log` |
| Types | `./scripts/check-types` | All checks passed, exit 0 | `phase2_types.log` |
| Validation | P1/P2 + affected suites, `-n 8 --dist loadfile -q` | 642 passed, 11904 subtests, exit 0 | `phase2_validation.log` |

Fetch grammar, projector integration and URL interpretation depth were not
touched.

Phase 2 gate: **satisfied (revalidated after the pre-slice, derived-bound,
emissions and range-provenance fixes)**. Classification copies are bounded,
every supported spelling classifies `KNOWN`, the work counter is real and
bounded, emissions carry exact inclusive/exclusive raw ranges, the
deterministic regressions cover each defect, and full P2, type-check and
affected P1/pre-existing suites all exit zero. Projector continuation, metadata
gating and consumer channels remain P3–P5 and were not started here.

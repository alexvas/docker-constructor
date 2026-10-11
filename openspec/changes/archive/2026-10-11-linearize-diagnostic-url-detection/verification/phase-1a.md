# Phase 1A Verification Evidence — Standalone Aho–Corasick Streaming Core

Change: `linearize-diagnostic-url-detection`
Schema: `spec-driven`
Phase: **P1A — Standalone Aho–Corasick streaming core** (tasks 1A.1–1A.10)
Depends on: successful revised P1 gate (`verification/phase-1.md`, satisfied)
Repository: `/home/aavasiljev/work/home/docker-constructor.git`

Phase 1A introduces the generic, dependency-free multi-literal matcher that is
the prerequisite for P2 literal secret matching.  It implements **only** the
matching core: percent decoding, URL semantics, secret selection and projector
coordination remain P2/P3 work.  No existing executable path is rewired in
this phase.

## Deliverables and evidence map

| Deliverable | Location |
|---|---|
| Internal dependency-free trie with failure/output links | `docker/versioning/aho_corasick.py` (`AhoCorasick`) |
| Immutable registered patterns, independent stream cursors | `AhoCorasick`, `AhoCorasickCursor`, `PatternMatch`, `SearchCounters` |
| Genuine compiled-state immutability (read-only maps + frozen slots) | `AhoCorasick`; `tests/test_aho_corasick_core.py` (`CompiledStateImmutabilityTests`) |
| Immutability RED/GREEN/audit/types/validation/full-suite logs | `phase1a_immutability_*.log`; §Post-gate fix |
| Deterministic cumulative counters with reset semantics | `SearchCounters`; `AhoCorasickCursor.reset` |
| P1A unit/continuation/differential tests | `tests/test_aho_corasick_core.py` |
| P1A RED run (full log + runner exit status) | `verification/phase1a_red.log` (exit 1) |
| P1A GREEN run | `verification/phase1a_green.log` (exit 0) |
| P1A API/ownership/complexity documentation | module/type docstrings in `aho_corasick.py` (§1A.8) |
| One-off construction/search ownership audit | `verification/phase1a_core_audit.py` + `phase1a_core_audit.log` (exit 0) |
| P1 + P1A + affected-suite validation run | `verification/phase1a_validation.log` (exit 0) |
| Applicable type check | `verification/phase1a_types.log` (exit 0) |
| Scope/ownership review record (task 1A.9) | §1A.9 below |

---

## RED

### 1A.1 Core contract tests

`tests/test_aho_corasick_core.py` defines the P1A contract as executable
obligations:

- **empty pattern sets:** `AhoCorasick(())` registers nothing, has one node and
  matches nothing while still counting input transitions;
- **registered non-empty patterns / duplicates / order:** empty and duplicate
  patterns are ignored, first-occurrence order is preserved, and node storage
  does not grow with duplicates;
- **prefix and suffix patterns:** `ab`/`abc` both report at their own ends and
  `bcd`/`cd`/`d` all report at overlapping ends;
- **overlapping and simultaneous matches:** `aba`/`ba`/`a` over `ababa` reports
  every occurrence exactly once;
- **Unicode Python-character coordinates:** `€TOKEN` reports `(1, 6)` and the
  UTF-8 byte offset is explicitly shown to be different;
- **independent streams sharing one compiled trie:** two cursors over one
  automaton stay isolated and the registration is unchanged;
- **immutable registration:** mutating the caller's pattern list does not
  change the automaton, `PatternMatch` is frozen, and the module is not
  re-exported by `docker`/`docker.versioning` (no public SDK surface); and
- **documented usage example:** the module-docstring `AhoCorasick(["he", "she",
  "hers"]).find_all("ushers")` example is executed verbatim.

### 1A.2 Continuation and counter tests

- **one-feed / every-split / single-character equivalence** over six cases,
  including an empty pattern set and a text with no matches;
- **`feed` versus `advance` equivalence** of matches, node state and counters;
- **deterministic counters:** `transitions` counts every input symbol,
  `outputs` counts every emitted match, and `failures` are amortised within the
  input length;
- **partition-independent counters and state:** every split yields identical
  counters and final node;
- **reset semantics:** `reset` returns to the root, preserves the absolute
  offset and cumulative lifetime totals, and never replays input;
- **state dependence only on recent characters:** `"x"*2000 + "ab"` and `"ab"`
  leave the same node state; and
- **payload-free cursor:** no cursor slot other than the shared automaton holds
  a `str`.

### 1A.3 Differential corpus

- an 80-case bounded, seeded differential corpus against a naive literal
  oracle, plus the same corpus replayed at every chunk split;
- **adversarial suffix-heavy patterns** (`a`..`aaaaaaaa`) over `a*24` with an
  explicit expected match count;
- **every match exactly once** and **shared-compilation stability** across
  repeated runs;
- **output-sensitive work:** a no-match run reports `outputs == 0`, a
  match-heavy run over the same 400 input symbols reports `outputs == 799`
  matches, independently of its `transitions == 400`; and
- **live state independent of stream length:** a 2-character and a 2000-character
  stream whose suffixes agree finish on the same node, below `node_count`.

No test imports `time` or asserts elapsed time; the core is greenfield, so
before it existed every obligation failed through the explicit
missing-module assertion rather than a collection error.

### 1A.4 P1A RED record

`phase1a_red.log` records the run against the pre-implementation state (the
module temporarily removed and restored) with the final test file:

```
.venv/bin/python -m pytest tests/test_aho_corasick_core.py \
  -n 8 --dist loadfile --tb=short -q
116 failed, 2 passed in 2.09s
RED exit status: 1
```

Every failure is the same attributable missing-core failure — for example:

```
tests/test_aho_corasick_core.py:74: in core
    self.fail(
E   AssertionError: docker.versioning.aho_corasick must define the P1A Aho-Corasick core
```

There were **no collection errors, import errors or environmental failures**,
and no pre-existing obligation was broken.  The `2 passed` are the two
Unittest `subTest` parents (`test_one_feed_every_split_and_single_character_agree`
and `test_bounded_differential_corpus_matches_literal_oracle`) whose individual
per-case failures are counted as `SUBFAILED` subtest items (86 in this run),
not as real passes; those parent nodes have no assertions outside their
`subTest` blocks, so there is no honestly-passing node-level obligation before
the implementation exists.

---

## GREEN

### 1A.5 Trie builder with failure/output links

`docker/versioning/aho_corasick.py` implements `AhoCorasick`:

- `_dedupe_patterns` copies and de-duplicates the configured patterns, drops
  empty patterns and rejects non-strings and a bare string argument;
- `__init__` builds the trie, then completes failure links with one
  breadth-first pass, and sets one `output_link` per node to the nearest proper
  suffix node that has its own output;
- the compiled structure is stored in private tuples (`_goto`, `_fail`,
  `_output`, `_output_link`) whose transition maps are read-only
  `types.MappingProxyType` views of dictionaries kept local to construction;
  `AhoCorasick` also refuses to reassign or delete any compiled attribute, so
  it is an immutable shared registration in fact, not by convention; and
- `node_count <= 1 + sum(len(pattern))`, and duplicate patterns add no nodes.

No new runtime dependency is introduced.

### 1A.6 Per-symbol streaming cursor

`AhoCorasickCursor` holds only `_automaton`, `_state`, `_offset`,
`_transitions`, `_failures` and `_outputs`.  `advance` consumes one symbol and
`feed` loops over a chunk.  On each symbol the cursor follows failure links to
a resolvable node, takes one direct transition, then emits each match by
walking the current node's own output and its output-link chain.  It reports
`pattern_index` plus inclusive `start`/exclusive `end` in Python-character
coordinates (`start == end - len(pattern)`), including overlapping and
simultaneous matches.  It never stores the consumed source, a chunk replay
buffer or an input-sized history.  The compiled transitions it reads are
read-only `MappingProxyType` views and the automaton rejects any compiled
attribute reassignment or deletion, so no cursor or caller can rewrite the
shared trie.

### 1A.7 Cumulative counters and reset semantics

`SearchCounters(transitions, failures, outputs)` is cumulative for the
cursor's lifetime: `transitions` counts consumed input symbols, `failures`
counts failure-link traversals and `outputs` counts emitted matches (own-output
plus output-link work).  `reset()` returns the cursor to the root while
preserving both `_offset` and all counters, so dropping obsolete recognition
state at finalization/overflow cannot replay input or hide work.  Letting a
cursor be replaced cannot reset the stream total because the counters belong to
the cursor instance and the evidence audit demonstrates that a fresh cursor
reports strictly less work than the reset-heavy one.

**GREEN run** — `phase1a_green.log`:

```
.venv/bin/python -m pytest tests/test_aho_corasick_core.py \
  -n 8 --dist loadfile --tb=short -q
32 passed, 96 subtests passed in 0.58s
green exit status: 0
```

---

## INTROSPECT

### 1A.8 API, ownership and complexity documentation

The `aho_corasick` module and type docstrings document:

- **registration versus live-state ownership** — `AhoCorasick` owns the
  immutable compiled trie; a cursor owns only integer state and references the
  shared automaton, so cursors are isolated and no cursor mutates registration;
- **pattern identity and output ordering** — de-duplicated first-occurrence
  order, stable `pattern_index`, longest-first at one position and increasing
  end order across positions, every occurrence once;
- **Unicode/coordinate semantics** — absolute Python `str` character offsets,
  inclusive `start`, exclusive `end`, never UTF-8 bytes or decoded-layer
  indices, with the optional initial cursor `offset` for a larger stream;
- **cursor continuation/reset** — any chunking is equivalent, offsets advance
  monotonically, and `reset` drops in-flight state while preserving offset and
  lifetime counters without replay;
- **`O(N + matches)` search bounds** — one transition per symbol, amortised
  failure traversals and output-sensitive output-link work, with live state
  `O(1)` in stream length; and
- **scope** — the docstring explicitly states the core does **not** perform
  percent decoding, URL recognition, secret selection, redaction or projector
  coordination; those remain P2/P3 work.

The docstring usage example is executed verbatim by
`test_documented_usage_example_matches_the_api`, so documentation and
deterministic tests agree with the API without claiming URL/secret
coordination.

### 1A.9 Construction/search ownership audit

`phase1a_core_audit.py` is a one-off, timing-free audit run outside
tests/CI (`phase1a_core_audit.log`, exit 0).  Resolved findings:

| Audit area | Finding | Evidence |
|---|---|---|
| Isolation (necessary, not sufficient) | Caller pattern input is copied, not aliased; search does not write registration.  This is explicitly labelled isolation and is **not** accepted as proof of compiled-state immutability | `[ok] caller pattern input is copied, not aliased (isolation, not immutability)` |
| Genuine compiled-state immutability | Every transition map is a read-only `mappingproxy`; adding, deleting or replacing a transition raises `TypeError`; reassigning or deleting any compiled attribute raises `AttributeError`; there is no instance dict; matching is unchanged after every rejected attempt | `[ok] every compiled transition map is a read-only mappingproxy`; `[ok] adding, deleting and replacing transitions each raise TypeError`; `[ok] replacing or deleting any compiled attribute raises AttributeError`; `[ok] compiled automaton has no instance dict for attribute injection`; `[ok] matching is unchanged after every rejected mutation attempt` |
| Pattern-dependent storage | 66 nodes for 951 pattern characters, bounded by `1 + sum(len(pattern))`; one `_goto`/`_fail`/`_output`/`_output_link` slot per node | `[ok] node_count ... <= ...`; `[ok] registration storage is O(nodes)` |
| Output-link handling | Exactly one own-output node per registered pattern and no per-node transitive output list, so suffix-heavy registration storage does not multiply | `[ok] exactly 50 own-output nodes`; `[ok] no node copies a transitive output list` |
| Payload-free cursors | Cursor slots are `('_automaton', '_state', '_offset', '_transitions', '_failures', '_outputs')`; no string payload; state is one integer | `[ok] cursor retains no string payload` |
| Cumulative work | Counters survive five reset cycles (10 transitions, 5 outputs) and exceed a fresh cursor's work | `[ok] lifetime transitions survive resets`; `[ok] replacing a cursor cannot hide its cumulative work` |
| Failure/output bounds | 4000 transitions, 3999 failures amortised within them, outputs == emitted matches | `[ok] failures 3999 amortized within transitions 4000` |
| No library/regex fallback | Module imports only `__future__`, `collections`, `dataclasses`, `types`; `re` is absent | `[ok] module imports only stdlib allowlist`; `[ok] no production regular-expression fallback` |

No unresolved blocking findings.  No production `re` fallback, external
Aho-Corasick library, decoded search copy, input-sized search map or
variant-enumeration table exists.

---

## VALIDATE

### 1A.10 P1/P1A validation gate

**P1 contract + P1A core + affected existing suites** — `phase1a_validation.log`:

```
.venv/bin/python -m pytest \
  tests/test_diagnostic_recognition_contract.py \
  tests/test_aho_corasick_core.py \
  tests/test_diagnostic_recognizer_phase2.py \
  tests/test_diagnostic_recognizer_phase3.py \
  tests/test_host_diagnostic_projection.py \
  tests/test_host_path_projection.py \
  tests/test_host_diagnostic_grouping_phase7.py \
  tests/test_host_diagnostic_identity_phase7.py \
  tests/test_npm_diagnostic_collection_phase8.py \
  tests/test_npm_fetch_phase4.py \
  tests/test_npm_environment_streaming.py \
  tests/test_retained_tail_phase6.py \
  -n 8 --dist loadfile --tb=short -q
750 passed, 12683 subtests passed in 12.67s
validation exit status: 0
```

The set includes the revised P1 contract tests, the new P1A core tests and
every suite that exercises the diagnostic recognizer, projector, npm collector,
npm fetch parser and retained tail.  Zero exit status confirms the new core
introduces no regression and does not rewire any existing path.

**Applicable configured type check** — `phase1a_types.log`:

```
./scripts/check-types
All checks passed!
types exit status: 0
```

Historical recognition evidence is not reused as this gate.  The full mandated
eight-worker loadfile suite, lint/type discovery and final performance
before/after evidence remain P6 deliverables and are not claimed here.

## Post-gate fix — genuine compiled-state immutability (tasks 1A.6/1A.9)

The initial P1A implementation stored `_goto` as a tuple of *mutable*
dictionaries and left the compiled slots assignable.  Caller-input isolation
and search-time non-mutation were demonstrated, but those are isolation
properties: they cannot show that the compiled trie itself rejects mutation.
This follow-up makes the guarantee real:

- construction keeps the mutable transition dictionaries local and publishes
  only their `types.MappingProxyType` read-only views;
- `AhoCorasick.__setattr__` permits each compiled attribute to be assigned once
  (during `__init__`) and raises `AttributeError` on any later reassignment;
- `AhoCorasick.__delattr__` raises `AttributeError` for every compiled
  attribute, and `__slots__` leaves no instance dict for attribute injection;
- the new `CompiledStateImmutabilityTests` drive transition and attribute
  mutation and require the exception before proving shared-cursor matching is
  unchanged.

Fresh evidence (the earlier `phase1a_green.log`, `phase1a_validation.log` and
`phase1a_types.log` are pre-fix artifacts; these supersede them):

| Evidence | Command | Result |
|---|---|---|
| Immutability RED (pre-fix module) | `pytest "...::CompiledStateImmutabilityTests" -n 8 --dist loadfile -q` | `phase1a_immutability_red.log`, exit 1 — `12 failed, 3 passed` |
| Immutability GREEN | `pytest tests/test_aho_corasick_core.py -n 8 --dist loadfile -q` | `phase1a_immutability_green.log`, exit 0 — `37 passed, 106 subtests passed` |
| Updated audit | `python .../phase1a_core_audit.py` | `phase1a_immutability_audit.log` and refreshed `phase1a_core_audit.log`, exit 0 |
| Type check | `./scripts/check-types` | `phase1a_immutability_types.log`, exit 0 — `All checks passed!` |
| Affected-suite validation | 12-suite command (§1A.10) | `phase1a_immutability_validation.log`, exit 0 — `755 passed, 12693 subtests passed` |
| Full Python suite | `pytest tests -n 8 --dist loadfile --tb=short -q` | `phase1a_immutability_full_suite.log`, exit 0 — `6322 passed, 13 skipped, 11 warnings, 16174 subtests passed` |

RED honesty: `phase1a_immutability_red.log` reports `12 failed, 3 passed`.  The
`3 passed` are the `test_compiled_automaton_has_no_attribute_injection` node
(genuine: `__slots__` already blocked *new* attributes) plus the two `subTest`
parents `test_replacing_compiled_attributes_raises` and
`test_deleting_compiled_attributes_raises`, whose per-attribute failures are
counted as `SUBFAILED` items rather than node failures.  The substantive
obligations — read-only transition maps, frozen compiled attributes and
unchanged matching after rejected mutation — all failed before the fix.

---

## P1A deliverable check

| Task | Deliverable | Evidence | Status |
|---|---|---|---|
| 1A.1 | Core contract tests: empty/registered/duplicate/prefix/suffix/overlap/simultaneous, Unicode coordinates, independent shared-trie cursors, no public SDK API, genuine compiled-state immutability | `tests/test_aho_corasick_core.py` (`RegistrationAndMatchTests`, `CompiledStateImmutabilityTests`) | done |
| 1A.2 | Continuation equivalence, deterministic counters, reset/ownership | `tests/test_aho_corasick_core.py` (`ContinuationAndCountersTests`) | done |
| 1A.3 | Bounded differential corpus, adversarial suffix patterns, stability, output-sensitive work, stream-length independence, no timing | `tests/test_aho_corasick_core.py` (`DifferentialCorpusTests`) | done |
| 1A.4 | P1A RED with full log and actual status; failures mapped to missing core; subtest parents recorded honestly | `phase1a_red.log` (exit 1); §1A.4 | done |
| 1A.5 | Trie builder + BFS failure/output links, no new dependency, bounded registration | `aho_corasick.py`; §1A.5; audit | done |
| 1A.6 | Per-symbol cursor with pattern identities, exclusive ends, overlapping outputs, no retained payload, compiled transitions read-only and attributes frozen | `aho_corasick.py`; `phase1a_immutability_green.log` | done |
| 1A.7 | Cumulative transitions/failures/outputs with reset preserving lifetime totals | `aho_corasick.py`; audit | done |
| 1A.8 | Registration/live ownership, Unicode, continuation/reset, ordering, `O(N + matches)` docs; example matches API; no URL/secret claim | module/type docstrings; §1A.8 | done |
| 1A.9 | Construction/search allocation, output-link/failure and genuine immutability audit; immutable shared registration, payload-free cursors, pattern-dependent storage, cumulative work, no regex/library fallback | `phase1a_core_audit.py` + `.log`; `phase1a_immutability_audit.log`; §1A.9; §Post-gate fix | done, no blocking findings |
| 1A.10 | P1/P1A + applicable checks with required eight-worker verification and preserved logs/statuses, including the full Python suite | `phase1a_immutability_validation.log` (exit 0); `phase1a_immutability_full_suite.log` (exit 0); `phase1a_immutability_types.log` (exit 0) | done |

Phase 1A gate: **satisfied**.  P2 integration may proceed from this core.

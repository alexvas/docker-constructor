# Phase 1 Verification Evidence — Recognition Result Contract

Change: `linearize-diagnostic-url-detection`
Schema: `spec-driven`
Phase: **P1 — Recognition result contract** (tasks 1.1–1.9)
Depends on: none
Repository: `/home/aavasiljev/work/home/docker-constructor.git`
Pre-implementation revision: `50f9ebc Plan linear incremental diagnostic URL detection`

> **Location note.** This evidence set was relocated from
> `docs/verification/linearize-diagnostic-url-detection/` into this change's
> `verification/` directory at the project owner's request so it is archived
> together with the change. Internal references and the baseline script's
> repo-root resolution were updated, and the baseline log was regenerated from
> the new location.

Phase 1 introduces the internal recognition result contract only. It does not
rewire the projector, the incremental recognizer, the metadata gate, or any
collector/transport/lock path — those are P2–P5 per the phase DAG.

## Deliverables and evidence map

| Deliverable | Location |
|---|---|
| Affected-path / caller inventory | `openspec/changes/linearize-diagnostic-url-detection/verification/phase1_caller_inventory.md` |
| Baseline experiment (one-off, outside tests/CI) | `openspec/changes/linearize-diagnostic-url-detection/verification/phase1_baseline.py` |
| Baseline full stdout log + environment/commands/medians/status | `openspec/changes/linearize-diagnostic-url-detection/verification/phase1_baseline.log` |
| Oversized-tail fixture baseline (one-off, outside tests/CI) | `openspec/changes/linearize-diagnostic-url-detection/verification/phase1_oversized_fixture_baseline.py` |
| Oversized-tail fixture medians/log/status | `openspec/changes/linearize-diagnostic-url-detection/verification/phase1_oversized_fixture_baseline.log` (exit 0) |
| Actual oversized-tail tests run with `--durations=0` | `openspec/changes/linearize-diagnostic-url-detection/verification/phase1_oversized_tests.log` (12 passed, exit 0) |
| Original retained-tail bottleneck scenario medians/log/status | `openspec/changes/linearize-diagnostic-url-detection/verification/phase1_oversized_fixture_baseline.log` incl. §1.2.3 (exit 0) |
| Original retained-tail scenario tests run with `--durations=0` | `openspec/changes/linearize-diagnostic-url-detection/verification/phase1_retained_tail_tests.log` (2 passed, 6 subtests, exit 0) |
| Whole `tests/test_retained_tail_phase6.py` file run on one `loadfile` worker | `openspec/changes/linearize-diagnostic-url-detection/verification/phase1_retained_tail_file.log` (46 passed, 97 subtests, exit 0) |
| P1 contract tests | `tests/test_diagnostic_recognition_contract.py` |
| Pre-implementation RED run (full log + runner exit status) | `openspec/changes/linearize-diagnostic-url-detection/verification/phase1_red.log` (exit 1) |
| Immutable candidate + closed outcome model | `docker/versioning/diagnostic_recognition.py` (`DiagnosticCandidate`, `RecognitionResult`, `CandidateKind`, `RecognitionKind`) |
| Fixed case-insensitive scheme whitelist | `docker/versioning/diagnostic_recognition.py` (`KNOWN_SCHEMES`, `classify_scheme`) |
| Internal-API documentation | module/class/function docstrings in `docker/versioning/diagnostic_recognition.py` (§1.7 below) |
| P1 GREEN run | `openspec/changes/linearize-diagnostic-url-detection/verification/phase1_green.log` (exit 0) |
| P1 + affected pre-existing validation run | `openspec/changes/linearize-diagnostic-url-detection/verification/phase1_validation.log` (exit 0) |
| Applicable type check | `openspec/changes/linearize-diagnostic-url-detection/verification/phase1_types.log` (exit 0) |
| Scope/contract review record | §Review record (task 1.8) below |

---

## RED

### 1.1 Caller inventory

`phase1_caller_inventory.md` covers detection (`_find_url_start`), unresolved
holding (`_ambiguous_hold` / integer `_URL_PREFIX_*` state), EOF
(`DiagnosticProjector.finish`), structured projection
(`sanitize_diagnostic_text`, `sanitize_host_paths`,
`project_structured_diagnostic`, `project_host_acquisition_failure`),
metadata (`_sanitize_candidate`), collector
(`npm_diagnostic_stream.py`, `host_presentation.py`, `host_progress.py`) and
fetch (`npm_fetch.py`) routes.

Each old **integer-contract dependency** (D1–D3, H1–H4, E1–E3, S1–S4) and each
**possible `UNKNOWN` fact bypass** (M1–M3, C1–C4, F1–F3) has a single
identified owning phase in the owner summary.

**Fetch correction (P1 rework).** The fetch route is *not* whitelist-gated:
`npm_fetch.py` validates the URL against the RFC 3986 scheme grammar
`_SCHEME_RE = [A-Za-z][A-Za-z0-9+.\-]*` plus `normalized_url_host`, so it
accepts any structurally valid scheme — `http`/`https` *and* non-whitelist
schemes such as `custom+transport://…`. Diagnostic whitelist membership
(`KNOWN_SCHEMES`/`classify_scheme`) is a separate policy that the fetch route
does not apply today. F1–F3 therefore record an UNKNOWN scheme as reachable,
and `phase1_caller_inventory.md` §7.1 assigns explicit P4/P5 obligations to
verify that an UNKNOWN successful-fetch URL contributes no host/path or
fingerprint fact and does not influence `host-path` grouping, **without**
restricting the fetch grammar or the shared hostname/transport helpers.

### 1.2 Baseline experiment

Two one-off experiments (neither a test nor wired into CI) capture the
demonstrated quadratic `_find_url_start` growth and the *actual existing*
oversized-tail scenarios. No fixture was modified in either.

#### 1.2.1 Synthetic detector probe (`phase1_baseline.py`)

`phase1_baseline.py` records the environment (CPython 3.14.7, Linux x86_64,
32 CPUs, `PENDING_LIMIT_BYTES = 8192`), the command, full stdout, and exit
status (0). Medians over 5 repeats:

`_find_url_start` — **uninterrupted** (single scheme-character run, no `://`):

| Size | Median (s) |
|---|---|
| 1 KiB | 0.013527 |
| 2 KiB | 0.053285 |
| 4 KiB | 0.212704 |
| 8 KiB | 0.861089 |
| 16 KiB | 3.470452 |

`_find_url_start` — **separated** (8-char tokens, single-space separated):

| Size | Median (s) |
|---|---|
| 1 KiB | 0.000139 |
| 2 KiB | 0.000279 |
| 4 KiB | 0.000563 |
| 8 KiB | 0.001103 |
| 16 KiB | 0.002255 |

The uninterrupted series grows ~4× per size doubling (quadratic); the
separated series is ~2× per doubling (linear). This reproduces the
demonstrated hotspot.

**Synthetic supplementary oversized-tail probe** (`"a"*N + " end"` through
`DiagnosticProjector`), medians over 5 repeats: 8 KiB → 1.08049 s,
16 KiB → 1.07614 s, both producing exactly `[sanitized oversized token] end`
(length 31). This `"a"*N + " end"` input is **not** an existing test fixture;
it is retained only as supplementary evidence and does **not** stand in for
the real fixture timings in §1.2.2.

Per-mode capture profiles (`project_tail` × 200 over a diagnostic containing a
known literal URL, a secret, and an unknown-scheme URL) are recorded in the
full log for `redacted`, `host-path`, and `exact`, including top cumulative
`cProfile` frames. Rendered outputs were respectively `GET <redacted> ...`,
`GET registry.example.com/pkg/-/pkg-1.0.0.tgz ... token=<redacted>
internal.example/private`, and the terminal-safe source line.

**Command** (from the log):
`.venv/bin/python openspec/changes/linearize-diagnostic-url-detection/verification/phase1_baseline.py 5`
→ exit status 0.

#### 1.2.2 Actual existing oversized-tail fixture timings

`phase1_oversized_fixture_baseline.py` reproduces the exact input
constructions of twelve existing oversized-tail tests — without modifying those
test files — and times each over 5 repeats (`phase1_oversized_fixture_baseline.log`,
exit 0). `phase1_oversized_tests.log` records the same twelve node ids run
through pytest with `--durations=0`: `12 passed`, exit status 0. Environment
matches §1.2.1 (`PENDING_LIMIT_BYTES = 8192`,
`DIAGNOSTIC_LINE_LIMIT_BYTES = 65536`). The same script and log also cover the
two original retained-tail bottleneck scenarios in §1.2.3.

| Existing test (identifier below) | What it exercises | Median (s) |
|---|---|---|
| `TestBoundarySafeUrlSanitization::test_candidate_cannot_be_retained_past_the_byte_limit` | projector: candidate past 8 KiB byte limit | 0.002996 |
| `…::test_multibyte_unterminated_candidate_stays_within_the_limit` | projector: multibyte unterminated, 512-char feeds | 0.004589 |
| `…::test_multibyte_append_never_exceeds_the_limit_at_drain_entry` | projector: multibyte whole feed, drain-entry peak | 0.004048 |
| `…::test_pending_state_remains_bounded_for_an_unterminated_token` | projector: 7-byte feeds of a 4×-limit token | 0.503475 |
| `…::test_oversized_token_discards_until_boundary_then_recovers` | projector: oversized token then recovery URL | 0.004249 |
| `…::test_oversized_token_without_boundary_finalizes_without_incomplete_marker` | projector: oversized token finalized at EOF | 0.003194 |
| `TestHostPathBoundaries::test_oversized_url_token_yields_no_host_path_fact` | host-path: oversized URL token, no fact | 0.001937 |
| `TestCollectorBranch::test_oversized_unterminated_url_stays_within_pending_limit` | collector: 16 KiB URL, 256-byte feeds | 0.021737 |
| `…::test_oversized_newline_free_diagnostic_stays_within_line_limit` | collector: newline-free line past 64 KiB | 0.042866 |
| `…::test_recovery_after_safe_boundary` | collector: recovery after safe boundary | 0.055630 |
| `…::test_input_keeps_draining_after_overflow` | collector: input keeps draining after overflow | 0.044929 |
| `TestConsumablePerLineFacts::test_oversized_url_heavy_line_discards_metadata_and_recovers` | collector: oversized URL-heavy line, metadata + recovery | 0.039659 |

The projector rows are in `tests/test_host_diagnostic_projection.py`, the
host-path row in `tests/test_host_path_projection.py`, and the collector rows
in `tests/test_npm_diagnostic_collection_phase8.py`. Recorded observables
confirm each fixture still hits its real branch: peak pending sizes of
8132/8190 bytes and a drain-entry peak of exactly 8192, the line-limit peak of
exactly 65536 bytes, the oversized markers present, no discarded content in
live text or tail, and recovery hostnames (`good.example`, `b.example`) only
where the fixture expects them. The 7-byte-feed fixture dominates the actual
oversized-tail cost (~0.50 s) and is the only projector fixture near a second.

**Command** (from the log):
`.venv/bin/python openspec/changes/linearize-diagnostic-url-detection/verification/phase1_oversized_fixture_baseline.py 5`
→ exit status 0. The pytest node-id run and its complete `--durations=0`
output are in `phase1_oversized_tests.log` (exit status 0).

#### 1.2.3 Original retained-tail bottleneck scenarios

The two scenarios cited in `proposal.md` as consuming approximately 16 seconds
and making `tests/test_retained_tail_phase6.py` cost approximately 24.7 seconds
on one `loadfile` worker are now part of the same one-off fixture baseline.
`phase1_oversized_fixture_baseline.py` reproduces their input constructions
verbatim — the `TAIL_BYTES * 2` uninterrupted `x` run (131072 bytes) is
preserved and each scenario runs the collector, failure context, text report
and (for the second) the JSON facade across all three `NetworkUrlDisplay` modes
— without modifying the test file. Medians over 5 repeats:

| Existing test | Payload (uninterrupted `x` run) | Modes | Median (s) |
|---|---|---|---|
| `tests/test_retained_tail_phase6.py::TestFailureContextChannels::test_truncated_tail_is_bounded_and_mode_selected` | `"npm warn " + "x"*(TAIL_BYTES*2) + " https://registry.npmjs.org/pkg.tgz http://proxy.internal/x\n"` | redacted / host-path / exact | 7.877 |
| `tests/test_retained_tail_phase6.py::TestFailureChannelMatrix::test_truncated_tail_is_bounded_in_text_and_json` | `"npm warn " + "x"*(TAIL_BYTES*2) + " https://registry.npmjs.org/pkg.tgz\n"` | redacted / host-path / exact | 7.893 |

The two medians sum to approximately 15.8 s, reproducing the ~16 s hotspot in
`proposal.md`. Per-mode observables confirm the existing bounded behavior:
`exact` retains exactly `TAIL_BYTES` (captured tail 65536 bytes; text context
and JSON tail 65535), `redacted` and `host-path` capture 59 bytes and render
58, every mode emits exactly one retained-diagnostics section, and the JSON
tail equals the text context tail in every mode.

Two independent pytest runs preserve full stdout and exit status:

- `phase1_retained_tail_tests.log` — the two node ids through
  `.venv/bin/python -m pytest … -n 8 --dist loadfile --durations=0 --tb=short -q`:
  `2 passed, 6 subtests passed in 16.56s`, exit status 0, with `7.93s` per call
  — consistent with the two medians above.
- `phase1_retained_tail_file.log` — the whole `tests/test_retained_tail_phase6.py`
  file through the same command: `46 passed, 97 subtests passed in 25.11s`,
  exit status 0, confirming the proposal's ~24.7 s single-worker file figure.

Both runs used the environment in §1.2.1/§1.2.2. The regenerated baseline
script log ends with `fixture baseline exit status: 0`;
`phase1_oversized_fixture_baseline.py` also records the same two scenarios'
medians in its final `--- medians ---` block.

### 1.3 Outcome/model contract tests

`tests/test_diagnostic_recognition_contract.py` expresses, as separate tests:

- closed `CandidateKind` = {`KNOWN`, `UNKNOWN`};
- `DiagnosticCandidate` raw inclusive `start`, exclusive `end`, `end=None` for
  an unterminated candidate, raw character (not byte) coordinates, frozen
  immutability, and rejection of negative/non-integer start, non-exclusive
  end, and a non-member `kind`;
- closed `RecognitionKind` = {`NO_MATCH`, `MATCH`, `NEED_MORE`} **separate**
  from `CandidateKind`, with `NO_MATCH` carrying nothing, `MATCH` carrying a
  candidate (possibly `end=None`) and no retention point, and `NEED_MORE`
  carrying a non-negative `retain_from` and no candidate; plus rejection of
  every inconsistent field combination and a non-member kind;
- the whitelist equals the 17 delta-requirement schemes exactly, every entry
  classifies `KNOWN` in upper/capitalized/title/alternating case, and
  non-whitelist variants (including `npm`, `custom+transport`) classify
  `UNKNOWN`;
- stream-coordinate semantics: `start`/`end`/`retain_from` are absolute
offsets within one recognizer stream (not per-feed), the documented
retained-buffer conversion `buffer_index = stream_offset -
retained_buffer_start` selects the candidate text after a nonzero consumed
prefix containing a multibyte character, and a UTF-8 byte offset is shown to
differ from the character offset.

Because the contract module is greenfield, the test case fails with an
explicit `self.fail(...)` message when the module is absent, so the
pre-implementation run is a set of attributable assertion failures rather than
a collection error.

### 1.4 Pre-implementation P1 run

`phase1_red.log` records
`.venv/bin/python -m pytest tests/test_diagnostic_recognition_contract.py -n 8 --dist loadfile --tb=short -q`
(run with the contract module temporarily absent to reproduce the
pre-implementation state):

```
30 failed in 0.74s
red exit status: 1
```

Every failure is
`AssertionError: docker.versioning.diagnostic_recognition must define the P1
recognition result contract` at `tests/.../contract` — attributable to the
missing contract behavior. There were **no collection errors, import errors,
or environmental failures**, and no pre-existing obligation was deliberately
broken or disabled. No originally-passing new obligation exists to record: all
30 tests are new and all failed for the same single reason (absent module).

---

## GREEN

### 1.5 Immutable candidate and closed outcome model

`docker/versioning/diagnostic_recognition.py` adds:

- `CandidateKind(Enum)` — `KNOWN`, `UNKNOWN`.
- `RecognitionKind(Enum)` — `NO_MATCH`, `MATCH`, `NEED_MORE`.
- `DiagnosticCandidate` — frozen, slotted dataclass with `start: int`,
  `end: int | None`, `kind: CandidateKind`; `__post_init__` rejects negative /
  non-integer `start`, `end <= start`, non-integer `end`, and a non-member
  `kind`.
- `RecognitionResult` — frozen, slotted dataclass with the closed `kind` plus
  the single matching payload (`candidate` for `MATCH`, `retain_from` for
  `NEED_MORE`), strict `__post_init__` validation, and the supported
  `no_match()` / `matched(candidate)` / `need_more(retain_from)` constructors.

No public SDK, DTO, persistence, or serialization surface was added. The
module is internal (`docker.versioning`), is not referenced by any
pre-existing module, and introduces no import cycle (stdlib only).

### 1.6 Fixed case-insensitive scheme whitelist

`KNOWN_SCHEMES` is a `frozenset` of the 17 schemes from the delta requirement.
`classify_scheme(scheme)` lowercases and tests membership, returning
`CandidateKind.KNOWN` / `UNKNOWN`; non-string input raises `TypeError`. Shared
transport and lock validation were not touched: the module imports nothing
from `npm_fetch.py`, `diagnostics`, transports, or dependency-lock modules,
and no existing file was modified in P1 (all P1 changes are new files).

**GREEN run** — `phase1_green.log` records
`.venv/bin/python -m pytest tests/test_diagnostic_recognition_contract.py -n 8 --dist loadfile --tb=short -q`:

```
30 passed, 102 subtests passed in 0.58s
green exit status: 0
```

---

## INTROSPECT

### 1.7 Internal-API documentation

The `diagnostic_recognition` module docstring and the
`DiagnosticCandidate` / `RecognitionResult` / `classify_scheme` docstrings
document:

- **Candidate validity** — a candidate is not proof of a valid URL;
  `KNOWN` is still subject to existing safe-URL validation; whitelist
  membership never makes a URL safe; `UNKNOWN` is hidden without metadata.
- **Offset conversion** — `start`, `end` and `retain_from` are absolute raw
  character offsets within one recognizer stream, `start` inclusive and `end`
  exclusive (`stream[start:end]`); they are not per-feed indices, not UTF-8
  byte offsets, and not decoded-layer coordinates. Incremental callers convert
  at the retention boundary with the documented formula
  `buffer_index = stream_offset - retained_buffer_start`.
- **Outcome ownership** — `MATCH` is processed once in source order,
  `NEED_MORE` owns retention from `retain_from`, `NO_MATCH` commits only
  independently safe text.
- **Classification semantics** — case-insensitive membership;
  `classify_scheme` receives an already-decoded scheme; encoded-scheme
  recognition is a P2 recognizer concern.
- **Incomplete separator vs incomplete URL** — stated explicitly and
  separately: an incomplete separator is `NEED_MORE` with no candidate; an
  incomplete URL is `MATCH` with `candidate.end is None`.

The documentation was checked against the P1 tests and matches them: the
distinction above is asserted by
`test_no_match_carries_no_candidate_and_no_retention_point`,
`test_match_may_carry_an_unterminated_candidate`, and
`test_need_more_carries_a_retention_start_and_no_candidate`. No P1 test or
docstring conflates the two.

**Finding (resolved):** the first draft of the module docstring referred to
"unfinished candidates" loosely; it was rewritten in P1 to separate the two
outcomes before the GREEN gate.

### 1.8 Review record — P1 diff scope and contract consistency

Diff reviewed in P1: three new files only
(`docker/versioning/diagnostic_recognition.py`,
`tests/test_diagnostic_recognition_contract.py`, and the
`openspec/changes/linearize-diagnostic-url-detection/verification/` evidence
set). No pre-existing **code** file was edited. The later P1 rework (§1.10)
also edited the planning artifacts `tasks.md` (task checkboxes plus the P4/P5
fetch obligations) and `design.md` (stream-coordinate wording); those are
planning text, not executable behavior.

| Review question | Finding |
|---|---|
| Does P1 change the projector, recognizer, metadata gate, collector, or fetch paths? | No executable path changed. Only new module/test files plus planning-artifact text (`tasks.md`, `design.md`) differ; no `docker/` or `tests/` behavior file was edited. |
| Does the model match design decision #1 (immutable candidate; closed outcome separate from candidate kind)? | Yes. `DiagnosticCandidate` is frozen/slotted; `CandidateKind` and `RecognitionKind` are separate closed enums. |
| Are illegal outcome states unrepresentable? | Yes via `RecognitionResult.__post_init__`, which rejects every non-matching payload/no-payload combination. (Chosen representation is a tagged dataclass with strict validation rather than a union alias; behaviorally equivalent and directly testable.) |
| Is the whitelist exactly the delta list, case-insensitive, with `npm` excluded? | Yes; `test_whitelist_matches_the_delta_requirement_exactly` binds it. |
| Does P1 touch shared transport or lock validation? | No; the new module has no transport/lock imports and no existing file changed. |
| Is any public SDK/persistence contract introduced? | No; `__all__` names only internal types/functions; nothing is serialized. |
| Is scope beyond P1 absorbed silently? | No. Wiring, retiming, metadata gating, and collector channels are explicitly deferred to P2–P5 by the inventory owner summary. |

No unresolved blocking findings. The only intentional design choice is the
tagged-dataclass outcome (documented above); it satisfies the design
decision's closed-state requirement.

---

## VALIDATE

### 1.9 P1 validation record

**Command** (`phase1_validation.log`):

```
.venv/bin/python -m pytest \
  tests/test_diagnostic_recognition_contract.py \
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
578 passed, 7142 subtests passed in 25.50s
exit status: 0
```

The set includes the new P1 contract tests and every pre-existing suite that
imports `DiagnosticProjector`, `sanitize_diagnostic_text`,
`sanitize_host_paths`, `project_structured_diagnostic`, the npm diagnostic
collector, the npm fetch recognizer, or the retained tail. Zero exit status
confirms no regression on the affected routes.

**Applicable configured type check** (`phase1_types.log`):

```
./scripts/check-types    # ty check --output-format concise docker tests/typing
All checks passed!
exit status: 0
```

The full mandated eight-worker loadfile suite, lint/type discovery, and final
performance before/after evidence are P6 deliverables (tasks 6.2–6.5) and are
not claimed here.

### 1.10 P1 rework — fetch inventory, fixture baseline, coordinates

Three corrections were folded into P1 after review; none changes executable
behavior beyond the new contract module and tests.

1. **Fetch caller inventory.** `phase1_caller_inventory.md` §7 no longer claims
   that only `http`/`https` can enter the fetch route or that no UNKNOWN scheme
   can reach it. It records that `npm_fetch.py` accepts any RFC 3986 scheme and
   that the diagnostic whitelist is a separate policy, and §7.1 adds explicit
   P4/P5 obligations for UNKNOWN successful-fetch facts and grouping without
   restricting the fetch grammar or shared hostname helpers. `tasks.md` 4.9 and
   5.3 reference those obligations.
2. **Oversized-tail baseline.** §1.2.2 records one-off medians for the twelve
   actual existing oversized-tail fixtures
   (`phase1_oversized_fixture_baseline.py`/`.log`) and the pytest
   `--durations=0` run (`phase1_oversized_tests.log`, exit 0). The synthetic
   `"a"*N + " end"` projector probe is retained only as supplementary
   evidence.
3. **Stream coordinates.** `diagnostic_recognition.py` now defines `start`,
   `end` and `retain_from` as absolute raw character offsets within one
   recognizer stream, counts Python characters (not UTF-8 bytes or decoded
   characters), and documents the retention-boundary conversion
   `buffer_index = stream_offset - retained_buffer_start`. Three contract tests
   bind the conversion after a nonzero consumed prefix with a multibyte
   character; `design.md` decision #1 and §1.7 were aligned. Executable
   chunk-continuation behavior remains a P3 concern.

Rerun evidence: RED `phase1_red.log` (30 failed, exit 1), GREEN
`phase1_green.log` (30 passed, 102 subtests, exit 0), validation
`phase1_validation.log` (578 passed, 7142 subtests, exit 0), types
`phase1_types.log` (all checks passed, exit 0).

### P1 deliverable check

| Task | Deliverable | Evidence | Status |
|---|---|---|---|
| 1.1 | Caller inventory, owners for every integer dependency / UNKNOWN bypass | `phase1_caller_inventory.md` incl. §7.1 fetch obligations | done |
| 1.2 | One-off baseline with medians/profiles/environment/log/status; actual oversized-tail fixture timings; original retained-tail bottleneck scenario timings | `phase1_baseline.py`+log; `phase1_oversized_fixture_baseline.py`+log; `phase1_oversized_tests.log`; `phase1_retained_tail_tests.log`; `phase1_retained_tail_file.log`; §1.2.3 | done |
| 1.3 | Outcome/model contract tests | `tests/test_diagnostic_recognition_contract.py` | done |
| 1.4 | Pre-implementation P1 run | `phase1_red.log` (30 failed, exit 1) | done |
| 1.5 | Immutable candidate + closed outcome model | `diagnostic_recognition.py`; `phase1_green.log` | done |
| 1.6 | Case-insensitive scheme whitelist | `diagnostic_recognition.py`; `phase1_green.log` | done |
| 1.7 | Internal-API documentation | module docstrings; §1.7 | done |
| 1.8 | Scope/contract review record | §1.8 | done, no blocking findings |
| 1.9 | P1 + affected pre-existing validation | `phase1_validation.log` (exit 0); `phase1_types.log` (exit 0) | done |

Phase 1 gate: **satisfied**. The baseline was reopened because the
oversized-tail fixture set of §1.2.2 omitted the two original bottleneck
scenarios; §1.2.3 now records their `TAIL_BYTES * 2` medians across all three
modes (7.877 s and 7.893 s, ~15.8 s combined), the two node-id and whole-file
`--durations=0` runs (exit status 0), and the regenerated script log (exit
status 0). All named deliverables exist, every run's full stdout/stderr and
exit status are preserved, RED failures are attributable to the missing
contract, and no failing or excluded test was suppressed. Task 1.2 was
unchecked while this evidence was outstanding and is re-checked only now.

# Phase 1 Verification Evidence — Revised Recognition and Projection Contract

Change: `linearize-diagnostic-url-detection`
Schema: `spec-driven`
Phase: **P1 — Revised recognition and projection contract** (tasks 1.1–1.9)
Depends on: none
Repository: `/home/aavasiljev/work/home/docker-constructor.git`
Planning revision: `365b2de Revise the diagnostic URL and secret recognition plan`

> **Revision note.** The pre-revision P1 record is historical. It defined an
> immutable candidate with a closed `KNOWN`/`UNKNOWN` classification and made
> "arbitrary UNKNOWN scheme hiding" a recognition obligation. The user-approved
> revision supersedes that: detection is **supported-only**, unknown schemes
> remain ordinary text without URL facts, and secret matching is modeled as
> source coverage to be coordinated in later phases. `CandidateKind` and
> `classify_scheme` are retained only as compatibility helpers that must not
> make an unsupported scheme a detected URL. The pre-revision logs
> (`phase1_red.log`, `phase1_green.log`, `phase1_validation.log`,
> `phase1_types.log`) remain on disk as historical evidence and are not
> reused as the revised gate.

Phase 1 introduces/revises the internal recognition result contract only. It
does not rewire the projector, the incremental recognizer, the metadata gate,
or any collector/transport/lock path — those are P2–P5 per the phase DAG.

## Deliverables and evidence map

| Deliverable | Location |
|---|---|
| Revised caller inventory (supported-only, secret coverage, markers/facts) | `verification/phase1_caller_inventory.md` |
| One-off baseline experiment (task 1.2, unchanged; outside tests/CI) | `verification/phase1_baseline.py` + `phase1_baseline.log` (exit 0) |
| Oversized-tail fixture baseline (task 1.2, unchanged) | `verification/phase1_oversized_fixture_baseline.py` + `phase1_oversized_fixture_baseline.log` (exit 0) |
| Oversized-tail and retained-tail test runs (task 1.2, unchanged) | `verification/phase1_oversized_tests.log`, `phase1_retained_tail_tests.log`, `phase1_retained_tail_file.log` (exit 0) |
| Revised P1 contract tests | `tests/test_diagnostic_recognition_contract.py` |
| Revised P1 RED run (full log + runner exit status) | `verification/phase1_revised_red.log` (exit 1) |
| Immutable supported-only candidate + coverage/outcome model | `docker/versioning/diagnostic_recognition.py` (`DiagnosticCandidate`, `SecretCoverage`, `RecognitionResult`, `RecognitionKind`) |
| Fixed case-insensitive supported-scheme set + detection depth docs | `docker/versioning/diagnostic_recognition.py` (`SUPPORTED_SCHEMES`, `is_supported_scheme`), `docker/versioning/diagnostic_recognizer.py` (§Detection versus interpretation depth, historical) |
| Internal-API documentation | module/class/function docstrings in `docker/versioning/diagnostic_recognition.py` (§1.7 below) |
| P1 GREEN run | `verification/phase1_revised_green.log` (exit 0) |
| P1 + affected existing validation run | `verification/phase1_revised_validation.log` (exit 0) |
| Applicable type check | `verification/phase1_revised_types.log` (exit 0) |
| Scope/contract review record (task 1.8) | §Review record below |

---

## RED

### 1.1 Revised caller inventory

`verification/phase1_caller_inventory.md` reconciles every caller and route
for the revised contract:

- **Detection (D1–D3):** typed `MATCH`/`NEED_MORE`/`NO_MATCH` for supported
  schemes only; unknown-scheme text is `NO_MATCH`.  Owner P2/P3.
- **Holding (H1–H4):** ambiguous *supported* prefix state and persistent
  continuation without restarting recognition over retained text.  Owner
  P2/P3.
- **EOF (E1–E3):** typed finalization plus the early irreversible
  commitment exception.  Owner P3/P4.
- **Structured projection (S1–S4):** support- and secret-gated facts.  Owner
  P4.
- **Metadata/facts (M1–M3):** validated, supported, secret-free facts; masked
  markers are not facts.  Owner P4.
- **Secret registration/coverage (K1–K6):** compiled immutable pattern set,
  literal matching across Text/URL boundaries, decoded matching inside
  recognized URLs only, `<secret>` replacement.  Owners P1A/P2/P3/P4.
- **Collector (C1–C7):** exactly one retained representation; permitted facts
  only.  Owner P5.
- **Fetch (F1–F4 and §8.1):** grammar preserved; unsupported RFC-valid
  schemes supply no URL facts or grouping fields.  Owner P4/P5.

The pre-revision fetch correction is retained: `npm_fetch.py` accepts any RFC
3986 scheme and the diagnostic supported set is a separate projection policy.
No route depends on a public SDK or persistence contract.

### 1.2 Baseline experiment (unchanged)

Task 1.2 was completed before the revision and is unchanged.  The one-off
`phase1_baseline.py` records the environment (CPython 3.14.7, Linux x86_64,
32 CPUs, `PENDING_LIMIT_BYTES = 8192`), commands, full stdout and exit status
(0), with `_find_url_start` medians of 0.0135 s → 0.0533 s → 0.2127 s →
0.8611 s → 3.4705 s for 1/2/4/8/16 KiB uninterrupted runs (≈4× per doubling,
quadratic) versus ≈2× per doubling for separated runs.  Per-mode capture
profiles and the synthetic oversized-tail probe are recorded in
`phase1_baseline.log`.  The actual existing oversized-tail fixture timings are
recorded in `phase1_oversized_fixture_baseline.log` (exit 0) with the pytest
`--durations=0` run in `phase1_oversized_tests.log` (12 passed, exit 0); the
two original retained-tail bottleneck scenarios (~7.88 s and ~7.89 s across
modes) and the whole-file run (46 passed, 97 subtests, exit 0) are recorded in
`phase1_retained_tail_tests.log` and `phase1_retained_tail_file.log`.  No
fixture was modified.

### 1.3 Revised outcome/model contract tests

`tests/test_diagnostic_recognition_contract.py` now expresses, as separate
tests:

- **supported-only detection policy:** the exact 17-scheme
  `SUPPORTED_SCHEMES` set; `KNOWN_SCHEMES` is the same set;
  `is_supported_scheme` is case-insensitive over every supported spelling and
  returns `False` for the reviewed unsupported variants (`""`, `httpx`,
  `https+`, `ssh2`, `git+file`, `git+`, `npm`, `custom+transport`, `gopher`,
  `mailto`, `data`, `socks6`, `wss2`, `HTTPX`, `Custom+Transport`) and rejects
  non-string input;
- **supported-only model:** a `DiagnosticCandidate` defaults to the supported
  classification, a supported scheme embedded in longer text
  (`TOKENhttps://host/public`) is a candidate at the supported position, and
  unknown text has no supported start and no URL-validity constructor;
- **raw coordinates:** inclusive `start`, exclusive `end`, `end=None` for a
  detected-but-unterminated candidate, raw Python-character indices after a
  multibyte prefix, frozen immutability, and rejection of negative /
  non-integer / non-exclusive offsets;
- **source secret coverage:** `SecretCoverage` carries inclusive `start` /
  exclusive `end`, is immutable, selects the covered source (including after a
  multibyte prefix), rejects invalid offsets, and shares the candidate
  coordinate space;
- **outcomes:** closed `RecognitionKind` = {`NO_MATCH`, `MATCH`, `NEED_MORE`}
  with strict payload validation, including that unknown text is
  `NO_MATCH` rather than a compatibility `UNKNOWN` match; and
- **stream-coordinate conversion:** the documented
  `buffer_index = stream_offset - retained_buffer_start` formula for
  candidates, retention points, and coverage spans.

**Superseded obligations recorded.** The pre-revision tests required arbitrary
schemes to be a recognised candidate class carrying `CandidateKind.UNKNOWN`.
That is intentionally superseded and is no longer asserted.  The single
compatibility test now only checks that the retained helper still classifies
unsupported schemes as `UNKNOWN`; it explicitly states that `UNKNOWN` is not a
detected URL and must not authorise hostname/host-path/fingerprint facts.

### 1.4 Revised P1 RED

`phase1_revised_red.log` records the revised contract tests run against the
pre-revision module (reset to `HEAD` to reproduce the unmet state):

```
.venv/bin/python -m pytest tests/test_diagnostic_recognition_contract.py \
  -n 8 --dist loadfile --tb=short -q
125 failed, 15 passed, 17 subtests passed in 1.21s
red exit status: 1
```

Every failure is attributable to the missing revised contract, not to
infrastructure: the pre-revision module has no `SUPPORTED_SCHEMES` /
`is_supported_scheme` / `SecretCoverage`, and its `DiagnosticCandidate`
requires an explicit `kind`, so the supported-only and coverage tests fail at
module lookup or constructor signature.  There were **no collection errors,
import errors or environmental failures**, and no pre-existing obligation was
deliberately broken.

The fifteen node-level tests that remain satisfied by the pre-revision module
are the unchanged outcome/JSON-free model obligations:
`test_candidate_rejects_negative_start`,
`test_candidate_rejects_end_not_strictly_after_start`,
`test_candidate_rejects_non_integer_offsets`,
`test_candidate_requires_a_closed_kind`,
`test_no_match_carries_no_candidate_and_no_retention_point`,
`test_need_more_carries_a_retention_start_and_no_candidate`,
`test_need_more_rejects_a_negative_retention_start`,
`test_outcome_kind_is_closed_no_match_match_need_more`,
`test_outcome_is_immutable`, `test_outcome_requires_a_closed_kind`,
`test_unknown_text_is_not_a_matched_candidate`,
`test_need_more_retain_from_is_an_absolute_stream_offset`,
`test_compatibility_classifier_still_marks_unsupported_schemes_unknown`, and
the subtest-level checks inside `test_every_supported_scheme_is_supported_case_insensitively`
and `test_unsupported_schemes_are_not_supported` that depend only on the
compatibility classifier.  The xdist summary counts subtest failures
individually, which is why the aggregate differs from the node-level count.

---

## GREEN

### 1.5 Immutable supported-only models and source secret coverage

`docker/versioning/diagnostic_recognition.py` now defines:

- `SUPPORTED_SCHEMES` — the fixed 17-scheme case-insensitive set (the
  authoritative copy of the delta requirement) with `KNOWN_SCHEMES` as a
  compatibility alias;
- `is_supported_scheme(scheme)` — the supported-only policy query, rejecting
  non-strings;
- `CandidateKind` / `classify_scheme` — retained **compatibility helpers**
  only, documented as superseded for detection and never an authorisation to
  publish an unsupported scheme;
- `DiagnosticCandidate(start, end, kind=CandidateKind.KNOWN)` — frozen and
  slotted with a supported default, so supported-only callers construct a
  candidate without a classification while pre-revision callers keep working;
- `SecretCoverage(start, end)` — frozen slotted source-coverage span with
  inclusive `start`, exclusive `end` and strict offset validation;
- `RecognitionKind` / `RecognitionResult` — the closed
  `NO_MATCH`/`MATCH`/`NEED_MORE` outcome model with strict payload validation
  and the `no_match()` / `matched(candidate)` / `need_more(retain_from)`
  constructors.

No public SDK, DTO, persistence, or serialization surface was added.  The
module is internal (`docker.versioning`), imports only the standard library,
and introduces no import cycle.  Detection is explicitly not proof of validity:
the docstrings separate detection from interpretation and state that markers
are not validated hostname/fingerprint facts.

**GREEN run** — `phase1_revised_green.log`:

```
.venv/bin/python -m pytest tests/test_diagnostic_recognition_contract.py \
  -n 8 --dist loadfile --tb=short -q
40 passed, 117 subtests passed in 0.58s
green exit status: 0
```

### 1.6 Exact scheme set and detection-versus-interpretation depth

`SUPPORTED_SCHEMES` is exactly the delta list: `http`, `https`, `ftp`, `ftps`,
`ssh`, `git`, `git+ssh`, `git+http`, `git+https`, `file`, `ws`, `wss`,
`socks`, `socks4`, `socks4a`, `socks5`, `socks5h`.  Membership is
case-insensitive and `npm:`, `:/` and every unsupported variant are excluded.
Detection depth remains the existing bounded language (three decoding passes
plus encoded-separator matching, effective depth four); the revised contract
adds no fourth whole-URL interpretation pass and no decoded-secret depth.  The
transport, lock and hostname validation contracts are untouched: the module
imports nothing from `npm_fetch.py`, transports or dependency-lock modules and
no existing behavior file was rewritten in P1.

---

## INTROSPECT

### 1.7 Internal-API documentation

The `diagnostic_recognition` module and type docstrings document:

- **supported-only detection** — only `SUPPORTED_SCHEMES` are candidates;
  unknown schemes are ordinary `NO_MATCH` text with terminal safety, literal
  secret handling and bounds but no URL facts, including at clean EOF and on
  fetch routes;
- **raw coordinates** — `start`/`end`/`retain_from`/`SecretCoverage.start`
  and `.end` are absolute raw Python-character offsets within one recognizer
  stream, inclusive start / exclusive end, not per-feed, not UTF-8 bytes, not
  decoded-layer, with the retention conversion
  `buffer_index = stream_offset - retained_buffer_start`;
- **outcome validity and ownership** — `MATCH` is processed once in source
  order and is not proof of validity; `NEED_MORE` owns retention from
  `retain_from`; `NO_MATCH` commits only independently safe text; an
  incomplete separator is `NEED_MORE`, an incomplete URL is `MATCH` with
  `end is None`;
- **supported consumption boundaries** — detection finds an extent; it never
  parses an authority/path, and a supported candidate is still subject to
  existing safe-URL validation before metadata;
- **marker/fact separation** — rendering markers are not validated hostname
  or fingerprint facts, and rendering has its own irreversible commit frontier
  defined in the delta specs; and
- **source secret coverage** — coverage is an independent immutable span, not
  carried on a candidate, because URL and secret recognition overlap.

The documentation was checked against the revised tests: the supported-only
policy, candidate default classification, embedded-start selection, unknown
text, coverage coordinates and outcome closure all match executable
assertions.  No P1 test or docstring conflates incomplete separator with
incomplete URL or treats a compatibility `UNKNOWN` as a detected URL.

### 1.8 Review record — revised P1 diff and historical evidence

Diff reviewed in the revised P1: `docker/versioning/diagnostic_recognition.py`
and `tests/test_diagnostic_recognition_contract.py`, plus the
`verification/` evidence set.  No projector, recognizer, metadata, collector,
transport, lock or fetch path was edited; P2–P5 callers still import the
retained compatibility surface and are unaffected by the P1 diff.  The staged
P2/P3 work in the working tree predates this revision and remains a
reopened-phase concern, not P1 acceptance.

| Review question | Finding |
|---|---|
| Does the revised P1 change any executable path outside the contract module/tests? | No.  Only the internal contract module and its tests differ; compatibility names (`KNOWN_SCHEMES`, `CandidateKind`, `classify_scheme`, `DiagnosticCandidate.kind`) are preserved for pre-revision callers. |
| Is detection now supported-only at the contract level? | Yes.  `SUPPORTED_SCHEMES` / `is_supported_scheme` are authoritative; the module documents unknown schemes as `NO_MATCH` ordinary text. |
| Are illegal model states still unrepresentable? | Yes — `DiagnosticCandidate`, `SecretCoverage` and `RecognitionResult` validate offsets/payload combinations in `__post_init__`. |
| Is source secret coverage modeled without coupling it to URL detection? | Yes — `SecretCoverage` is an independent immutable raw span; coverage is not a candidate field and carries no validity claim. |
| Are the old UNKNOWN-hiding results reused? | No.  The historical logs are labeled historical; the revised gate uses the new RED/GREEN/validation/type logs. |
| Does P1 touch shared transport or lock validation? | No.  The module has no transport/lock imports and no behavior file changed. |
| Is any public SDK/persistence contract introduced? | No.  `__all__` names internal types/functions only; nothing is serialized. |
| Is later-phase scope absorbed silently? | No.  Wiring, secret matching, early commitment, fact gates and collector channels are explicitly deferred to P1A–P5 by the owner summary. |

No unresolved blocking findings.  The retained compatibility helpers are the
only deliberate carry-over and are documented as non-authoritative for
detection and metadata.

---

## VALIDATE

### 1.9 Revised P1 validation record

**Contract + affected pre-existing suites** (`phase1_revised_validation.log`):

```
.venv/bin/python -m pytest \
  tests/test_diagnostic_recognition_contract.py \
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
```

Result:

```
718 passed, 12587 subtests passed in 12.78s
validation exit status: 0
```

The set includes the revised P1 contract tests and every suite that imports
`DiagnosticProjector`, `sanitize_diagnostic_text`, `sanitize_host_paths`,
`project_structured_diagnostic`, the npm diagnostic collector, the npm fetch
recognizer, the recognizer contract, or the retained tail.  Zero exit status
confirms no regression on the affected routes and that the retained
compatibility helpers did not break the reopened P2/P3 suites.

**Applicable configured type check** (`phase1_revised_types.log`):

```
./scripts/check-types
All checks passed!
types exit status: 0
```

The full mandated eight-worker loadfile suite, lint/type discovery, and final
performance before/after evidence are P6 deliverables (tasks 6.2–6.5) and are
not claimed here.

## Revised P1 deliverable check

| Task | Deliverable | Evidence | Status |
|---|---|---|---|
| 1.1 | Revised caller inventory (supported-only, secret coverage, markers/facts, collector/fetch/grouping) | `verification/phase1_caller_inventory.md` | done |
| 1.2 | One-off baseline with medians/profiles/environment/log/status; oversized-tail and retained-tail timings (unchanged) | `phase1_baseline.py`+log; `phase1_oversized_fixture_baseline.py`+log; `phase1_oversized_tests.log`; `phase1_retained_tail_tests.log`; `phase1_retained_tail_file.log` | done |
| 1.3 | Revised contract tests for supported-only detection, coordinates, `end=None`, ambiguous prefixes, embedded starts, unknown text, coverage, and recorded superseded obligations | `tests/test_diagnostic_recognition_contract.py` | done |
| 1.4 | Revised P1 RED with attributable failures and honestly recorded already-passing obligations | `verification/phase1_revised_red.log` (exit 1) | done |
| 1.5 | Immutable supported-only models + source secret coverage; no public/persisted DTO; detection not proof of validity | `diagnostic_recognition.py`; `phase1_revised_green.log` | done |
| 1.6 | Exact case-insensitive scheme set and detection-versus-interpretation depth without changing transport/lock acceptance | `diagnostic_recognition.py`; §1.6 | done |
| 1.7 | Raw-coordinate, validity, consumption-boundary and marker/fact documentation agreeing with the revised tests | module/type docstrings; §1.7 | done |
| 1.8 | Revised P1 diff/historical-evidence review with resolved findings | §1.8 | done, no blocking findings |
| 1.9 | P1 + affected existing validation | `phase1_revised_validation.log` (exit 0); `phase1_revised_types.log` (exit 0) | done |

Phase 1 gate: **satisfied**.  All named deliverables exist, every run's full
stdout/stderr and exit status are preserved, revised RED failures are
attributable to the missing supported-only/coverage contract, and no failing
or excluded test was suppressed.

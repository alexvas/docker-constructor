# P1 Caller Inventory — Diagnostic URL Recognition Contract

Change: `linearize-diagnostic-url-detection` · Phase 1 · task 1.1
Repository root: `/home/aavasiljev/work/home/docker-constructor.git`
Line numbers are against the pre-implementation revision checked out for P1
(`50f9ebc Plan linear incremental diagnostic URL detection`).

This inventory enumerates every caller that depends on the current
**integer-based** recognition/holding contract and every route that could
**bypass** a future `UNKNOWN` classification gate. Each dependency has one
identified owning phase. No earlier phase depends on later work, so an owner
named for P2–P5 is expected to still consume the P1 model while the P1 diff
itself only introduces the model and whitelist.

## 1. Detection contract — `_find_url_start`

`_find_url_start(text: str) -> int | None`
(`docker/versioning/diagnostic_projection.py:314`) is the legacy integer
contract: it returns only a raw start index (or `None`) and discards any
candidate **end** and any **classification** it implicitly discovered. It is
implemented with the unanchored quadratic `_URL_START_RE.search`
(`diagnostic_projection.py:130`) plus bounded `_decode_percent_layer`
(`:286`) passes.

| # | Caller | File:line | Old integer dependency | Possible `UNKNOWN` fact bypass | Owner |
|---|--------|-----------|------------------------|--------------------------------|-------|
| D1 | `_unresolved_candidate` | `diagnostic_projection.py:413` | `_find_url_start(pending) is not None` — a boolean only | None (holding/EOF only) | P2 recognizer; P3 EOF |
| D2 | `_is_complete_url_at_eof` | `diagnostic_projection.py:927` | `_find_url_start(token) != 0` — start-only check | Feeds clean-EOF `_sanitize_candidate`, which derives facts unconditionally | P3 finalization; P4 gate |
| D3 | `DiagnosticProjector._drain` | `diagnostic_projection.py:1226` | `url_index` compared with `secret_index` as plain integers | Drives `_sanitize_candidate` for both known and unknown schemes | P2 raw recognition; P4 gate |

Notes:
- D2's `!= 0` comparison assumes the start cannot be expressed as a typed
  candidate and re-derives completeness separately in
  `_is_complete_url_literal` (`:881`).
- D3 is the only place that selects among multiple candidates and applies
  secret precedence (`_leftmost_secret_match`, `:217`); the P1 model must keep
  raw start/end and classification available here.

## 2. Unresolved holding contract — integer prefix state

`_ambiguous_hold` (`diagnostic_projection.py:390`) calls
`_is_url_prefix_token` (`:368`) → `_url_prefix_state` (`:349`) →
`_url_prefix_state_single` (`:239`). These return the integer states
`_URL_PREFIX_NONE = 0`, `_URL_PREFIX_SCHEME = 1`, `_URL_PREFIX_SEPARATOR = 2`
(`:234`–`:236`).

| # | Caller | File:line | Old integer dependency | Possible `UNKNOWN` fact bypass | Owner |
|---|--------|-----------|------------------------|--------------------------------|-------|
| H1 | `_ambiguous_hold` | `diagnostic_projection.py:394` | `_is_url_prefix_token` boolean derived from the integer state | None (retention only); must not become a recognizer restart | P2 state model; P3 continuation |
| H2 | `DiagnosticProjector._drain` | `diagnostic_projection.py:1253` | `hold = _ambiguous_hold(...)` returns a trailing length | Retains bytes only; no facts emitted | P3 continuation |
| H3 | `_unresolved_candidate` | `diagnostic_projection.py:417` | `state = _url_prefix_state(_trailing_token(pending))` integer comparison | None (EOF decision only) | P3 EOF/abort |
| H4 | `_secret_overlap_hold` | `diagnostic_projection.py:373` | Trailing length integer shared by holding and EOF | None | P3 secret/URL retention merge |

## 3. EOF contract — `finish`

`DiagnosticProjector.finish` (`diagnostic_projection.py:1070`) has three
integer/boolean-dependent branches at `:1094`–`:1111`.

| # | Dependency | File:line | Old integer dependency | Possible `UNKNOWN` fact bypass | Owner |
|---|-----------|-----------|------------------------|--------------------------------|-------|
| E1 | `_secret_overlap_hold` gate | `:1097` | Trailing length truthiness | None (secret) | P3 |
| E2 | `_is_complete_url_at_eof` gate | `:1100` | Start-only + separate completeness integers | Calls `_sanitize_candidate` at `:1102` → facts for UNKNOWN | P3 transitions; P4 gate |
| E3 | `_unresolved_candidate` gate | `:1103` | Integer prefix state + `find_url_start` | Chooses incomplete marker vs ordinary word | P3 abort/EOF |

## 4. Structured projection routes

These are the public/internal entry points that project complete text and
return URL-free text plus facts. All route through `DiagnosticProjector`, so a
single gate in `_sanitize_candidate` covers them, but each must be asserted
independently in P4/P5.

| # | Route | File:line | Old integer dependency | Possible `UNKNOWN` fact bypass | Owner |
|---|-------|-----------|------------------------|--------------------------------|-------|
| S1 | `sanitize_diagnostic_text` | `diagnostic_projection.py:1293` | Via projector `_drain`/`finish` | Returns `hostnames` for any detected candidate | P4 |
| S2 | `sanitize_host_paths` | `diagnostic_projection.py:1307` | Via projector `_drain`/`finish` | Returns `SafeHostPath` facts for any detected candidate | P4 |
| S3 | `project_structured_diagnostic` | `diagnostic_projection.py:1322` | Via projector `_drain`/`finish` | Emits `hostnames` + `url_fingerprints` for any detected candidate | P4 |
| S4 | `project_host_acquisition_failure` | `diagnostic_projection.py:1437` | delegates to S3 | Same as S3 | P4 |

## 5. Metadata derivation

`DiagnosticProjector._sanitize_candidate` (`diagnostic_projection.py:1265`) is
the **single metadata origin**: it derives `_hostnames`, `_host_paths`, and
`_fingerprints` (`:1274`–`:1284`) for every detected candidate without any
scheme classification. This is the primary `UNKNOWN` fact bypass.

| # | Fact | File:line | Current behavior | Owner |
|---|------|-----------|------------------|-------|
| M1 | `_hostnames` append | `:1274` | Unconditional on scheme | P4 classification gate |
| M2 | `_host_paths` append | `:1276` | Unconditional on scheme | P4 classification gate |
| M3 | `_fingerprints` append | `:1279` | Unconditional on scheme | P4 classification gate |

Fact drains/properties that must stay consistent with the gate:
`hostnames` (`:1114`), `url_fingerprints` (`:1126`), `host_paths`
(`:1132`), `take_host_paths` (`:1143`), `take_facts` (`:1149`).

## 6. Collector routes

`docker/versioning/npm_diagnostic_stream.py`:

| # | Route | File:line | Dependency | Possible `UNKNOWN` fact bypass | Owner |
|---|-------|-----------|------------|--------------------------------|-------|
| C1 | `project_tail` | `:122` | `DiagnosticProjector` (`:149`) and `sanitize_diagnostic_text` (`:155`) | Renders facts into `host-path` tail | P5 |
| C2 | `NpmDiagnosticStream.__init__` | `:223`, `:230` | Builds recognition/retained projectors | Both projectors derive UNKNOWN facts | P5 |
| C3 | `_drain_retained_facts` | `:457` | `take_facts` / `take_host_paths` | Drains facts derived from UNKNOWN | P5 |
| C4 | `_absorb_projector_facts` | `:609`, `:613` | `take_facts` / `take_host_paths` | Attaches hostnames/fingerprints to a line event | P5 |

`docker/versioning/host_presentation.py` consumes the projected facts and is
presentation-only:

| # | Route | File:line | Dependency | Owner |
|---|-------|-----------|------------|-------|
| C5 | `format_diagnostic_text` | `:218`, renders `:229`–`:230` | `diagnostic.hostnames` | P5 |
| C6 | `format_failure_report` | `:234`, renders `:261`–`:262` | `hostnames` argument | P5 |
| C7 | interactive grouping | field `:680`, assignment `:892`, `_attach_hosts` `:926` | `envelope.hostnames` | P5 |

`docker/versioning/host_progress.py` validates the DTO tuple fields
(`:260`, `:311`, `:366`, `:444`); no derivation happens there.

## 7. Fetch route

`docker/versioning/npm_fetch.py`:

The fetch parser (`parse_npm_fetch_line` / `NpmFetchRecognizer`) validates the
URL field against the RFC 3986 scheme grammar `_SCHEME_RE =
[A-Za-z][A-Za-z0-9+.\-]*` (`:101`) plus `normalized_url_host`. It does **not**
consult the diagnostic scheme whitelist, so it accepts any structurally valid
scheme, including non-whitelist schemes such as `custom+transport://...`.
Diagnostic whitelist membership (`KNOWN_SCHEMES` / `classify_scheme`) is a
separate policy and is not applied on this route today. Consequently an
`UNKNOWN` scheme *can* enter F1–F3 and can potentially produce host/path
facts or influence `host-path` grouping.

| # | Route | File:line | Dependency | `UNKNOWN` scheme reachable? / fact bypass | Owner |
|---|-------|-----------|------------|-------------------------------------------|-------|
| F1 | `_safe_host_path_text` | `:880`, `:899` | `sanitize_host_paths` | **Yes, potential.** Derives normalized host + safe path for any RFC-valid scheme the parser accepts, including non-whitelist `UNKNOWN`. The result feeds `canonical_fetch_text` (rendered line) and `fetch_group_identity` (`host-path` group key). | P4 gate; P5 channel |
| F2 | `NpmFetchRecognizer` literal layer | `:805` | `literal_authority_host` | Reachable once the grammar accepts the scheme; not whitelist-gated. Feeds literal-host acceptance for any RFC-valid scheme. | P4 audit; grammar preserved |
| F3 | `NpmFetchRecognizer` decoded layer | `:815` | `normalized_authority_host` | Same, after user-information decoding. | P4 audit; grammar preserved |
| F4 | `_is_complete_url` | `:287` | `normalized_url_host` | RFC scheme/transport validation, not diagnostic projection or whitelist classification. | **Not in scope** (design non-goal) |

Do **not** restrict fetch parsing (`_SCHEME_RE`, `_LINE_RE`,
`NpmFetchRecognizer`) or the shared `normalized_url_host` /
`literal_authority_host` / `normalized_authority_host` helpers to enforce the
diagnostic whitelist. The whitelist is a projection policy; the fetch grammar
stays RFC-valid.

### 7.1 UNKNOWN successful-fetch verification obligations (P4/P5)

Because F1–F3 are not whitelist-gated, P4 and P5 MUST explicitly cover a
recognized `npm http fetch` line whose URL scheme is RFC-valid but absent from
`KNOWN_SCHEMES`, in addition to the ordinary diagnostic routes:

- **P4 (fact derivation):** verify that `host-path` rendering and
  `fetch_group_identity` / `canonical_fetch_text` do not emit, render, or group
  by normalized host/path or fingerprint for an UNKNOWN successful-fetch URL.
  If they do, apply the classification gate (`classify_scheme` on the record's
  scheme) at the fetch fact-derivation boundary (`_safe_host_path_text` and the
  `host-path` branch of `fetch_group_identity`), without changing the fetch
  grammar or the shared hostname helpers.
- **P4 (known behavior preserved):** verify `http`/`https` fetch URLs keep
  their existing host/path facts, fingerprints, and `host-path` grouping, and
  that `exact` local source is unchanged by classification.
- **P4/P5 (grammar preserved):** verify the parser still accepts and renders
  structurally valid UNKNOWN fetch URLs under the existing fetch grammar and
  `redacted` behavior (URL hidden, no facts).
- **P5 (channels and grouping):** assert collector/retained-tail and SDK
  channels expose no host/path/fingerprint from an UNKNOWN successful-fetch
  line, and that `host-path` request-count grouping is not influenced by an
  UNKNOWN URL's host/path, while known fetch grouping and the recognized
  successful-fetch grammar remain intact.

## 8. Owner summary

- **P1 (this phase):** introduce `DiagnosticCandidate`, `RecognitionResult`,
  `CandidateKind`, `RecognitionKind`, and `KNOWN_SCHEMES`/`classify_scheme`
  in `docker/versioning/diagnostic_recognition.py`. No projector caller is
  rewired in P1.
- **P2:** replace D1/D3/H1–H4 integer start/prefix logic with the compact
  recognizer producing P1 results.
- **P3:** rewire E1–E3 and H2/H3 continuation/finalization to typed outcomes;
  keep retention bounded.
- **P4:** apply the classification gate at M1–M3 and S1–S4; reconcile this
  inventory route by route, including the §7.1 UNKNOWN successful-fetch
  fact/grouping obligations.
- **P5:** assert C1–C7 channel behavior and the §7.1 fetch channel/grouping
  obligations.

No route in this inventory depends on a public SDK or persistence contract;
the P1 model is internal to `docker.versioning`.

# P1 Caller Inventory — Revised Diagnostic URL and Secret Recognition Contract

Change: `linearize-diagnostic-url-detection` · Phase 1 · task 1.1
Repository root: `/home/aavasiljev/work/home/docker-constructor.git`

This inventory reconciles every caller and route that depends on the
diagnostic URL/secret recognition contract, for the **revised** supported-only
detection and overlapping-secret design.  It supersedes the pre-revision
inventory that assigned owners for *arbitrary UNKNOWN hiding*.  Unknown
schemes are now ordinary text, not a hidden candidate class; secret matching
is coordinated over source coordinates rather than applied as independent
per-span filters.

Legacy line numbers are against the pre-implementation revision checked out
when the original inventory was written
(`50f9ebc Plan linear incremental diagnostic URL detection`).  They identify
the legacy integer-contract call sites; the revised phases replace them.  The
P1 diff introduces only the contract model in
`docker/versioning/diagnostic_recognition.py` and does not rewire any caller.

## 1. Detection contract — supported-only

The legacy detector `_find_url_start(text) -> int | None`
(`diagnostic_projection.py:314`) returns only a raw start and discards the
candidate end and any classification.  The revised recognizer returns a
typed `RecognitionResult` for **supported** schemes only:

* `MATCH(candidate)` for a supported scheme followed by a supported
  separator, with inclusive raw `start` and exclusive raw `end` (`end=None`
  while unterminated);
* `NEED_MORE(retain_from)` for an ambiguous supported prefix;
* `NO_MATCH` for everything else, including every unknown-scheme string.

| # | Caller | Legacy site | Revised dependency | Owner |
|---|--------|-------------|--------------------|-------|
| D1 | `_unresolved_candidate` | `diagnostic_projection.py:413` | Typed `NEED_MORE`/`NO_MATCH` instead of a start-index boolean | P2 recognizer; P3 continuation |
| D2 | `_is_complete_url_at_eof` | `diagnostic_projection.py:927` | `MATCH` with `end is None` vs a prefix; unknown schemes are `NO_MATCH` | P3 finalization |
| D3 | `DiagnosticProjector._drain` | `diagnostic_projection.py:1226` | Typed candidate start/end and secret coverage ranges instead of integer comparisons | P2 recognition; P3 coordination; P4 facts |

Detection no longer has an "arbitrary UNKNOWN candidate" obligation.  A
supported scheme embedded in longer text (for example `TOKENhttps://host`)
is selected at the supported scheme position; a longer unsupported prefix does
not become a candidate.

## 2. Unresolved holding contract — ambiguous supported prefix

`_ambiguous_hold` (`diagnostic_projection.py:390`) calls
`_is_url_prefix_token` (`:368`) → `_url_prefix_state` (`:349`) →
`_url_prefix_state_single` (`:239`), returning the integer states
`_URL_PREFIX_NONE = 0`, `_URL_PREFIX_SCHEME = 1`, `_URL_PREFIX_SEPARATOR = 2`
(`:234`–`:236`).

| # | Caller | Legacy site | Revised dependency | Owner |
|---|--------|-------------|--------------------|-------|
| H1 | `_ambiguous_hold` | `:394` | Ambiguous **supported** prefix state; must not restart recognition over retained text | P2 state; P3 continuation |
| H2 | `DiagnosticProjector._drain` | `:1253` | Persistent continuation state and earliest required suffix | P3 continuation |
| H3 | `_unresolved_candidate` | `:417` | Typed prefix outcome for EOF/abort finalization | P3 EOF/abort |
| H4 | `_secret_overlap_hold` | `:373` | Secret overlap must merge with URL holding without copied/replayed suffix text | P1A core; P3 secret/URL coordination |

## 3. EOF contract — `finish`

`DiagnosticProjector.finish` (`diagnostic_projection.py:1070`) has three
integer/boolean-dependent branches at `:1094`–`:1111`.

| # | Dependency | Legacy site | Revised dependency | Owner |
|---|------------|-------------|--------------------|-------|
| E1 | secret-overlap gate | `:1097` | Source secret coverage ranges, not a trailing length | P1A core; P3 coordination |
| E2 | `_is_complete_url_at_eof` gate | `:1100` | `MATCH` eligibility for supported URLs only; no facts for unknown schemes | P3 finalization; P4 facts |
| E3 | `_unresolved_candidate` gate | `:1103` | Existing incomplete marker only for unresolved uncommitted display remainders | P3 EOF/abort |

Irreversible early commitments change E2/E3: a detected redacted URL emits one
`<redacted>` at separator completion and is not replaced or duplicated at
EOF/abort/overflow; only an unresolved uncommitted remainder receives the
existing marker.

## 4. Structured projection routes

These entry points project complete text and return URL-free text plus facts.
All route through `DiagnosticProjector`, so one fact gate covers them, but
each must be asserted independently in P4/P5.

| # | Route | Legacy site | Revised dependency | Owner |
|---|-------|-------------|--------------------|-------|
| S1 | `sanitize_diagnostic_text` | `diagnostic_projection.py:1293` | Supported-only projection + secret-aware rendering | P4 |
| S2 | `sanitize_host_paths` | `diagnostic_projection.py:1307` | `SafeHostPath` facts only from validated supported secret-free hosts/paths | P4 |
| S3 | `project_structured_diagnostic` | `diagnostic_projection.py:1322` | Hostname/fingerprint facts gated by support and secret coverage | P4 |
| S4 | `project_host_acquisition_failure` | `diagnostic_projection.py:1437` | Same gates as S3 | P4 |

## 5. Metadata derivation and marker/fact separation

`DiagnosticProjector._sanitize_candidate` (`diagnostic_projection.py:1265`)
is the single metadata origin: it derives `_hostnames`, `_host_paths`, and
`_fingerprints` (`:1274`–`:1284`).  Under the revised contract:

* only independently validated **supported** and **secret-free** hosts yield a
  normalized-host fact or URL fingerprint;
* a secret-bearing host yields neither; a secret-bearing path yields no
  fingerprint but may retain a safe hostname fact;
* a preceding adjacent secret (`TOKENhttps://host/public`) yields no URL facts
  after `<secret>` replacement;
* rendering markers (`<redacted>`, `<redacted-host>`, `/<redacted-path>`) are
  **not** validated hostname or fingerprint facts.

| # | Fact | Legacy site | Revised obligation | Owner |
|---|------|-------------|--------------------|-------|
| M1 | `_hostnames` append | `:1274` | Supported + secret-free + independently validated only | P4 gate |
| M2 | `_host_paths` append | `:1276` | Safe secret-free components only; masked components are display-only | P4 gate |
| M3 | `_fingerprints` append | `:1279` | Supported + secret-free only; order/multiplicity preserved | P4 gate |

Fact drains that must stay consistent with the gate: `hostnames` (`:1114`),
`url_fingerprints` (`:1126`), `host_paths` (`:1132`),
`take_host_paths` (`:1143`), `take_facts` (`:1149`).

## 6. Secret registration and source coverage

Secret handling is new work for the revised contract.  It is not a per-span
filter: literal secrets can cross Text/URL boundaries and raw terminators, and
URL starts can overlap secret characters.

| # | Concern | Current mechanism | Revised obligation | Owner |
|---|---------|-------------------|--------------------|-------|
| K1 | Secret registration | `dedupe_secrets`, `longest_secret_length` (`diagnostic_projection.py:74`–`75`) | Immutable compiled pattern set shared across streams | P1A core; P2 integration |
| K2 | Literal matching | `_leftmost_secret_match` (`:228`), `_secret_overlap_hold` (`:384`) | Streaming literal matching over original source with exclusive ends | P1A core; P2 integration |
| K3 | Cross-boundary coverage | `_ambiguous_hold` (`:401`), `_unresolved_candidate` (`:411`) | Secret coverage survives Text/URL boundaries and raw terminators without rejection | P3 coordination |
| K4 | URL-decoded matching | currently no bounded decoded secret matching | Up to three percent-decoding passes **inside recognized URLs** only, with raw provenance | P2 integration |
| K5 | Overlap with URL continuations | none (per-span filters) | `TOKENht`/`https` continuation survives without replay or copied suffix | P3 coordination |
| K6 | Replacement marker | deletion / `<redacted>` | Ordinary safe text secret renders `<secret>`; no URL is manufactured by joining | P2/P4 |

## 7. Collector routes

`docker/versioning/npm_diagnostic_stream.py`:

| # | Route | Legacy site | Dependency | Owner |
|---|-------|-------------|------------|-------|
| C1 | `project_tail` | `:122` | `DiagnosticProjector` (`:149`), `sanitize_diagnostic_text` (`:155`) | P5 |
| C2 | `NpmDiagnosticStream.__init__` | `:223`, `:230` | Builds recognition/retained projectors; exactly one retained representation | P5 |
| C3 | `_drain_retained_facts` | `:457` | `take_facts` / `take_host_paths` gated by support/secret coverage | P5 |
| C4 | `_absorb_projector_facts` | `:609`, `:613` | Attaches only permitted hostnames/fingerprints to a line event | P5 |

`docker/versioning/host_presentation.py` is presentation-only:

| # | Route | Legacy site | Dependency | Owner |
|---|-------|-------------|------------|-------|
| C5 | `format_diagnostic_text` | `:218`, renders `:229`–`:230` | `diagnostic.hostnames` | P5 |
| C6 | `format_failure_report` | `:234`, renders `:261`–`:262` | `hostnames` argument | P5 |
| C7 | interactive grouping | field `:680`, assignment `:892`, `_attach_hosts` `:926` | `envelope.hostnames` | P5 |

`docker/versioning/host_progress.py` validates the DTO tuple fields
(`:260`, `:311`, `:366`, `:444`); no derivation happens there.

## 8. Fetch route

`docker/versioning/npm_fetch.py`:

The fetch parser (`parse_npm_fetch_line` / `NpmFetchRecognizer`) validates the
URL field against the RFC 3986 scheme grammar `_SCHEME_RE =
[A-Za-z][A-Za-z0-9+.\-]*` (`:101`) plus `normalized_url_host`.  It does **not**
consult the diagnostic supported-scheme set, so it accepts any structurally
valid scheme, including unsupported schemes such as `custom+transport://...`.
Supported-scheme recognition is a projection policy, not a fetch-grammar
policy.

| # | Route | Legacy site | Dependency | Unsupported scheme reachable? | Owner |
|---|-------|-------------|------------|-------------------------------|-------|
| F1 | `_safe_host_path_text` | `:880`, `:899` | `sanitize_host_paths` | Yes for structurally valid schemes; must emit no host/path/fingerprint for unsupported schemes | P4 gate; P5 channel |
| F2 | `NpmFetchRecognizer` literal layer | `:805` | `literal_authority_host` | Reachable; feeds literal-host acceptance for any RFC-valid scheme | P4 audit; grammar preserved |
| F3 | `NpmFetchRecognizer` decoded layer | `:815` | `normalized_authority_host` | Same, after user-information decoding | P4 audit; grammar preserved |
| F4 | `_is_complete_url` | `:287` | `normalized_url_host` | RFC scheme/transport validation only | **Not in scope** (design non-goal) |

Do **not** restrict fetch parsing (`_SCHEME_RE`, `_LINE_RE`,
`NpmFetchRecognizer`) or the shared `normalized_url_host` /
`literal_authority_host` / `normalized_authority_host` helpers to enforce
supported-scheme recognition.  The supported set is a projection policy; the
fetch grammar stays RFC-valid.

### 8.1 Fetch/collector obligations (P4/P5)

Because F1–F3 are not supported-scheme gated, P4 and P5 MUST explicitly cover a
recognized `npm http fetch` line whose URL scheme is RFC-valid but
unsupported:

- **P4 (facts):** `host-path` rendering and `fetch_group_identity` /
  `canonical_fetch_text` must emit, render, or group by no normalized
  host/path or fingerprint for an unsupported successful-fetch URL, while
  keeping the fetch grammar and shared hostname helpers.  If they do, apply
  the supported-scheme gate at the fetch fact-derivation boundary.
- **P4 (known behavior):** `http`/`https` fetch URLs keep host/path facts,
  fingerprints, and `host-path` grouping; `exact` local source is unchanged.
- **P4 (grammar versus canonical grouping regression):** the parser still
  accepts structurally valid unsupported fetch URLs. Test
  `fetch_group_identity` and `canonical_fetch_text` independently of text
  projection: recognized successful-fetch groups use the selected canonical
  request-count policy (including URL/latency omission in `redacted`), but
  unknown URLs supply no URL-derived host/path/fingerprint grouping fields.
  Canonical omission is fetch presentation policy, not unknown-URL detection
  or permission to remove unknown text from other channels.
- **P4 (projection regression):** project the same recognized fetch source
  through structured/redacted and host-path text projection, including clean
  EOF and literal secrets. An unknown scheme with no supported embedded start
  remains ordinary text: preserve it subject to literal `<secret>` replacement,
  terminal safety and bounds, and derive no URL facts. Do not apply decoded
  URL-secret matching or URL-hiding markers solely because it is URL-shaped.
- **P5 (SDK/retained-text versus grouping regressions):** feed the same
  recognized unknown-scheme fetch through the collector. Assert SDK text and
  retained redacted/host-path text preserve the ordinary unknown-scheme source
  subject to literal secrets, terminal safety and bounds, independently of
  canonical local fetch presentation. Assert no host/path/fingerprint facts
  reach those channels, while request-count grouping follows the canonical
  fetch key without URL-derived fields. Cover different unknown URL text,
  changed latency, clean EOF, and local `exact` with an attached SDK sink:
  exact retains source/secrets; SDK still replaces literal secrets but does
  not hide unknown-scheme text. Ordinary non-fetch diagnostics may group
  differently when their preserved unknown text differs; recognized fetches
  instead follow the selected canonical fetch grouping policy.

## 9. Owner summary

- **P1 (this phase):** introduce the revised recognition result model
  (`DiagnosticCandidate`, `RecognitionResult`, `SecretCoverage`,
  `RecognitionKind`, `SUPPORTED_SCHEMES`, `is_supported_scheme`) in
  `docker/versioning/diagnostic_recognition.py`.  `CandidateKind` and
  `classify_scheme` remain compatibility helpers only and must not make an
  unsupported scheme a detected URL.  No projector caller is rewired in P1.
- **P1A:** standalone dependency-free Aho–Corasick trie/cursor for literal
  secret matching, with failure/output links and deterministic cumulative
  counters (K1, K2).
- **P2:** replace D1–D3 and H1–H4 integer start/prefix logic with
  supported-only recognition integrated with the P1A core; add decoded secret
  matching inside recognized URLs (K4, K6).
- **P3:** rewire E1–E3 and H2/H3 continuation/finalization; persistent
  overlapping secret/URL coordination, one raw owner, early irreversible
  commitments (K3, K5).
- **P4:** apply support/secret gates at M1–M3 and S1–S4; reconcile this
  inventory route by route, including §8.1.
- **P5:** assert C1–C7 channel behavior and the §8.1 fetch channel/grouping
  obligations.

No route in this inventory depends on a public SDK or persistence contract;
the P1 model is internal to `docker.versioning`.  Shared transport, lock and
hostname validation contracts are unchanged.

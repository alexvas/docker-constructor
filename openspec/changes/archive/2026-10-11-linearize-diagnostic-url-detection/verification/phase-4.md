# Phase 4 — policy rendering and secret-aware validated facts

**Status: PASS for P4 only.**  The revised policy markers, distinct masked
display products and validated fact gates are implemented by the persistent
coordinator introduced in P3; P4 locks them with an independent focused suite
and reconciles every inventoried projection/metadata/fetch route.  The full
suite and configured type check pass on the final code state.  Overall
progress is 54/72, P4 10/10.  P5–P6 remain pending.

## RED → GREEN

`phase4_red.log` records the initial run of the new focused suite: 31
methods passed with 2 failing subtests (30 subtests passed), exit 1.  Both
failing subtests were traceable to test-harness assumptions, not production
defects:

1. `test_unknown_scheme_at_a_delimiter_stays_text` parametrized the *safe*
   projector over `NetworkUrlDisplay.EXACT`.  The safe projector deliberately
   rejects `exact`; exact rendering is owned by the collector.  The test now
   drives the projector over `redacted`/`host-path` and the collector over all
   three modes.
2. `test_secret_bearing_fetch_url_yields_no_host_path_fact` expected a
   secret-bearing *path* to yield no host/path fact.  The revised contract
   allows the safe hostname with a masked path
   (`host.example/<redacted-path>`), so the expectation was corrected to match
   the spec and the normal projector route.

Neither failure exposed a P3 defect: the P3 coordinator already delivers the
revised P4 behavior (`_settle_components`, `_record_url`,
`_host_contains_secret`, `_coverage`).  This is recorded honestly rather than
manufacturing a production RED.  `phase4_policy_green.log` records the
corrected focused suite: **33 passed, 32 subtests passed, exit 0.**

## Task-to-evidence review

| Task | Executable evidence |
| --- | --- |
| 4.1 | `UnknownTextPolicyTests`: unknown scheme at delimiter/EOF/structured, unknown-scheme text in every local and SDK channel, `TOKENhttps` secret-prefix non-manufacture |
| 4.2 | `PolicyMarkerTests`: credential/host/path secrets, masked host/path products, malformed and encoded URLs, early irreversible redacted marker, standalone `<secret>`, delayed component/fact settlement |
| 4.3 | `DecodedSecretScopeTests`: credential-only permits facts, host secret suppresses hostname+fingerprint, path secret suppresses fingerprint but keeps hostname, three-pass decoded scope |
| 4.4 | `phase4_red.log` with the two harness-expectation failures distinguished from production behavior; corrected GREEN above |
| 4.5 | `PolicyMarkerTests`, `DecodedSecretScopeTests`, `phase4_policy_metadata.md`; markers absent from `hostnames`/`host_paths`/fingerprints |
| 4.6 | `FactGateRouteTests`: same gates on normal, clean-EOF, structured and host-acquisition-failure routes; unrelated-URL facts preserved |
| 4.7 | `CanonicalIdentityTests`: canonical host, explicit default port, userinfo/query/fragment exclusion, order/multiplicity, session isolation; `test_no_fingerprint_from_secret_bearing_host_path_or_prefix` |
| 4.8 | `phase4_policy_metadata.md` and the `diagnostic_projection` module docstring "Policy rendering versus validated metadata", "Decoded-secret scope" and "Exact exception" sections; examples mirror focused test assertions |
| 4.9 | `FetchRouteReconciliationTests` and the route table below |
| 4.10 | `phase4_validation.log` (full eight-worker suite) and `phase4_types.log` |

## Route reconciliation

Routes from `phase1_caller_inventory.md` §4–§8.

### Structured projection (S1–S4) — P4 gate

| # | Route | Revised obligation | Evidence |
| --- | --- | --- | --- |
| S1 | `sanitize_diagnostic_text` | supported-only projection, `<secret>` replacement | `UnknownTextPolicyTests`, `PolicyMarkerTests`, P3 suite |
| S2 | `sanitize_host_paths` | `SafeHostPath` only from a validated supported URL; masked path carries the marker | `test_secret_bearing_and_unsafe_path_render_masked_path`, `test_secret_bearing_fetch_url_facts_match_the_projector` |
| S3 | `project_structured_diagnostic` | hostname/fingerprint gates identical to S1/S2 | `test_structured_route_applies_the_same_gates` |
| S4 | `project_host_acquisition_failure` | same gates as S3; no exception message or credential leak | `test_host_acquisition_failure_route_applies_the_same_gates` |

### Metadata derivation (M1–M3) — P4 gate

| # | Fact | Revised obligation | Evidence |
| --- | --- | --- | --- |
| M1 | `_hostnames` | supported + secret-free + independently validated only | `DecodedSecretScopeTests` |
| M2 | `_host_paths` | safe components; masked components are display-only and never fingerprints | `PolicyMarkerTests`, `FactGateRouteTests` |
| M3 | `_fingerprints` | supported + secret-free only; order/multiplicity/session key preserved | `CanonicalIdentityTests` |

Markers (`<redacted>`, `<redacted-host>`, `/<redacted-path>`) are never
normalized-host or fingerprint facts: `test_markers_are_not_normalized_host_facts`.

### Fetch route (F1–F4) — P4 facts gate, P5 channel

The fetch parser keeps the RFC 3986 scheme grammar and the shared
`normalized_url_host` / `literal_authority_host` / `normalized_authority_host`
helpers; supported-scheme recognition stays a projection policy, not a grammar
policy (`FetchRouteReconciliationTests.test_unknown_scheme_fetch_line_is_recognized_without_facts`).

| # | Route | P4 result |
| --- | --- | --- |
| F1 | `_safe_host_path_text` | unsupported scheme yields no host/path grouping field; supported secret-bearing host yields no fact; secret-bearing path yields the safe host with `/<redacted-path>` |
| F2 | literal authority layer | grammar preserved for any RFC-valid scheme; no supported-scheme restriction added |
| F3 | decoded authority layer | same; shared validation unchanged |
| F4 | `_is_complete_url` | RFC scheme/transport validation only; unchanged (design non-goal) |

Unsupported successful-fetch URLs keep fetch grammar recognition but supply no
URL-derived host/path grouping field (`test_unknown_scheme_fetch_line_is_recognized_without_facts`).
`redacted` canonical rendering omits the URL as presentation policy
(`test_canonical_fetch_text_hides_secret_bearing_supported_urls`), which is not
unknown-URL detection and grants no facts.

### Collector routes (C1–C7) — P5

Named for traceability only; behavior is asserted in P5.  No P4 change is made
to these channels.

## Outstanding P5 obligation (INTROSPECT finding)

`npm_fetch._safe_host_path_text` derives its display key through
`sanitize_host_paths`, which intentionally has no normalized-host fact for a
secret-bearing host.  As a result the *canonical fetch group text* falls back
to `<redacted>` for a secret-bearing host even though the projector's selected
`host-path` local text correctly renders `<redacted-host>/public`.  The
`local_text` and retained tail are correct; only the canonical request-count
group rendering loses the masked host.  The revised spec requires
`<redacted-host>` here, and task 5.6 ("Align grouping and fetch display paths
with masked rendering/fact gates") owns the fix, together with the existing
`add-configurable-network-url-display` tests
(`test_host_path_identity_fails_closed_for_secret_host`,
`test_host_path_canonical_text_fails_closed_for_secret_host`) that still lock
the pre-revision `<redacted>` fallback.  This is a P5 obligation, not a P4
defect: P4's fact gate (no normalized-host fact) is correct.

**Resolved in P5 (task 5.6):** the masked host/path display product is now
carried separately from the validated fact, so the fetch group key and
canonical text render `<redacted-host>/public`.  The two pre-revision tests
were renamed to `test_host_path_identity_masks_secret_host` and
`test_host_path_canonical_text_masks_secret_host`; see `phase-5.md`.

## Final-state verification

All pytest commands use `.venv/bin/python -m pytest ... -n 8 --dist loadfile
--tb=short -q`.  Full stdout/stderr and the actual exit status are in each log.

| Command / evidence | Result | Exit |
| --- | --- | --- |
| pytest `tests/test_diagnostic_policy_phase4.py`; `phase4_policy_green.log` | 33 passed, 32 subtests passed | 0 |
| pytest `tests`; `phase4_validation.log` | 6385 passed, 13 skipped, 11 warnings, 17567 subtests passed | 0 |
| `./scripts/check-types`; `phase4_types.log` | All checks passed | 0 |

The full suite includes revised P1, P1A, P2, P3 and P4 plus affected
projection, collector, lifecycle, presentation and all remaining repository
tests.  The existing multiprocessing/fork `DeprecationWarning`s are retained in
the log.  There is no configured Python lint/build check; typecheck uses `ty`
and Python 3.14.

## Scope and migration

No specification file was edited except the completion boxes in `tasks.md`.  No
production regex/library fallback, new dependency, configuration option,
persistence format, argv, deadline, scheduler or shutdown-budget change was
introduced.  Documentation lives in `phase4_policy_metadata.md` and the
`diagnostic_projection` module docstring.  Existing P1–P3 evidence remains
valid; P4 reuses it only for unchanged obligations and adds the policy and fact
assertions above.  Sync/archive remains a separate explicit workflow.

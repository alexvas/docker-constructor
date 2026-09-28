# Phase 5 verification — policy-aware live presentation

Change: `add-configurable-network-url-display` (schema `spec-driven`).
Scope: tasks 5.1–5.10. This evidence file is separate from the specification
artifacts.

## Deliverables

### Internal presentation-only envelope (`docker/versioning/host_progress.py`)

`HostDiagnosticEnvelope(phase, step, stream, classification, text, hostnames,
logical_resource, url_fingerprints, fetch_key, fetch_text)` is the
collection-to-presentation interface. It is deliberately **not** a widening of
the external DTO:

- `text` is the mode-selected local representation (for a fetch it retains the
  source latency, matching the selected retained-tail representation).
- `fetch_key: FetchGroupKey | None` and `fetch_text: str | None` appear
  together for a recognized fetch in `redacted` or `host-path`; `fetch_text`
  is the canonical latency-free rendering.
- Validation rejects a fetch key without canonical text, canonical text
  without a key, a non-`FetchGroupKey` key, and any key whose
  `display` is `NetworkUrlDisplay.EXACT`. Exact mode never groups, so an EXACT
  key is a contract error: exact envelopes carry `fetch_key=None` and the
  terminal-safe source line as `text`.
- `presentation_text` returns `fetch_text` for a fetch and `text` otherwise;
  `is_fetch` reports fetch membership.
- `for_diagnostic(diagnostic, *, text=None, fetch_key=None, fetch_text=None)`
  preserves the ordinary presentation metadata and supplies the internal-only
  fetch inputs.

The envelope is absent from `HostBuildEvent`, so the external SDK contract
(callback type `Callable[[HostBuildEvent], None]`) can never observe it.

### Dependency-neutral fetch identity (`docker/versioning/fetch_identity.py`)

`FetchGroupKey` moved from `npm_fetch.py` into a new low-level module whose
only dependency is `NetworkUrlDisplay`. `npm_fetch` re-exports the exact same
class, so its public identity API is stable (`npm_fetch.FetchGroupKey is
fetch_identity.FetchGroupKey`). This removes the presentation → parser →
projector → presentation import cycle while keeping one authoritative key.

### Fetch request-count grouping (`docker/versioning/diagnostic_identity.py`)

- `DiagnosticIdentity` gains `fetch_key`; `presentation_metadata` includes it,
  so a fetch group and an ordinary group can never compare equal.
- `identity_for` accepts either the external DTO (normalized via
  `HostDiagnosticEnvelope.for_diagnostic`) or the internal envelope. For a
  fetch it uses the canonical `presentation_text` and drops URL fingerprints,
  because the fetch key — not the hidden URL identity — delimits the group.
- `format_request_count(text, count)` produces the canonical
  `text + " — N requests"` suffix (`count < 2` returns the text unchanged).
- `DiagnosticGroup.final_text()` selects the request-count suffix for a fetch
  group and the legacy ` (repeated N times)` suffix otherwise.
- `DiagnosticDisposition.FETCH_REQUEST` is the new disposition.
- `DiagnosticCoalescer.admit` still accepts the external DTO, so the existing
  ordinary repeat/numeric-variant contracts are unchanged.

### Live presentation (`docker/versioning/host_presentation.py`)

- `HostPresentationState` retains the full `NetworkUrlDisplay` enum (it is no
  longer collapsed to a `show` boolean); `network_url_display` returns it.
- `admit_diagnostic` accepts `HostStructuredDiagnostic | HostDiagnosticEnvelope`
  and normalizes the former, preserving every existing direct caller.
- Recognized fetch grouping: interactive admission immediately replaces the
  mutable slot with the canonical line and then ` — N requests` for the
  admitted total; `lines` emits the first canonical line immediately and the
  counted form at the fixed one-second window or an earlier boundary.
- A recognized host-path fetch keeps no attachable hostnames for its group, so
  the canonical hostname/path line bypasses the legacy ` [hostname]` formatter.
  Ordinary diagnostics keep that attachment.
- `admit_diagnostic` enforces policy consistency at the internal boundary: a
  keyed envelope whose `fetch_key.display` differs from the state's
  `network_url_display` raises `ValueError` *before* any provisional,
  coalescer, renderer, or deadline state is touched, so a redacted state can
  never render host-path identity (or the reverse). Mismatches are rejected in
  both interactive and `lines` modes; matching envelopes, unkeyed ordinary
  diagnostics, and unkeyed exact envelopes are unaffected.
- `exact` bypasses the coalescer entirely: every admitted terminal-safe source
  line is written durably in both interactive and `lines` modes, and repeat /
  fetch / numeric-variant aggregation are disabled.

Policy behaviour:

| | group key | canonical live text | count suffix |
| --- | --- | --- | --- |
| `redacted` | method, exact status, attempt presence/value, cache outcome | `npm http fetch GET 200 <redacted> …`, latency omitted | ` — N requests` |
| `host-path` | above **plus** normalized hostname + canonical safe path | `npm http fetch GET 200 registry.npmjs.org/a/b …`, latency omitted | ` — N requests` |
| `exact` | none (bypassed) | terminal-safe source line | none |

Boundaries that finalize the current fetch group in order: any different
classification (warning/error/retry/timeout/status), a malformed ordinary
diagnostic, an omission notice, a lifecycle step/phase event, and a terminal
event.

## RED evidence

`tests/test_host_presentation_phase5.py` was added first and run against the
pre-implementation state (envelope type defined, grouping/exact not
implemented):

```
python3 -m unittest tests.test_host_presentation_phase5 -v
# Ran 24 tests
# FAILED (failures=20)
```

Representative failures:

- interactive redacted showed the local line and ` (repeated N times)` instead
  of ` — N requests`; the admitted total never appeared in the slot;
- host-path latency variants used the legacy ` [registry.npmjs.org]` form and
  never aggregated;
- boundary finalization emitted the ordinary repetition suffix;
- `lines` emitted ` (repeated 3 times)` rather than ` — 3 requests`;
- exact mode coalesced source lines and wrote to the mutable slot;
- `DiagnosticDisposition` had no `FETCH_REQUEST` member.

The four passing cases were structural assertions that were already true after
relocating the identity type (external `HostStructuredDiagnostic` field set,
envelope absence from `HostBuildEvent`, DTO attribute absence, and the parser
re-export identity).

## GREEN and INTROSPECT results

```
python3 -m unittest tests.test_host_presentation_phase5
# Ran 32 tests in 0.006s — OK
```

The 32 tests cover: immediate canonical first slot; in-place ` — N requests`
totals including the first request; no `observed` qualifier and no durable
duplicates; host-path latency aggregation; delimiter separation for
hostname/path, attempt, method, status, and cache; canonical text without
legacy bracketed hostnames; ordered boundary finalization for every closed
classification, malformed diagnostics, omitted notices, lifecycle and terminal
events; the `lines` first line and one-second counted summary with a fresh
group afterwards; exact preservation in interactive and `lines`; ordinary
exact-repeat and numeric-variant behavior in redacted and host-path; fetch
identity ignoring URL fingerprints; fetch counts excluding events dropped
before admission; and the external DTO/envelope audit.

Introspection audit:

- `HostStructuredDiagnostic` field set is unchanged and carries no fetch key,
  canonical text, safe path, or source text; the envelope is a distinct type,
  not a supertype.
- Fetch groups key on `FetchGroupKey`, not on URL fingerprints.
- A fetch count reflects only admitted envelopes: with two admitted fetches and
  a five-event omission notice the finalized total is `2 requests`, never `7`.
- No producer event dropped before admission can be counted, because the
  coalescer only ever sees admitted envelopes.
- `docker/versioning/fetch_identity.py` imports no host/parser/projector module,
  so no import cycle is introduced.

## VALIDATE

```
python3 -m unittest \
  tests.test_host_presentation_phase5 \
  tests.test_host_diagnostic_grouping_phase7 \
  tests.test_host_diagnostic_identity_phase7 \
  tests.test_host_presentation_phase9 \
  tests.test_npm_fetch_phase4 \
  tests.test_host_path_projection
# Ran 306 tests in 0.416s — OK

python3 -m unittest discover -s tests
# Ran 4281 tests in 51.373s — OK (skipped=13)

./scripts/check-types
# All checks passed!

git diff --check
# clean

openspec validate add-configurable-network-url-display --strict
# Change 'add-configurable-network-url-display' is valid
```

The 13 skips are the same environment-gated cases as before (8 need the
`scripts/validate-bound-pi-assembly-execution-phase6-*` harnesses, 3 need a
`docker-dev` account, 1 needs `sudo`/`runuser`, 1 needs `NPM_ENV_REAL_DOCKER=1`).

## Policy-consistency hardening

Fetch identity is policy-specific, so the presentation boundary now rejects a
mismatched envelope rather than silently aggregating or rendering it under the
wrong policy. The invariant is enforced at both layers:

- `HostDiagnosticEnvelope.__post_init__` rejects `fetch_key.display is
  NetworkUrlDisplay.EXACT` (exact mode must be unkeyed).
- `HostPresentationState.admit_diagnostic` raises `ValueError` when a keyed
  envelope's `display` differs from the state's `network_url_display`, checked
  before any provisional, coalescer, renderer, or deadline mutation.

Focused evidence:

```
python3 -m unittest tests.test_host_presentation_phase5
# Ran 39 tests in 0.006s — OK
```

The added cases cover: envelope rejection of an EXACT fetch key; exact-mode
envelopes staying unkeyed with source text as `presentation_text`;
redacted-state `ValueError` for a host-path envelope and host-path-state
`ValueError` for a redacted envelope (asserting no renderer calls, no pending
coalescer group, and no refresh/window deadline); `lines`-mode mismatch
rejection; matching redacted and host-path envelopes still accepted; and exact
envelopes remaining unkeyed and unaggregated.

Focused regression and static checks after the hardening:

```
python3 -m unittest \
  tests.test_host_presentation_phase5 \
  tests.test_host_diagnostic_grouping_phase7 \
  tests.test_host_diagnostic_identity_phase7 \
  tests.test_host_presentation_phase9 \
  tests.test_npm_fetch_phase4 \
  tests.test_host_path_projection
# Ran 313 tests in 0.416s — OK

python3 -m unittest discover -s tests
# Ran 4288 tests in 51.761s — OK (skipped=13)

./scripts/check-types
# All checks passed!

openspec validate add-configurable-network-url-display --strict
# Change 'add-configurable-network-url-display' is valid

git diff --check
# clean
```

## Lifecycle-boundary and exact-slot hardening

Two presentation-boundary gaps were closed in this turn.

**Lifecycle STARTED flush.** A recognized-fetch group must not span a
lifecycle boundary, or a later fetch would keep counting under the previous
phase/step. `observe_step` and `observe_phase` now call
`_finalize_pending_fetch_group()` at the start of their `STARTED` branch, which
flushes a pending *keyed* group (emitting its ordered summary and resetting the
refresh/window deadlines) before the lifecycle output. Ordinary diagnostic
groups are untouched, so their historical behavior is preserved.

Evidence (interactive and lines, redacted and host-path; two admitted fetches,
a `STARTED` event, then a third fetch):

- interactive step/phase start render `("finalize", "<canonical> — 2 requests")`
  immediately before the lifecycle `status`, and the next fetch re-opens the
  slot at one request;
- `lines` step/phase start render `("finalize", "<canonical> — 2 requests")`
  immediately before the lifecycle `durable` line, and a subsequent pair of
  fetches produces a fresh `— 2 requests` window summary;
- a pending ordinary repeat group is *not* flushed by a `STARTED` event.

**Exact interactive slot clearing.** `HostPresentationState.admit_diagnostic`
previously wrote an interactive exact-mode completed line with `durable()`,
which leaves any provisional prefix in the renderer slot for a later heartbeat
to redraw. Interactive exact lines are now finalized via
`_finalize_diagnostic(..., restore_status=self._status)`, which clears the slot,
writes the completed line once, and restores only a still-applicable status.
`lines` exact output stays durable and unaggregated.

Evidence with the real `TerminalHostRenderer`: a provisional prefix is
admitted, its completed exact-mode diagnostic is admitted, then a heartbeat is
sent. The completed line appears exactly once, `renderer._slot` is `None`, and
the heartbeat redraw does not contain the stale prefix.

```
python3 -B -m unittest \
  tests.test_host_presentation_phase5 \
  tests.test_host_presentation_phase9 \
  tests.test_host_diagnostic_grouping_phase7 \
  tests.test_host_diagnostic_identity_phase7 \
  tests.test_npm_fetch_phase4
# Ran 276 tests in 0.412s — OK

python3 -m unittest tests.test_host_presentation_phase5
# Ran 45 tests in 0.007s — OK

python3 -m unittest discover -s tests
# Ran 4294 tests in 52.128s — OK (skipped=13)

./scripts/check-types
# All checks passed!
```

The interactive exact-mode tests were updated to assert the finalized form
(the completed line reaches the terminal durably while the mutable slot is
cleared); `lines` exact tests remain durable-only.

## Out of scope

Phase 5 stops at the presentation actor and its internal envelope. Populating
the envelope from complete raw npm lines at the collection integration boundary
(task 7.7), the single mode-selected retained tail (Phase 6), and failure-report
host-path text remain open and are not claimed here. The internal envelope is
not yet part of the mailbox event union; that wiring belongs to Phase 7.

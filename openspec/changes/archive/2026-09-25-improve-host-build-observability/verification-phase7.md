# Phase 7 Verification — Diagnostic Identity and Numeric Coalescing

Change: `improve-host-build-observability`
Phase: 7 (tasks 7.1–7.9)
Date: 2026-09-20T06:53:14Z
Scope: ephemeral, ordered, session-keyed URL fingerprints; pure structured
diagnostic presentation identity; and the pure exact-repeat /
conservative-numeric-variant group state transitions consumed by the later
facade presentation worker.

## Deliverables

- `docker/versioning/diagnostic_identity.py` (new)
  - `SessionUrlIdentity` holds a fresh 32-byte key from
    `secrets.token_bytes` unless a test supplies one, and derives a
    fixed-width (64 hex character) HMAC-SHA256 fingerprint. The digest input
    is produced by the projector, so this class never parses a URL and accepts
    no output policy. `repr`/`str` reveal no key material.
  - `DiagnosticIdentity.__post_init__` applies the same shared
    `require_approved_url_fingerprints` contract, so both DTOs enforce exactly
    the same fingerprint shape.
  - `DiagnosticIdentity` carries phase, step, stream, classification, final
    safe rendered text, ordered URL fingerprints, and logical resource. It
    deliberately excludes normalized hostnames.
  - `identity_for(diagnostic)` projects a `HostStructuredDiagnostic` without
    copying `hostnames`.
  - `strict_numeric_spans` / `strict_numeric_tokens` extract valid standalone
    tokens: a maximal `[0-9.]+` run that fully matches `digits ("." digits)*`
    with neither adjacent boundary an ASCII letter, digit, underscore, dot,
    plus, or minus.
  - `numeric_template_match` requires equal token counts, byte-identical
    inter-token nonnumeric text, and exactly one changed token.
  - `compare_diagnostics` returns `DIFFERENT`, `EXACT_REPEAT`, or
    `NUMERIC_VARIANT` from the shared identity rules.
  - `DiagnosticCoalescer` / `AdmissionDecision` / `DiagnosticGroup` own the
    pure group transitions for both `PresentationMode.INTERACTIVE` and
    `PresentationMode.LINES`. No clock, mailbox, worker, timer, renderer, or
    npm policy lives here.
- `docker/versioning/host_progress.py`
  - `URL_FINGERPRINT_LENGTH`, `URL_FINGERPRINT_ALPHABET`, and the shared
    `require_approved_url_fingerprints` validator own the fixed-width
    lowercase-hexadecimal fingerprint contract at the structured DTO boundary.
  - `HostStructuredDiagnostic.url_fingerprints: tuple[str, ...]` is an ordered,
    multiplicity-preserving tuple of opaque fingerprints; the field is
    validated through the shared validator and defaults to empty.
- `docker/versioning/diagnostic_projection.py`
  - `_canonical_url_identity_literal` builds the digest input from the
    lowercased scheme, normalized hostname, path, and the port exactly when one
    was explicitly written (including an explicit default port). Userinfo,
    query, fragment, and all other authority detail are excluded, and an
    absent port is never synthesized.
  - `_canonical_url_identity` applies the same bounded percent-decoding passes
    used for URL detection.
  - `DiagnosticProjector(..., url_identity=...)`,
    `DiagnosticProjector.url_fingerprints`, and
    `project_structured_diagnostic(..., url_identity=...)` derive fingerprints
    only for complete, non-secret-host URLs; the retained-tail helper
    `sanitize_diagnostic_text` is unchanged and therefore produces no
    fingerprints.
- `tests/test_host_diagnostic_identity_phase7.py` (7.1–7.3)
- `tests/test_host_diagnostic_grouping_phase7.py` (7.4–7.5)

## RED evidence

Before implementation the focused tests failed for the intended missing
behavior:

```text
ImportError: Failed to import test module: test_host_diagnostic_identity_phase7
ModuleNotFoundError: No module named 'docker.versioning.diagnostic_identity'
ImportError: Failed to import test module: test_host_diagnostic_grouping_phase7
ModuleNotFoundError: No module named 'docker.versioning.diagnostic_identity'
```

## Green focused evidence

```bash
python -m unittest tests.test_host_diagnostic_identity_phase7 \
  tests.test_host_diagnostic_grouping_phase7
```

```text
............................................................................
----------------------------------------------------------------------
Ran 52 tests in 0.003s

OK
```

The 52 tests cover:

- URL identity: same-session equality, different paths, explicit-port
  presence and value (including an explicit default port), ordered
  multiplicity, removal of userinfo/query/fragment, and malformed candidates.
- Configured-proxy redaction: a text containing both a configured proxy
  endpoint and an unrelated target registry URL renders `proxy <redacted>
  fetched <redacted>`; the configured proxy is removed by prior secret
  redaction and produces neither a `proxy.internal` hostname fact nor a
  fingerprint, while the unrelated target URL still contributes the
  normalized `registry.npmjs.org` hostname and exactly one fingerprint equal
  to the target URL projected independently in the same session.
- Confidentiality: fixed-width opaque values, non-equality with unkeyed
  `md5`/`sha1`/`sha256`/`blake2b`/`blake2s` digests, fresh random key per
  session, explicit-key determinism, key-free `repr`, and absence of
  fingerprints from rendered text and retained tails.
- DTO fingerprint boundary: `HostStructuredDiagnostic` and
  `DiagnosticIdentity` both accept a 64-character lowercase-hexadecimal value
  and empty tuples, and both reject 63/65-character values, uppercase or
  non-hexadecimal characters, arbitrary text such as `SECRET URL HERE`, empty
  strings, non-string tuple members, and a list instead of a tuple. Invalid
  fingerprints are rejected rather than lowercased, truncated, padded, or
  normalized.
- Identity: every metadata component partitions groups, different hidden URL
  identity forms a different group, normalized hostnames are not a separate
  key, and hostnames never appear on the identity surface.
- Numeric boundaries: acceptance of `progress 1` → `progress 2` and
  `version 1.2.3` → `version 1.2.4`; rejection of `-1`, `+1`, `1.`, `.1`,
  `1..2`, `item1`, `1_2`, `1-2`, `1+2`; rejection of zero/multiple changed
  tokens and template drift; non-monotonic values accepted.
- Mode state: interactive numeric variants replace the mutable value, reset
  the exact-repeat count to one, omit the suffix until an exact repeat, and
  finalize only the latest value; `lines` emits every changed numeric value as
  a separate durable line while exact-repeat windows still summarize; retained
  tails remain ungrouped; the coalescer owns no worker/timer/renderer.

## Invariants recorded

- Fingerprints are ephemeral, fixed-width, and non-presentable: they never
  enter rendered text, retained tails, failure reports, persistence, evidence,
  or policy identity, and a fresh key per session prevents cross-session
  correlation. The structured DTO boundary validates every fingerprint as
  exactly 64 lowercase hexadecimal characters, so arbitrary or malformed data
  cannot reach the fingerprint surface.
- Fingerprint validation is never a normalization step: uppercase,
  wrong-length, and non-hexadecimal values are rejected rather than
  lowercased, truncated, or padded.
- Fingerprinting is keyed HMAC-SHA256, not an unkeyed fast/stable hash, so it
  is not an offline guessing oracle.
- An implicit default port is never inserted; only an explicitly written port
  (including an explicit default) participates.
- URL fingerprint order and multiplicity are preserved. Configured proxy
  endpoints are removed by prior secret redaction, so they produce neither
  hostname facts nor fingerprints; unrelated target URLs in the same
  diagnostic remain fingerprinted, and secret/proxy hosts match
  hostname-fact suppression.
- Grouping identity includes phase, step, stream, classification, logical
  resource, final safe rendered text, and the ordered fingerprint tuple, and
  excludes normalized hostnames.
- Numeric matching consumes the whole maximal numeric-looking run and never
  extracts a valid-looking substring from an invalid sequence.
- Exact repeats remain unchanged, interactive numeric variants replace only
  one mutable slot, `lines` retains each changed value, and retained tails
  remain ungrouped.
- The pure Phase 7 layer owns no activity state, so it cannot delay or reorder
  the diagnostic-activity observation that occurs before mailbox admission and
  presentation in the collector.

## Validation commands

```bash
python -m unittest tests.test_host_diagnostic_identity_phase7 \
  tests.test_host_diagnostic_grouping_phase7
python -m unittest \
  tests.test_host_diagnostic_identity_phase7 \
  tests.test_host_diagnostic_grouping_phase7 \
  tests.test_host_diagnostic_projection
python -m unittest tests.test_host_diagnostic_projection \
  tests.test_host_operational_events tests.test_host_download_observability
ty check docker --python-version 3.14 --output-format concise
python -m unittest discover -s tests -p 'test_*.py'
openspec validate improve-host-build-observability --strict
```

Results:

- Focused Phase 7 identity + grouping: `Ran 52 tests ... OK`.
- Focused identity + grouping + projection: `Ran 146 tests ... OK`.
- Adjacent Phase 1/2/5 suites: `Ran 133 tests ... OK`.
- `ty check`: `All checks passed!`.
- Full repository suite: `Ran 3855 tests in 40.832s` — `OK (skipped=13)`.
- `openspec validate ... --strict`: `Change 'improve-host-build-observability' is valid`.

## Scope confirmation

This is a Phase 7 identity-boundary correction: the structured DTO boundary now
validates that every `url_fingerprints` member is exactly 64 lowercase
hexadecimal characters through one shared validator reused by
`HostStructuredDiagnostic` and `DiagnosticIdentity`. HMAC-SHA256 fingerprint
generation, fingerprint order and multiplicity, empty fingerprint tuples, the
session-key lifecycle, exact-repeat and numeric-update grouping, hostname
handling, and retained-tail behavior are unchanged.

Phase 7 did not add a facade worker, timer, renderer, mailbox change, or any
production npm-policy change. The accepted Phase 6 `--loglevel=http` decision
remains unapplied to the production invocation until Phase 8 (task 8.10). No
Phase 8 task was modified or checked.

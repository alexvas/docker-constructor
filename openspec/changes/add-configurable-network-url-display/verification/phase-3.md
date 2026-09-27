# Phase 3 Verification Evidence

## 2026-09-27 (safe URL and host-path projection, tasks 3.1–3.8)

### Deliverable

The safe projector now derives one bounded internal `SafeHostPath`
presentation fact in addition to the existing URL-free text, normalized
hostnames, and ephemeral URL fingerprints. The fact is a frozen, slotted
value with a normalized `hostname` and a canonical safe encoded `path`, plus a
renderable `text` property. It is **local-presentation metadata only**: it is
absent from the external SDK DTO, verification/assembler evidence,
persistence, failure JSON, and every semantic identity.

```text
https://ExAmPlE.com:8443/A/b%2Fc?q=1#frag
  -> SafeHostPath(hostname="example.com", path="/A/b%2Fc")
  -> text = "example.com/A/b%2Fc"
```

### Implementation

`docker/versioning/diagnostic_projection.py`:

- `REDACTED_PATH_MARKER = "/<redacted-path>"` fixed fail-closed path.
- `SafeHostPath` frozen/slotted value: `hostname` (nonempty), `path`
  (absolute encoded path, or the marker), and `text = hostname + path`.
- `_candidate_url_layer` returns the first candidate layer (literal or
  bounded-percent-decoded) whose host normalizes safely; `_candidate_host`
  now delegates to it so the host/path fact reads the same encoded path that
  produced the hostname fact.
- `_normalize_percent_encoding` canonicalizes percent escapes: complete
  `%XX` only, RFC 3986 unreserved bytes decode to text, every other escape
  (including percent-encoded controls) stays encoded with uppercase hex, and
  a partial/invalid escape fails closed. It keeps the RFC 3986 `pchar` plus
  `/` set literal and UTF-8 percent-encodes every other character (see the
  terminal-safety correction below).
- `_percent_encode_utf8` encodes a raw character's UTF-8 bytes as uppercase
  `%XX` units, returning `None` for unencodable text (such as a lone
  surrogate) so the path fails closed.
- `_remove_dot_segments` applies RFC 3986 section 5.2.4 canonicalization.
- `_path_contains_secret` compares the original and each bounded
  percent-decoded path layer case-insensitively against every registered
  secret (mirroring hostname suppression) so a secret-bearing path is never
  disclosed.
- `_canonical_safe_path` rejects raw C0/DEL/C1 controls, incomplete/invalid
  escapes, and secret-bearing content; `_safe_host_path` then substitutes
  `REDACTED_PATH_MARKER` so a safe host remains visible when only its path is
  unsafe. Confidentiality is checked on the original path, each bounded
  percent-decoded layer, **and** the canonicalized result (see the
  confidentiality correction below).
- `DiagnosticProjector` gains `_host_paths`, a `host_paths` property, and
  `take_host_paths()`; `_sanitize_candidate` appends one deduplicated fact
  per safe URL when the host is not a registered secret.
- `sanitize_host_paths(text, secrets=())` pure helper mirrors
  `sanitize_diagnostic_text`.

`docker/versioning/npm_diagnostic_stream.py`: `_absorb_projector_facts` now
also drains `take_host_paths()` per line so the projector's per-line fact
buffer stays bounded during a long-running stream. The facts are discarded in
Phase 3 because binding the one selected local representation is Phase 6 work;
no `StreamChunk` or event field carries them.

### Terminal-safety correction: encode raw Unicode path text

**Defect found in review:** `_normalize_percent_encoding` copied every
non-`%` character verbatim, so a raw Unicode character in a path survived into
`SafeHostPath.path` and `.text`. A raw bidi control such as U+202E stayed
*active*, violating the safe encoded-path contract:

```text
input  = https://example.com/\u202eabc
before -> path = "/\u202eabc"   (active bidi control in rendered text)
after  -> path = "/%E2%80%AEabc"
```

**Fix:** `_normalize_percent_encoding` now keeps only the RFC 3986 `pchar`
plus `/` set (`_PATH_SAFE_ASCII`) literal, UTF-8 percent-encodes every other
ASCII byte and every non-ASCII character (including Unicode bidi, format, and
line/paragraph controls) with uppercase hex, and fails closed on unencodable
text. `_percent_encode_utf8` performs the byte-level encoding.
`SafeHostPath.__post_init__` additionally requires `path` to be ASCII with no
C0/DEL bytes, so an active Unicode control can never appear in `path` or
`text`. The existing raw C0/DEL/C1 rejection (``_canonical_safe_path``), the
percent-escape behavior, and both secret checks are retained.

**Regressions added:** `tests/test_host_path_projection.py::TestHostPathBoundaries`
now includes `test_raw_bidi_control_is_percent_encoded_not_active` (U+202E ->
`%E2%80%AE`), `test_raw_non_ascii_path_is_utf8_percent_encoded` (`/caf\u00e9` ->
`/caf%C3%A9`), `test_other_unicode_format_controls_are_encoded` (U+200B, U+200E,
U+2066, U+00AD, U+FEFF, U+2028),
`test_rendered_host_path_has_no_active_unicode_controls` (asserts ASCII and no
`unicodedata` `C*` category in rendered output), and
`test_safe_host_path_rejects_active_non_ascii_paths` (direct construction of a
non-ASCII or C0/DEL path raises `ValueError`).

### Confidentiality correction: recheck after canonicalization

**Defect found in review:** the original implementation checked secrets only on
the raw path and its bounded percent-decoded layers, *before* dot-segment
removal. Dot-segment removal can therefore **create** a secret that appeared in
neither layer:

```text
path    = /sec/x/../ret
secret  = sec/ret
original-path check        -> False
canonical (after removal)  = /sec/ret
canonical-path check       -> True   (the fix)
```

Before the fix, `sanitize_host_paths("https://example.com/sec/x/../ret",
secrets=("sec/ret",))` disclosed `/sec/ret`.

**Fix:** `_canonical_safe_path` keeps the original-path check (canonicalization
can equally **erase** a secret-bearing segment that must still redact, e.g.
`/sec/ret/x/..` -> `/sec/ret`) and now also runs
`_path_contains_secret(canonical, secrets)` after dot-segment removal and
leading-slash normalization, returning `None` on a match so `_safe_host_path`
substitutes `REDACTED_PATH_MARKER`.

**Regression added:** `tests/test_host_path_projection.py::TestHostPathBoundaries`
now includes `test_canonicalization_cannot_create_a_secret` (URL
`https://example.com/sec/x/../ret`, secret `sec/ret`; asserts the hostname stays
`example.com`, the path is `REDACTED_PATH_MARKER`, and the rendered text
contains neither the secret nor the canonical path) and
`test_canonicalization_cannot_erase_a_secret` (URL
`https://example.com/sec/ret/x/..`, secret `sec/ret`; asserts the retained
original-path check still redacts).

### Boundaries preserved

- Existing scheme, userinfo, explicit port, query, fragment, proxy detail,
  credential, and caller-secret exclusions still hold: the existing
  `tests/test_host_diagnostic_projection.py` and
  `tests/test_host_diagnostic_identity_phase7.py` regressions pass unchanged.
- URL fingerprint identity is unchanged and remains path-sensitive
  (`_canonical_url_identity_literal` still includes the raw path) and opaque:
  different paths produce different session fingerprints, and no plaintext
  path appears in a fingerprint.
- `HostStructuredDiagnostic`, `HostDiagnosticPrefix`, `HostFailureContext`,
  and `StreamChunk` have no `host_paths`/`host_path`/`path` field. The
  internal `docker.versioning.host_progress` module does not export
  `SafeHostPath`.
- Exception projection still emits only bounded exception type names and
  never an exception message.

### Tests

New `tests/test_host_path_projection.py` (38 tests):

- `TestSafeHostPathProjection` (3.1): normalized hostname plus canonical
  encoded path, scoped package path, order/dedup, IDN/IPv4/IPv6, authority-only
  root path, and non-URL text.
- `TestConfidentialityPreserved` (3.2): every URL component excluded from
  text and path, proxy/registered-host secrets suppress the fact, bare secret
  redaction.
- `TestHostPathBoundaries` (3.3): percent-encoded controls stay encoded and
  inert, raw bidi/format controls and raw non-ASCII text are UTF-8
  percent-encoded, rendered output stays ASCII with no active Unicode
  controls, unreserved decoding, dot-segment removal, malformed escape
  fallback, secret-bearing/encoded-secret path fallback, canonicalization
  cannot create or erase a secret, the 8 KiB token bound, incomplete/oversized
  tokens, and a path exactly at the bound.
- `TestHostPathApiShape` (3.4): no host/path field on the public DTOs, no
  host/path or policy input on the projection APIs, path-sensitive opaque
  fingerprints, no plaintext path in a fingerprint.
- `TestInternalOnlyBoundary` (3.7): the collector drains the facts per line,
  no chunk/event carries them, the host-event module does not export the fact,
  and exception projection emits only type names.

### Commands and results

RED (before implementation): the module failed to import because
`REDACTED_PATH_MARKER`, `SafeHostPath`, and `sanitize_host_paths` did not
exist.

```sh
python -m unittest tests.test_host_path_projection
# ImportError: cannot import name 'REDACTED_PATH_MARKER' ...
```

```sh
ty check docker --python-version 3.14 --output-format concise
./scripts/check-types
PYTHONDONTWRITEBYTECODE=1 python -m unittest \
  tests.test_host_path_projection \
  tests.test_host_diagnostic_projection \
  tests.test_host_diagnostic_identity_phase7 \
  tests.test_npm_diagnostic_collection_phase8
PYTHONDONTWRITEBYTECODE=1 python -m unittest \
  tests.test_host_path_projection \
  tests.test_host_diagnostic_projection \
  tests.test_host_diagnostic_identity_phase7 \
  tests.test_host_diagnostic_grouping_phase7 \
  tests.test_npm_diagnostic_collection_phase8 \
  tests.test_npm_environment_streaming \
  tests.test_host_download_observability \
  tests.test_host_operational_events \
  tests.test_host_failure_output_regression \
  tests.test_locked_assembly_observability_phase8 \
  tests.test_host_observability_acceptance_phase10 \
  tests.test_constructor_pi_assembly \
  tests.test_constructor_evidence_collector \
  tests.test_constructor_verification \
  tests.test_constructor_build_persistence
git diff --check
```

Results:

- `ty check docker --python-version 3.14 --output-format concise`: All checks
  passed.
- `./scripts/check-types` (`docker` + `tests/typing`): All checks passed.
- Requested focused suite (projector, identity, collection — earlier review):
  `Ran 203 tests in 1.390s` — `OK`.
- Requested focused suite (projector, identity — Unicode review):
  `Ran 163 tests in 0.568s` — `OK`.
- Focused URL sanitizer / projector / confidentiality / fingerprint /
  SDK-shape / evidence / persistence suite: `Ran 579 tests in 6.793s` — `OK`.
- Phase 3 suite alone: `Ran 38 tests in 0.008s` — `OK`.
- `git diff --check`: clean (exit 0).

Full discovery (`python -m unittest discover -s tests -p 'test_*.py'`):
`Ran 4151 tests in 53.134s` — `FAILED (failures=1, skipped=13)`.

The only failure is the same pre-existing, unrelated
`test_npm_environment_phase6_introspection.TestScenarioCoverage.test_every_spec_scenario_is_mapped_to_a_test`
scenario-coverage gap (54 canonical scenarios vs 36 coverage-map entries).
It is not part of Phase 3 and is intentionally left for the Phase 9
requirement-tracing task.

### Task status

Tasks 3.1–3.8 are complete. Binding the host/path fact into a mode-selected
live or retained representation, and rendering it in `host-path` mode, remains
Phase 6/7 work; Phase 3 delivers and bounds the projector fact and proves it
cannot cross a public, evidence, persistence, or semantic-identity boundary.

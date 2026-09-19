# Phase 2 Verification: Safe Diagnostic Projection

Change: `improve-host-build-observability`
Schema: `spec-driven`
Scope: tasks 2.1–2.8 only (depends on Phase 1)

## Deliverable summary

Phase 2 adds one pure, output-policy-independent projection layer in
`docker/versioning/diagnostic_projection.py`:

- `DiagnosticResourceKind` / `DiagnosticLogicalResource`: validated logical
  context accepting only the closed reviewed artifact names (`rustup`, `uv`,
  `rtk`, `fd`), the closed Pi release asset names (`SHA256SUMS`,
  `pi-coding-agent-install-package.json`,
  `pi-coding-agent-install-package-lock.json`), and the fixed
  `npm-assembler-<digest-prefix>` container form. Arbitrary resource labels are
  rejected before they can enter an event.
- `DiagnosticProjector`: incremental UTF-8 projector that applies the existing
  deterministic secret redaction, then identifies URL-shaped tokens across
  arbitrary decoder/chunk boundaries, removes each complete URL, and records
  only a separate normalized hostname fact. Scheme, port, userinfo, path,
  query, fragment, and registered-secret proxy details never enter text.
- Bounded pending sanitizer state: retained ambiguous URL/secret state never
  exceeds `PENDING_LIMIT_BYTES` (8 KiB). An oversized candidate emits exactly
  `[sanitized oversized token]` and its remainder is discarded through the next
  safe boundary before processing resumes. At EOF, reader failure, or
  cancellation every unresolved URL/secret candidate is replaced exactly with
  `[sanitized incomplete token]` and never flushed as ordinary text.
- `sanitize_diagnostic_text`: pure complete-text convenience wrapper.
- `project_structured_diagnostic`: produces `HostStructuredDiagnostic`
  (Phase 1 DTO) with URL-free text, normalized host facts, closed
  classification, and a validated logical resource. It accepts no output
  policy, so hostname display remains a facade-only decision.
- `project_exception_type_chain`: deterministic `reason` → `__cause__` →
  `__context__` traversal with object-identity cycle termination, duplicate
  class-name suppression, and at most four unique type names; exception
  messages are never evaluated.

No transport instrumentation, heartbeat scheduling, output configuration,
collector line assembly, or presentation worker is introduced (later phases).

## RED evidence (before GREEN)

Command:

```
python -m unittest tests.test_host_diagnostic_projection
```

Result: `FAILED (errors=1)` with
`ModuleNotFoundError: No module named 'docker.versioning.diagnostic_projection'`
— tasks 2.1–2.4 each bound a contract that the missing projector could not
satisfy.

## GREEN evidence (after implementation)

Command:

```
python -m unittest tests.test_host_diagnostic_projection -v
```

Result: `Ran 37 tests ... OK`.

Coverage mapped to the Phase 2 contract:

| Contract | Test |
| --- | --- |
| Closed reviewed-artifact / Pi-asset / assembler-container logical context; arbitrary labels rejected | `test_reviewed_artifact_names_are_the_closed_supported_set`, `test_pi_release_asset_names_are_the_closed_supported_set`, `test_pi_release_name_set_tracks_authoritative_module`, `test_assembler_container_requires_a_hex_digest_prefix`, `test_rejects_arbitrary_resource_labels`, `test_rejects_non_member_kind_and_non_string_name`, `test_projection_rejects_unvalidated_resource_labels`, `test_projection_carries_only_the_validated_resource_name` |
| URL-free text plus separately normalized host facts; no scheme/port/userinfo/path/query/fragment/proxy detail | `test_removes_every_url_component_and_separates_normalized_host`, `test_proxy_endpoint_is_removed_without_a_host_fact`, `test_malformed_candidates_are_fully_replaced_without_host_facts`, `test_normalizes_idn_ipv4_and_ipv6_hosts`, `test_plain_text_is_unchanged_and_bare_secret_is_redacted` |
| Output-policy independence | `test_projection_is_deterministic_and_has_no_output_policy_input` |
| Arbitrary chunking of URLs, split credentials, encoded forms, IDN across bytes | `test_url_split_across_many_small_chunks`, `test_split_credentials_and_encoded_url_forms`, `test_idn_host_split_across_bytes` |
| Fail-closed incomplete-candidate finalization at EOF/reader failure | `test_incomplete_url_candidate_finalizes_fail_closed`, `test_incomplete_secret_prefix_finalizes_fail_closed`, `test_reader_failure_finalizes_pending_candidate` |
| Bounded pending state; oversized-token marker, discard through boundary, recovery | `test_pending_state_remains_bounded_for_an_unterminated_token`, `test_oversized_token_discards_until_boundary_then_recovers`, `test_oversized_token_without_boundary_finalizes_without_incomplete_marker` |
| Adversarial: secret host, encoded delimiters, zone/percent hosts, non-http proxy schemes, secret-order determinism, lifecycle | `test_host_fact_is_withheld_when_the_host_is_a_registered_secret`, `test_encoded_delimiters_inside_a_url_are_removed`, `test_zone_scoped_and_percent_hosts_fail_closed`, `test_non_http_proxy_schemes_are_sanitized`, `test_secret_ordering_does_not_change_projection`, `test_finish_is_idempotent_and_feed_after_finish_is_rejected` |
| Property/fuzz-style every-boundary disclosure and bound check | `test_every_chunk_boundary_withholds_sensitive_tokens` |
| Deterministic bounded exception-type chain | `test_cause_is_preferred_over_context`, `test_context_is_traversed_when_no_cause_exists`, `test_relationship_cycle_terminates`, `test_duplicate_types_are_suppressed_and_four_unique_limit_applies`, `test_does_not_evaluate_exception_messages`, `test_none_reason_is_empty`, `test_rejects_non_exception_reason` |

## INTROSPECT result (task 2.7)

The phase diff and adversarial fixtures were reviewed for credentials, signed
queries, IPv4/IPv6, IDN, encoded delimiters, malformed/incomplete candidates,
proxy forms, cycles, exception `__str__` side effects, unbounded token
buffering, unresolved ordinary-text flushes, and failure to recover after a
safe boundary.

Findings:

- One GREEN defect was found and corrected before validation: the partial
  URL-scheme detector required a trailing colon, so a scheme split as
  `ht` + `tps://...` leaked the bare scheme prefix. The prefix pattern now
  allows the colon to be absent and withholds known-scheme prefixes at chunk
  boundaries.
- `test_every_chunk_boundary_withholds_sensitive_tokens` exercises sizes 1–7
  and confirms bounded pending state and zero disclosure of credentials, hosts,
  ports, paths, and queries at every boundary.
- Hosts that are themselves registered secrets (for example a configured proxy
  endpoint) are detected and withheld, so proxy hostnames never become host
  facts.
- Zone-scoped and percent-encoded hosts fail closed (fully replaced, no host
  fact) rather than partially exposing a malformed candidate.
- No remaining disclosure, bound violation, or nondeterminism was found. No
  transport instrumentation was added.

## Focused validation (task 2.8)

Command:

```
python -m unittest tests.test_host_diagnostic_projection \
  tests.test_npm_environment_streaming tests.test_host_operational_events \
  tests.test_constructor_host_progress tests.test_constructor_build_output \
  tests.test_constructor_pi_assembly
```

Result: `Ran 129 tests ... OK`.

Recorded guarantees: pending sanitizer state stays within 8 KiB
(`test_pending_state_remains_bounded_for_an_unterminated_token`,
`test_every_chunk_boundary_withholds_sensitive_tokens`); no event text contains
an original network identifier or unresolved candidate fragment
(`test_removes_every_url_component_and_separates_normalized_host`,
`test_every_chunk_boundary_withholds_sensitive_tokens`); every host fact is
normalized (`test_normalizes_idn_ipv4_and_ipv6_hosts`); terminal paths fail
closed (`test_incomplete_url_candidate_finalizes_fail_closed`,
`test_incomplete_secret_prefix_finalizes_fail_closed`,
`test_reader_failure_finalizes_pending_candidate`); processing recovers after a
safe boundary (`test_oversized_token_discards_until_boundary_then_recovers`);
and projection has no output-policy input
(`test_projection_is_deterministic_and_has_no_output_policy_input`).

## Typecheck

Command:

```
ty check docker --python-version 3.14 --output-format concise
```

Result: `All checks passed!`

## Full-suite result

```
python -m unittest discover -s tests -p 'test_*.py'
Ran 3637 tests in 28.789s
OK (skipped=13)
```

## Spec validation

```
openspec validate --changes "improve-host-build-observability" --json
```

Result: `improve-host-build-observability` → `valid: true`, `issues: []`. The
same run reports three pre-existing failures in unrelated active changes
(`add-locked-image-owned-pi-extensions`, `compose-parent-constructor-projects`,
`harden-runtime-artifact-installation`); none belongs to this change and Phase 2
does not modify them.

## Follow-up hardening (post-validation defects)

Two defects found by review after the initial Phase 2 validation were fixed
inside the same phase.

### Defect 1 — percent-encoded URL scheme separators passed through

An encoded URL such as `https%3A%2F%2Falice%3Asecret%40example.com:8443/path`
was not recognized by the literal-only `://` detector, so credentials,
hostname, port, and path were emitted unchanged despite the encoded-URL
contract.

Fix in `docker/versioning/diagnostic_projection.py`:

- `_URL_START_RE` now recognizes a scheme followed by `://` **or** its
  percent-encoded form (`%3A%2F%2F`), case-insensitive and arbitrarily mixed
  (`https%3A//`, `https:/%2F`, `https:%2F%2F`, ...).
- `_URL_PREFIX_RE` and the new `_is_url_prefix_token()` withhold partially
  delivered encoded separators (`https%`, `https%3`, `https%3A`, `https%3A%2`,
  ...) so a chunk boundary cannot leak a scheme or separator.
- `_candidate_host()` derives the host fact from the literal form first, then
  from a strict percent-decoding (`urllib.parse.unquote(..., errors="strict")`)
  only when the decoded form normalizes to a safe hostname; otherwise no host
  fact is emitted.
- Encoded URL forms are replaced as a single unsafe token with `<redacted>`.

Proof the new tests catch the pre-fix behavior: restoring the literal-only
detector produced
`'GET https%3A%2F%2Falice%3A<redacted>%40example.com:8443/path done'`
(hostname, port, and path leaked) versus `'GET <redacted> done'` now.

### Defect 2 — the 8 KiB pending bound could be exceeded transiently

`_feed` appended 4096-**character** slices before draining, so a multibyte
slice could reach 16 KiB of retained UTF-8 state, violating the strict
`PENDING_LIMIT_BYTES` bound.

Fix in `docker/versioning/diagnostic_projection.py`:

- `_FEED_SLICE_CHARS` is replaced by `_FEED_SLICE_BYTES`; `_feed` computes the
  remaining room and appends only a UTF-8-byte-bounded slice
  (`_take_utf8_prefix`), so pending state never exceeds 8 KiB.
- When the next character cannot fit, `_begin_overflow` emits
  `OVERSIZED_TOKEN_MARKER`, clears the buffer, and discards through the next
  safe boundary before processing resumes — preserving the existing oversized
  marker and recovery behavior.
- The redundant `> PENDING_LIMIT_BYTES` checks inside `_drain` were removed
  because the bound is now enforced at the append site.

Proof the new tests catch the pre-fix behavior: a probe recording retained
bytes at every drain entry observed a maximum of 65556 bytes with the old
character-sliced feed versus `<= 8192` now.

### New tests (`tests/test_host_diagnostic_projection.py`)

| Contract | Test |
| --- | --- |
| Fully encoded, uppercase, and mixed literal/encoded separators fully redacted with no credential/host/port/path/query leak | `test_encoded_scheme_separators_are_fully_redacted` |
| Encoded forms split across byte/chunk boundaries | `test_encoded_scheme_separators_split_across_byte_chunks`, `test_encoded_scheme_separator_split_by_characters` |
| Fully encoded authority decoded to the normalized host fact | `test_fully_encoded_authority_is_decoded_for_the_host_fact` |
| Undecodable/unsafe encoded authority omits the host fact | `test_encoded_url_without_decodable_host_omits_the_host_fact` |
| A candidate longer than the bound is replaced, not partially sanitized | `test_candidate_cannot_be_retained_past_the_byte_limit` |
| Multibyte unterminated candidate stays within the bound | `test_multibyte_unterminated_candidate_stays_within_the_limit` |
| Bound enforced before appending (drain-entry probe) | `test_multibyte_append_never_exceeds_the_limit_at_drain_entry` |

### Follow-up validation

```
python -m unittest tests.test_host_diagnostic_projection
Ran 45 tests ... OK

python -m unittest tests.test_host_diagnostic_projection \
  tests.test_npm_environment_streaming tests.test_host_operational_events \
  tests.test_constructor_host_progress tests.test_constructor_build_output \
  tests.test_constructor_pi_assembly
Ran 137 tests ... OK

ty check docker --python-version 3.14 --output-format concise
All checks passed!

python -m unittest discover -s tests -p 'test_*.py'
Ran 3645 tests ... OK (skipped=13)
```

## Follow-up hardening 2 — percent-encoded scheme characters

A further defect was found by review: the URL detector required literal scheme
characters, so a scheme with one or more percent-encoded characters passed
through unchanged (`%68%74%74%70%73%3A%2F%2Falice%3Asecret%40example.com...`
leaked credentials, hostname, port, and path) or leaked a prefix
(`h%74tps%3A...` emitted `h%74` and mis-attributed the host to a truncated
scheme such as `tps`).

### Fix in `docker/versioning/diagnostic_projection.py`

- `_SCHEME_PATTERN` now accepts literal scheme characters **or** `%XX` units
  (`_SCHEME_FIRST_UNIT`, `_SCHEME_UNIT`), so `_URL_START_RE` recognizes schemes
  such as `h%74tps`, `%68%74%74%70%73`, and mixed forms.
- `_is_url_prefix_token()` is now an explicit unit-by-unit scanner instead of a
  regex plus literal comparison. It decodes `%XX` scheme units; an encoded
  `:`/`/` (that is `%3A`/`%2F`) marks the separator, and a bare scheme is held
  only while its decoded text (or an incomplete trailing escape) still matches
  a known scheme. This withholds `h`, `h%`, `h%7`, `h%74`, `h%74tps`,
  `h%74tps%3A`, `%68`, `%68%74`, ... at chunk boundaries.
- The whole encoded URL token is replaced with `<redacted>`; `_candidate_host()`
  still emits a host fact only when the decoded URL normalizes safely.

### New tests (`tests/test_host_diagnostic_projection.py`)

| Contract | Test |
| --- | --- |
| Fully encoded, mixed literal/encoded, and partially encoded schemes fully redacted with no credential/host/port/path/query/fragment leak | `test_percent_encoded_scheme_characters_are_fully_redacted` |
| Encoded scheme split across byte and small-chunk boundaries | `test_encoded_scheme_split_across_byte_chunks` |
| Encoded scheme split character by character | `test_encoded_scheme_split_by_characters` |

### Pre-fix behavior reproduced

With the previous literal-scheme detector restored:

- `GET h%74tps%3A%2F%2Falice%3Asecret%40example.com:8443/path done`
  → `GET h%74<redacted> done` (leaked `h%74` and mis-attributed host `tps`).
- `GET %68%74%74%70%73%3A%2F%2Falice%3Asecret%40example.com:8443/path done`
  → leaked the encoded scheme, hostname, port, and path.

Both now produce `GET <redacted> done` with host fact `example.com`.

### Updated validation

```
python -m unittest tests.test_host_diagnostic_projection
Ran 48 tests ... OK

python -m unittest tests.test_host_diagnostic_projection \
  tests.test_npm_environment_streaming tests.test_host_operational_events \
  tests.test_constructor_host_progress tests.test_constructor_build_output \
  tests.test_constructor_pi_assembly
Ran 140 tests ... OK

ty check docker --python-version 3.14 --output-format concise
All checks passed!

python -m unittest discover -s tests -p 'test_*.py'
Ran 3648 tests ... OK (skipped=13)
```

A randomized chunk fuzz additionally confirmed encoded-scheme forms never leak
credentials, hostnames, ports, paths, or queries across random boundaries and
that pending state stays within `PENDING_LIMIT_BYTES`.

## Follow-up hardening 3 — nested percent-encoding

Another disclosure was found by review: URL detection decoded at most one
percent-encoding layer. A double-encoded URL such as
`https%253A%252F%252Fexample.com/private` therefore passed through unchanged
with its hostname and path intact, and a literal URL later in the buffer could
mask a nested-encoded one earlier in it.

### Fix in `docker/versioning/diagnostic_projection.py`

- New `URL_DECODE_LAYER_LIMIT = 3`: percent-decoding is applied in bounded
  repeated passes, stopping early when a pass no longer changes the text or
  when the limit is reached. Double-encoded forms need two passes; the third
  keeps a small margin.
- New `_decode_percent_layer()` performs one total, non-raising decoding pass
  and returns, for every decoded character, the raw index it came from. This
  is what keeps nested detection boundary-safe: a decoded match can be mapped
  back to the exact raw offset to redact from.
- `_find_url_start()` replaces the direct `_URL_START_RE.search()` in
  `_drain()`. It searches every decoding layer and takes the **smallest raw
  index**, so a nested-encoded URL earlier in the buffer cannot be masked by a
  literal URL later in it. The whole original token from that raw index is
  replaced with `<redacted>`.
- `_is_url_prefix_token()` now returns true when **any** decoding layer of the
  trailing token could become a URL start, so a double-encoded candidate split
  across chunks is withheld until its terminator arrives.
- `_candidate_host()` decodes in the same bounded repeated passes and emits a
  host fact only for the first layer that normalizes safely, so hostname
  extraction stays limited to a safely normalized decoded URL.

### New tests (`tests/test_host_diagnostic_projection.py`)

| Contract | Test |
| --- | --- |
| Double-encoded separator (`https%253A%252F%252Fexample.com/private`) fully redacted | `test_double_encoded_separator_is_fully_redacted` |
| Fully double-encoded scheme and authority (credentials, encoded dot, port, path, query, fragment) redacted | `test_fully_double_encoded_scheme_and_authority_are_redacted` |
| Double-encoded delimiters split across byte and small-chunk boundaries | `test_nested_encoded_delimiters_split_across_byte_chunks` |
| Double-encoded delimiters split character by character | `test_nested_encoded_delimiters_split_by_characters` |
| Decoding reaches exactly `URL_DECODE_LAYER_LIMIT` and then stops | `test_nested_decoding_is_bounded_to_the_layer_limit` |

Each case asserts credentials, hostname, path, port, query, and fragment are
absent from the emitted text, and that pending state stays within
`PENDING_LIMIT_BYTES`.

### Pre-fix behavior reproduced

With layered decoding disabled, `GET https%253A%252F%252Fexample.com/private
done` produced `GET https%253A%252F%252Fexample.com/private done` with no host
fact — a full hostname and path disclosure. It now produces
`GET <redacted> done` with host fact `example.com`.

### Updated validation

```
python -m unittest tests.test_host_diagnostic_projection
Ran 53 tests ... OK

python -m unittest tests.test_host_diagnostic_projection \
  tests.test_npm_environment_streaming tests.test_host_operational_events \
  tests.test_constructor_host_progress tests.test_constructor_build_output \
  tests.test_constructor_pi_assembly
Ran 145 tests ... OK

ty check docker --python-version 3.14 --output-format concise
All checks passed!

python -m unittest discover -s tests -p 'test_*.py'
Ran 3653 tests ... OK (skipped=13)
```

A randomized nested-encoding chunk fuzz additionally confirmed that
double- and triple-encoded forms never leak credentials, hostnames, ports,
paths, or queries across random boundaries and that pending state stays within
`PENDING_LIMIT_BYTES`. One earlier full-suite run reported a single failure in
an unrelated test; two consecutive re-runs were clean, so it was flaky and not
caused by this change.

## Follow-up hardening 4 — exception-type chain cap enforced for every caller

`project_exception_type_chain(reason, *, limit=EXCEPTION_TYPE_LIMIT)` accepted
any positive integer `limit`, so a caller could request `limit=5` and receive
five unique type names — violating the "at most four unique names" contract
fixed by the `docker-build-output` specification.

### Fix in `docker/versioning/diagnostic_projection.py`

`limit` is retained so callers may still ask for a *shorter* chain, but it can
no longer raise the cap. Values above `EXCEPTION_TYPE_LIMIT` are now rejected
with `ValueError`, alongside the existing non-integer, boolean, zero, and
negative validation. The documented contract is unchanged: the returned chain
is always at most `EXCEPTION_TYPE_LIMIT` (four) unique type names.

### New tests (`tests/test_host_diagnostic_projection.py`)

| Contract | Test |
| --- | --- |
| A five-type chain requested with `limit=EXCEPTION_TYPE_LIMIT + 1` or `limit=5` raises `ValueError`; `limit=EXCEPTION_TYPE_LIMIT` still yields four names and `limit=2` yields two | `test_requested_limit_cannot_exceed_the_four_type_cap` |
| `0`, `-1`, `2.5`, and `True` limits still raise `ValueError` | `test_rejects_invalid_limit_values` |

### Behavior reproduced

With a five-distinct-type chain (`_MessageBombError → KeyError → OSError →
TypeError → RuntimeError`):

```
limit=1 -> ('_MessageBombError',)
limit=2 -> ('_MessageBombError', 'KeyError')
limit=3 -> ('_MessageBombError', 'KeyError', 'OSError')
limit=4 -> ('_MessageBombError', 'KeyError', 'OSError', 'TypeError')
limit=5 -> ValueError: limit must be a positive integer no greater than EXCEPTION_TYPE_LIMIT (4)
```

Before this change the `limit=5` call returned all five names.

### Updated validation

```
python -m unittest tests.test_host_diagnostic_projection
Ran 55 tests ... OK

focused diagnostic-projection set
Ran 147 tests ... OK

ty check docker --python-version 3.14 --output-format concise
All checks passed!

python -m unittest discover -s tests -p 'test_*.py'
Ran 3655 tests ... OK (skipped=13)
```

## Follow-up hardening 5 — arbitrary scheme prefixes held across boundaries

Another disclosure was found by review: the boundary hold only withheld bare
schemes that matched the fixed `_KNOWN_SCHEMES` list (`http`, `https`, `ftp`,
`socks5`, `socks5h`). A URL using any other valid scheme, split so that the
scheme itself straddled a chunk boundary, leaked the leading fragment. Feeding
`["gi", "t://user@secret.example/path "]` emitted `gi<redacted> ` — the
fragment `gi` of the `git` scheme entered the output, violating arbitrary-split
URL handling even though `_URL_START_RE` already recognizes any scheme.

### Fix in `docker/versioning/diagnostic_projection.py`

- Removed `_KNOWN_SCHEMES` and `_is_known_scheme_prefix`; prefix handling now
  follows the same rule as URL detection.
- `_url_prefix_state_single()` replaces the boolean scanner and classifies a
  token as one of `_URL_PREFIX_NONE`, `_URL_PREFIX_SCHEME`, or
  `_URL_PREFIX_SEPARATOR`. Any syntactically valid RFC 3986-style scheme is
  accepted — a letter followed by letters, digits, `+`, `.`, `-`, or their
  percent-encoded equivalents — so `git`, `custom+transport`, and future
  schemes are withheld exactly like `https`.
- `_url_prefix_state()` applies that classification across the bounded
  decoding layers; `_is_url_prefix_token()` is now a thin wrapper.
- `_ambiguous_hold()` withholds any viable scheme prefix until a safe boundary
  or an invalid scheme character disambiguates it.
- `_unresolved_candidate()` separates the two end-of-stream outcomes. A
  detected URL, a scheme whose separator has begun (`git:`, `git/`,
  `git%3A`), or a trailing partial secret fails closed with
  `INCOMPLETE_TOKEN_MARKER`. A bare scheme-like suffix (`git`,
  `custom+transport`, `status`) never became a URL, so it is flushed
  unchanged — ordinary unterminated text no longer becomes an incomplete
  token at EOF.

Pending state stays within 8 KiB, oversized candidates still emit
`OVERSIZED_TOKEN_MARKER`, complete URLs are still fully replaced, and
processing still resumes after a safe boundary.

### New tests (`tests/test_host_diagnostic_projection.py`)

| Contract | Test |
| --- | --- |
| `git://`, `custom+transport://` (with credentials, host, port, path, query, fragment) withheld at every two-part boundary | `test_non_listed_schemes_are_withheld_at_every_boundary` |
| `custom+transport://` split across single bytes | `test_non_listed_scheme_split_across_single_bytes` |
| The reported `["gi", "t://…"]` split leaks no scheme fragment, credentials, host, or path | `test_scheme_fragment_does_not_leak_across_a_chunk_boundary` |
| Non-listed-scheme URLs fully replaced and processing resumes after boundaries | `test_non_listed_scheme_urls_are_replaced_and_processing_resumes` |
| `git`, `custom`, `status`, `custom+transport` emitted unchanged when disambiguated mid-stream | `test_scheme_like_words_are_emitted_unchanged` |
| EOF flushes bare words but marks `git:`, `git/`, `git%3A`, `git://`, `git://host`, and partial secrets | `test_eof_separates_ordinary_words_from_unresolved_candidates` |

### Pre-fix behavior reproduced

With the fixed known-scheme list restored for the hold decision:

```
pre-fix: 'gi<redacted> ' ('secret.example',)
fixed  : '<redacted> '  ('secret.example',)
```

### Updated validation

```
python -m unittest tests.test_host_diagnostic_projection -v
Ran 61 tests ... OK

focused diagnostic-projection set
Ran 153 tests ... OK

ty check docker --python-version 3.14 --output-format concise
All checks passed!

python -m unittest discover -s tests -p 'test_*.py'
Ran 3661 tests ... OK (skipped=13)
```

## Follow-up hardening 6 — abort/cancellation fails closed for ambiguous scheme prefixes

The previous change made `finish()` flush a bare scheme-like suffix as ordinary
text, but it did so for both the clean-EOF and `abort=True` paths, silently
ignoring the `abort` argument. On reader failure or cancellation a truncated
URL prefix such as `git`, `custom+transport`, `%`, `%6`, `%67`, `%67it`, or
`git%3` was therefore emitted verbatim instead of failing closed.

### Fix in `docker/versioning/diagnostic_projection.py`

- `_unresolved_candidate()` now takes `abort`:
  - a detected URL or a trailing partial secret always fails closed;
  - with `abort=True` both `_URL_PREFIX_SCHEME` and `_URL_PREFIX_SEPARATOR` are
    unresolved, so every still-ambiguous literal or percent-encoded scheme
    prefix fails closed;
  - at clean EOF only `_URL_PREFIX_SEPARATOR` is unresolved, so a bare
    scheme-like word is still flushed unchanged.
- `finish()` forwards its `abort` argument instead of ignoring it. Complete
  URLs are still fully replaced, partial secrets still produce
  `INCOMPLETE_TOKEN_MARKER`, and overflow/8 KiB-bound behavior is unchanged.
- Docstrings now state that bare scheme-like text is flushed only at clean EOF
  and that reader failure or cancellation fails closed for every ambiguous URL
  or encoded-scheme prefix.

### New tests (`tests/test_host_diagnostic_projection.py`)

| Contract | Test |
| --- | --- |
| Clean EOF preserves `git` and `custom+transport` unchanged | `test_clean_eof_preserves_bare_scheme_like_words` |
| `finish(abort=True)` replaces `git`, `custom+transport`, `git:`, `git:/` with exactly the marker | `test_abort_fails_closed_for_ambiguous_literal_prefixes` |
| `finish(abort=True)` replaces `%`, `%6`, `%67`, `%67it`, `git%3` with exactly the marker | `test_abort_fails_closed_for_ambiguous_encoded_prefixes` |
| Abort still replaces a complete URL and records its host | `test_abort_still_replaces_complete_urls` |

Each abort case feeds the prefix one character at a time, asserts the emitted
text is exactly `INCOMPLETE_TOKEN_MARKER`, and asserts no candidate fragment
survives.

### Pre-fix behavior reproduced

```
pre-fix abort: {'git': 'git', 'custom+transport': 'custom+transport',
                'git:': '[sanitized incomplete token]',
                'git:/': '[sanitized incomplete token]'}
pre-fix abort encoded: {'%': '%', '%6': '%6', '%67': '%67',
                        '%67it': '%67it', 'git%3': 'git%3'}
fixed   abort: every case -> '[sanitized incomplete token]'
```

### Updated validation

```
python -m unittest tests.test_host_diagnostic_projection -v
Ran 65 tests ... OK

focused diagnostic-projection set
Ran 157 tests ... OK

ty check docker --python-version 3.14 --output-format concise
All checks passed!

python -m unittest discover -s tests -p 'test_*.py'
Ran 3665 tests ... OK (skipped=13)
```

## Follow-up hardening 7 — canonical hostname comparison for secret suppression

`_host_contains_secret()` compared the already-normalized projected hostname
against the **raw** registered secret text case-sensitively:

```python
host in secret or secret in host
```

A registered secret such as `https://PROXY.EXAMPLE:8080` was therefore still
redacted from text, but its host fact `proxy.example` was emitted, exposing
proxy details.

### Fix in `docker/versioning/diagnostic_projection.py`

- Added `_normalize_hostname_text()`: lowercase, drop a trailing dot, convert
  Unicode domain names to IDNA ASCII, and reject anything that is not a safe
  hostname. `_normalized_host()` now delegates to it, so a projected hostname
  and every hostname-like secret value use the same rules.
- Added `_secret_canonical_hosts(secret)`: normalizes each secret both as a URL
  (`https://PROXY.EXAMPLE:8080/path`) and, when it carries no `://`, as an
  authority-relative reference (`PROXY.EXAMPLE`, `PROXY.EXAMPLE:8080`,
  `user:pass@PROXY.EXAMPLE:8080`, `PROXY.EXAMPLE.`). Scheme, credentials, port,
  path, query, and fragment are discarded before comparison.
- Rewrote `_host_contains_secret()`: exact canonical-hostname equality is the
  primary test, and a conservative case-insensitive containment fallback still
  covers secrets that cannot be parsed safely (or embed a host inside a path or
  query), so a matching host fact can never be exposed. The raw case-sensitive
  comparison is gone.
- Only hostname-fact suppression uses canonical comparison. Text redaction
  remains deterministic and case-sensitive; URL removal, pending-state limits,
  and overflow handling are unchanged.

### New tests (`tests/test_host_diagnostic_projection.py`)

`TestCanonicalHostSuppression` covers, with `"retry <redacted> next"`, empty
`hostnames`, and no leaked token assertions:

| Case | Test |
| --- | --- |
| Uppercase proxy URL secret | `test_uppercase_proxy_url_secret_suppresses_the_host_fact` |
| Uppercase bare hostname secret | `test_uppercase_bare_hostname_secret_suppresses_the_host_fact` |
| Hostname with a trailing dot | `test_trailing_dot_hostname_secret_suppresses_the_host_fact` |
| IDN secret vs. normalized punycode hostname (both directions) | `test_idn_secret_matches_its_normalized_punycode_hostname` |
| Proxy URL with user information and a port | `test_proxy_url_with_user_information_and_port_suppresses_the_host_fact` |
| Positive control: unrelated source URL still records its host | `test_unrelated_source_url_still_records_its_hostname` |

### Pre-fix behavior reproduced

Running the new class against the old comparison fails 5 of 6 tests, each with
e.g. `AssertionError: Tuples differ: () != ('proxy.example',)`; the positive
control passes before and after.

### Updated validation

```
python -m unittest tests.test_host_diagnostic_projection -v
Ran 71 tests ... OK

focused diagnostic-projection set
Ran 163 tests ... OK

ty check docker --python-version 3.14 --output-format concise
All checks passed!

python -m unittest discover -s tests -p 'test_*.py'
Ran 3671 tests ... OK (skipped=13)
```

## Follow-up hardening 8 — bounded percent decoding for secret host suppression

`_secret_canonical_hosts()` canonicalized only the **original** secret string,
so a percent-encoded proxy secret was redacted from text but still emitted its
decoded host fact:

```
secret 'https://b%C3%BCcher.example:8080' -> canon=[]  hosts=('xn--bcher-kva.example',)
secret 'https://%70roxy.example:8080'     -> canon=[]  hosts=('proxy.example',)
secret '%70roxy.example'                  -> canon=[]  hosts=('proxy.example',)
```

### Fix in `docker/versioning/diagnostic_projection.py`

- Added `_canonical_hosts_in_text(value)`: canonicalizes one layer by parsing it
  both as a full URL and, when it carries no `://`, as an authority-relative
  reference (bare host, credentials, port, trailing dot). Every extracted host
  goes through `_normalize_hostname_text()`.
- Rewrote `_secret_canonical_hosts(secret)`: it now canonicalizes the original
  value **and each bounded percent-decoded layer**, reusing `_decode_percent()`
  and the same `URL_DECODE_LAYER_LIMIT` bound as diagnostic URL candidates.
  Decoding stops on failure, on no change, or at the layer limit, so it is
  never unbounded.
- `_host_contains_secret()` keeps exact canonical-host equality as the primary
  test and retains the conservative case-insensitive containment fallback for
  secrets that yield no canonical host (or embed a host inside a path or
  query). Text redaction stays deterministic and case-sensitive; URL removal,
  pending limits, and overflow handling are unchanged.
- Module and function docstrings now state that secret-host suppression uses
  bounded percent decoding and the same hostname normalization as diagnostic
  candidates.

### New tests (`tests/test_host_diagnostic_projection.py`)

| Case | Test |
| --- | --- |
| Percent-encoded ASCII host, both directions | `test_percent_encoded_ascii_hostname_secret_suppresses_the_host_fact` |
| Percent-encoded Unicode/IDN host, both directions | `test_percent_encoded_idn_hostname_secret_suppresses_the_host_fact` |
| Fully and nested encoded URL secrets (incl. nested IDN) | `test_fully_and_nested_encoded_url_secrets_suppress_the_host_fact` |
| Encoded bare-host secret, with and without a trailing dot | `test_encoded_bare_host_secret_suppresses_the_host_fact` |
| Positive control with encoded secrets registered | `test_unrelated_source_url_still_records_its_hostname_with_encoded_secrets` |

Every suppression case asserts the emitted text is exactly
`"retry <redacted> next"`, `hostnames == ()`, and that no encoded, decoded,
Unicode, or punycode host form appears in the emitted text.

### Pre-fix behavior reproduced

Running `TestCanonicalHostSuppression` against the single-layer
`_secret_canonical_hosts()` fails 4 of 11 tests, each with
`AssertionError: Tuples differ: () != ('proxy.example',)` or
`() != ('xn--bcher-kva.example',)`; the positive controls pass before and after.

### Updated validation

```
python -m unittest tests.test_host_diagnostic_projection -v
Ran 76 tests ... OK

focused diagnostic-projection set
Ran 168 tests ... OK

ty check docker --python-version 3.14 --output-format concise
All checks passed!

python -m unittest discover -s tests -p 'test_*.py'
Ran 3676 tests ... OK (skipped=13)
```

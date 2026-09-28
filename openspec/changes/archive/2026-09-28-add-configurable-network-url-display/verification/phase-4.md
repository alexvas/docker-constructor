# Phase 4 verification — successful npm fetch parsing and identity

Change: `add-configurable-network-url-display` (schema `spec-driven`).
Scope: tasks 4.1–4.7 and their parser hardening. This evidence file is
separate from the specification artifacts.

## Deliverable

New pure module `docker/versioning/npm_fetch.py`:

- `NPM_FETCH_PREFIX = "npm http fetch"`.
- `NpmFetchRecord(method, status, url, latency_ms, attempt, cache_outcome)` —
  frozen/slotted, validated on construction (uppercase method token, exact 2xx
  status, non-empty URL, non-negative integer latency, optional non-negative
  attempt, optional cache outcome).
- `parse_npm_fetch_line(text) -> NpmFetchRecord | None` — anchored
  (`fullmatch`) recognition and consumption of the complete grammar
  `npm http fetch <METHOD> <STATUS> <URL> <ASCII digits>ms [attempt #<ASCII
  digits>] [(cache <OUTCOME>)]`. At most one trailing newline is tolerated.
  The URL field must be a *complete* URL: literal C0 controls and DEL are
  rejected outright, scheme, non-empty authority, and an absent or in-range port
  are checked locally, and hostname acceptance delegates to the shared projector
  helper `normalized_url_host`; malformed URL-shaped fields are rejected as
  ordinary diagnostics. Latency and attempt
  fields are bounded: a digit run whose significant digits exceed
  `_MAX_NUMERIC_DIGITS = 18` is rejected conservatively instead of being
  converted.
- `FetchGroupKey(display, method, status, attempt, cache_outcome, host_path)` —
  frozen/slotted, hashable request-count group identity.
- `fetch_group_identity(record, display, *, secrets=()) -> FetchGroupKey | None`.
- `canonical_fetch_text(record, display, *, secrets=()) -> str | None`.

Policy behaviour:

| | group key | canonical text |
| --- | --- | --- |
| `redacted` | method, exact status, attempt presence/value, cache outcome | `npm http fetch GET 200 <redacted> …`, latency omitted |
| `host-path` | above **plus** normalized hostname + canonical safe path | `npm http fetch GET 200 registry.npmjs.org/a/b …`, latency omitted |
| `exact` | `None` (aggregation bypassed) | `None` |

`host-path` derives its fact from the shared Phase 3 projector
(`sanitize_host_paths`), so credentials, explicit port, query, fragment, and
scheme are excluded, percent-encoded/unreserved and dot-segment forms are
canonicalized, raw non-ASCII is percent-encoded, unsafe paths fail closed to
`/<redacted-path>`, and secret-bearing hosts yield no fact (canonical text then
falls back to `<redacted>`). `FetchGroupKey` stores no raw URL, so `repr()`
cannot disclose one.

## RED evidence

`tests/test_npm_fetch_phase4.py` was added before the module existed:

```text
ModuleNotFoundError: No module named 'docker.versioning.npm_fetch'
Ran 1 test in 0.000s — FAILED (errors=1)
```

## GREEN evidence

```sh
python -m unittest -v tests.test_npm_fetch_phase4
# Ran 95 tests in 0.067s — OK
```

## Post-validation hardening (amends 4.4/4.5)

Three correctness gaps were closed after the first validation pass:

- **Malformed URL fields.** The line grammar matched any `scheme://<nonspace>`
  field, so `https://%`, `https://[`, `https://?x`, `https:///x`, and
  `http://:80/x` were accepted as records and could receive a fetch-group
  identity. `_is_complete_url` now requires only the structural facts -- a
  valid scheme, a non-empty authority, and an absent or in-range port -- and
  `urlsplit`'s `ValueError` for an invalid IPv6 literal or out-of-range port is
  contained inside the validator.
- **Shared host rule.** The first hardening pass kept a narrow local
  `_LABEL_RE`/`_is_valid_hostname`/`ipaddress` grammar, which *rejected* host
  forms the Phase 3 projector normalizes (percent-encoded hosts, raw IDN hosts,
  underscore-bearing reg-names, empty labels). That local grammar is deleted.
  `normalized_url_host` is now the single shared authority/hostname rule in
  `diagnostic_projection.py`, reusing the projector's candidate-layer percent
  decoding and hostname normalization, and `_is_complete_url` accepts a URL
  exactly when that helper returns a hostname. Parser recognition and host/path
  projection can no longer disagree about which hosts are valid. The helper
  also runs the projector's bounded percent-decoding first and applies the
  `_AUTHORITY_RE` authority validation and the `urlsplit(...)` `ValueError`
  port check to the **decoded candidate layer** (after stripping userinfo),
  then normalizes that layer's `parsed.hostname`. A structurally malformed
  authority -- an empty explicit port such as `https://example.com:/x`,
  `https://example.com:`, or `https://[2001:db8::1]:/x`, a non-numeric port, an
  invalid IPv6 literal, and a port above 65535 -- returns no host. Because
  validation runs on the decoded layer rather than the original encoded string,
  an encoded malformed authority is rejected too: `https://example.com%3A/x`,
  `https://example.com%3Aabc/x`, and `https://example.com%3A99999/x` all return
  no host, while a valid encoded host such as `https://%65xample.com/x` still
  normalizes to `example.com` and yields the same host/path fact. (Pre-fix
  reproductions: `normalized_url_host('https://example.com:/x')` returned
  `'example.com'` and `normalized_url_host('https://example.com%3A/x')` returned
  `'example.com'`, each while `sanitize_host_paths` correctly emitted no fact, so
  the fetch parser recognized malformed URLs. Fixed by validating the decoded
  layer's authority and port before normalizing its host.)
- **Oversized numeric fields.** `int()` was called directly on the latency and
  attempt digit runs, so a run longer than Python's decimal conversion limit
  raised `ValueError` out of the parser. `_bounded_numeric` now strips leading
  zeros, rejects a significant run longer than `_MAX_NUMERIC_DIGITS = 18`, and
  only converts the bounded remainder, so an oversized field returns `None`
  without raising. (Verified directly: `int('9' * 5000)` raises
  `ValueError: Exceeds the limit (4300 digits) …`.)

These behaviours are pinned by committed tests, so RED is documented by the
pre-fix reproductions above rather than by a captured pre-fix run.

### Parser/projector alignment cases (`TestParserProjectorAlignment`)

The projector is the oracle: for every URL in the alignment table the tests
first assert `sanitize_host_paths(url)` yields a fact, then assert
`parse_npm_fetch_line` recognizes the line, then assert the parser's
`host-path` identity `host_path` equals the projector's `SafeHostPath.text`,
and finally assert `normalized_url_host(url)` equals the projector's hostname.
The accepted table covers ordinary DNS hosts, uppercase hosts, trailing FQDN
dots, IPv4 literals, IPv6 literals (`[::1]`, `[2001:db8::1]`), punycode, raw
IDNA (`café.example.org`), percent-encoded hosts (`%72egistry.npmjs.org`),
underscore reg-names (`a_b.org`), explicit ports (`:0`, `:443`, `:65535`),
credentials/query/fragment, and empty-label forms the projector accepts
(`example..org`, `-bad.org`, `bad-.org`). A generated host matrix additionally
asserts the implication "projector emits a fact ⇒ parser recognizes and
agrees" over 16 host spellings.

Rejection alignment (`test_malformed_authorities_have_no_fact_and_no_record`)
asserts, for `https://%`, `https://%2e`, `https://[`, `https://]`, `https://?x`,
`https:///x`, `https://`, `http://:80/x`, `https://user@/x`,
`https://exa%20mple.org/x`, empty explicit ports (`https://example.com:/x`,
`https://example.com:`, `https://[2001:db8::1]:/x`), encoded trailing,
non-numeric, and out-of-range ports (`https://example.com%3A/x`,
`https://example.com%3Aabc/x`, `https://example.com%3A99999/x`), and
out-of-range or non-numeric ports, that `sanitize_host_paths(url) == ()`
**and** `parse_npm_fetch_line(...) is None`, so the two acceptance rules cannot
drift apart again. `TestNormalizedUrlHostHelper` in
`tests/test_host_path_projection.py` pins the helper directly: malformed
authorities, non-numeric ports (`:0x50`, `:-1`), out-of-range ports (`:70000`,
`:65536`, `:99999`), and encoded malformed authorities (`%3A`, `%3Aabc`,
`%3A99999`, `%3A0x50`) return `None`, while ordinary/uppercase DNS, trailing
FQDN dots, IPv4/IPv6 literals, punycode, raw IDNA, percent-encoded hosts,
underscore reg-names, and credentials plus an explicit port still normalize.

### Delimiter-complete URL projection (`TestDelimiterCompleteHostPathProjection`, amends 4.5)

`_safe_host_path_text` passed the bare `record.url` to `sanitize_host_paths`,
which applies the streaming projector's conservative end-of-input handling for a
potentially truncated token. Because the fetch parser already knows the URL field
ends before the latency token, that boundary must be preserved: the field is now
projected as `record.url + " "`, a delimiter-complete field. The shared
projector's EOF protections are deliberately unchanged, because other callers
may genuinely receive truncated URLs.

Pre-fix reproductions: `http://localhost/a` and `http://localhost/b` both
rendered `npm http fetch GET 200 <redacted>` and produced equal `host-path`
identities, because a single-label host at EOF is not resolved; and
`https://example.com/%` fell back to `<redacted>`, losing the hostname.
After the fix the single-label paths render as `localhost/a` and `localhost/b`
with distinct `host-path` keys (while their `redacted` keys still match, since
all other grouping fields match), and `https://example.com/%` renders as
`example.com/<redacted-path>` in both canonical text and `host-path` identity.

### Literal control rejection (`TestFetchUrlControlRejection`, amends 4.2)

A literal C0 control (U+0000--U+001F) or DEL (U+007F) in the URL field is now
rejected before any parsing. Projection discards a control-bearing suffix, so
without this check a malformed line such as
`npm http fetch GET 200 https://example.com/a\x00junk 15ms` was recognized and
grouped with `example.com/a`, hiding the malformed input. The parser returns
`None` (it does not strip or neutralize the control), so the complete line stays
an ordinary diagnostic and can receive no fetch-group identity. This rejects only
literal controls: the percent-encoded forms `%00`, `%1B`, and `%7F` remain
recognized and render inertly as `example.com/a%00junk`, `example.com/a%1Bjunk`,
and `example.com/a%7Fjunk` with no literal control in the canonical text.

Coverage by task:

- 4.1 recognition: research cache-miss fixture, `0ms`, ordinary/leading-zero
  latencies, large integer status/latency/attempt, optional attempt, optional
  cache outcome, both clauses together, every method token, all 200–299 statuses
  (and only those), trailing newline, non-string type rejection, and complete-URL
  acceptance (credentials, explicit port, trailing FQDN dot, IPv4/IPv6 literals,
  punycode).
- 4.2 rejection: research `npm http cache` form, research retry/failure form,
  unrelated/malformed prefixes, bare `<METHOD>`, every non-2xx status,
  missing/non-URL-shaped URLs and malformed URL-shaped fields (`https://%`,
  `https://[`, `https://?x`, `https:///x`, `http://:80/x`, out-of-range port,
  empty host), literal C0 controls and DEL in the URL field (all of U+0000--U+001F
  plus U+007F, including NUL, ESC, and DEL; percent-encoded `%00`/`%1B`/`%7F`
  stay valid), malformed attempts, malformed latency units (`mks`, `s`, `m`,
  `h`, `MS`, `15`, `-15ms`, `+15ms`, `1.5ms`), unknown variants, reordered
  clauses, lowercase methods, trailing whitespace/CR/double newline, embedded
  newline, and oversized numeric runs.
- 4.3 identity: `redacted` excludes URL and latency and includes method, exact
  status, cache outcome, attempt presence/value; `host-path` adds normalized
  hostname/path and excludes latency, credentials, explicit port, query, and
  fragment; unsafe path and secret host fallbacks; `exact` has no identity;
  cross-mode keys never compare equal.
- 4.5 canonical text: redacted/host-path forms, `attempt #N` preserved, latency
  never rendered, `<redacted-path>` fallback, secret-host fail-closed, `exact`
  returns `None`; delimiter-complete projection of single-label hosts
  (`http://localhost/a` vs `http://localhost/b`: distinct `host-path` keys, equal
  `redacted` keys) and malformed-path fallback (`https://example.com/%` →
  `example.com/<redacted-path>`).

## 4.6 INTROSPECT

`TestFetchParserIntrospection` table-drives the grammar surface:

- all `768` valid cross-product combinations
  (`4 methods × 4 statuses × 3 latencies × 4 attempts × 4 cache outcomes`)
  parse to exactly the intended fields;
- a `52`-case single-field corruption table (method, status, URL, latency,
  attempt, cache) is rejected in full, never partially parsed;
- redacted identity is injective over its key fields (`256` distinct keys for
  `256` distinct field tuples);
- URL and latency changes never split a redacted group (one key across 9
  combinations);
- host-path identity collapses equivalent URL variants (uppercase scheme/host,
  explicit default port, trailing FQDN dot, credentials/query/fragment) to one
  key while `%2F`, a trailing slash, and a different host stay distinct;
- secret-bearing paths render `/<redacted-path>` and never enter `repr(key)` or
  the canonical text.

Deterministic complexity regression (`TestFetchPathologicalInputs`): a 200 KB
URL without latency, a 100 KB scheme, 20 000 path segments, a 100 KB dot run, a
5 000-token prefix run, a 5 000-digit latency, and a 5 000-character
percent-encoded host are each parsed once. The test asserts the recognition
postcondition (a recognized line always has a shared-projector-normalizable
host), asserts unambiguous rejections, and fails if any input raises. It makes
no wall-clock assertion, because a fixed millisecond threshold is
environment-sensitive.

Parser/projector alignment (`TestParserProjectorAlignment`) is described under
"Post-validation hardening" above; it makes the projector the oracle for host
acceptance and identity so the two rules cannot diverge again.

Numeric boundaries (`TestFetchNumericBoundaries`): latency and attempt parse at
17 and 18 significant digits and are rejected at 19, 4 301, and 20 000 digits
without raising; 20 000 leading zeros are insignificant and parse normally.
Malformed URLs (`TestFetchUrlValidation`): each malformed field returns `None`
and, because there is no record, `fetch_group_identity` and `canonical_fetch_text`
raise `TypeError` instead of yielding a key or text.

Introspected non-issues: `FetchGroupKey` carries `display`, so a `redacted` key
can never equal a `host-path` key; the anchored regex has no wildcard between
semantically distinct clauses, so no rejected form can be partially parsed; no
new state, timer, mailbox, or renderer is introduced.

## 4.7 VALIDATE

```sh
python -m unittest -v \
  tests.test_npm_fetch_phase4 tests.test_host_path_projection \
  tests.test_host_diagnostic_projection
# Ran 234 tests in 0.635s — OK

python -m unittest discover -s tests -p 'test_*.py'
# Ran 4249 tests in 52.158s — OK (skipped=13)

./scripts/check-types
# All checks passed!

openspec validate add-configurable-network-url-display --strict
# Change 'add-configurable-network-url-display' is valid

git diff --check
# clean
```

The 13 skips are the same environment-gated cases as before (8 need the
`scripts/validate-bound-pi-assembly-execution-phase6-*` harnesses, 3 need a
`docker-dev` account, 1 needs `sudo`/`runuser`, 1 needs `NPM_ENV_REAL_DOCKER=1`).

## Out of scope

Phase 4 stops at the pure parser and identities. Feeding recognized records
into the live presentation actor (Phase 5), binding the selector and selecting
the single retained representation (Phase 6), and end-to-end wiring (
Phase 7) remain open and are not claimed here.

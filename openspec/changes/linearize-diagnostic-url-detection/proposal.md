# Proposal

## Why

Diagnostic URL-start detection performs quadratic regex searches on long uninterrupted scheme-character runs: measured isolated searches grow from 0.013 seconds at 1 KiB to 3.383 seconds at 16 KiB. Two retained-tail scenarios consume approximately 16 seconds, and their file takes approximately 24.7 seconds on one `loadfile` worker; improving the production diagnostic path addresses both runtime latency and the test-suite bottleneck without shrinking coverage.

## What Changes

- Replace repeated unanchored scheme regex searches with compact linear raw-text candidate recognition using bounded percent-decoding transducers and structural matching, without materializing whole decoded search buffers or enumerating encoded spellings. Return an internal typed candidate containing a raw start index, an exclusive raw end index or `None` for an unterminated candidate, and `KNOWN` or `UNKNOWN` classification. A candidate is not proof of a valid URL. Recognition outcomes are separate: `NO_MATCH`, `MATCH(candidate)` and `NEED_MORE(retain_from)`. Preserve exact ambiguous raw tails and recognizer state across feeds, without fixed overlap or rescanning already consumed input.
- Detect `://`, not `:/`; preserve existing mixed/nested encoding coverage, earliest-source-candidate selection and raw-coordinate semantics. Separator detection covers effective encoding depth four (three existing decoding passes plus one encoded separator understood by the old regex). Carry raw source ranges through bounded recognizer states; do not add a fourth whole-URL interpretation pass.
- Classify schemes case-insensitively against the fixed whitelist: `http`, `https`, `ftp`, `ftps`, `ssh`, `git`, `git+ssh`, `git+http`, `git+https`, `file`, `ws`, `wss`, `socks`, `socks4`, `socks4a`, `socks5`, and `socks5h`. `npm:` is outside this separator-based detector.
- **BREAKING**: Unknown-scheme candidates remain conservatively hidden in `redacted` and `host-path`, but no longer produce normalized hostname, host/path, or URL-fingerprint metadata. Unknown identities may consequently coalesce when their remaining safe diagnostic identity matches. Known schemes remain subject to existing URL validation and safe projection; whitelist membership never makes a URL safe.
- Preserve arbitrary unknown scheme lengths and conservatively treat ambiguous `%XX` scheme units as candidates for hiding rather than rejecting them into ordinary output.
- Preserve local `exact` source-content behavior, terminal-control neutralization, bounds, EOF/abort handling, secret precedence in safe projection, and URL-free external SDK output independently of local mode. The current authoritative spec explicitly disables application content/secret redaction in local `exact`; this change does not introduce it.
- Validate complexity through deterministic automated coverage without recognizer timing measurements or timing-based assertions. Perform one-off before/after profiling and scaling measurements during implementation as separate evidence, not as automated tests or recurring CI benchmarks. Preserve the existing oversized tail fixtures and run the complete suite without exclusions.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `locked-npm-environment-assembly`: Refine bounded diagnostic URL recognition, known-scheme interpretation, unknown-scheme conservative hiding and metadata omission, while preserving stream and presentation-mode guarantees. Restrict the existing complete-URL fingerprint obligation to validated known-scheme URLs.
- `docker-build-output`: Align shared host diagnostic presentation and ephemeral URL-identity grouping with known/unknown scheme classification; unknown URLs remain hidden without metadata in safe channels.

## Impact

- Primary code boundary: `docker/versioning/diagnostic_projection.py`, especially `_find_url_start`, candidate termination, projector draining and clean-EOF finalization. Audit shared helpers and consumers before changing internal return contracts so no legacy regex path remains in an affected route.
- Integration coverage: retained-tail collection, selected local presentation, safe SDK events, hostname/path facts, ephemeral identities and grouping. Explicitly test malformed known URLs, unknown URLs, encoded forms, chunk splits, source offsets, unfinished candidates, secret overlap, overflow and recovery.
- No new runtime dependencies, configuration options, persistence formats, execution-policy changes, identity inputs, npm/Docker flags, or timeout changes.
- Out of scope: optimizing long-authority fetch recognition, shortening renderer shutdown budgets, changing xdist scheduling or AGENTS.md, splitting or excluding slow tests, and rewriting the entire streaming projector.
- Performance acceptance targets linear compact candidate recognition at fixed depth, bounded recognizer state independent of input length, no whole-buffer decoded search copies or variant tables, and removal of the demonstrated regex hotspot. Incremental candidate recognition must also avoid rescanning growing tails on single-byte feeds; verify amortized linear work deterministically. Preserve the existing 8 KiB unresolved-tail limit and EOF/abort/overflow behavior, without asserting linearity of unrelated downstream processing.
- Baseline measurements and subsequent verification evidence belong in separate evidence files, not in requirement text or completion claims. Planning does not implement the change.

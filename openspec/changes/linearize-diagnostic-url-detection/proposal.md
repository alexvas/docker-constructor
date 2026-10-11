# Proposal

## Why

Diagnostic URL-start detection performs quadratic regex searches on long uninterrupted scheme-character runs: measured isolated searches grow from 0.013 seconds at 1 KiB to 3.383 seconds at 16 KiB. Subsequent P3 review also exposed retained-suffix replay and duplicate raw payload ownership; fixing the production path requires cumulative work and ownership guarantees, not feed-only measurements.

## What Changes

- Replace repeated scheme regex searches with bounded streaming recognition of the fixed case-insensitive scheme set: `http`, `https`, `ftp`, `ftps`, `ssh`, `git`, `git+ssh`, `git+http`, `git+https`, `file`, `ws`, `wss`, `socks`, `socks4`, `socks4a`, `socks5`, and `socks5h`. Detect `://`, not `:/`; `npm:` is outside this detector. A recognized scheme is not proof of URL validity.
- **BREAKING**: Unknown schemes remain ordinary text, subject to literal secret handling and terminal safety, with no URL-derived hostname, host/path or fingerprint. Safe SDK text hides supported-scheme URLs, but is no longer guaranteed free of unknown-scheme URL-shaped strings. Select the earliest supported start, including within a longer string such as `TOKENhttps://host`.
- Preserve supported mixed/nested separator encodings, hex case and separately encoded escapes through effective detection depth four (three decoding passes plus encoded-separator matching). Keep raw provenance and existing interpretation depth; no fourth whole-URL decoding pass.
- Coordinate overlapping streaming URL and secret recognizers over the original source. Text/URL boundaries do not reset literal secret matching. Preserve possible URL continuations whose starts lie inside a matched secret as bounded states and coordinates, not copied or replayed suffix text. Do not reject secrets for spaces, Text/URL crossings, scheme suffixes or partial scheme suffixes.
- **BREAKING**: In safe modes replace literal secrets outside URL projection with `<secret>` rather than deleting them or using `<redacted>`. Within recognized URLs also detect secrets in up to three percent-decoding passes; this does not add decoded secret matching to ordinary text. Never create a valid URL or metadata by removing a secret and joining the remaining characters.
- **BREAKING**: `redacted` emits one irreversible `<redacted>` immediately on recognizing a complete supported scheme and `://`, regardless of authority completeness or secrets, except when a registered secret includes the URL separator and an earlier unresolved literal prefix of that secret crosses the recognized separator: marker delivery must wait for that prefix to resolve so source order is preserved. Once resolved, emit exactly one irreversible URL marker; otherwise immediate delivery remains mandatory. Clean EOF resolves this crossing prefix as a non-match if the secret never completed: with secret `Xhttps://host`, `Xhttps://ho` followed by clean EOF yields `X<redacted>`, without an incomplete-token marker. Abort and overflow rules remain unchanged. Suppress the rest through the raw terminator. Later EOF/abort/token overflow adds no replacement URL marker. `host-path` commits independently validated, secret-settled host and path components as their boundaries resolve; later failure cannot retract committed components. Before the full separator is detected, existing ambiguous-prefix finalization and overflow markers remain unchanged. Early display does not authorize early metadata.
- `redacted` hides the whole recognized URL as `<redacted>`, regardless of secrets. `host-path` renders host/path without scheme, credentials, port, query or fragment; a secret-bearing host becomes `<redacted-host>`, and an unsafe or secret-bearing path becomes `/<redacted-path>`. Local `exact` preserves source, including secrets, with existing bounds and terminal neutralization.
- Metadata derives only from independently validated supported URLs: credentials may contain a secret without preventing otherwise safe host/path facts; a secret-bearing host yields no normalized-host fact or URL fingerprint; a secret-bearing path yields no fingerprint but may retain a safe hostname fact. Rendering markers are not validated hostname or URL-identity facts. A URL-looking suffix attached to a preceding secret, such as `TOKENhttps://host/public` with secret `TOKEN`, supplies no URL facts after replacement with `<secret>`.
- Preserve current prompt independent prefix and secret delivery, exact still-required ambiguous-tail retention and source ordering. Marker sequencing changes only for the agreed secret marker and early irreversible URL/component commitment; uncommitted remainders retain fail-closed markers. Raw retained payload has one owner, the projector. Eliminate suffix replay in recognizer consumption and projector resets, without deferring secret commits to raw terminators.
- Verify deterministic cumulative recognition work across feeds, commits, secret processing and resets, including one-byte feeds. Keep one-off profiling outside automated tests/CI, unchanged stress fixtures, the complete required suite and applicable project checks.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `locked-npm-environment-assembly`: Supported-scheme recognition, overlapping literal/URL-decoded secret handling, policy-specific markers and metadata, replay-free bounded continuation and unchanged local exact behavior.
- `docker-build-output`: Align host presentation, retained diagnostics, SDK guarantees and grouping with supported schemes and secret-aware rendering/metadata.

## Impact

- Primary boundaries: `docker/versioning/diagnostic_recognizer.py`, `docker/versioning/diagnostic_projection.py`, secret registration and affected collector/fetch, structured projection, EOF and grouping routes. No new secret configuration rejection policy.
- Preserve shared transport, lock and hostname validation contracts; apply presentation-specific markers and fact gates at diagnostic boundaries. Fetch grammar continues to recognize otherwise valid unknown-scheme source URLs without generating URL facts from them.
- No new runtime dependencies, configuration options, persistence formats, assembly identity inputs, npm/Docker flags, timeout changes or execution semantics.
- Out of scope: optimizing long-authority fetch recognition, changing renderer shutdown budgets, xdist scheduling or AGENTS.md, reducing/excluding stress tests and unrelated projector rewrites.
- Recognition is amortized linear in input length at fixed encoding depth, scheme set and registered secret set. Search-specific live state is independent of stream length; registration storage may depend on the configured secret set. No duplicate raw recognizer tail, decoded search copies, input-sized search maps or encoded-variant enumeration. Preserve the 8 KiB pending limit, 64 KiB line limit and raw-boundary recovery, with the agreed early-commit EOF/abort/token-overflow marker exceptions, without claiming whole-pipeline linearity.
- Previous UNKNOWN-hiding, unrestricted suffix batch-equivalence and independent Text/URL-filter plans are superseded. Existing evidence remains historical; changed phase gates are reopened, and P3 cannot close until ownership, prefix delivery and cumulative-work guarantees pass final-state verification. Sync/archive requires a separate request.

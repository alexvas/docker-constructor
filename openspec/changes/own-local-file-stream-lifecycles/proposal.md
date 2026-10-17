# Proposal

## Why

Native local-file context cleanup can replace an active interruption with an ordinary close error, including silently converting it to a cache miss. The staged five-reader patch improves that behavior but adopts a `_FileRelease` proxy rather than the acquired file object itself, and its tests do not establish repository-wide convergence.

## What Changes

- Take ownership of the local-file findings delegated from `eliminate-unsafe-direct-descriptor-closes`: runtime projection and installed metadata reads, HTTP-cache file reads, cached-artifact verification, and build-projection loading. Include equivalent builtin `open`, `io.open` and `Path.open` origins discovered across production, not just five historical functions.
- Replace the staged proxy with direct adoption of each actual fd-owning file object through an explicitly specified prepared `OwnedStream` acquisition mechanism. Prepare validation/synchronization before opening; keep the public already-open constructor's rejection contract unchanged.
- Preserve each domain's open/read/parse/close mapping, encoding, bytes, paths, integrity checks and public signatures; apply shared precedence and exact first-interruption identity during release.
- Add complete-tree local-file origin/convergence checks, an executable acquisition/transfer/release ledger, real-file coverage and isolated image-layout verification. Record current evidence in this change's `verification.md` during implementation.
- Treat the staged implementation and its green runs as unaccepted historical baseline, not completed tasks or approval of proxy ownership. Plan its replacement and packaging/test reconciliation here without changing code or staging during proposal creation.
- Exclude the delegated local-file family from the original change's Phase 7 acceptance scope. Its remaining complete-tree raw-fd, file-wrapper and subprocess audit still must pass independently; this delegation is not a safe-release classification or a Phase 7 completion claim.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `descriptor-capabilities`: Add prepared acquisition of actual fd-owning streams and require complete-tree proof of local-file ownership, terminal release and failure precedence without a close-delegating proxy.

## Impact

Implementation will affect `docker/filesystem/streams.py`, `docker/_owned_file.py` (replace or remove its proxy), the five readers in `docker/runtime_installer.py`, `docker/versioning/{cache,build_materialization,verification}.py`, equivalent audited local-file sites, `Dockerfile`, lifecycle documentation, image-layout tests and `tests/enforcement/`. Any new public stream method must be explicitly approved by this change's API contract; no domain/path policy moves into filesystem and no process-private transfer helper becomes a consumer API.

The [original preflight](../eliminate-unsafe-direct-descriptor-closes/phase7-native-file-preflight-blocker.md) records the original reproduction. The separate [network change](../own-http-response-and-probe-server-lifecycles/proposal.md) continues to own HTTP responses, artifact-download iterator finalization and probe-server coordination. Archive/container wrappers and other newly discovered resource families require their own characterization decision rather than automatic inclusion or exemption.

# Proposal

## Why

Phase 7 preflight of `eliminate-unsafe-direct-descriptor-closes` found uncharacterized network resources: buffered and streaming HTTP responses, error-response bodies, and the gateway probe's listening server. The preflight demonstrated that the buffered transport returned an `HTTPError` body without releasing it; the other paths also needed explicit ownership and failure characterization rather than exemptions based on stdlib names or native context managers. The immediate minimal repairs below are now implemented; this change retains the broader lifecycle guarantees as remaining work.

## Delivered Minimal Baseline

- Buffered metadata and streaming acquisition HTTPError paths now release the error response through `OwnedStream`; successful responses still use their existing native context managers. Buffered HTTPError-handler sole close errors currently propagate rather than use the planned uniform 503 mapping.
- The artifact consumer explicitly finalizes closeable chunk iterators, including externally retained generators after processing failure, using shared cleanup precedence; compatible plain iterators remain supported.
- Probe startup prepares a listener adapter and `OwnedStream` before server acquisition and cleans failed startup without shutdown/join of a never-started thread. Sequential stop detaches references before shared-accumulator shutdown, release and started-thread join, preventing ordinary repeated/reentrant close; a second sequential active start is rejected.
- Added 29 targeted regressions, including operation-plus-close/interruption cases and real listener/worker release. Regenerated the existing source-hash/dependency inventory without weakening its equality assertion.
- Minimal-patch acceptance: **7862 passed, 13 skipped, 11 warnings**, typecheck, diff check and both strict change validations passed. Complete logs, intermediate failures and scope limits are recorded in [minimal-fix-verification.md](minimal-fix-verification.md). This does not claim full change acceptance or original Phase 7 completion.

Preparation before HTTP acquisition, ordinary-response migration, full readiness/concurrency coordination, safe retained incomplete cleanup, uniform error mapping and expanded ownership enforcement remain pending. The completed baseline is tracked separately at the beginning of `tasks.md`; later tasks extend and verify it rather than discard or reimplement it.

## What Changes

The following describes the full target beyond the delivered minimal baseline; partial overlap is not evidence that the broader tasks are complete.

- Give successful and error HTTP responses one prepared filesystem stream owner before fallible use, including exceptions raised by `urlopen`; preserve response bytes, status, headers, request configuration and existing error boundaries.
- Release streaming responses on EOF, read failure, interruption and explicit iterator termination. Finalize owned download iterators deterministically when the artifact consumer fails, without depending on garbage collection or requiring injected plain iterators to implement a new API.
- Make probe-server startup failure-safe and shutdown terminal, independent and repeat/concurrency-safe. Preserve active diagnosis failures, attempt every safe cleanup action, and retain responsibility rather than declare completion while a worker or winning release is unsettled.
- Keep HTTP/server policy in domain adapters and reuse `OwnedStream` / `CleanupFailures`; add no public filesystem API, integer owner for a response/socket `fileno()`, process coordinator dependency or generic network framework.
- Extend lifecycle enforcement to implicit HTTP-response acquisition and `server_close()` aliases, with concrete adapter/ownership proofs and negative fixtures, not a fourth direct-close category.
- Characterize all new families RED before migration, verify compatibility and real loopback resource release, and produce executable inventory/evidence for the original change's later Phase 7 re-audit.

## Capabilities

### New Capabilities

- `http-transport-lifecycle`: Explicit successful/error response ownership and deterministic streamed-response finalization across metadata and artifact HTTP consumers.

### Modified Capabilities

- `build-networking`: Failure-safe ownership and cleanup of the temporary host probe server and its serving worker without changing gateway diagnosis policy.

## Impact

Production scope is `docker/versioning/transports.py::build_transports._ProductionHttp.request`, `docker/versioning/artifact_cache.py::HttpStreamingTransport.fetch_chunks` and its artifact-download consumer, and `docker/networking.py::HostProbeServer` plus the diagnosis cleanup boundary. Small private domain adapters may be introduced to prepare a release capability before acquisition; the filesystem foundation's public surface remains unchanged.

Implementation also refreshes the existing generated source-hashed architecture inventory as derived evidence when these source edits make it stale, preserving its equality gate and recording why it changed; that refresh is not Phase 7 acceptance. Tests cover transport results, download publication/error boundaries, networking diagnosis, injected lifecycle faults and loopback fd/port safety. Architecture/documentation enforcement stays in `tests/enforcement/`. Request, cache, auth, retry, timeout, redaction, artifact integrity/publication and Docker gateway-selection policies are not redesigned. Pending metadata-revalidation work must retain these lifecycle guarantees without being implemented here.

This change builds on the implemented filesystem ownership/cleanup baseline from `eliminate-unsafe-direct-descriptor-closes`, not on that change's unfinished Phase 7. Creating this plan neither completes nor narrows its tasks and does not resolve its preflight blocker. After the remaining improvements are implemented and verified, the original complete-tree audit must be rerun and its evidence regenerated. The minimal patch already refreshed its derived source-hash/dependency inventory; original specifications, task states and historical acceptance remain unchanged.

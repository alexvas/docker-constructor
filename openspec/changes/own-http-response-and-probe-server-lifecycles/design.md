# Design

## Context

See `proposal.md` for motivation and the two delta specs for behavior. The original change's `phase7-preflight-blocker.md` records a descriptor-less deterministic reproduction: the buffered transport returns a 404 body while its retained `HTTPError` body remains open. It is historical evidence of missing explicit release before the minimal repair, not a real-network fd-leak measurement.

### Implemented minimal baseline

The user subsequently authorized immediate minimal fixes independently of the full improvement plan. The implementation and verification are recorded in [minimal-fix-verification.md](minimal-fix-verification.md), and completed baseline tasks are grouped under section 0 of `tasks.md`.

| Site | Implemented now | Remaining full-plan work |
| --- | --- | --- |
| `transports.py::build_transports._ProductionHttp.request` | HTTPError is released through `OwnedStream`, including read failure; normal responses retain their native context manager | Pre-acquisition response preparation, ordinary-response shared ownership, full fault matrix and uniform sole-close mapping to 503; the minimal error-handler close-only OSError currently propagates |
| `artifact_cache.py::HttpStreamingTransport.fetch_chunks` | HTTPError resources are released through `OwnedStream` before propagation; normal streamed responses retain their existing native context manager | Prepared ownership for all response paths, explicit throw/close/EOF fault coverage and origin proofs |
| `artifact_cache.py::_materialize_one` | Actual closeable chunk iterator is finalized explicitly under `CleanupFailures`, including when externally retained after write failure; plain iterators remain supported | Extend the full chunk/hash/write/cancellation and independent iterator/response/temp/lock fault matrix and executable ownership proofs |
| `networking.py::HostProbeServer.start/stop` | Prepared listener adapter/owner, cleanup after server/thread setup failure, sequential active-start rejection, reference detachment before accumulated shutdown/release/join, no repeat/reentrant close | Actual serving-loop readiness, started-then-raised races, concurrent state coordination, worker-safe deferred release, shared deadline and retained winning/incomplete outcomes |
| `networking.py::diagnose_gateway` | Existing finalization path unchanged | Preserve active diagnosis failures across teardown |

The minimal probe stop still releases the listener before joining and drops its saved references before cleanup; it does not retain an unsettled worker/outcome or claim safe incomplete/concurrent shutdown. The full design below replaces those limited mechanics rather than treating them as its target.

The patch added 29 regression cases in `tests/test_network_resource_minimal_cleanup.py`, regenerated the existing architecture/source-hash inventory, and passed the full suite with **7862 passed, 13 skipped, 11 warnings** plus typecheck, diff check and strict validation of both changes. These results accept only the minimal patch; explicit unittest discovery and the complete planned matrices remain pending.

`OwnedStream` already prevalidates and synchronizes terminal release, but its constructor can fail before adoption. Its private prepared-slot functions are currently proved for `docker.process`, not a general consumer escape hatch. `CleanupFailures` supplies the required precedence. Neither shared primitive should gain HTTP or server authority.

## Goals / Non-Goals

**Goals:**
- Prepare release capacity before acquiring a response or listening server, and bind every actual returned resource before any fallible inspection or worker setup.
- Use one shared filesystem release authority across success, failure, interruption and concurrent finalization; retain the actual winning result, not just a release-claimed bit.
- Preserve existing transport/diagnosis APIs and domain policy while making newly observed cleanup failures explicit.
- Make the implicit network acquisition/release chain visible to complete-tree enforcement and later Phase 7 verification.

**Non-Goals:**
- No new public filesystem API, generic network framework, process-owner use for threads/sockets, raw fileno adoption or socket duplication.
- No metadata revalidation, caching, auth, retry, timeout, TLS, URL-display or artifact-publication redesign; no changes to the pending cooperative Docker wrapper.
- No arbitrary external reader ownership, forced cancellation of arbitrary blocking Python code, or guarantee that dropping a retained iterator triggers cleanup.
- No completion, exclusion, task rewriting or automatic inventory refresh for the original change merely because this plan exists.

## Decisions

These decisions describe the remaining full target. Reuse the implemented baseline and its regressions; do not treat its narrower passing cases as proof of the stronger contracts or require already-fixed leaks to become RED again.

### 1. Prepare narrow domain release adapters before acquisition

Use a small private HTTP lifecycle helper under `docker/versioning/` for the two HTTP consumers, and a separate private listener adapter in `docker/networking.py`. Each prepares a concrete release adapter and its `OwnedStream` (including locks and label validation) before the acquisition call. The adapter initially holds no acquired target. Immediately on a returned response/server, bind that target by a non-calling assignment before status/header/address inspection, reads or worker creation.

The HTTP adapter's sole close delegate releases the acquired response object. For an HTTPError, own that exception's response interface as one object; do not independently own its `fp`, socket or descriptor number. An exception with no response body requires no fabricated resource. Acquisition that raises without exposing a resource leaves cleanup with an empty prepared adapter and no target close.

The listener adapter translates its close delegate to the bound server's `server_close()`. Stop signalling, serving readiness and worker joins remain networking coordination, not the adapter's generic close operation. It does not own the socket again or independently close an accepted connection or request stream managed by the standard server.

The `OwnedStream` terminal claim remains the sole release authority. The adapter adds no competing reusable live/released state machine. No native response/server context manager may independently release the same adopted resource. Adapter close delegates are reachable only through the verified shared-owner release chain; they are not a new direct-close category or name-based exemption.

**Alternatives rejected:** wrapping the response after `urlopen` in a fallible public owner constructor (the preparation gap); adding new uses of process-private bind functions; closing a rejected adoption; owning a socket `fileno()` again; moving HTTP/server adapters into the filesystem foundation.

### 2. Preserve distinct buffered transport error boundaries

Characterize status, body, headers, timeout, request construction and the existing three boundary behaviors before edits:
- Ordinary request/normal-response read OSError maps to the existing synthetic 503 response.
- A successfully read HTTPError maps to its HTTP status, headers and body, not a generic network failure.
- An error thrown while reading inside the existing HTTPError handler currently propagates outside that handler; retain that boundary rather than inadvertently swallowing it through a larger try block.

Use shared cleanup around each operation. HTTP status exceptions deliberately consumed as valid buffered response data are not an active operation primary after successful body processing. A newly observed sole release OSError uses the existing 503 operational mapping, including after a successfully read error response. Normal/error response read failures remain primary over ordinary release failures and retain existing mapping or raw propagation. Unexpected release defects and control flow retain shared precedence and exact selected identity before any ordinary domain mapping. Do not add raw cleanup notes to public response text; preserve current safe diagnostic projection and carry retained secondaries at existing exception boundaries.

For the streaming transport, an HTTPError from acquisition remains the exact operation failure, but any exposed error response is owned and released before propagating it. Do not turn streaming HTTP errors into buffered metadata responses.

**Alternative rejected:** treating all HTTPError values as failed operations, or flattening all read/close errors into a new uniform transport policy; both change characterized behavior beyond lifecycle correction.

### 3. Finalize the acquired iterator at the artifact-consumer boundary

Keep `fetch_chunks(url) -> Iterator[bytes]` lazy and unbuffered. Response ownership spans iteration and shared finalization covers EOF, read failure, explicit throw and close. No network call is added before first advancement.

The artifact download consumer captures its actual iterator and finalizes it deterministically on all exits when it exposes a callable close operation. Plain injected iterators remain supported; their protocol is not expanded. The consumer's active chunk-validation/hash/write/cancellation failure is retained while iterator finalization, temporary cleanup and lock cleanup run under shared precedence. No finalize/publication step follows a failed download or response close.

Generator finalization is not a second raw descriptor release: it enters the response's same owner-controlled cleanup. Preserve Python GeneratorExit behavior: `generator.close()` normally consumes GeneratorExit, whereas an explicitly thrown interruption retains its identity. Do not promise a new public failure/status channel for discarded iterators or depend on GC. Test the distinction between a close-only failure at normal EOF and an ordinary secondary cleanup failure during control-flow termination.

**Alternative rejected:** assuming a for-loop closes a retained generator when its body raises; it does not provide that ownership guarantee.

### 4. Keep a per-probe lifecycle with safe readiness and winning outcomes

Retain `HostProbeServer`'s constructor, token/port behavior, `start() -> int` and successful `stop() -> None`. Prepare labels, owner, synchronization and lifecycle tracking before listener acquisition. A failed listener factory starts no worker. Immediately bind a returned listener before reading its address or creating/starting its worker. Account for a thread that actually starts even if its start/readiness seam then raises.

Reject a second start before acquisition while an earlier generation owns a listener, worker or unsettled outcome. A fresh start after fully settled cleanup may create a new generation; stop callers for one generation never obtain a fresh deadline or trigger another acquisition.

Track never-started, serving/reader-safe, exited and cleanup-settled states separately. Never call the stdlib blocking shutdown path for a serving loop known never to have entered, and never join a never-started thread. Readiness must establish actual serving-loop progress or exit, not just a flag immediately before a possibly failing serve call. Use deterministic barriers to prove that startup/stop races cannot wait on a loop that will never run.

The first stop request establishes one monotonic worker-wait deadline using the existing five-second join allowance, shared by concurrent/repeated observers. Supported stdlib shutdown and worker-exit cleanup use the same lifecycle. Arbitrarily blocked injected shutdown/close callbacks have no forcible timeout guarantee.

Accumulate stop signalling, every safe listener release and started-worker joining rather than stopping at the first ordinary failure. Release only after a tracked worker-safety barrier; the serving worker can finalize the same owner on exit so a caller interruption or bounded join cannot strand later-safe release. Record winning release failure/completion before publishing cleanup settlement. A losing caller seeing `owner.released` is not evidence that the winning close returned.

If bounded observation cannot settle safe cleanup, retain the same worker, listener owner and outcome storage; raise a controlled RuntimeError for incomplete probe cleanup rather than return successful stop. This adds no public status DTO or generic process dependency. Later stop observes the same lifecycle, does not retry release or reset its deadline, and may report later settlement without mutating an already-delivered error. Do not emit late CLI diagnostics from the worker.

Protect the diagnosis finalization boundary with the same shared precedence so ordinary stop failures do not mask an active probe/diagnosis failure. Cleanup-only ordinary failures propagate through existing networking/facade error handling; unexpected defects and interruptions are not normalized as success.

**Alternatives rejected:** blindly calling shutdown after failed thread startup (deadlock); clearing references before outcome retention (lost responsibility); unconditional server_close beneath a live worker; retrying close after an ambiguous exception; introducing a generic server/process framework.

### 5. Extend enforcement with resource origins and call-chain proofs

Under `tests/enforcement/`, resolve literal/aliased `urlopen`, HTTP-status exception response binding, iterator-finalization paths, server factories and `server_close` callables, including keyword calls and newly added production modules. Inventory actual acquisition, prepared binding, release delegation, worker barrier and retained outcome locations.

Accept an adapter only when the AST/dataflow proves its bound target is released through the sole terminal shared owner. Negative fixtures cover native-context-manager double release, missing error-response accounting, use before binding, raw-fileno ownership, recreated owners, borrowed close aliases, server_close aliases, skipped worker/independent cleanup, false release settlement and dropped incomplete references. Positive fixtures cover the concrete proved release chain and descriptor-less iterators without granting a blanket non-descriptor exemption.

Filesystem imports remain explicit and HTTP/server policy must not move into the foundation. Keep the primitive / terminal shared-owner internal release / verified handoff classification unchanged. Generate a scoped resource ledger and API/dependency evidence for this change. Source changes also require regenerating `eliminate-unsafe-direct-descriptor-closes/phase6bd-process-inventory.json` through its existing generator as derived evidence, without changing its equality assertion, original specifications or task checkboxes. Record the baseline-to-current inventory differences in this change's verification evidence and retain historical acceptance as historical. This maintenance is explicitly in implementation scope so the full suite can pass without a circular dependency on unfinished Phase 7; it is not a substitute for that later re-audit.

## Risks / Trade-offs

- **[Ownership gaps in exception acquisition]** → Prepare before the request and bind the caught response before any read, formatting or cleanup; fault-inject every seam.
- **[Generator GC masks missing finalization]** → Retain external references in tests, disable reliance on finalizers and assert response close counts before returning from the consumer.
- **[Server shutdown deadlock or fd reuse]** → Test never-started and pre-entry failure paths; require worker-safe release and loopback/fd-reuse evidence.
- **[Terminal claim mistaken for settled cleanup]** → Block the winning close in tests and verify losing/reentrant/concurrent observers retain incomplete responsibility and eventual failure identity.
- **[Newly observed close failures alter a previously successful result]** → This is the authorized lifecycle correction; pin mappings above and preserve all unrelated output/policy.
- **[Concurrent metadata-revalidation work touches the same transport]** → Integrate lifecycle boundaries without implementing or redesigning revalidation, cache format or provider provenance.
- **[Original generated architecture inventory becomes stale]** → Regenerate derived evidence using the existing generator and verify equality; record differences without changing original requirements/task states or claiming Phase 7 acceptance.

## Migration Plan

0. **Delivered:** retain the minimal repairs, 29 regressions, regenerated derived inventory and passing checks described above and in `minimal-fix-verification.md`. Completed section 0 tasks record this acceptance without checking off the larger remaining contracts.
1. Capture current transport, artifact and gateway behavior plus baseline signatures and the exact original preflight reproduction. Reuse existing minimal regressions as passing characterization and add RED fixtures only for the still-unmet preparation/precedence, iterator and server fault/race contracts, preserving complete logs/statuses.
2. Implement the prepared HTTP lifecycle and migrate buffered/error responses, then streamed responses and consumer finalization. Validate each family before server migration.
3. Implement prepared probe ownership, readiness/stop coordination and diagnosis-finalization precedence. Validate deterministic faults and real loopback behavior without Docker/external network access.
4. Add executable origin/adapter proofs, scoped inventory and concise lifecycle documentation links. Regenerate the existing source-hashed process inventory as derived evidence, preserving its equality test; record inventory differences and run API/dependency and no-drift comparisons.
5. Run project typecheck, the full eight-worker pytest suite, explicit unittest discovery, diff check and strict validation. Preserve full output/statuses and require all gates to pass; any uncharacterized site or additional scope is a blocker, not an exemption.
6. Keep original Phase 7 tasks unchecked until their own re-audit and release gates pass. Updating its blocker/evidence after implementation is a separate authorized follow-up, not accomplished by this planning change.

Rollback is source-only: revert network consumer/adaptor changes together, leaving existing filesystem/process ownership intact. No persistent data, cache format, configuration or deployment layout migration is introduced.

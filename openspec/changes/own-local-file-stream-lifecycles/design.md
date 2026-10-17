# Design

## Context

See [proposal.md](proposal.md). The original local-file preflight proved native file-context close masking an exact `KeyboardInterrupt`. A subsequent staged patch covers five readers through `docker/_owned_file.py::_FileRelease`; it prepares an `OwnedStream` around the adapter, not the acquired file itself. Its tests intentionally prove that proxy shape and therefore need replacement, not promotion to an ownership exemption.

The existing `OwnedStream(stream, *, label)` validates before transfer and must not close rejected caller-owned streams. Process-private `_prepare_stream`/`_bind_prepared` are currently proved for `OwnedProcess` only. Merely importing them in a file consumer would not establish an approved transfer boundary. The proposed mechanism below must gain its own executable proof and exact API checks before migration.

The observed baseline includes 7880 passing pytest tests (13 skipped, 11 warnings; `/tmp/docker-constructor-pytest.ypEaIV.log`) and 6448 unittest tests (13 skipped; `/tmp/docker-constructor-native-file-unittest.yFMHga.log`). These are historical behavior results only: no task in this new change is completed by them, and they do not accept the proxy, the complete-tree audit or original Phase 7.

## Goals / Non-Goals

**Goals:** direct actual-file ownership prepared before acquisition; exact shared precedence; compatible readers; a source-derived complete-tree local-file inventory and executable ledger; independently verifiable image packaging.

**Non-Goals:** no VFS, path policy, parsing or file validation in filesystem; no network/probe migration, new process policy, raw-fileno adoption, raw-fd `fdopen` contract change, retries or GC authority. No original Phase 7 acceptance, staging changes or automatic code rollback during this planning action.

## Decisions

### 1. Propose one bounded acquired-stream entry point

The API contract for review is additive:

```python
OwnedStream.acquire(opener: Callable[[], Any], *, label: str) -> OwnedStream
```

Keep `OwnedStream(stream, *, label)` and existing public properties/context/close behavior unchanged. The new entry point accepts a prompt or blocking caller-owned factory, not a filename/mode/options policy API. File paths, modes, encodings and domain mapping stay in callers. It provides no timeout or forcible cancellation guarantee for arbitrary Python I/O.

Implement acquisition inside the shared stream lifecycle. Validate the callable/label and prepare owner storage, synchronization and empty acquisition state before invoking the factory. Immediately bind the exact factory result into that owner's resource slot with no intervening fallible inspection or replacement allocation. Protect any subsequent interface/setup validation through the same owner and shared precedence. Empty preparation owns no resource and must not close a fabricated target. Once a result exists, injected transfer/setup failures must still leave that exact object accounted for by the prepared authority.

Use the existing synchronized terminal release state machine, extended only as necessary to represent private prepared/empty versus acquired state; do not create another wrapper or independent live/released ledger. The factory may raise without returning a stream; its internally hidden resources remain the factory's responsibility, not a promise of discovery by the owner. Binding/factory tests must use genuine faults and a returned-resource ledger, never mocks that close rejected candidates themselves.

This explicitly proposed public addition is part of this change, not a retroactive claim that the original bounded stream API already allowed it. Update exact-surface/dependency assertions and executable documentation with the implementation. Existing process-private helpers may share internal mechanics only if their preparation, partial transfer, reader barriers and retained outcomes remain proved unchanged; consumers must not call those private helpers directly.

**Rejected alternatives:** constructing `OwnedStream(open(...))` after acquisition (fallible preparation gap); a public prepare/adopt pair composed by every caller (unprotected handoff combinations); retaining `_FileRelease` (proxy rather than actual-file adoption); another raw-fd owner for `fileno()` (double ownership); hiding path/open policy in filesystem.

### 2. Replace the staged helper without accepting its ownership model

Migrate each reader to a scoped directly acquired owner and borrow `owner.stream` only for its existing read/parse/hash operation. Do not also enter the native stream context. Remove `_FileRelease` and its delegated close. Remove `docker/_owned_file.py` and its Dockerfile copy if no justified policy-free facade remains; if retaining a private convenience facade, it may only compose the public acquired-stream owner and must own neither a proxy nor a second release state machine.

Preserve the staged passing behavioral regressions where valid. Replace structural tests that demand the proxy with direct-target/preparation proofs. The reader patches, helper packaging and generated inventory are provisional baseline artifacts now assigned to this change; planning does not move, unstage, revert or accept them.

### 3. Characterize each domain boundary separately

| Reader | Compatibility boundary to pin before migration |
| --- | --- |
| `runtime_installer.read_projection` | Existing missing-file `ProjectionError`, TOML parse mapping, schema/integrity validation, and successful entry ordering; interruption must not become a parse failure |
| `InstallContext.real_metadata().read` | Missing metadata versus read/permission/JSON `InstallError`, original cause relationships and object validation |
| `DiskCache.get` | Existing open/read/JSON/ordinary sole-close cache-miss policy, successful response fields and text encoding; do not swallow control flow |
| `build_materialization._verify_hit` | Existing stat/mode/digest checks and ordinary I/O miss behavior; no new publication or permission policy |
| `verification._load_build_projection` | Existing raw operational/parse failures and loaded TOML value |

Apply `CleanupFailures` precedence within lifecycle release with ordinary `(OSError,)`. Original/first control-flow identity wins over later close defects or interruptions; unexpected cleanup defects outrank an active ordinary failure; active ordinary read/parse errors outrank ordinary close. Retain selected and secondary identities before domain mapping. Preserve each boundary's established mapping and diagnostic exposure rather than imposing one generic file error. Extend tests for simultaneous failures and mapped causes, not only successful values or exception text.

### 4. Scan the complete production tree, but classify by resource family

Generate a source-hashed inventory across every production Python module, including future modules. Resolve builtin and `io.open`, imported/assigned aliases, `Path.open`, bound opener/closer aliases, function defaults/keywords and context-manager bindings. Record convenience reads and wrapper factories as candidates when they conceal a local-file acquisition; investigate them rather than exempting names such as `read_text`, archive or container methods. Nonmatching or uncharacterized archive/container/resource families require an explicit decision; they are not silently migrated as plain files.

For each in-scope origin, identify acquisition, preparation, actual-target binding, borrowed use, release delegate, terminal guard and winning outcome. Prove the shared acquired-stream path rather than recognizing a class/import name. No five-function allowlist, whole-module exclusion, fourth direct-close category or success classification for a delegated site.

The runtime/injected repository ledger must span every discovered in-scope local-file site, not a stand-alone model: compare actual factory returns and close events with the static inventory, and test missing/delayed adoption, proxy/native double release, duplicate raw-fileno owners, retries, skipped sibling cleanup and lost concurrent outcomes. Use real files for supported EOF/read/release behavior and deterministic barriers for concurrency. Descriptor-less doubles prove injected precedence, not OS-fd safety by themselves.

### 5. Keep independent acceptance boundaries

The original change's Phase 7 still runs its whole-tree audit, retains delegated local-file/network rows as such, and supplies evidence for every remaining raw-fd, `mkstemp`, `fdopen` and subprocess lifecycle. Only local-file factory stream defects and this proxy replacement are delegated here; raw-fd work in the same modules is not excluded. Completion of this change is not a prerequisite to that narrowed Phase 7, nor is delegation evidence that either audit passed.

This change writes its own `verification.md` with the complete local-file inventory, per-site ownership/failure ledger, API/DAG/image evidence, no-drift comparisons, full logs and statuses, including unsuccessful runs. Regenerate derived inventories only through their generators; preserve equality checks. Required validation includes eight-worker full pytest, explicit unittest discovery, typecheck, diff check and strict OpenSpec validation. No skipped failing checks, warning suppression or baseline-log substitution.

## Risks / Trade-offs

- **[Risk] Shared API growth changes process ownership.** → Bound the addition to one acquired-stream entry point and rerun constructor, process preparation/transfer, reader-safety, concurrency and late-outcome regressions.
- **[Risk] Transfer instrumentation fails before ordinary validation.** → Require actual returned-target accounting and protected release through the already-prepared owner; do not repair a failed transfer with a new owner or borrowed close.
- **[Risk] Cache error suppression hides interruption again.** → Test exact selected identities through every domain boundary with combined close failures.
- **[Risk] Five passing sites conceal another factory alias.** → Generate the inventory from all production sources and bind runtime ledger coverage to its in-scope rows.
- **[Risk] Staged proxy tests pass and are mistaken for approval.** → Keep all new tasks unchecked and explicitly replace ownership-model assertions; preserve useful behavior tests as baseline only.
- **[Risk] Image imports depend on an obsolete helper copy.** → Verify the exact resulting Dockerfile layout from outside the repository with a scrubbed import path.

## Migration Plan

1. Establish the approved delegated scope and characterize all discovered local-file sites before expanding production edits.
2. Demonstrate unmet direct-target/preparation/enforcement contracts RED, keeping existing behavior tests green where already fixed.
3. Implement the bounded acquired-stream API, migrate consumers and remove proxy authority; reconcile helper/image dependencies.
4. Complete whole-tree origin proof, repository ledger, documentation and API/DAG checks.
5. Run fresh verification gates and record evidence here. Leave original Phase 7 task states independent.

Rollback is source-only: restore reader call sites and packaging before removing a used shared entry point; preserve the already-open constructor and process behavior. No persistent formats or identities change.

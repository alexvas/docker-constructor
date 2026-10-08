## Why

Several descriptor-owning paths still use bare `finally: os.close(fd)`, multi-descriptor loops that stop after the first close failure, or manual parent/child handoffs that can mask an active operation failure, skip independent releases, retry an ambiguously failed close, or leak an already-open child. The new lightweight `docker.filesystem` ownership and cleanup primitives now provide a common way to remove these defects without coupling leaf domains to `docker.transactions` or turning the foundation into a general VFS.

## What Changes

- Inventory every direct and implicit descriptor lifecycle across the production tree—including `os.fdopen(..., closefd=True)` adoption and raw descriptors returned by `tempfile.mkstemp()`—migrate every consumer-owned release to an owned lifecycle or protected cleanup, and bind every retained direct close to exactly one permitted classification: low-level POSIX primitive, shared-owner internal release, or verified ownership handoff.
- Replace single-descriptor consumer lifecycles in npm lockfile writing/tree hashing, runtime artifact verification, private build-context writing, snapshot destination copy, artifact-cache temporary creation, and validated project state with `OwnedDescriptor`; active-primary cleanup additionally uses shared cleanup precedence while success-path release remains terminal and at-most-once through the owner.
- Package `docker/filesystem/` into `/usr/local/lib/pi-cli/docker/filesystem/` in the image so the migrated runtime installer can resolve its lightweight lifecycle imports without relying on the source repository.
- Refactor build-cache multi-descriptor release so every descriptor has an `OwnedDescriptor` lifecycle and one aggregate `CleanupFailures` attempts every independent release even when another close fails.
- Replace unsafe manual directory parent/child handoff and `_bootstrap_constructor_project_lock_parent()` in `docker/versioning/build_cache.py` with `DirectoryDescriptor`/`OwnedDescriptor` lifecycle where the existing foundation contract is sufficient; retain build-cache path derivation, recursion, lock policy, cache identity, durability, and domain errors locally.
- Preserve direct close only inside low-level POSIX adapters, inside `OwnedDescriptor.close()` (or an equivalent shared-owner implementation) after ownership is terminal, and as an ownership-handoff operation whose parent is terminal and whose child is already tracked; consumer success-path and lifecycle close operations delegate to that shared owner even when only one descriptor is involved.
- Add production-tree symbol-origin and fd-ledger gates that resolve direct and aliased POSIX close callables, arbitrarily named injected descriptor backends, aliased `os.fdopen`, and aliased `tempfile.mkstemp`; reject unauthorized direct release, consumer `fdopen(closefd=True)`, `fdopen(closefd=False)` without a shared owner, and unadopted factory fds, and prove no descriptor is leaked, retried, skipped, implicitly released, or hidden by renaming.
- Preserve existing public APIs, error types/messages, persistent paths, modes, hashes, cache identities, publication order, lock semantics, and durability behavior except for one explicit correction: map a sole runtime-artifact close `OSError` at `open_verified()` to a close-specific `InstallError` so existing callers and CLI/result handling report a controlled installation failure.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `durable-filesystem-transactions`: Require descriptor-owning consumers to preserve active failures, attempt every independent release once, and use the lightweight filesystem lifecycle where its complete contract applies.
- `locked-npm-environment-assembly`: Require lockfile-input and tree-file descriptor release not to mask assembly/tree failures while preserving npm domain policy and output identity.
- `docker-runtime`: Require mounted runtime-artifact descriptor release not to mask validation, read, or integrity failures and to remain at-most-once.
- `build-artifact-materialization`: Require build-context, artifact-cache, build-cache, snapshot, and project-state descriptor lifecycles to avoid skipped cleanup, unsafe handoff, repeated close, and child leaks without changing domain protocols.

## Impact

Affected production code includes `Dockerfile`, `docker/npm_environment/execution.py`, `docker/npm_environment/tree.py`, `docker/runtime_installer.py`, `docker/versioning/build_context_confinement.py`, `docker/versioning/build_snapshot.py`, `docker/versioning/artifact_cache.py`, `docker/versioning/build_cache.py` (including `_bootstrap_constructor_project_lock_parent()`), `docker/versioning/project_state.py`, `docker/npm_environment/publication.py`, `docker/transactions/capabilities.py`, `docker/transactions/locking.py`, `docker/transactions/regular.py`, `docker/versioning/build_cleanup.py`, `docker/versioning/cache_storage.py`, `docker/versioning/rendering.py`, `docker/versioning/build_materialization.py`, `docker/versioning/cache.py`, every additional production consumer-owned close found by the binding inventory, and any narrowly related lifecycle helper needed to adopt `docker.filesystem.cleanup`, `descriptors`, or `operations`. Existing raw POSIX adapters remain raw, and `docker.filesystem` gains no recursion, cache, transaction, publication, lock, durability, or domain authority. Tests will add injected close failures, process-control cases, multi-resource ledgers, handoff leak/retry checks, AST inventories, an isolated image-layout import check with the repository absent from `sys.path`, and full compatibility gates.
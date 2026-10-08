## Context

See `proposal.md` for motivation. The repository now has a lightweight `docker.filesystem` foundation with `CleanupFailures`, `OwnedDescriptor`, `DirectoryDescriptor`, and injectable descriptor operations. A final ownership inventory still identifies explicit and implicit descriptor ownership in npm regular-file work, runtime artifact verification, build-context writing, artifact/build-cache traversal, materialization, rendering, persistent cache writing, snapshots, and project state.

A direct descriptor-close call remains appropriate only inside raw POSIX adapters, inside `OwnedDescriptor.close()` or an equivalent shared-owner implementation after it has made ownership terminal, and as the principal operation of a handoff whose releasing parent is already terminal and whose child is already tracked by the surrounding ownership-aware failure path. Every consumer-owned single descriptor—including a success-path release outside `finally` and a lifecycle object's release method—uses `OwnedDescriptor`. The defects include masking `finally` closes, implicit file-object ownership from `os.fdopen(..., closefd=True)`, unadopted raw descriptors from `tempfile.mkstemp()`, unowned single releases, nonterminal/multi-resource close loops, and handoffs whose ledger cannot account for an opened child after parent close failure.

The foundation remains below `docker.transactions` and every domain package. Npm and versioning leaves may import explicit `docker.filesystem` submodules, but `docker.filesystem` must not import them or acquire their path, cache, recursion, lock, publication, durability, or error-mapping policy.

## Goals / Non-Goals

**Goals:**
- Give every migrated raw descriptor one explicit live/transferred/release-attempted lifecycle.
- Preserve an active operation/domain failure over ordinary close failure and retain the close diagnostic.
- Attempt every independent release once even after another release fails.
- Make parent/child handoff mechanically leak-free and retry-free.
- Keep an executable production-tree classification for every explicit close, injected backend release, `fdopen` wrapper, and fd-returning `mkstemp`, permitting direct close only for a low-level POSIX primitive, gated shared-owner internal release, or verified ownership handoff.

**Non-Goals:**
- Remove every textual `os.close()` from production.
- Move regular-file validation, hashing, integrity, cache layout, traversal, recursion, lock policy, publication, or durability into `docker.filesystem`.
- Make domain modules import `docker.transactions` merely to reuse its L0 backend.
- Add a generic filesystem, path-policy, retry, recovery, or recursive removal API.
- Change public APIs, persistent data, user-visible domain diagnostics, or success semantics.

## Decisions

### Use `OwnedDescriptor` for every single consumer-owned lifecycle

After a successful raw `os.open`, npm lockfile writing, npm tree hashing, runtime artifact verification, private build-context writing, snapshot destination copy, and artifact-cache temporary creation transfer the fd immediately into directly constructed `OwnedDescriptor(PosixDescriptorOps(), fd, label=...)`. `ValidatedProjectState` likewise stores or delegates to one private `OwnedDescriptor` while preserving its public construction and descriptor access contract. Operations continue to use the live `fd` property.

With an active primary, `CleanupFailures(primary)` runs `owner.close` alongside any independent cleanup such as unlink. On a successful path, `owner.close()` reports a sole close failure while retaining terminal at-most-once state. A bare `finally: close()` is never considered a sole-failure exception because an operation failure may already be active.

This reuses only ownership and close precedence. It does not add regular-file open/read/stat/hash APIs to the foundation. Alternative: add `RegularFileDescriptor`. Rejected because these consumers have different type, ownership, mode, integrity, and domain policies, and only lifecycle is common. Alternative: retain bare success-path or `finally` closes. Rejected because they bypass uniform terminal ownership, and a `finally` close can additionally mask an active failure.

### Make implicit file-object adoption explicit

Every raw fd returned by `tempfile.mkstemp()` or another integer-fd factory transfers immediately to `OwnedDescriptor`. Consumers that need a Python file object construct it with `os.fdopen(owner.fd, ..., closefd=False)`, so closing the file object cannot implicitly release the descriptor and the shared owner remains the sole release authority. File-object flush/close errors remain operation failures; owner release runs afterward through `CleanupFailures(primary)`, making descriptor-close failure secondary and terminal. The migration covers artifact materialization, effective-inventory rendering, and HTTP cache persistence while preserving temporary-name cleanup, publication order, bytes, modes, and domain mapping.

The convergence gate resolves aliases of `os.fdopen` and `tempfile.mkstemp`, treats omitted `closefd` as `True`, rejects consumer `closefd=True`, and requires every fd component returned by `mkstemp()` to transfer to a shared owner before another fallible operation. `closefd=False` is accepted only when the same fd has a proven external shared owner.

### Use an accumulator plus terminal state for multi-descriptor owners

`docker.versioning.build_cache.BuildCacheState`, the authoritative class returned by `open_build_cache_state()`, privately owns one `OwnedDescriptor` for each of its five descriptor fields: `persistent_fd`, `blobs_fd`, `tmp_fd`, `markers_fd`, and `transactions_fd`. Its existing constructor, `paths` field, five integer descriptor fields, signatures, and return identity remain compatible. `close()` first makes aggregate ownership terminal or detaches its complete owner collection, then submits every `owner.close` action to one `CleanupFailures(None)`. A repeat close is a no-op. This prevents skipped later cleanup and unsafe retries after ambiguous close failure.

Alternative: stop at the first close error. Rejected because descriptors are independent cleanup actions. Alternative: retain raw integers plus a second bespoke terminal ledger. Rejected because the shared owner already supplies the required state machine; private owners avoid duplicating it.

### Migrate unsafe directory handoff to `DirectoryDescriptor` where complete semantics match

Build-cache path walking and `_bootstrap_constructor_project_lock_parent()` in `docker/versioning/build_cache.py` use `DirectoryDescriptor.open_secure_path`, `open_directory`, `create_directory`, or `open_or_create_directory` only where basename, owner, mode, and no-follow contracts match exactly. Build-cache continues to derive paths, decide missing-component creation, validate cache-specific state, map `BuildCacheError`/`BuildTransactionError`, and own recursion and lock policy.

If a handoff cannot use `DirectoryDescriptor` without changing an established validation or pre-transfer ownership contract, it uses `OwnedDescriptor` as an explicit ledger. The child becomes owned before parent release; the parent becomes terminal before close; a parent failure triggers exactly one child release.

Alternative: wrap the existing parent close in `CleanupFailures(None)`. Rejected because precedence alone cannot repair an untracked child or prevent retry of an ambiguously failed parent close.

### Preserve correct direct-close categories

The implementation starts from an executable inventory and keeps these categories:

- **primitive:** `PosixDescriptorOps.close` and `PosixFileOps.close` remain direct raw delegates;
- **shared-owner internal release:** `OwnedDescriptor.close()` or an equivalent shared-owner implementation may invoke its injected close operation only after atomically making ownership terminal; executable gates reject the same call shape in ordinary consumers;
- **owned lifecycle:** every single consumer-owned release, including success paths, lifecycle methods, fds returned by `mkstemp`, and descriptors exposed through file objects, delegates to the shared owner;
- **file-object wrapper:** `fdopen(closefd=False)` is permitted only while a proven shared owner retains the wrapped fd; omitted or true `closefd` is forbidden in consumers;
- **protected cleanup:** active-primary or multiple independent releases use `CleanupFailures` with one or more shared-owner `close` actions;
- **ownership handoff:** a direct close is permitted only after the parent is terminal and the child is tracked before the potentially failing call.

There is no out-of-scope classification for production consumer-owned descriptor release, whether explicit or hidden behind a file object: every such lifecycle discovered by the repository inventory is migrated to the shared owner/cleanup lifecycle.

The convergence gate resolves symbol origins rather than matching spellings for explicit closes, `os.fdopen`, and `tempfile.mkstemp`. It follows aliased `os` imports, `from os import close as ...`, transitive local assignments of close callables, aliases of backend objects, and injected descriptor-operation values through parameters, constructor fields, and local assignments. A call whose resolved origin is the POSIX close primitive or an injected descriptor backend's `close` method is classified regardless of variable name; unresolved close-like calls in the audited production tree fail closed until explicitly proven non-descriptor. Fixtures cover each alias path, so renaming `os`, `close`, `ops`, `self._ops`, or an intermediate callable cannot evade the gate.

### Keep failure translation at domain boundaries

`docker.filesystem` continues to propagate raw ordinary close errors. Npm tree and lockfile helpers preserve their established boundary distinctions. `_write_lockfile()` maps staging-file open failure to `LockedNpmError("unsafe_staging_path", ...)`, and `_hash_file_entry()` maps file-entry open failure to `LockedNpmError("tree_type_mismatch", ...)`; both retain the original `OSError` as cause. Unsafe tree type also remains `LockedNpmError`. Stat, read, write, hashing, and close failures propagate raw, and migration does not reclassify those operational failures. Build/versioning boundaries retain their existing error types and wording. With an active primary, the raw close error is secondary. Without one, existing mapping or propagation remains unchanged except at `open_verified()`: its formerly escaping sole close `OSError` is intentionally mapped to `InstallError(f"cannot close artifact at {_path}: {exc}")` with that `OSError` as the direct cause, so callers and the CLI/result path use their controlled installation-failure behavior. Process-control exceptions remain unwrapped and follow shared precedence.

Alternative: make `OwnedDescriptor.close()` produce transaction/domain errors. Rejected because the foundation is domain-neutral and cannot select those classifications.

### Bind migration with RED, GREEN, introspection, and release gates

Each consumer migration begins with fault-injection tests for operation+close, sole close, interruption, repeated cleanup, and fd ledger behavior. Production changes follow only after the RED contract is demonstrated. Production-tree AST/API checks then prove dependency direction, reject every retained direct descriptor close outside the three allowed categories, reject consumer `fdopen(closefd=True)`, and reject any fd-returning factory result not transferred immediately to shared ownership. Focused suites and the complete project checks close the change.

## Risks / Trade-offs

- **[Risk] A broad search-and-replace wraps handoff closes and breaks ownership sequencing.** → Require the executable inventory and per-site classification before edits; migrate consumer releases to owners while preserving justified primitives and handoff operations.
- **[Risk] Constructing `OwnedDescriptor` after open can strand an fd if local argument preparation fails.** → Validate/compute labels and backend before `os.open`, then transfer immediately with no intervening operation.
- **[Risk] Directory foundation validation differs from a legacy domain's pre-transfer contract.** → Characterize validation order and ownership first; use `DirectoryDescriptor` only when complete semantics match, otherwise use `OwnedDescriptor` ledger mechanics.
- **[Risk] Close fault injection no longer patches module-local `os.close`.** → Inject `DescriptorOps` explicitly in new tests and retain compatibility seams only where existing public tests require them.
- **[Risk] A terminal multi-fd owner changes publicly observable attributes.** → Preserve public fields/signatures and implement terminal ownership privately; add compatibility introspection tests.
- **[Risk] Domain messages drift when raw close failure becomes secondary or when runtime sole close gains its authorized mapping.** → Pin exact primary type/message/cause and secondary diagnostics, plus the new close-specific `InstallError` and CLI/result output, before migration.

## Migration Plan

1. Add the repository-wide explicit/implicit descriptor-ownership inventory and focused RED tests for every selected defect.
2. Migrate every selected single-fd consumer lifecycle, including successful release and lifecycle methods, to `OwnedDescriptor` without moving domain operations.
3. Make multi-fd build-cache close terminal and exhaustive.
4. Migrate or ledger unsafe directory handoffs, one function at a time, with fd accounting.
5. Re-run domain characterization, AST dependency/close gates, type checking, full tests, and strict OpenSpec validation.

Rollback is source-only: each consumer migration can be reverted independently because no public API or persistent representation changes.
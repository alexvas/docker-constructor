## Context

See `proposal.md` for motivation. The repository now has a lightweight `docker.filesystem` foundation with `CleanupFailures`, `OwnedDescriptor`, `DirectoryDescriptor`, and injectable descriptor operations. A final ownership inventory still identifies explicit and implicit descriptor ownership in npm regular-file work, runtime artifact verification, build-context writing, artifact/build-cache traversal, materialization, rendering, persistent cache writing, snapshots, and project state.

The preliminary Phase 7 inventory also found fd-owning subprocess streams in `execution.py::_close_stream_pipes`, `SubprocessExecutor.run_streaming` (including its nested cleanup hooks), and `lifecycle.py::{close_descriptors,reap_process,terminate_and_reap,run_captured}`. These were not characterized by the original Phase 6 ledger. Repeated raw file-object close is currently treated as idempotent, and a deadline test explicitly expects repeat attempts. They are included in the new Phase 6B before convergence, not exempted as non-descriptor calls.

A direct descriptor-close call remains appropriate only inside raw POSIX adapters, inside `OwnedDescriptor.close()`, `OwnedStream.close()`, or an equivalent shared-owner implementation after it has made ownership terminal, and as the principal operation of a handoff whose releasing parent is already terminal and whose child is already tracked by the surrounding ownership-aware failure path. Every consumer-owned integer descriptor—including a success-path release outside `finally` and a lifecycle object's release method—uses `OwnedDescriptor`; an already fd-owning file object uses `OwnedStream` instead of giving its `fileno()` to a second owner. The defects include masking `finally` closes, implicit file-object ownership from `os.fdopen(..., closefd=True)`, unadopted raw descriptors from `tempfile.mkstemp()`, unowned single releases, nonterminal/multi-resource close loops, and handoffs whose ledger cannot account for an opened child after parent close failure.

The foundation remains below `docker.transactions` and every domain package. Npm and versioning leaves may import explicit `docker.filesystem` submodules, but `docker.filesystem` must not import them or acquire their path, cache, recursion, lock, publication, durability, or error-mapping policy.

## Goals / Non-Goals

**Goals:**
- Give every migrated raw descriptor one explicit live/transferred/release-attempted lifecycle.
- Preserve an active operation/domain failure over ordinary close failure and retain the close diagnostic.
- Attempt every independent release once even after another release fails.
- Make parent/child handoff mechanically leak-free and retry-free.
- Keep an executable production-tree classification for every explicit close, injected backend release, `fdopen` wrapper, fd-returning `mkstemp`, and subprocess-created fd-owning stream, permitting direct close only for a low-level POSIX primitive, gated shared-owner internal release, or verified ownership handoff.
- Give subprocess stream cleanup one synchronized release claim across concurrent and repeated cleanup paths, without double ownership or closing an fd while a reader can still use its number.
- Make filesystem-wrapper selection and lifecycle verification mandatory in documented rules for new descriptor-owning code.

**Non-Goals:**
- Remove every textual `os.close()` from production.
- Move regular-file validation, hashing, integrity, cache layout, traversal, recursion, lock policy, publication, or durability into `docker.filesystem`.
- Make domain modules import `docker.transactions` merely to reuse its L0 backend.
- Add a generic filesystem, path-policy, retry, recovery, or recursive removal API.
- Change existing consumer public APIs, persistent data, user-visible domain diagnostics, or success semantics beyond the expressly authorized lifecycle corrections. The only new public foundation surface is the bounded `OwnedStream` wrapper.

## Decisions

### Use `OwnedDescriptor` for every single consumer-owned lifecycle

After a successful raw `os.open`, npm lockfile writing, npm tree hashing, runtime artifact verification, private build-context writing, snapshot destination copy, and artifact-cache temporary creation transfer the fd immediately into directly constructed `OwnedDescriptor(PosixDescriptorOps(), fd, label=...)`. `ValidatedProjectState` likewise stores or delegates to one private `OwnedDescriptor` while preserving its public construction and descriptor access contract. Operations continue to use the live `fd` property.

With an active primary, `CleanupFailures(primary)` runs `owner.close` alongside any independent cleanup such as unlink. On a successful path, `owner.close()` reports a sole close failure while retaining terminal at-most-once state. A bare `finally: close()` is never considered a sole-failure exception because an operation failure may already be active.

This reuses only ownership and close precedence. It does not add regular-file open/read/stat/hash APIs to the foundation. Alternative: add `RegularFileDescriptor`. Rejected because these consumers have different type, ownership, mode, integrity, and domain policies, and only lifecycle is common. Alternative: retain bare success-path or `finally` closes. Rejected because they bypass uniform terminal ownership, and a `finally` close can additionally mask an active failure.

### Make implicit file-object adoption explicit

Every raw fd returned by `tempfile.mkstemp()` or another integer-fd factory transfers immediately to `OwnedDescriptor`. Consumers that need a Python file object construct it with `os.fdopen(owner.fd, ..., closefd=False)`, so closing the file object cannot implicitly release the descriptor and the shared owner remains the sole release authority. File-object flush/close errors remain operation failures; owner release runs afterward through `CleanupFailures(primary)`, making descriptor-close failure secondary and terminal. The migration covers artifact materialization, effective-inventory rendering, and HTTP cache persistence while preserving temporary-name cleanup, publication order, bytes, modes, and domain mapping.

The convergence gate resolves aliases of `os.fdopen` and `tempfile.mkstemp`, treats omitted `closefd` as `True`, rejects consumer `closefd=True`, and requires every fd component returned by `mkstemp()` to transfer to a shared owner before another fallible operation. `closefd=False` is accepted only when the same fd has a proven external shared owner.

### Own already fd-owning streams without a second raw-fd owner

Add `OwnedStream` in `docker.filesystem.streams`, imported explicitly without aggregate re-exports. It adopts one already-open file object and exclusively controls its release; it does not open streams, derive paths, launch processes, drain readers, or decide shutdown order. Its bounded surface is construction with a prevalidated non-empty label, borrowed live stream access, terminal-state inspection, `close()`, and context-manager cleanup using shared precedence. It needs no raw-fd accessor, generic proxy API, detach/retry API, or process-aware factory. Stream access is borrowed and grants no independent close authority. A descriptor-less injected test stream follows the same owner lifecycle without requiring `fileno()`.

A wrapper is prepared/adopted immediately after a successful subprocess launch, before callbacks, reader startup, supervisor startup, or other fallible setup. Adoption of the pair is failure-safe: if setup/adoption of the second stream fails, the first owner and any not-yet-adopted stream are cleaned up once and the launched child is boundedly reaped. Ownership and cleanup protection must cover the entire post-launch setup, not just the existing collection try block. Missing streams and injected process/stream compatibility are characterized before changing validation.

`OwnedStream.close()` synchronizes only the live-to-release-attempted claim, makes it terminal before invoking the underlying stream's `close()`, and invokes that action outside the claim lock. Repeat, concurrent, or reentrant calls issue no second underlying close; a losing concurrent call does not imply the winning close has finished. The domain cleanup coordinator must await the winning cleanup outcome before returning and retain its failure once. Neither the same stream nor its raw `fileno()` receives a second owner, and neither GC nor context-manager cleanup is relied on as the explicit release path. An underlying stream close may fail ambiguously; no raw-close fallback or retry is permitted.

The same owner pair is retained for the whole process execution and reused by reader-failure, deadline, interruption, reaping, and finalization hooks. Creating a new owner each time `_close_stream_pipes()` or `close_descriptors()` runs would reset terminal state and is forbidden. Preserve existing consumer signatures and process-result interfaces through characterized internal adapters; do not make a hidden global stream registry the lifecycle authority.

Release-attempt synchronization does not make reading thread-safe or make buffered close bounded. Reader-stop, process terminate/remove/reap, deadlines, and safe close ordering remain in the subprocess layer. Real-pipe tests must pin bounded completion when a descendant retains a write end and when readers fail or cancellation races with timeout. Where blocking buffered reads prevent that guarantee, use domain-local cancellation-aware/non-blocking reads and coordinated worker shutdown; do not close raw fd numbers out from under readers. Preserve output tails, stream decoding, diagnostic redaction, and supported worker-join guarantees. Descriptor-less test streams must not be presented as evidence that production fd reuse is safe.

Independent stdout/stderr releases use `CleanupFailures` with an explicit ordinary-error policy. Preserve an active ordinary reader/wait/domain failure over ordinary close failure; retain process-control identity and shared higher-precedence interruption/defect behavior. Domain adapters retain their controlled result/error shapes, close diagnostics, ordering, and bounded sanitization. Do not expose raw URL/secret-bearing secondary notes merely because foundation diagnostics now retain failure objects.

Alternative: adopt `pipe.fileno()` into `OwnedDescriptor` while leaving the file object fd-owning. Rejected because that creates two release authorities. Alternative: allowlist subprocess `.close()` as non-descriptor cleanup. Rejected because it releases a real pipe fd. Alternative: retry `pipe.close()` because file objects are usually idempotent. Rejected because faulted close does not establish successful release, and repeated/concurrent cleanup must have one explicit terminal owner.

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
- **shared-owner internal release:** `OwnedDescriptor.close()`, `OwnedStream.close()`, or an equivalent shared-owner implementation may invoke its injected close operation only after making ownership terminal; stream release additionally requires a synchronized claim; executable gates reject the same call shape in ordinary consumers;
- **owned lifecycle:** every single consumer-owned release, including success paths, lifecycle methods, fds returned by `mkstemp`, and descriptors exposed through file objects, delegates to the shared owner;
- **file-object wrapper:** `fdopen(closefd=False)` is permitted only while a proven shared owner retains the wrapped fd; omitted or true `closefd` is forbidden in consumers;
- **already fd-owning stream:** subprocess-created file objects transfer release authority to `OwnedStream`; their underlying file-object close is a shared-owner internal release, not a fourth direct-close exception. This does not permit consumer `fdopen(closefd=True)`;
- **protected cleanup:** active-primary or multiple independent releases use `CleanupFailures` with one or more shared-owner `close` actions;
- **ownership handoff:** a direct close is permitted only after the parent is terminal and the child is tracked before the potentially failing call.

There is no out-of-scope classification for production consumer-owned descriptor release, whether explicit or hidden behind a file object: every such lifecycle discovered by the repository inventory is migrated to the shared owner/cleanup lifecycle.

The convergence gate resolves symbol origins rather than matching spellings for explicit closes, `os.fdopen`, and `tempfile.mkstemp`. It follows aliased `os` imports, `from os import close as ...`, transitive local assignments of close callables, aliases of backend objects, and injected descriptor-operation values through parameters, constructor fields, and local assignments. A call whose resolved origin is the POSIX close primitive or an injected descriptor backend's `close` method is classified regardless of variable name; unresolved close-like calls in the audited production tree fail closed until explicitly proven non-descriptor. Fixtures cover each alias path, so renaming `os`, `close`, `ops`, `self._ops`, or an intermediate callable cannot evade the gate.

### Keep failure translation at domain boundaries

`docker.filesystem` continues to propagate raw ordinary close errors. Npm tree and lockfile helpers preserve their established boundary distinctions. `_write_lockfile()` maps staging-file open failure to `LockedNpmError("unsafe_staging_path", ...)`, and `_hash_file_entry()` maps file-entry open failure to `LockedNpmError("tree_type_mismatch", ...)`; both retain the original `OSError` as cause. Unsafe tree type also remains `LockedNpmError`. Stat, read, write, hashing, and close failures propagate raw, and migration does not reclassify those operational failures. Build/versioning boundaries retain their existing error types and wording. With an active primary, the raw close error is secondary. Without one, existing mapping or propagation remains unchanged except at `open_verified()`: its formerly escaping sole close `OSError` is intentionally mapped to `InstallError(f"cannot close artifact at {_path}: {exc}")` with that `OSError` as the direct cause, so callers and the CLI/result path use their controlled installation-failure behavior. Process-control exceptions remain unwrapped and follow shared precedence.

Alternative: make `OwnedDescriptor.close()` produce transaction/domain errors. Rejected because the foundation is domain-neutral and cannot select those classifications.

### Establish durable developer rules and executable enforcement

Create `docs/filesystem-lifecycle.md` as the developer contract, with executable examples for immediate raw-fd adoption, owned `fdopen(closefd=False)` wrappers, secure directory ownership, already fd-owning stream adoption, and independent cleanup. Distinguish an owned resource from a borrowed descriptor/stream and a proven descriptor-less object. Document `OwnedDescriptor.detach()` transfer responsibilities, validation before acquisition, terminal-before-close, no retries, ordinary-error policy, full shared precedence, synchronized stream release versus reader coordination, and domain-neutral dependency direction. Raw close remains restricted to the existing three categories. Document file-object cleanup separately from descriptor release; `OwnedStream` is not an alternative to the established `closefd=False` rule for raw fds.

Create root `AGENTS.md` with a concise mandatory rule and a link to the guide; add the same planning obligation and guide reference to `openspec/config.yaml` project context/rules, preserving any existing instructions. Every new fd acquisition (including open/dup/pipe/mkstemp and fd-owning factory results), transfer, and release must identify one filesystem lifecycle owner and include operation-plus-close, sole-close, interruption, repeat, independent-cleanup, and handoff/concurrency tests where applicable. Borrowed resources grant no close authority. Add discoverable guide links to all three README language variants and explain subprocess shutdown/ownership in `docs/npm-environment-assembler.md`. These are implementation tasks, not changes to project instructions during this planning update.

The executable convergence gate must scan newly added production modules, not just named migrated functions. Resolve subprocess factory/PIPE aliases, stream attributes, assigned close callables, and owner aliases; reject unowned fd-owning streams, duplicate owners, reset-per-cleanup owners, nonterminal/concurrent releases, and raw-fileno fallback. Do not accept a filesystem-like class name or import as ownership proof. Update foundation API/dependency assertions for exactly the new bounded stream surface; imports of subprocess, npm, versioning, transactions, or domain policy from the foundation remain forbidden. Documentation examples and links are tested so prose cannot diverge from the implemented API.

### Bind migration with RED, GREEN, introspection, and release gates

Each consumer migration begins with fault-injection tests for operation+close, sole close, interruption, repeated cleanup, and fd ledger behavior. Production changes follow only after the RED contract is demonstrated. Production-tree AST/API checks then prove dependency direction, reject every retained direct descriptor close outside the three allowed categories, reject consumer `fdopen(closefd=True)`, and reject any fd-returning factory result not transferred immediately to shared ownership. Phase 6B separately characterizes and migrates the newly inventoried subprocess family before Phase 7. Its documentation and expanded convergence fixtures are required deliverables; earlier checked Phase 1–6 obligations and their evidence remain historical and are not retroactively expanded. Focused suites and the complete project checks close the change.

## Risks / Trade-offs

- **[Risk] A broad search-and-replace wraps handoff closes and breaks ownership sequencing.** → Require the executable inventory and per-site classification before edits; migrate consumer releases to owners while preserving justified primitives and handoff operations.
- **[Risk] Constructing `OwnedDescriptor` after open can strand an fd if local argument preparation fails.** → Validate/compute labels and backend before `os.open`, then transfer immediately with no intervening operation.
- **[Risk] Directory foundation validation differs from a legacy domain's pre-transfer contract.** → Characterize validation order and ownership first; use `DirectoryDescriptor` only when complete semantics match, otherwise use `OwnedDescriptor` ledger mechanics.
- **[Risk] Close fault injection no longer patches module-local `os.close`.** → Inject `DescriptorOps` explicitly in new tests and retain compatibility seams only where existing public tests require them.
- **[Risk] A terminal multi-fd owner changes publicly observable attributes.** → Preserve public fields/signatures and implement terminal ownership privately; add compatibility introspection tests.
- **[Risk] Domain messages drift when raw close failure becomes secondary or when runtime sole close gains its authorized mapping.** → Pin exact primary type/message/cause and secondary diagnostics, plus the new close-specific `InstallError` and CLI/result output, before migration.

- **[Risk] A stream wrapper and an integer-fd owner close the same resource.** → Give already fd-owning streams only `OwnedStream`; prohibit raw-fileno adoption and raw-close fallbacks in tests and the ledger.
- **[Risk] Concurrent close is claimed once but blocks on a live buffered reader.** → Keep the claim lock narrow, coordinate readers and process shutdown in domain code, and require real-pipe deadline/fd-reuse tests before migration is accepted.
- **[Risk] A timeout winner's close failure is lost or a losing cleanup returns too soon.** → Share the owner pair and await/publish the winning cleanup outcome without repeat close or duplicate diagnostics.
- **[Risk] Shared diagnostic attachment leaks unsanitized subprocess failures.** → Characterize and retain domain-local bounded redaction for all public notes/results, including unexpected defects and interruptions.

## Migration Plan

1. Add the repository-wide explicit/implicit descriptor-ownership inventory and focused RED tests for every selected defect.
2. Migrate every selected single-fd consumer lifecycle, including successful release and lifecycle methods, to `OwnedDescriptor` without moving domain operations.
3. Make multi-fd build-cache close terminal and exhaustive.
4. Migrate or ledger unsafe directory handoffs, one function at a time, with fd accounting.
5. Execute Phase 6B RED → GREEN → INTROSPECT → VALIDATE for subprocess stream ownership, safe reader coordination, compatibility diagnostics, and the bounded `OwnedStream` API.
6. Publish developer lifecycle rules, OpenSpec planning guidance, assembler shutdown documentation, and tested README links as Phase 6B deliverables.
7. Re-run domain characterization, the expanded complete-tree AST/dependency/ownership gates, type checking, full tests, and strict OpenSpec validation in Phase 7. Newly discovered families not characterized by Phases 1–6 or 6B still require a planning update rather than an uncharacterized refactor.

Rollback is source-only: each consumer migration can be reverted independently because no public API or persistent representation changes.
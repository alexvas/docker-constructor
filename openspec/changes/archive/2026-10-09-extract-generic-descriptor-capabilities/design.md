## Context

See `proposal.md` for motivation. Three leaf areas currently own similar raw directory-descriptor mechanics: npm storage, npm tree inspection, and shared cache storage. `docker.transactions.capabilities` already implements secure walking and primary-preserving release, but importing any `docker.transactions` submodule executes an aggregate `__init__` that loads the L0–L2 substrate. `cache_storage.py` also has an AST-enforced acyclic-leaf import allowlist.

The unsafe shape is not limited to `finally: os.close(fd)`. A handoff such as opening `next_fd`, closing `current_fd`, and only then assigning the child can retry an ambiguously failed parent close and leak the already-open child. POSIX does not make retrying a failed close safe because the descriptor number may already have been released and reused.

## Goals / Non-Goals

**Goals:**
- Establish one lightweight foundation for directory descriptor ownership, secure traversal, child operations, and release precedence.
- Make descriptor ownership and transfer explicit and close-at-most-once.
- Let leaf modules reuse the foundation without loading `docker.transactions`.
- Reuse the foundation from transaction capabilities rather than create competing ownership models.
- Present one typed transaction L1 boundary for operational descriptor failures without weakening ownership or cleanup guarantees.
- Preserve existing domain paths, layouts, and policy decisions; preserve domain error types except where this change intentionally normalizes an adopted transaction capability's operational failure to the typed L1 boundary.

**Non-Goals:**
- Create a generic VFS, tree transaction, recursive removal API, or path-policy engine.
- Move npm namespace layout, cache-root resolution, domain traversal, absence handling, or error wording into the foundation.
- Move transaction locking, regular-file publication, durability, or recovery contracts.
- Convert every project descriptor site in one change; consumers outside npm storage/tree and cache storage remain a later audit.
- Change public CLI behavior, persistent layouts, identity, publication, or lock semantics.

## Decisions

### Place the foundation outside `docker.transactions`

Create the lightweight top-level package `docker.filesystem` whose production dependencies are limited to the standard library and its own failure-diagnostics primitive. `docker.transactions` may import this foundation; the foundation must never import `docker.transactions` or a domain package. `docker.filesystem.__init__` remains empty and performs no aggregate imports, so consumers import the required submodule explicitly.

```text
npm_environment.storage ─┐
npm_environment.tree ────┼──▶ descriptor foundation ──▶ os/stat + diagnostics
versioning.cache_storage ─┘              ▲
                                         │
transactions.capabilities ───────────────┘
```

This preserves acyclic direction while avoiding the runtime and conceptual cost of the transaction aggregate. Alternative: make `docker.transactions.__init__` lazy. Rejected because it changes aggregate import behavior while leaving leaf modules conceptually coupled to transaction L0–L2. Alternative: move only `CleanupFailures`. Rejected as the primary design because it fixes masking but retains duplicated and unsafe ownership handoffs.

### Keep the public foundation directory-focused and minimal

The initial public surface consists of:

- an injectable descriptor-operations protocol and production POSIX implementation sufficient for directory operations;
- an owned descriptor abstraction with live, transferred, and release-attempted terminal states;
- a directory descriptor capability;
- secure absolute-path walking;
- descriptor adoption and explicit ownership transfer;
- basename validation;
- no-follow child directory open, exclusive create, and create-or-open with explicit validation/mode policy;
- primitive child stat/list/unlink/rmdir operations only where required by migrated domain traversal;
- lightweight typed operation/validation errors carrying their original causes.

The package and API names are part of this design contract:

```text
docker/filesystem/
├── __init__.py       # empty; no aggregate imports or re-exports
├── cleanup.py        # cleanup precedence and secondary diagnostics
├── operations.py     # injectable descriptor operations
└── descriptors.py    # owned and directory descriptors
```

`docker.filesystem.cleanup` exposes the existing contracts under their stable names:

```python
class CleanupFailures:
    def __init__(self, primary: BaseException | None) -> None: ...
    def run(
        self,
        action: Callable[[], object],
        *,
        ordinary: tuple[type[Exception], ...],
    ) -> None: ...
    def complete(self) -> Exception | None: ...

def attach_secondary(
    primary: BaseException,
    secondary: list[BaseException],
) -> None: ...

def carry_secondary_diagnostics(
    target: BaseException,
    source: BaseException,
) -> None: ...
```

`docker.filesystem.operations` exposes:

```python
class DescriptorOps(Protocol):
    def openat(
        self, directory_fd: int | None, name: str, flags: int, mode: int = 0,
    ) -> int: ...
    def close(self, fd: int) -> None: ...
    def fstat(self, fd: int) -> os.stat_result: ...
    def fchmod(self, fd: int, mode: int) -> None: ...
    def mkdirat(self, directory_fd: int, name: str, mode: int) -> None: ...
    def statat(
        self, directory_fd: int, name: str, *, follow_symlinks: bool,
    ) -> os.stat_result: ...
    def listdir(self, fd: int) -> list[str]: ...
    def unlinkat(self, directory_fd: int, name: str) -> None: ...
    def rmdirat(self, directory_fd: int, name: str) -> None: ...

class PosixDescriptorOps:
    # Production DescriptorOps implementation.
    ...
```

`docker.filesystem.descriptors` exposes:

```python
class DescriptorError(Exception):
    stage: str
    cause: BaseException | None

    def __init__(
        self,
        stage: str,
        message: str,
        *,
        cause: BaseException | None = None,
    ) -> None: ...

class UnsafeDescriptorError(DescriptorError):
    # Inherits DescriptorError.__init__ unchanged.
    ...

class OwnedDescriptor:
    def __init__(
        self,
        ops: DescriptorOps,
        fd: int,
        *,
        label: str,
    ) -> None: ...

    @property
    def fd(self) -> int: ...
    @property
    def label(self) -> str: ...
    @property
    def released(self) -> bool: ...
    def close(self) -> None: ...
    def detach(self) -> int: ...
    def __enter__(self) -> Self: ...
    def __exit__(self, exc_type, exc, tb) -> None: ...

class DirectoryDescriptor(OwnedDescriptor):
    @classmethod
    def open_secure_path(
        cls,
        ops: DescriptorOps,
        path: str | os.PathLike[str],
        *,
        label: str | None = None,
        require_owner: bool = True,
    ) -> DirectoryDescriptor: ...

    @classmethod
    def adopt(
        cls,
        ops: DescriptorOps,
        fd: int,
        *,
        label: str,
        require_owner: bool = True,
    ) -> DirectoryDescriptor: ...

    def child_basename(self, name: str) -> str: ...
    def open_directory(
        self,
        name: str,
        *,
        label: str | None = None,
        require_owner: bool = True,
    ) -> DirectoryDescriptor: ...
    def create_directory(
        self,
        name: str,
        *,
        mode: int,
        label: str | None = None,
        require_owner: bool = True,
    ) -> DirectoryDescriptor: ...
    def open_or_create_directory(
        self,
        name: str,
        *,
        mode: int,
        label: str | None = None,
        require_owner: bool = True,
    ) -> DirectoryDescriptor: ...
    def stat_child(
        self, name: str, *, follow_symlinks: bool = False,
    ) -> os.stat_result: ...
    def list_names(self) -> tuple[str, ...]: ...
    def unlink_child(self, name: str) -> None: ...
    def remove_child_directory(self, name: str) -> None: ...
```

`DescriptorError(stage, message, *, cause=None)` stores `stage` and `cause`, uses `message` as its exception text, and sets `__cause__` when a cause is supplied. `UnsafeDescriptorError` inherits that constructor without widening it.

`OwnedDescriptor(ops, fd, *, label)` is directly constructible and is the supported lifecycle-test seam. It validates `fd` as an `int >= 0` and `label` as a non-empty string before accepting ownership; rejection leaves ownership with the caller. Successful construction unconditionally transfers ownership to the instance but performs no `fstat`, type, ownership, or mode validation. `close()` follows Python/POSIX terminology and is the irreversible release attempt. `detach()` explicitly transfers the raw descriptor without closing it.

`DirectoryDescriptor` is not directly constructible: its initializer is module-internal and guarded by an unexported authority token. A direct call must raise `TypeError` before accepting ownership. Its only public construction seams are `open_secure_path()` and `adopt()`. After validating the ordinary `ops`, `fd`, and `label` arguments, `adopt()` consumes ownership of the raw fd. If directory-type or requested effective-owner validation fails, it releases that fd exactly once under shared cleanup precedence; on success the returned `DirectoryDescriptor` is the sole owner. Tests that need an existing directory descriptor construct it through `DirectoryDescriptor.adopt(fake_ops, fd, label=...)`, not through the internal initializer.

`open_directory()` requires an existing child, `create_directory()` creates exclusively, and `open_or_create_directory()` deliberately permits either outcome before validating and returning authority. The remaining child operations each target one validated basename. No method accepts an arbitrary descendant path where one basename suffices, and no public recursive removal operation is introduced. Changes to these public module paths, names, signatures, or construction-authority rules require a design revision rather than an implementation-local choice.

Alternative: extract all existing `transactions.capabilities`, including regular-file authority and transaction errors. Rejected because file link-count, publication stage, and directory-token semantics are coupled to L1/L2 transaction contracts and are unnecessary for the leaf consumers.

### Model release as an irreversible attempt

A capability marks itself released before invoking close. A failed close is never retried by the same capability. Explicit detach/transfer makes the source terminal without closing and is used only at compatibility boundaries or when ownership moves to another capability.

Context-manager cleanup uses the shared failure precedence: an ordinary declared close failure is secondary to an active primary; without a primary it propagates as the sole failure; interruption and unexpected defects retain their defined precedence. Multi-descriptor cleanup attempts every independent release once.

The error boundary follows ownership state. Before adoption succeeds, the caller still owns the raw descriptor, releases it through the injected descriptor operations, and observes an ordinary raw `OSError` from that release. After adoption succeeds, the capability is the sole owner and every release must go through its `close()` method. A transaction capability normalizes an operational release failure to `TransactionError(STAGE_CLOSE, ..., cause=raw_error)`, and a consumer must preserve that typed close error rather than unwrap it merely to reproduce historical raw-descriptor behavior. With no active primary the typed close error propagates unchanged; with an active primary it remains typed and is attached as secondary diagnostic context. Its original POSIX failure remains available through both `.cause` and `.__cause__`, while a process-control exception propagates unchanged.

`docker.versioning.rendering.write_effective_build()` is an explicit ownership-split consumer of this rule. If generated-directory adoption fails, it releases the still caller-owned descriptor directly. If adoption succeeds, it releases only through `DirectoryCapability.close()`: a sole post-adoption close failure is the typed close-stage transaction error, and a simultaneous publication failure remains primary with that typed close error attached as secondary context.

### Preserve typed failures across consumer boundaries

A successful L1 or lock-layer normalization is not undone merely to reproduce a historical raw-descriptor implementation detail. An L2 adapter, cleanup accumulator, lock wrapper, or domain boundary preserves the typed exception unchanged when no domain translation is required. When a domain exception is required, it chains directly from the typed exception with `raise DomainError(...) from exc`; it does not skip that layer by chaining from or raising `exc.cause`. This keeps operation stage, the exact POSIX cause, and attached secondary cleanup diagnostics on one exception graph without copying diagnostics between exception objects.

Cause inspection remains permitted when it drives behavior rather than exception replacement: examples include recognizing `FileNotFoundError`, examining `errno`, distinguishing a no-follow safety rejection, or selecting a documented domain result. Inspection alone does not authorize `raise exc.cause`, `return exc.cause`, or substituting the cause into an aggregate. A raw `OSError` also remains valid before capability adoption, at an injected POSIX operation boundary, or under a separately documented public contract whose tests require the exact raw exception type. Every such compatibility exception must identify that contract and must not be inferred solely from pre-refactor implementation behavior.

This rule applies to transaction errors, lock errors, their cleanup/release paths, and the npm publication and versioning build-cache, artifact-cache, build-cleanup, project-state, effective-state, and rendering adapters. A repository-wide executable inventory classifies each cause access as inspect-only, typed propagation, domain wrapping, pre-adoption raw cleanup, or a justified raw-contract exception. New cause-unwrapping sites are rejected unless they carry that explicit justification.

A secure walk retains both sides of a parent-to-child handoff until the child is validated. If parent release fails, the child is released once and is not returned. This directly addresses the leak/retry shape in the current npm and cache walkers.

Alternative: rely on `contextlib.closing` or plain `finally`. Rejected because neither supplies close-at-most-once state, ownership transfer, secondary diagnostics, nor multi-resource precedence.

### Keep domain sequencing and translation at call sites

Npm modules continue to own safe digest/name requirements, assembler layout, recursive manifest traversal/removal, idempotent staging absence, and `LockedNpmError` reasons. Cache storage continues to own root selection, XDG behavior, mode policy, recovery guidance, and `CacheStorageError`/`InventoryError` mapping.

The foundation reports bounded generic stages and raw causes; consumers translate at their existing boundaries. It never selects user-facing remediation or decides whether absence is success.

### Reuse the foundation from transactions without breaking imports

Existing `docker.transactions` public imports remain compatible. Its directory capability should delegate ownership and secure-directory mechanics to the foundation or become a thin specialization/re-export. Transaction-specific regular-file capabilities, authority tokens, errors, locking, and L2 contracts remain in `docker.transactions`.

`DirectoryCapability.from_fd(ops, fd, label)` is a permanent public transaction compatibility API with its current signature and pre-transfer validation semantics. It performs transaction-specific `fstat`, directory-type, and effective-owner validation while the caller still owns `fd`. If `fstat` or either validation fails, the caller retains ownership and `from_fd()` must not close the descriptor. Only after every validation succeeds may ownership transfer exactly once through a permanent module-internal validated-transfer seam that constructs the shared descriptor owner without repeating validation. Calling public `DirectoryDescriptor.adopt()` from this point is not an equivalent implementation because its repeated consuming validation could fail and close an fd that the observable `from_fd()` contract still treats as caller-owned on failure.

This compatibility rule intentionally differs from public `DirectoryDescriptor.adopt()`: `adopt()` is also permanent, keeps its staged consuming contract, and closes an accepted fd when its own directory/owner validation fails. The permanent validated-transfer seam is internal rather than a third public constructor and may be invoked only after the compatibility layer has completed validation. It transfers ownership without adding another ownership state machine or secure walker.

Only a migration shim used while moving callers or implementations may be temporary. `DirectoryCapability.from_fd()`, `DirectoryDescriptor.adopt()`, and the internal validated-transfer seam are not migration shims and must remain after migration. The small transaction-specific pre-transfer validator is not a second ownership implementation: it owns no descriptor, closes no validation failure, and transfers exactly once after success. Existing transaction fault-injection behavior remains available through the shared operations protocol.

### Normalize operational failures at the transaction L1 boundary

The transaction capability API classifies failures by meaning rather than by the POSIX call site that happened to expose them:

- capability misuse and authority violations remain `CapabilityError`;
- unsafe filesystem objects retain the existing dedicated safety error;
- operational open, stat, read, and close failures become `TransactionError` with a stable stage and the original `OSError` as `cause` and direct `__cause__`;
- `KeyboardInterrupt`, `SystemExit`, and other process-control exceptions propagate unchanged.

Add the public constant `STAGE_OPEN = "open-directory"` rather than overloading `STAGE_READ`; retain `STAGE_VALIDATE`, `STAGE_READ`, and `STAGE_CLOSE` for validation, regular-file reading, and release respectively. Root, intermediate, leaf, direct-path, and secure-path operational open failures use `TransactionError(STAGE_OPEN, ...)`. `ELOOP` or `ENOTDIR` from a no-follow directory open is excluded: platforms may report either for a symlink or non-directory safety rejection, so both retain the existing `CapabilityError` classification with the raw error as direct `__cause__`. This does not add a `.cause` attribute to `CapabilityError`. Existing error text should remain stable where it is already coherent, but exception type, stage, cause chaining, and ownership are authoritative compatibility properties for this intentional normalization.

`DirectoryCapability.from_fd()` still validates while the caller owns the descriptor. A failed `fstat` is wrapped as a validation-stage `TransactionError`, but no close is attempted and ownership remains with the caller. Directory type and owner-policy rejection remain non-operational capability/safety failures under their existing classification. Translation therefore does not imply adoption.

`DirectoryCapability.close()` and `FileCapability.close()` translate an ordinary close `OSError` into `TransactionError(STAGE_CLOSE, ...)` after the irreversible release transition. A second close remains a no-op. `DirectoryCapability` context-manager exit without an active failure exposes that typed close error; with an active failure, the active failure stays primary and the typed close error, whose cause is the raw `OSError`, is attached as secondary diagnostic context. `FileCapability` gains no context-manager API and is covered through direct `close()` calls. This keeps raw POSIX details inspectable without leaking them as the public L1 exception.

`FileCapability.read_all()` translates read failures to `TransactionError(STAGE_READ, ...)`. The normalization is bounded to public L1 capability methods; injected L0 `PosixFileOps` continues to preserve raw `OSError`, and L2/domain mappings remain responsible for translating typed L1 failures at their existing boundaries.

Changing the close exception type also changes every direct and indirect boundary that invokes or translates `DirectoryCapability.close()` or `FileCapability.close()`. Every cleanup accumulator, direct close, wrapper, `except OSError` suppression policy, and downstream translation must be inventoried. A `CleanupFailures.run(capability.close, ordinary=(OSError, ...))` site must recognize the typed close error as ordinary so it cannot displace an active primary. Direct handlers must recognize only `TransactionError` at `STAGE_CLOSE`, preserving deliberate suppression or propagation without swallowing unrelated transaction failures. In particular, npm publication's `read_index()`, `_append_index()`, and `_read_private_regular()` retain their existing best-effort/read-primary policies and continue to propagate process-control exceptions.

L2 mappings require the same care. `RegularFileContracts.validated_read()` must pass an already typed close error through unchanged rather than wrapping it in a second `TransactionError`; a sole close failure therefore retains the original `OSError` as both `.cause` and `.__cause__`. When reading has already failed, that read failure remains primary and the typed close failure remains secondary. Raw `OSError` translation remains only at boundaries that can still receive an L0/raw failure.

Factories and domain adapters that currently catch a raw `OSError` from capability construction or validation must accept the typed transaction error where appropriate and inspect or carry its raw `cause` where their public mapping requires the POSIX detail. They must retain `OSError` handling wherever the same boundary still directly invokes POSIX operations. This adaptation includes, but is not limited to, npm publication and versioning build-cache, artifact-cache, build-cleanup, and effective-state paths; an executable repository-wide inventory prevents a fixed example list from becoming incomplete.

Alternative: normalize only path-open failures. Rejected because `from_fd().fstat()`, reads, and closes would continue exposing backend details and leave the public L1 contract dependent on which operation failed. Alternative: convert all failures to `CapabilityError`. Rejected because capability misuse is categorically different from an operational failure and `TransactionError` already carries stage, cause, and secondary diagnostics.

### Amend the cache-storage allowlist narrowly

The architecture test will permit only the concrete lightweight foundation import used by `cache_storage.py`; it will continue to reject `docker.transactions` and higher-level cache consumers. A companion dependency-direction test will require the foundation to avoid domain and transaction imports, so widening the allowlist does not silently invert ownership.

## Risks / Trade-offs

- **[Risk] The extracted API grows into a generic filesystem abstraction.** → Keep operations descriptor-relative and basename-only; prohibit recursion, durability, locking, retries, and domain paths in tests and documentation.
- **[Risk] Refactoring transaction capabilities changes established failure behavior.** → Pin ownership and interruption behavior before extraction, then explicitly replace raw operational `OSError` expectations with the uniform typed L1 contract while retaining causes and secondary diagnostics.
- **[Risk] Domain exception wording or causes change during migration.** → Preserve mapping at existing boundaries and add characterization tests for reason codes, causes, and recovery text.
- **[Risk] Test patches against module-local `os` calls stop injecting failures.** → Introduce explicit operations injection and migrate tests to that seam before replacing raw calls.
- **[Risk] Nested capabilities retain parents longer than before.** → Define ownership and release order explicitly; secure walk returns only the final capability and retains no intermediate descriptor.
- **[Risk] Moving diagnostic helpers creates import cycles or compatibility breaks.** → Keep diagnostics below both foundation and transactions, retain old transaction import paths through re-export, and add import-isolation tests.

## Migration Plan

1. Characterize current domain behavior and add red tests for masking close, failed handoff, at-most-once release, and child leak prevention.
2. Extract lightweight cleanup diagnostics needed by both the new foundation and transactions while preserving existing import paths.
3. Introduce the descriptor operations seam and owned directory capability with isolated unit tests.
4. Refactor transaction directory capabilities onto the foundation and run existing L0/L1/L2 tests.
5. Normalize operational open, stat, read, and close failures at the public transaction L1 boundary while preserving ownership, interruption, and cleanup precedence.
6. Migrate npm storage, then npm tree traversal, preserving domain-level behavior and tests.
7. Migrate cache storage and narrowly amend its import allowlist plus dependency-direction checks.
8. Run a final AST/runtime import audit to confirm migrated modules have no raw owned-directory close lifecycle and importing the foundation does not load `docker.transactions`.

Rollback is source-compatible: retain adapters until all consumers pass, and revert individual consumer migrations without changing persistent data. No data migration is required.

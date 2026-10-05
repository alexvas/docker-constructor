## Context

See `proposal.md` for motivation. The repository has three independent Python `flock` implementations and several overlapping durable-write/read helpers with different validation and failure behavior. Build state currently replaces one mutable `committed-build.json` and then removes superseded blobs without durable evidence that cleanup remains incomplete after interruption.

The checkout build lock is project-scoped and fail-fast. Runtime-artifact and npm-environment locks are identity-scoped and blocking. Runtime artifacts and npm outputs are immutable publications and do not need the build-generation cleanup protocol.

## Goals / Non-Goals

**Goals:**
- Provide one security-reviewed implementation of owner-private file locks and matching durable filesystem operations.
- Replace mutable committed build state with immutable generations whose retained predecessor is sufficient to recover deferred cleanup.
- Attempt all cleanup candidates, preserve recovery evidence on any failure, and reject a later build until recovery completes.
- Preserve every consumer's existing lock scope, waiting policy, cache ownership, publication semantics, and diagnostics except for the intentional build-cleanup operational failure.
- Give later multi-target and externally mutable-state work narrow lock, regular-file, and canonical JSON primitives without claiming a shared envelope or stronger generic transaction semantics.

**Non-Goals:**
- Provide general ACID transactions, distributed locks, leases, lock expiry, Windows locking, or network-filesystem guarantees beyond supported `flock` semantics.
- Provide a generic mutable single-authority recovery engine.
- Move digest validation, TTL policy, tree evidence, quarantine, snapshot lifecycle, or domain path derivation into generic infrastructure.
- Implement multi-target lockfile publication or settings CAS in this change.
- Migrate the old development build-cache format at runtime.
- Provide a project-wide virtual filesystem, generic atomic-write function, generic tree transaction, or generic content-addressed publisher.

## Decisions

### Separate filesystem mechanics, complete leaf contracts, and domain protocols

Create `docker/transactions/` around four explicit levels:

```text
L3  domain protocols
    build generations, blob publishers, npm trees, snapshots,
    quarantine, advisory indexes, user/evidence outputs
                         │
L2  complete regular-file contracts
    atomic no-clobber | durable no-clobber | durable replace
    validated read    | durable unlink
                         │
L1  descriptor capabilities
    validated directory | validated/prepared regular file
    live descriptor authority | basename-only child operations
                         │
L0  injected POSIX operations
    openat/read/write/fstat/fsync/linkat/renameat/unlinkat/
    chmod/flock/close
```

L0 is an internal fault-injection backend, not a project-wide virtual filesystem or domain-facing god object. It preserves real descriptor-relative operations and errno behavior. L1 carries already validated live descriptor authority so callers do not re-resolve paths between validation and mutation. L2 provides complete, separately named regular-file contracts; it does not expose one configurable `atomic_write`, a `durable=False` switch, arbitrary paths, tree publication, or domain identity. L3 owns sequencing whenever the commit unit or authority is wider than one regular file.

The hierarchy is cumulative: L3 may compose L2, L1, and L0; L2 may compose L1 and L0; L1 may compose L0. A consumer SHALL use the highest layer whose complete contract matches the operation and SHALL descend only when no higher contract is semantically sufficient. Reusing an L2 leaf operation inside an L3 protocol does not transfer the protocol's identity, multi-entry commit, collision, recovery, retention, error, or lifecycle authority to L2.

`locking` remains alongside these I/O levels and owns lock-file validation, `flock`, mandatory contention policy, release, and namespace-bound live capabilities. `codec` provides deterministic canonical JSON encoding and generic JSON decoding while granting no schema, version, identity, path, commit, recovery, or deletion authority.

Alternative considered: one callback-heavy transaction/VFS abstraction spanning files, trees, blobs, and user state. Rejected because it would hide different commit points, make descriptor authority optional, and let mocks falsely model POSIX hard links, rename, ownership, and directory fsync as ordinary in-memory path operations. A generic previous/intended fingerprint coordinator is also rejected: immutable build generations provide a smaller domain-specific oracle, while future multi-target/CAS protocols need stronger domain-owned semantics. A generic versioned envelope is rejected as premature: build manifests, multi-target journals, settings sidecars, and metadata cache records have different closed fields, evolution policies, and recovery authority, so their owning domains define their complete schemas over the shared codec.

### Use PosixFileOps as L0 and retain domain filesystem facades

The production L0 implementation is `PosixFileOps`. It delegates directly to descriptor-relative POSIX operations and preserves their exception subclasses, errno values, and chaining inputs. Secure L1/L2 operations use this descriptor-relative surface directly and never reconstruct a pathname from an open descriptor.

The existing runtime-projection `Filesystem` becomes a compatibility/domain facade over L0: it retains path generation, runtime-root validation, lifecycle ownership, and existing injection seams, while delegating publication mechanics to L2 through `PosixFileOps`. Other filesystem abstractions, including runtime artifact cache, networking, and orchestration ports, remain separate domain interfaces; this change does not merge them into L0 or one project-wide hierarchy.

Alternative considered: make the existing path-oriented `Filesystem` the L0 implementation. Rejected because secure operations would have to convert validated descriptor authority back into paths, reopening pathname/descriptor TOCTOU windows. Replacing every existing filesystem port with L0 in one step is also rejected as unnecessary blast radius.

### Make lock capability explicit and namespace-bound

Acquisition returns a non-forgeable-in-normal-use capability carrying the normalized namespace and live descriptor state. Protected operations require that capability and reject released or mismatched instances. Consumers must choose `BLOCK` or `FAIL_FAST`; contention behavior has no default.

The common implementation validates no-follow type, effective owner, single link, and inode identity under the acquired descriptor before any repair. A missing lock entry is created atomically with `O_CREAT|O_EXCL` at mode `0600`; because a restrictive process umask can strip the owner read and write bits from that creation, a known-new inode is repaired to exactly `0600` only after the exclusive acquisition and full validation. A pre-existing owner-owned single-link regular lock is repaired only when its mode grants its owner read or write access: it is opened with the strongest access the owner currently has (falling back from `O_RDWR` to `O_RDONLY` or `O_WRONLY`), exclusively acquired, and only then repaired to exactly `0600`. Owner accessibility is decided from the acquired descriptor's mode bits, not from whether an open succeeded, so a privileged process (root or `CAP_DAC_OVERRIDE`) that can open a pre-existing entry anyway still rejects it after acquisition and before any repair. A pre-existing lock whose mode grants the owner neither read nor write access cannot be exclusively acquired before repair by an unprivileged process, so the shared layer fails closed and leaves it unmodified rather than chmodding it before holding the lock. The known-new repair is limited to an inode created by the current acquisition and never applies to a pre-existing entry: an existing `0000` lock is rejected without mutation, while a `0000` inode created by this acquisition because of the umask is acquired, validated, and then repaired to `0600`. Unsafe entries are rejected without repair. Existing path-bootstrap rules remain domain adapters.

Alternative considered: expose only a context manager yielding no capability. Rejected because nested domain helpers could then mutate protected state without evidence that the correct lock remains live.

### Keep atomic no-clobber distinct from durable publication

The L2 regular-file layer operates on bytes and validated descriptor capabilities. Canonical JSON encoding is a deterministic codec over it, not a schema authority. It exposes these distinct contracts:

```text
atomic no-clobber
  complete bytes + final mode before visibility + destination never replaced
  no persistence-after-power-loss claim

durable no-clobber
  atomic no-clobber + file fsync before publication + parent fsync before success

durable replace
  complete private sibling + file fsync + validated destination replace
  + parent fsync before success

validated read
  one no-follow descriptor + owner/type/link/mode checks before bytes

durable unlink
  one validated owned entry + parent fsync before success
```

Atomic and durable variants are separate operations rather than a boolean policy because directory-fsync failure changes the success contract. Temporary files are siblings of their destination; cleanup removes only transaction-owned temporary state and never masks the primary failure. Every descriptor is closed through all outcomes.

Temporary allocation generates a new private sibling name for each of at most three total attempts. `EEXIST` while allocating a temporary name retries without reading, repairing, or deleting the collided entry. Three exhausted attempts produce an ordinary publication failure at the allocation stage. `EEXIST` from the final no-clobber commit is instead the expected `DESTINATION_EXISTS` outcome and is never conflated with temporary-name collision.

L2 represents expected final-destination collision as a typed outcome, not a generic exception. Unexpected L2 operation failures carry their stage and original cause but do not cross a domain boundary directly. L3 adapters preserve each consumer's existing exception types and messages, retain observable raw `OSError` values and exception chaining, and never translate `KeyboardInterrupt` or cancellation into ordinary I/O failure. Cleanup and close failures are secondary to an existing primary failure and are attached as diagnostic context rather than replacing it. When no earlier failure exists, failure of an operation required by the selected contract remains fatal: in particular, required parent-directory fsync failure after publication means the durable operation failed even though the destination may already be visible.

### Unify primary-preserving cleanup without creating a cleanup engine

Add an internal cross-cutting `CleanupFailures` accumulator alongside locking and the codec rather than assigning it filesystem or domain authority in L0–L5. It standardizes only failure precedence and diagnostic preservation while each consumer continues to own resource identity, cleanup order, idempotent-absence policy, exactly-once state, path/deletion authority, retry policy, domain mapping, and whether its actions are independent.

The minimum API is explicit:

```python
class CleanupFailures:
    def __init__(self, primary: BaseException | None) -> None: ...

    def run(
        self,
        action: Callable[[], None],
        *,
        ordinary: tuple[type[Exception], ...],
    ) -> None: ...

    def complete(self) -> Exception | None: ...
```

`ordinary` is mandatory and keyword-only so every caller declares which expected operational exceptions may be accumulated. `run()` invokes its action exactly once, never retries it, captures every action exception, and returns so later independent cleanup actions are still attempted. An exception matching `ordinary` is accumulated as an ordinary cleanup failure; another `Exception` is an unexpected cleanup defect; a non-`Exception` `BaseException` is a process-control interruption. Misuse of the accumulator itself, including invalid `ordinary` types, `run()` after completion, or a second `complete()`, fails immediately.

The accumulator retains this conceptual state:

```python
original_primary: BaseException | None
ordinary_failures: list[Exception]
unexpected: list[Exception]
interruptions: list[BaseException]
```

Construction classifies an original primary that is not an `Exception` as the first process-control interruption rather than as an ordinary original primary. Its object identity and position precede every interruption raised later by cleanup. An original primary that is an `Exception` remains in `original_primary`; it may be displaced by an unexpected cleanup defect according to the precedence below.

`complete()` is the sole terminal method and applies the precedence `interruption > unexpected cleanup defect > original `Exception` primary > ordinary cleanup failure`. If an interruption exists, `complete()` raises the first interruption unchanged. For an original non-`Exception` primary this is that original object; cleanup defects and cleanup interruptions cannot displace it. Its secondary diagnostics are flattened in this deterministic order: later interruptions in action order, unexpected defects in action order, any displaced original `Exception` primary, then ordinary failures in action order. Otherwise, if an unexpected defect exists, `complete()` raises the first defect unchanged and attaches later defects in action order, the displaced original `Exception` primary, then ordinary failures in action order. If only an original `Exception` primary and ordinary cleanup failures exist, `complete()` attaches those failures in action order and returns `None` so the caller's existing propagation remains authoritative. If no original primary exists, `complete()` returns the first ordinary cleanup failure with later ordinary failures attached in action order; the caller then maps or raises it according to its layer. With no cleanup failure it returns `None`.

Secondary-diagnostic storage and attachment remain owned by `docker.transactions.errors`. The low-level `attach_secondary` mechanism preserves exception objects and avoids duplicate attachment by identity where the authoritative exception permits object attachment. When object attachment is unavailable, it provides only bounded `BaseException.add_note()` text without promising identity de-duplication. `CleanupFailures` uses that mechanism internally. The accumulator remains internal to `docker.transactions.cleanup` and is not exported from top-level `docker.transactions`.

Domain adapters that replace a transaction or capability wrapper with its raw operational cause use a separate intention-revealing `carry_secondary_diagnostics(target, source)` operation rather than reading storage or constructing an arbitrary diagnostic list. The operation carries, without consuming, only the secondary diagnostics already retained by `source`; it does not carry `source` itself, `__cause__`, `__context__`, or unrelated notes. When `target` can retain exception objects, the operation preserves those objects in source order and de-duplicates them by identity across repeated carries. When object attachment is unavailable, it provides only bounded textual observability through exception notes; that fallback neither promises exception-object retention nor identity de-duplication across repeated carries. In both cases it preserves target identity. The operation lives beside the storage contract in `docker.transactions.errors` and is not re-exported from top-level `docker.transactions`.

This narrower remap operation replaces the twelve audited wrapper-to-cause calls in artifact cache, build cache, effective projection, npm publication, and rendering. It does not become cleanup orchestration and accepts no caller-supplied diagnostic iterable. The two non-remap uses remain explicit exceptions: lock descriptor release constructs a new `LockError` from the first ordinary release failure and aggregates later failures, while build-lock release both attaches and returns the release failure under its caller-owned result model. Those sites may continue to use the low-level attachment mechanism, are documented as aggregators rather than remaps, and do not broaden the carry operation.

Idempotent absence is deliberately outside this API. A consumer that owns an entry or tree wraps `unlink` or `shutil.rmtree` and suppresses only `FileNotFoundError` when its domain contract declares absence successful; permission, I/O, partial-recursion, and unexpected-state failures still enter `run()`. The API exposes no `unlink`, `remove_tree`, `close`, rollback, compensation, path, or generic transaction operation.

Migrate in three bounded waves. First, audit every shared capability, locking, and regular-file migration site and behavior-preservingly replace only the cases whose precedence, ordering, continuation, and exactly-once results already match the accumulator. Known exceptions are intentional hardening, not parity: `locking._release_descriptor` currently permits an exception raised during cleanup to displace an original non-`Exception` primary; `regular._discard` likewise permits cleanup interruption to displace an original interruption and can skip temporary unlink when close raises an unexpected exception or interruption. The accumulator requires the original interruption to remain authoritative and every later independent cleanup action to be attempted exactly once. Regression tests SHALL pin corrected precedence, continuation through unlink, secondary diagnostics, and resource ownership for these and every additional mismatch found by the audit before any site is described as behavior-preserving. Second, migrate matching build, project-state, rendering, runtime, and npm adapters while retaining domain translation and L3 authority, again classifying audit findings as parity or explicit hardening before migration. Third, add fault-injection tests before hardening existing specialized build-blob, snapshot, materialization, and npm-tree cleanup paths that currently lose or mask non-absence cleanup failures. npm process lifecycle, streaming, activity-monitor policy, and user/presentation outputs remain excluded because they have different timeout, cancellation, suppression, redaction, or result-precedence models. Existing metadata cache storage is deferred to `revalidate-update-metadata`, which must consume this accumulator rather than duplicate it.

The required initial direct-L2 adoption set is:

```text
atomic no-clobber:
  docker/versioning/effective.py runtime projection publication

durable no-clobber:
  docker/versioning/project_state.py project identity metadata
  docker/versioning/build_cache.py build generation manifests

durable replace:
  build control files
  docker/versioning/rendering.py effective build projection
  future metadata envelopes
```

`write_effective_inventory` is a user-directed atomic output and is not required to migrate in this change. The set above identifies mandatory direct migrations, not a permission boundary. Content-addressed build/runtime blob publication, npm immutable output trees, snapshots and build-context confinement, quarantine and recursive cleanup, advisory indexes, and user/evidence outputs remain L3 domain protocols. They SHOULD reuse a compatible individual L2 leaf contract where its complete semantics fit, and otherwise MAY descend to L1 or L0, while keeping their identity, commit, collision, recovery, retention, error, and lifecycle semantics in L3.

Alternative considered: centralize consumer schemas and every operation containing `fsync`, `replace`, or `link`. Rejected because equal syscalls do not imply equal authority: a blob digest, immutable tree, ephemeral snapshot, advisory index, and user output have different commit and failure contracts.

### Publish immutable monotonically numbered build generations

Committed build manifests use the canonical form:

```text
committed-build-00000000000000000001.json
committed-build-00000000000000000002.json
```

The suffix is exactly 20 decimal digits and nonzero. Under the checkout-wide `FAIL_FAST` lock, the next generation is `max(valid generations) + 1`; overflow fails before publication. Generation files are created once through durable no-clobber publication and are never modified. The newest valid generation becomes authoritative only after its parent directory is fsynced.

A no-clobber commit may make a complete generation entry visible before its required parent-directory fsync fails. Therefore every startup discovery first validates and classifies the visible generation entries, then successfully fsyncs the open generation directory before accepting the newest generation as authoritative or deleting any predecessor blob. This synchronization completes a surviving visible publication whose earlier durability result was unknown; failure preserves all generations and blobs, returns an operational failure, and starts no build work. Filename/content discovery alone is not proof of durable commit.

A stable namespace contains one generation. Two generations mean that the older generation is durable evidence for incomplete cleanup:

```text
previous generation: {A, B}
current generation:  {B, C}  ← authoritative
cleanup candidates:  {A}
```

Zero generations is valid before the first successful build. More than two generations and malformed or noncanonical generation names or contents are ambiguous and fail closed without cleanup or build execution. The legacy `committed-build.json` name is outside the generation namespace: runtime code does not inspect, adopt, reject, or delete it, and generation-state discovery ignores it.

Alternative considered: UUIDv7 names. Rejected because only namespace-local ordering is required and a counter under the existing exclusive lock avoids clock rollback and monotonic UUID generation concerns.

### Recover cleanup sequentially and preserve the predecessor until completion

After durable publication of a new generation, the constructor first reconciles markers for every blob in the authoritative generation. It removes any surviving uncommitted markers as one batch and fsyncs the shared marker directory once before superseded cleanup. Marker removal never precedes durable generation authority: before that point the markers remain the retention evidence for blobs whose commit may fail. Marker unlink or marker-directory fsync failure leaves the new generation committed, returns `ExitKind.OPERATIONAL` (process exit code `4`), preserves any predecessor that still exists, and blocks both superseded cleanup and build work. Discovery repeats authoritative-generation marker reconciliation, including one marker-directory fsync when all relevant markers are already absent, before admitting a build or deleting the predecessor. Tests inject every marker unlink and marker-directory fsync failure and verify generation/no-rollback state, predecessor retention where one exists, cleanup/build blocking, diagnostics, and operational/4 mapping.

Superseded cleanup then computes canonical `previous - current` identities and attempts every candidate even after individual failures. Deletions are batched by parent directory: cleanup performs all applicable unlinks first, then fsyncs each affected existing blob-algorithm directory once and the shared marker directory once. It does not fsync after each candidate. Candidate completion requires the blob and its uncommitted marker to be absent and a successful post-unlink fsync of each relevant existing parent directory. On recovery, directories relevant to the retained candidate set are synchronized even when every entry in a batch is already absent, because an earlier unlink may have succeeded before its directory fsync failed. Only after every candidate batch is durably absent may the previous manifest be durably unlinked and the generation directory fsynced.

If any candidate unlink or required directory fsync fails, all remaining candidates are still attempted, all failures are reported, the previous manifest remains, and the constructor returns `ExitKind.OPERATIONAL`, mapped to process exit code `4`. A successful Docker image and current generation are not rolled back after this post-commit failure. This ordering prevents power loss from restoring a superseded blob or marker after its recovery evidence has been discarded.

Blobs materialized before a generation is published retain their durable uncommitted markers. If snapshot cleanup prevents publication, or if a later successful generation selects a different set, those blobs are not part of `previous - current`; they remain reusable uncommitted cache entries and existing cache maintenance removes them with their markers after the fixed 30-day TTL unless a later committed generation references them. Generation cleanup therefore handles only superseded committed blobs, while marker/TTL maintenance handles materialized blobs that never become committed.

On a later invocation, recovery runs under the checkout lock before materialization, snapshot work, or Docker execution. Recovery applies the same algorithm to the two generations. Failure again returns operational exit code `4` and prevents the build; success durably removes the predecessor and admits the build.

```text
one generation ──publish next──▶ two generations
       ▲                               │
       │                     delete previous-current
       │                               │
       └──── durable unlink previous ◀─┘

failure at cleanup:
  attempt remaining candidates
  preserve previous manifest
  return operational/4
```

Alternative considered: run recovery concurrently with a new build. Rejected because sequential recovery is fast, avoids pinning and cache-use races, and keeps the two-generation invariant simple.

### Migrate the required adoption set without homogenizing domain protocols

- Build cache retains checkout-wide `FAIL_FAST` locking, adopts durable regular-file operations for control/generation state, and uses immutable generations plus build-owned canonical digest cleanup. Markers, fixed TTL, snapshots, blob verification/publication, and shared-XDG exclusion remain local.
- Project identity metadata adopts durable no-clobber publication while keeping canonical project identity derivation and concurrent-winner validation local.
- The effective build projection adopts durable replacement while keeping project-state validation, TOML serialization, destination identity, and diagnostics local.
- Runtime projection publication adopts atomic no-clobber while keeping DTO validation, content identity, `0444` mode policy, lifecycle handle, and cleanup local; it does not gain a power-loss durability claim.
- Runtime artifact materialization migrates only its digest-identity `BLOCK` lock. Its fast path, post-lock recheck, streaming verification, content-addressed blob publication, and quarantine remain specialized.
- npm environment publication/storage migrates only its input-identity `BLOCK` lock and compatible regular-file leaf mechanics. Immutable-tree commit, recursive fsync/sealing, evidence, collision handling, advisory index policy, quarantine, and cancellation remain specialized.

Migration tests assert the selected L2 contract and unchanged L3 policy rather than merely replacing imports or eliminating every repeated syscall.

### Let future changes own stronger adapters

`add-locked-image-owned-pi-extensions` may consume shared locks, regular-file operations, and the canonical JSON codec, but owns the complete closed versioned schemas and recovery protocols for its sync-lock journal and settings sidecar. It must not treat the codec, L2 operations, or immutable build generations as a generic transaction engine.

`revalidate-update-metadata` may consume validated reads, replacement, durable removal, and the codec for disposable cache records, but owns the complete metadata-envelope schema and does not adopt build-generation cleanup or a broad lock.

Alternative considered: implement speculative multi-target and CAS coordinators now. Rejected because their authority, conflict, rollback, and recovery rules must be driven by their owning requirements.

## Risks / Trade-offs

- **[An abstraction weakens a consumer's security checks]** → Keep bootstrap, identity parsing, containment, and deletion authority in domain adapters; require parity tests before removing local code.
- **[A cleanup error hides additional failing blobs]** → Attempt every candidate and return one aggregate operational diagnostic while retaining the predecessor.
- **[Recovery evidence is discarded too early]** → Batch candidate unlinks by parent directory, fsync each affected blob directory and the shared marker directory once after their batches, and only then unlink the previous manifest and fsync the generation directory. Recovery repeats the relevant directory fsync even when a batch is already absent.
- **[A stale predecessor authorizes deletion of a current blob]** → Derive candidates strictly as validated `previous - current` under the checkout lock.
- **[Generation ordering becomes ambiguous]** → Require canonical fixed-width names, no-clobber publication, at most two generations, and fail closed on malformed or excess state; after discovery, fsync the generation directory before accepting visible authority so a surviving post-publication/pre-fsync entry can be completed safely.
- **[Post-build cleanup failure surprises callers because an image exists]** → Return operational exit code `4` with an explicit message that the image and newest generation remain committed and recovery is required.
- **[A lock migration changes waiting behavior]** → Make contention policy mandatory and test same/different namespaces for every consumer.
- **[Refactoring mature paths creates a large blast radius]** → Land levels and explicit contracts first, migrate the required direct-adoption set one consumer at a time, prefer compatible higher-layer reuse in specialized protocols, and remove code only after parity suites pass.
- **[A project-wide VFS hides security and durability semantics]** → Keep `PosixFileOps` internal and descriptor-relative, retain existing filesystem objects as domain facades, expose complete L2 operations, and prohibit pathname reconstruction from descriptor capabilities.
- **[Atomic publication is mistaken for durable publication]** → Give atomic and durable no-clobber separate operations and tests; runtime projection deliberately receives no directory-durability claim, while required post-publication directory-fsync failure remains fatal for durable operations.
- **[A temporary collision is mistaken for destination contention]** → Retry a fresh private name at most three total times, never touch collided entries, and reserve `DESTINATION_EXISTS` for the final commit only.
- **[Shared exceptions leak across domain boundaries]** → Make expected collision an outcome, map unexpected L2 failures through L3 adapters, preserve existing exception types/messages and observable causes, and pass interruption/cancellation through unchanged.
- **[Secondary cleanup failure masks the primary failure]** → Preserve the primary exception and attach cleanup/close failures as secondary diagnostic context; report secondary failure directly only when no primary failure exists.
- **[A specialized protocol accidentally delegates domain authority]** → Prefer the highest compatible shared leaf contract while retaining blob/tree/snapshot/quarantine/index identity, commit, collision, and failure decisions in L3.
- **[POSIX-specific primitives appear portable]** → Keep support explicit and fail clearly where `flock`, no-follow, hard-link, or directory-fsync semantics are unavailable.
- **[No runtime legacy migration inconveniences developers]** → Publish one explicit cache-removal instruction before archive; runtime generation discovery does not inspect, adopt, reject, or delete the legacy manifest name.

## Migration Plan

1. Publish and execute the one-time developer instruction that removes the old development build cache; do not add runtime inspection, adoption, rejection, or deletion of `committed-build.json`.
2. Add the internal L0 POSIX backend, L1 descriptor capabilities, distinct L2 regular-file operations, shared locks, and the canonical JSON codec with security, concurrency, determinism, and fault-injection tests.
3. Define the build generation manifest's complete closed versioned schema in the build domain over the shared codec; add no generic envelope or recovery authority.
4. Migrate build locking and control files, introduce durable no-clobber generations, aggregate cleanup, pre-build recovery, and operational exit code `4` while retaining specialized blob publication.
5. Migrate project identity metadata to durable no-clobber publication and the effective build projection to durable replacement.
6. Migrate runtime projection publication to atomic no-clobber without adding a directory-durability claim; migrate runtime-artifact locking while retaining its content-addressed publisher.
7. Migrate npm-environment locks and only compatible leaf mechanics while retaining its immutable-tree, advisory-index, quarantine, and cleanup protocols.
8. Inventory every production filesystem writer against the L0–L3 matrix, verify the required direct-L2 adoption set and specialized L3 list, and justify every descent below an available higher-level contract.
9. Run complete cache, build, npm, launcher, versioning, CLI, and acceptance suites; verify no unexplained duplicate production `flock` implementation, ambiguous regular-file contract, or generic envelope remains.

Rollback is source-level before new-format use. After a new generation has been published, rollback requires deleting the development cache because the old implementation does not understand generation manifests. No persistent user-data migration or compatibility adapter is provided.

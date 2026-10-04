# Phase 7 Verification — Runtime Lock and Projection Migration

Date: 2026-10-04T01:20:15Z
Change: `add-durable-filesystem-transactions`
Phase: 7 (Runtime Lock and Projection Migration)
Depends on: Phase 1 (`Add durable filesystem transactions substrate`, commit
`05fe4b4`), Phase 2 (`Add validated owner-private advisory locks`, commit
`006c4dd`)

## Scope delivered

The runtime-artifact download lock now composes the shared L2 **`BLOCK`**
capability, the runtime-projection `Filesystem` is a compatibility/domain
facade over the shared L0 `PosixFileOps`, and runtime projection publication
uses the shared L2 **atomic no-clobber** contract.  The runtime
content-addressed blob publisher remains a specialized L3 protocol: SRI
validation, content-addressed path derivation, collision/quarantine,
revalidation, permission policy, and cleanup stay in the runtime domain.  The
lock scope (per SRI identity), blocking policy, publication semantics, and
existing diagnostics are preserved.

Production changes:

- `docker/versioning/artifact_cache.py`
  - `FileIdentityLock` is now a thin adapter over the shared
    `LockCapability`.  It keeps the identity-derived private lock-entry name
    (`<urlsafe-base64>.lock` under the private `locks/` directory), acquires
    with `LockPolicy.BLOCK`, and delegates entry validation (symlink,
    non-regular, foreign-owned, multiply linked), atomic exclusive creation,
    post-acquisition mode repair, and release to the shared layer.  The
    runtime domain keeps only the identity namespace and its existing
    `ArtifactMaterializationError("containment", ...)` diagnostics.
  - `LockError` mapping preserves the previous diagnostics: `validate`-stage
    failures map to `"identity lock is not a private regular file"`, other
    prepare failures to `"identity lock path is unsafe"`, and operational
    descriptor-stat (`STAGE_LOCK_STAT`, see the follow-up fix below),
    mode-repair, and acquisition failures re-raise their raw `OSError` with
    any attached cleanup diagnostics.  A `validate`-stage failure is never
    blindly unwrapped even when it carries an `OSError` cause, because an
    identity-probe failure can indicate an unsafe namespace change.
    Process-control interruptions propagate unchanged.
  - `release()` is unconditional and idempotent: the shared capability is
    closed (raw `OSError` cause preserved), then the retained directory
    capability is closed with any secondary close failure attached.
  - `acquire()` transfers the lock-directory descriptor to
    `DirectoryCapability.from_fd` explicitly.  If adoption fails the
    still caller-owned descriptor is closed exactly once through the
    injectable backend, with an ordinary close failure attached as a secondary
    diagnostic rather than replacing the primary failure (see the follow-up
    fix below); after successful adoption only `DirectoryCapability.close()`
    releases it.
- `docker/versioning/effective.py`
  - `Filesystem` gains a descriptor-relative `ops` seam (default
    `PosixFileOps`) while retaining every legacy path-oriented method and
    constructor keyword, so it stays a compatibility/domain facade.
  - `create_runtime_projection` serializes and validates the DTO exactly as
    before, then publishes through
    `RegularFileContracts(_fs.ops).atomic_no_clobber(directory, basename,
    content, 0o444)` over a `DirectoryCapability` obtained by a secure
    component walk (`DirectoryCapability.from_secure_path`) of the injected
    backend.  A typed `DestinationExists` maps to the unchanged
    `EffectiveConfigError("runtime projection ... already exists; refusing to
    overwrite another launch's projection")`; non-collision operational
    failures (including parent-directory open/stat failures unwrapped from a
    `CapabilityError`) re-raise their raw `OSError` cause with shared cleanup
    diagnostics attached; interruptions propagate unchanged.  The retained
    directory descriptor is closed exactly once with a secondary close
    failure never replacing a publication failure.  Atomic publication makes
    no parent-directory `fsync` claim.  `RuntimeProjectionHandle` and
    `cleanup_runtime_projection` are unchanged.

`docker/transactions/capabilities.py` gains the shared
`DirectoryCapability.from_secure_path` component walk and an internal
`_adopt` helper (also used by `from_path`); the walk closes the retained
parent descriptor before returning the adopted leaf and releases the leaf
rather than leaking it if that close fails, attaching an ordinary leaf-close
failure as a secondary diagnostic while a leaf-close process-control
interruption propagates unchanged.  No other shared behavior changed, and no
specification file was changed except task checkboxes.

## RED (tasks 7.1–7.4)

Four focused test modules were added before the migration was applied:

- `tests/test_transactions_phase7_runtime_lock.py` (task 7.1; 13 migration
  tests, 17 after the follow-up fixes below):
  identity-selected private lock entry, `0600` repair, independent
  concurrency for different identities, same-identity blocking until release,
  symlink/non-regular/multiply-linked/symlinked-directory rejection without
  mutation, idempotent release and reacquisition, a no-op release without
  acquire, factory freshness, and the valid-hit fast path leaving the lock
  namespace untouched.
- `tests/test_transactions_phase7_runtime_projection.py` (task 7.2; 15
  migration tests, 18 after the hardening pass below): complete bytes with
  the final `0444` mode observable at the commit
  link, one atomic `linkat` and no `renameat`, no `fsync` durability claim,
  content-hash parity, typed collision mapped to the existing
  `EffectiveConfigError` text with the `DestinationExists` cause preserved,
  raw `OSError` re-raise on write/link failure, interruption passthrough,
  symlink/out-of-root rejection, and lifecycle cleanup/discard.
- `tests/test_transactions_phase7_runtime_facade.py` (task 7.3; 9 migration
  tests, 10 after the hardening pass below): legacy constructor seams
  retained, default and explicit `ops` seam, path
  generation and runtime-root validation through the injected seams,
  lifecycle ownership via the injected `unlink`, and descriptor-relative
  publication (`linkat` of a `.transaction-` sibling within one retained
  directory descriptor, no `renameat`, parent directory opened through a
  descriptor-relative component walk over `ops.openat`).
- `tests/test_transactions_phase7_artifact_specialization.py` (task 7.4, 7
  tests): the blob publisher is not the L2 regular-file contract (no
  `RegularFileContracts`, `os.replace` production path retained), SRI
  identity derives content-addressed paths, malformed integrity is rejected
  before any publication, publication is content-addressed and revalidated
  through the domain boundary, corrupt entries are quarantined then replaced,
  and temporary cleanup stays domain-owned.

RED result against the pre-Phase-7 sources (`effective.py` and
`artifact_cache.py` temporarily reverted, then restored byte-for-byte from
`/tmp/phase7_impl.patch`):

```text
Ran 44 tests in 0.336s
FAILED (errors=20)
```

All 20 errors were the new `ops`-seam/descriptor-publication tests (12
projection, 8 facade) failing with `AttributeError: 'Filesystem' object has
no attribute 'ops'`.  The lock (13) and specialization (7) modules passed
before and after: they pin behavior that must be preserved by the migration
(parity) rather than behavior that must change.

## GREEN (tasks 7.5–7.8)

```text
$ python -m unittest tests.test_transactions_phase7_runtime_lock \
      tests.test_transactions_phase7_runtime_projection \
      tests.test_transactions_phase7_runtime_facade \
      tests.test_transactions_phase7_artifact_specialization
Ran 44 tests in 0.339s
OK
```

- 7.5 Runtime artifact lock replaced with the shared `BLOCK` adapter over the
  unchanged identity namespace and diagnostics; task 7.1 passes.
- 7.6 `Filesystem` is a compatibility/domain facade over production
  `PosixFileOps` without merging any other filesystem interface
  (`CacheFilesystem`, networking, orchestration ports are untouched); task
  7.3 passes.
- 7.7 Runtime projection publication migrated to L2 atomic no-clobber while
  DTO validation, canonical serialization, content identity, the `0444` mode
  policy, the lifecycle handle, error mapping, and cleanup stay in the
  adapter; task 7.2 passes.
- 7.8 The content-addressed blob publisher remains an L3 protocol.  Every
  individual operation that reuses a shared mechanic reuses the highest
  compatible layer: the per-identity lock composes L2 `LockCapability`
  (`BLOCK`), and directory/temporary/replace mechanics keep their existing
  descriptor-relative L0 implementations (`_open_directory_chain`,
  `_open_parent`, `LocalCacheFilesystem.atomic_publish`) because no complete
  L2 contract matches the replace-after-quarantine, content-addressed
  publication.  Digest, collision, quarantine, permission, and lifecycle
  authority stay in the runtime domain; task 7.4 passes.

Existing tests that pinned the removed path-oriented publication helper were
migrated to the L0 seam rather than weakened:

- `tests/test_constructor_runtime_lifecycle.py` gains an in-memory
  `_InMemoryOps` L0 backend (and `_FakePath.basename`) so the fully-fake
  lifecycle tests still prove zero host I/O; the former `fsync`/`link`
  injection tests now inject a write and a `linkat` failure through
  `ops` and assert the same temp-cleanup/no-destination outcomes.
- The two signature-level injection tests
  (`test_atomic_write_cleans_temp_on_write_failure`,
  `test_link_failure_cleans_temp_file`) use `InjectedOps` at the L0 seam.

## INTROSPECT (task 7.9)

1. **Widened locking** — the lock name is still derived from the SRI identity
   and the namespace is still the identity; different identities remain
   independent (`test_different_identities_are_independently_concurrent`).
   No finding.
2. **Changed waiting** — the shared acquisition uses `LockPolicy.BLOCK`,
   preserving blocking; `test_same_identity_blocks_until_release` fails under
   a `FAIL_FAST` mutation.  No finding.
3. **Weakened SRI** — SRI parsing, content-addressed path derivation, and
   digest verification are unchanged and remain in the runtime domain
   (`test_blob_identity_is_derived_from_sri`,
   `test_malformed_integrity_is_rejected_before_publication`).  No finding.
4. **Blob-authority leakage** — the runtime cache does not import
   `RegularFileContracts`; the blob publisher still uses its path-based
   `atomic_publish` (`test_blob_publication_is_not_the_l2_regular_file_contract`).
   No finding.
5. **False projection durability** — atomic no-clobber performs no `fsync`
   (`test_makes_no_parent_directory_durability_claim`), and switching the
   migration to `durable_replace` fails the durability and single-link tests.
   No finding.
6. **Build-generation coupling** — no build-generation symbols are imported or
   referenced by the runtime projection or runtime artifact cache.  No
   finding.
7. **Interruption conversion** — `create_runtime_projection` catches
   `BaseException` only to record the primary and re-raise it, and the
   `LockCapability` release path never catches a process-control interruption;
   `test_interruption_passes_through_unchanged` pins the projection boundary.
   No finding.
8. **Cleanup masking** — the projection directory descriptor close is a
   one-attempt `finally` that attaches an ordinary `OSError` as secondary to
   the publication failure (or is itself the only failure), and
   `FileIdentityLock.release` attaches a directory-close failure after
   preserving the raw capability-close failure.  No finding.
9. **Path reconstruction** — publication opens the parent through a secure
   descriptor walk over the injected L0 backend and commits with a single
   `linkat` of a `.transaction-` sibling in the same retained directory
   descriptor; the destination is never reopened by pathname
   (`test_publication_uses_descriptor_relative_l0`,
   `test_parent_walk_is_descriptor_relative_across_components`,
   `test_no_descriptor_path_is_reconstructed`).  The walk returns the adopted
   leaf only after the retained parent closes, so a failed parent close
   cannot leak the leaf (Issues C and D, fixed below).  No finding.
10. **Forced filesystem-interface merging** — `Filesystem` retains all legacy
    methods and kwargs alongside the new `ops` seam; `CacheFilesystem`,
    transports, and orchestration ports are untouched.  No finding.
11. **Intermediate-symlink traversal and parent error parity (finding and
    fix)** — the projection-parent containment check used `realpath`
    plus `islink` on the destination only, and opened the parent by its full
    pathname, so an in-root symlinked *intermediate* component (with a real
    directory after it) was followed.  Parent-directory open and stat
    failures were also wrapped in `CapabilityError` instead of preserving the
    raw `OSError`.  Both are fixed in the hardening pass below.

The remaining checklist items resolved without a code change; one finding
(item 11) required the hardening pass recorded below.

## Mutation checks

- Changing the shared lock policy from `LockPolicy.BLOCK` to
  `LockPolicy.FAIL_FAST` fails exactly
  `test_same_identity_blocks_until_release` (the contender is rejected instead
  of waiting).
- Replacing the projection's `atomic_no_clobber` with `durable_replace` fails
  exactly 7 projection tests (fsync durability claim, single-link commit, and
  the failure-path expectations that depend on the atomic mechanics).
- Reverting the hardening pass (opening the parent with
  `DirectoryCapability.from_path` and removing the `CapabilityError`
  unwrapping) fails exactly 4 tests:
  `test_symlinked_intermediate_parent_is_rejected`,
  `test_parent_open_failure_reraises_raw_oserror`,
  `test_parent_walk_is_descriptor_relative_across_components`, and
  `test_no_descriptor_path_is_reconstructed`.  `test_parent_stat_failure_reraises_raw_oserror`
  passes before and after because the leaf adoption already re-raised the raw
  stat failure.
- Restoring the original leaking `from_secure_path` (returning the adopted
  leaf from inside the `try`) fails exactly the 3 new `SecurePathTests`
  cases: `test_parent_close_failure_after_adoption_releases_leaf`,
  `test_parent_and_leaf_close_failures_keep_parent_primary`, and
  `test_parent_close_interruption_releases_leaf_and_propagates`.
- Restoring the earlier swallowing interruption handler (`except
  BaseException: pass` around the leaf release) fails exactly
  `test_parent_interruption_attaches_leaf_close_failure` and
  `test_leaf_close_interruption_propagates_unchanged`.
- Restoring the pre-fix `acquire` body (raw `os.close` in `finally`) fails
  exactly `test_adoption_stat_failure_preserves_primary_and_closes_raw_descriptor`
  (the injected cleanup failure is never attached as a secondary diagnostic).
- Reverting the shared `STAGE_LOCK_STAT` classification and the adapter
  branch fails exactly the 2 end-to-end `OperationalLockFailureTests` cases
  (`test_lock_entry_stat_failure_reraises_raw_oserror` and
  `test_lock_entry_stat_failure_preserves_cleanup_secondary`), which error
  because the containment error escapes `assertRaises(OSError)`.

`docker/versioning/artifact_cache.py` and `docker/versioning/effective.py`
were restored byte-for-byte after each mutation (verified with `diff -q`), as
was `docker/transactions/capabilities.py` for the leak and swallowing
mutations, and the 47 focused tests plus the 44 shared L1 tests pass again.

## Post-completion hardening pass: intermediate-symlink traversal and
parent-directory error parity (task 7.9/7.10)

### Issue A — in-root symlinked intermediate parent was followed

`_validate_safe_path` resolves the destination with `realpath` and rejects a
symlink only at the destination itself, and `create_runtime_projection`
opened the parent by its full pathname.  A symlinked *intermediate*
component whose target stayed inside the runtime root (for example
`<root>/link-parent/real-sub/projection.toml` with `link-parent ->
real-parent`) therefore passed containment and was followed by the kernel,
letting a projection land in a directory the caller did not intend.

**Fix** — `DirectoryCapability.from_secure_path` walks every component from
the filesystem root, opening each with `O_DIRECTORY | O_NOFOLLOW |
O_CLOEXEC` relative to the previously opened descriptor and adopting the
final component through the same validation as `from_path`.  A symlinked or
otherwise non-directory intermediate component is rejected (`ENOTDIR` for a
symlink opened with `O_DIRECTORY`, or `ELOOP`), and no pathname is ever
reconstructed from a descriptor.  Only the basename is passed to
`atomic_no_clobber`.  Absolute paths are required; a relative path raises a
`CapabilityError` rather than being anchored to ambient process state.

### Issue B — parent-directory operational failures lost their raw cause

`DirectoryCapability.from_path` (and now `from_secure_path`) wraps a failed
parent open in `CapabilityError(...) from OSError`, and the adapter had no
`CapabilityError` branch, so an operational parent open failure surfaced as a
`ValueError` instead of the previous raw `OSError`.

**Fix** — `create_runtime_projection` catches `CapabilityError`; when its
cause is an `OSError` the original error object is re-raised (carrying any
attached cleanup diagnostics), otherwise the genuine capability-validation
error is left unchanged.  A parent `fstat` failure already propagated raw
(the leaf adoption re-raises it), and this is now pinned.

### Tests

`tests/test_transactions_phase7_runtime_projection.py` gains:
`test_symlinked_intermediate_parent_is_rejected` (in-root symlink, real
subdirectory after it; publication rejected and no destination or temporary
entry created), `test_parent_open_failure_reraises_raw_oserror` (a
`PermissionError` injected on the parent component; exact object, subtype,
and `errno` preserved), and `test_parent_stat_failure_reraises_raw_oserror`
(raw `EIO` preserved).

`tests/test_transactions_phase7_runtime_facade.py` replaces the single
``openat`` expectation with
`test_parent_walk_is_descriptor_relative_across_components` and
`test_no_descriptor_path_is_reconstructed`: the walk anchors at the
filesystem root and opens one single-component name per path element, each
relative to the previously opened descriptor.

`tests/test_transactions_l1_capabilities.py` gains `SecurePathTests` pinning
the shared helper directly: descriptor-relative component opens, an
intermediate symlink rejected with an `OSError` cause, no descriptor leak on
a missing component, relative paths refused, and final-component ownership
validated.

### Pre-fix reproduction

```text
Ran 28 tests in 0.016s
FAILED (failures=4)
```

with the four failures listed under the mutation checks above; the L1
`SecurePathTests` also error against the old shared module (no
`from_secure_path` attribute).

## Follow-up hardening pass: parent-close failure no longer leaks the
adopted leaf

### Issue C — adopted leaf descriptor leaked when the retained parent close
failed

`DirectoryCapability.from_secure_path` returned the adopted leaf from inside
the `try`, so Python ran the `finally` before transferring ownership to the
caller.  When `ops.close(parent_fd)` in that `finally` raised an ordinary
`OSError` and there was no pending exception, the `raise` replaced the
pending return and the already-adopted leaf capability was dropped without
ever closing its descriptor: a descriptor leak on the successful-adoption /
parent-close-failure path.  A process-control interruption raised while
closing the parent had the same leak.

**Fix** — the adopted capability is retained in a local variable and returned
only after the retained parent closes successfully.  When the parent close
raises:

- an ordinary `OSError` stays the primary failure and the adopted leaf is
  closed exactly once; a failed leaf close is attached as a secondary
  diagnostic with `attach_secondary`;
- a `KeyboardInterrupt`/cancellation-style `BaseException` propagates
  unchanged (never converted to `CapabilityError`/`OSError`) while the leaf
  is released best-effort so it cannot remain silently live.

Neither close is retried (descriptor state is ambiguous after a failed POSIX
close), `_adopt` still owns and closes the leaf when final validation fails,
and a failed later-component open still closes the retained traversal
descriptor.

### New fault-injection tests

`tests/test_transactions_l1_capabilities.SecurePathTests` gains three tests
that capture the real leaf and retained-parent descriptors (the kernel reuses
descriptor numbers, so assertions key on the captured values rather than on
values appearing once):

- `test_parent_close_failure_after_adoption_releases_leaf` — fails the
  trailing retained-parent close after successful adoption; asserts the exact
  parent-close error propagates, the adopted leaf is closed exactly once
  immediately after, and the total number of closes is one per opened
  descriptor (no leak, no retry).
- `test_parent_and_leaf_close_failures_keep_parent_primary` — additionally
  fails the leaf release; asserts the parent-close error stays primary, the
  leaf-close error is attached as `_transaction_secondary`, and neither close
  is retried.
- `test_parent_close_interruption_releases_leaf_and_propagates` — raises a
  cancellation-style `BaseException` from the retained-parent close; asserts
  it propagates unchanged and the leaf is still released exactly once.

### Issue D — leaf-cleanup failures were swallowed on the interruption path

The interruption handler around the retained-parent close released the adopted
leaf with a broad `except BaseException: pass`, discarding every leaf-cleanup
failure.  Releasing a descriptor can fail for the same reasons any close can,
so those failures were silently lost: an ordinary `OSError` was dropped
instead of being reported, and a second process-control interruption (the
newer one, which the shared cleanup contract lets win) was swallowed while the
stale parent interruption was propagated.

**Fix** — the handler now catches only `OSError` from `capability.close()` and
attaches it to the parent interruption with `attach_secondary`, then re-raises
the parent interruption unchanged.  A `KeyboardInterrupt`/cancellation-style
`BaseException` from the leaf close is not caught, so it propagates unchanged
as the newer interruption and is never converted to
`CapabilityError`/`OSError`.  `DirectoryCapability.close()` still marks itself
closed before its `close(2)` syscall and is single-attempt, so the parent and
leaf each receive exactly one close attempt and neither is retried.

- `test_parent_interruption_attaches_leaf_close_failure` — interrupts the
  retained-parent close and fails the leaf close with `OSError`; asserts the
  parent interruption stays the raised exception, the leaf `OSError` appears in
  `_transaction_secondary`, and neither descriptor is closed more than once.
- `test_leaf_close_interruption_propagates_unchanged` — interrupts the parent
  close and then the leaf close with two distinct cancellation-style
  exceptions; asserts the newer leaf interruption propagates unchanged and
  both closes are attempted exactly once.

### Pre-fix reproduction

Against the original leaking implementation (returning the adopted leaf from
inside the `try`, restored byte-for-byte afterwards):

```text
Ran 8 tests in 0.004s
FAILED (failures=3)
```

All three leak-regression `SecurePathTests` cases fail.  Against the earlier
swallowing interruption handler (restored byte-for-byte afterwards):

```text
Ran 10 tests in 0.004s
FAILED (failures=2)
```

`test_parent_interruption_attaches_leaf_close_failure` and
`test_leaf_close_interruption_propagates_unchanged` fail.  The shared L1 suite
then passes **44/44** with the fix.

## Follow-up fix: operational lock-stat failures keep their raw `OSError`

### Issue E — a lock-descriptor stat failure was reported as a containment
error

`LockCapability.acquire` wraps an operational `ops.fstat(fd)` failure in
`LockError(STAGE_LOCK_VALIDATE, "cannot stat lock ...", cause=OSError)`, the
same stage used for genuine unsafe-entry rejections (symlink, non-regular,
foreign-owned, multiply linked, owner-inaccessible, in-place replacement).
The runtime adapter mapped every `STAGE_LOCK_VALIDATE` failure to the
containment diagnostic `"identity lock is not a private regular file"`, so an
ordinary I/O failure such as `EIO` while statting the locked descriptor lost
its raw `OSError` (and `errno`) and was misreported as an unsafe entry.

**Fix** — the shared layer classifies an operational descriptor-stat failure
under its own stage, `STAGE_LOCK_STAT = "stat-lock"` (added in
`docker/transactions/errors.py` and raised in
`docker/transactions/locking.py`).  The adapter (`_identity_lock_failure`)
re-raises the raw `OSError` for `STAGE_LOCK_STAT` just as it already did for
`STAGE_LOCK_MODE`/`STAGE_LOCK_ACQUIRE`, transferring any attached cleanup
diagnostics with `attach_secondary`.  The validate stage keeps its containment
diagnostic and is never blindly unwrapped even when it carries an `OSError`
cause, so an identity-probe failure that may indicate an unsafe namespace
change stays a containment error.  No error was classified by matching its
message text, and the entry-shape/ownership/link/type checks are unchanged.
`docker/versioning/build_cache.py` already preserved the raw `OSError` for
cause-carrying lock failures, so the new stage needed no change there.

### Regression tests

`tests/test_transactions_phase7_runtime_lock.OperationalLockFailureTests`
injects the L0 backend into `FileIdentityLock` and fails only the `fstat` of
the lock-file descriptor (the directory descriptor still validates):

- `test_lock_entry_stat_failure_reraises_raw_oserror` — asserts the exact
  injected `OSError` is raised (not a containment error), neither the lock nor
  the directory capability is retained, both descriptors are released, and a
  fresh acquisition immediately takes the lock again.
- `test_lock_entry_stat_failure_preserves_cleanup_secondary` — additionally
  fails the directory close; asserts the operational stat error stays primary
  and the cleanup failure is attached as `_transaction_secondary`.
- `test_validate_stage_failure_is_not_unwrapped` — a `STAGE_LOCK_VALIDATE`
  failure carrying an `OSError` cause still maps to a containment error.

The existing `UnsafeLockEntryTests` still pin containment errors for
symlinked/non-regular/multiply-linked entries and a symlinked lock directory
without mutation.

### Pre-fix reproduction

Reverting both the shared `STAGE_LOCK_STAT` classification and the adapter
branch (restored byte-for-byte afterwards):

```text
Ran 3 tests in 0.003s
FAILED (errors=2)
```

`test_lock_entry_stat_failure_reraises_raw_oserror` and
`test_lock_entry_stat_failure_preserves_cleanup_secondary` error because the
containment error escapes `assertRaises(OSError)`; the lock suite then passes
**16/16** with the fix.

## Follow-up fix: adoption cleanup can no longer replace its primary failure

### Issue F — the unconditional `finally` close could mask an adoption failure

`FileIdentityLock.acquire` opened the private lock directory with
`_open_directory_chain` and then handed the descriptor to
`DirectoryCapability.from_fd` for validation.  A `finally` block closed the raw
descriptor with `os.close` whenever adoption had not yet transferred
ownership.  When `from_fd` failed (for example an `EIO` while statting the
directory), the `except` block mapped and re-raised the primary failure, and
the `finally` then ran `os.close` unconditionally: an ordinary close failure
in that `finally` **replaced** the primary failure (Python discards the
in-flight exception when a `finally` raises), and the close was not part of
the injectable L0 seam so it could not carry cleanup diagnostics.

**Fix** — `acquire` now tracks the caller-owned descriptor in a local
(`raw_fd`) that is cleared as soon as `DirectoryCapability.from_fd` adopts it.
On failure it closes that descriptor exactly once through `self._ops.close`
(the same injectable backend used everywhere else in the lock), catching only
`OSError` and attaching it to the original exception with `attach_secondary`;
the primary failure is then re-raised through the existing
`_identity_lock_failure` mapping.  After successful adoption only
`DirectoryCapability.close()` releases the descriptor -- the raw descriptor is
never closed and a failed close is never retried.  A process-control
interruption from adoption is propagated unchanged (with an ordinary close
failure attached), and a newer interruption from the close itself propagates
unchanged.  There is no remaining `finally` that closes the raw descriptor.

### Regression test

- `tests/test_transactions_phase7_runtime_lock.OperationalLockFailureTests.
  test_adoption_stat_failure_preserves_primary_and_closes_raw_descriptor` --
  injects `OSError(EIO)` when the lock directory is statted during adoption
  and a second `OSError(EIO)` when the raw descriptor is closed; asserts the
  exact stat error stays primary, the close error is attached as
  `_transaction_secondary`, close is attempted exactly once
  (`ops.counts["close"] == 1`), and neither a lock nor a directory capability
  is retained.

### Pre-fix reproduction

Restoring the pre-fix `acquire` body (raw `os.close` in `finally`, restored
byte-for-byte afterwards):

```text
Ran 1 test in 0.001s
FAILED (failures=1)
```

`AssertionError: Lists differ: [] != [OSError(5, 'injected directory close
failure')]` -- the old code never routed the raw close through the injectable
backend, so the cleanup failure was neither preserved as a secondary
diagnostic nor even observable.  The lock suite then passes **17/17** with the
fix.

## VALIDATE (task 7.10)

- Focused Phase 7 suites — **51 tests, OK**
  (`runtime_lock` 17, `runtime_projection` 18, `runtime_facade` 10,
  `artifact_specialization` 7).
- Shared L1 capability suite (`test_transactions_l1_capabilities`) —
  **44 tests, OK** (10 `SecurePathTests`: 5 for the secure component walk, 3
  for the retained-parent-close leak, and 2 for leaf-cleanup interruption
  handling).
- Runtime materializer, corrupt-cache recovery, projection, lifecycle, and
  verification suites (`test_constructor_materialization`,
  `test_constructor_runtime_projection`, `test_constructor_runtime_lifecycle`,
  `test_constructor_runtime_verification`) — **186 tests, OK**.
- Launcher, cache-security, cache-ownership, release-ordering, multiprocessing/
  concurrency, and installer suites (`test_constructor_launcher`,
  `test_constructor_cache_contracts`, `test_cache_root_ownership_phase3`,
  `test_cache_release_ordering_phase3`,
  `test_npm_environment_concurrency_cache`,
  `test_constructor_runtime_installer`) — **641 tests, OK**.
- Combined focused Phase 7 + lifecycle + launcher + cache-security +
  corrupt-cache + concurrency run — **501 tests, OK**.
- Complete repository suite — **5004 tests, OK (skipped=13)** in 98.1s, exit
  code `0` (the migration adds 48 focused tests; the hardening passes add 14
  more: 3 projection, 1 facade, and 10 shared-layer).
- `ty check docker --python-version 3.14 --output-format concise` —
  **All checks passed!**
- `openspec validate add-durable-filesystem-transactions --strict` — valid.
- `git diff --check` — clean.

Recorded outcomes:

- **Lock parity** — per-identity scope and blocking contention preserved;
  different identities remain concurrent; unsafe entries fail closed with the
  existing containment diagnostics; release is idempotent and preserves the
  raw descriptor failure.
- **Projection atomicity** — complete bytes at the final `0444` mode appear
  only through one atomic no-clobber link; collisions map to the existing
  `EffectiveConfigError`; no directory-durability claim is made; failures
  never leave a partial destination or a temporary sibling.
- **Facade compatibility** — path generation, runtime-root validation,
  lifecycle ownership, and every legacy injection seam are retained;
  publication is descriptor-relative.
- **Specialized blob behavior** — SRI validation, content-addressed
  publication, quarantine, revalidation, and cleanup remain under runtime
  domain authority.

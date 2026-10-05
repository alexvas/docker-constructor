# Phase 8 — npm-Environment Lock Migration (verification)

**Change:** `add-durable-filesystem-transactions`
**Phase:** 8 (tasks 8.1–8.9)
**Depends on:** Phase 1, Phase 2
**Result:** PASS — one shared npm input-identity `BLOCK` capability; compatible
L2 leaf-mechanic reuse; immutable-tree, advisory-index, quarantine, cleanup,
evidence, and cancellation protocols unchanged.

## Scope

Phase 8 migrates the npm environment assembler's hand-rolled
`prepare_identity_lock` + `fcntl.flock` pair onto the shared
`docker.transactions` substrate, while keeping every npm L3 protocol intact:

* **shared** — the input-identity `BLOCK` lock composes `LockCapability` over
  a descriptor-validated `DirectoryCapability`; the advisory index read/write
  and the manifest/evidence read reuse compatible L2 leaf contracts;
* **npm-owned (unchanged)** — assembler identity/schema, immutable-tree
  commit (`os.rename`), recursive fsync/sealing, evidence authority,
  collision handling, quarantine, recursive cleanup, advisory-index policy,
  and cancellation/cleanup semantics.

## RED (tasks 8.1–8.4)

Three focused modules were added before the production change:

* `tests/test_transactions_phase8_npm_lock.py` (task 8.1, 8.2) — input-identity
  scope, same-identity blocking, different-identity concurrency, post-lock
  lookup/publication ordering, release parity, and the shared entry-safety
  cases (symlink, non-regular, foreign-owned, hard-linked, wrong-mode,
  bootstrap race, replaced entry) mapped to the npm `unsafe_lock_path`
  diagnostic, plus operational raw-`OSError` parity.
* `tests/test_transactions_phase8_npm_specialization.py` (task 8.3) —
  immutable-output collision, recursive sealing, evidence authority,
  advisory-index best-effort failure, corrupt-output quarantine, cancellation,
  workspace cleanup, and primary-error preservation.
* `tests/test_transactions_phase8_npm_leaf_mechanics.py` (task 8.4) — the
  highest-compatible-contract selection and the explicit descent
  justification, without a shared generic tree publisher.

Against the pre-Phase-8 sources:

```text
tests.test_transactions_phase8_npm_lock
  Ran 16 tests in 0.323s
  FAILED (failures=1, errors=5)

tests.test_transactions_phase8_npm_leaf_mechanics
  Ran 7 tests in 0.011s
  FAILED (failures=5)
  # the module later gained one manifest/evidence-read assertion (8 tests).

tests.test_transactions_phase8_npm_specialization
  Ran 10 tests in 0.263s
  FAILED (errors=1)
```

The lock module failed because `identity_coordination_lock` did not expose an
L0 `ops` seam and did not reject a hard-linked entry; the leaf module failed
because the module had no shared-contract imports and `read_index` followed a
symlink; the specialization module failed because the advisory index write did
not go through the shared `RegularFileContracts`.

## GREEN (tasks 8.5–8.7)

### `docker/npm_environment/publication.py`

* The module now imports the shared substrate (`DirectoryCapability`,
  `CapabilityError`, `LockCapability`, `LockError`, `LockPolicy`,
  `PosixFileOps`, `RegularFileContracts`, the `STAGE_*` constants, and
  `attach_secondary`); `fcntl` is no longer imported.
* `identity_coordination_lock(namespace, input_identity, *, ops=None)`:
  * validates the identity and derives the canonical `<digest>.lock` basename;
  * opens the namespace `locks/` directory with
    `DirectoryCapability.from_secure_path` (absolute path, no-follow walk);
  * acquires the shared `LockCapability` under `namespace=input_identity` and
    the mandatory `LockPolicy.BLOCK`;
  * releases both capabilities exactly once on scope exit.
* `_identity_lock_failure` maps shared failures to the npm domain: an unsafe
  entry/directory (`STAGE_LOCK_VALIDATE`, `STAGE_LOCK_PREPARE`,
  `CapabilityError`) becomes `LockedNpmError("unsafe_lock_path", …)`; an
  operational descriptor-stat, mode-repair, acquisition, or lock-probe-close
  failure preserves its raw `OSError` (with attached cleanup diagnostics);
  process-control interruptions pass through unchanged.
* `_release_identity_lock` preserves an in-flight primary failure, attaching
  ordinary unlock/close failures as secondary diagnostics. A process-control
  interruption from the lock release (`KeyboardInterrupt` during unlock or
  during the lock-descriptor close) is captured so the directory capability
  is still released exactly once, and the original interruption is re-raised
  unchanged with any ordinary directory-close failure (or a subsequent
  directory interruption) attached as a secondary diagnostic. Neither
  capability is closed twice and a failed close is not retried.
* `read_index` reuses the shared L2 `RegularFileContracts.validated_read`
  through a `DirectoryCapability` for `namespace.index`, passing
  `allowed_mode=0o600` so the npm-owned index mode is enforced.
* `_append_index` reuses the shared L2 `RegularFileContracts.durable_replace`
  (complete write + file fsync, one rename, parent-directory fsync) and
  remains best-effort.
* `_read_private_regular` reuses `validated_read` for the immutable
  manifest/evidence reads inside `verify_output`, passing
  `allowed_mode=0o444` so the npm-owned immutable mode is enforced.
* `_durable_write` deliberately **descends to L0** for the manifest/evidence
  write: the L2 no-clobber/replacement contracts make a different, stronger
  durability claim (immediate parent fsync plus a temporary/link commit) that
  does not fit npm's explicit recursive-seal ordering, where the tree,
  manifest, and evidence are fsynced inside the temporary directory before the
  single npm-owned rename commits the output. The descent is documented in the
  function. The requested mode is established explicitly with `os.fchmod(fd,
  mode)` after the exclusive open and before the write/fsync, so it is exact
  and is not filtered by the process umask.

### `docker/npm_environment/storage.py`

* `prepare_identity_lock` was removed: entry preparation, validation, mode
  repair, and safety checks are now owned by the shared `LockCapability`. The
  namespace preparation and staging containment code is unchanged.
* `docker/npm_environment/__init__.py` no longer imports or re-exports
  `prepare_identity_lock`; `__all__` remains internally consistent.

### Retained specialization

Tree rename (`os.rename(str(tmp), str(final))`), recursive fsync/sealing
(`_fsync_tree`), the advisory index policy, quarantine
(`_quarantine_corrupt_output`), recursive cleanup (`_remove_redundant_tree`,
`_make_writable`), evidence authority, and cancellation/cleanup
(`_remove_staging_safely`, `_attach_cleanup_notes`) are untouched. The shared
substrate gained no tree, evidence, collision, quarantine, or cleanup
authority.

### Migrated existing tests

* `tests/test_npm_environment_storage.py::TestIdentityLocks` now drives
  `identity_coordination_lock` (including foreign-ownership via an injected
  `InjectedOps.fstat_override`).
* `tests/test_npm_environment_phase2_validate.py` acquires the shared lock
  instead of calling the removed `prepare_identity_lock`.
* `tests/test_npm_environment_phase5_introspection.py` pins the shared
  contract (`LockCapability.acquire`, `LockPolicy.BLOCK`, no `fcntl.flock`).
* `tests/test_npm_environment_concurrency_cache.py::TestCoordinationLock`
  asserts the shared capability is acquired with `BLOCK` and released.

## INTROSPECT (task 8.8)

The migration was reviewed against the targeted failure modes; all findings
were resolved or accepted deliberately:

| Risk | Finding | Resolution |
| --- | --- | --- |
| Split namespaces | The shared lock namespace is the input-identity digest inside the per-assembler `locks/` directory. | No split: same assembler + same input identity share one entry; different assemblers use different directories. |
| Cross-identity serialization | Different identities use different `<digest>.lock` entries. | `test_different_identities_are_independently_concurrent` passes. |
| Evidence leakage | The shared substrate only ever sees bytes and individual file names. | `SpecializationBoundaryTests` proves no evidence/tree authority crosses the boundary. |
| Changed waiting | `LockPolicy.BLOCK` uses the same blocking `flock(LOCK_EX)`. | `test_same_identity_blocks_until_release` and `TestCoordinationWait` pass. |
| Generic-tree abstraction | Tree commit stays a single npm `os.rename`; no shared tree publisher. | `test_tree_commit_remains_an_npm_rename`, `test_tree_commit_is_not_delegated_to_a_shared_tree_publisher`. |
| Advisory-index durability promotion | `durable_replace` adds a parent-directory fsync; the pre-migration code already fsynced the index directory. | No promotion; index stays non-authoritative and best-effort. |
| Quarantine authority | `_quarantine_corrupt_output` unchanged. | Corrupt-output quarantine tests pass. |
| Cancellation masking | `identity_coordination_lock` re-raises the in-flight `BaseException`; only ordinary release `OSError`s are attached. A release interruption still releases the directory capability before propagating. | `test_primary_failure_is_preserved_and_lock_released`, `ReleaseInterruptionTests`. |
| Build-generation coupling | npm uses no build generations. | No coupling introduced. |
| Exception drift | The old `prepare_identity_lock` wrapped every open failure as `unsafe_lock_path` while the reopen could surface a raw `OSError`; the shared lock classifies containment vs. operational by stage. | Containment (`validate`/`prepare`) stays `unsafe_lock_path`; operational (`stat`/`mode`/`acquire`/`close`) keeps its raw `OSError`; documented in `_identity_lock_failure`. A failed lock-probe close (`STAGE_CLOSE`) is operational I/O, not unsafe containment. |
| Incompatible helper reuse | `from_secure_path` requires an absolute path; `RegularFileContracts` is constructed with a default `PosixFileOps`. | Callers pass `os.path.abspath(...)`; the best-effort index/output readers catch `CapabilityError` and return a cache miss. |
| Mode-authority loss | The shared validated read defaults to accepting any owner-owned single-link regular file, which would let a widened index (`0644`) or manifest/evidence (`0644`) be trusted. | `read_index` passes `allowed_mode=0o600` and `_read_private_regular` passes `allowed_mode=0o444`, so the npm-owned modes are enforced; behavioral tests cover both rejections plus the accepted immutable modes. |

### Mutation checks (restored byte-for-byte)

| Mutation | Expected focused failures | Observed |
| --- | --- | --- |
| `LockPolicy.BLOCK` → `LockPolicy.FAIL_FAST` | same-identity blocking + concurrency-lock policy | `FAILED (failures=2)` |
| containment (`validate`/`prepare`) mapped to the raw `LockError` | every unsafe-entry case | `FAILED (errors=8)` |
| index write `durable_replace` → `atomic_no_clobber` | leaf-mechanic contract selection | `FAILED (failures=1)` |
| drop `allowed_mode` from the index/immutable reads | wrong-mode index/evidence/manifest rejections | `FAILED (failures=3)` |
| drop `os.fchmod(fd, mode)` from `_durable_write` | restrictive-umask exact-mode preservation | `FAILED (failures=1)` (`0o400 != 0o444`) |
| drop `STAGE_CLOSE` from the operational mapping | probe-close raw-`OSError` parity | `FAILED (errors=1)` (`LockedNpmError: identity lock path is unsafe`) |
| skip directory release on a lock-release interruption (pre-fix `_release_identity_lock`) | release-interruption directory cleanup | `FAILED (failures=4)` (`AssertionError: 0 != 1`) |

### Mode-preservation coverage (follow-up)

The shared validated read enforces the npm-owned leaf modes so that reusing
L2 does not widen npm's permission authority, and `_durable_write`
independently guarantees the written mode is exact:

* `read_index` passes `allowed_mode=0o600`;
* `_read_private_regular` (immutable manifest/evidence) passes
  `allowed_mode=0o444`;
* `_durable_write` calls `os.fchmod(fd, mode)` after the exclusive open and
  before the write/fsync, because the `os.open` mode argument is filtered by
  the process umask (a restrictive umask such as `0o077` would otherwise leave
  new manifest/evidence leaves at `0o400`, which the exact-mode read then
  refuses and which would break reuse).

Five behavioral tests were added in
`tests/test_transactions_phase8_npm_leaf_mechanics.py`
(`IndexLeafReuseTests.test_read_index_rejects_a_wrong_mode_index` and
`ImmutableReadModeTests`).  They assert that a `0644` index reads as absent
(and reads again once its mode is restored to `0600`, proving the mode is the
discriminator), that a `0644` evidence or manifest makes `verify_output`
return `None`, that published `0444` manifest/evidence still verify, and that
publishing under a `0o077` umask (restored in a `finally`) still yields exact
`0444` manifest/evidence that `verify_output` accepts.

Against the same code with the `allowed_mode` arguments removed:

```text
Ran 12 tests in 0.126s
FAILED (failures=3)
```

Only the three rejection tests fail; the positive immutable-mode control
passes in both states, confirming the tests discriminate on mode rather than
on the mere reuse of `validated_read`.  Against the same code with the
`os.fchmod(fd, mode)` call removed, the restrictive-umask test fails alone:

```text
Ran 1 test in 0.045s
FAILED (failures=1)
AssertionError: 256 != 292
```

(`256` is `0o400`, the umask-reduced mode; `292` is `0o444`).  The production
change was restored byte-for-byte after each mutation.

### Probe-close operational parity (follow-up)

A failed close of the post-acquisition identity-probe descriptor is raised by
the shared lock as `LockError(STAGE_CLOSE, …)` with the failing `OSError` as
its cause.  This is operational I/O, not unsafe containment, so
`_identity_lock_failure` includes `STAGE_CLOSE` with `STAGE_LOCK_STAT`,
`STAGE_LOCK_MODE`, and `STAGE_LOCK_ACQUIRE`: the original `OSError` is
returned (carrying any attached cleanup diagnostics) and is never mapped to
`LockedNpmError("unsafe_lock_path")`.  Containment still comes only from the
`validate`/`prepare` stages (and `CapabilityError`).

The regression test
`OperationalFailureParityTests.test_probe_close_failure_keeps_raw_oserror`
in `tests/test_transactions_phase8_npm_lock.py` first creates a valid lock
entry, tracks the probe descriptor, faults only that descriptor's close via
`InjectedOps`, and asserts that the raised exception is the injected raw
`OSError` (identity-preserved and not a `LockedNpmError`, with `errno ==
EIO`), that the lock is still unlocked and both the lock and directory
descriptors are still closed, and that a later normal acquisition succeeds.

Against the same code with `STAGE_CLOSE` removed from the operational tuple:

```text
Ran 1 test in 0.003s
FAILED (errors=1)
docker.npm_environment.errors.LockedNpmError: identity lock path is unsafe
```

The production change was restored byte-for-byte after the mutation.

### Release-interruption directory cleanup (follow-up)

`LockCapability.close` already attempts its unlock and descriptor close
exactly once and preserves a process-control interruption.  The npm release
wrapper must not let that interruption skip `directory.close()`, which would
leak the directory descriptor.  `_release_identity_lock` therefore captures a
`BaseException` from `capability.close()` (not only `LockError`/`OSError`),
still closes the directory capability exactly once, and re-raises the
original interruption unchanged; an ordinary directory-close failure (or a
second, newer interruption from the directory close) is attached to the
original interruption as a secondary diagnostic rather than replacing it.
Ordinary cleanup failures remain secondary to an active primary error, and
neither close is ever retried.

`tests/test_transactions_phase8_npm_lock.py::ReleaseInterruptionTests`
covers the seam with `InjectedOps` faults installed only after entry (and
after `ops.reset()`), so descriptor-number reuse during the secure walk
cannot confuse the checks: an interrupted unlock and an interrupted
lock-descriptor close each assert the directory close is
attempted exactly once and the same interruption object propagates; an
accompanying directory-close `OSError` is asserted to remain secondary to the
interruption; and release is exercised both after successful execution and
while a primary error is active.  Against the pre-fix `_release_identity_lock`
(interruption escaping before `directory.close()`):

```text
Ran 5 tests in 0.008s
FAILED (failures=4)
AssertionError: 0 != 1
```

Only the mutation-relevant tests fail; the primary-error secondary-diagnostic
control passes in both states.  The production change was restored
byte-for-byte after the mutation.

## VALIDATE (task 8.9)

* Focused Phase 8 suites — **45 tests, OK**
  (npm lock 22, leaf mechanics 13, specialization 10).
* npm preflight/execution/publication/storage/evidence/validation/
  concurrency/cancellation/smoke suites (`test_npm_*.py`) — **823 tests, OK
  (skipped=1)** in 31.1s.
* Complete repository suite — **5049 tests, OK (skipped=13)** in 99.4s, exit
  code `0` (the migration adds 45 focused Phase 8 tests).
* `ty check docker --python-version 3.14` — All checks passed.
* `openspec validate add-durable-filesystem-transactions --strict` — valid.
* `git diff --check` — clean.

### Unchanged protocols recorded

* **Tree commit** — one npm-owned `os.rename(str(tmp), str(final))` inside the
  temporary output directory; recursive fsync/sealing and read-only modes
  unchanged.
* **Advisory index** — still non-authoritative and best-effort; membership
  never establishes a cache hit; a failed write never fails publication.
* **Quarantine** — a corrupt committed output is still moved aside (never
  followed or deleted) and the validated reconstruction republished at the
  same identity; a quarantine failure preserves the corrupt bytes and fails
  publication.
* **Shared-lock behavior** — one `BLOCK` capability per input identity;
  different identities remain independently concurrent; release is
  unconditional; unsafe entries fail closed with `unsafe_lock_path`; the
  shared substrate holds no npm domain authority.

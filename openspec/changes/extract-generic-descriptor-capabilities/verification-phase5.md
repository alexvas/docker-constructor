# Phase 5 Verification Record — Transaction L1 Error Boundary

Date: 2026-10-06

Change: `extract-generic-descriptor-capabilities`

Scope: Phase 5 only. Phases 6–8 remain unstarted; Phase 9 not started.

## Deliverable

One typed public L1 boundary for operational directory open, descriptor stat,
regular-file read, and capability close failures. Raw POSIX causes stay
inspectable through `TransactionError.cause`/`.__cause__` without escaping as
the public exception, while capability misuse, unsafe objects, no-follow
`ELOOP`/`ENOTDIR` safety rejections, process-control exceptions, ownership
transfer, and cleanup precedence retain their distinct contracts.

## Production files changed

```text
docker/transactions/errors.py          # public STAGE_OPEN = "open-directory"
docker/transactions/capabilities.py    # normalized open/stat/read/close boundary
docker/transactions/regular.py         # validated_read typed-close passthrough
docker/npm_environment/publication.py  # advisory close suppression helper
docker/versioning/build_cleanup.py     # factory cause unwrap + typed close accumulator
docker/versioning/build_cache.py       # typed close accumulators; typed close propagation
docker/versioning/artifact_cache.py    # typed close accumulator; factory cause unwrap
docker/versioning/effective.py         # typed close accumulator
docker/versioning/project_state.py     # factory cause unwrap at the publication boundary
```

No public CLI, cache layout, npm identity, publication format, lock policy, or
durability contract changed. No stage value changed and no new transaction
aggregate export was added.

## Test files changed

```text
tests/test_transactions_l1_error_boundary.py        # new, 66 tests (all Phase 5 tasks)
tests/test_transactions_l1_capabilities.py          # revised raw-OSError expectations
tests/test_transactions_descriptor_integration.py   # revised raw-OSError expectations
tests/test_transactions_locking.py                  # cleanup helpers tolerate typed close
tests/test_transactions_phase7_runtime_lock.py      # typed close secondary expectation
tests/test_transactions_phase8_npm_lock.py          # typed close secondary expectation
tests/test_transactions_phase9b_remap_boundaries.py # extended remap inventory (12 -> 14)
tests/test_constructor_build_cleanup.py             # typed secondary / factory raw cause
tests/test_constructor_build_generation_integration.py  # typed close expectations
```

## RED evidence

Captured in a temporary detached worktree at the pre-change commit `4fd553d`
(the main working tree and git index were untouched; the worktree was removed
after capture). At HEAD, `STAGE_OPEN` does not exist:

```text
python -m unittest tests.test_transactions_l1_error_boundary
  -> ImportError: cannot import name 'STAGE_OPEN' from 'docker.transactions.errors'
     Ran 1 test ... FAILED (errors=1)
```

With only `STAGE_OPEN` stubbed to its specified literal (still at HEAD), the
new module fails on the pre-normalization behavior while the safety and L0
cases stay green:

```text
Ran 49 tests ... FAILED (failures=7, errors=12)
```

(The RED capture predates the two `StageOpenContractTests` cases added to pin
the exact `STAGE_OPEN` value; those also fail at HEAD because the name does not
exist.)

```text
errors (pre-normalization raw OSError / missing typed stage):
  OpenErrorNormalizationTests.from_path_* / from_secure_path_*          (5)
  StatErrorNormalizationTests.from_fd / from_path / from_secure_path    (3)
  ReadErrorNormalizationTests.test_read_all_failure_is_typed...         (1)
  ReleaseNormalizationTests.*_close_failure_is_typed...                 (3)

failures (pre-normalization behavior):
  ActivePrimaryCleanupTests.test_close_failure_is_secondary_to_primary
  FactoryMappingTests.test_artifact_cache_factory_mapping_unwraps_raw_cause
  RegularFileValidatedReadTests.test_success_then_close_failure_...
  RegularFileValidatedReadTests.test_read_failure_stays_primary_...
  RepositoryCloseInventoryTests.test_capability_close_accumulators_...
  RepositoryCloseInventoryTests.test_no_capability_close_is_wrapped_...
  RepositoryFactoryMappingInventoryTests.test_build_cleanup_factory_...
```

`NoFollowSafetyClassificationTests`, `CharacterizationTests`,
`PublicBoundaryClassificationTests` (misuse/safety/L0/signature) were green at
HEAD as required: the unaffected boundary is characterized before migration.

The revised legacy tests produced the expected RED during the session as each
production normalization landed, e.g. the 5.22 gate reported
`FAILED (failures=1, errors=17)` and the 5.23 gate reported
`FAILED (failures=4, errors=2)` before the consumer/test adaptations, and the
full discovery run reported `FAILED (failures=2)` before the remap-inventory
update.

## GREEN evidence

### Task 5.22 gate

```text
python -m unittest tests.test_transactions_l1_error_boundary \
  tests.test_transactions_descriptor_integration tests.test_transactions_l0_posix \
  tests.test_transactions_l1_capabilities tests.test_transactions_locking \
  tests.test_transactions_l2_atomic tests.test_transactions_l2_durable \
  tests.test_transactions_l2_lifecycle tests.test_transactions_phase9_boundaries
  -> Ran 319 tests in 1.740s
     OK
```

### Task 5.23 gate

```text
python -m unittest tests.test_npm_environment_publication_cleanup \
  tests.test_npm_environment_publication tests.test_transactions_phase9a_build_cache_cleanup \
  tests.test_transactions_phase9a_specialized tests.test_transactions_phase6_effective_projection \
  tests.test_constructor_build_cleanup tests.test_constructor_build_generation_integration \
  tests.test_transactions_phase9a_project_state_cleanup \
  tests.test_transactions_phase9b_remap_boundaries tests.test_transactions_phase6_project_metadata \
  tests.test_transactions_phase7_runtime_lock tests.test_transactions_phase8_npm_lock \
  tests.test_transactions_phase9a_descriptor_handoff
  -> Ran 277 tests in 2.930s
     OK
```

(The four modules after `project_state_cleanup` are the additional regression
modules identified by the task 5.7 and 5.10 inventories: the remap-boundary
architecture inventory and the three domain consumers whose factory/close
boundaries were adapted.)

### Full test suite

```text
python -m unittest discover -s tests -p 'test_*.py'
  -> Ran 5508 tests in 102.218s
     OK (skipped=13)
```

### Type check

```text
ty check docker --python-version 3.14 --output-format concise
  -> All checks passed!
```

## Boundary classification inventory (tasks 5.7 / 5.21)

Recorded in `tests/test_transactions_l1_error_boundary.py`:

- `RepositoryCloseInventoryTests` AST-scans every `docker/**/*.py` module:
  - every accumulator `CleanupFailures.run(<x>.close, ordinary=...)` site
    outside `docker/filesystem/` must include `TransactionError` (or the
    shared-lock `LockError` subclass, or the explicit raw-fd allowlist);
  - a pure `DirectoryCapability`/`FileCapability` close accumulator must
    **not** retain `OSError`, since a normalized capability close can no
    longer raise a raw POSIX failure;
  - every `Try` that closes a capability-shaped receiver must recognize a
    typed close error in at least one handler.
- `RepositoryFactoryMappingInventoryTests` pins the `build_cleanup.py`
  factory mapping.
- `_PUBLIC_BOUNDARY_CLASSIFICATION` records the misuse / safety / operational /
  process-control / cleanup classification for every public
  `DirectoryCapability` and `FileCapability` method.
- `PublicBoundaryClassificationTests.test_public_capability_oserror_handlers_are_classified`
  AST-verifies that every `except OSError` handler in the two capability
  classes wraps into `TransactionError`, `CapabilityError`, or
  `UnsafeFileError` rather than re-raising a raw `OSError`.
- Behavior tests verify every `TransactionError` raw cause is reachable
  through both `.cause` and `.__cause__`, and that retained `CapabilityError`
  safety rejections expose the raw error through `.__cause__` while adding no
  `.cause` contract.

### Raw `OSError` audit

After normalization, raw `except OSError` handling was removed from every
capability-close boundary and from every npm/versioning handler that no longer
invokes POSIX directly:

- npm advisory suppressors (`_suppress_advisory_close`) and `_append_index`
  catch only `TransactionError`/`ValueError`; `read_index` and `verify_output`
  likewise;
- `RegularFileContracts.validated_read()` no longer translates a raw read or
  close `OSError` — `FileCapability.read_all()`/`close()` are already typed;
- every `CleanupFailures.run(<capability>.close, ...)` uses
  `ordinary=(CloseStageFailure,)` (see the close-stage section below);
  `build_cleanup._open_algorithm_directory` catches only
  `(CapabilityError, TransactionError)`.

Retained raw `except OSError` sites are all on raw POSIX operations (raw `fd`
releases such as `os.close`/`ops.close`/`ops.openat`/`ops.fstat`, `os.rename`,
`os.stat`, `os.rmdir`) or the lock-capability closures that deliberately
re-raise the raw descriptor cause; the L0 `PosixFileOps` backend remains the
raw fault-injection boundary.

## Close-stage-only ordinary classification (follow-up)

Task 5.17/5.21 require every close boundary to “recognize only
`TransactionError` at `STAGE_CLOSE` … without swallowing unrelated
transaction failures”.  The initial Phase 5 adaptation used a broad
`ordinary=(TransactionError,)` (and an unconditional `except TransactionError`
in `build_cleanup._close_algorithm()`), which would demote an unrelated
read/validate/open failure surfaced by a release action to an ordinary close
diagnostic.

Because `CleanupFailures.run` classifies by exception type and its signature
is frozen by the change design (`test_filesystem_cleanup`
`test_public_signatures_are_exact`), the restriction is expressed with a
classification marker in `docker.transactions.errors`:

- `CloseStageFailure` is a `TransactionError` subclass that is **never raised**.
  Its metaclass `_CloseStageMeta.__instancecheck__` matches an instance only
  when `isinstance(instance, TransactionError) and instance.stage ==
  STAGE_CLOSE`.
- Passing `ordinary=(CloseStageFailure,)` therefore treats only a close-stage
  failure as ordinary; any other `TransactionError` stage reaches the
  accumulator as an authoritative unexpected defect (and, without a primary,
  is raised unchanged).
- Exception matching (`except`) does not consult `__instancecheck__`, so the
  marker is documented as valid only inside an `ordinary` tuple.

Applied to all 14 capability-close accumulators
(`capabilities.DirectoryCapability.__exit__`, `regular.validated_read`,
`publication._release_identity_lock`/acquisition cleanup,
`build_cleanup` ×3, `artifact_cache` ×2, `build_cache` ×4, `effective`),
and `build_cleanup._close_algorithm()` now re-raises a non-close-stage
`TransactionError` from its unpaired-close suppressor instead of swallowing
it.  The repository inventory test now asserts the marker (not the broad
`TransactionError`) at every capability-close accumulator and still forbids
raw `OSError`.

### Behavioral coverage (`CloseStageSemanticsTests`, 8 tests)

Each handler pattern injects a non-close-stage `TransactionError` and asserts
it stays authoritative:

- marker matches only `STAGE_CLOSE` (`isinstance` unit test);
- `DirectoryCapability` context exit with an active primary — the unrelated
  error replaces the primary and carries the primary as secondary;
- `RegularFileContracts.validated_read` (no primary) — the unrelated error is
  raised unchanged;
- npm `_suppress_advisory_close` and `_release_identity_lock` — the unrelated
  error propagates;
- `build_cleanup._close_algorithm(failure=None)` — a close-stage failure is
  suppressed, an unrelated failure propagates;
- `build_cleanup._close_algorithm(failure=...)` — an unrelated failure is
  raised with the aggregated domain failure attached as secondary.

### RED evidence

Reverting `capabilities.py` to `ordinary=(TransactionError,)` and restoring
`build_cleanup._close_algorithm()`'s unconditional `except TransactionError`:

```text
python -m unittest \
  ...test_directory_context_non_close_error_displaces_the_primary \
  ...test_close_algorithm_unpaired_non_close_error_propagates
  -> FAILED (failures=1, errors=1)
```

The production files were restored byte-for-byte after the mutation.

## Lock contract audit (follow-up)

Requested follow-up on the shared `LockCapability` trust boundary.  The
shared L2 lock is fully typed: `LockCapability.close()` (via
`_release_descriptor()`) **never** raises a raw `OSError`.  With no primary it
raises `LockError(stage, ...)` whose `.cause`/`.__cause__` is the failing
`OSError`; with an active primary it attaches the ordinary failure as a
secondary diagnostic and lets the primary propagate.  The L3 domain adapters
(`publication._release_identity_lock`, `build_cache`
`ConstructorProjectBuildLock.release`, `artifact_cache.FileIdentityLock.release`)
then *deliberately* downgrade the typed `LockError` back to the raw `OSError`
cause to preserve the pre-extraction release parity (design: "retain
observable raw `OSError` values").  Consequently, at the closure sites
`OSError` is reachable and `LockError` is a defensive superset; at the direct
`failures.run(capability.close, ...)` site `LockError` is reachable and
`OSError` is a defensive superset.  Both are intentional and were left as-is.

One genuine defect was found and fixed: the runtime adapter
`artifact_cache._identity_lock_failure()` omitted the post-acquisition probe
close stage `STAGE_CLOSE` from its operational set, so a failed close of the
identity-probe descriptor was misreported as an unsafe-entry containment error
(`ArtifactMaterializationError("containment", "identity lock path is unsafe")`)
instead of preserving the raw `OSError`.  The npm adapter
(`publication._identity_lock_failure`) already unwrapped `STAGE_CLOSE` (the
prior `add-durable-filesystem-transactions` phase 8 follow-up), and
`build_cache._raise_lock_failure` unwraps any `OSError` cause, so the runtime
domain was the odd one out.  Fixed by adding `STAGE_CLOSE` to the operational
tuple and documenting it.

### RED evidence (runtime probe-close parity)

Before the fix:

```text
LockError(STAGE_CLOSE, cause=OSError(EIO))
  artifact -> ArtifactMaterializationError containment
  npm      -> OSError
```

Mutating the fix back out (dropping `STAGE_CLOSE` from the tuple) makes the new
regression test fail:

```text
python -m unittest
  tests.test_transactions_phase7_runtime_lock.OperationalLockFailureTests.test_probe_close_failure_keeps_raw_oserror
  -> FAILED (errors=1)
     docker.versioning.artifact_cache.ArtifactMaterializationError: identity lock path is unsafe
```

The production file was restored byte-for-byte after the mutation.

### Regression coverage

- `tests/test_transactions_phase7_runtime_lock.py`
  `OperationalLockFailureTests.test_probe_close_failure_keeps_raw_oserror` —
  end-to-end: a valid lock entry reaches the identity probe, only the probe
  descriptor's `close` is faulted, and the exact raw `OSError` (not a
  containment error) is raised while the lock and directory are still
  released and a fresh acquisition succeeds.
- `tests/test_transactions_l1_error_boundary.py` `LockFailureMappingTests` —
  inventory: every operational lock stage (`STAGE_LOCK_STAT`,
  `STAGE_LOCK_MODE`, `STAGE_LOCK_ACQUIRE`, `STAGE_CLOSE`) is unwrapped to the
  raw `OSError` by the npm, runtime, and build-cache mappers; `validate`/
  `prepare` stay containment in both adapters; the npm and runtime adapters
  must agree stage-by-stage; and `LockCapability.close()` is pinned to raise
  `LockError` (never a raw `OSError`) with the raw error as `.cause` and
  `.__cause__`.

### Results

```text
python -m unittest tests.test_transactions_locking \
  tests.test_transactions_phase7_runtime_lock tests.test_transactions_phase8_npm_lock \
  tests.test_transactions_phase9a_audit tests.test_transactions_l1_error_boundary
  -> Ran 164 tests ... OK

python -m unittest discover -s tests -p 'test_*.py'
  -> Ran 5508 tests in 102.218s
     OK (skipped=13)

ty check docker --python-version 3.14 --output-format concise
  -> All checks passed!
```

## Rendering consumer migration (follow-up)

`docker/versioning/rendering.py::write_effective_build` was omitted from the
first Phase 5 consumer pass even though it calls
`DirectoryCapability.from_fd()` and owns the retained `generated` descriptor.

### Production changes

- The generated-directory factory now runs inside a
  `try/except TransactionError`.  A typed operational `STAGE_VALIDATE` failure
  is translated back to its raw `OSError` cause (with
  `carry_secondary_diagnostics(cause, exc)`), preserving the pre-Phase-5 public
  contract; a non-operational capability failure and every process-control
  interruption propagate unchanged.
- The unconditional
  `failures.run(lambda: ops.close(generated_fd), ordinary=(OSError,))` was
  removed.  Cleanup is split by adoption state:
  - `generated is not None` →
    `failures.run(generated.close, ordinary=(CloseStageFailure,))`;
  - adoption failed but `generated_fd` was opened →
    `failures.run(lambda: ops.close(generated_fd), ordinary=(OSError,))`.
  `CleanupFailures(primary)` still keeps an active primary authoritative.
- A close-stage failure is therefore reported as a typed
  `TransactionError(STAGE_CLOSE)` whose `.cause`/`.__cause__` is the raw
  `OSError` (consistent with `build_cleanup`); with an active publication
  failure it is attached as a typed secondary diagnostic while the raw
  publication `OSError` stays primary.

### Test coverage (`GeneratedAdoptionOwnershipTests`, 7 tests)

- injected second `fstat` from `from_fd()` preserves the original raw
  `OSError` identity (not a `TransactionError`);
- retained secondary diagnostics survive the typed-wrapper-to-raw-cause
  translation;
- successful adoption closes through `DirectoryCapability.close()` exactly
  once (and the raw descriptor exactly once);
- failed adoption closes the still caller-owned raw descriptor exactly once
  and never through a capability;
- close failure without a primary surfaces the typed `STAGE_CLOSE` error with
  the raw cause, and with an active primary is attached as typed secondary;
- `KeyboardInterrupt`/`SystemExit` during adoption are unwrapped and
  authoritative with no secondary diagnostics, and the descriptor is still
  released once.

### Inventory

`docker/versioning/rendering.py` is now in both the required cleanup-consumer
set and the required factory-mapping inventory, and
`test_transactions_phase9b_remap_boundaries` counts its second
`carry_secondary_diagnostics` remap site.

### RED evidence

Reverting the `from_fd` translation and the ownership-split cleanup (restoring
code that still closes the raw descriptor unconditionally):

```text
python -m unittest tests.test_transactions_phase6_effective_projection.GeneratedAdoptionOwnershipTests
  -> FAILED (failures=2, errors=4)  (6 of 7 new tests)
```

`rendering.py` was restored byte-for-byte after the mutation.

### Results

```text
python -m unittest tests.test_transactions_phase6_effective_projection
  -> Ran 32 tests ... OK

python -m unittest <task 5.22 command>
  -> Ran 319 tests ... OK

python -m unittest <task 5.23 command>
  -> Ran 203 tests ... OK

python -m unittest discover -s tests -p 'test_*.py'
  -> Ran 5508 tests in 102.218s
     OK (skipped=13)

ty check docker --python-version 3.14 --output-format concise
  -> All checks passed!
```

## Summary

| Task | Status |
| --- | --- |
| 5.1–5.12 (RED / characterization) | complete |
| 5.13–5.19 (GREEN) | complete |
| 5.20–5.21 (INTROSPECT) | complete |
| 5.22–5.23 (VALIDATE) | complete |

`openspec validate extract-generic-descriptor-capabilities --strict` is a
Phase 9 task (9.11); the change artifacts remain valid and were not edited
beyond the Phase 5 task checkboxes.

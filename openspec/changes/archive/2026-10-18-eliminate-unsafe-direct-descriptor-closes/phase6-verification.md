# Phase 6 — Residual Production Close Migration (complete)

**Status:** COMPLETE. Tasks **6.1–6.18** (RED) and **6.19–6.38** (GREEN,
INTROSPECT, VALIDATE) are complete and verified. Phase 7 remains.

**Schema:** `spec-driven` · **Change:** `eliminate-unsafe-direct-descriptor-closes`

This phase is substantially larger than Phases 1–5: it spans 13 production
modules and migrates every residual consumer-owned descriptor release
(including `os.fdopen(..., closefd=True)` and unadopted `tempfile.mkstemp()`
descriptors) to shared ownership.  It is being completed module-by-module so
that each finished migration keeps the full suite green rather than risking a
partially-migrated core module.  The npm-publication, transaction,
artifact-cache, build-cache, project-state, snapshot, and build-cleanup
subgroups are complete; the remaining modules continue task-by-task in later
turns.

## Completed slice

| Task | Artifact | Result |
| ---- | -------- | ------ |
| 6.1 | `tests/test_descriptor_close_phase6_npm.py` | RED (6 failures / 2 errors) then GREEN |
| 6.2–6.4 | `tests/test_descriptor_close_phase6_transactions.py` | RED (6 failures) then GREEN |
| 6.5–6.7 | `tests/test_descriptor_close_phase6_artifact_cache.py` | RED (21 of 22 fail) then GREEN |
| 6.8–6.9 | `tests/test_descriptor_close_phase6_build_cache.py` | RED (16 of 17 fail) then GREEN |
| 6.10–6.11 | `tests/test_descriptor_close_phase6_project_state.py` + `tests/test_transactions_phase9a_project_state_cleanup.py` | RED (10 of 12 and 3 of 10 fail) then GREEN |
| 6.12 | `tests/test_descriptor_close_phase6_build_snapshot.py` | RED (7 of 7 fail) then GREEN |
| 6.13 | `tests/test_descriptor_close_phase6_build_cleanup.py` | RED (4 of 5 fail) then GREEN |
| 6.14 | `tests/test_descriptor_close_phase6_cache_storage.py` | RED (2 failures + 5 errors) then GREEN |
| 6.15 | `tests/test_descriptor_close_phase6_rendering.py` | RED (6 of 7 fail) then GREEN |
| 6.16 | `tests/test_descriptor_close_phase6_build_materialization.py` | RED (4 of 4 fail) then GREEN |
| 6.17 | `tests/test_descriptor_close_phase6_rendering.py` | RED (6 of 7 fail) then GREEN |
| 6.18 | `tests/test_descriptor_close_phase6_cache.py` | RED (3 of 3 fail) then GREEN |
| 6.19 | `docker/npm_environment/publication.py` | `_durable_write` / `_fsync_dir` / `_fsync_tree` / `_atomic_publish` transfer the fd immediately to `OwnedDescriptor(PosixDescriptorOps(), fd, ...)` and release through `owner.close` |
| 6.20 | `docker/transactions/capabilities.py` | `_adopt` / `open_regular` own the fd before validation and release the owner on failure; capability `close()` methods keep their existing terminal at-most-once state |
| 6.21 | `docker/transactions/locking.py` | `_verify_unchanged` probe and `_release_descriptor` release through `OwnedDescriptor` |
| 6.22 | `docker/transactions/regular.py` | `_Publication` now holds an `OwnedDescriptor` (with a compatibility `fd` property); `_allocate_temporary`, `_close_fd`, `_discard.close_descriptor`, `durable_unlink`, and `_validate_destination` release through the owner |
| 6.23–6.25 | `docker/versioning/artifact_cache.py` | every descriptor-relative traversal, inspection, mutation, temporary-directory, and identity-lock release now transfers to `OwnedDescriptor`; `_open_directory_chain` hands off a detached live fd; `FileIdentityLock.acquire` releases its pre-adoption descriptor through a shared owner |
| 6.26 | `docker/versioning/build_cache.py` (namespace/state) | `_open_namespace_fd`, `_validate_relative_dir`, `_ensure_relative_dir`, `prepare_build_cache`, `_open_validated_child`, and `open_build_cache_state` adopt each descriptor into `OwnedDescriptor` with verified detach handoffs; `BuildCacheState.close` aggregates its owners |
| 6.27 | `docker/versioning/build_cache.py` (publication/recovery) | `publish_verified_blob`, `_validate_blob_descriptor`, `_verify_published_blob`, `_BuildStorageHandle._open`, `acquire_constructor_project_build_lock`, `_open_blob_algorithm`, and `_remove_snapshot_tree` release through shared owners (capability adoption detaches the tracker) |
| 6.28 | `docker/versioning/project_state.py` (private dir / metadata / resolution) | `_open_private_dir_at` adopts its descriptor and transfers it on success; `_read_metadata` and `resolve_project_state` release every descriptor through `_adopt` + shared `CleanupFailures` |
| 6.29 | `docker/versioning/project_state.py` (`validate_project_state`) | every ancestor release and the retained-namespace transfer delegate to shared owners while preserving aggregate cleanup, ownership-clearing order, at-most-once, failure precedence, and returned-state ownership |
| 6.30 | `docker/versioning/build_snapshot.py` | `_open_validated_source` adopts and detaches its source blob; `_validate_hard_link_destination` releases through a shared owner; `create_artifact_snapshot` adopts each source and the staging directory into `OwnedDescriptor`s and releases them through shared precedence |
| 6.31 | `docker/versioning/build_cleanup.py` | `_close_leaf` takes an `OwnedDescriptor`; `_unlink_entry` adopts its leaf and transfers/releases it; `_open_algorithm_directory` adopts the candidate and `detach()`es on adoption success, releasing through the owner on every pre-adoption failure |
| 6.32 | `docker/versioning/cache_storage.py` | `open_private_entry` adopts the opened regular file into an `OwnedDescriptor` and returns `owner.detach()`; `_release_file_and_capability` / `_release_file_keeping_primary` now take the owner and release through `owner.close` |
| 6.33 | `docker/versioning/rendering.py` | `_validate_effective_destination` and `write_effective_build` adopt their descriptors and release through local owners (`generated_owner.detach()` on adoption, `generated_owner.close` before adoption) |
| 6.34 | `docker/versioning/build_materialization.py` | `materialize_artifact` adopts the `mkstemp()` fd into `output_owner` immediately and releases it through `owner.close` via `os.fdopen(owner.fd, "wb", closefd=False)` |
| 6.35 | `docker/versioning/rendering.py` | `write_effective_inventory` adopts the `mkstemp()` fd immediately and releases it through `owner.close` via `os.fdopen(owner.fd, ..., closefd=False)` before publication |
| 6.36 | `docker/versioning/cache.py` | `DiskCache.set` adopts the `open_private_entry()` fd immediately and releases it through `owner.close` via `os.fdopen(owner.fd, ..., closefd=False)` before publication |
| 6.37 | `tests/test_descriptor_close_convergence.py` | generated per-function ownership ledger over 64 migrated functions/methods with symbol-origin resolution (fdopen/mkstemp aliases, capability factories, parallel-owner assignment, injected owner parameters, proven-terminal backend closes) |
| 6.38 | focused + full suites | final validation: 1247-test focused batch `OK (skipped=3)`, full suite 5976 `OK (skipped=13)`, `ty check` clean, `git diff --check` clean |

### Shared test support

`tests/descriptor_ownership_support.py` patches a production module's
`OwnedDescriptor` symbol with a recording subclass and wraps the
production-supplied descriptor backend.  It records, per owner, whether
ownership was terminal before the close syscall and whether any owner received
more than one close attempt, and it can inject ordinary close faults (ordered
or selected by fd/owner).  Every migrated release is asserted to be
terminal-before-close and at-most-once, and operation-primary precedence is
asserted by injecting an operation failure plus a close failure.

## RED evidence (pre-migration)

`tests/test_descriptor_close_phase6_npm.py` and
`tests/test_descriptor_close_phase6_transactions.py` were run against the
Phase 5 production (before the edits above):

```text
$ .venv/bin/python -m unittest tests.test_descriptor_close_phase6_npm
Ran 9 tests
FAILED (failures=6, errors=2)
# 2 characterization tests passed (bytes/mode; capability close state)

$ .venv/bin/python -m unittest tests.test_descriptor_close_phase6_transactions
FAILED (failures=6)
# 2 characterization tests passed (directory/file capability close)
```

The pre-migration defects demonstrated are the absence of any shared terminal
owner for these releases (each used `CleanupFailures(...).run(lambda: os.close(fd))`
or a bare `ops.close`), so a `OwnerFor(fd)` assertion could observe no owner,
no terminal transition, and no at-most-once guarantee.

## GREEN evidence (post-migration)

```text
$ .venv/bin/python -m unittest tests.test_descriptor_close_phase6_npm
Ran 9 tests in 0.040s
OK

$ .venv/bin/python -m unittest tests.test_descriptor_close_phase6_transactions
Ran 8 tests in 0.003s
OK
```

Focused regression batches:

```text
$ .venv/bin/python -m unittest \
    tests.test_npm_environment_publication \
    tests.test_npm_environment_publication_cleanup \
    tests.test_npm_environment_phase9a_staging_cleanup \
    tests.test_transactions_phase8_npm_leaf_mechanics \
    tests.test_transactions_phase8_npm_specialization \
    tests.test_descriptor_close_phase6_npm
Ran 66 tests
OK

$ .venv/bin/python -m unittest \
    tests.test_transactions_l1_capabilities tests.test_transactions_locking \
    tests.test_transactions_l2_durable tests.test_transactions_l2_atomic \
    tests.test_transactions_l2_lifecycle tests.test_transactions_l0_posix \
    tests.test_transactions_phase8_npm_lock tests.test_transactions_phase8_npm_leaf_mechanics \
    tests.test_transactions_phase9a_specialized tests.test_transactions_phase5a_typed_preservation
Ran 297 tests
OK

$ .venv/bin/python -m unittest \
    tests.test_transactions_phase9a_audit tests.test_transactions_l1_error_boundary \
    tests.test_descriptor_close_phase6_transactions tests.test_descriptor_close_phase6_npm
Ran 94 tests
OK

$ .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
Ran 5875 tests in 100.759s
OK (skipped=13)
```

`tests/test_descriptor_close_phase6_npm.py` + `..._transactions.py` add 17 tests
over the Phase 5 baseline (5858 → 5875).

## Artifact-cache module (tasks 6.5–6.7 / 6.23–6.25)

RED and GREEN, both over the 22-test
`tests/test_descriptor_close_phase6_artifact_cache.py`:

```text
$ .venv/bin/python -m unittest tests.test_descriptor_close_phase6_artifact_cache
# against git show HEAD:docker/versioning/artifact_cache.py (pre-Phase-6)
Ran 22 tests in 0.015s
FAILED (failures=21)

# after migration (file restored byte-identical with cmp)
Ran 22 tests in 0.011s
OK
```

Migrated release sites: `inspect_verified_blob_readonly` (three owners),
`_ensure_dir_private` (parent + child), `_open_directory_chain` (retained
parent
handoff returns a detached live fd), `_remove_tree_at`,
`LocalCacheFilesystem.{stat_blob,read_bytes,inspect_and_digest}`,
`{ensure_secure_dir,create_temp,append_temp,finalize_temp,cleanup_temp,quarantine_or_remove,get_permissions,set_permissions,atomic_publish}`,
`LocalTemporaryDirectory.mkdtemp`, and `FileIdentityLock.acquire` (the
pre-adoption descriptor is a shared owner that detaches on adoption).

Regression batches:

```text
$ .venv/bin/python -m unittest \
    tests.test_cache_release_ordering_phase3 tests.test_cache_root_ownership_phase3 \
    tests.test_constructor_cache_contracts tests.test_transactions_phase7_artifact_specialization \
    tests.test_transactions_phase7_runtime_lock tests.test_transactions_phase9a_descriptor_handoff \
    tests.test_transactions_phase9a_ambient_primary tests.test_transactions_phase9_boundaries \
    tests.test_transactions_phase9b_remap_boundaries tests.test_constructor_build_cache_permissions \
    tests.test_constructor_build_digest_identity tests.test_descriptor_close_phase6_artifact_cache
Ran 210 tests
OK (skipped=1)

$ .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
Ran 5897 tests in 101.836s
OK (skipped=13)
```

The 22 new tests raise the full-suite baseline from 5875 to 5897.

`ty check docker --python-version 3.14 --output-format concise` reports only
the two pre-existing `@contextmanager` deprecation warnings in
`docker/npm_environment/publication.py` and
`docker/versioning/assembly_activity.py`.

## Build-cache module (tasks 6.8–6.9 / 6.26–6.27)

RED and GREEN, both over the 17-test
`tests/test_descriptor_close_phase6_build_cache.py`:

```text
$ .venv/bin/python -m unittest tests.test_descriptor_close_phase6_build_cache
# against git show HEAD:docker/versioning/build_cache.py (pre-Phase-6)
Ran 17 tests in 0.136s
FAILED (failures=16)

# after migration
Ran 17 tests in 0.131s
OK
```

Migrated release sites: `_open_namespace_fd`, `_validate_relative_dir`,
`_ensure_relative_dir`, `prepare_build_cache`, `_open_validated_child`,
`open_build_cache_state` (adopting each verified child and transferring the
namespace during handoff), `publish_verified_blob`,
`_validate_blob_descriptor`, `_verify_published_blob`,
`_BuildStorageHandle._open`, `acquire_constructor_project_build_lock`,
`_open_blob_algorithm`, and `_remove_snapshot_tree`.  `_validate_existing_blob`
and `_validate_existing_marker` were already shared-capability based and stay
characterized (no build-cache-local owner).

Regression batches:

```text
$ .venv/bin/python -m unittest \
    tests.test_descriptor_close_phase6_build_cache \
    tests.test_constructor_build_cache_paths \
    tests.test_constructor_build_cache_permissions \
    tests.test_constructor_build_cache_state_lifecycle \
    tests.test_constructor_build_transactions \
    tests.test_constructor_build_generation_integration \
    tests.test_constructor_build_persistence \
    tests.test_constructor_build_orchestration \
    tests.test_constructor_build_materialization \
    tests.test_transactions_phase9a_build_cache_cleanup \
    tests.test_retained_tail_phase6 tests.test_host_download_observability \
    tests.test_descriptor_close_convergence tests.test_transactions_l1_error_boundary
Ran 504 tests in 33.110s
OK (skipped=1)

$ .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
Ran 5914 tests in 100.417s
OK (skipped=13)
```

The 17 new tests raise the full-suite baseline from 5897 to 5914.
`ty check docker --python-version 3.14 --output-format concise` still reports
only the two pre-existing `@contextmanager` deprecation warnings.

## Project-state module (tasks 6.10–6.11 / 6.28–6.29)

RED and GREEN.  The characterization lives in two suites: the new 12-test
`tests/test_descriptor_close_phase6_project_state.py` and the three added
delegation tests in `tests/test_transactions_phase9a_project_state_cleanup.py`.

```text
$ .venv/bin/python -m unittest tests.test_descriptor_close_phase6_project_state
# against git show HEAD:docker/versioning/project_state.py (pre-Phase-6)
Ran 12 tests in 0.113s
FAILED (failures=10)

# after migration
Ran 12 tests in 0.120s
OK

$ .venv/bin/python -m unittest tests.test_transactions_phase9a_project_state_cleanup
# pre-migration: only the 3 new shared-owner delegation tests fail
Ran 10 tests in 0.011s
FAILED (failures=3)

# after migration
Ran 10 tests in 0.010s
OK
```

Migrated release sites: `_open_private_dir_at` (both failure branches now call
`_release(failures, owner)`; the success path returns `owner.detach()`),
`_read_metadata` (metadata owner released in `finally`), `resolve_project_state`
(every opened directory is adopted and released in reverse order), and every
`validate_project_state` release branch (ancestors, the retained namespace
handoff, and the `ancestor_failure` path).  `ValidatedProjectState` already
owned its namespace descriptor (Phase 3) and is unchanged.  A module-level
`_adopt(fd, *, label)` / `_release(failures, owner)` pair centralizes the
consumer-boundary transfer and keeps the release receivers owner-shaped for the
repository inventory.

Regression batch:

```text
$ .venv/bin/python -m unittest \
    tests.test_descriptor_close_phase6_project_state \
    tests.test_transactions_phase9a_project_state_cleanup \
    tests.test_constructor_build_cache_paths \
    tests.test_constructor_build_cache_permissions \
    tests.test_constructor_build_cache_directory_handoff \
    tests.test_constructor_build_cache_state_lifecycle \
    tests.test_constructor_build_orchestration \
    tests.test_constructor_build_materialization \
    tests.test_constructor_acceptance \
    tests.test_cache_release_ordering_phase3 \
    tests.test_cache_root_ownership_phase3 \
    tests.test_transactions_phase9a_descriptor_handoff \
    tests.test_transactions_l1_error_boundary \
    tests.test_descriptor_close_convergence
Ran 401 tests in 7.214s
OK (skipped=1)

$ .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
Ran 5929 tests in 99.772s
OK (skipped=13)
```

The 15 new tests raise the full-suite baseline from 5914 to 5929.
`ty check docker --python-version 3.14 --output-format concise` still reports
only the two pre-existing `@contextmanager` deprecation warnings.

## Snapshot module (tasks 6.12 / 6.30)

RED and GREEN, both over the 7-test
`tests/test_descriptor_close_phase6_build_snapshot.py`:

```text
$ .venv/bin/python -m unittest tests.test_descriptor_close_phase6_build_snapshot
# against git show HEAD:docker/versioning/build_snapshot.py (pre-Phase-6)
Ran 7 tests in 0.021s
FAILED (failures=7)

# after migration
Ran 7 tests in 0.020s
OK
```

Migrated release sites: `_open_validated_source` (source-blob owner released on
validation failure, `owner.detach()` on success), `_validate_hard_link_destination`
(hard-link owner released in `finally`), and `create_artifact_snapshot` (each
source owner and the staging-directory owner release through shared
precedence).  `_copy_source_fd_to_destination` already owned its destination
descriptor in Phase 3 and is unchanged.

Regression batch:

```text
$ .venv/bin/python -m unittest \
    tests.test_descriptor_close_phase6_build_snapshot \
    tests.test_constructor_build_snapshot \
    tests.test_constructor_pi_snapshot \
    tests.test_constructor_build_orchestration \
    tests.test_transactions_l1_error_boundary \
    tests.test_descriptor_close_convergence \
    tests.test_descriptor_close_phase6_project_state \
    tests.test_descriptor_close_phase6_build_cache \
    tests.test_descriptor_close_phase6_artifact_cache
Ran 330 tests in 6.682s
OK (skipped=2)

$ .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
Ran 5936 tests in 99.640s
OK (skipped=13)
```

The 7 new tests raise the full-suite baseline from 5929 to 5936.
`ty check docker --python-version 3.14 --output-format concise` still reports
only the two pre-existing `@contextmanager` deprecation warnings.

## Build-cleanup module (tasks 6.13 / 6.31)

RED and GREEN, both over the 5-test
`tests/test_descriptor_close_phase6_build_cleanup.py`:

```text
$ .venv/bin/python -m unittest tests.test_descriptor_close_phase6_build_cleanup
# against git show HEAD:docker/versioning/build_cleanup.py (pre-Phase-6)
Ran 5 tests in 0.004s
FAILED (failures=4)

# after migration
Ran 5 tests in 0.003s
OK
```

Migrated release sites: `_close_leaf` now releases an `OwnedDescriptor`;
`_unlink_entry` adopts its ``O_NOFOLLOW`` leaf and releases it on interruption
or completion; `_open_algorithm_directory` adopts the candidate directory and
``detach()``es on adoption success, releasing through the owner on either
pre-adoption failure branch.  The adopted `DirectoryCapability` release path
stays capability-typed (``CloseStageFailure``) and is unchanged.

Regression batch:

```text
$ .venv/bin/python -m unittest \
    tests.test_descriptor_close_phase6_build_cleanup \
    tests.test_constructor_build_cleanup \
    tests.test_transactions_phase5a_typed_preservation \
    tests.test_transactions_phase9a_build_cache_cleanup \
    tests.test_transactions_phase9b_remap_boundaries \
    tests.test_transactions_l1_error_boundary \
    tests.test_descriptor_close_convergence \
    tests.test_constructor_build_generation_integration
Ran 316 tests in 5.230s
OK

$ .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
Ran 5941 tests in 100.176s
OK (skipped=13)
```

The 5 new tests raise the full-suite baseline from 5936 to 5941.
`ty check docker --python-version 3.14 --output-format concise` still reports
only the two pre-existing `@contextmanager` deprecation warnings.

## Cache-storage module (tasks 6.14 / 6.32)

RED and GREEN, both over the 7-test
`tests/test_descriptor_close_phase6_cache_storage.py`:

```text
$ .venv/bin/python -m unittest tests.test_descriptor_close_phase6_cache_storage
# against git show HEAD:docker/versioning/cache_storage.py (pre-Phase-6)
Ran 7 tests in 0.005s
FAILED (failures=2, errors=5)

# after migration
Ran 7 tests in 0.003s
OK
```

Migrated release sites: `open_private_entry` now adopts the just-opened regular
file with `OwnedDescriptor(descriptor_ops, fd, label=...)` and returns
`owner.detach()` only after the owning directory capability closes cleanly;
`_release_file_and_capability(owner, capability, primary)` and
`_release_file_keeping_primary(owner, primary)` take the owner and release
through `owner.close` (terminal before the syscall, never retried).  The
private helpers changed signature; no other production or test caller used
them.  The pre-existing `tests/versioning/test_cache_storage_capabilities.py`
precedence, at-most-once, double-close, and independent-release assertions all
still pass unchanged.

Regression batch:

```text
$ .venv/bin/python -m unittest \
    tests.test_descriptor_close_phase6_cache_storage \
    tests.versioning.test_cache_storage_capabilities \
    tests.versioning.test_cache_storage_security \
    tests.test_constructor_cache_contracts \
    tests.test_version_cache \
    tests.test_cache_release_ordering_phase3 \
    tests.test_cache_root_ownership_phase3 \
    tests.test_transactions_phase5a_typed_preservation \
    tests.test_transactions_l1_error_boundary \
    tests.test_filesystem_convergence \
    tests.test_descriptor_close_convergence
Ran 354 tests in 6.035s
OK

$ .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
Ran 5948 tests in 101.600s
OK (skipped=13)
```

The 7 new tests raise the full-suite baseline from 5941 to 5948.
`ty check docker --python-version 3.14 --output-format concise` still reports
only the two pre-existing `@contextmanager` deprecation warnings.

## Rendering module (tasks 6.15 / 6.33, 6.17 / 6.35)

RED and GREEN over the 9-test
`tests/test_descriptor_close_phase6_rendering.py`:

```text
# against git show HEAD:docker/versioning/rendering.py (pre-Phase-6)
Ran 9 tests … FAILED (see the combined RED below)

# after migration
Ran 9 tests …
OK
```

Migrated release sites: `_validate_effective_destination` adopts its no-follow
destination descriptor; `write_effective_build` adopts the generated-directory
descriptor as `generated_owner` and `detach()`es it once
`DirectoryCapability.from_fd` succeeds, releasing through the local owner on a
failed adoption and through the capability after adoption; and
`write_effective_inventory` adopts the `mkstemp()` fd immediately and releases
it through `owner.close` after the file object closes (via
`os.fdopen(owner.fd, ..., closefd=False)`) and before `os.replace`.  The
follow-up fault tests additionally inject a file-object-close failure (with a
simultaneous descriptor-release failure) and require the file-object failure to
stay primary while the descriptor failure stays observable as secondary, with
no publication.  The
pre-existing `tests/test_transactions_phase6_effective_projection.py`
precedence, at-most-once, adoption-split, and typed-close assertions all still
pass unchanged.

Regression batch:

```text
$ .venv/bin/python -m unittest \
    tests.test_descriptor_close_phase6_rendering \
    tests.test_transactions_phase6_effective_projection \
    tests.test_transactions_phase5a_typed_preservation \
    tests.test_transactions_l1_error_boundary \
    tests.test_constructor_build_persistence \
    tests.test_transactions_phase9_boundaries \
    tests.test_descriptor_close_convergence \
    tests.test_constructor_build_orchestration
Ran 395 tests in 8.572s
OK

$ .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
Ran 5955 tests in 100.177s
OK (skipped=13)
```

The 7 new tests raise the full-suite baseline from 5948 to 5955.
`ty check docker --python-version 3.14 --output-format concise` still reports
only the two pre-existing `@contextmanager` deprecation warnings.

## Descriptor-release ordering and 6.16–6.18 fault coverage

Follow-up hardening for tasks 6.16–6.18.  Three production files changed and
their phase-6 suites grew from 14 to 23 tests (materialization 4 → 9, rendering
7 → 9, persistent cache 3 → 5):

```text
# RED — all three suites against git show HEAD:<module>
.venv/bin/python -m unittest \
    tests.test_descriptor_close_phase6_build_materialization \
    tests.test_descriptor_close_phase6_rendering \
    tests.test_descriptor_close_phase6_cache
Ran 23 tests … FAILED (failures=16, errors=6)

# GREEN — after the migration
Ran 23 tests …
OK
```

**Materialization release ordering (6.16 / 6.34).**
`materialize_artifact` previously released `output_owner` only in the outer
`finally`, i.e. *after* `os.replace`.  A close failure therefore occurred after
publication and escaped as a raw `OSError`, leaving the artifact published
despite the failure.  The migration:

* adopts the `mkstemp()` fd immediately, *before* `Path(temporary_name)` and
  `hashlib.new()` (both now inside the protected region), so an initialization
  failure releases the descriptor once and removes the temporary entry;
* removes the temporary entry by the **raw** `mkstemp()` pathname via a new
  `_remove_owned_entry` helper (`os.unlink`, swallowing only
  `FileNotFoundError`), so cleanup no longer depends on `Path(temporary_name)`
  succeeding.  Previously the outer `finally` guarded the unlink with
  `if temporary is not None`, so a `Path`-construction failure left the
  temporary entry behind;
* flushes/closes the file object and releases `output_owner` **before**
  `os.chmod`/`os.replace`, inside the existing `MaterializationError` mapping
  boundary, so a release failure maps to `MaterializationError` and publication
  is never attempted;
* uses explicit (not implicit `with`) file-object cleanup, so a write/flush
  failure stays primary while a file-object-close and/or descriptor-release
  failure is retained as ordinary secondary diagnostics.

**File-object fault injections (6.16–6.18).**  A new
`tests.descriptor_ownership_support.file_object_faults` context manager wraps
the real `os.fdopen` result so a test can inject `write`, `flush`, and `close`
failures independently of the descriptor release.  Each suite now injects an
operation/file-object failure together with a descriptor-release failure and
asserts: the operation/file-object failure stays primary, the descriptor
failure stays observable via `_transaction_secondary`, and
`publish`/`os.replace` was never called (destination absent, temporary entry
removed, terminal owner attempted exactly once).  Materialization also covers
an initialization failure (`hashlib.new`), a `Path`-construction failure (one
release attempt, temporary entry removed, no publication), and a transport
`MaterializationError` that stays primary over a release failure.

## Supporting test adjustments (required by the migration)

- `tests/test_transactions_phase9a_audit.py` — its `DiscardTests._state()`
  helper constructed `_Publication(fd=...)`; updated to construct the new
  `_Publication(owner=...)` shape (the terminal/at-most-once behavior it pins
  is unchanged).
- `tests/test_transactions_l1_error_boundary.py` — extended `RAW_CLOSE_ACTIONS`
  with the new `owner.close` / `outputs_owner.close` sites in
  `publication.py`, `capabilities.py`, `locking.py`, and `regular.py`, matching
  the existing Phase 5 precedent for the raw-`OSError` foundation owner.
- `tests/test_transactions_phase8_npm_specialization.py` — the source-detail
  assertion for the npm-owned durability fsync was updated from
  `os.fsync(outputs_fd)` to `os.fsync(outputs_owner.fd)`.
- `tests/test_transactions_l1_error_boundary.py` — the shared-owner inventory
  now recognizes any `owner` / `<role>_owner` receiver as a lightweight
  `OwnedDescriptor` release (whose contract is a raw `OSError`), mirroring the
  existing `owner`-in-`RAW_RECEIVERS` and explicit `RAW_CLOSE_ACTIONS`
  exemptions.  No rule was weakened for transaction-typed closes.
- `tests/test_constructor_materialization.py` —
  `test_terminal_owner_ignores_repeated_close` now selects the freshly created
  child owner (the last one) because `create_temp` also owns the parent path.
- `tests/test_descriptor_close_convergence.py` —
  `test_create_temp_migrates_child_and_parent` (renamed) now requires zero raw
  closes in `create_temp`, matching the Phase 6 parent migration.
- `tests/test_constructor_build_cache_directory_handoff.py` — the Phase 5
  `_OwnerLedger` now ignores transferred owners (a verified handoff), because
  `_open_namespace_fd` adopts its descriptor and transfers it to the caller;
  `owner_for` prefers the non-transferred owner.  The three
  `BootstrapLockParentTests` assertions count `retained_owners` accordingly.
- `tests/test_transactions_phase9a_project_state_cleanup.py` — retained the
  existing raw-close recorder characterization and added
  `SharedOwnerDelegationTests` (3 tests) requiring every `validate_project_state`
  release to pass through a shared owner; only those delegation assertions were
  RED pre-migration (the terminal, retry, independent-cleanup, and precedence
  guarantees were already green).
- `tests/test_transactions_phase5a_typed_preservation.py` — the Phase 5A
  ownership-boundary inventory pinned the pre-Phase-6 raw close text for two
  modules: `build_cleanup._open_algorithm_directory`
  (`accumulator.run(lambda: ops.close(fd), ...)`) and rendering's
  `write_effective_build` / `_validate_effective_destination`
  (`failures.run(lambda: ops.close(generated_fd), ...)` and
  `elif generated_fd is not None:`).  Both now require the migrated
  `owner.close` / `owner.detach()` / `elif generated_owner is not None:` form.
- `tests/test_host_download_observability.py` — the `_GatedWriter` test double
  (which patches `os.fdopen` to gate the disk write) gained a `close` method,
  because the follow-up replaces the implicit `with os.fdopen(...)` cleanup
  with an explicit file-object close.
- `tests/test_transactions_l1_error_boundary.py` — the shared-owner inventory
  now also permits an explicit file-object close (receivers `fh` / `output`),
  which flushes the Python wrapper opened with `closefd=False` without touching
  the descriptor; the descriptor release still goes through the shared owner.
- `tests/test_descriptor_close_convergence.py` — the generated ownership ledger
  gained `_file_object_names`, so a `.close` on a file object opened from a
  proven owner fd (`os.fdopen(owner.fd, ..., closefd=False)`) is a resolved
  cleanup receiver rather than an unresolved consumer close.
- `tests/test_transactions_phase9a_specialized.py` and
  `tests/test_transactions_phase9a_ambient_primary.py` — the temporary-cleanup
  fault injections patched `Path.unlink`; because the owned entry is now
  removed by the raw `mkstemp()` pathname via `os.unlink`, the injections now
  patch `os.unlink` (raising only for `.materialize-` names so unrelated cache
  unlinks keep working).  The behavior they pin — a temporary unlink failure
  stays secondary to a digest/verification failure, and an ambient temporary
  unlink failure raises to the caller — is unchanged.

No inventory rule was weakened: the added `RAW_CLOSE_ACTIONS` entries are
exactly the foundation `OwnedDescriptor.close` releases whose contract is a raw
`OSError`, identical to the existing `build_cache` / `build_snapshot` entries.

## Build-materialization module (tasks 6.16 / 6.34)

RED and GREEN over the 9-test
`tests/test_descriptor_close_phase6_build_materialization.py`:

```text
# against git show HEAD:docker/versioning/build_materialization.py
Ran 9 tests … FAILED (see the combined RED below)

# after migration
Ran 9 tests …
OK
```

`materialize_artifact` adopts the `tempfile.mkstemp()` fd into `output_owner`
immediately (before `transport.stream`), opens the file object with
`os.fdopen(output_owner.fd, "wb", closefd=False)`, flushes/closes the file
object, and releases the owner through shared precedence **before**
`os.replace` (the release is inside the `MaterializationError` mapping boundary,
so a close failure can never follow a successful publish).  A write/stream
failure stays primary; a file-object-close and/or release failure is secondary.
Full suite rises 5955 → 5985 after the whole follow-up.

## Persistent-cache module (tasks 6.18 / 6.36)

RED and GREEN over the 5-test
`tests/test_descriptor_close_phase6_cache.py`:

```text
# against git show HEAD:docker/versioning/cache.py
Ran 5 tests … FAILED (see the combined RED below)

# after migration
Ran 5 tests …
OK
```

`DiskCache.set` adopts the `open_private_entry()` fd into `owner` immediately,
opens the file object with `os.fdopen(owner.fd, "w", encoding="utf-8",
closefd=False)`, and releases the owner through shared precedence before
`publish_private_entry`.  A JSON-write failure stays primary and prevents
publication; a file-object-close failure stays primary over a descriptor
failure, which stays observable as secondary; a release failure without a
primary also prevents publication.  Full suite rises 5955 → 5985 after the
whole follow-up.

## Failure-path hardening — `atomic_publish` and `_open_private_dir_at`

Two post-migration failure paths still leaked a descriptor and bypassed the
shared owner:

- `LocalCacheFilesystem.atomic_publish` opened the target parent *before* the
  source was adopted and before the protected cleanup region, so a failing
  target-parent open stranded the already-opened source fd.  The source is now
  adopted immediately, the target owner starts as `None`, and the target
  parent is opened and adopted inside the protected region.  Cleanup always
  releases the source owner and releases the target owner only when its
  adoption succeeded.  Existing validation, replacement ordering, and
  exception mappings are unchanged.
- `_open_private_dir_at` protected only `OSError` from `os.fstat` and the
  explicit unsafe-directory rejection; a `KeyboardInterrupt` or unexpected
  defect escaped without releasing the adopted descriptor.  A single protected
  lifecycle now covers `fstat` and every subsequent validation, releasing the
  owner through `CleanupFailures` on any failure while preserving the
  `OSError` → `ProjectStateError` mapping and the explicit unsafe-directory
  diagnostic.  The fd is detached and returned only after successful
  validation.

Six regression tests were added (2 artifact-cache, 4 project-state):

- `test_atomic_publish_target_parent_failure_releases_source_once` — the source
  owner is the only adopted publish owner and receives exactly one
  terminal-before-close release; no target owner is recorded, no publication
  occurs, and the temporary entry survives.
- `test_atomic_publish_target_parent_failure_keeps_close_failure_secondary` —
  with an injected source-close failure, the target-open `OSError` stays
  primary and the close failure is retained via `_transaction_secondary`.
- `test_stat_interruption_releases_through_shared_owner` /
  `test_stat_unexpected_defect_releases_through_shared_owner` — a
  `KeyboardInterrupt` / `RuntimeError` from `os.fstat` stays authoritative
  (identity preserved) and the descriptor receives one terminal close attempt.
- `test_stat_interruption_keeps_primary_over_close_failure` /
  `test_stat_unexpected_defect_keeps_primary_over_close_failure` — the
  injected primary stays authoritative while the ordinary close failure is
  retained as a secondary diagnostic.

The successful-return ownership test
(`test_success_transfers_live_descriptor_to_the_caller`) is retained unchanged.

RED — both focused suites against the pre-fix staged production:

```text
$ .venv/bin/python -m pytest \
    tests/test_descriptor_close_phase6_artifact_cache.py \
    tests/test_descriptor_close_phase6_project_state.py --tb=line -q
6 failed, 34 passed
# artifact-cache: the target-parent failure leaked/omitted the source owner;
# project-state: the interruption/unexpected-defect fstat leaked the descriptor.
```

GREEN — after the production edits:

```text
$ .venv/bin/python -m pytest \
    tests/test_descriptor_close_phase6_artifact_cache.py \
    tests/test_descriptor_close_phase6_project_state.py --tb=short -q
40 passed in 0.24s

$ .venv/bin/python -m pytest \
    tests/test_descriptor_close_phase6_artifact_cache.py \
    tests/test_descriptor_close_phase6_project_state.py \
    tests/test_transactions_phase9a_project_state_cleanup.py \
    tests/test_descriptor_close_convergence.py \
    tests/test_transactions_phase5a_typed_preservation.py \
    tests/test_transactions_l1_error_boundary.py \
    tests/test_constructor_project_state_phase1.py --tb=short -q
253 passed, 364 subtests passed

$ ty check docker --python-version 3.14 --output-format concise
All checks passed!

$ .venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q
6090 passed, 13 skipped, 10520 subtests passed in 29.67s

$ git diff --check
(clean)
```

The focused suite grows from 34 to 40 tests; the full suite passes at 6090
with 13 skipped and 10520 subtests passing.

## Stream-wrapper and ensured-directory hardening (follow-up)

Three further reproduced paths masked a failure or leaked a descriptor:

- `build_cache.publish_verified_blob` wrapped the temporary blob in
  `with os.fdopen(..., closefd=False)`.  A failing `stream.close()` in the
  context-manager `__exit__` replaced an in-flight `write`/`flush`/`fsync`
  failure.  The file object is now created explicitly and closed through
  `CleanupFailures` *before* verification and publication, so the operation
  failure keeps precedence and the wrapper close is retained secondarily.  The
  descriptor-owner cleanup and temporary-entry cleanup are unchanged and the
  wrapper close is never retried.
- `LocalCacheFilesystem.read_bytes` and `inspect_and_digest` had the same
  `with os.fdopen(...)` masking path.  Both now close the file object through
  the same accumulator that releases the descriptor owner, so neither the
  wrapper close nor the descriptor close can replace a read/hash failure.
  Return values, domain mappings, and release ordering are preserved.
- `project_state._ensure_private_dir_at` called `os.fchmod` on the raw fd
  returned by `_open_private_dir_at`; a chmod failure leaked the newly created
  child descriptor.  The fd is now adopted into an `OwnedDescriptor`
  immediately, chmod runs through the live owner inside protected cleanup, and
  the owner is released through `CleanupFailures` on any failure while the
  descriptor is detached and returned only on success.  Pre-existing entries
  still take the early return and are never chmodded.

Sixteen regression tests were added:

- `PublicationStreamCleanupTests` (4): simultaneous write (or flush) +
  wrapper-close + descriptor-close keeps the write/flush fault primary with
  both cleanup failures retained secondarily and exactly one terminal
  temporary-owner release; a sole wrapper-close failure is primary with no
  publication; a descriptor-close interruption stays primary over the wrapper
  close.
- `StreamWrapperCleanupTests` (6): the same primary/secondary, terminal, and
  exactly-once checks for `read_bytes` and `inspect_and_digest`, using an
  injected read fault, a file-object close fault, and a descriptor close fault,
  plus a sole wrapper-close failure and a cleanup interruption for each.
- `EnsurePrivateDirectoryOwnershipTests` (6): ordinary and interrupting chmod
  failures release the created child exactly once and keep their identity over
  a simultaneous descriptor-close failure; successful creation and existing
  entries return live, detached descriptors.

The shared `FileObjectFaults`/`file_object_faults` helper gained a `read_fault`
so a read/hash operation failure can be injected independently of the wrapper
close.

RED — the new suites against the pre-fix staged production:

```text
$ .venv/bin/python -m pytest \
    tests/test_descriptor_close_phase6_build_cache.py::PublicationStreamCleanupTests \
    tests/test_descriptor_close_phase6_artifact_cache.py::StreamWrapperCleanupTests \
    tests/test_descriptor_close_phase6_project_state.py::EnsurePrivateDirectoryOwnershipTests \
    --tb=line -q
15 failed, 1 passed
# publication/read/wrapper masking and the created-child chmod leak;
# the retained existing-entry success test passes.
```

GREEN — after the production edits:

```text
$ .venv/bin/python -m pytest \
    tests/test_descriptor_close_phase6_build_cache.py \
    tests/test_descriptor_close_phase6_artifact_cache.py \
    tests/test_descriptor_close_phase6_project_state.py --tb=short -q
73 passed in 0.56s

$ .venv/bin/python -m pytest \
    tests/test_descriptor_close_phase6_build_cache.py \
    tests/test_descriptor_close_phase6_artifact_cache.py \
    tests/test_descriptor_close_phase6_project_state.py \
    tests/test_descriptor_close_phase6_build_materialization.py \
    tests/test_descriptor_close_phase6_rendering.py \
    tests/test_descriptor_close_phase6_cache.py \
    tests/test_descriptor_close_phase6_cache_storage.py \
    tests/test_transactions_phase9a_project_state_cleanup.py \
    tests/test_transactions_phase9a_build_cache_cleanup.py \
    tests/test_constructor_build_cache_directory_handoff.py \
    tests/test_constructor_build_cache_permissions.py \
    tests/test_constructor_build_cache_paths.py \
    tests/test_constructor_build_cache_state_lifecycle.py \
    tests/test_descriptor_close_convergence.py --tb=short -q
231 passed, 1 skipped, 339 subtests passed

$ .venv/bin/python -m pytest tests/test_transactions_l1_error_boundary.py -q
66 passed, 28 subtests passed

$ ty check docker --python-version 3.14 --output-format concise
All checks passed!

$ .venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q
6106 passed, 13 skipped, 10520 subtests passed in 29.77s

$ git diff --check
(clean)
```

The `test_transactions_l1_error_boundary.py` close-inventory exempt set gained
the `stream` receiver alongside `fh`/`output`, since the explicit
`os.fdopen(..., closefd=False)` wrapper close keeps the raw `OSError` cleanup
contract and never releases the descriptor itself.

### Publication-name generation before namespace acquisition

`build_cache.publish_verified_blob` generated the temporary name with
`os.urandom` **after** acquiring the publication namespace descriptor but
**before** the protected `try`, so a failure or interruption while generating
the name leaked the namespace fd.  Name generation now runs before
`_open_namespace_fd`, so a name-generation failure acquires no descriptor that
needs cleanup; the naming format and existing publication behavior are
unchanged.

`PublicationInitializationFailureTests` (3) reproduces the leak:

- `os.urandom` raising `OSError` propagates as the exact injected exception,
  opens no `publication namespace` owner, leaves the cache untouched, and every
  descriptor `prepare_build_cache` opened is released terminally and at most
  once.
- `os.urandom` raising `KeyboardInterrupt` behaves the same with the
  interruption as the authoritative exception.
- a later initialization failure (`_require_algorithm_dir`) keeps precedence
  while the already-acquired publication namespace descriptor receives exactly
  one terminal release attempt, whose ordinary close failure is retained as a
  secondary diagnostic.

RED — the two `urandom` tests against the pre-fix ordering:

```text
$ .venv/bin/python -m pytest \
    tests/test_descriptor_close_phase6_build_cache.py::PublicationInitializationFailureTests \
    --tb=line -q
2 failed, 1 passed
# each failure: AssertionError: True is not false
#   (a "publication namespace" owner was recorded before name generation
#    failed); the terminal/secondary-release characterization stays green.
```

GREEN:

```text
$ .venv/bin/python -m pytest tests/test_descriptor_close_phase6_build_cache.py --tb=short -q
24 passed in 0.44s

$ .venv/bin/python -m pytest \
    tests/test_descriptor_close_phase6_build_cache.py \
    tests/test_descriptor_close_phase6_artifact_cache.py \
    tests/test_descriptor_close_phase6_project_state.py \
    tests/test_descriptor_close_phase6_build_materialization.py \
    tests/test_descriptor_close_phase6_rendering.py \
    tests/test_descriptor_close_phase6_cache.py \
    tests/test_descriptor_close_phase6_cache_storage.py \
    tests/test_transactions_phase9a_project_state_cleanup.py \
    tests/test_transactions_phase9a_build_cache_cleanup.py \
    tests/test_constructor_build_cache_directory_handoff.py \
    tests/test_constructor_build_cache_permissions.py \
    tests/test_constructor_build_cache_paths.py \
    tests/test_constructor_build_cache_state_lifecycle.py \
    tests/test_descriptor_close_convergence.py --tb=short -q
234 passed, 1 skipped, 339 subtests passed

$ .venv/bin/python -m pytest tests/test_transactions_l1_error_boundary.py -q
66 passed, 28 subtests passed

$ ty check docker --python-version 3.14 --output-format concise
All checks passed!

$ .venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q
6109 passed, 13 skipped, 10520 subtests passed in 29.74s

$ git diff --check
(clean)
```

## Generated ownership ledger (task 6.37)

`tests/test_descriptor_close_convergence.py` gains a generated per-function
ownership ledger over **64** migrated functions/methods covering tasks 6.1–6.18.
The ledger resolves descriptor origins symbolically:

* explicit `OwnedDescriptor` construction and equivalent
  `DirectoryCapability`/`FileCapability`/`DirectoryDescriptor` factories and
  `.open_regular`/`.from_fd`/`.from_path`/`.open_secure_path` handoffs;
* parallel-owner assignment (`previous, current = current, child`);
* injected parameters/fields annotated as a shared-owner type;
* aliased `os.fdopen` (omitted `closefd` treated as `True` and rejected;
  `closefd=False` accepted only with a proven `owner.fd`);
* aliased `tempfile.mkstemp` (each fd must be adopted **unconditionally**
  and before any other fallible operation; delayed, conditional, or
  adopted-elsewhere-in-the-function forms are rejected);
* an injected backend close is permitted only inside an explicitly
  **verified shared owner** that marks itself terminal before the close and
  guards the close behind that terminal field (repeat-safe release);
  mimicking the terminal flag alone is rejected.

The ledger fails closed on unresolved close-like calls.  Five new ledger tests
plus seven evasion/acceptance fixtures bring the convergence file to 51 tests;
the task-6.37 follow-up below raises it to 57.

### Ledger hardening (task 6.37 follow-up)

Two gaps in the generated ledger were closed after the initial green run:

* `_mkstemp_violations` previously accepted any adoption because an owner
  constructor appeared *somewhere* in the function, so it accepted delayed
  adoption (fd used before the transfer) and conditional adoption (transfer
  inside an `if`).  The checker now builds a parent/DFS-order view of the
  function and requires the transfer to be an unconditional statement that
  precedes every other call and every use of the returned fd.
  `OwnershipLedgerEvasionTests` adds three rejecting fixtures — fd use before
  adoption, a fallible operation before adoption, and adoption behind an
  `if` — while the immediate, unconditional adoption fixture stays green.
* the backend-close exemption was removed.  `self._closed = True` immediately
  before `self._ops.close(...)` no longer grants an exemption.  A backend
  close is permitted only for an implementation listed in
  `_VERIFIED_SHARED_OWNER_RELEASES` (currently
  `docker/transactions/capabilities.py::FileCapability.close`) **and** only
  when `_shared_owner_release_proof` proves the terminal transition precedes
  the close and the close is guarded by `if not self.<terminal-field>:`, so a
  repeated release is suppressed.  Fixtures reject consumer code disguised as
  a shared owner and a verified-but-unguarded release; a positive fixture
  proves the full terminal/guard shape is accepted for a verified owner, and
  `test_verified_shared_owner_releases_are_ledger_entries` pins each registry
  entry to a real scanned implementation with clean ledger facts.

RED — the new rejecting fixtures against the original permissive helpers:

```text
$ # _mkstemp_violations reverted to "owner call anywhere in the function"
testsRun=4 failures=3 errors=0
# test_mkstemp_fd_use_before_adoption_is_rejected            FAIL
# test_mkstemp_fallible_operation_before_adoption_is_rejected FAIL
# test_mkstemp_conditional_adoption_is_rejected             FAIL
# test_mkstemp_adoption_is_accepted                         pass

$ # backend exemption reverted to "any terminal flag before close"
testsRun=3 failures=1 errors=0
# test_shared_owner_release_without_repeat_guard_is_rejected FAIL
# test_verified_shared_owner_release_is_accepted            pass
# test_consumer_backend_close_disguised_as_shared_owner_is_rejected pass
```

GREEN:

```text
$ .venv/bin/python -m pytest tests/test_descriptor_close_convergence.py --tb=short -q
57 passed, 325 subtests passed

$ .venv/bin/python -m pytest \
    tests/test_descriptor_close_convergence.py \
    tests/test_configuration_document_validation_phase1.py \
    tests/test_transactions_l1_error_boundary.py --tb=short -q
146 passed, 369 subtests passed

$ .venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q
6115 passed, 13 skipped, 10521 subtests passed in 30.01s
```

### Flaky payload-leak assertion (kept separate from descriptor migration)

The prior full-suite run failed at
`tests/test_configuration_document_validation_phase1.py:197`
(`test_schema_error_has_only_document_identity_classification_and_field`):

```text
$ .venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q
FAILED tests/test_configuration_document_validation_phase1.py::...::test_schema_error_has_only_document_identity_classification_and_field
# full log: /tmp/docker-constructor-pytest.V2DTUH.log
```

The test asserted `assertNotIn("42", str(error))` while the projected error
embeds the document path, and a random temporary-directory name can
legitimately contain `"42"`.  Deterministic reproduction with
`TMPDIR=/tmp/dc-42-tmp`:

```text
OLD assertion (payload blanket): "42" not in rendered               -> False
NEW assertion (identity removed): "42" not in rendered.replace(path)  -> True
```

The correction asserts the permitted document identity (`error.role`,
`error.path`, and the path rendered in the message) separately from the
rejected payload, after subtracting the identity path.  Classification,
field, coordinates, and exception-chain checks are unchanged.  This fix is
confined to the configuration-document test file and does not touch
descriptor-migration code.

## Final validation (task 6.38)

```text
$ .venv/bin/python -m unittest <13 phase-6 files + npm-publication, transactions,
      artifact-cache, build-cache, project-state, snapshot, materialization,
      build-cleanup, cache-storage, rendering, persistent-cache, locking,
      durability, publication suites>
Ran 1247 tests in 12.358s
OK (skipped=3)

$ ty check docker --python-version 3.14 --output-format concise
All checks passed!

$ .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
Ran 5985 tests in 110.043s
OK (skipped=13)

$ git diff --check
(clean)
```

Follow-up: the two long-standing `@contextmanager` deprecation warnings were
resolved by annotating `identity_coordination_lock`
(`docker/npm_environment/publication.py`) and `HostActivityMonitor.step`
(`docker/versioning/assembly_activity.py`) as `-> Generator[None]` instead of
`-> Iterator[None]`.  `ty check` now reports `All checks passed!` with zero
diagnostics.

Full-suite baseline progression across Phase 6: 5875 → 5897 → 5914 → 5929 →
5936 → 5941 → 5948 → 5955 → 5959 → 5962 → **5976**.  Every Phase 6 task is
complete; the change is now waiting on Phase 7 (repository convergence and
release gate).

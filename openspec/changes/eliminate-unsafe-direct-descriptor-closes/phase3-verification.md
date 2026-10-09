# Phase 3 Verification — Versioning Single-Descriptor Lifecycle

Change: `eliminate-unsafe-direct-descriptor-closes`
Phase: 3 (tasks 3.1–3.8)
Status: complete

This file is implementation evidence only. It does not modify any OpenSpec
artifact other than the Phase 3 checkboxes in `tasks.md`.

## Deliverables

- `docker/versioning/build_snapshot.py::_copy_source_fd_to_destination()`
  transfers the `O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW` destination
  descriptor into `OwnedDescriptor(PosixDescriptorOps(), fd, label=...)`
  immediately after the open.  The active copy/digest failure stays primary
  over an ordinary close failure, the close and the destination unlink run as
  independent actions in one `CleanupFailures` accumulator, and the success
  path releases through terminal `owner.close()`.  Digest, mode (`0444`),
  `fsync`, and unlink semantics are unchanged.
- `docker/versioning/artifact_cache.py::LocalCacheFilesystem.create_temp()`
  transfers the freshly created blob descriptor into a shared
  `OwnedDescriptor` immediately and releases it through `owner.close()`; the
  existing parent-directory accumulator and `ArtifactMaterializationError`
  policy are retained.  Path, `0600` mode, and `O_EXCL` creation behavior are
  unchanged.
- `docker/versioning/project_state.py::ValidatedProjectState` stores one
  private `OwnedDescriptor` for its namespace fd.  The public constructor,
  `state`/`namespace_fd` attributes, context-manager behavior, and
  `ProjectStateError`-preserving `__exit__` mapping are unchanged; the
  bespoke `_closed` release state was removed in favour of the shared owner.
  Ancestor and metadata descriptors inside `validate_project_state()` are
  intentionally untouched (residual Phase 6 scope).
- New tests:
  - `tests/test_constructor_build_snapshot.py::TestSnapshotDestinationLifecycle`
    (tasks 3.1 and 3.4)
  - `tests/test_constructor_materialization.py::TestTemporaryCreationLifecycle`
    (tasks 3.2 and 3.5)
  - `tests/test_transactions_phase6_project_metadata.py::ValidatedProjectStateOwnershipTests`
    (tasks 3.3 and 3.6)
  - `tests/test_descriptor_close_convergence.py::VersioningConvergenceTests`
    plus helpers `_class_owner_names`, `_close_violations_with_owners`, and
    `_raw_close_targets` (task 3.7)

`docker/filesystem/` was **not** modified: only ownership and close precedence
are reused; snapshot, cache-temporary, and project-state path/domain policy
stayed in the versioning modules.

The repository-wide L1 close inventory in
`tests/test_transactions_l1_error_boundary.py` was extended to recognize the
lightweight `OwnedDescriptor` as a raw-`OSError` release authority: `"owner"`
joined `RAW_RECEIVERS` and `("docker/versioning/build_snapshot.py",
"owner.close")` joined `RAW_CLOSE_ACTIONS`.  This is inventory maintenance,
not a behavior change.

## Task-by-task evidence

| Task | Artifact | Evidence |
| --- | --- | --- |
| 3.1 | `TestSnapshotDestinationLifecycle` | RED (terminal-owner case: `build_snapshot` has no `OwnedDescriptor`), then GREEN; operation failure primary over close with destination unlinked, sole close failure raw, interruption identity preserved, one close attempt, terminal owner |
| 3.2 | `TestTemporaryCreationLifecycle` | RED (terminal-owner case: `artifact_cache` has no `OwnedDescriptor`), then GREEN; child close authoritative over independent parent cleanup, one attempt per fd, interruption identity preserved, terminal child owner |
| 3.3 | `ValidatedProjectStateOwnershipTests` | RED (terminal-owner case: `project_state` has no `OwnedDescriptor`), then GREEN; success/close/repeat, sole close raw, active primary plus close secondary, interruption identity preserved, one close attempt |
| 3.4 | `_copy_source_fd_to_destination()` refactor | GREEN; task 3.1 passes with unchanged digest, mode, fsync, and unlink semantics |
| 3.5 | `LocalCacheFilesystem.create_temp()` refactor | GREEN; task 3.2 passes with unchanged path, mode, and creation behavior; parent accumulator retained |
| 3.6 | `ValidatedProjectState` refactor | GREEN; task 3.3 passes with constructor, public attributes, context-manager behavior, and domain mapping preserved |
| 3.7 | `VersioningConvergenceTests` | GREEN; explicit foundation imports, no aggregate/reverse dependency, no raw/duplicated release in the migrated releases, exact `inspect.signature` matches for the public snapshot/artifact/project-state interfaces |
| 3.8 | focused + full suites | GREEN; 538 focused tests pass, full suite 5811 tests pass (13 skipped); `openspec validate` passes |

## RED evidence (new tests against pre-migration production)

The three new behavioral classes were run against the unmodified production
before the GREEN refactors.

```text
$ python -m unittest \
    tests.test_constructor_build_snapshot.TestSnapshotDestinationLifecycle \
    tests.test_constructor_materialization.TestTemporaryCreationLifecycle \
    tests.test_transactions_phase6_project_metadata.ValidatedProjectStateOwnershipTests
...E...E....E
ERROR: test_terminal_owner_ignores_repeated_close (TestSnapshotDestinationLifecycle)
    AttributeError: module 'docker.versioning.build_snapshot' has no attribute 'OwnedDescriptor'
ERROR: test_terminal_owner_ignores_repeated_close (TestTemporaryCreationLifecycle)
    AttributeError: module 'docker.versioning.artifact_cache' has no attribute 'OwnedDescriptor'
ERROR: test_terminal_owner_ignores_repeated_close (ValidatedProjectStateOwnershipTests)
    AttributeError: module 'docker.versioning.project_state' has no attribute 'OwnedDescriptor'
Ran 13 tests in 0.013s
FAILED (errors=3)
```

The ten characterization cases (operation-primary precedence, sole close
failure, interruption identity, and one-attempt-per-fd) already passed before
migration; the three terminal-ownership cases are the RED contract that only
the shared owner satisfies.

## GREEN evidence

New Phase 3 tests (behavioral + introspection):

```text
$ python -m unittest \
    tests.test_constructor_build_snapshot.TestSnapshotDestinationLifecycle \
    tests.test_constructor_materialization.TestTemporaryCreationLifecycle \
    tests.test_transactions_phase6_project_metadata.ValidatedProjectStateOwnershipTests \
    tests.test_descriptor_close_convergence.VersioningConvergenceTests
Ran 21 tests in 0.083s
OK
```

Focused suites (task 3.8): snapshot, pi-snapshot, artifact-cache,
cache-contracts, build-materialization, project-state metadata and cleanup,
effective projection, project-state phase 1, effective-state, build-projection,
build-cleanup, build-generation integration, build-generations, descriptor
close convergence:

```text
Ran 538 tests in 3.977s
OK (skipped=2)
```

Full test suite (`python -m unittest discover -s tests -p 'test_*.py'`):

```text
Ran 5811 tests in 99.885s
OK (skipped=13)
```

OpenSpec validation:

```text
$ openspec validate "eliminate-unsafe-direct-descriptor-closes"
Change 'eliminate-unsafe-direct-descriptor-closes' is valid
```

## Notes

- Snapshot `_copy_source_fd_to_destination` intentionally keeps the close and
  the destination `unlink` in one `CleanupFailures` accumulator so a
  process-control close interruption still precedes the unlink, matching the
  previous failure ordering.
- `create_temp` deliberately retains `os.close(parent_fd)` for the directory
  accumulator; only the child blob descriptor is part of this phase.  The
  introspection test pins that the only raw close target in the method is
  `parent_fd`.
- `ValidatedProjectState.close()` is now simply `self._owner.close()`; the
  removed `_closed` flag is supplied by the owner's terminal state.
- The L1 close inventory extension is the minimal recognition of the new
  lightweight foundation owner; no other inventory rule or production module
  changed.

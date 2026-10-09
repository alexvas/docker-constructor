# Phase 6 Verification Record — npm Storage Migration

Date: 2026-10-07

Change: `extract-generic-descriptor-capabilities`

Scope: Phase 6 only. Phase 7 and later remain unstarted.

## Deliverable

`docker/npm_environment/storage.py` uses the lightweight
`docker.filesystem` descriptor foundation for namespace and staging directory
ownership. Returned paths, `0700` modes, namespace sequencing, recursion,
absence policy, and `LockedNpmError` reasons/diagnostics are unchanged. No raw
owned-directory close lifecycle remains in the npm storage module.

## Production files changed

```text
docker/npm_environment/storage.py  # namespace/staging directory ownership now uses
                                   #   DirectoryDescriptor.open_secure_path(),
                                   #   open_or_create_directory(), create_directory(),
                                   #   open_directory(), stat_child(), list_names(),
                                   #   unlink_child(), remove_child_directory();
                                   #   nested capabilities released at most once with
                                   #   ordinary close failures secondary; foundation
                                   #   DescriptorError/UnsafeDescriptorError mapped back
                                   #   to LockedNpmError("unsafe_cache_path" /
                                   #   "unsafe_staging_path"); recursion and absence
                                   #   policy stay in storage.py
docker/filesystem/descriptors.py   # added the generic create/reopen operation stages
                                   #   (_STAGE_CREATE, _STAGE_REOPEN) so a domain
                                   #   consumer can tell an exclusive mkdir failure from
                                   #   the open that follows it without inspecting the
                                   #   raw OSError; DescriptorError's
                                   #   (stage, message, *, cause=None) contract is
                                   #   unchanged
```

No npm path, assembler digest, namespace child, recursion, or domain error
reason entered `docker.filesystem`. No public CLI, cache layout, npm identity,
publication format, or lock policy changed.

## Test files changed

```text
tests/test_npm_environment_storage.py            # new, 49 tests (Phase 6 6.1–6.4,
                                                 #   6.6/6.8 create-vs-reopen diagnostics,
                                                 #   6.10–6.11)
tests/test_npm_environment_fs_introspection.py   # Phase 2 introspection retargeted to
                                                 #   the foundation owner of the mechanics
tests/test_transactions_phase5a_typed_preservation.py
                                                 # task 5A.1 inventory classifies the new
                                                 #   npm storage cause-inspection sites
```

## Task evidence

### RED

- **6.1** `TestSecureWalkHandoff.test_parent_close_failure_is_not_retried_and_child_is_released`:
  a faulted first close during the secure walk raises the raw `OSError`
  unchanged, the failing descriptor is closed exactly once (no retry), the
  next descriptor is released once, and the ledger reports no double close or
  leaked descriptor. Against the pre-migration raw handoff the test errors
  because the walk has no injectable `ops` seam.
- **6.2** `TestStagingPreparationPrecedence`: an injected `fchmod`, `mkdirat`,
  or child-open failure stays the authoritative `unsafe_staging_path` failure;
  a simultaneous release failure is secondary (retained on the foundation
  error's `_transaction_secondary` or, for the domain error, bounded notes),
  and no descriptor is closed twice. `test_existing_workspace_is_rejected_without_reuse`
  pins the exclusive-create "already exists" diagnostic.
- **6.3** `TestRecursiveRemoval`: an injected `unlinkat` or `listdir` failure
  stays primary over directory release, every opened directory is released
  once, a full tree removal closes each directory once and removes the tree,
  and a missing workspace is a no-op.
- **6.4** `TestPreMigrationCharacterization`: namespace children are created
  in the fixed order (`npm-environments`, `assembler`, `<digest>`, `npm-cache`,
  `locks`, `staging`, `outputs`, `index`); no-follow and foreign-ownership
  rejections pin `LockedNpmError.reason` **and the exact `detail` text**
  (`unsafe cache component '<component>' in '<absolute>' (symlink or
  non-directory): <cause>`, `unsafe cache entry PosixPath('<path>') (symlink or
  non-directory): <cause>`, and `'<caller-supplied-path>' is not owned by the
  invoking user; restore ownership or remove the entry`); returned
  staging/namespace paths and `0700` modes are pinned. All eight cases pass
  against the pre-migration implementation as well as after (verified by
  swapping in `git show HEAD:docker/npm_environment/storage.py`); the
  layer-normalizing `_raw_cause` helper lets one exact assertion span both the
  raw-`OSError` and foundation-`DescriptorError` chains.

RED demonstration against the raw handoff implementation:

```text
python -m unittest \
  tests.test_npm_environment_storage.TestSecureWalkHandoff \
  tests.test_npm_environment_storage.TestStagingPreparationPrecedence \
  tests.test_npm_environment_storage.TestRecursiveRemoval
  -> Ran 9 tests in 0.012s
     FAILED (errors=7)   # every ops-seam behavior case; the two no-ops pass
```

### GREEN

- **6.5** `_open_directory_no_follow()` is now a domain wrapper over
  `DirectoryDescriptor.open_secure_path()`; foundation open/validation
  failures map to `LockedNpmError("unsafe_cache_path", ...)`, while a raw
  descriptor-handoff close failure keeps its `OSError` identity.
- **6.6** `_create_or_open_child()` delegates to
  `DirectoryDescriptor.open_or_create_directory(mode=0o700,
  require_owner=True)` and preserves `0700`, labels, and the
  `unsafe_cache_path` mapping. The mapping consumes the foundation's generic
  operation stage so an initial open rejection keeps the
  `(symlink or non-directory)` parenthetical while the open that follows a
  successful exclusive create omits it (see *Create-vs-reopen diagnostics*).
- **6.7** `prepare_assembler_namespace()` transfers ownership to nested
  capabilities (`with` blocks) with each child nested immediately inside its
  parent's block (`root → env → assembler → digest → children`), keeping the
  exact creation sequence and returned `AssemblerNamespace` paths. A failed
  child creation leaves the operation failure primary and closes the retained
  parent secondarily, and a failed parent close can no longer strand an
  already-created descendant (see *Handoff ownership hardening*).
- **6.8** `prepare_staging_workspace()` uses
  `DirectoryDescriptor.create_directory(mode=0o700)` for exclusive creation;
  `FileExistsError` maps to the unchanged "already exists" diagnostic, an
  exclusive-`mkdirat` failure keeps `cannot create staging workspace ...`, and
  the open that follows a successful creation restores
  `unsafe staging workspace ...`; a failed creation or reopen leaves the
  parent release secondary (see *Create-vs-reopen diagnostics*).
- **6.9** `_remove_entry()`/`remove_staging_workspace()` compose
  `stat_child()`, `open_directory()`, `list_names()`, `unlink_child()`, and
  `remove_child_directory()`; recursion and the `FileNotFoundError`-is-absent
  policy stay in `storage.py`, and a traversal/removal failure stays primary
  over the child-directory release.

### INTROSPECT

- **6.10** `TestStorageFoundationBoundary.test_storage_has_no_raw_owned_directory_close`
  parses `storage.py` and rejects any `os.close(...)` call (and the literal
  `os.close`); `test_storage_imports_only_foundation_and_shared_error` requires
  `docker.filesystem.descriptors` and `docker.filesystem.operations` and
  rejects `docker.transactions` or any other `docker.*` import.
- **6.11** `TestStorageFoundationBoundary.test_foundation_owns_no_npm_domain_tokens`
  scans every `docker/filesystem/*.py` for npm-domain tokens
  (`LockedNpmError`, `npm-environments`, `npm-cache`, `unsafe_cache_path`,
  `unsafe_staging_path`, `AssemblerNamespace`, the storage entry points,
  `remove_tree`/`rmtree`, `assembler`) and finds none; domain ownership stays
  in npm storage.
- The Phase 2 introspection module was retargeted: it now requires storage to
  delegate `open_secure_path`/`create_directory` and requires the foundation
  (`descriptors.py`, `operations.py`) to own `O_NOFOLLOW`, `fchmod`,
  `os.mkdir`, and `dir_fd`. The observable `0700`/no-follow/ownership outcomes
  are still pinned behaviorally in `test_npm_environment_storage.py`.
- The repository-wide Phase 5A cause-access inventory gained the new npm
  storage inspection sites (`_foundation_cause`, `_failing_component`,
  `_open_failure_detail`, `_child_failure_detail`, `_create_staging_directory`),
  all classified `inspect-only`; the AST guard still reports no prohibited
  escape.

## Diagnostic wording preservation

The migrated `_open_failure_detail()` reproduces the pre-migration
`_open_directory_no_follow()` diagnostic verbatim. `_failing_component()`
recovers the failing path component from the foundation's chained raw
`OSError.filename` (`PosixDescriptorOps.openat` opens one component relative to
its parent), so the detail still reads
`unsafe cache component '<component>' in '<absolute>' (symlink or
non-directory): <cause>`. The ownership/type branches use the caller-supplied
path (not the absolute path), so a relative cache root reports
`cache-root is not owned by the invoking user; ...` exactly as before. The
child-open/create mapping (`_child_failure_detail`) keeps the original
`PosixPath(...)` label rendering, `cannot create cache directory ...`, and
`unsafe cache entry ...` wording.

## Handoff ownership hardening

The original migration created a child descriptor inside its parent's `with`
block but entered the child's own `with` block only after the parent block
exited (``with root: env = ...`` then `with env: ...` then `with assembler:`
...).  A parent close failure therefore propagated with already-opened
descendants (`assembler`, `digest`) still live and never closed.

`prepare_assembler_namespace()` now nests each child capability immediately
inside its parent (`with root: env = ...; with env: assembler = ...; with
assembler: digest = ...; with digest: ...`), so Python unwinds the innermost
`with` first: `digest.close()`, `assembler.close()`, then `env.close()`.  A
close failure at any level therefore finds every descendant already released.
The creation order, namespace paths, `0700` modes, and `LockedNpmError`
mappings are unchanged.

Regression coverage (`TestNamespaceHandoffOwnership`, task 6.1) injects a
close failure keyed on the descriptor's resolved `/proc/self/fd` path (immune
to descriptor-number reuse during the secure walk):

- `test_env_close_failure_releases_assembler_and_digest` — closing `env`
  (after `assembler`/`digest` exist) raises the injected `OSError`; the
  assembler and digest descriptors are each closed exactly once; `ops.live`
  and `ops.double_closes` are empty.
- `test_assembler_close_failure_releases_digest` — closing `assembler` raises
  the injected `OSError`; the digest descriptor is closed exactly once;
  `ops.live` and `ops.double_closes` are empty.

Both tests were confirmed RED against the previous (non-nested) structure —
the assembler/digest descriptors were reported still live / never closed — and
GREEN after the nesting change.

## Create-vs-reopen diagnostics

`DirectoryDescriptor.create_directory()` performs an exclusive `mkdirat` and
then reopens the entry it created; `open_or_create_directory()` first opens an
existing entry and only falls back to `create_directory()`. These are distinct
operations that can surface the *same* raw `OSError` type, so the domain
mapping cannot infer operation provenance from the exception type alone.

Following the pre-migration behavior, the foundation now reports two generic
stages alongside the existing ones:

- `_STAGE_CREATE = "create-descriptor"` — the exclusive `mkdirat` failed.
- `_STAGE_REOPEN = "reopen-descriptor"` — the open that follows a successful
  exclusive create failed.

`DescriptorError`'s design-fixed `(stage, message, *, cause=None)` constructor
contract is unchanged; only two additional generic stage string values are
used. `storage.py` consumes them to keep the historical wording:

| Foundation stage | Namespace (`_child_failure_detail`) | Staging (`_create_staging_directory`) |
| --- | --- | --- |
| `_STAGE_CREATE`, non-`FileExistsError` | `cannot create cache directory <label>: <cause>` | `cannot create staging workspace <name>: <cause>` |
| `_STAGE_CREATE`, `FileExistsError` | (namespace reports the create failure) | `staging workspace <name> already exists; clean residue before reuse` |
| `_STAGE_REOPEN` | `unsafe cache entry <label>: <cause>` (no parenthetical) | `unsafe staging workspace <name>: <cause>` |
| initial/existing open (`open-descriptor`) | `unsafe cache entry <label> (symlink or non-directory): <cause>` | (not reached) |

Both mappings re-raise with the foundation `DescriptorError` chained as the
direct cause (`raise LockedNpmError(...) from exc`), so the original `OSError`
remains reachable through `DescriptorError.cause` and
`DescriptorError.__cause__`. When a parent release fails at the same time, the
`LockedNpmError` stays primary and the ordinary close failure is retained as
secondary cleanup diagnostic context (a bounded `__notes__` note for the
frozen `LockedNpmError`).

Regression coverage (`TestCreateVersusReopenDiagnostics`) pins each path:

- `test_namespace_child_create_failure_keeps_cannot_create_wording`
- `test_namespace_child_reopen_failure_keeps_unsafe_cache_entry_wording`
- `test_staging_create_failure_keeps_cannot_create_wording`
- `test_staging_reopen_failure_keeps_unsafe_staging_workspace_wording`

Each asserts the exact `LockedNpmError.reason`/`.detail`, the foundation stage,
the chained raw cause, the secondary close note, and no leaked or
double-closed descriptors. The two reopen tests were confirmed RED against the
previous mapping (which reported `cannot create ...` for a reopen failure) and
GREEN after the distinction.

### VALIDATE

```text
python -m unittest tests.test_npm_environment_storage \
  tests.test_transactions_phase8_npm_leaf_mechanics \
  tests.test_transactions_phase8_npm_lock \
  tests.test_npm_environment_phase9a_staging_cleanup
  -> Ran 103 tests in 0.761s
     OK

python -m unittest tests.test_npm_environment_storage
  -> Ran 49 tests
     OK

python -m unittest discover -s tests -p 'test_*.py'
  -> Ran 5599 tests in 94.828s
     OK (skipped=13)

ty check docker --python-version 3.14 --output-format concise
  -> All checks passed!

scripts/check-types
  -> All checks passed!

git diff --check
  -> clean

openspec validate extract-generic-descriptor-capabilities --strict
  -> Change 'extract-generic-descriptor-capabilities' is valid
```

## Summary

| Task | Status |
| --- | --- |
| 6.1–6.4 (RED / characterization) | complete |
| 6.5–6.9 (GREEN) | complete; 6.6/6.8 create-vs-reopen diagnostics restored, 6.7 handoff nesting hardened |
| 6.10–6.11 (INTROSPECT) | complete |
| 6.12 (VALIDATE) | complete |

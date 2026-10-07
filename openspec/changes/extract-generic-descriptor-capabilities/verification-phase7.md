# Phase 7 Verification Record — npm Tree Migration

Date: 2026-10-08

Change: `extract-generic-descriptor-capabilities`

Scope: Phase 7 only. Phase 8 and Phase 9 remain unstarted.

## Deliverable

`docker/npm_environment/tree.py` opens and releases every owned directory
descriptor through the lightweight `docker.filesystem` descriptor foundation.
`build_tree_manifest()` and `verify_tree()` return the same canonical manifest
bytes and content digest, keep the same entry ordering, containment rules,
no-follow behavior, and `LockedNpmError` reasons/diagnostics, and still own
recursive traversal and regular-file hashing. Every opened directory receives
at most one close attempt, and an ordinary close failure stays secondary to an
active traversal failure.

## Production files changed

```text
docker/npm_environment/tree.py  # _open_tree_root() now uses
                                #   DirectoryDescriptor.open_secure_path() with
                                #   require_owner=False (preserving the prior
                                #   no-owner-check tree root); _open_tree_root()
                                #   preserves the caller's components
                                #   (prefixing cwd for relative roots instead
                                #   of lexical normalization), so
                                #   ``missing/../tree`` etc. are rejected
                                #   component-by-component; _walk_entries()
                                #   visits children with list_names(),
                                #   stat_child(follow_symlinks=False), and
                                #   open_directory(), keeping recursion here;
                                #   the callback walker (not a generator)
                                #   keeps each subdirectory capability active
                                #   while visit() runs, so a verification
                                #   rejection propagates through the real
                                #   error and its close failure is retained;
                                #   build_tree_manifest()/verify_tree() enter the
                                #   root as a capability context manager; the
                                #   regular-file hash boundary stays raw
                                #   (O_NOFOLLOW open/read/close) because the
                                #   foundation owns no regular-file authority;
                                #   DescriptorError maps back to the existing
                                #   LockedNpmError("unsafe_cache_path" /
                                #   "tree_type_mismatch"); optional ops= keyword
                                #   on the public entry points
                                #
                                # Refinement: _root_is_not_a_directory() maps a
                                #   foundation validation-stage non-directory
                                #   root to the historical
                                #   "tree root <root> is not a directory"
                                #   detail while every other root failure keeps
                                #   "unsafe tree root <root>: <cause>";
                                #   build_tree_manifest() raises the
                                #   special-entry and escaping-symlink errors
                                #   while the root capability is still owned,
                                #   so its release can retain a root-close
                                #   failure as secondary

```

No npm path, digest, manifest field, containment rule, recursion policy, or
domain error reason changed. No `docker.transactions`, Docker, CLI, transport,
or cache module entered `tree.py`. The foundation keeps its design-fixed
`DescriptorError(stage, message, *, cause=None)` constructor and the same
`open_secure_path` signature. The final-component classification is **not**
type-aware: every open failure is translated by `_open_failure()` and keeps
its open-stage `DescriptorError` classification with the raw `OSError` as its
cause.

## Test files changed

```text
tests/test_npm_environment_tree.py               # new, 58 tests (Phase 7
                                                 #   7.1–7.3, 7.7–7.8, plus the
                                                 #   non-directory-root,
                                                 #   root-close-precedence,
                                                 #   verify-lifecycle, and
                                                 #   root-path-component
                                                 #   follow-up tests)
tests/test_transactions_phase5a_typed_preservation.py
                                                 # task 5A.1 inventory classifies
                                                 #   the new tree
                                                 #   cause-inspection site
                                                 #   (`_foundation_cause`)
```

## Task evidence

### RED

- **7.1** `TestTreeTraversalPrecedence` (5 tests) injects faults through a
  ledger `ops` backend and asserts a traversal failure stays primary while the
  enclosing subdirectory is released exactly once:
  `test_enumeration_failure_primary_over_subdirectory_close`,
  `test_stat_failure_primary_over_subdirectory_close`,
  `test_hash_read_failure_primary_over_subdirectory_close`,
  `test_directory_open_failure_primary_over_parent_close`, and
  `test_every_opened_directory_receives_one_close`. Each asserts the primary
  exception identity, the retained close diagnostic, `double_closes == []`,
  `live == set()`, and a one-to-one open/close pairing. Before the migration
  the `ops=` keyword did not exist (`TypeError`), so the cases fail.
- **7.2** `TestTreeRootLifecycle` (5 tests). The headline case
  `test_root_validation_failure_keeps_primary_with_secondary_close` patches
  `os.fstat`/`os.close` directly: the raw root helper closed in a bare
  `finally`, so the close error replaced the validation error before the
  migration (RED: an `OSError` escaped instead of `LockedNpmError`). The
  remaining cases inject a root `fstat`/`close` failure through `ops` and
  assert validation stays primary, the close diagnostic is retained, and
  manifest/verification root descriptors are never closed twice.
- **7.3** `TestTreePreMigrationCharacterization` (7 tests) passes **before and
  after** the migration: canonical content digest, canonical manifest bytes,
  canonical entry ordering, the symlinked-root `unsafe_cache_path` mapping, the
  special-file `unsupported_entry_type` mapping, the escaping-symlink
  `unsafe_symlink_target` mapping, and the `verify_tree` `tree_hash_mismatch`
  mapping.

### GREEN

- **7.4** `_open_tree_root(ops, root)` maps
  `DirectoryDescriptor.open_secure_path()` failures back to
  `LockedNpmError("unsafe_cache_path", f"unsafe tree root {root}: …")` and
  chains the foundation `DescriptorError`, matching storage. The root is
  opened with `require_owner=False`, preserving the prior no-owner-check root
  contract (ownership is still recorded and compared by the manifest). A
  validation-stage non-directory rejection is the sole exception: it maps to
  the historical `tree root <root> is not a directory` detail (see the
  follow-up section). Every open-stage failure -- including the raw no-follow
  `ENOTDIR`/`ENOENT` reported for a regular-file, symlinked, or missing
  component -- keeps the `unsafe tree root <root>: <cause>` diagnostic.
  The caller's path components are preserved verbatim (no lexical
  normalization): a relative root is prefixed with the current working
  directory rather than collapsed, so `missing/../tree`,
  `regular-file/../tree`, and `symlink/../tree` walk -- and reject -- every
  supplied component instead of silently inspecting the sibling `tree` (see
  the root-path-components follow-up).
- **7.5** `_walk_entries(directory, prefix, visit)` resolves names with
  `list_names()`, stats with
  `stat_child(name, follow_symlinks=False)`, and descends with
  `open_directory(name, label=rel, require_owner=False)`. Recursion stays in
  `tree.py`. A failed child-directory open maps to
  `LockedNpmError("tree_type_mismatch", f"cannot open directory entry {rel!r}: …")`.
  A `with sub:` block wraps the recursive `_walk_entries(sub, rel, visit)`
  call, releases each child capability at most once, and keeps an ordinary
  close failure secondary to the active traversal failure. Because `visit` is
  invoked inline while the containing capability is still active, an exception
  it raises is the real primary -- not a discarded `GeneratorExit` from
  closing a suspended generator.
- **7.6** `build_tree_manifest()` and `verify_tree()` enter the root capability
  with a context manager; the manifest/verification semantics are unchanged.
  Regular files stay on the raw `O_NOFOLLOW` boundary in `_hash_file_entry()`
  (the only remaining `os.close` in the module).
- **7.1/7.2/7.3** all pass after the migration.

### INTROSPECT

- **7.7** `TestTreeFoundationBoundary`:
  `test_no_raw_owned_directory_close` parses `tree.py` and proves the only
  `os.close(...)` call is inside `_hash_file_entry` (the regular-file
  boundary); every owned directory descriptor is released through the
  capability. `test_imports_only_foundation_and_shared_error` requires
  `docker.filesystem.descriptors` and `docker.filesystem.operations`, rejects
  `docker.transactions`, and admits only `docker.filesystem.*` foundation
  imports. `test_tree_still_owns_recursive_traversal` proves `_walk_entries`
  and its recursion remain in the module and that no recursive-removal API
  (`remove_tree`/`rmtree`) was introduced.
- **7.8** `test_canonical_manifest_bytes_are_pinned` serializes a fixture tree
  with uid/gid normalized to a sentinel and compares to bytes captured from
  the **pre-migration** implementation; `test_canonical_digest_is_pinned`
  compares the exact content-only digest. Both passed before the migration and
  pass after it, proving there is no serialized or identity drift. The
  content-only digest is also machine-independent:
  `9aadfdf6456a4c36bd551b3581a41d71c5f667c389ef8bda780c45ff10c2847c`.
- The Phase 5A repository-wide cause-access inventory gained the new
  `docker/npm_environment/tree.py::_foundation_cause` site, classified
  `inspect-only`; the AST guard still reports no prohibited escape. `tree.py`
  chains its domain error from the foundation error (`raise … from exc`) and
  does **not** copy diagnostics onto the domain wrapper, matching the Phase 5A
  / 9B contract that foundation secondaries stay reachable through `__cause__`.
  The correction to the non-directory detail removed the only other cause read,
  so the inventory again lists just `_foundation_cause`.

### Follow-up — non-directory root detail and build-validation root close

- `_root_is_not_a_directory(exc)` restores the historical
  `tree root <root> is not a directory` detail only for a validation-stage
  non-directory rejection while leaving every other root failure on
  `unsafe tree root <root>: <cause>`. It branches only on the foundation's
  typed error: `exc.stage == validate` with the `… is not a directory`
  message. There is **no** raw `os.lstat`, no `errno`/`OSError` inspection, and
  no `import errno` in the domain module.
- Final-component **open** failures stay typed at the open stage. The
  foundation translates every failed component open through `_open_failure()`
  (`DescriptorError(_STAGE_OPEN, "cannot open directory '<name>'", cause=exc)`,
  or `UnsafeDescriptorError` only for the `ELOOP` symlink rejection): there is
  no type probe and no post-open `statat`. Consequently a regular-file root
  rejected by `O_DIRECTORY` keeps the pre-migration
  `unsafe tree root <root>: [Errno 20] Not a directory: …` diagnostic rather
  than the non-directory domain wording, while a symlinked root keeps the
  `ELOOP` safety rejection. In every case the raw `OSError` stays reachable as
  `DescriptorError.cause` and the foundation error is chained as the direct
  cause of the domain error.
- Only a genuine **validation-stage** type rejection (the foundation proved the
  adopted root descriptor is not a directory) selects the historical
  `tree root <root> is not a directory` wording; it carries no raw cause and is
  reported exactly like a rejected adopted descriptor.
- `build_tree_manifest()` now raises the `unsupported_entry_type` and
  `unsafe_symlink_target` errors *inside* the root capability context, so the
  root descriptor is still owned when they are raised. Its release retains an
  ordinary root-close failure as secondary diagnostic context instead of
  letting it replace the domain error. Serialization and digest construction
  stay after the `with` block; entry ordering, manifest bytes, the canonical
  digest, and the error reasons/details are unchanged.
- Regression tests added to `TestTreeRootLifecycle`:
  - `test_regular_file_root_keeps_unsafe_root_diagnostic` creates a regular
    file as the root and asserts the reason is `unsafe_cache_path`, the detail
    starts with `unsafe tree root <root>:`, the direct cause is a
    `DescriptorError`, and the nested `DescriptorError.cause` is the raw
    `OSError` with `errno.ENOTDIR`.
  - `test_validate_stage_non_directory_root_maps_to_historical_detail` forces
    the validate-stage non-directory rejection (patched `os.fstat`) and asserts
    the historical `tree root <root> is not a directory` detail with the
    `DescriptorError` chained.
  - `test_special_entry_failure_keeps_primary_with_secondary_root_close` (FIFO)
    and
    `test_escaping_symlink_failure_keeps_primary_with_secondary_root_close`
    inject an ordinary root close failure and assert the historical reason and
    exact detail stay primary, the close failure is retained as secondary,
    the root receives exactly one close attempt
    (`close_paths.count(realpath(root)) == 1`), `double_closes == []`, and no
    descriptor remains live.
  - `test_regular_file_root_operational_open_failure_keeps_unsafe_wording`
    injects a final root-open `EIO` while the root really is a regular file
    and asserts the diagnostic stays `unsafe tree root <root>: <EIO>` (never
    `tree root <root> is not a directory`), proving operational open failures
    preserve their raw typed cause.

### Follow-up — verify-tree traversal lifecycle

`_iter_entries()` was a generator that held each subdirectory capability
across `yield`. When `verify_tree()` rejected a yielded entry (the `_compare()`
and extra-entry checks run in the consumer, outside the generator frame), the
`for` loop exited with the real `LockedNpmError`, but the *suspended* generator
was only closed afterwards. Closing threw `GeneratorExit` into the innermost
`with sub:` block, so the containing directory's ordinary close failure was
attached to a discarded `GeneratorExit` instead of the verification error.

The fix replaces the generator with a callback walker
`_walk_entries(directory, prefix, visit)`:

- `build_tree_manifest()` collects with `found.append`, materializing the full
  list before validation exactly as `list(_iter_entries(...))` did.
- `verify_tree()` performs the expected-entry lookup, extra-entry rejection,
  `seen` update, and `_compare()` call inside its `visit` closure.
- Recursion stays inside each directory's capability context
  (`with sub: _walk_entries(sub, rel, visit)`), so an exception raised by
  `visit` propagates synchronously through every active `__exit__` and each
  containing directory's close failure attaches to the actual primary failure.
- Sorting, the no-follow `list_names()` / `stat_child()` / `open_directory()`
  operations, regular-file hashing, and the special-entry classification are
  unchanged.

RED evidence (before the fix): a nested hash mismatch with an injected close
failure on the containing directory produced
`LockedNpmError("tree_hash_mismatch")` with no retained secondary note (the
close failure was discarded). GREEN: the same scenario retains
`secondary cleanup failure: OSError: [Errno 5] injected nested close failure`
and closes the containing directory exactly once.

Regression tests (`TestTreeVerificationLifecycle`):

- `test_nested_hash_mismatch_keeps_containing_close_secondary`
- `test_nested_extra_entry_keeps_containing_close_secondary`

Both inject a close failure on the containing nested directory, assert the
original `LockedNpmError` reason stays primary, the injected close error is
retained as a secondary diagnostic, and every opened directory is closed
exactly once (`double_closes == []`, `live == set()`, `close_order` and
`open_order` share a set, and the containing directory's close count is exactly
one). `test_tree_still_owns_recursive_traversal` was updated to require
`def _walk_entries` / `_walk_entries(sub, rel, visit)` and to reject
`_iter_entries`.

### Follow-up — root path components

`_open_tree_root()` previously made the caller's path absolute with
`os.path.abspath()`. That lexical normalization erases components before the
secure walker can validate them: `missing/../tree`, `regular-file/../tree`, and
`symlink/../tree` all collapsed to `<cwd>/tree`, so a missing directory, a
regular file, or a symlink in the prefix was silently skipped and a different
valid tree was inspected instead of the supplied path being rejected.

The fix preserves the components:

```python
absolute = str(root)
if not os.path.isabs(absolute):
    absolute = os.path.join(os.getcwd(), absolute)
```

`os.path.join` never collapses `..`, and `resolve()`/`normpath()` are not used,
so `DirectoryDescriptor.open_secure_path()` walks every component the caller
supplied. A missing component raises `ENOENT`, a regular file raises `ENOTDIR`,
and a symlink component is refused by `O_DIRECTORY | O_NOFOLLOW`; all of them
map through the existing domain translation to
`LockedNpmError("unsafe_cache_path", "unsafe tree root <root>: <cause>")`.
The `require_owner=False` flag, the label, and the validation-stage
`tree root <root> is not a directory` mapping are unchanged.

RED evidence (before the fix): the three flagged paths were accepted and
inspected the sibling `tree` under both `build_tree_manifest()` and
`verify_tree()` -- the regression test failed with `LockedNpmError not raised`
for each supplied spelling (12 subtest failures). GREEN: every bad component is
rejected with reason `unsafe_cache_path`.

Regression tests (`TestTreeRootPathComponents`):

- `test_build_tree_manifest_rejects_bad_parent_component`
- `test_verify_tree_rejects_bad_parent_component`

Both cover `missing/../tree`, `regular-file/../tree`, and `symlink/../tree` as
**relative** inputs (with the working directory set to the tree's parent) and as
**absolute** inputs, and require `LockedNpmError` with reason
`unsafe_cache_path`. They do not rely on lexical normalization: the relative
spellings only resolve because `os.getcwd()` is prefixed.

`test_existing_directory_parent_component_still_addresses_tree` is the
compatibility check: when every traversed component (`existing-directory`, then
`..`) is a real directory, `existing-directory/../tree` still builds and
verifies the expected manifest for both relative and absolute spellings.

## Secondary-diagnostic preservation

When a root or child directory validation fails and its release also reports
an ordinary close failure, the close failure is retained as secondary
diagnostic context on the foundation `DescriptorError` (via the shared
`CleanupFailures` precedence). The domain `LockedNpmError` chains directly from
that error, so the stage, original cause, and secondary cleanup diagnostics
remain reachable together through `__cause__`. A traversal failure raised while
a parent capability is still active additionally receives that parent's close
failure directly on the domain error (a bounded `__notes__` note, since
`LockedNpmError` is frozen). Manifest validation now runs before the root is
released, so a root-close failure during `unsupported_entry_type` /
`unsafe_symlink_target` is retained the same way.

## Serialization stability

The fixture manifest is unchanged byte-for-byte apart from the
machine-specific uid/gid that the golden test normalizes. Directory and file
modes are pinned explicitly (`0o755`/`0o644`; symlinks report `0o777`), so the
comparison is umask-independent. The content digest omits mode/ownership by
design and matches the pre-migration value exactly.

## VALIDATE

```text
python -m unittest tests.test_npm_environment_tree \
  tests.test_filesystem_directory_descriptors \
  tests.test_transactions_descriptor_integration \
  tests.test_transactions_l1_error_boundary \
  tests.test_transactions_l1_capabilities \
  tests.test_transactions_phase5a_typed_preservation \
  tests.test_transactions_phase9b_remap_boundaries \
  tests.test_npm_environment_fs_introspection
  -> Ran 352 tests in 3.508s
     OK

python -m unittest tests.test_npm_environment_tree \
  tests.test_transactions_phase9b_remap_boundaries \
  tests.test_transactions_phase5a_typed_preservation \
  tests.test_npm_environment_fs_introspection
  -> Ran 140 tests in 2.521s
     OK

python -m unittest <tests.test_npm_environment_*> \
  tests.test_transactions_phase8_npm_leaf_mechanics \
  tests.test_transactions_phase8_npm_specialization \
  tests.test_transactions_phase8_npm_lock
  -> Ran 735 tests in 14.529s
     OK (skipped=1)

python -m unittest tests.test_npm_environment_tree \
  tests.test_npm_environment_serialization \
  tests.test_npm_environment_publication \
  tests.test_npm_environment_publication_cleanup \
  tests.test_npm_environment_output_validation \
  tests.test_npm_environment_manifest_policy \
  tests.test_npm_environment_phase2_validate \
  tests.test_npm_environment_phase3_validate \
  tests.test_npm_environment_phase5_validate \
  tests.test_transactions_phase8_npm_leaf_mechanics
  -> Ran 147 tests in 1.347s
     OK (skipped=1)

python -m unittest discover -s tests -p 'test_*.py'
  -> Ran 5629 tests in 106.475s
     OK (skipped=13)

ty check docker --python-version 3.14 --output-format concise
  -> All checks passed!

scripts/check-types
  -> All checks passed!

git diff --check / git diff --cached --check
  -> clean

openspec validate extract-generic-descriptor-capabilities --strict
  -> Change 'extract-generic-descriptor-capabilities' is valid
```

## Summary

| Task | Status |
| --- | --- |
| 7.1–7.3 (RED / characterization) | complete |
| 7.4–7.6 (GREEN) | complete |
| 7.7–7.8 (INTROSPECT) | complete |
| 7.9 (VALIDATE) | complete |

## Notes

- The root open now walks every path component no-follow, so a symlink in an
  *intermediate* component of the caller-supplied root is rejected where the
  legacy single `os.open(O_NOFOLLOW)` followed it. This is the intended
  stronger containment of the descriptor foundation; final-component symlink
  rejection (the documented contract) is unchanged.
- A root-open failure now renders the failing component name inside the
  chained raw `OSError` (the foundation opens one basename at a time). The
  domain detail prefix `unsafe tree root <root>:` and the `unsafe_cache_path`
  reason are unchanged for operational and symlink failures; a
  validation-stage non-directory rejection instead uses the historical
  `tree root <root> is not a directory` detail. No caller pins the OS-supplied
  suffix.

# Phase 8 Verification Record — Cache Storage Migration

Date: 2026-10-07

Change: `extract-generic-descriptor-capabilities`

Scope: Phase 8 only. Phase 9 remains unstarted.

## Deliverable

`docker/versioning/cache_storage.py` uses the lightweight
`docker.filesystem` descriptor foundation for cache-root directory ownership.
Cache-root resolution, XDG behavior, `0700` mode policy, parent
non-mutation, recovery guidance, the `CacheStorageError`/`InventoryError`
mapping, and regular-file publication policy are unchanged. No raw
owned-directory close lifecycle remains in the cache storage module, and its
import allowlist admits only the explicit `docker.filesystem.cleanup`,
`docker.filesystem.descriptors`, and `docker.filesystem.operations`
submodules.

## Production files changed

```text
docker/versioning/cache_storage.py  # _open_parent_fd() walks components with
                                    #   DirectoryDescriptor.open_secure_path()
                                    #   plus open_directory()/create_directory()
                                    #   and closes the retained parent only after
                                    #   the next component is validated, so a
                                    #   failed parent release releases the child
                                    #   once and never retries the parent; a
                                    #   DescriptorError from the initial root
                                    #   acquisition is translated to
                                    #   CacheStorageError chained from it;
                                    #   _open_entry_no_follow()/ _inspect_entry()/
                                    #   _create_and_secure() now consume
                                    #   DirectoryDescriptor capabilities and
                                    #   treat ENOENT as absence only for an
                                    #   open-stage failure, never for validation;
                                    #   _open_directory_for_write() uses
                                    #   open_secure_path() and requires mode 0700,
                                    #   releasing through the shared
                                    #   CleanupFailures accumulator so an fstat or
                                    #   non-private-mode CacheStorageError stays
                                    #   primary and a close failure is retained as
                                    #   secondary;
                                    #   open_private_entry()/publish_private_entry()
                                    #   keep the regular-file 0600/rename policy
                                    #   local but open the directory capability;
                                    #   _prepare_explicit_xdg() walks with child
                                    #   capabilities and releases the final
                                    #   component on the success path so a
                                    #   successful walk never leaks the last XDG
                                    #   directory descriptor, while failure paths
                                    #   use _release_keeping_primary() so the
                                    #   active failure stays primary; every public
                                    #   filesystem entry point gains an injectable
                                    #   ``ops`` keyword; foundation DescriptorError
                                    #   maps back to CacheStorageError with the
                                    #   historical wording, and a descriptor-handoff
                                    #   close failure stays a raw OSError
```

The pure resolution layer (`_normalize`, `resolve_default_root`,
`resolve_local_root`, `resolve_effective_root`, and the named-child helpers)
is untouched and still performs no filesystem I/O.

## Test files changed

```text
tests/versioning/test_cache_storage_capabilities.py  # new, 37 tests (Phase 8
                                                     #   8.1–8.7, 8.9, 8.11)
tests/versioning/test_cache_storage.py               # 8.10 — every pure resolver
                                                     #   and named-child helper runs
                                                     #   under the no-filesystem-I/O
                                                     #   guard; docker.transactions
                                                     #   and the aggregate
                                                     #   docker.filesystem join the
                                                     #   forbidden-import set
tests/test_ownership_cutover_phase5.py               # _CACHE_STORAGE_ALLOWED_IMPORTS
                                                     #   admits the explicit
                                                     #   docker.filesystem.cleanup /
                                                     #   descriptors / operations
                                                     #   submodules; full-path import
                                                     #   helper and positive-adoption
                                                     #   test (8.3/8.8)
tests/test_transactions_phase5a_typed_preservation.py
                                                     # task 5A.1 inventory classifies
                                                     #   the new cache-storage
                                                     #   cause-inspection sites
tests/test_transactions_l1_error_boundary.py         # close inventory exempts
                                                     #   cache_storage's raw
                                                     #   foundation close boundary
                                                     #   from both close-rule scans
```

## Task evidence

### RED

New module run against the pre-migration raw-handoff implementation
(`python -m unittest tests.versioning.test_cache_storage_capabilities`):
**Ran 16 tests, 3 failures, 6 errors.**

- **8.1** `test_parent_close_failure_is_not_retried_and_child_is_released`
  failed `AssertionError: 2 != 1` — the raw parent/child handoff closed the
  failing parent twice (the bare `except BaseException: os.close(current_fd)`
  retried a close whose descriptor number was already released) and leaked the
  already-opened child. `test_opened_child_is_released_when_parent_close_fails`
  errored because the raw walker had no injectable ops seam.
- **8.2** All five primary/secondary cases errored because a failing parent
  close inside a bare `finally: os.close(parent_fd)` replaced the active
  `CacheStorageError` with the raw close `OSError` (and `publish_private_entry`
  had no ops seam).
- **8.3** `test_adopts_explicit_foundation_submodules` failed with
  `'docker.filesystem.descriptors' not found in {...}` — the positive adoption
  case fails before migration.
- **8.9** `test_has_no_raw_owned_directory_close` failed with 16 raw `os.close`
  calls parsed from the pre-migration module.
- **8.4** All three characterization tests (pure resolution, unsafe local-root
  mapping, parent non-mutation/recovery guidance) **passed before migration**,
  as required.

The Phase 5 allowlist check also failed RED:
`test_cache_storage_admits_only_explicit_foundation_submodules` —
`'docker.filesystem.descriptors' not found`.

### GREEN

- **8.5** `_open_parent_fd(ops, path, *, create_missing)` walks with
  `DirectoryDescriptor.open_secure_path(ops, os.sep, …)` and single-basename
  `stat_child()`/`open_directory()`/`create_directory()` calls. The initial
  root acquisition is wrapped so a `DescriptorError` becomes a
  `CacheStorageError` (`cannot access parent directory for {path}: …; restore
  access or retry`) chained from the foundation error, keeping callers'
  existing `OSError`/`ValueError` handling intact; the foundation still owns
  the cleanup of a failed factory call. The retained
  parent is closed only after the next component is validated; on failure the
  `except BaseException as primary` releases the current capability through
  `_release_keeping_primary(current, primary)`, so an ordinary close failure is
  retained as secondary diagnostic context instead of replacing an active
  unsafe/missing-parent `CacheStorageError`; the failing capability is
  attempted once and never leaked. Cache-specific
  root policy (`0700` creation, no chmod of pre-existing components), labels,
  and the historical component diagnostics are preserved.
- **8.6** `_open_entry_no_follow()` and `_inspect_entry()` consume capabilities;
  a genuinely missing entry (an initial open that fails with `ENOENT` at the
  ``open-descriptor`` stage) still maps to absence, while a validation-stage
  `ENOENT` (`fstat` after a successful open) propagates through `_entry_error()`
  and stays chained from the retained `DescriptorError`, so it can never be
  swallowed and let preparation continue to mutate the tree. Ownership/type/
  access failures map back to their exact historical `CacheStorageError` text.
  `_inspect_entry()`
  uses `with parent:` so an ordinary parent-close failure is attached as
  secondary diagnostic context instead of replacing the validation failure.
  `_create_and_secure()` uses `open_or_create_directory(mode=0o700)` and maps
  create/reopen/securing failures through `_secure_error()`. In
  `_open_directory_for_write()`, the `fstat` access failure and the non-`0700`
  privacy failure each construct the primary `CacheStorageError` first and then
  release the capability through `_release_keeping_primary()` (the shared
  `CleanupFailures` accumulator with `ordinary=(OSError,)`), so an ordinary
  directory-close failure is attached to the primary as secondary diagnostic
  context instead of masking it and the descriptor is released exactly once.
  A non-`OSError` privacy-check failure (a cancellation or unexpected defect)
  is caught by a following `except BaseException as primary` that releases the
  adopted capability through `_release_keeping_primary()` and re-raises the
  original exception unchanged, so process-control interruptions and defects
  never leak the adopted directory descriptor and are never remapped to
  `CacheStorageError`.
- **8.7** `open_private_entry()`/`publish_private_entry()` open the directory
  through `_open_directory_for_write()` (which uses `open_secure_path()` and
  enforces `0700`) while keeping the regular-file `0600` clamp and the
  descriptor-relative `os.replace` local. `open_private_entry()` no longer uses
  `with capability: return fd`: it opens and secures the file while tracking
  the descriptor separately, then hands the file to the caller only after
  `capability.close()` succeeds. A directory-close failure after the file was
  opened and secured releases the file exactly once
  (`_release_file_keeping_primary()`) and propagates the directory-close error
  as primary, with an ordinary file-close failure retained as secondary; an
  open/secure failure releases the file (when opened) and the directory once
  each through `_release_file_and_capability()` while the `CacheStorageError`
  stays primary. `_prepare_explicit_xdg()` wraps its initial filesystem-root
  acquisition the same way, reporting
  `cannot access filesystem root while preparing XDG_CACHE_HOME {xdg}: …`
  chained from the foundation error, then creates a
  missing component with `0700` (only when the initial open fails with
  ``ENOENT`` at the ``open-descriptor`` stage — a validation-stage `ENOENT`
  propagates through the domain-error mapping instead of triggering creation),
  validates existing components without
  chmod-ing them, rejects symlink/non-directory components, and keeps the final
  `os.access(..., W_OK)` writability check local. `_map_write_directory_error()`
  reports "does not exist" only for an open-stage `ENOENT`, so a validation
  failure is never misclassified as absence. The final walk capability is
  released on the success path (`current.close()` after the loop) so a
  successful preparation never leaks the last XDG directory descriptor, while
  an active failure releases the remaining capability through
  `_release_keeping_primary()` and is never retried. Either helper with an
  active failure retains a directory-close failure as secondary context.
- **8.8** `_CACHE_STORAGE_ALLOWED_IMPORTS` is now
  `{os, stat, errno, pathlib, errors, model, __future__,
  docker.filesystem.cleanup, docker.filesystem.descriptors,
  docker.filesystem.operations}`; the full-path import helper rejects the
  aggregate `docker.filesystem`, `docker.transactions`, and every higher cache
  consumer.

### INTROSPECT

- **8.9** `TestCacheStorageFoundationBoundary::test_has_no_raw_owned_directory_close`
  parses `cache_storage.py` and proves there is no `os.close(...)` call (and no
  `"os.close"` text); `test_still_owns_cache_root_resolution_and_security_policy`
  proves every resolver, prepare function, and publication function is still
  defined by the module.
- **8.10** `TestPureResolutionFunctionsHaveNoFilesystemIO` runs
  `resolve_default_root`, `resolve_local_root`, `resolve_effective_root`, and
  all five named-child helpers inside the no-filesystem-I/O guard; the existing
  `_PureResolutionTestCase` coverage is unchanged.
- **8.11** `test_foundation_imports_no_domain_or_transaction_package` parses
  every `docker/filesystem/**/*.py` file and proves the foundation imports
  neither `docker.versioning`, `docker.transactions`, nor
  `docker.npm_environment`. Cache-root policy authority is still proven unique
  to `cache_storage.py` by
  `tests/test_ownership_cutover_phase5.py::test_cache_authority_api_is_defined_only_by_cache_storage`.

### VALIDATE

Task 8.12 command (before Phase 8: 117 tests):

```text
python -m unittest tests.versioning.test_cache_storage \
                   tests.versioning.test_cache_storage_security \
                   tests.test_ownership_cutover_phase5 \
                   tests.test_cache_root_ownership_phase3 \
                   tests.test_moved_local_state_obligations_phase5
  -> Ran 119 tests in 2.832s
     OK
```

Task 8.12 command plus the new capability module (the final combined Phase 8
gate):

```text
python -m unittest tests.versioning.test_cache_storage \
                   tests.versioning.test_cache_storage_security \
                   tests.versioning.test_cache_storage_capabilities \
                   tests.test_ownership_cutover_phase5 \
                   tests.test_cache_root_ownership_phase3 \
                   tests.test_moved_local_state_obligations_phase5
  -> Ran 156 tests in 2.951s
     OK
```

New capability module (37 tests):

```text
python -m unittest tests.versioning.test_cache_storage_capabilities
  -> Ran 37 tests in 0.095s
     OK
```

Broader cache/versioning regression gate (313 tests, adds the new module,
`test_http_cache_handoff`, `test_version_cache`,
`test_constructor_cache_contracts`, `test_cache_release_ordering_phase3`, and
the typed-preservation/remap guards):

```text
  -> Ran 313 tests in 5.511s
     OK
```

L1 close/typed-boundary inventory (the new cache-storage close accumulator is
exempted from both close-rule scans alongside the foundation):

```text
python -m unittest tests.test_transactions_l1_error_boundary
  -> Ran 66 tests in 0.869s
     OK
```

Repository-wide discovery (before Phase 8: 5629 tests):

```text
python -m unittest discover -s tests -p 'test_*.py'
  -> Ran 5668 tests in 95.371s
     OK (skipped=13)
```

Static checks:

```text
ty check docker --python-version 3.14 --output-format concise   -> All checks passed!
scripts/check-types                                             -> All checks passed!
git diff --check                                                -> clean
```

The 39 added tests are the 37 capability tests plus the 8.10 pure-resolution
guard and the 8.3/8.8 positive-adoption test.

### Follow-up — XDG success release and write-directory precedence

Two descriptor-lifecycle defects remained after the initial migration, both
fixed and pinned by new fault-injection tests.

**Successful XDG walk leaked its final capability.** `_prepare_explicit_xdg()`
kept the last opened component as ``current`` and only released it on an
exception path, so a successful `prepare_default_root()` left one directory
descriptor live. The writability check now runs after the parent/child
handoff (so exactly one capability is ever open), the walk releases the final
component with `current.close()` after the loop, and every failure path uses
`_release_keeping_primary()` so the active `CacheStorageError` stays primary.
`TestExplicitXdgLifecycle::test_successful_explicit_xdg_walk_releases_every_capability`
asserts `sorted(ops.open_paths) == sorted(ops.close_paths)`,
`ops.double_closes == []`, and `ops.live == set()`.

**Write-directory validation errors were masked by close failures.**
`_open_directory_for_write()` released the capability with a bare
`capability.close()` after building the fstat/non-private `CacheStorageError`,
so an ordinary close failure replaced the intended primary. Both branches now
release through the shared `CleanupFailures` accumulator
(`_release_keeping_primary()`), attaching an ordinary close failure as
secondary context while the validation `CacheStorageError` (with the fstat
`OSError` as its `__cause__`) stays authoritative.
`TestWriteDirectoryValidationPrecedence` covers an injected second-call fstat
failure and a real non-`0700` directory, each combined with an injected
directory-close failure, and asserts the primary message, the secondary
retention, `ops.double_closes == []`, and `ops.live == set()`.

RED evidence (the two fixes temporarily reverted):
`TestExplicitXdgLifecycle` and `TestWriteDirectoryValidationPrecedence` →
**Ran 3 tests, 1 failure, 2 errors** — the leaked `xdg-cache` open path has no
matching close, and the injected directory-close `OSError` propagates out of
`open_private_entry()` instead of the intended `CacheStorageError`. GREEN
restored the then-19-test module.

**Regular-file handoff leaked the file when the directory close failed.**
`open_private_entry()` used `with capability: return fd`; a failure in the
capability's `__exit__` (`capability.close()`) raised after the file was opened
and secured but before the return, so the regular-file descriptor leaked. The
function now tracks the file descriptor separately, opens and secures it while
the capability is held, and only returns it after an explicit, successful
`capability.close()`. A directory-close failure releases the file exactly once
through `_release_file_keeping_primary()` (the directory capability has
already recorded its single release attempt, so neither close is retried) and
propagates the directory-close error as primary, retaining an ordinary
file-close failure as secondary. An open/secure failure releases the file
(when opened) and the directory once each through
`_release_file_and_capability()`, keeping the `CacheStorageError` primary and
ordinary cleanup failures as secondary. `TestOpenPrivateEntryHandoff` covers
(a) a directory-close failure after a successful secure, (b) a combined
directory-close plus file-close failure keeping the directory error primary
with the file error as secondary, and (c) a secure failure staying primary
over a directory-close failure; each asserts `ops.live == set()`,
`ops.double_closes == []`, and that every opened descriptor received exactly
one close attempt.

RED evidence (the `with capability: return fd` shape temporarily restored):
`TestOpenPrivateEntryHandoff` → **Ran 3 tests, 2 failures** — the opened
`.../versioning/entry` file never appears in `close_paths` for either
directory-close case. GREEN restores 22/22.

**Parent-walk validation failures were masked by a close failure.**
`_open_parent_fd()` ended with a bare `except BaseException: current.close();
raise`, so an ordinary close `OSError` on the current directory capability
replaced an active unsafe/missing-parent `CacheStorageError`. The cleanup now
runs through `_release_keeping_primary(current, primary)`, which keeps the
validation error authoritative and retains the close failure as secondary
diagnostic context while releasing the capability exactly once.
`TestParentWalkValidationPrecedence` makes a regular file stand in for a
parent component (rejecting the walk one level above the root) and injects a
close failure on that parent capability; it asserts the `unsafe parent
component` `CacheStorageError` stays primary, the close `OSError` is retained
as secondary, the capability is closed exactly once, and `ops.live == set()`.

RED evidence (the bare `except BaseException: current.close()` temporarily
restored): `TestParentWalkValidationPrecedence` → **Ran 1 test, 1 error** —
the injected `OSError: injected current close failure` propagates instead of
the `CacheStorageError: unsafe parent component`. GREEN restores 23/23.

**Non-ordinary privacy-check failures leaked the adopted directory.**
`_open_directory_for_write()` only caught `OSError` around the post-adoption
`ops.fstat()` privacy check, so a `KeyboardInterrupt`/`SystemExit` or an
unexpected defect propagated out of the helper without releasing the adopted
directory capability. A following `except BaseException as primary` now
releases the capability through `_release_keeping_primary()` and re-raises the
original exception unchanged (it is never converted to `CacheStorageError`).
`TestWriteDirectoryValidationPrecedence` adds
`test_unexpected_defect_fstat_failure_is_reraised_and_released_once`
(`RuntimeError`) and
`test_process_control_fstat_failure_is_reraised_and_released_once`
(`KeyboardInterrupt`); each injects the failure on the second `fstat()` call
(the privacy check, after adoption validates the owner) and asserts the exact
exception object is raised, the capability is closed exactly once
(`close_paths.count(versioning) == 1`), `ops.double_closes == []`, and
`ops.live == set()`.

RED evidence (the `except BaseException` branch temporarily removed):
`TestWriteDirectoryValidationPrecedence` → **Ran 4 tests, 2 failures** — both
new tests fail with `AssertionError: 0 != 1` because the capability was never
released (leaked). GREEN restores 25/25.

**Validation-stage `ENOENT` was swallowed as absence.** `_open_entry_no_follow()`,
`_prepare_explicit_xdg()`, and `_map_write_directory_error()` treated *any*
`ENOENT` as a missing entry. Because `DirectoryDescriptor` validation (`fstat`
after a successful open) also raises `ENOENT`, a directory removed between the
open and the stat was mistaken for absence: inspection silently passed,
preparation continued and chmod-ed the tree, and the write helper reported
"does not exist" for an entry that really did exist. All three now require
`exc.stage == _STAGE_OPEN` in addition to the errno, so only an initial open
that finds no entry is absence; a validation-stage `ENOENT` propagates through
the existing domain-error mapping, stays chained from the retained
`DescriptorError`, and prevents any subsequent creation or hardening.
`TestStageNarrowedAbsenceHandling` covers all three paths (cache-root
inspection via `prepare_resolved_root()`, explicit-XDG preparation via
`prepare_default_root()`, and write-directory validation via
`open_private_entry()`), injecting a one-shot validation `ENOENT` together with
a directory-close failure and asserting the `CacheStorageError` is raised, the
close failure is reachable through the exception chain, `ops.mkdir_order == []`
and `ops.chmod_order == []`, the existing mode is unchanged, the capability is
closed exactly once, `ops.double_closes == []`, and `ops.live == set()`. Three
sibling tests confirm a genuinely missing descendant, a genuinely missing XDG
component, and a genuinely missing write directory keep their intended absence
behavior (creation with `0700`, or the "does not exist" diagnostic).

RED evidence (the three stage checks temporarily reverted to the errno-only
condition): `TestStageNarrowedAbsenceHandling` → **Ran 6 tests, 2 failures, 1
error** — the XDG path reports `cannot create ... File exists`, the write helper
reports `does not exist`, and the cache-root path lets hardening proceed until
the injected close failure escapes. GREEN restores 31/31.

**Root-acquisition failures escaped as `DescriptorError`.** The initial
`DirectoryDescriptor.open_secure_path(ops, os.sep, …)` calls in
`_open_parent_fd()` and `_prepare_explicit_xdg()` were not wrapped, so a root
`openat()` failure (for example `EMFILE`) or a root `fstat()` validation
failure (`EIO`) surfaced as a foundation `DescriptorError`, bypassing the
`OSError`/`ValueError` handlers callers already use for cache preparation.
Both acquisition sites now catch `DescriptorError` and raise a
`CacheStorageError` chained from it (`cannot access parent directory for
{path}: …; restore access or retry` and `cannot access filesystem root while
preparing XDG_CACHE_HOME {xdg}: …; restore access or retry`), so the
foundation cause and any secondary close diagnostics stay reachable through
`__cause__`. Nothing is closed manually on the factory-failure path (the
foundation owns that cleanup), no `BaseException` is caught or converted, and
no caller needed new error handling.

`TestRootAcquisitionDomainMapping` covers both preparation paths
(`prepare_resolved_root()` and `prepare_default_root()`) against three fault
scenarios: a root `openat()` `EMFILE`, a root `fstat()` validation `EIO`, and a
root validation failure combined with a root cleanup-close failure. Each test
asserts the raised error is a `CacheStorageError`, that `__cause__` is the
foundation `DescriptorError` with the injected error as its `cause` (and, in
the combined case, the close failure retained as secondary diagnostic
context), that the root mode stays `0755` with no `mkdir`/`chmod` attempt, and
that every opened descriptor receives exactly one release attempt
(`open_paths` vs `close_paths`, empty `double_closes`, empty `live`). The
`EMFILE` case additionally confirms the factory returned no descriptor, so
nothing was opened or released.

RED evidence (the two `except DescriptorError` wrappers temporarily removed):
`TestRootAcquisitionDomainMapping` → **Ran 6 tests, 6 errors** — the raw
`DescriptorError: cannot stat directory '/'`/`cannot open directory` escapes
instead of a `CacheStorageError`. GREEN restores 37/37.

The `docker.filesystem.cleanup` submodule joins the cache-storage allowlist,
and the L1 close-boundary inventory exempts
`docker/versioning/cache_storage.py` from both close-rule scans through the
shared `RAW_OSERROR_CLOSE_MODULES` set: cache storage holds
raw-`OSError` foundation capabilities and must not import the transaction
substrate to obtain `CloseStageFailure`.

No specification requirement file was changed; only `tasks.md` checkboxes
8.1–8.12 were marked complete.

# Phase 9A addendum — exhaustive `close()` examination

This records the close-site sweep requested on top of the Phase 9A
primary-preserving cleanup unification: every `close()` call in `docker/` was
examined and those that fit the shared `CleanupFailures` contract were migrated.

## Method

An AST inventory of every `Call` whose attribute/name is `close` was produced
for all of `docker/**/*.py`, tagged with its enclosing context (`finally`,
`except`, `try`-body, method definition) and function. Each site was then
classified as:

- **FIT** — runs in a `finally`/`except` cleanup context where an exception can
  be propagating, so a close failure could mask the primary; migrated to
  `CleanupFailures` with the operation's own tracked primary.
- **SOLE** — a success/absence path close with no in-flight primary; a close
  failure is the only failure and propagates naturally. Left raw.
- **SWALLOW** — a deliberate best-effort close whose failure is intentionally
  ignored by the owning contract. Left raw.
- **PRIMITIVE** — the L0 `close` implementation or a `def close` method.
- **BOUNDARY** — the module may not import `docker.transactions.cleanup`
  without violating a documented leaf/ownership boundary or an enforced import
  allowlist. Left raw.

## Architectural boundary

`docker.transactions.cleanup` lives inside the `docker.transactions` package,
whose `__init__` imports the full L0–L2 substrate (`capabilities`, `codec`,
`errors`, `locking`, `posix`, `regular`). Importing the accumulator therefore
pulls the whole substrate in. Only modules that already depend on
`docker.transactions` can adopt it without a new package dependency:

```
docker/npm_environment/publication.py
docker/versioning/artifact_cache.py
docker/versioning/build_cache.py
docker/versioning/build_cleanup.py
docker/versioning/build_generations.py
docker/versioning/build_materialization.py
docker/versioning/build_orchestration.py
docker/versioning/build_snapshot.py
docker/versioning/effective.py
docker/versioning/project_state.py
docker/versioning/rendering.py
```

This is the deciding "fits" criterion. The leaf/allowlisted modules below can
only adopt the accumulator if it is relocated to a lightweight module (or the
allowlists are amended) — a separate architecture decision, not a Phase 9A
change.

## Migrated (this sweep)

| Module | Sites migrated |
| --- | --- |
| `npm_environment/publication.py` | `_durable_write` fd; `_fsync_dir` fd; `_fsync_tree` file fd; `_fsync_tree` directory fd; `_atomic_publish` `outputs_fd` |
| `versioning/project_state.py` | `_open_private_dir_at` fstat-failure and mode-rejection closes (now secondary to the raised `ProjectStateError`); `_read_metadata` fd; namespace `fds` batch; `validate_project_state` `namespace_fd`/`projects_fd`/`root_fd` |
| `versioning/build_cache.py` | `_open_namespace_fd`; `_open_relative_dir`; `_validate_relative_dir`; `_ensure_relative_dir`; `prepare_build_cache`; `_open_validated_child`; `open_build_cache_state` (`opened` batch + `namespace_fd`); `publish_verified_blob` (fd + `fd/algorithm/blob/tmp/namespace` batch); `_validate_blob_descriptor`; `_verify_published_blob`; `_bootstrap_constructor_project_lock_parent` (`current_fd` + `namespace_fd`); `_remove_snapshot_tree`; `recover_abandoned_snapshots` `state.close()` |
| `versioning/artifact_cache.py` | 25 descriptor closes: read-only verifier `blob_fd/algo_fd/root_fd`; `_ensure_dir_private` `fd/parent_fd`; `_open_directory_chain`; `_remove_tree_at`; `stat_blob`; `read_bytes`; `inspect_and_digest`; `ensure_secure_dir`; `create_temp`; `append_temp`; `finalize_temp`; `cleanup_temp`; `quarantine_or_remove`; `get_permissions`; `set_permissions`; `atomic_publish` (`source_fd` + `target_fd`); `mkdtemp` |
| `versioning/build_snapshot.py` | validated-source fd (per iteration); staging-directory fd; plus the permission-recovery actions in `cleanup_artifact_snapshot` (initial `PermissionError` primary; `_collect_snapshot_directories` enumeration, `_restore_directory_permissions` chmod, `_remove_readonly_snapshot_tree` final removal) |

All migrated `finally` sites track the operation's own exception explicitly so
no ambient exception is ever captured:

```python
primary: BaseException | None = None
try:
    <operation>
except BaseException as exc:
    primary = exc
    raise
finally:
    failures = CleanupFailures(primary)
    failures.run(<close action>, ordinary=(OSError,))
    result = failures.complete()
    if result is not None:
        raise result
```

Migrated `except` scopes instead bind the handler (`except BaseException as
exc:`) and pass `exc` directly. The `CleanupFailures(sys.exc_info()[1])` form
used during the first migration pass was replaced because, inside a `finally`,
`sys.exc_info()` reports a caller's handled exception when the protected
operation succeeded, which silently dropped a close failure on the success
path and adopted a caller interruption as the primary. Nested scopes each hold
their own tracked primary; remaining `sys.exc_info()` uses were removed from
the six modules that no longer referenced `sys`.

Multi-action batches (`opened`, the `publish_verified_blob` tuple, the
`validate_project_state` release set, `atomic_publish`) attempt every close even
when an earlier one fails, and never retry.

### `publish_verified_blob` and `open_build_cache_state`

These two `build_cache` release paths were subsequently hardened so that a
failed release cannot skip an independent action or leak a retained descriptor
(regression module `tests/test_transactions_phase9a_build_cache_cleanup.py`):

- `publish_verified_blob`'s failure path now runs the temporary-descriptor
  close, the temporary-entry unlink (`_unlink_absent_ok`, which suppresses only
  `FileNotFoundError`), and every remaining descriptor close through one
  accumulator.  Ownership of the temporary descriptor is dropped before its
  close, so an interruption or unexpected close defect neither skips the unlink
  nor causes a retry in the `finally`.  A separate `temporary_owned` flag is
  set only after the exclusive create succeeds and cleared once the entry is
  published, so an `O_EXCL` name collision never unlinks or modifies the
  pre-existing entry.
- `open_build_cache_state` now owns every child descriptor until the namespace
  close succeeds.  A failed namespace close — ordinary, unexpected, or
  interruption — releases every retained child exactly once before propagating,
  and on the failed-open path a child-release interruption is authoritative
  over the original open failure and later ordinary namespace release failures.

### `validate_project_state`

The failed-validation path now maps a missing ancestor to `ProjectStateError`
and retains it as the local primary before releasing the namespace and every
opened ancestor through one accumulator, so an ordinary ancestor-close failure
is a secondary diagnostic rather than a replacement, and a namespace-close
interruption or unexpected defect stays authoritative over later ancestor
releases.  The success path releases the ancestors first and transfers the
namespace descriptor to the returned state only when that release succeeds;
if an ancestor release prevents the return, the retained namespace descriptor
is released exactly once before the failure propagates
(`tests/test_transactions_phase9a_project_state_cleanup.py`).

## Examined and left raw (with reason)

| Site(s) | Reason |
| --- | --- |
| `npm_environment/publication.py` `read_index`, `_append_index`, `_read_private_regular` | **SWALLOW** — `try: directory.close() except OSError: pass`; these best-effort reads promise never to fail, so a close failure is intentionally ignored |
| `docker/transactions/posix.py::close`; `DirectoryCapability.close`, `FileCapability.close`, `LockCapability.close`, `BuildCacheState.close`, `ValidatedProjectState.close`, `_BuildStorageHandle` methods | **PRIMITIVE** — the close implementation/method, not a call site |
| `npm_environment/storage.py` | **BOUNDARY** — documented leaf that imports only the shared error type; adopting the accumulator would pull in L0–L2 |
| `npm_environment/tree.py` | **BOUNDARY** — same leaf property |
| `npm_environment/execution.py`, `npm_environment/lifecycle.py` | **BOUNDARY** + **EXCLUDED** — npm process lifecycle cleanup is a domain-owned result/redaction model, already excluded by Phase 9A |
| `versioning/cache_storage.py` | **BOUNDARY** — enforced import allowlist `{os, stat, pathlib, errors, model, __future__}` (`test_ownership_cutover_phase5`) forbids the accumulator |
| `versioning/build_context_confinement.py`, `docker/runtime_installer.py`, `versioning/host_presentation.py` | **BOUNDARY** — modules do not depend on `docker.transactions`; adopting would introduce a new substrate dependency |

## Finding

The two `docker/versioning/build_snapshot.py` staging/source closes were
genuine in-scope 9A.9 misses. The remainder of the migrated set hardens
pre-existing masking closes that were not enumerated by the Phase 9A task
lists; each is an intentional hardening recorded here, not a behavior change
for a conforming path.

### `create_artifact_snapshot` staging-open and cleanup precedence

`docker/versioning/build_snapshot.py::create_artifact_snapshot` was further
hardened (regression coverage in `tests/test_transactions_phase9a_specialized.py`):

- The staging-directory open failure no longer discards cleanup failures with
  `except BaseException: pass`.  The original open `OSError` is retained as the
  `SnapshotError` cause and the tree removal runs through the accumulator: an
  ordinary failure is secondary, an unexpected defect or interruption is
  authoritative.
- Construction cleanup narrows its ordinary policy from `(Exception,)` to the
  cleanup function's declared `(OSError, SnapshotError)`, so a programmer
  defect receives unexpected-defect precedence instead of silently becoming a
  secondary diagnostic.
- An exception raised by `CleanupFailures.complete()` is captured into
  `snapshot_primary` and re-raised, so the `finally` closes `staging_fd` under
  the authoritative failure.  The first tree-cleanup interruption stays
  primary over a later staging-close interruption, and the staging descriptor
  is still closed exactly once.

### `cleanup_artifact_snapshot` permission-recovery branch

`docker/versioning/build_snapshot.py::cleanup_artifact_snapshot` previously
suppressed the failures of its own readonly-tree recovery work (`except
OSError: pass` around directory chmod, then `shutil.rmtree(...,
ignore_errors=True)` followed by a generic `SnapshotError`).  That violates the
9A.5/9A.9 requirement that cleanup failures stay observable.  The recovery
branch now:

- runs directory enumeration, each snapshot-directory `chmod 0o700`, the root
  `chmod`, and the final removal as independent `CleanupFailures` actions (with
  the initial `PermissionError` as the primary), so an unexpected defect or
  interruption cannot skip the remaining actions.  Enumeration appends into a
  caller-owned list, so a failure from `rglob()` no longer bypasses root
  restoration and final removal or loses the initial permission diagnostic;
- returns normally only when every action succeeds; an ordinary recovery
  failure is attached to the initial permission failure and surfaced as a
  `SnapshotError` whose `__cause__` retains that raw failure, and an unexpected
  defect/interruption is authoritative;
- restores permissions only on snapshot directories (never hard-linked payload
  files), declares `FileNotFoundError` as the sole idempotent absence in the
  initial attempt and in the `_collect_snapshot_directories`,
  `_restore_directory_permissions`, and `_remove_readonly_snapshot_tree`
  helpers, and lets every other initial removal failure (e.g. `EIO`) propagate
  raw without triggering recovery.  A snapshot root or discovered child that
  vanishes before its `chmod` is accepted when the final removal also confirms
  absence.

Fault-injection coverage in `tests/test_constructor_build_snapshot.py`
(`SnapshotCleanupRecoveryFaultTests`, 16 tests) drives the real recovery branch
on a real readonly tree; 7 of the 10 earlier cases and all 6 enumeration/vanish
cases fail against the prior implementation.

## Correction — descriptor-handoff closes are not SOLE

`build_cache._open_relative_dir` and `artifact_cache._open_directory_chain`
were previously classified **SOLE** ("sequential descriptor hand-off after a
successful open"). That classification was wrong. At each loop iteration the
walker owns the parent descriptor *and* the freshly opened child descriptor at
the same time, and the parent close is a cleanup action whose failure requires
releasing the child. The original ordering

```python
child_fd = os.open(component, flags, dir_fd=current_fd)
os.close(current_fd)   # may raise
current_fd = child_fd  # skipped on failure
```

left the handler closing `current_fd`, which was still the parent, so a failed
parent close was retried and the child leaked (reproduced under fault
injection). The missing-component path had the same retry shape
(`os.close(current_fd); return None`).

Both walkers now transfer ownership of the child immediately after opening it
and relinquish the parent reference before closing it:

```python
next_fd = os.open(component, flags, dir_fd=current_fd)
parent_fd, current_fd = current_fd, next_fd   # child owned first
...
releasing_fd, parent_fd = parent_fd, -1        # drop before close
os.close(releasing_fd)                         # a failure is never retried
```

The handler releases the child and any still-owned parent through independent
`CleanupFailures` actions, so an unexpected defect or process-control
interruption follows the accumulator precedence and both descriptors are
released exactly once. `_open_relative_dir` additionally transfers ownership
before `os.fchmod`, so an fchmod failure releases both descriptors, and clears
the tracked descriptor before the missing-component close.

Regression coverage: `tests/test_transactions_phase9a_descriptor_handoff.py`
(15 tests) injects ordinary parent-close failures, unexpected defects,
process-control interruptions, and child-close-after-parent-close failures for
both walkers, plus the `_open_relative_dir` missing-component and `fchmod`
failure paths. Reverting either fix makes the corresponding tests fail with a
retried parent close and a leaked child.

## Scope decision outstanding

To cover the **BOUNDARY** modules (`storage`, `tree`, `execution`, `lifecycle`,
`cache_storage`, `build_context_confinement`, `runtime_installer`,
`host_presentation`), `CleanupFailures` would need to move to a lightweight
module outside the `docker.transactions` package (which would also let
`attach_secondary` be shared without the L0–L2 import). That is an architecture
change beyond Phase 9A and is intentionally not done here.

## Validation

```text
ty check docker --python-version 3.14 --output-format concise
All checks passed!

python -m unittest discover -s tests -p 'test_*.py'
Ran 5223 tests in 100.915s
OK (skipped=13)

git diff --check
clean
```

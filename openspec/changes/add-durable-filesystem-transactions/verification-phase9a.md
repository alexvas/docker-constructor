# Phase 9A — Primary-Preserving Cleanup Unification (verification)

**Change:** `add-durable-filesystem-transactions`
**Phase:** 9A (tasks 9A.1–9A.12)
**Result:** PASS

## Delivered artifacts

- `docker/transactions/cleanup.py` — internal `CleanupFailures` accumulator with
  the mandatory keyword-only `ordinary` policy and the sole terminal
  `complete()` operation. It is deliberately **not** re-exported from
  `docker.transactions`.
- `docker/transactions/errors.py` — `attach_secondary` now de-duplicates by
  identity (including `TransactionError.add_secondary`) and falls back to
  bounded `BaseException.add_note()` text when object attachment is
  unavailable, without changing the authoritative exception identity.
- Migrated shared L1/L2 contracts and consumer adapters (below).
- Tests:
  - `tests/test_transactions_phase9a_accumulator.py` (9A.1–9A.3)
  - `tests/test_transactions_phase9a_audit.py` (9A.4)
  - `tests/test_transactions_phase9a_specialized.py` (9A.5)

## Accumulator precedence (9A.1, 9A.6)

`complete()` applies `interruption > unexpected defect > original Exception
primary > ordinary failure`:

- An original non-`Exception` `BaseException` is classified as the first
  authoritative interruption and stays authoritative over later cleanup
  defects and interruptions.
- A higher-precedence failure that displaces an original `Exception` primary
  retains it (and every other failure) as secondary diagnostics in the
  deterministic order: later interruptions, unexpected defects, displaced
  original primary, ordinary failures (each in action order).
- With no primary, the first ordinary failure is returned for caller-owned
  mapping; later ordinary failures are attached to it.
- `run()` attempts each action exactly once and never re-raises an action
  failure; `run()` after completion and a second `complete()` raise
  `RuntimeError`; positional or invalid `ordinary` policies raise `TypeError`;
  action return values are discarded.

The truth table in `tests/test_transactions_phase9a_accumulator.py` covers no
failure, an original `Exception` primary, an original non-`Exception`
primary, one/multiple ordinary failures with and without a primary, one/multiple
unexpected defects, one/multiple cleanup interruptions, and every mixed
precedence combination.

## Attachment (9A.3, 9A.7)

- Exception objects are retained without duplicate identity for both
  `TransactionError.secondary` and the generic `_transaction_secondary` slot.
- When attribute storage is unavailable, each secondary is rendered as a
  bounded (≤ 240 chars) `add_note()` entry, and the total number of
  secondary-cleanup notes is capped per primary **across repeated
  `attach_secondary` calls** (≤ 32, counting any pre-existing notes); the
  authoritative exception object and text are unchanged.

## Audit and migration (9A.4, 9A.8)

Every audited site was classified. `HARDEN` means the shared accumulator
changed behavior intentionally, with a regression test; `PARITY` means the
existing precedence/ordering/continuation already matched and the site keeps
its explicit form.

| Site | Class | Evidence |
| --- | --- | --- |
| `locking._release_descriptor` | HARDEN | Original non-`Exception` primary survives later cleanup interruption; both unlock and close attempted exactly once; `test_transactions_phase9a_audit.ReleaseDescriptorTests` |
| `regular._discard` | HARDEN | Original interruption authoritative; owned temporary unlink still attempted exactly once after close raises an unexpected defect or interruption; `test_transactions_phase9a_audit.DiscardTests` |
| `capabilities._adopt`, `open_regular`, `DirectoryCapability.__exit__` | HARDEN | Ordinary close failure secondary; interruption authoritative |
| `capabilities.from_secure_path` | HARDEN | Both the retained parent and the adopted leaf close through the shared accumulator (the leaf only when it will not be returned); the original interruption stays authoritative over a later leaf interruption |
| `npm_environment.publication._release_identity_lock` | HARDEN | Original non-`Exception` primary survives later release interruption; both lock and directory release attempted |
| `npm_environment.publication._atomic_publish` staging-tree cleanup | HARDEN | `shutil.rmtree(..., ignore_errors=True)` removed; making the owned read-only tree writable and removing it are independent accumulator actions; only `FileNotFoundError` is idempotent absence; publication failure stays authoritative over ordinary cleanup failures, while an unexpected defect or interruption takes accumulator precedence (`test_npm_environment_phase9a_staging_cleanup`) |
| `build_cache._BuildStorageHandle.__exit__` | HARDEN | Every capability close attempted; primary preserved |
| `build_cache.ConstructorProjectBuildLock.release` | HARDEN | Raw lock cause re-mapped; generation-directory close still attempted |
| `artifact_cache.FileIdentityLock.release` | HARDEN | Raw lock cause re-mapped; directory close still attempted |
| `artifact_cache._materialize_one` validation + finally cleanup | HARDEN | Cleanup/release failures attach to the primary; interruption now propagates instead of being wrapped |
| `project_state.ValidatedProjectState.__exit__` | HARDEN | Namespace close interruption authoritative |
| `rendering` effective-destination / generated-directory closes | HARDEN | Close interruption authoritative; no masking |
| `effective` runtime-projection close | HARDEN | Close interruption authoritative; ordinary close failure still raised with no primary |
| `build_cleanup.cleanup_superseded` directory-close batch | HARDEN | All adopted directories released; ordinary failures still aggregated into `BuildCleanupError` |
| `build_materialization` failed-verification, uncommitted-blob, and temporary cleanup | HARDEN | Unlink failures no longer swallowed; temporary idempotent absence preserved |
| `build_snapshot` construction, invalid-hard-link, copy, source-open, hard-link-validation, validated-source-descriptor, and staging-directory-descriptor cleanup | HARDEN | Absence idempotent; non-absence close/unlink failures observable as secondary; the staging and validated-source descriptor closes run through the accumulator so a close failure never masks an in-flight construction primary (`test_transactions_phase9a_specialized.SnapshotDescriptorCloseTests`) |
| `build_orchestration._release_build_lock` | EXCLUDED | Caller-owned result model returns the ordinary release failure *and* attaches it; does not fit the accumulator's return contract (stale docstring corrected) |
| `npm_environment.execution._cleanup_container` / `_remove_staging_safely` | EXCLUDED | Domain-owned redaction/result model already converts every failure (including interruption) into a structured `CleanupFailure`; excluded per design |
| Evidence/user outputs, activity monitor, streaming, metadata cache storage | EXCLUDED | Different timeout/cancellation/redaction/result-precedence models; metadata remains owned by `revalidate-update-metadata` |

## npm staging-tree cleanup (`_atomic_publish`)

9A.5/9A.9 require the npm-owned staging/tree cleanup to be observable and
primary-preserving. `_atomic_publish` previously removed its owned temporary
publication directory with `shutil.rmtree(..., ignore_errors=True)` and made it
writable with a broad `except OSError: continue`/`pass`, so a permission, I/O,
or interruption failure during cleanup was silently lost.

Hardened contract:

- **Idempotent absence** — the *only* suppressed outcome is `FileNotFoundError`
  for the owned temporary tree, meaning it is already gone. `_remove_owned_tree`
  swallows exactly that; every other `OSError` propagates. `_restore_write_access`
  likewise treats `FileNotFoundError` from `lstat` as absence and lets
  permission/I/O failures from `lstat` or `chmod` propagate.
- **Independent, exactly-once actions** — making the outer temporary directory
  writable, restoring each manifest entry's owner write bits, restoring the tree
  root, and removing the tree are separate `CleanupFailures.run` actions. One
  failure never aborts the remaining actions, and each runs exactly once.
- **Failure precedence** — the accumulator is constructed with the in-flight
  publication failure as its primary. Therefore:
  - an ordinary cleanup `OSError` is attached to the publication failure as a
    secondary diagnostic and the publication failure remains authoritative;
  - an unexpected cleanup `Exception` (not a declared ordinary `OSError`) is
    raised, with the publication failure preserved on it as secondary;
  - a publication or cleanup process-control interruption (`KeyboardInterrupt`,
    cancellation) stays authoritative, with the other failures attached as
    secondary.
- **Ownership** — only the declared temporary tree and its descendants are ever
  removed (`_remove_owned_tree(tmp)`); the committed `final` output is never a
  cleanup target. `_remove_redundant_tree` (the verified-collision path) uses
  `CleanupFailures(None)` and re-raises the first ordinary failure for the
  caller's `collision_cleanup_failed` mapping.
- **`_make_writable`** no longer swallows arbitrary `OSError`s; each entry is an
  independent accumulator action so one unremovable entry does not stop the
  rest, and non-absence failures stay observable.

Tests: `tests.test_npm_environment_phase9a_staging_cleanup` (19) cover
publication-failure preservation, idempotent absence, chmod/lstat/rmtree
observability, unexpected-defect and interruption precedence, action
independence, and exactly-once/unrelated-path invariants.

## Final review-pass migrations

The Phase 9A review (and a post-review audit of every remaining
`attach_secondary` call) migrated the residual cleanup sites that either
sequenced more than one independent action or were named shared contracts:

- `locking` lock-probe close after a failed `fstat`.
- `regular.validated_read`, `regular.durable_unlink`, and
  `regular._validate_destination` close sites (named regular-file contracts) —
  all now map the unopposed close failure to the close-stage
  `TransactionError` from the accumulator's returned ordinary failure.
- `artifact_cache.FileIdentityLock.acquire` (caller-owned descriptor +
  adopted directory, both attempted exactly once).
- `npm_environment.publication` identity-lock acquisition directory close.
- `build_cache.BuildStorage._open`, `BuildStorage.__enter__`,
  `_BuildStorageHandle.__exit__`, constructor-lock acquisition, and
  `_open_algorithm_directory`.
- `build_cleanup._close_leaf`, `_open_algorithm_directory`, and
  `_close_algorithm`.

`attach_secondary` remains the sanctioned primitive only where it is not a
cleanup accumulator:

- carrying diagnostics across a domain re-map (`attach_secondary(cause,
  list(exc.secondary))`) in `artifact_cache`, `build_cache`, `effective`,
  `npm_environment.publication`, and `rendering`; and
- `locking._release_descriptor`'s final attachment of the already-`reported`
  later ordinary failures and `effective`'s `__cause__` carry-over.

`build_orchestration._release_build_lock` stays explicit (see table): it
returns the ordinary release failure as an operational result even when it
attaches that same failure to a primary, which the accumulator's
`complete()` contract deliberately does not express.

### Uniform terminal handling

Every migrated site that owns its own result model now consumes the
accumulator's terminal value with the same guarded form::

    result = failures.complete()
    if result is not None:
        raise result

This replaced an earlier bare `failures.complete()`/`accumulator.complete()`
idiom at the sites whose primary was only *locally* provably non-`None`
(every `except ... as exc` handler, every guarded `__exit__`, and the
`_discard` caller contract). The guarded form is behaviorally identical there
- `complete()` raises an interruption/unexpected defect or returns `None` when
an original `Exception` primary is present - but it no longer silently drops a
returned ordinary failure if a future edit ever makes the primary optional.

The one remaining deliberate bare call is `build_cleanup.cleanup_superseded`,
whose `close_failures.complete()` result is intentionally not raised: when no
primary is in flight the full `ordinary_failures` list is aggregated into the
domain `CleanupFailure` report, so raising only the first would drop the rest.

### Explicit cleanup primary (no ambient capture)

Every migrated `finally` scope originally constructed the accumulator from
`CleanupFailures(sys.exc_info()[1])`. Inside a `finally` that is ambient: it
returns the caller's currently-handled exception when the protected operation
*succeeded*, so a successful operation plus a failed close attached the close
failure to an unrelated caller exception (and `complete()` then returned
`None`, silently dropping it), and a successful operation inside an
interruption handler adopted the caller's `KeyboardInterrupt` as its primary.

Every such scope now tracks the operation's own exception explicitly:

```python
primary: BaseException | None = None
try:
    perform_operation()
except BaseException as exc:
    primary = exc
    raise
finally:
    failures = CleanupFailures(primary)
    ...
```

`except BaseException` is deliberate: interruptions retain their established
precedence. A scope whose owner already has an `except BaseException` handler
(`_atomic_publish`, `open_build_cache_state`, `publish_verified_blob`,
`_bootstrap_constructor_project_lock_parent`, `validate_project_state`,
`create_artifact_snapshot`, `materialize_artifact`, `execute_build`) sets the
tracked primary from the exception that actually propagates, while the nested
handler continues to pass its own caught exception directly. Nested scopes
(`inspect_verified_blob_readonly`, `_ensure_dir_private`, `read_bytes`,
`inspect_and_digest`, `append_temp`, `finalize_temp`, `set_permissions`) each
get their own tracked primary, so an inner close failure stays the primary that
the outer close attaches to.

Sites that were already inside a genuine `except BaseException` handler now
bind the name (`except BaseException as exc:`) and pass `exc` directly. In
`build_snapshot.create_artifact_snapshot`, `build_orchestration.execute_build`,
and `build_materialization.materialize_artifact` the tracked primary is kept in
a variable distinct from the handler's own binding (Python deletes the
`except ... as` name before the `finally` runs).

After this change no `sys.exc_info()` remains anywhere under `docker/` (the
only remaining match is the `*exc_info` parameter of an `__exit__` signature in
`npm_environment.observability`). The six modules that no longer referenced
`sys` had the import removed: `npm_environment.publication`,
`versioning.artifact_cache`, `versioning.build_snapshot`,
`versioning.project_state`, `versioning.build_materialization`, and
`versioning.build_orchestration`.

Regression coverage: `tests/test_transactions_phase9a_ambient_primary.py`
(14 tests) invokes `publication._fsync_dir`, `publication._durable_write`,
`project_state._read_metadata`, `build_cache._open_namespace_fd`,
`build_snapshot._validate_hard_link_destination`,
`materialize_artifact`, and the nested `inspect_verified_blob_readonly` inside
an unrelated caller `except` block, asserting the four contract behaviors
(close failure raised, caller untouched, success/interruption returns normally,
local primary preserved with ordinary failures attached). Reverting a site to
`sys.exc_info()[1]` makes the corresponding test fail.

## build_cache publication and state-open release

Two `docker/versioning/build_cache.py` release paths were hardened so a failed
release is attempted exactly once and never hides or skips an independent
action:

- `publish_verified_blob` failure path.  The temporary payload descriptor,
  the temporary entry, and every descriptor opened for the publication are now
  released through **one** accumulator.  Ownership of the temporary descriptor
  is dropped (`descriptor, fd = fd, None`) *before* its close, so a close that
  raises an interruption or unexpected defect can no longer (a) skip the
  `os.unlink` of the temporary entry or (b) cause the `finally` to close the
  same descriptor a second time.  The previous early `complete()` and
  `except OSError: pass` are gone; the unlink runs through the domain helper
  `_unlink_absent_ok`, which suppresses only `FileNotFoundError` (declared
  idempotent absence) and lets permission/I/O failures surface as ordinary
  cleanup failures attached to the in-flight publication failure.  A separate
  `temporary_owned` flag is set only after the exclusive
  (`O_CREAT | O_EXCL`) create succeeds and cleared once `os.replace` moves the
  entry to the destination, so a name collision never makes this publication
  own — and therefore never makes it unlink or modify — a pre-existing entry.
  A failed removal is never retried because ownership is dropped before the
  action.

## project_state validation release

`docker/versioning/project_state.py::validate_project_state` now tracks a
single local primary across every release and owns the namespace descriptor
until the validated state can actually be returned:

- The `FileNotFoundError` handler maps the missing ancestor to a
  `ProjectStateError` and retains it as the local primary before any release.
  An ordinary ancestor-close failure is therefore attached as a secondary
  diagnostic instead of replacing the domain error (previously the primary was
  never set, so the first ordinary close failure was raised in its place).
- The failed-validation path releases the namespace and every opened ancestor
  through **one** accumulator, attempting each action exactly once before
  completing.  A namespace-close interruption or unexpected defect stays
  authoritative over later ancestor releases rather than being lost behind a
  stale primary, and the displaced domain error remains a secondary diagnostic.
- The success path releases the ancestor descriptors first and transfers the
  namespace descriptor to the returned `ValidatedProjectState` only when that
  release succeeds.  If an ancestor release prevents the return — ordinary
  error, unexpected defect, or process-control interruption — the retained
  namespace descriptor is released exactly once before the failure propagates.
  Previously a failed ancestor close prevented the return and leaked the
  namespace descriptor.  Every descriptor's ownership is cleared before its
  close attempt, so a failed close is never retried.

Fault-injection coverage: `tests/test_transactions_phase9a_project_state_cleanup.py`
(7 tests) records descriptor opens by position and injects per-position close
failures/interruptions.  It covers: a missing namespace or missing `projects`
directory preserving the `ProjectStateError` with the close failure secondary;
a successful validation whose ancestor close fails still releasing the
namespace exactly once; a successful validation retaining the namespace when
ancestors close cleanly (and releasing it exactly once on `close`); a namespace
interruption staying authoritative over later ancestor cleanup; the first
namespace interruption preceding a later root interruption; and every
independent release on a failed-validation path being attempted exactly once.
Running the module against the pre-fix implementation fails 4 of the 7 cases.
- `open_build_cache_state`.  On the failed-open path the already-opened
  children (reverse order) and the namespace descriptor are released through a
  single accumulator, so a child release that raises an interruption or
  unexpected defect is authoritative over the original open failure and over
  later ordinary namespace release failures (previously the namespace close
  saw a stale primary).  On the success path, if the namespace close fails for
  **any** reason — ordinary error, unexpected defect, or interruption — every
  retained child is still released exactly once before the failure propagates.
  Previously a failed namespace close leaked all five child descriptors.

Fault-injection coverage: `tests/test_transactions_phase9a_build_cache_cleanup.py`
(10 tests) uses fd- and path-keyed close faults plus injected `os.replace`
failures and a forced temporary-name collision, and asserts exception identity,
secondary diagnostics, post-fault call windows, and exact close/unlink counts.
It covers: close interruption and unexpected close defect still unlinking (and
releasing every other descriptor once); the failed close never retried;
permission and I/O unlink failures remaining observable; declared temporary
absence remaining harmless; a temporary-name collision leaving the pre-existing
entry's contents and inode untouched while releasing every opened descriptor;
namespace-close failure (ordinary and interruption) releasing every retained
child; and a child-release interruption staying authoritative over a later
ordinary namespace release failure.  Running the module against the pre-fix
implementation fails 9 of the 10 cases.

## Specialized L3 fault injection (9A.5, 9A.9)

- `_remove_owned_leaf` in `build_materialization` treats `FileNotFoundError`
  as idempotent absence and raises every other failure.
- Failed verification and uncommitted-blob cleanup attach a non-absence unlink
  failure to the primary instead of swallowing it; the final temporary-cleanup
  action cannot mask an in-flight primary.
- `build_snapshot` construction, invalid-hard-link removal, copy-fallback, and
  descriptor closes all run through domain-owned actions plus the accumulator;
  only an owned temp/destination action declares `FileNotFoundError` as
  idempotent absence.
- The accumulator never suppresses `FileNotFoundError` itself
  (`AccumulatorIdempotentAbsenceBoundaryTests`).

### `create_artifact_snapshot` staging-open and cleanup precedence

`docker/versioning/build_snapshot.py::create_artifact_snapshot` no longer uses
`except BaseException: pass` when the staging directory cannot be adopted, and
its construction cleanup no longer lets a later descriptor close replace the
first authoritative cleanup failure:

- When the staging-directory open fails, the original `OSError` is retained as
  the `SnapshotError` cause and the tree removal runs through the accumulator
  with the domain error as primary.  An ordinary removal failure becomes a
  secondary diagnostic; an unexpected cleanup defect or interruption remains
  authoritative with the domain error (and its cause) preserved as secondary.
  Previously these cleanup failures were discarded entirely.
- Construction cleanup's ordinary-failure policy is narrowed from
  `(Exception,)` to the cleanup function's declared contract
  `(OSError, SnapshotError)`.  A `RuntimeError` or other programmer defect now
  receives unexpected-defect precedence instead of silently becoming a
  secondary diagnostic behind the construction failure.
- An exception raised by `CleanupFailures.complete()` is captured, assigned to
  `snapshot_primary`, and re-raised, so the `finally` releases `staging_fd`
  under the authoritative failure.  The first tree-cleanup interruption stays
  primary over a later staging-close interruption (the close becomes a
  secondary diagnostic) instead of being lost behind a stale primary, and the
  staging descriptor is still closed exactly once.

Fault-injection coverage lives in `tests/test_transactions_phase9a_specialized.py`
(`SnapshotStagingOpenCleanupTests` and `SnapshotCleanupPrecedenceTests`,
6 tests) and injects staging open/close faults plus patched construction and
cleanup failures.  It pins: ordinary cleanup failure secondary to the domain
error with the open error retained as cause; unexpected defect and interruption
remaining authoritative; a programmer defect in cleanup becoming authoritative
while the construction failure is secondary; the first tree-cleanup
interruption preceding a staging-close interruption; and cleanup plus close
ordinary failures both staying secondary to the construction failure with the
staging descriptor closed exactly once.  Running the module against the pre-fix
implementation fails 5 of the 6 cases.

### `cleanup_artifact_snapshot` permission-recovery branch

`docker/versioning/build_snapshot.py::cleanup_artifact_snapshot` no longer
silently discards the recovery work it performs after a readonly snapshot tree
makes the initial removal fail with `PermissionError`:

- The per-directory `except OSError: pass` suppressions and the final
  `shutil.rmtree(..., ignore_errors=True)` are gone.  Recovery is split into
  domain-owned helpers that each ignore only `FileNotFoundError`:
  `_collect_snapshot_directories` (enumeration),
  `_restore_directory_permissions` (directory/root `chmod 0o700`), and
  `_remove_readonly_snapshot_tree` (final removal).  Every action runs as an
  independent `CleanupFailures` action, so an unexpected recovery defect or
  interruption cannot skip the remaining actions.
- Directory enumeration is itself a protected accumulator action that appends
  into a caller-owned list as entries are discovered.  A failure from
  `rglob()` (e.g. a permission error) no longer bypasses root restoration and
  final removal or loses the initial permission diagnostic: the directories
  discovered before the failure remain available, the root restoration and
  final removal still run, and only then is `complete()` called.
- The recovery accumulator's primary is the initial `PermissionError`.  When
  every restoration and the removal succeed the function returns normally; any
  ordinary recovery failure is attached to the initial permission failure as a
  secondary diagnostic and surfaced as a `SnapshotError` whose `__cause__` is
  that raw permission failure, so raw failures are never replaced by only a
  generic message.  An unexpected defect or interruption is authoritative
  (raised unchanged) with the initial permission failure attached as secondary.
- Permissions are restored only on snapshot directories, never on hard-linked
  payload files, so a cached blob's 0444 inode and mode are untouched.
- `FileNotFoundError` remains the only idempotent-absence case, declared in the
  initial attempt, in the enumeration/restoration helpers, and in the
  `_remove_readonly_snapshot_tree` final-removal helper.  A snapshot root or a
  discovered child that vanishes before its `chmod` (a concurrent cleanup
  race) is therefore an accepted outcome rather than a `SnapshotError` when the
  final removal also confirms absence.  Any other initial removal failure
  (e.g. `EIO`) propagates raw without triggering directory recovery.

Fault-injection coverage lives in `tests/test_constructor_build_snapshot.py`
(`SnapshotCleanupRecoveryFaultTests`, 16 tests) and calls the real cleanup on a
real readonly tree so the recovery branch actually runs, injecting
per-directory chmod `PermissionError`/`EIO`, unexpected `RuntimeError`,
`KeyboardInterrupt`, final-removal failures, and enumeration
absence/defect/interruption.  It pins ordinary-failure observability after
later actions succeed, continuation after defects and interruptions,
unexpected/interruption precedence, retained partial enumeration, multiple
retained recovery failures, vanished root/child idempotent absence, raw
non-permission propagation, and unchanged hard-linked blob mode/inode.  Running
the module against the previous implementation fails 7 of the 10 earlier cases
and all 6 enumeration/vanish cases.

## Intentional behavior changes (recorded, tests updated)

1. `tests/test_constructor_materialization.py::test_system_exit_after_temp_creation`:
   a `SystemExit` raised by cleanup with no primary now propagates unchanged
   (the independent lock release still runs). Previously it was wrapped as an
   ordinary `ArtifactMaterializationError`.
2. `tests/test_transactions_l1_capabilities.py::test_leaf_close_interruption_propagates_unchanged`:
   the original parent-close interruption is now authoritative over a later
   leaf-close interruption, which becomes a secondary diagnostic.
3. `tests/test_transactions_phase9_boundaries.py::test_snapshot_and_confinement_remain_domain_owned`:
   refined to allow the internal cleanup accumulator in `build_snapshot.py`
   while still forbidding adoption of `PosixFileOps`, `DirectoryCapability`, or
   `RegularFileContracts`; confinement stays free of `docker.transactions`.
4. `docker/versioning/build_snapshot.py::create_artifact_snapshot` staging-open
   failure: cleanup errors are no longer discarded (`except BaseException:
   pass` removed).  An ordinary cleanup failure is now secondary to the
   `SnapshotError`, and an unexpected cleanup defect or interruption is
   authoritative over it.  Construction cleanup also narrows its ordinary
   policy to `(OSError, SnapshotError)`, so a programmer defect in cleanup is
   authoritative over the construction failure, and the staging descriptor
   close now runs under the authoritative cleanup primary rather than a stale
   one.
5. `docker/versioning/build_snapshot.py::cleanup_artifact_snapshot` permission
   recovery: a recovery failure that previously vanished behind
   `ignore_errors=True` (and a generic `SnapshotError` with no cause) is now
   observable.  Success is still reported when every restoration and the
   removal actually succeed, but a non-fatal chmod failure is preserved as a
   secondary diagnostic and the final removal failure surfaces as a
   `SnapshotError` whose cause is the initial `PermissionError`; unexpected
   recovery defects and interruptions are authoritative.  Directory
   enumeration is now a protected accumulator action whose partial results are
   retained, and a snapshot root or discovered child that vanishes before its
   `chmod` is treated as idempotent absence instead of a `SnapshotError`.  No
   test asserted the old generic message.

## Downstream metadata obligation (9A.10)

`openspec/changes/revalidate-update-metadata/tasks.md` already requires the
shared accumulator for metadata-owned descriptor cleanup (tasks 1.0 and 1.2)
while retaining metadata-owned schema, path/removal, idempotent-absence, and
domain error-mapping authority. No metadata production code was modified.

## Review (9A.11)

Reviewed every migrated call site for independent-action validity,
deterministic ordering, accidental retry, duplicate attachment, swallowed
programmer defects, interruption conversion, broad `FileNotFoundError`
suppression, domain/path/deletion leakage, and changed exception identity.

Findings and resolutions:

1. `locking._release_descriptor` and `regular._discard` let a later cleanup
   interruption / unexpected defect displace an original primary and could skip
   an owned temporary unlink. Resolved by the accumulator and pinned by
   regressions.
2. `capabilities.from_secure_path` documented the pre-9A "newer interruption
   wins" behavior. Resolved so the original interruption stays authoritative.
3. `artifact_cache` wrapped cleanup `SystemExit` as an ordinary publication
   error. Resolved so process-control interruptions propagate unchanged.
4. `build_materialization` and `build_snapshot` silently swallowed non-absence
   unlink/close failures. Resolved through domain-owned actions plus the
   accumulator; only declared idempotent absence is suppressed. The
   `create_artifact_snapshot` staging-directory and validated-source descriptor
   closes were an in-scope 9A.9 miss (raw `finally: os.close(...)`), found by a
   post-review AST sweep and migrated so a close failure is attached to the
   in-flight construction primary instead of masking it.
5. No duplicate attachment, retry, path derivation, deletion authority, or
   domain-mapping authority was introduced into the accumulator; it exposes no
   `unlink`/`remove_tree`/`close`/rollback operation.
6. npm process lifecycle/streaming, activity-monitor policy, user/presentation
   outputs, and existing metadata cache storage remain excluded.

## Residual findings — raw `finally` closes outside the 9A site list

A post-review AST sweep for bare `close()`/`unlink()`/`rmtree()` calls directly
in `finally` bodies (excluding nested accumulator actions) found the same
primary-masking shape at sites that were **not** part of the task 9A.4/9A.8
audited list. The two inside an explicitly 9A.9-scoped function
(`create_artifact_snapshot`, "snapshot construction") were genuine misses and
are migrated (see the audit table and review finding 4).

The remaining hits are pre-existing resource-lifetime closes in modules the 9A
audit did not classify. A follow-up exhaustive close sweep
(`verification-phase9a-close-audit.md`) migrated every such site in modules that
already depend on `docker.transactions` — including the `build_cache`
validate/open helpers, the `artifact_cache` dry-run verifiers,
`project_state._read_metadata`, and `npm_environment.publication`
`_fsync_dir`/`_fsync_tree` — and this change additionally hardened the
`_atomic_publish` staging-tree cleanup (see the section above). No raw
`finally`-close remains in a module that may import the accumulator.

The sites that stay raw are: deliberate best-effort swallows
(`read_index`/`_append_index`/`_read_private_regular`), sole-failure
success/absence-path closes, L0 primitives, and modules that may not import
`docker.transactions.cleanup` without introducing a new L0-L2 dependency
(`npm_environment` `storage`/`tree`/`execution`/`lifecycle`,
`versioning.cache_storage` (enforced import allowlist), `runtime_installer`,
`build_context_confinement`, `host_presentation`). Covering those requires
relocating the accumulator to a lightweight module — a separate architecture
decision, not part of this change.

## Validation (9A.12)

Accumulator and migrated-phase focused selection:

```text
python -m unittest \
  tests.test_transactions_phase9a_accumulator \
  tests.test_transactions_phase9a_audit \
  tests.test_transactions_phase9a_specialized \
  tests.test_transactions_phase9a_ambient_primary \
  tests.test_transactions_phase9a_descriptor_handoff \
  tests.test_transactions_phase9a_build_cache_cleanup \
  tests.test_transactions_phase9a_project_state_cleanup \
  tests.test_npm_environment_phase9a_staging_cleanup \
  tests.test_transactions_phase9_boundaries \
  tests.test_transactions_phase9_consumer_inventory \
  tests.test_transactions_l0_posix tests.test_transactions_l1_capabilities \
  tests.test_transactions_l2_atomic tests.test_transactions_l2_durable \
  tests.test_transactions_l2_lifecycle tests.test_transactions_locking \
  tests.test_transactions_phase6_project_metadata \
  tests.test_transactions_phase6_effective_projection \
  tests.test_transactions_phase7_runtime_lock \
  tests.test_transactions_phase7_runtime_projection \
  tests.test_transactions_phase7_runtime_facade \
  tests.test_transactions_phase7_artifact_specialization \
  tests.test_transactions_phase8_npm_lock \
  tests.test_transactions_phase8_npm_specialization \
  tests.test_constructor_materialization \
  tests.test_constructor_build_materialization \
  tests.test_constructor_build_snapshot tests.test_constructor_pi_snapshot \
  tests.test_constructor_build_cleanup \
  tests.test_constructor_build_generation_integration \
  tests.test_constructor_build_orchestration \
  tests.test_npm_environment_cleanup \
  tests.test_npm_environment_publication_cleanup
Ran 839 tests in 12.210s
OK (skipped=2)
```

Complete repository checks:

```text
ty check docker --python-version 3.14 --output-format concise
All checks passed!

python -m unittest discover -s tests -p 'test_*.py'
Ran 5223 tests in 100.915s
OK (skipped=13)

git diff --check
(clean)
```

Migrated sites, excluded sites, corrected masking/loss cases, and the exact
idempotent-absence decisions are recorded above.

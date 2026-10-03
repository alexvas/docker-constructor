# Phase 5 Verification — Build-Cache and Orchestration Integration

Date: 2026-10-03T09:41:35Z
Change: `add-durable-filesystem-transactions`
Phase: 5 (Build-Cache and Orchestration Integration)
Depends on: Phase 4 (`Add durable build cleanup and recovery`, commit `7515055`)

## Scope delivered

The mutable `committed-build.json` build cache is replaced with the Phase 3
immutable generation protocol, the Phase 4 sequential cleanup/recovery is
connected into orchestration, the checkout build lock is migrated to the
shared fail-fast capability, and post-commit/recovery failures map to
`ExitKind.OPERATIONAL` (process exit code `4`). Marker retention, the fixed
30-day uncommitted TTL, snapshot recovery, content-addressed blob
verification/publication, and shared-XDG isolation are retained as
specialized build-domain code.

Production changes:

- `docker/versioning/build_cache.py`
  - `ConstructorProjectBuildLock` now wraps the shared
    `LockCapability` acquired through `acquire_build_generation_lock` over the
    `build-artifacts` generation directory, plus a retained
    `DirectoryCapability`. It keeps `assert_held_for`, `namespace`,
    `cache_root`, `project_state`, `__enter__`/`__exit__`, and adds `ops`,
    `capability`, `generation_directory`, and `open_storage()`.
  - `acquire_constructor_project_build_lock` delegates acquisition to the
    shared layer: `LockContention` maps to the unchanged
    `"constructor project already has an active build"` diagnostic and any
    other `LockError` to `"unsafe constructor-project build lock"`,
    preserving the raw cause and chaining.
  - `commit_build_set` verifies every committed blob, publishes the next
    immutable generation (`publish_generation`), reconciles
    authoritative-generation markers (`reconcile_authoritative_markers`), and
    runs `cleanup_superseded`, in that order.
  - `recover_build_generations` runs `build_cleanup.recover_generations`
    (discovery + generation-directory `fsync` + marker reconciliation +
    predecessor cleanup) under the lock.
  - `_read_live_set` (legacy manifest read) is removed;
    `_read_authoritative_blobs` reads the authoritative generation through
    `discover_generations`. `_MANIFEST_NAME` is removed.
  - `release()` unwraps a shared `LockError` release failure back to its raw
    `OSError` cause so a release failure keeps its previous exception type.
- `docker/versioning/build_orchestration.py`
  - `recover_build_generations` is invoked immediately after lock acquisition
    and before `recover_abandoned_snapshots`, artifact materialization,
    snapshot creation, and Docker execution.
- `docker/versioning/build_generations.py`, `docker/versioning/build_cleanup.py`
  were consumed unchanged.

No shared (`docker/transactions/`) layer was modified; no specification file
was changed except task checkboxes.

## RED (tasks 5.1–5.5)

New coverage:

- `tests/test_constructor_build_generation_integration.py`
  (`BuildLockParityTests`, `BuildCacheParityTests`, `PostCommitFailureTests`,
  `ExitCodeMappingTests`): 18 tests.
- `tests/test_constructor_build_orchestration.py::TestGenerationRecoveryBoundary`:
  4 tests.

These pin the migrated behaviors: shared fail-fast lock parity, competing
build rejection before mutation, canonical project/cache binding, release on
every outcome, marker/TTL/snapshot/XDG parity, recovery-before-side-effect
ordering, marker-directory `fsync` on an all-markers-absent restart, result
mapping, no image/generation rollback after a post-commit failure, and
snapshot-cleanup-failure TTL retention.

Existing tests that asserted the removed mutable manifest were migrated to
the immutable-generation representation (not weakened):
`tests/test_constructor_build_transactions.py`,
`tests/test_constructor_build_persistence.py`,
`tests/test_constructor_build_orchestration.py`.

## GREEN (tasks 5.6–5.10)

- 5.6 Checkout build lock migrated to the shared `FAIL_FAST` capability over
  the unchanged `build.lock` path/namespace, preserving the existing
  diagnostics.
- 5.7 Build control (generation/manifest) reads, replacements, and unlinks
  route through the L2/L3 adapters in `build_generations`/`build_cleanup`
  (`validated_read`, `durable_no_clobber`, `durable_unlink`).  Marker
  control-file reads and durable replacements route through the dedicated
  build-domain adapters `_read_marker_json`/`_replace_marker_json`, which
  delegate the complete mechanics to the shared
  `docker.transactions.regular.RegularFileContracts` (`validated_read`,
  `durable_replace`) while marker naming, timestamp validation, JSON
  interpretation, and retention decisions stay in the build domain.
- 5.8 `commit_build_set` publishes an immutable generation, reconciles
  authoritative markers, then runs normal superseded cleanup; a successful
  build returns to exactly one stable generation with no marker for any
  committed blob.
- 5.9 `recover_build_generations` is connected before superseded cleanup and
  every build side effect; incomplete reconciliation/cleanup maps to
  `ExitKind.OPERATIONAL` (exit `4`) through the existing `BuildCacheError`
  handler (`BuildGenerationError`/`BuildCleanupError` subclass it).
- 5.10 The local `fcntl` lock, the duplicate legacy manifest control helpers
  (`_MANIFEST_NAME`, `_read_live_set`), and the duplicate marker control-file
  helpers (`_atomic_json_at`, `_read_json_no_follow_at`,
  `_validate_control_destination`) are removed; specialized blob, TTL,
  snapshot, and lock-parent-bootstrap code, and `_require_control_name`
  basename validation are retained.

## INTROSPECT (task 5.11)

1. **Cleanup before commit** — `commit_build_set` publishes the generation
   before any marker/blob deletion; blob verification precedes publication.
   No finding.
2. **Image rollback claims** — a post-commit reconciliation/cleanup failure
   returns `OPERATIONAL` with the successful `process_result`; the newest
   generation and image are preserved. No finding.
3. **Hidden legacy adoption** — `_read_authoritative_blobs` uses
   `discover_generations`, which skips `committed-build.json`; the legacy name
   is neither read nor deleted. No finding.
4. **Widened deletion authority** — candidates remain exactly canonical
   `previous - current`; current-generation identities are never deleted.
   No finding.
5. **Changed dry-run effects** — recovery is inside the confirmed execution
   path only; dry-run behavior is unchanged (dry-run suite green).
   No finding.
6. **TTL drift** — `UNCOMMITTED_TTL_SECONDS` and the TTL boundary logic are
   unchanged; only the authoritative-live-set source changed. No finding.
7. **Snapshot ordering** — generation recovery now runs before snapshot
   recovery; both are independent and fail closed. No finding.
8. **Blob-publisher migration** — `publish_verified_blob` is unchanged
   (content-addressed L3 protocol). No finding.
9. **L2 exception leakage** — build-domain errors wrap `TransactionError`; a
   `CapabilityError` from storage opening is a `ValueError` and is caught by
   orchestration. No finding.
10. **Interruption conversion** — `commit_build_set`, recovery, and cleanup
    do not catch `BaseException`; `KeyboardInterrupt`/cancellation propagate
    unchanged and the lock is released. No finding.
11. **Release masking (finding and fix)** — the shared
    `LockCapability.close()` wraps a descriptor close failure in a
    `LockError`. `ConstructorProjectBuildLock.release()` originally let that
    `LockError` escape, changing the previous raw-`OSError` release failure.
    It now preserves the L2 failure's raw `OSError` cause as the primary
    failure and attaches a directory-close failure as secondary.
    `BuildLockParityTests.test_release_failure_preserves_raw_oserror` pins it.
12. **Context-manager release masking (finding and fix)** —
    `ConstructorProjectBuildLock.__exit__` previously discarded the active body
    exception and called `release()` without primary-error handling, allowing an
    ordinary unlock/close failure to replace even `KeyboardInterrupt`.  It now
    always attempts release, attaches an ordinary release error to the active
    body exception, and returns normally so that exact primary propagates.
    Without a body exception the release error propagates.  A process-control
    interruption raised by release remains uncaught.  When `release()` unwraps
    a shared `LockError` to its raw `OSError` cause, it now transfers every
    `LockError.secondary` entry before the generation-directory close is
    attempted, so simultaneous unlock, lock-close, and directory-close failures
    remain observable.  Released state is set before cleanup, preserving
    exactly-once attempts and harmless repeated `release()` calls.

`BuildLockParityTests` adds four combined lifecycle regressions: body
`RuntimeError` plus unlock failure, body `KeyboardInterrupt` plus ordinary
unlock failure, simultaneous unlock/lock-close/generation-directory-close
failures, and release failure without a body exception.  They assert primary
identity, transferred secondary identity, exactly-once cleanup, and idempotent
repeat release.

## Mutation checks

- Removing the `recover_build_generations(...)` call from orchestration fails
  exactly the 2 recovery-ordering/post-commit tests.
- Removing the `reconcile_authoritative_markers(...)` call from
  `commit_build_set` fails exactly the 3 marker-lifecycle/post-commit tests.
- Both modules were restored byte-for-byte after each mutation.

## VALIDATE (task 5.12)

- Focused Phase 5 suites:
  `tests.test_constructor_build_generation_integration` plus
  `TestGenerationRecoveryBoundary` — **48 tests, OK**.
- Build integration suites
  (`test_constructor_build_transactions`, `test_constructor_build_persistence`,
  `test_constructor_build_orchestration`,
  `test_constructor_build_generation_integration`) — **218 tests, OK**.
- Complete repository suite — **4866 tests, OK (skipped=13)**, exit code `0`.
- `ty check docker --python-version 3.14 --output-format concise` —
  **All checks passed!**
- `openspec validate add-durable-filesystem-transactions --strict` — valid.
- `git diff --check` — clean.

Recorded outcomes across first build, changed build, recovery, partial
deletion, post-commit failure, corrupt generation state, ignored legacy
manifest names, and excess generations:

- **First build** — generation `...0001` published, committed blobs have no
  markers, no legacy manifest is created.
- **Changed build** — generation `...0002` published, `previous - current`
  blobs and markers durably removed, predecessor manifest unlinked; exactly
  one stable generation remains.
- **Recovery** — two-generation state discovered under the lock; marker
  reconciliation then idempotent cleanup completes to one stable generation;
  failure preserves the predecessor and blocks materialization/snapshot/
  Docker.
- **Partial deletion** — every candidate attempted, failures aggregated,
  predecessor retained, `OPERATIONAL`/`4`.
- **Post-commit failure** — image and newest generation preserved,
  `OPERATIONAL`/`4`, no rollback.
- **Corrupt/excess generation state** — fail closed with no deletion.
- **Ignored legacy manifest name** — `committed-build.json` neither inspected
  nor deleted.
- **Snapshot-cleanup failure** — no generation published; newly materialized
  blobs retain uncommitted markers and remain subject to the fixed 30-day
  TTL.

## Post-completion hardening pass

Two defects found by post-completion review were fixed and pinned with
fault-injection tests. No specification or shared-layer file changed.

### Storage-lifecycle descriptor leaks (`_BuildStorageHandle`)

- `_open()` now closes the still caller-owned descriptor when
  `DirectoryCapability.from_fd()` rejects it, attaching any close failure as a
  secondary diagnostic without replacing the adoption failure.
- `__enter__()` closes an already-adopted `blobs` capability when opening the
  marker view fails, keeping the opening failure primary.
- `__exit__()` no longer raises on the first close failure: both marker and
  blob capabilities are always attempted. With an active exception all close
  failures are attached to it; otherwise the first close failure is raised
  with the later ones attached.

New `StorageLifecycleTests` fault-injection coverage demonstrates blob-fd
adoption failure, marker adoption and marker-open failure after blobs opened,
marker close failure still closing blobs, and multiple close failures
preserving the primary with the remainder attached — each asserting the open
file-descriptor count returns to its pre-call value.

### Legacy lock-acquisition diagnostics

`acquire_constructor_project_build_lock()` no longer converts every shared
`LockError` into `BuildTransactionError("unsafe constructor-project build
lock")`. `_raise_lock_failure()` inspects the lock entry at the pathname
(`follow_symlinks=False`) and raises the unsafe-build diagnostic only for a
symlink, non-regular, foreign-owned, or multiply-linked entry (or an
indistinguishable in-place replacement). Operational open, stat, and
mode-repair failures re-raise the underlying `OSError`, carrying over any
attached cleanup failures. Contention still maps to
`"constructor project already has an active build"`.

New `LockDiagnosticParityTests` inject lock-entry open, stat, and mode-repair
failures and assert the raw `OSError`/errno is preserved; symlink, directory,
FIFO, foreign-owned, and multiply-linked entries still produce the unsafe
lock diagnostic; contention still produces the active-build diagnostic; and
an injected directory-close failure is attached as secondary without
replacing the primary acquisition failure.

### Mutation checks for the hardening pass

- Removing the `_open()` cleanup fails exactly 2 storage tests.
- Removing the `__enter__()` cleanup fails exactly 2 storage tests.
- Restoring the immediate-raise `__exit__()` fails exactly 2 storage tests.
- Restoring the blanket lock conversion fails exactly 4 lock tests.
- Removing the lock-entry inspection fails exactly the 2 symlink/directory
tests.
- `build_cache.py` was restored byte-for-byte after each mutation (`diff -q`).

Tests added this pass: **15** (10 lock diagnostic parity, 5 storage
lifecycle). Full suite **4848 OK (skipped=13)**; `ty` clean; OpenSpec strict
validation valid; `git diff --check` clean.

## Post-completion hardening pass: build-lock release masking

### Issue

`execute_build()` called `lock.release()` unprotected in three cleanup
paths. A release failure could therefore replace an active
`KeyboardInterrupt`/unexpected build failure (or a returned `BuildResult`)
instead of being reported alongside it.

### Fix (`docker/versioning/build_orchestration.py`)

- Added `_release_build_lock(lock, primary=None)`, which attempts
  `lock.release()` exactly once. With a primary exception it attaches an
  ordinary release failure as a secondary diagnostic via `attach_secondary`
  and returns it; with no primary exception it returns the release failure so
  the caller can surface it without letting it escape.
- **Ordinary release failures vs. process-control interruptions.** Only
  ordinary `Exception` release failures are captured. A `BaseException`
  raised while releasing (for example `KeyboardInterrupt` or a
  cancellation-style signal) is never suppressed as secondary and never
  converted into an operational result: it propagates unchanged, even when a
  primary exception is already in flight, per the shared locking contract.
- The `except BaseException` cleanup path now captures the active exception,
  attaches an ordinary release failure as secondary, and re-raises the
  original exception unchanged.
- The materialization/`ConfinementError` BuildResult paths attach an ordinary
  release failure as secondary and append it to the diagnostic message; an
  otherwise operational path keeps its operational result.
- The cleanup `finally` no longer abandons release when an earlier cleanup
  step raises: release is wrapped in a `finally` of its own and is attempted
  exactly once on every path. With no in-flight exception, an ordinary
  release failure raises a private `_LockReleaseFailed` signal (avoiding a
  `return` inside a `finally`, which previously emitted a `SyntaxWarning`
  into the CLI's stderr).  The signal carries any pending `BuildResult`, so
  the handler appends the release diagnostic and retains its process result,
  publication result, host failure, build arguments, and display string; a
  pending success becomes operational with the same context. With an active
  exception (including a cleanup failure), an ordinary release failure is
  attached as secondary and the primary propagates unchanged. A release-time
  `BaseException` always propagates on its own.

### Tests

Added ten tests to `TestMaterializationBoundary`
(`tests/test_constructor_build_orchestration.py`).

Ordinary release failures:

- `test_interruption_with_lock_release_failure_preserves_primary` —
  `KeyboardInterrupt` remains the raised exception and the release `OSError`
  is attached as secondary.
- `test_unexpected_failure_with_lock_release_failure_preserves_primary` —
  same guarantee for an ordinary `RuntimeError`.
- `test_successful_build_with_lock_release_failure_is_operational` — an
  ordinary release failure after a successful build becomes an operational
  result instead of escaping.
- `test_materialization_failure_with_lock_release_failure_surfaces_both` —
  both the materialization failure and the ordinary release failure appear in
  the operational diagnostic.
- `test_docker_failure_with_lock_release_failure_preserves_result` — both
  diagnostics remain visible and the Docker process/publication/command
  context survives.
- `test_publication_failure_with_lock_release_failure_preserves_result` — the
  publication diagnostic and structured host failure survive alongside the
  release diagnostic.
- `test_snapshot_cleanup_failure_with_lock_release_failure_preserves_result`
  — the cleanup and release diagnostics remain visible with the successful
  Docker process and publication context.

Process-control interruptions during release (propagate unchanged):

- `test_release_interruption_on_successful_build_propagates` — a release-time
  `KeyboardInterrupt` on an otherwise successful build propagates as the
  raised exception rather than becoming `ExitKind.OPERATIONAL`.
- `test_release_interruption_with_existing_primary_propagates` — with an
  existing primary failure, the release-time `KeyboardInterrupt` still
  propagates unchanged and is not attached as a secondary of the primary.
- `test_release_cancellation_style_base_exception_propagates` — a
  cancellation-style custom `BaseException` raised during release propagates
  unchanged.

### Mutation checks

- Restoring the unprotected `lock.release()` in the `except BaseException`
  path fails exactly the 2 primary-preservation tests.
- Swallowing the release failure in the cleanup `finally` fails the
  successful-build operational test.
- Widening `_release_build_lock` back to `except BaseException` fails all 3
  release-interruption tests (the interruption is suppressed or converted).
- `build_orchestration.py` was restored byte-for-byte after each mutation.

### Verification

Focused generation/orchestration suites — **253 tests, OK**;
`TestMaterializationBoundary` — **27 tests, OK**; build
transaction/persistence/materialization/CLI-output-acceptance/constructor
acceptance suites — **196 tests, OK**; build integration suites — **207
tests, OK**. Complete repository suite — **4855 tests, OK (skipped=13)**, exit
code `0`. `ty check docker --python-version 3.14` — All checks passed.
`openspec validate --strict` — valid. `git diff --check` — clean. No
specification file changed.

## Post-completion hardening pass: post-commit raw I/O mapping (tasks 5.4/5.9)

### Fix (`docker/versioning/build_orchestration.py`)

`commit_build_set()` now establishes an explicit publication boundary.
Published-blob verification and `publish_generation()` failures retain their
original `BuildCacheError`/`OSError` behavior and map to the neutral `failed to
commit successful build artifacts` diagnostic.  Only after generation
publication succeeds are ordinary inspection, storage lifecycle, marker
reconciliation, and superseded-cleanup failures wrapped in
`PostCommitBuildError` with the original exception as its cause.  The
post-publication orchestration handler alone states that the image remains
available, the newest generation remains committed, and recovery is required
and will be retried on the next build.  The post-publication boundary also
classifies expected `CapabilityError` validation failures from externally
mutated blob/marker storage as `PostCommitBuildError`.  It remains intentionally
limited to `BuildCacheError`, `CapabilityError`, and `OSError`: programming
defects and process-control interruptions remain unconverted.  Both outcomes
retain the successful Docker `process_result`, projection `publish_result`, and
`ExitKind.OPERATIONAL`/process exit code `4`.  `KeyboardInterrupt` and other
process-control `BaseException` values are not wrapped, and the existing
`finally` still attempts lock release.

Added `_format_build_cleanup_diagnostic()` at the build-domain orchestration
boundary.  It follows `PostCommitBuildError.__cause__` to a
`BuildCleanupError`, retains the aggregate count summary, and appends every
collected `CleanupFailure` target and underlying error.  Pre-build recovery
mapping remains unchanged.  Cleanup collection, ordering, secondary failures,
interruption behavior, result context, and committed state are unchanged.  The
CLI preserves its structured host-failure report and also emits the complete
build-domain aggregate instead of reducing it to the count-only summary.

### Fault injection

Added publication-boundary `TestGenerationRecoveryBoundary` tests:

- `test_blob_verification_failure_has_no_post_publication_claim` and
  `test_generation_publication_failure_has_no_post_publication_claim` inject
  failures before durable generation publication.  Both remain operational,
  preserve successful Docker process context and the raw diagnostic, and do
  not claim a committed newest generation or required recovery.

- `test_post_commit_storage_open_oserror_is_operational_and_preserves_state`
  injects an open failure only after generation publication; the successful
  image and newest generation remain, the predecessor and its blob are
  retained, the result maps to operational/4, and the lock is reacquirable.
- `test_post_commit_unsafe_storage_capability_is_operational` injects a
  deterministic `CapabilityError` from storage opening only after publication.
  It verifies operational/4 mapping, the original unsafe-directory diagnostic,
  successful Docker context, two preserved generations (including predecessor
  recovery evidence), and lock release/reacquisition.
- `test_post_commit_storage_close_oserror_is_operational` injects a close
  failure after successful reconciliation/cleanup; the image and current
  generation remain, the result is operational, and the lock is reacquirable.
  Predecessor retention is deliberately not required after completed cleanup.
- `test_post_commit_storage_interruption_propagates_and_releases_lock` injects
  the identical `KeyboardInterrupt` at the post-publication storage-open
  boundary and proves it propagates unchanged while release is attempted and
  the lock becomes reacquirable.
- `test_post_commit_reconciliation_and_release_failures_preserve_result`
  combines post-commit cleanup and release failures.  Both diagnostics remain
  visible, process/publication/command context survives, and the successful
  image plus newest generation remain committed with the predecessor retained.
- `test_recovery_aggregate_reports_every_failure_and_blocks_build` injects two
  candidate failures plus a directory-sync failure and verifies every target
  and raw error is reported, recovery blocks all build work, host context is
  retained, and the result maps to exit code `4`.
- `test_post_commit_aggregate_reports_every_failure_and_preserves_state`
  injects the same multi-failure shape after generation publication and verifies
  complete diagnostics, operational/4 mapping, successful Docker/publication
  context, newest-generation preservation, retained recovery evidence, and the
  explicit image-available/generation-committed/next-build-recovery guidance.
- `test_post_commit_reconciliation_and_release_failures_preserve_result`
  verifies the same committed-state and recovery guidance for marker
  reconciliation failure while retaining its underlying diagnostic and a
  simultaneous lock-release failure.
- `TestBuildOutputEndToEnd.test_cleanup_aggregate_details_reach_cli_with_operational_exit`
  runs the text CLI and verifies exit code `4` plus every candidate and
  synchronization diagnostic rather than only the aggregate count.
- `test_prepublication_commit_failure_has_no_committed_state_claim` and
  `test_postpublication_commit_failure_renders_state_and_details` exercise both
  CLI message classes: the former has no durability/recovery claim, while the
  latter includes committed-state guidance and every aggregate cleanup detail.

## Post-completion hardening pass: marker L2 adapter migration (tasks 5.7/5.10)

### Issue

Tasks 5.7/5.10 were marked complete, but the marker control-file read and
replacement mechanics were still implemented with the raw-``os`` duplicates
``_atomic_json_at``, ``_read_json_no_follow_at``, and
``_validate_control_destination`` rather than the shared L2 contracts.  Only
the generation/manifest control files had actually migrated.

### Fix (`docker/versioning/build_cache.py`)

- Added ``_replace_marker_json(ops, directory, name, value)``, which validates
an existing marker through
``DirectoryCapability.open_regular(name, allowed_mode=0o600)`` before
serializing the marker object and delegating the complete durable replacement
to ``RegularFileContracts(ops).durable_replace(directory, name, payload,
0o600)``.  Missing markers are allowed.  Existing markers must remain regular,
owner-owned, single-link files with mode exactly ``0600``; forbidden modes are
rejected without changing bytes or mode.  Validation is descriptor-relative,
does not follow symlinks, and never repairs an existing marker.
- Added ``_read_marker_json(ops, directory, name)``, which delegates the secure
no-follow read to ``RegularFileContracts(ops).validated_read(directory, name,
allowed_mode=0o600)`` and performs ``json.loads`` in the build domain, so a
malformed marker still raises ``json.JSONDecodeError`` (a ``ValueError``).
- Both adapters retain the existing
``BuildTransactionError("unsafe transaction state file: ...")`` diagnostic for
unsafe-entry validation stages.  For ordinary operational failures they unwrap
the shared ``TransactionError`` and re-raise its original ``OSError`` object,
preserving subclass, errno, message, and identity; shared close/cleanup
failures are transferred to that raw exception as secondary diagnostics.
Non-operational shared errors remain unchanged.  A process-control interruption
is not caught and propagates unchanged.
- ``mark_uncommitted_blob`` now prepares the cache and writes through
``lock.open_storage()``/``_replace_marker_json``.
- ``maintain_uncommitted_blobs`` opens ``lock.open_storage()`` and reads each
marker through ``_read_marker_json`` from ``storage.markers``; listing,
identity parsing, timestamp validation, TTL decisions, and marker/blob
removal remain unchanged.
- Removed ``_atomic_json_at``, ``_read_json_no_follow_at``, and
``_validate_control_destination``.  ``_require_control_name`` basename
validation is retained for the remaining control-file unlink paths.
- Scope boundaries preserved: the fixed 30-day TTL, marker-before-blob
ordering, snapshot recovery, the specialized content-addressed blob
publisher, and generation cleanup's batch unlink/fsync protocol are
unchanged.
- Migrated uncommitted-maintenance deletion away from `_unlink_entry`,
`_remove_marker`, and `_remove_blob_and_marker`.  The replacement build-domain
adapter derives blob/marker names only from validated canonical identities,
opens algorithm directories descriptor-relatively through retained
capabilities, prevalidates both entries before paired deletion, and composes
`RegularFileContracts.durable_unlink(..., allow_absent=True)` with exact
marker `0600` and blob `0444` modes.  Each single-file removal now includes
its required parent-directory fsync.  Ordinary raw `OSError` causes and
attached close/cleanup secondaries are preserved; unsafe entries fail closed
and interruptions pass through.  `maintain_uncommitted_blobs` lists and
removes through `lock.open_storage()` capabilities and no longer opens a
pathname-oriented `BuildCacheState`; no raw `os.unlink` remains on this
migrated marker/control-state path.

### Tests

Added ``MarkerL2AdapterTests`` (14 tests) to
``tests/test_constructor_build_generation_integration.py``:

- ``test_replacement_routes_through_l2_durable_replace`` — a recording
``RegularFileContracts`` subclass proves ``durable_replace`` is invoked once
with the marker basename, payload, and ``0o600`` mode against the marker
directory capability.
- ``test_missing_marker_publishes_successfully`` and
``test_existing_private_marker_publishes_successfully`` — prove both an absent
marker and an existing valid ``0600`` marker continue through L2 replacement.
- ``test_read_routes_through_l2_validated_read`` — proves ``validated_read``
is invoked with the marker basename and ``allowed_mode=0o600`` during
maintenance.
- ``test_superseded_marker_helpers_are_removed`` — the three raw-``os``
helpers no longer exist.
- ``test_unsafe_marker_read_is_a_build_transaction_error`` — a forbidden-mode
marker yields the existing build diagnostic with an ``UnsafeFileError`` cause.
- ``test_missing_marker_read_preserves_file_not_found_error`` — a missing marker
raises the original ``FileNotFoundError`` directly with its legacy errno and
message.
- ``test_malformed_marker_json_remains_a_value_error`` — malformed JSON still
raises ``json.JSONDecodeError``.
- ``test_forbidden_mode_marker_blocks_replacement_without_mutation`` — an
existing ``0644`` marker raises the existing build-domain diagnostic while its
bytes and mode remain unchanged.
- ``test_unsafe_marker_destination_blocks_replacement`` — a symlinked marker
destination fails closed with the existing diagnostic.
- ``test_replacement_failure_preserves_raw_error_and_secondary`` — an injected
``renameat`` failure re-raises the identical ``OSError`` with unchanged errno
and message, and transfers an injected temporary-cleanup failure as secondary.
- ``test_directory_fsync_failure_preserves_raw_error`` — an injected
marker-directory ``fsync`` failure re-raises the identical ``OSError`` with
unchanged errno and message.
- ``test_interruption_during_replacement_propagates_unchanged`` — a
``KeyboardInterrupt`` raised mid-replacement propagates unchanged and leaves
no marker.
- ``test_unsafe_marker_close_failure_is_secondary`` — a cleanup close failure
is attached as a secondary diagnostic to the primary ``UnsafeFileError``.

Added `MaintenanceDurableUnlinkTests` (8 tests) covering marker and blob
unlink failures, marker- and blob-directory fsync failures, durable idempotence
for already-absent entries, fail-closed unsafe marker/blob entries, close
failure retained as secondary to the primary unlink error, and unchanged
`KeyboardInterrupt` propagation.  Existing retention tests now distinguish
content corruption in otherwise safe entries (eligible for cleanup) from
unsafe modes (preserved fail-closed).  Committed identities still lose only
their stale markers, malformed marker names authorize no blob, and the exact
`current_time - verified_at > UNCOMMITTED_TTL_SECONDS` comparison is unchanged.

### Mutation checks

- Setting ``_MARKER_MODE`` to ``0o644`` fails 4 of the marker adapter tests.
- Catching ``BaseException`` in the replacement adapter fails the interruption
passthrough test.
- ``build_cache.py`` was restored byte-for-byte after each mutation.

### Verification

After restoring legacy operational-exception parity and existing-marker mode
validation, the focused ``MarkerL2AdapterTests`` suite — **14 tests, OK**.  The
requested build generation-integration, orchestration, persistence, and
transaction suites — **236 tests, OK**.  The broader Phase 5 transaction,
persistence, generation-integration, materialization, orchestration,
CLI-output acceptance, and constructor acceptance suites — **304 tests, OK**.
The aggregate-cleanup integration, orchestration, CLI-output, and constructor
acceptance suites — **213 tests, OK**.  After finalizing the post-commit state
guidance, the orchestration and CLI-output suites — **114 tests, OK**.  After
introducing the explicit publication boundary, the build transaction,
generation-integration, orchestration, and CLI-output suites — **208 tests,
OK**.  After extending that boundary to expected capability-validation
failures, the transaction, generation-integration, and orchestration suites —
**201 tests, OK**.  The shared lock, build transaction,
generation-integration, and orchestration suites after the lock lifecycle fix
— **254 tests, OK**.  These tests confirm every collected
cleanup target/error reaches orchestration and CLI diagnostics with exit code
`4`.  They also retain prior coverage for durable maintenance unlink faults;
combined Docker, publication, snapshot-cleanup, post-commit, and lock-release
failures; post-commit storage lifecycle mapping; valid and unsafe marker
handling; raw operational exception identity; secondary cleanup diagnostics;
malformed JSON; and interruption passthrough.

## Dependency boundary

Phase 5 composes the Phase 3 generation protocol and the Phase 4 cleanup/
recovery protocol; it adds no authority to a shared layer. The build lock,
generation publication, marker reconciliation, and superseded cleanup stay
build-domain decisions, while the L0–L2 substrate supplies lock preparation/
validation, durable no-clobber/unlink, and validated reads. Marker/TTL/blob/
snapshot mechanics and the shared-XDG exclusion remain specialized.

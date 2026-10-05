# Phase 4 Validation Record — Sequential Cleanup and Recovery

## Run

Date: 2026-10-03T01:13:36Z

Commands (run from the repository root with Python 3.14.7):

```bash
python -m unittest tests.test_constructor_build_cleanup
python -m unittest discover -s tests -p 'test_*.py'
ty check docker --python-version 3.14 --output-format concise
openspec validate add-durable-filesystem-transactions --strict
git diff --check
git diff --cached --check
```

## Results

- RED state confirmed before implementation: the new suite failed at
  collection with `ModuleNotFoundError: No module named
  'docker.versioning.build_cleanup'`.
- Focused Phase 4 module after implementation: **65 tests, OK**.
- Complete repository suite (regression guard): **4811 tests, OK
  (skipped=13)**, exit code `0`. (Phase 3 left the suite at 4746; Phase 4 adds
  65 build-cleanup tests.)
- Typecheck: `All checks passed!`.
- Strict OpenSpec validation: `Change 'add-durable-filesystem-transactions' is
  valid`.
- `git diff --check` and `git diff --cached --check`: clean.

## Scope delivered (tasks 4.5-4.7)

New module `docker/versioning/build_cleanup.py` (build-domain L3) and test
suite `tests/test_constructor_build_cleanup.py`. The module composes the
Phase 3 generation protocol (`discover_generations`, `GenerationInventory`,
`canonical_blob_key`), the L2 durable-unlink leaf contract, the Phase 2
`LockCapability`, and the L0/L1 descriptor substrate. It adds no authority to
any shared layer.

- **Canonical candidate derivation** — `superseded_candidates(inventory)`
  returns exactly `previous - current` over validated canonical identities in
  canonical order. An identity present in the authoritative current generation
  is never a candidate, even when it also appears in the retained predecessor.
- **Authoritative-generation marker reconciliation** —
  `reconcile_authoritative_markers` removes the uncommitted marker of every
  blob admitted to the authoritative generation as one batch, then
  synchronizes the shared marker directory exactly once, including when every
  marker is already absent. Every marker is attempted even after another
  fails; any unlink or directory-sync failure raises `BuildCleanupError`
  carrying every aggregated step failure.
- **Batched superseded cleanup** — `cleanup_superseded` derives the candidate
  set, opens and validates each affected existing blob algorithm directory
  (regular owned `0700`), unlinks every candidate blob and its marker, then
  synchronizes each affected existing blob directory once and the shared
  marker directory once. It then removes the previous manifest through the
  durable-unlink leaf contract, synchronizing the generation directory.
  Candidate removal never precedes a successful conclusion of these
  synchronizations, and the previous manifest is removed only when every
  candidate is durably absent.
- **Aggregate, fail-closed failures** — every candidate is attempted even
  after another fails; blob-unlink, marker-unlink, per-directory, and
  marker-directory synchronization failures are aggregated into
  `BuildCleanupError.failures`; a missing algorithm directory means the blob is
  already absent and still authorizes marker removal, while an unsafe
  (symlinked, foreign-owned, non-directory, or non-`0700`) algorithm directory
  preserves the blob and its marker and retains the predecessor. Ordinary
  close failures are attached as secondary diagnostics; process-control
  interruptions propagate unchanged.
- **Validated leaf removal** — before any candidate blob or marker basename is
  unlinked, `_unlink_entry` opens it descriptor-relatively with a no-follow
  flag, validates it through `fstat` as a regular, owned, single-linked entry
  (blobs must be exactly `0444`; markers must have no group/other write bit),
  and retains the descriptor until the basename has been unlinked. An unsafe
  entry is never followed, repaired, replaced, or deleted; a genuinely absent
  entry remains idempotent success. Validation reuses the L1 owned-entry
  authority rules while `unlinkat` stays batched at L0, so ownership authority
  and batch durability are both preserved. Only expected filesystem errors
  (`OSError` from open, `fstat`, unlink, or close) and unsafe-entry rejections
  (`UnsafeFileError`) are aggregated into `CleanupFailure`; unexpected
  exceptions (programming defects such as `RuntimeError`) and process-control
  interruptions propagate immediately, so cleanup stops without further
  mutation rather than being reclassified as recoverable diagnostics.
- **Algorithm-directory lifecycle** — opening each blob algorithm directory
  adopts the descriptor through `DirectoryCapability.from_fd`, which
  validates with `fstat`. An `OSError` there is now aggregated like a
  `CapabilityError` (the still-caller-owned descriptor is released and any
  close failure attached as secondary), so one unreadable algorithm directory
  no longer aborts cleanup of candidates in other algorithm directories. Once
  ownership transfers, the subsequent mode-check `fstat` is guarded by a
  `BaseException` handler that closes the adopted capability exactly once and
  re-raises any unexpected exception or process-control interruption
  unchanged, attaching an ordinary close failure as a secondary diagnostic.
- **Deterministic restart recovery** — `recover_generations` synchronizes the
  generation directory through `discover_generations` before granting
  authority, reconciles authoritative-generation markers (blocking superseded
  cleanup on failure), and then applies the same idempotent cleanup to a
  retained predecessor. Directories relevant to the candidate set are
  synchronized even when every assigned entry is already absent.

## Boundary behavior

| Input / injected boundary | Observable outcome |
| --- | --- |
| candidate derivation, `previous - current` | current identities never candidates; canonical order |
| authoritative-generation marker unlink failure | aggregated failure; generation remains committed; predecessor and superseded candidates preserved; cleanup blocked |
| authoritative marker-directory `fsync` failure | aggregated failure; predecessor preserved; cleanup blocked |
| all authoritative markers already absent | one marker-directory `fsync` still issued; no failure |
| superseded blob unlink failure | every remaining candidate still attempted; predecessor retained; aggregated failure |
| superseded marker unlink failure | aggregated failure; predecessor retained |
| one blob-directory post-batch `fsync` failure | aggregated failure; predecessor retained; other directories still synchronized |
| several blob-directory and marker-directory `fsync` failures | every failure aggregated |
| superseded marker-directory post-batch `fsync` failure | aggregated failure; predecessor retained |
| missing algorithm directory | blob treated as absent; marker still removed |
| unsafe algorithm directory (symlink / non-directory / foreign / non-`0700`) | fail closed; blob and marker preserved; predecessor retained |
| unsafe superseded blob leaf (symlink / non-regular / foreign-owned / multiply linked / forbidden mode `≠ 0444`) | fail closed; blob and any symlink target preserved; its marker is not removed; predecessor retained |
| unsafe superseded marker leaf (symlink / non-regular / foreign-owned / multiply linked / group- or other-writable) | fail closed; marker and any symlink target preserved; predecessor retained |
| other candidates beside an unsafe leaf | still attempted and their directory batches still synchronized once |
| unsafe leaf among candidates | no per-candidate directory `fsync` introduced |
| unexpected validation exception during marker reconciliation (`RuntimeError`) | propagates unchanged; later markers untouched; no marker-directory `fsync`; retained descriptor closed |
| unexpected validation exception during superseded cleanup (`RuntimeError`) | propagates unchanged; no later candidate deletion; predecessor retained; every opened descriptor closed |
| `OSError` from algorithm-directory `from_fd` validation, candidates in two algorithms | failure aggregated; healthy algorithm's candidate and marker durably removed and synchronized; failing directory's entries preserved; predecessor retained; descriptor released |
| unexpected exception / interruption during algorithm mode-check `fstat` | primary propagates unchanged; adopted capability released; no descriptor leak; close failure attached as secondary without replacing the primary |
| `previous ⊆ current` (no candidates) | previous manifest durably removed |
| predecessor unlink failure | failure reported with `predecessor` target; earlier candidate batches already durable |
| generation-directory `fsync` failure after predecessor unlink | failure reported; predecessor not claimed to remain; both generations/blobs intact |
| interruption during candidate/marker unlink or directory `fsync` | process-control exception propagates unchanged; predecessor retained; clean retry completes |
| restart, one visible generation | generation-directory `fsync` required before markers/blobs are touched |
| restart, two visible generations | idempotent cleanup; one stable generation left durably |
| `fsync` count | scales with unique affected directories, never with candidate count |

## INTROSPECT review (task 4.8)

Reviewed each required area against the implementation and test suite.

1. **Early evidence removal** — the previous manifest is unlinked only after
   the candidate `failures` list is empty, which can happen only after every
   candidate blob and marker was unlinked and every batch directory
   synchronized. `test_predecessor_is_removed_only_after_candidate_batches_are_durable`
   asserts every candidate unlink and candidate-directory `fsync` precedes the
   predecessor unlink. No finding.
2. **Non-durable blob/marker unlink** — candidate blob and marker removals use
   L0 `unlinkat` (the commit unit is a whole directory batch) and are always
   followed by a batch directory `fsync`; a failed `fsync` is aggregated and
   retains the predecessor, so a visible absence is never mistaken for a
   durable absence. `test_recovery_is_idempotent_when_a_batch_is_already_absent`
   proves an already-absent batch still requires one fresh directory `fsync`.
3. **Missing or per-candidate redundant directory `fsync`** — each affected
   existing blob algorithm directory and the shared marker directory are
   synchronized exactly once per cleanup.
   `test_batches_unlinks_then_fsyncs_each_directory_once` asserts the `fsync`
   set has no duplicates, and
   `test_fsync_count_scales_with_directories_not_candidates` proves five
   candidates in one directory issue three `fsync`s total.
4. **Incorrect batch boundaries** — blob removals are grouped by algorithm
   directory; the marker batch is global and synchronized once. Candidate
   blobs/markers are unlinked before any directory synchronization. No
   finding.
5. **Stop-on-first-error** — every candidate is attempted;
   `test_all_candidates_are_attempted_after_a_failure`,
   `test_blob_and_marker_failures_are_aggregated`, and
   `test_multiple_directory_fsync_failures_are_aggregated` cover it. No
   finding.
6. **Current-blob deletion** — candidates exclude every current-generation
   identity; `test_current_identities_are_never_candidates` and
   `test_removes_only_candidates_and_preserves_current_blobs` pin it. No
   finding.
7. **Marker authority leakage** — reconciliation removes markers only for
   blobs in the authoritative generation; a stale marker for an uncommitted
   identity is preserved (`test_removes_committed_markers_and_fsyncs_marker_directory_once`).
   Marker removal requires a live root- and namespace-matching lock
   (`test_reconciliation_requires_a_live_matching_lock`,
   `test_cleanup_requires_a_live_matching_lock`). No finding.
8. **Path interpretation below L3** — every basename is derived from a
   validated `DigestIdentity` and passed through
   `DirectoryCapability.child_basename`; descriptors are never converted back
   to paths. No finding.
9. **Error masking** — ordinary cleanup failures are aggregated into
   `BuildCleanupError.failures` (including ordinary leaf open, validation,
   unlink, and close failures), close failures during a process-control
   interruption are attached as secondary diagnostics, and the interruptions
   themselves propagate unchanged; unexpected non-filesystem exceptions are
   never aggregated and propagate immediately (finding 13).
   (`test_interruption_during_candidate_unlink_leaves_recoverable_state`,
   `test_interruption_during_marker_unlink_leaves_predecessor`,
   `test_interruption_during_marker_fsync_leaves_predecessor`,
   `test_interruption_during_candidate_directory_fsync_leaves_predecessor`,
   `test_interruption_during_predecessor_unlink_leaves_recoverable_state`,
   `test_interruption_during_generation_fsync_propagates`). **Finding:** the
   descriptor-close path in `_open_algorithm_directory` for a non-`OSError`
   `BaseException` originally swallowed a close `OSError`; it now attaches the
   close failure as secondary context and re-raises. Resolved.
10. **Retries that mistake visible absence for durable absence** — affected
    directories are synchronized from the candidate set even when every entry
    is already absent, and `recover_generations` synchronizes the generation
    directory before any authority is granted. No finding.
11. **Incomplete cleanup after generation-directory `fsync` failure** —
    `cleanup_superseded` wraps the durable-unlink failure with the
    `predecessor` target and never claims the predecessor remains visible;
    `test_generation_directory_fsync_failure_after_unlink_reports_failure`
    asserts the predecessor file is gone and the message omits `remain`.
    `test_one_generation_state_after_failed_generation_fsync_preserves_current`
    and `test_restart_with_two_generations_completes_idempotent_recovery`
    cover the two permitted restart observations. Resolved.
12. **Unsafe leaf bypass below L3** — `_unlink_entry` originally issued
    `unlinkat` for any canonical basename after only basename validation, so a
    symlinked, non-regular, foreign-owned, multiply linked, or forbidden-mode
    blob/marker would be removed without the substrate's owned-entry
    authority. **Finding and fix:** every candidate leaf (and every
    authoritative marker) is now opened descriptor-relatively with
    `O_NOFOLLOW`, validated through `fstat`, and the validated descriptor is
    retained until the basename has been unlinked; unsafe leaves make cleanup
    fail closed. `UnsafeLeafTests` covers all five blob and all five marker
    cases plus a mixed candidate set, and
    `MarkerReconciliationTests.test_unsafe_authoritative_marker_is_preserved`
    and `test_forbidden_mode_authoritative_marker_is_preserved` cover
    reconciliation.
13. **Unexpected failures masked as recoverable diagnostics** — the
    validation handler in `_unlink_entry` originally caught bare `Exception`,
    so a `RuntimeError`, assertion failure, or programming defect raised during
    `fstat`/`_validate_leaf` would be converted into a `BuildCleanupError`
    diagnostic and cleanup would continue deleting other candidates, contrary
    to the documented `OSError`-only aggregation policy. **Finding and fix:**
    the handler now catches only `(OSError, UnsafeFileError)`. Unexpected
    exceptions propagate unchanged, stop cleanup before further mutation, and
    still release the retained descriptor (via the existing `BaseException`
    path and `finally`); process-control interruptions are unchanged.
    `UnexpectedValidationFailureTests` proves both the marker-reconciliation
    and superseded-cleanup paths propagate the same exception, leave later
    entries untouched, perform no `fsync`, retain the predecessor, and close
    every opened descriptor.
14. **Algorithm-directory validation failure aborts cleanup** —
    `_open_algorithm_directory` adopted the algorithm capability through
    `DirectoryCapability.from_fd`, which validates with `fstat`; an `OSError`
    there escaped the `CapabilityError`-only handler and propagated, so one
    unreadable algorithm directory aborted the whole cleanup and prevented
    candidates in every other algorithm directory from being attempted (and
    leaked the still-caller-owned descriptor). **Finding and fix:** the
    handler now catches `(CapabilityError, OSError)`, releases the
    still-caller-owned descriptor (attaching any close failure as a secondary
    diagnostic), and returns an aggregated `CleanupFailure`, so cleanup
    continues and fails closed at the end while preserving the predecessor.
    `test_multi_algorithm_oserror_is_aggregated_and_other_candidate_cleaned`
    proves the unaffected algorithm's candidate and marker are durably removed
    and synchronized, the failing directory's entries are preserved, and the
    failure is aggregated.
15. **Adopted-capability descriptor leak on mode-check interruption** — once
    `from_fd` transferred ownership, the following mode-check `fstat` was
    wrapped only for `OSError`; a `RuntimeError` or `KeyboardInterrupt` there
    propagated without closing the adopted capability, leaking the
    descriptor. **Finding and fix:** a `BaseException` handler now closes the
    adopted capability exactly once and re-raises the primary unchanged,
    attaching an ordinary close failure as a secondary diagnostic.
    `AlgorithmDirectoryLifecycleTests` proves both the unexpected-exception
    and interruption paths propagate unchanged with no descriptor leak, and
    that a close failure while handling a primary exception is attached
    without replacing it.
16. **Member mutations** — nine mutation checks (include current identities,
    stop on first failure, per-candidate marker `fsync`, omit the
    unconditional reconciliation `fsync`, omit predecessor removal,
    neutralize leaf validation, broaden the validation catch back to bare
    `Exception`, revert the algorithm-directory catch to `CapabilityError`
    only, and remove the mode-check `BaseException` handler) each make the
    corresponding tests fail; the module was restored after each check.
    Neutralizing `_validate_leaf` fails exactly the 13 unsafe-leaf tests,
    broadening the validation catch fails exactly the 2
    unexpected-validation-failure tests, reverting the algorithm-directory
    catch fails exactly the 1 multi-algorithm test, and removing the
    mode-check handler fails exactly the 3 algorithm-lifecycle tests. Tests
    genuinely pin the behavior.

No unresolved findings remain.

## VALIDATE (task 4.9)

Exhaustive fault injection was run across every required boundary:

- **Authoritative-generation marker unlink** — aggregated failure; the
  authoritative generation remains committed; the predecessor and every
  superseded candidate are preserved; superseded cleanup and build work are
  blocked and must be recovered first
  (`test_recovery_blocks_cleanup_when_marker_reconciliation_fails`).
- **Authoritative marker-directory `fsync`** — aggregated failure; the
  predecessor is preserved and cleanup is blocked
  (`test_marker_directory_fsync_failure_is_reported`,
  `test_recovery_blocks_cleanup_when_marker_fsync_fails`).
- **Superseded blob unlink** — all candidates attempted; predecessor retained
  after the durability failure; aggregated diagnostics
  (`test_all_candidates_are_attempted_after_a_failure`,
  `test_recovery_failure_preserves_the_predecessor`).
- **Superseded marker unlink** — aggregated with blob failures
  (`test_blob_and_marker_failures_are_aggregated`).
- **Each unique blob-directory post-batch `fsync` and the superseded-marker
  post-batch `fsync`** — every failure aggregated; predecessor retained
  (`test_blob_directory_post_batch_fsync_failure_retains_predecessor`,
  `test_multiple_directory_fsync_failures_are_aggregated`,
  `test_marker_directory_post_batch_fsync_failure_retains_predecessor`).
- **Predecessor unlink and generation-directory `fsync`** — interruption and
  failure propagation covered
  (`test_interruption_during_predecessor_unlink_leaves_recoverable_state`,
  `test_interruption_during_generation_fsync_propagates`,
  `test_generation_directory_fsync_failure_after_unlink_reports_failure`).
- **Unsafe superseded blob and marker leaves** — symlink, non-regular
  (`mkfifo`), foreign-owned (`fstat` override), multiply linked, and
  forbidden-mode (`0444` blob / group- or other-writable marker) entries all
  fail closed: the unsafe entry and any symlink target are preserved, a
  superseded blob's marker is not removed, a superseded marker retains the
  predecessor, other candidates and their directory batches still complete,
  and no per-candidate directory `fsync` is introduced. Authoritative-
  generation markers are validated the same way. Mutation of `_validate_leaf`
  fails exactly the 13 new unsafe-leaf tests.
- **Unexpected validation exceptions** — a `RuntimeError` injected from
  `fstat` while validating one marker/blob propagates unchanged (never a
  `BuildCleanupError`), the failing and every later entry are untouched, no
  directory `fsync` is issued, the predecessor manifest is retained, and every
  already-opened descriptor is closed. Broadening the validation catch back to
  bare `Exception` fails exactly the 2 new regression tests.
- **Algorithm-directory open/validation `OSError`** — aggregated; candidates
  in other algorithm directories are still durably removed and synchronized;
  the failing directory's entries and the predecessor are preserved
  (`test_multi_algorithm_oserror_is_aggregated_and_other_candidate_cleaned`).
- **Algorithm-directory mode-check unexpected exception / interruption** — a
  `RuntimeError` or `KeyboardInterrupt` propagates unchanged with the adopted
  descriptor released; an ordinary close failure while handling the primary is
  attached as a secondary diagnostic without replacing it
  (`AlgorithmDirectoryLifecycleTests`).
- **`fsync` scaling** — three `fsync`s for five candidates in one directory
  versus four for three candidates across two directories, i.e. scaling with
  unique affected directories rather than candidate count.
- **Current blobs** — never deleted; current-generation identities are never
  candidates.
- **Restart after predecessor unlink without a completed directory `fsync`** —
  the one-visible-generation state is accepted only after a discovery-time
  generation-directory `fsync`
  (`test_one_generation_state_after_failed_generation_fsync_preserves_current`),
  and a two-generation state completes the same idempotent recovery to one
  stable generation durably
  (`test_restart_with_two_generations_completes_idempotent_recovery`,
  `test_restart_after_durable_removal_observes_one_stable_generation`).

## Dependency boundary

Phase 4 is additive. It composes the Phase 3 generation protocol, the L2
durable-unlink leaf contract, and the Phase 2 `LockCapability` without
modifying any shared layer or adding build-domain schema or authority to a
shared layer. Batch candidate removal descends to L0 `unlinkat` only after an
L1-equivalent retained-descriptor validation (`fstat` type/ownership/link/mode)
with a no-follow open, so it preserves both the substrate's owned-entry
authority and whole-directory batch durability rather than trading one for the
other. It does not yet migrate the legacy mutable `committed-build.json`
consumer or connect recovery into build orchestration, CLI result mapping,
marker TTL, or snapshot ordering — those integrations belong to Phase 5.

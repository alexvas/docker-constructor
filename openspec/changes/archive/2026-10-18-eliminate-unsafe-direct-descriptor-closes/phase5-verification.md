# Phase 5 Verification — Build-Cache Directory Handoff

Change: `eliminate-unsafe-direct-descriptor-closes`
Phase: 5 (tasks 5.1–5.11)
Status: complete

This file is implementation evidence only. It does not modify any OpenSpec
artifact other than the Phase 5 checkboxes in `tasks.md`.

## Deliverables

- `docker/versioning/build_cache.py::_open_relative_dir()` no longer retains a
  raw integer descriptor plus a direct `os.close`.  Each opened descriptor
  (the namespace duplicate and every walked child) is adopted by a private
  `OwnedDescriptor(PosixDescriptorOps(), fd, label=...)`.  The child is owned
  before any fallible operation, the parent is made terminal before its handoff
  release, and the failure path releases the child and retained parent exactly
  once each through one shared cleanup accumulator.  Build-cache path
  derivation, missing-component creation, mode policy, and the exact
  `BuildCacheError` text stay local.  Public signature unchanged.
- `docker/versioning/build_cache.py::_bootstrap_constructor_project_lock_parent()`
  now owns the namespace fd and every walked component through
  `OwnedDescriptor`.  The child is adopted before validation, so the unsafe
  `BuildTransactionError` rejection stays primary over an ordinary close
  failure (close secondary, one release attempt), the intermediate parent is
  terminal before its handoff close and is never retried, and the namespace is
  cleaned up independently on every path.  Public signature unchanged.
- A small shared helper `_release_owners(primary, *owners)` runs every
  non-`None` owner release through one `CleanupFailures`, so an interruption
  raised while releasing one owner does not skip the others.  It is the only
  `.run(...)` close site added in the module, and it matches the already
  recognized lightweight `owner.close` classification in the repository
  close inventory (`tests/test_transactions_l1_error_boundary.py`).
- `DirectoryDescriptor` was intentionally **not** used, and the executable
  comparison in `HandoffDecisionTests` demonstrates *why* with a real
  behavioral mismatch rather than an absent feature.  Both APIs are exercised
  against the same fixture with `require_owner=False`, so optional ownership
  checking creates no artificial difference.  `_open_relative_dir()` records
  `mkdir, chmod, open, fchmod`: it secures the freshly created pathname to
  `0700` *before* opening it, then re-applies the mode on the descriptor.
  `DirectoryDescriptor.create_directory()` records
  `mkdirat, openat, fstat, fchmod`: it opens and validates the child before
  applying the mode, so it never repairs a mode the ambient umask stripped.
  Under `umask(0o777)` the walker still returns a live `0o700` descriptor for
  the entry it just created, while `create_directory()` raises
  `DescriptorError` (cause `PermissionError`) because it tries to open the
  mode-`0000` child it created.  That ordering/mode mismatch is the reason the
  walker keeps its own `OwnedDescriptor` ledger instead of delegating to the
  capability.  The restrictive-umask assertion relies on an unprivileged owner
  to observe the `EACCES`, so it is skipped for root; the recorded-operation
  comparison is permission-independent and always runs.
- New tests:
  - `tests/test_constructor_build_cache_directory_handoff.py` — fd-ledger,
    failure-mapping, handoff, characterization, and decision coverage
    (tasks 5.1–5.7).
  - `tests/test_descriptor_close_convergence.py::Phase5HandoffConvergenceTests`
    — AST/API introspection (task 5.10).

`docker/filesystem/` was **not** modified: only ownership and cleanup
precedence are reused; build-cache recursion, path derivation, lock policy, and
domain errors stayed in `docker/versioning/build_cache.py`.

## Task-by-task evidence

| Task | Artifact | Evidence |
| --- | --- | --- |
| 5.1 | `RelativeDirLedgerTests.test_success_walk_accounts_for_every_open_transfer_and_close`, `...test_missing_without_create_releases_owner_and_returns_none` | RED (owner ledger empty pre-migration: no explicit terminal ownership evidence), then GREEN; every open/dup is matched to exactly one owner, intermediates released once in handoff order, the final descriptor transferred live, and the missing-without-create path releases its owner and returns `None` |
| 5.2 | `...test_open_failure_maps_to_build_cache_error_and_releases_owner`, `...test_stat_failure_maps_to_build_cache_error_and_releases_owner`, `...test_mkdir_failure_maps_to_build_cache_error_and_releases_owner`, `...test_fchmod_failure_releases_child_and_parent_once`, `...test_unsafe_component_rejected_and_owner_released`, `...test_repeated_close_after_failure_is_noop` | RED then GREEN; open/stat/mkdir/unsafe-validation keep their exact `BuildCacheError` text and cause/raw semantics, fchmod stays a raw `OSError`, every still-owned fd receives exactly one release attempt, and a repeated close is a no-op |
| 5.3 | `RelativeDirHandoffTests.test_parent_close_failure_releases_child_without_retrying_parent`, `...test_parent_close_interruption_releases_child_and_stays_authoritative` | RED (empty ledger / nonterminal contract) then GREEN; the parent is terminal despite its failed close and never retried, the child is released once, no child is returned, and a `KeyboardInterrupt` from the parent release stays authoritative |
| 5.4 | `BootstrapLockParentTests.test_unsafe_child_rejection_stays_primary_over_close_failure` | RED (pre-migration raw `os.close(child_fd)` lets the close `OSError` mask the `BuildTransactionError`) then GREEN; the exact `BuildTransactionError("unsafe lock parent component: ...")` stays primary, the close failure is secondary, and the child receives one release attempt |
| 5.5 | `BootstrapLockParentTests.test_parent_close_failure_is_not_retried_and_child_does_not_leak`, `...test_parent_close_interruption_stays_authoritative` | RED (pre-migration retries the failed parent and leaks the opened child; carries multiple `_PERSISTENT_PARTS`) then GREEN; the parent is terminal before close and not retried, the child is tracked and released once, the namespace is cleaned up independently, and interruption stays authoritative |
| 5.6 | `BootstrapCharacterizationTests.test_bootstrap_creates_private_component_and_returns_child_fd`, `...test_bootstrap_is_idempotent` plus the `_open_relative_dir` successful/missing/unsafe characterization in `RelativeDirLedgerTests` | GREEN before and after migration; pins owner/mode rules, missing-component creation, labels, path derivation, and public error text |
| 5.7 | `HandoffDecisionTests.test_creation_contracts_diverge_in_operation_order`, `...test_restrictive_umask_exposes_the_creation_contract_mismatch`, `...test_open_relative_dir_uses_owned_descriptor_ledger` | Executable comparison, no name inspection: records the walker order `mkdir, chmod, open, fchmod` against the capability order `mkdirat, openat, fstat, fchmod` (both `require_owner=False`), and shows the mismatch is observable — under `umask(0o777)` the walker returns a live `0o700` descriptor while `DirectoryDescriptor.create_directory()` raises `DescriptorError` (cause `PermissionError`) on the mode-`0000` child it created before securing it.  The restrictive-umask case is guarded by `os.geteuid() == 0 → skipTest(...)` (root bypasses the mode-`0000` `EACCES`); the operation-order comparison runs unconditionally.  `test_open_relative_dir_uses_owned_descriptor_ledger` is retained separately as implementation-convergence evidence, not as proof the APIs are semantically incompatible |
| 5.8 | `_open_relative_dir()` refactor | GREEN; tasks 5.1–5.3 pass, every child is owned before parent release, each parent is terminal before its handoff release, and build-cache path/missing/mode/error policy remains local |
| 5.9 | `_bootstrap_constructor_project_lock_parent()` refactor | GREEN; tasks 5.4 and 5.5 pass, validation rejection stays primary, child ownership precedes parent release, and namespace/parent/child cleanup follows the shared lifecycle without retry |
| 5.10 | `Phase5HandoffConvergenceTests` | RED (6 of 8 fail pre-migration: no owner ledger, raw close remains) then GREEN; both functions construct a shared owner, contain no unowned/raw close or bespoke release state, do not introduce `DirectoryDescriptor`, the bootstrap is not a generic second walker, domain errors stay local, and signatures are stable |
| 5.11 | focused + full suites | GREEN; 69-test review command (directory handoff + close convergence + permissions), 282-test build/lock/transaction batch, 624-test materialization/project-state/cleanup/orchestration/generation/convergence batch, full suite 5858 tests pass (13 skipped); `ty check` reports no errors; `git diff --check` clean |

## RED evidence (new tests against pre-migration production)

The two new suites were run against
`git show HEAD:docker/versioning/build_cache.py` (pre-Phase-5) and then the
Phase 5 file was restored byte-for-byte (`cmp` identical).

```text
$ python -m unittest tests.test_constructor_build_cache_directory_handoff
Ran 18 tests in 0.050s

FAILED (failures=7, errors=3)

$ python -m unittest tests.test_descriptor_close_convergence.Phase5HandoffConvergenceTests
Ran 8 tests in 0.229s

FAILED (failures=6)
```

The pre-migration defects demonstrated by the failures are the empty
`OwnedDescriptor` ledger (no explicit terminal ownership), the masking
`os.close(child_fd)` in the bootstrap unsafe-child branch, and the
bootstrap handoff that retried an ambiguously failed parent close while
leaking the freshly opened child.

The two task 5.7 contract-comparison tests pass before migration as intended:
they characterize the pre-existing creation ordering, while the ten
ledger/handoff failures above are the migration's RED evidence.

Non-vacuity check for the ordering contract: patching out the walker's
pre-open pathname `chmod` makes `_open_relative_dir()` fail under `umask(0o777)`
with `BuildCacheError: cannot open build-cache component 'walker-child':
[Errno 13] Permission denied`.  The ordering difference is therefore
load-bearing, not incidental:

```text
$ python - <<'PY'  # umask(0o777); build_cache.os.chmod patched to a no-op
walker-without-chmod: BuildCacheError: cannot open build-cache component 'walker-child': [Errno 13] Permission denied: 'walker-child'
PY
```

## GREEN evidence (post-migration)

```text
$ python -m unittest tests.test_constructor_build_cache_directory_handoff
Ran 18 tests

OK

$ python -m unittest tests.test_descriptor_close_convergence.Phase5HandoffConvergenceTests
Ran 8 tests

OK

$ .venv/bin/python -m unittest tests.test_constructor_build_cache_directory_handoff \
    tests.test_descriptor_close_convergence tests.test_constructor_build_cache_permissions
Ran 69 tests in 0.602s

OK (skipped=1)
```

Root-guard check for the task 5.7 permission-sensitive case: `sudo` and user
namespaces are unavailable in this environment, so the guard was exercised by
patching `os.geteuid` to return 0 for `HandoffDecisionTests`.  Only
`test_restrictive_umask_exposes_the_creation_contract_mismatch` skipped
(`requires an unprivileged owner to observe EACCES`); the permission-independent
`test_creation_contracts_diverge_in_operation_order` still passed.  In the
normal unprivileged run above the case executes normally.

Focused suites (task 5.11):

```text
# build-cache directory handoff, paths, permissions, state lifecycle,
# phase9a handoff/cleanup, build transactions, locking, typed preservation,
# retained-tail phase6
Ran 282 tests (skipped=1)

OK

# build materialization, constructor materialization, project-state phase1,
# phase9a project-state cleanup, build cleanup, filesystem cleanup,
# orchestration, generation integration, generations, snapshot, projection,
# persistence, build-context confinement, close convergence, filesystem
# convergence
Ran 624 tests (skipped=2)

OK
```

Repository-wide:

```text
$ python -m unittest discover -s tests -p 'test_*.py'
Ran 5858 tests in 101.410s

OK (skipped=13)

$ ty check docker/versioning/build_cache.py --python-version 3.14 --output-format concise
All checks passed!

$ ty check docker --python-version 3.14 --output-format concise
Found 2 diagnostics   # both pre-existing @contextmanager deprecation warnings

$ git diff --check
(clean)
```

## Notes

- Two pre-existing, unrelated deprecation warnings remain in
  `docker/npm_environment/publication.py:208` and
  `docker/versioning/assembly_activity.py:102`; Phase 5 adds no diagnostics.
- The Phase 5 release helper keeps the lightweight raw-`OSError` contract, so
  it is recognized by the existing repository close inventory as
  `("docker/versioning/build_cache.py", "owner.close")`; no inventory rule was
  weakened or reclassified for this phase.
- The task 5.7 comparison was reviewed and replaced: the earlier AST check
  only observed that the walker lacked `require_owner`, which is not evidence
  of incompatibility because `DirectoryDescriptor` supports
  `require_owner=False`.  The replacement exercises both APIs with equivalent
  fixtures and records a genuine creation-ordering/mode mismatch that is
  observable under a restrictive umask.
- The task 5.7 restrictive-umask failure comparison is permission-sensitive:
  root can open the mode-`0000` directory, so no `DescriptorError` is raised
  and the capability descriptor would otherwise leak.  It follows the existing
  repository pattern and skips via `os.geteuid() == 0 → self.skipTest(...)`;
  the operation-order comparison remains unconditional.
- Phases 6 and 7 remain pending (48 tasks); the change is not ready to archive.

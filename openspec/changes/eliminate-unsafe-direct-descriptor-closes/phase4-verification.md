# Phase 4 Verification — Build-Cache Aggregate Owner

Change: `eliminate-unsafe-direct-descriptor-closes`
Phase: 4 (tasks 4.1–4.8)
Status: complete

This file is implementation evidence only. It does not modify any OpenSpec
artifact other than the Phase 4 checkboxes in `tasks.md`.

## Deliverables

- `docker/versioning/build_cache.py::BuildCacheState` now privately owns one
  shared `OwnedDescriptor` for each of its five retained descriptor fields
  (`transactions_fd`, `markers_fd`, `tmp_fd`, `blobs_fd`, `persistent_fd`).
  `__post_init__` constructs the owners in the historical release order
  (reverse of the open order) using explicit foundation imports
  `docker.filesystem.descriptors.OwnedDescriptor` and
  `docker.filesystem.operations.PosixDescriptorOps`.  The public constructor,
  the six dataclass fields, the integer field values, `paths`, and the
  `BuildCacheState` instance returned by `open_build_cache_state()` are
  unchanged.
- `BuildCacheState.close()` detaches the complete five-owner tuple before any
  release, then runs every `owner.close` through one `CleanupFailures(None)`.
  A repeat close is a no-op.  The first ordinary close failure is
  authoritative and later failures stay ordered secondary diagnostics; a
  process-control interruption or an unexpected defect keeps the shared
  precedence without being wrapped.  No raw multi-close loop remains.
- New tests:
  - `tests/test_constructor_build_cache_state_lifecycle.py` — behavioral
    (tasks 4.1–4.6) and construction/compatibility (task 4.5) coverage.
  - `tests/test_descriptor_close_convergence.py::BuildCacheStateConvergenceTests`
    — AST/API introspection (task 4.7).
- Inventory maintenance: `tests/test_transactions_l1_error_boundary.py`
  classified `("docker/versioning/build_cache.py", "owner.close")` as an
  intentional raw-`OSError` shared-owner release action, matching the
  lightweight foundation contract.  This is inventory recognition, not a
  behavior change.

`docker/filesystem/` was **not** modified: only ownership and close precedence
are reused; build-cache path derivation, cache identity, locking, durability,
and domain errors stayed in `docker/versioning/build_cache.py`.

## Task-by-task evidence

| Task | Artifact | Evidence |
| --- | --- | --- |
| 4.1 | `BuildCacheStateReleaseTests.test_single_ordinary_failure_at_each_position_closes_every_descriptor` | RED (stop-on-first-failure: later fds unattempted), then GREEN; one ordinary failure at first/middle/last release position still closes all five descriptors exactly once and the injected failure is authoritative |
| 4.2 | `BuildCacheStateReleaseTests.test_multiple_ordinary_failures_attempt_every_and_order_secondary`, `...test_no_repeated_operation_after_multiple_ordinary_failures` | RED then GREEN; three ordinary failures, all five attempts, first failure authoritative with the rest as ordered secondary diagnostics, no repeated operation |
| 4.3 | `BuildCacheStateReleaseTests.test_unexpected_defect_at_each_position_attempts_every_release`, `...test_interruption_at_each_position_attempts_every_release` | RED (later fds unattempted) then GREEN; `RuntimeError` and `KeyboardInterrupt` injected at each of the five positions, every independent release attempted, authoritative object identity preserved (`assertIs`) |
| 4.4 | `BuildCacheStateTerminalTests.test_success_close_is_terminal_and_repeat_is_noop`, `...test_failure_close_is_terminal_and_repeat_is_noop`, `...test_owner_set_terminal_before_first_close_syscall`, `...test_context_cleanup_preserves_active_primary` | RED (nonterminal raw loop retried/raised `EBADF`) then GREEN; owner set detached before the first close syscall, repeat direct/context cleanup issues no operation after success or failure, active primary preserved |
| 4.5 | `BuildCacheState.__post_init__` | GREEN; `BuildCacheStateCompatibilityTests` passes: six dataclass fields unchanged, `open_build_cache_state()` returns a `BuildCacheState`, integer field values equal the five private owner fds, owners are live `OwnedDescriptor`s |
| 4.6 | `BuildCacheState.close()` | GREEN; tasks 4.1–4.4 pass with one `CleanupFailures(None)` aggregate, detached terminal owner set, and no raw multi-close loop |
| 4.7 | `BuildCacheStateConvergenceTests` | RED (5 of 7 fail pre-migration: no `__post_init__`, raw close loop), then GREEN; no unowned/raw multi-close, exactly one aggregate accumulator, exactly five owners in release order, no bespoke release state, explicit foundation imports, signature stability |
| 4.8 | focused + full suites | GREEN; 20 new/convergence tests pass, 331 + 451 focused tests pass, full suite 5831 tests pass (13 skipped); `ty check docker` reports no errors; `git diff --check` clean |

## RED evidence (new tests against pre-migration production)

The new behavioral and introspection suites were run against
`git show HEAD:docker/versioning/build_cache.py` (pre-Phase-4) and then the
Phase 4 file was restored.

```text
$ python -m unittest tests.test_constructor_build_cache_state_lifecycle
Ran 13 tests in 0.103s

FAILED (failures=13, errors=7)

$ python -m unittest \
    tests.test_descriptor_close_convergence.BuildCacheStateConvergenceTests
Ran 7 tests in 0.102s

FAILED (failures=5)
```

The release-exhaustiveness failures are the stop-on-first-failure loop; the
terminal failures are the missing aggregate terminal state; the compatibility
and introspection errors are the absent private owner set and `__post_init__`.

## GREEN evidence

New Phase 4 tests (behavioral + construction + introspection):

```text
$ python -m unittest \
    tests.test_constructor_build_cache_state_lifecycle \
    tests.test_descriptor_close_convergence.BuildCacheStateConvergenceTests
Ran 20 tests in 0.159s

OK
```

Focused suites (task 4.8) — build-cache paths/permissions/cleanup, locking,
maintenance cache scripts, generation integration, generations, project-state
phase 1 and cleanup, pi consumer, cross-consumer cache, npm publication and
publication cleanup, consumer inventory, descriptor close convergence:

```text
Ran 331 tests in 4.411s

OK (skipped=1)
```

Second focused batch — build orchestration, L1 error boundary, filesystem
cleanup/operations/directory/owned/convergence, build snapshot,
build/materialization, pi snapshot:

```text
Ran 451 tests in 7.777s

OK (skipped=2)
```

Full test suite (`python -m unittest discover -s tests -p 'test_*.py'`):

```text
Ran 5831 tests in 99.812s

OK (skipped=13)
```

Type check (`ty check docker --python-version 3.14 --output-format concise`):

```text
Found 2 diagnostics
```

Both diagnostics are pre-existing `contextmanager`-overload deprecation
warnings in `docker/npm_environment/publication.py` and
`docker/versioning/assembly_activity.py`; neither file is part of Phase 4 and
`docker/versioning/build_cache.py` passes cleanly.

Whitespace check:

```text
$ git diff --check
git diff --check: clean
```

OpenSpec validation was not re-run as a Phase 4 gate (it is Phase 7 task 7.10);
the specs were not modified in this phase.

## Descriptor-leak regression fix (test hygiene)

`BuildCacheStateReleaseTests` opens a real state in `setUp` and then
overwrites `self.state` inside each per-position loop.  The initial state was
never released, so each of the three position-loop tests leaked its five
descriptors: 5 per test, 15 per suite run.

- Released the `setUp` state with `self.state.close()` immediately before the
  position loop in
  `test_single_ordinary_failure_at_each_position_closes_every_descriptor`,
  `test_unexpected_defect_at_each_position_attempts_every_release`, and
  `test_interruption_at_each_position_attempts_every_release`.  Per-position
  state creation and fault-injection assertions are unchanged, and no
  production code changed.
- Added `BuildCacheStateDescriptorLeakTests.test_release_suite_leaves_descriptor_count_unchanged`,
  which runs the release suite in-process (Linux `/proc/self/fd`) and asserts
  the open-descriptor count is unchanged.

Measured `/proc/self/fd` growth (same-process probe):

```text
before fix:  single position-loop test = 5   full release suite = 15
 after fix:  single position-loop test = 0   full release suite =  0
```

Regression guard demonstrated non-destructively by running the suite from a
copy with the three `self.state.close()` guards removed:

```text
AssertionError: 19 != 4 : descriptor growth across BuildCacheStateReleaseTests: 4 -> 19
FAILED (failures=1)
```

Post-fix suites:

```text
$ python -m unittest tests.test_constructor_build_cache_state_lifecycle
Ran 14 tests in 0.141s

OK

$ python -m unittest tests.test_descriptor_close_convergence
Ran 29 tests in 0.302s

OK
```

## Notes

- `BuildCacheState.close()` intentionally detaches the whole owner tuple
  (`owners, self._owners = self._owners, ()`) before the first release, so a
  re-entrant close raised from a failing release cannot retry an ambiguously
  failed descriptor and a repeated close is a no-op.  Every independent owner
  is still attempted exactly once under the aggregate accumulator.
- The integer fields (`persistent_fd`, etc.) remain public and are not
  rewritten by `close()`; callers continue to use them for descriptor-relative
  operations after construction, and the private owners are solely the release
  authority.
- `CleanupFailures` continues to be imported from the existing
  `docker.transactions.cleanup` re-export because `build_cache.py` already
  depends on the transaction substrate; the new ownership imports are the
  explicit lightweight `docker.filesystem` submodules.
- The L1 close inventory extension recognises the new
  `("docker/versioning/build_cache.py", "owner.close")` action as a
  lightweight foundation release that preserves the raw `OSError` contract;
  no other inventory rule or production module changed.

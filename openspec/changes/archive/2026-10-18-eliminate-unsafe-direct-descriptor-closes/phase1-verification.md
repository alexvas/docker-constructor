# Phase 1 Verification — npm Single-Descriptor Lifecycle

Change: `eliminate-unsafe-direct-descriptor-closes`
Phase: 1 (tasks 1.1–1.9)
Status: complete

This file is implementation evidence only. It does not modify any OpenSpec
artifact other than the Phase 1 checkboxes in `tasks.md`.

## Deliverables

- `docker/npm_environment/execution.py::_write_lockfile()` transfers the
  `O_NOFOLLOW | O_EXCL` lockfile-input descriptor into
  `OwnedDescriptor(PosixDescriptorOps(), ...)` immediately after open, writes
  through the live owner, and releases through the owner.
- `docker/npm_environment/tree.py::_hash_file_entry()` transfers the no-follow
  regular-file descriptor into `OwnedDescriptor(PosixDescriptorOps(), ...)`
  immediately after open and releases through the owner.
- New tests:
  - `tests/test_npm_environment_execution.py::TestLockfileWriteLifecycle`
  - `tests/test_npm_environment_tree.py::TestTreeFileEntryLifecycle`
  - `tests/test_descriptor_close_convergence.py` (task 1.8 introspection)
    - `MigratedFunctionOwnershipTests`
    - `CloseGateEvasionTests`
    - `NpmDomainAuthorityTests`
- `tests/test_npm_environment_tree.py::TestTreeFoundationBoundary` updated to
  assert tree.py contains no direct `os.close` (was pinned to
  `{"_hash_file_entry"}`).

`docker/filesystem/` was **not** modified: only ownership and close precedence
are reused; npm open/read/hash/error policy stayed local.

## Task-by-task evidence

| Task | Artifact | Evidence |
| --- | --- | --- |
| 1.1 | `test_write_and_close_both_fail_keeps_write_primary_once` | RED (worktree) then GREEN; asserts exact object, `type is OSError`, message, `errno`/`args`, and unchanged `__cause__`/`__context__`/`__suppress_context__` |
| 1.2 | `test_sole_close_failure_propagates_raw`, `test_close_interruption_identity_preserved`, `test_repeat_cleanup_after_failing_close_issues_no_second_close` | RED then GREEN; repeated cleanup after a failing close issues no second operation |
| 1.3 | `test_file_entry_open_failure_maps_to_tree_type_mismatch` (passing characterization), `test_stat_failure_primary_over_file_close`, `test_unsafe_type_rejection_primary_over_file_close` | characterization GREEN, precedence cases RED then GREEN |
| 1.4 | `test_read_failure_primary_over_file_close`, `test_hashing_failure_primary_over_file_close` | RED then GREEN |
| 1.5 | `test_sole_close_failure_propagates_raw`, `test_close_interruption_identity_preserved`, `test_repeat_cleanup_after_failing_close_issues_no_second_close` | terminal assertion RED, all GREEN after; ownership terminal and no repeated operation after failure |
| 1.6 | `_write_lockfile()` refactor | GREEN |
| 1.7 | `_hash_file_entry()` refactor | GREEN, golden manifest bytes/digest unchanged |
| 1.8 | `tests/test_descriptor_close_convergence.py` (symbol-origin resolver + evasion fixtures), updated `TestTreeFoundationBoundary` | GREEN |
| 1.9 | focused suites | 292 passed, 0 failed |

## RED evidence (new tests against pre-migration `HEAD`)

A detached `git worktree` at `HEAD` (original production code) was populated
with the new/modified test files and run. Command:

```bash
tmp=$(mktemp -d)
git worktree add --detach "$tmp" HEAD
cp tests/test_npm_environment_execution.py \
   tests/test_npm_environment_tree.py \
   tests/test_descriptor_close_convergence.py "$tmp/tests/"
(cd "$tmp" && python -m unittest \
  tests.test_npm_environment_execution.TestLockfileWriteLifecycle \
  tests.test_npm_environment_tree.TestTreeFileEntryLifecycle)
git worktree remove --force "$tmp"
```

Observed (7 unmet-contract cases):

```text
ERROR: test_repeat_cleanup_after_failing_close_issues_no_second_close (TestLockfileWriteLifecycle)
       AttributeError: module 'docker.npm_environment.execution' has no attribute 'OwnedDescriptor'
FAIL:  test_write_and_close_both_fail_keeps_write_primary_once (TestLockfileWriteLifecycle)
       AssertionError: OSError(5, 'injected close failure') is not OSError(5, 'injected write failure')
ERROR: test_hashing_failure_primary_over_file_close (TestTreeFileEntryLifecycle)
ERROR: test_repeat_cleanup_after_failing_close_issues_no_second_close (TestTreeFileEntryLifecycle)
       AttributeError: module 'docker.npm_environment.tree' has no attribute 'OwnedDescriptor'
ERROR: test_unsafe_type_rejection_primary_over_file_close (TestTreeFileEntryLifecycle)
FAIL:  test_read_failure_primary_over_file_close (TestTreeFileEntryLifecycle)
       AssertionError: OSError(5, 'injected file close failure') is not OSError(5, 'injected read failure')
FAIL:  test_stat_failure_primary_over_file_close (TestTreeFileEntryLifecycle)
       AssertionError: OSError(5, 'injected file close failure') is not OSError(5, 'injected stat failure')
```

The two cases that are expected to behave correctly before and after
(`test_sole_close_failure_propagates_raw`,
`test_close_interruption_identity_preserved`) and the passing 1.3
characterization passed against `HEAD` as designed.

The convergence test (task 1.8) also failed against `HEAD`:

```text
FAIL: test_each_migrated_function_constructs_an_owned_descriptor (execution.py, tree.py)
FAIL: test_each_migrated_function_has_no_unowned_close_call (execution.py, tree.py)
```

## GREEN evidence (after migration)

```text
$ python -m unittest tests.test_npm_environment_execution.TestLockfileWriteLifecycle \
                     tests.test_npm_environment_tree.TestTreeFileEntryLifecycle
Ran 12 tests in 0.051s
OK

$ python -m unittest tests.test_descriptor_close_convergence
Ran 9 tests in 0.046s
OK
```

## Task 1.9 focused suites

```text
$ python -m unittest \
    tests.test_npm_environment_tree tests.test_npm_environment_execution \
    tests.test_npm_environment_storage tests.test_npm_environment_manifest_policy \
    tests.test_npm_environment_serialization tests.test_npm_environment_publication \
    tests.test_npm_environment_publication_cleanup \
    tests.test_npm_environment_output_validation tests.test_npm_environment_phase2_validate \
    tests.test_npm_environment_lockfile tests.test_descriptor_close_convergence
Ran 292 tests in 1.183s
OK
```

Whole `test_npm_environment_*` family:

```text
Ran 702 tests in 13.254s
OK (skipped=1)
```

Additional npm/locked-environment suites:

```text
$ python -m unittest tests.test_npm_fetch_phase4 tests.test_npm_logging_policy_phase8 \
    tests.test_locked_assembly_observability_phase8 \
    tests.test_npm_diagnostic_collection_phase8 tests.test_descriptor_close_convergence
Ran 217 tests in 5.667s
OK
```

Behavior pinned by these runs: exact lockfile bytes, canonical manifest
bytes/digests, no-follow behavior, domain reasons/messages, and normal import
behavior.

## Static validation

```text
$ ty check docker --python-version 3.14 --output-format concise
All checks passed!

$ git diff --check
(no output, exit 0)

$ openspec validate eliminate-unsafe-direct-descriptor-closes --strict
Change 'eliminate-unsafe-direct-descriptor-closes' is valid
```

## Review strengthening

Two gaps found in review were closed.

### Symbol-origin close gate (task 1.8)

The previous close assertion only matched `os.close`, `ops.close`, and bare
`close`, so a renamed injected backend such as `backend.close(fd)` evaded it.
`tests/test_descriptor_close_convergence.py` now resolves symbol origins
before classifying a close:

- import aliases (`import os as o`, `from os import close as c`),
- transitive local assignments of a raw close callable
  (`closer = os.close`),
- the receiver of every `*.close(...)` call; and
- the only permitted close is one whose receiver is provably an
  `OwnedDescriptor` (or an alias of one).

The gate fails closed: any close not bound to a proven shared owner is a
violation.  Adversarial check (temporary in-memory mutation of both real
functions) confirms a renamed backend close is detected:

```text
execution.py:_write_lockfile -> detected violations: ['backend.close']
tree.py:_hash_file_entry   -> detected violations: ['backend.close']
```

`CloseGateEvasionTests` pins the renamed-backend, aliased-`os`,
imported-close-alias, assigned-callable, bare-close, and direct-`os.close`
evasion paths, and `test_gate_permits_only_shared_owner_close` proves a
direct and a context-managed `OwnedDescriptor.close()` still pass.

### Explicit cause-state and failing-close repeat assertions (tasks 1.1/1.2/1.5)

- `test_write_and_close_both_fail_keeps_write_primary_once` now asserts the
  exact write `OSError` object, `type(...) is OSError`, message, `args`,
  `errno`, and that `__cause__`, `__context__`, and `__suppress_context__`
  are unchanged by the migration.
- `test_repeat_cleanup_after_failing_close_issues_no_second_close` (in both
  `TestLockfileWriteLifecycle` and `TestTreeFileEntryLifecycle`) now fails the
  first close, captures the created `OwnedDescriptor`, asserts ownership is
  terminal despite the failure, invokes `close()` twice more, and requires
  exactly one close attempt.

## Scope notes

- No production `os.close`, injected backend close, or bespoke release-state
  machine remains in either migrated function (AST-enforced by task 1.8).
- Both npm modules import only explicit `docker.filesystem.*` submodules and
  never `docker.transactions`.
- `_write_lockfile` and `_hash_file_entry` use `with OwnedDescriptor(...)`,
  which routes active-failure cleanup through `CleanupFailures` (primary
  preserved, close secondary) and the success path through the owner's
  terminal at-most-once `close()`.

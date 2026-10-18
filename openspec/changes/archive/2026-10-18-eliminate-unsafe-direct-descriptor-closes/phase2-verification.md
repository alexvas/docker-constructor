# Phase 2 Verification — Runtime Artifact and Build-Context Lifecycle

Change: `eliminate-unsafe-direct-descriptor-closes`
Phase: 2 (tasks 2.1–2.11)
Status: complete

This file is implementation evidence only. It does not modify any OpenSpec
artifact other than the Phase 2 checkboxes in `tasks.md`.

## Deliverables

- `docker/runtime_installer.py::RuntimeArtifactReader.open_verified()` transfers
  the single no-follow mounted-artifact descriptor into
  `OwnedDescriptor(PosixDescriptorOps(), fd, label="runtime artifact <path>")`
  immediately after the `O_NOATIME` / fallback open.  The active
  stat/type/mode/read/integrity failure stays primary over an ordinary close
  failure, the descriptor is released at most once, and a sole close `OSError`
  is mapped at the domain boundary to
  `InstallError(f"cannot close artifact at {_path}: {exc}")` with the original
  `OSError` as its direct cause.  Process-control interruptions propagate
  unwrapped.
- `docker/versioning/build_context_confinement.py::_write_private()` transfers
  the `O_WRONLY | O_CREAT | O_EXCL` descriptor into a shared `OwnedDescriptor`
  immediately; the exclusive-create, write loop, short-write detection, mode,
  path, and `ConfinementError` policy remain local.
- `Dockerfile` now copies the lightweight foundation beside the versioning and
  runtime-installer copies:
  `COPY docker/filesystem/ /usr/local/lib/pi-cli/docker/filesystem/`.
- New tests:
  - `tests/test_constructor_runtime_installer.py::TestRuntimeArtifactReaderLifecycle`
    (tasks 2.1–2.3)
  - `tests/test_build_context_confinement.py::TestWritePrivateLifecycle`
    (tasks 2.4 and 2.5)
  - `tests/test_runtime_installer_image_layout.py` (task 2.6)
  - `tests/test_descriptor_close_convergence.py::RuntimeBuildContextConvergenceTests`
    plus `_find_method` (task 2.10)
  - `tests/test_dockerfile_contracts.py::TestDockerfileBuildContract.test_filesystem_foundation_is_copied_for_runtime_imports`
    (task 2.10)

`docker/filesystem/` was **not** modified: only ownership and close precedence
are reused; runtime-artifact, integrity, path, and confinement policy stayed in
the domain modules.

## Task-by-task evidence

| Task | Artifact | Evidence |
| --- | --- | --- |
| 2.1 | `test_foreign_owned_noatime_fallback_returns_bytes`, `test_fallback_stat_failure_primary_over_close`, `test_fallback_non_regular_type_primary_over_close`, `test_fallback_group_writable_primary_over_close` | RED then GREEN; fallback open applies the same type/mode checks and adds no ownership rejection; masking cases fail before migration |
| 2.2 | `test_read_failure_primary_over_close`, `test_empty_artifact_primary_over_close`, `test_integrity_failure_primary_over_close` | RED then GREEN; each exact active failure stays primary with close secondary and one close attempt |
| 2.3 | `test_sole_close_failure_maps_to_install_error`, `test_sole_close_reports_controlled_install_failure`, `test_close_interruption_identity_preserved` | RED then GREEN; sole close maps to `InstallError` with the raw `OSError` as `__cause__`, the `install_extensions` result path reports `FAILED`, interruption propagates unchanged |
| 2.4 | `test_write_failure_primary_over_close`, `test_short_write_primary_over_close` | RED then GREEN; write `OSError` and `ConfinementError` remain primary with close secondary and one attempt |
| 2.5 | `test_success_then_sole_close_failure_propagates_raw`, `test_close_interruption_identity_preserved`, `test_terminal_owner_ignores_repeated_close` | established raw `OSError` mapping preserved, interruption identity unchanged, output bytes intact, owner terminal and repeat close a no-op |
| 2.6 | `tests/test_runtime_installer_image_layout.py` | RED (`ModuleNotFoundError: No module named 'docker.filesystem'`) before the Dockerfile copy; GREEN after 2.9, with the repository absent from the subprocess `sys.path` |
| 2.7 | `RuntimeArtifactReader.open_verified()` refactor | GREEN; tasks 2.1–2.3 pass |
| 2.8 | `_write_private()` refactor | GREEN; tasks 2.4 and 2.5 pass |
| 2.9 | `Dockerfile` filesystem copy | GREEN; task 2.6 passes without adding the repository to the subprocess import path |
| 2.10 | `RuntimeBuildContextConvergenceTests`, `_find_method`, Dockerfile-layout assertion | GREEN; no direct close or bespoke release state, only explicit `docker.filesystem` submodule imports, foundation gains no domain authority, exact source/destination copy present |
| 2.11 | focused suites | 22/22 Phase 2 tests pass; broader focused run 552 tests pass (2 skipped) on the repaired baseline; full suite 5790 tests pass (13 skipped) |

## RED evidence (new tests against pre-migration production)

The new tests were run against the unmodified production before the GREEN
refactors.

```text
$ python -m unittest tests.test_constructor_runtime_installer.TestRuntimeArtifactReaderLifecycle
Ran 10 tests ... FAILED (errors=8)
# e.g. test_fallback_stat_failure_primary_over_close:
#   InstallError "cannot stat artifact ..." is masked by
#   OSError(5, 'injected close failure') from the bare ``finally: os.close``
# test_read_failure_primary_over_close / test_empty_artifact_primary_over_close
#   / test_integrity_failure_primary_over_close: same finally-close masking
# test_sole_close_failure_maps_to_install_error: raw OSError escapes, not InstallError
# test_sole_close_reports_controlled_install_failure: raw OSError escapes install_extensions
# Passing characterizations before and after: fallback success, close interruption

$ python -m unittest tests.test_build_context_confinement.TestWritePrivateLifecycle \
      tests.test_descriptor_close_convergence.RuntimeBuildContextConvergenceTests
Ran 10 tests ... FAILED (failures=5, errors=2)
# _write_private write failure masked by close; OwnedDescriptor attribute absent;
# convergence gate finds os.close in both migrated functions
```

## GREEN evidence

Re-run on the repaired baseline, which places the staged Phase 2 change on top
of commit `51fbe39` ("Decouple behavioral tests from project inventory") that
repairs the behavioral tests that were broken when Phase 2 was implemented.
All results below are from that worktree.

```text
$ python -m unittest \
    tests.test_constructor_runtime_installer.TestRuntimeArtifactReaderLifecycle \
    tests.test_build_context_confinement.TestWritePrivateLifecycle \
    tests.test_descriptor_close_convergence.RuntimeBuildContextConvergenceTests \
    tests.test_runtime_installer_image_layout \
    tests.test_dockerfile_contracts.TestDockerfileBuildContract.test_filesystem_foundation_is_copied_for_runtime_imports
Ran 22 tests in 0.110s
OK
```

Focused suites (task 2.11): runtime-installer, runtime-projection,
build-context confinement, build-materialization, build-snapshot,
Dockerfile-contracts, build-generation integration, build-generations,
image-layout, descriptor-close convergence:

```text
Ran 552 tests in 2.808s
OK (skipped=2)
```

Full test suite (`python -m unittest discover -s tests -p 'test_*.py'`):

```text
Ran 5790 tests in 100.014s
OK (skipped=13)
```

The focused-suite count moved from 551 to 552 because the repaired baseline
replaces the previously failing
`tests.test_constructor_build_materialization.TestBuildArtifactSelection.test_exact_effective_linux_amd64_pairs`
characterization with test-owned inventory fixtures; the unrelated uv `0.12.5`
vs `0.12.24` failure recorded in the original run no longer reproduces.

## Notes

- The image layout test derives the staged tree from the Dockerfile COPY
  instructions themselves, so it fails closed if the `docker/filesystem/`
  copy is ever removed.
- The fallback tests intentionally do not add any file-ownership rejection:
  `O_NOATIME` denial is the only ownership-related signal and the fallback
  read-only open keeps the existing type/mode checks.

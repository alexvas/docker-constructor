# Phase 10 verification evidence

## Focused validation (task 10.7)

- `python -m unittest -v tests.test_host_observability_acceptance_phase10 tests.test_host_operational_events tests.test_host_activity_monitor tests.test_host_diagnostic_projection tests.test_host_download_observability tests.test_constructor_build_materialization tests.test_constructor_pi_release tests.test_locked_assembly_observability_phase8 tests.test_npm_diagnostic_collection_phase8 tests.test_npm_environment_streaming tests.test_host_presentation_phase9 tests.test_host_failure_output_regression tests.test_constructor_host_progress tests.test_constructor_build_output tests.test_constructor_build_output_acceptance tests.test_constructor_cache_contracts tests.test_npm_environment_cleanup`
  - PASS: 467 tests in 5.096s.
- `python -m unittest tests.test_host_observability_acceptance_phase10.TestIntegratedSilentNpmAcceptance -v`
  - PASS: the production `assemble` / `DockerRunExecutor.run_streaming` path used an injected fake subprocess, fake monotonic clock, and fake heartbeat waiter; it crossed 120 simulated seconds, resumed stdout, and exited successfully in 0.104 wall-clock seconds with no Docker, registry, network, or real-time wait.
- `python -m unittest tests.test_host_observability_acceptance_phase10 -v`
  - PASS: 3 outer acceptance tests in 1.214s. The matrix contract executed all 23 mapped behavior tests with `unittest.TestResult` and reported no failure or error; it did not merely resolve their IDs.
- Focused host-observability/npm execution and streaming/presentation/failure-output/build-output command recorded in the session.
  - PASS: 390 tests in 12.597s.

## Repository validation (task 10.8)

- `npx pi-green-loop check`
  - PASS after the integrated subprocess and executable-matrix revisions.
  - Typecheck: PASS (`ty check docker --python-version 3.14 --output-format concise`).
  - Complete test suite: PASS (4011 tests, 13 skipped).
- `openspec validate improve-host-build-observability --strict`
  - PASS: change is valid.
- `git diff --check`
  - PASS.
- `git diff --cached --name-only`
  - PASS: empty; no files staged or unstaged by the agent.
- `./scripts/check-dockerfile`
  - Initial host run reported Hadolint `DL3002` because the `pi-tools` stage ended as root.
  - Added the explicit non-root terminal state `USER dev`; focused Dockerfile/workspace tests passed (25 tests).
  - PASS: the user reran the Docker-backed Hadolint command on the host and confirmed it completed without warnings.

Task 10.8 is complete; all required checks are green.

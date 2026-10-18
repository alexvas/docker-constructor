# P1 skip inventory

Baseline and exit runs report the same 13 skips. Skips were read from an
additional full pytest run with `-rs`; the mandated command remains
`.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q` and is
not replaced by that reporting run. The same 13 skips appear in the additional
unittest discovery run (`OK (skipped=13)`), which
[`docs/testing.md`](../../../../docs/testing.md) requires only when a task
explicitly names it.

No fixed numeric skip count is frozen: the inventory records reasons and
conditions so a legitimate platform difference is visible.

| # | Test ID | Reported reason | Applicability condition | Disposition |
| --- | --- | --- | --- | --- |
| 1 | `tests/test_constructor_build_snapshot.py::TestArtifactSnapshot.test_foreign_owner_hard_link_is_rejected_without_adoption` | `an existing 'docker-dev' account is required` | host/image provides a `docker-dev` account and group for foreign-ownership checks | environmental prerequisite; provision the project image account. Test is meaningful only with that account. |
| 2 | `tests/test_constructor_build_snapshot.py::TwoBuildOwnershipMaintenanceRegression.test_two_builds_reuse_blobs_after_project_ownership_maintenance` | `an existing 'docker-dev' account is required` | same | environmental prerequisite; no regression hidden. |
| 3 | `tests/test_constructor_build_cache_permissions.py::ConstructorProjectForeignOwnership.test_foreign_owned_constructor_project_keeps_external_state_private` | `an existing 'docker-dev' account is required` | same | environmental prerequisite; no regression hidden. |
| 4 | `tests/test_constructor_phase4_named_context.py::TestPhase4Lifecycle.test_differing_uid_cannot_traverse_private_snapshot` | `sudo runuser -u nobody unavailable: [Errno 2] No such file or directory: 'sudo'` | host has `sudo` and an unprivileged `nobody` account | environmental prerequisite; install/configure `sudo` and the account, or run in the project image. |
| 5 | `tests/test_bound_pi_assembly_execution_phase6_registry.py::TestDockerBackedRegistryAcceptance.test_constructor_cold_and_warm_build_preserve_progress_and_final_image` | `run through validate-bound-pi-assembly-execution-phase6-registry` | `BOUND_PI_PHASE6_HERMETIC=1`; Docker-backed acceptance is driven by `scripts/validate-bound-pi-assembly-execution-phase6-registry` | explicit opt-in/standalone-runner condition; not a silently trimmed suite. |
| 6 | `…::test_cold_then_network_blocked_warm_reuses_verified_publication` | same | same | same |
| 7 | `…::test_stalled_request_fails_finitely_without_network_classification` | same | same | same |
| 8 | `…::test_large_registry_failure_uses_bounded_redacted_production_diagnostics` | same | same | same |
| 9 | `…::test_partial_download_preserves_opaque_cache_and_retry_succeeds` | same | same | same |
| 10 | `…::test_constructor_build_interruption_cleans_up_and_retry_succeeds` | same | same | same |
| 11 | `…::test_interruption_preserves_prior_output_and_cache_then_retry_succeeds` | same | same | same |
| 12 | `tests/test_npm_environment_phase3_validate.py::TestRealPinnedContainerSmoke.test_node_image_executes_assembler_script` | `set NPM_ENV_REAL_DOCKER=1 to run the real pinned-container smoke test` | explicit `NPM_ENV_REAL_DOCKER=1` and a usable Docker daemon | explicit opt-in; provisioning blocked in this environment by the same missing Docker. |
| 13 | `tests/test_bound_pi_assembly_execution_phase6_host.py::TestDockerBackedHostAcceptance.test_outer_deadline_reaps_container_and_removes_staging` | `run through validate-bound-pi-assembly-execution-phase6-host` | `BOUND_PI_PHASE6_HERMETIC=1`; driven by `scripts/validate-bound-pi-assembly-execution-phase6-host` | explicit opt-in/standalone-runner condition. |

## Review

- Every skip has a documented environmental or explicit opt-in condition.
- No skip was added or removed by this P1 preparation; the prepared changes
  add tests and do not reduce collection.
- No unexpected xfail/xpass appeared; the runner exit status is 0.
- Missing Docker/sudo/`docker-dev` prerequisites are recorded, not turned into
  passes. P6/P7 CI provisioning must supply the installable prerequisites.

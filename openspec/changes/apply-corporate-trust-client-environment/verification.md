# Verification Evidence

## Phase 1 — Build-Time Client CA Environment

### RED baseline

- `python -m unittest tests.test_constructor_corporate_network_build_red.TestCorporateNetworkEnvironmentHelper.test_enabled_trust_exports_complete_fixed_client_ca_mapping -v`
  - **Expected failure:** `REQUESTS_CA_BUNDLE`, `PIP_CERT`, and `CURL_CA_BUNDLE` were absent; the existing `NODE_EXTRA_CA_CERTS` and `SSL_CERT_FILE` exports had the fixed system-bundle value.
- `python -m unittest tests.test_constructor_corporate_network_build_red.TestCorporateNetworkEnvironmentHelper.test_absent_or_disabled_trust_preserves_inherited_client_ca_values -v`
  - **Passed:** absent and explicitly false trust preserved conflicting inherited values for all five names.
- `python -m unittest tests.test_constructor_corporate_network_build_red.TestDockerfileTrustReplacementRed.test_client_ca_not_persisted_and_not_same_named_args -v`
  - **Passed:** no persistent same-named `ENV` or `ARG` instruction existed for any of the five variables before implementation.

### Introspection

The complete networked Dockerfile `RUN` enumeration contains seven operations:

1. `base`: `apt-get update`
2. `toolchain`: `apt-get update`
3. `toolchain`: `/tmp/rustup-init -y`
4. `toolchain`: `setup-python.sh`
5. `toolchain`: `uv tool install`
6. `openspec-tools`: `npm install`
7. `runtime`: `setup-zsh.sh`

Every networked `RUN` sources `/tmp/corp-network-env.sh` before its first detected network operation. The structural assertion checks ordering rather than merely requiring the helper somewhere within the block. The Dockerfile metadata audit found no same-named client CA `ARG` or `ENV`; `PI_CORPORATE_CA_PATH` is the sole CA-path build transport.

### Final focused validation

- `python -m unittest tests.test_constructor_corporate_network_build_red -v`
  - **Passed:** 25 tests.
  - Covers enabled mapping with replacement of conflicting inherited values, absent/disabled omission when no values are inherited, absent/disabled inherited-value preservation, complete networked-run coverage, non-persistence, build-vector behavior, proxy coexistence, bundle validation, and replacement.
- `python -m unittest tests.test_dockerfile_contracts -v`
  - **Passed:** 14 tests.
  - Covers the broader Dockerfile and build-context contracts.

## Phase 2 — Runtime Launch Propagation

### RED baseline

Before implementation the runtime renderers emitted only the read-only bundle mount
(and, for the standalone npm assembler, the existing `npm_config_cafile`).

- `python -m unittest tests.test_constructor_corporate_network_run_red`
  - **Failed:** 4 tests, all on the missing five-variable propagation:
    - `TestRunVectorClientCaEnvironmentRed.test_enabled_trust_assigns_exactly_one_fixed_path_per_client_ca_variable`
    - `TestRunVectorClientCaEnvironmentRed.test_enabled_trust_preserves_readonly_mount_alongside_client_ca_env` — only the new environment assertions failed; the read-only mount assertion passed.
    - `TestRunVectorClientCaEnvironmentRed.test_client_ca_mapping_is_closed_to_arbitrary_values_and_host_paths`
    - `TestRunVectorClientCaOrchestrationRed.test_enabled_trust_orchestrates_mount_and_client_ca_environment`
  - **Passed unchanged:** absent/disabled omission, proxy-only and host-access-only independence, the constructor `docker run` entry-point enumeration, and the internal gateway-probe/build-verification audit assertions.
- `python -m unittest tests.test_npm_environment_corporate_network`
  - **Failed:** 7 tests:
    - `TestClientCaEnvironmentEnabled.test_vector_assigns_all_five_client_ca_variables_to_fixed_path`
    - `TestClientCaEnvironmentEnabled.test_vector_has_exactly_one_assignment_per_client_ca_variable`
    - `TestClientCaEnvironmentEnabled.test_render_docker_argv_carries_client_ca_assignments`
    - `TestClientCaEnvironmentExecution.test_assemble_executes_client_ca_assignments`
    - `TestStandaloneAssemblerLaunchVectorParity.test_vector_and_argv_agree_on_enabled_client_ca_policy`
    - `TestEnabledDisabledProjection.test_disabled_proxy_emits_no_proxy_env` — updated exact environment set now requires the five names when trust is enabled.
    - `TestOnlyResolvedPolicyReceived.test_executable_argv_receives_only_resolved_policy` — same updated exact environment set.
  - The existing `npm_config_cafile` assignment already matched, so every failure was exclusive to the missing five-variable propagation. Absent/disabled and proxy-only assertions passed before implementation.

### Introspection

**One closed mapping, one destination (2.12).** `CLIENT_CA_ENVIRONMENT` and
`SYSTEM_CA_BUNDLE` are defined once in `docker/versioning/corporate_network.py`,
the corporate-network domain owner, and imported by both runtime renderers:

- `docker/versioning/rendering.py` (`_emit_client_ca_run_env`) emits the mapping
  immediately after the read-only system-bundle mount.
- `docker/npm_environment/run_vector.py` appends the mapping before the existing
  `npm_config_cafile` entry.

The mapping is a fixed tuple of five constant names and one constant value. The
resolved corporate-trust decision reaches the renderers as the
`corporate_trust_bundle` absolute host path resolved by `docker/launcher.py`
(user runtime) and by `docker/versioning/pi_assembly.py` (assembler). The host
bundle path is used only as the mount source; it never enters an environment
value, and the invoking-host environment is never read. This is pinned by
`test_client_ca_mapping_is_closed_to_arbitrary_values_and_host_paths`, which
patches conflicting host values and asserts the fixed value survives.

**Standalone npm assembler enumeration (2.13).** There is exactly one
constructor-launched standalone npm assembler `docker run` builder:
`docker/npm_environment/run_vector.py` (`render_run_vector` /
`render_docker_argv`). It is reached through `docker/npm_environment/execution.py`
(`assemble`, documented as "run one standalone pinned assembler container"):

1. `docker/versioning/pi_assembly.py` (`materialize_pi` -> `assemble_environment`)
2. `docker/npm_environment/publication.py` (`assemble_environment` -> `assemble`)
3. `docker/npm_environment/smoke.py` (`run_smoke` -> `assemble`), which passes no
   corporate policy and therefore renders a disabled vector

Every enabled launch vector receives the same five-variable policy as the primary
run-rendering path, proven by
`TestStandaloneAssemblerLaunchVectorParity.test_assembler_policy_matches_primary_run_rendering_path`
and by the execution-boundary assertion in
`TestClientCaEnvironmentExecution.test_assemble_executes_client_ca_assignments`.
None of these vectors is excluded from the constructor-launch contract.

**Alternate constructor `docker run` entry points (2.14).** The audited builders
are enumerated by `TestConstructorRunEntryPointAudit.test_constructor_run_argument_builders_are_enumerated`:

| Path | Role | Client CA mapping | Bundle mount |
| --- | --- | --- | --- |
| `docker/versioning/rendering.py` | shared user runtime renderer | emitted only when enabled | enabled only |
| `docker/npm_environment/run_vector.py` | standalone npm assembler | emitted only when enabled | enabled only |
| `docker/versioning/verification.py` | build verification (`docker run --rm <image> <tool> --version`) | never | never |
| `docker/networking.py` | gateway probe (`docker run --rm alpine:3.20 ...`) | never | never |

The build-verification and gateway-probe containers are constructor-launched
internal diagnostic paths explicitly excluded from runtime trust injection. Their
launch vectors introduce neither `--mount` nor `--env`, so they preserve inherited
image trust settings regardless of the corporate-trust setting. This scope decision
does not assert that network access is technically disabled. The mapping is emitted
only on in-scope paths that also receive the read-only bundle;
`test_gateway_probe_emits_no_client_ca_or_bundle_mount` and
`test_build_verification_emits_no_client_ca_or_bundle_mount` pin both diagnostic
exceptions. Every other enumerated constructor launch path remains in scope,
including the standalone npm assembler. Direct image launches that bypass the
constructor remain outside the runtime propagation contract by design.

### Final focused validation

- `python -m unittest tests.test_constructor_corporate_network_run_red`
  - **Passed:** 28 tests. Covers enabled mount plus five fixed assignments, absent/disabled omission, proxy-only and host-access-only independence, orchestration propagation, mapping closure, and the constructor launch entry-point audit.
- `python -m unittest tests.test_npm_environment_corporate_network`
  - **Passed:** 29 tests. Covers enabled vector/argv/execution assignment, preserved `npm_config_cafile`, absent/disabled omission, proxy-only independence, and primary-path parity.
- `python -m unittest tests.test_npm_environment_run_vector`
  - **Passed:** 19 tests. Vector shape and explicit-environment contract for the default (disabled) assembler vector.
- `python -m unittest tests.test_npm_environment_policy`
  - **Passed:** 14 tests. Locked npm policy environment remains unchanged.
- `python -m unittest tests.test_constructor_corporate_network_acceptance_red`
  - **Passed:** 14 tests. End-to-end configured/disabled dry-run acceptance.
- `python -m unittest tests.test_constructor_corporate_network_red`
  - **Passed:** 52 tests. Corporate trust/proxy configuration, bundle validation, and fail-closed behavior.
- `python -m unittest tests.test_constructor_host_access_acceptance`
  - **Passed:** 10 tests. Host-access policy independence.
- `python -m unittest tests.test_constructor_run_vector`
  - **Passed:** 118 tests. Full primary run-vector contract.
- `python -m unittest discover -s tests -p 'test_*.py'`
  - **Passed:** 4478 tests, 13 skipped.
- `ty check docker --python-version 3.14 --output-format concise`
  - **Passed:** all checks passed.

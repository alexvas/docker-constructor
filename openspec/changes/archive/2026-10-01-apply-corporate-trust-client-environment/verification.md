# Verification Evidence

## Phase 5 — Release Integration

### Release acceptance matrix and RED baseline

The release matrix is exercised by:

`python -m unittest tests.test_constructor_corporate_network_build_red tests.test_constructor_corporate_network_run_red tests.test_constructor_corporate_network_verification_red tests.test_constructor_corporate_network_acceptance_red tests.test_constructor_corporate_network_boundaries_red -v`

| Boundary | Enabled coverage | Disabled/independence coverage |
| --- | --- | --- |
| Build-helper export | Exact five-name/fixed-path export | Omission and inherited-value preservation |
| Networked Dockerfile coverage | Every networked `RUN` sources the helper | No constructor CA values or bootstrap |
| Runtime argv and mount | Five exact assignments plus read-only mount | No assignments or mount |
| Runtime verification | Per-variable exact checks plus mount check | No enabled-policy expectation |
| Proxy policy | Coexists with enabled trust | Proxy-only does not activate trust |
| Host-access policy | Coexists with enabled trust | Host-access-only does not activate trust |
| Image metadata | No persistent same-named `ARG`/`ENV` | Direct launches remain outside policy |

- **Passed:** 86 tests in 0.223 seconds; no unsatisfied matrix cases were found, so task 5.3 required no integration fixes.

The first complete pre-release run produced:

- `scripts/check-types` — **Passed:** `All checks passed!`
- `python -m unittest discover -s tests -p 'test_*.py'` — **Passed:** 4505 tests in 84.666 seconds, 13 skipped.
- `scripts/check-dockerfile` — **Passed on the Docker-capable host:** exit 0 with no output.
- `./docker/docker-constructor.py build --yes --no-pull` — **Passed on the Docker-capable host:** all 67 BuildKit steps completed and `pi-cli-pi:latest` was exported; a second cached build also passed all 67 steps in 3.5 seconds.

The complete task 5.4 gate is green: typecheck, full unit/integration suite, Hadolint, and the canonical enabled-trust image build all pass. The agent host itself has no `docker` executable, so Docker-backed task 5.8 still requires execution on the Docker-capable host.

### Requirement and scenario traceability

| Delta-spec contract | Completed tasks | Automated or observable evidence |
| --- | --- | --- |
| Enabled build trust validates, bootstraps, replaces, and survives package installation | 1.1–1.8 | `TestCorporateNetworkEnvironmentHelper`, `TestDockerfileTrustReplacementRed`, canonical enabled-trust build |
| Disabled build skips constructor trust bootstrap and preserves inherited values | 1.2, 1.5–1.8 | disabled helper/build-vector and Dockerfile structural tests |
| Restarted in-scope runtime receives current read-only bundle | 2.1–2.15 | enabled run-vector, orchestration, and acceptance tests; task 5.8 supplies Docker-backed confirmation |
| Build-verification and gateway-probe launches receive no trust injection | 2.14–2.15 | constructor run-entry-point audit tests for both exceptions |
| Enabled build and runtime clients receive exactly five fixed assignments | 1.1–1.8, 2.1–2.15, 3.1–3.11 | build-helper, primary renderer, npm assembler, orchestration, and runtime-verification suites |
| Disabled trust introduces no assignments and preserves inherited image settings | 1.2, 2.3, 2.6, 2.9, 3.3, 3.7 | helper, primary/assembler renderer, and runtime-verification disabled tests |
| Policy is closed to arbitrary names, paths, host environment, and configuration | 2.12, 4.3, 4.7, 4.9 | mapping-closure, schema-rejection, projection, identity, and evidence-owner tests |
| Client CA values do not persist in image metadata; direct launches are out of scope | 1.3, 1.7, 4.2, 4.6, 4.9 | Dockerfile `ARG`/`ENV`, image-inspection disclosure, and README tests |
| Runtime verification checks policy without TLS connectivity assumptions | 3.2–3.3, 3.6–3.7, 3.9–3.10 | exact environment/mount checks and no-network-command regression |
| Node trust is augmentation-oriented and supports enterprise interception | 4.2, 4.6 | multilingual documentation assertions |

Every requirement and scenario has a completed implementation task and automated or explicit observable evidence; no uncovered delta-spec contract remains.

### Final diff audit

The staged and unstaged diff was reviewed for every task 5.6 risk. No unresolved finding remains:

- disabled mode emits no constructor CA overrides and preserves inherited values;
- no client CA name is persisted as Dockerfile `ARG` or `ENV` metadata;
- configuration remains a closed boolean and exposes no arbitrary environment passthrough;
- evidence redacts host bundle paths and both raw and JSON-escaped certificate content;
- proxy and host-access policy remain independent of corporate trust;
- cache, digest, fetch, dependency, and npm identities do not import the client CA mapping;
- runtime verification uses only local `docker exec ... printenv` and mount inspection, with no external TLS assumption;
- phase dependencies remain forward-only and no late-phase decision changes an earlier contract.

### Final validation

- `python -m unittest tests.test_constructor_corporate_network_build_red tests.test_constructor_corporate_network_run_red tests.test_constructor_corporate_network_verification_red tests.test_constructor_corporate_network_acceptance_red tests.test_constructor_corporate_network_boundaries_red -v` — **Passed:** 86 tests in 0.233 seconds.
- `python -m unittest discover -s tests -p 'test_*.py'` — **Passed:** 4505 tests in 84.137 seconds, 13 skipped.
- `scripts/check-types` — **Passed:** `All checks passed!` with `ty` reporting `ok` under Python 3.14.7.
- `scripts/check-dockerfile` — **Passed on the Docker-capable host:** exit 0 with no output.
- `./docker/docker-constructor.py build -y` and `./docker/docker-constructor.py build --yes --no-pull` — **Passed on the Docker-capable host:** full 67-step build in 143.0 seconds and cached 67-step build in 3.5 seconds.
- Docker-capable host tool versions:
  - `docker --version` — Docker 29.7.2, build `a7dcaa6`.
  - `docker buildx inspect --bootstrap` — default Docker driver running BuildKit v0.32.2.
  - `docker run --rm hadolint/hadolint@sha256:27173fe25e062448490a32de410c08491c626a0bef360aa2ce5d5bdd9384b50d hadolint --version` — Haskell Dockerfile Linter 2.12.0.
- `openspec validate apply-corporate-trust-client-environment --strict` — **Passed** with OpenSpec 1.13.0: change is valid.

The only environment limitation is that this agent host lacks Docker; Docker lint, canonical build, and runtime evidence were therefore supplied from the Docker-capable host.

### Docker-backed enabled runtime acceptance

The Docker-capable host ran the following exact command against `pi-cli-pi:latest` with corporate trust enabled. It checks the five exact values and read-only mount locally without making a TLS or network request:

```bash
./docker/docker-constructor.py run \
  --workspace "$PWD" \
  --no-tty \
  --no-interactive \
  --image pi-cli-pi:latest \
  -- sh -ceu '
expected=/etc/ssl/certs/ca-certificates.crt
for name in \
  NODE_EXTRA_CA_CERTS \
  SSL_CERT_FILE \
  REQUESTS_CA_BUNDLE \
  PIP_CERT \
  CURL_CA_BUNDLE
do
  actual=$(printenv "$name")
  test "$actual" = "$expected"
  printf "%s=%s\n" "$name" "$actual"
done

options=$(findmnt -n -o VFS-OPTIONS --target "$expected")
case ",$options," in
  *,ro,*) printf "bundle_mount=%s options=%s\n" "$expected" "$options" ;;
  *) printf "bundle mount is not read-only: %s\n" "$options" >&2; exit 1 ;;
esac
'
```

Captured result (exit 0), matching the command above:

```text
[SUCCESS] stdout: ==> fix ownership dev:dev /home/aavasiljev/work/home/docker-constructor.git
==> fix permissions ug+rwX /home/aavasiljev/work/home/docker-constructor.git
==> Installing Pi extensions from runtime projection
==> Configuring rtk integration
RTK Pi extension already up to date:
  Extension: /home/dev/.pi/agent/extensions/rtk.ts

Pi will load the extension automatically on next start.
Verify: pi -e /home/dev/.pi/agent/extensions/rtk.ts --no-session
Telemetry disabled.
NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt
SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
PIP_CERT=/etc/ssl/certs/ca-certificates.crt
CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
bundle_mount=/etc/ssl/certs/ca-certificates.crt options=ro,relatime
```

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

## Phase 4 — Boundaries and Documentation

### RED baseline

- `python -m unittest tests.test_constructor_corporate_network_docs_red -v`
  - **Failed as expected:** all README translations lacked the exact five-variable mapping and the required enabled/disabled, metadata, direct-launch, diagnostic-exception, and Node-augmentation statements.
- `python -m unittest tests.test_constructor_corporate_network_boundaries_red -v`
  - **Passed:** 7 tests. The implementation has a closed five-name/fixed-value mapping, rejects arbitrary local configuration fields, and omits client CA policy from reviewed configuration and projection/identity owners. Exercised disclosure tests feed unique host-path and PEM-content sentinels through redacted run summaries, runtime verification, evidence capture, and image inspection, then prove the resulting details, evidence files, notes, index, and inspected metadata exclude them.

### Introspection

A non-archived source, test, example, and documentation search found no current claim that only `SSL_CERT_FILE` and `NODE_EXTRA_CA_CERTS` comprise the enabled client policy. The main corporate-network specification's longer disabled-mode list is intentionally broader and does not describe the enabled mapping. Historical archived artifacts were not modified.

All five values originate only in `CLIENT_CA_ENVIRONMENT` and flow to the build helper, direct run rendering, standalone npm rendering, and runtime policy verification. Regression assertions cover the closed local/reviewed schema, generated projections, evidence owners and payloads, fetch/digest/cache/dependency identity owners, and Dockerfile `ARG`/`ENV` metadata. Host source paths and certificate contents are excluded; only the fixed in-container destination is observable in policy diagnostics.

### Final focused validation

- `python -m unittest tests.test_constructor_corporate_network_docs_red tests.test_constructor_corporate_network_boundaries_red tests.test_constructor_corporate_network_build_red tests.test_constructor_corporate_network_verification_red -v`
  - **Passed:** 58 tests covering documentation, closed schema, disclosure, build policy, runtime policy diagnostics, and image-metadata exclusion.
- `python -m unittest tests.test_constructor_build_projection tests.test_constructor_build_digest_identity tests.test_npm_environment_identity -v`
  - **Passed:** 86 projection and identity tests.
- `python -m unittest tests.test_constructor_evidence_collector -v`
  - **Passed:** 49 evidence payload, redaction, inspection, and output tests.
- `npx pi-green-loop check --since HEAD`
  - **Passed:** project typecheck and test checks (the configured test check completed in 84.076 seconds).

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

## Phase 3 — Runtime Verification and Orchestration

### RED baseline

- `python -m unittest tests.test_constructor_corporate_network_verification_red -v`
  - **Failed:** 2 of 10 tests, both at the missing runtime-verification
    environment boundary (no `corporate-trust.environment` checks existed):
    - `TestEnabledClientCaRuntimeVerificationRed.test_reports_each_variable_with_exact_fixed_value`
    - `TestEnabledClientCaRuntimeVerificationRed.test_missing_and_mismatched_variables_reported_individually`
  - **Passed unchanged (8):** the orchestration mount/environment contract,
    the disabled non-injection guards, the malformed-configuration pre-effect
    regressions, and the disclosure/no-TLS boundaries. The orchestration
    wiring (3.1, 3.5) and the malformed pre-effect rejection (3.4, 3.8) were
    already delivered by the Phase 2 launch propagation and the existing
    fail-closed resolution; their Phase 3 tests are regression guards and
    therefore passed at RED while the client CA verification tests failed.

### Introspection

**One resolved trust decision (3.5).** `docker/launcher.py` resolves
`corporate_trust_bundle` exactly once, only when
`local_corporate.corporate_trust.enabled` is true and the fixed host bundle
validates, and passes that single value into `RunRenderInputs`. The renderer
derives the enabled/disabled decision from `corporate_trust_bundle is not None`;
no second boolean or duplicate trust source exists. The same decision is
resolved once for verification through `_resolve_verify_corporate_network`.

**Fixed, non-disclosing diagnostics (3.9).** `verify_runtime` imports
`CLIENT_CA_ENVIRONMENT` and `SYSTEM_CA_BUNDLE` from
`docker/versioning/corporate_network.py`; it never receives the host bundle
source and has no field for it. Each check is exactly
`docker exec <container> printenv <fixed-name>` and its raw stdout is the fixed
in-container path. The new assertions pin that check keys, details, command
vectors, and raw structured fields contain only the five fixed variable names
and `/etc/ssl/certs/ca-certificates.crt`, never `.docker-local`,
`corporate-ca-bundle`, certificate contents, or host paths.

**No TLS or validity claim (3.10).** Every verification command remains a
targeted `docker exec`; none invokes `openssl`, `gnutls-cli`, `curl`, `wget`,
`nc`, `ncat`, or `telnet`, and none uses `s_client` or `/dev/tcp`. The client CA
details contain no claim about certificate validity, connectivity, replacing
Node's roots, or augmentation behavior. Verification observes environment
values only; it makes no external request.

**Disabled policy is a no-op (3.3, 3.7).** With trust disabled no client CA
variable is inspected, no `corporate-trust.environment` check is emitted, and
conflicting values inherited from the base image are neither required to be
absent nor reported as enabled policy. The existing read-only mount check and
its disabled absence check are retained unchanged.

### Final focused validation

- `python -m unittest tests.test_constructor_corporate_network_verification_red`
  - **Passed:** 10 tests. Enabled per-variable reporting, missing/mismatched
    reporting with retained mount, disabled non-requirement and inherited-value
    tolerance, orchestration mount + five assignments, malformed pre-effect
    rejection, and disclosure/no-TLS boundaries.
- `python -m unittest tests.test_constructor_corporate_network_run_red`
  - **Passed:** 28 tests. Prior runtime launch/verification contracts remain green.
- `python -m unittest tests.test_constructor_corporate_network_acceptance_red`
  - **Passed:** 14 tests. End-to-end configured/disabled dry-run acceptance.
- `python -m unittest tests.test_constructor_corporate_network_red`
  - **Passed:** 52 tests. Corporate trust/proxy configuration and fail-closed behavior.
- `python -m unittest tests.test_constructor_runtime_verification`
  - **Passed:** 34 tests. Full runtime verification contract.
- `python -m unittest tests.test_constructor_host_access_verify_red`
  - **Passed:** 16 tests. Host-access verification independence.
- `python -m unittest tests.test_constructor_host_access_acceptance`
  - **Passed:** 10 tests. Host-access policy independence.
- `python -m unittest tests.test_npm_environment_corporate_network`
  - **Passed:** 29 tests. Standalone assembler client CA parity.
- `python -m unittest tests.test_constructor_facade`
  - **Passed:** 162 tests. Public facade regression coverage.
- `ty check docker --python-version 3.14 --output-format concise`
  - **Passed:** all checks passed.
- `python -m unittest discover -s tests -p 'test_*.py'`
  - **Passed:** 4489 tests, 13 skipped.

### Hardening addendum — exact values and non-disclosure

Two follow-up defects were found in the first Phase 3 implementation and
fixed:

1. **Whitespace was silently normalized.** The loop used
   `r.stdout.strip()`, accepting leading/trailing whitespace as a valid CA
   path. It now removes only the single terminating newline added by
   `printenv`, so `" /etc/ssl/certs/ca-certificates.crt"`,
   `"/etc/ssl/certs/ca-certificates.crt "`, and
   `"/etc/ssl/certs/ca-certificates.crt\nextra"` are exact mismatches.
2. **Mismatched values were disclosed.** The failure detail interpolated
   the observed value and the raw `printenv` stdout/stderr flowed into the
   structured result; a hostile value could expose a host path or PEM
   certificate contents. Mismatch (and missing) client CA checks now use a
   fixed message (`<name> does not equal expected <fixed path>` /
   `<name> is not set (expected <fixed path>)`) and clear `raw_stdout` and
   `raw_stderr` while preserving `command` and `exit_code`.

Regression coverage added in
`tests/test_constructor_corporate_network_verification_red.py`:

- `TestEnabledClientCaRuntimeVerificationRed.test_whitespace_and_extra_content_values_are_exact_mismatches`
  (five variants: leading space/tab, trailing space, trailing carriage
  return, embedded extra line) — each fails its own variable while the other
  four and the mount check remain green.
- `TestEnabledClientCaRuntimeVerificationRed.test_mismatched_value_is_redacted_from_detail_and_structured_output`
  — a value containing `.docker-local`, `corporate-ca-bundle`, and PEM
  certificate text is absent from `detail`, `raw_stdout`, and `raw_stderr`,
  while the fixed variable name and expected system path remain in the
  detail and the command/exit code are preserved.

Re-validation after the hardening:

- `python -m unittest tests.test_constructor_corporate_network_verification_red`
  - **Passed:** 12 tests.
- `python -m unittest discover -s tests -p 'test_*.py'`
  - **Passed:** 4491 tests, 13 skipped.
- `ty check docker --python-version 3.14 --output-format concise`
  - **Passed:** all checks passed.

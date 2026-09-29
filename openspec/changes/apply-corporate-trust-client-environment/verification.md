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

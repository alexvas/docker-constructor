# Phase 1 Verification Evidence

## 2026-09-26

Passed:

```sh
./scripts/check-types
python -m unittest tests.test_output_configuration tests.test_local_project_configuration_phase2 tests.test_constructor_build_output tests.test_constructor_pi_assembly tests.test_npm_environment_streaming tests.test_host_presentation_phase9
python - <<'PY'
from docker.versioning.inventory import load_project_configuration
from docker.versioning.model import NetworkUrlDisplay
_, local = load_project_configuration('docker-constructor.toml')
assert local.output.network_url_display is NetworkUrlDisplay.HOST_PATH
print('local network_url_display: host-path')
PY
git diff --check
```

Results: `All checks passed!`; 215 unittest tests passed; the project-local companion resolved to `network_url_display: host-path`; diff check passed.

The focused suite covers closed local `[output]` parsing and rejection, default `redacted` resolution, host-only output-policy confinement, host-side request DTO propagation, exported SDK-boundary defaulting/rejection before effects, and host-presentation enum selection. The audit confirms `network_url_display` appears only in local parsing/facade rendering, the host materialization and Pi assembly DTOs, the direct assembly API, and collector configuration; it is absent from semantic assembler identity, cache identity, evidence, Docker argv, reviewed/effective projections, and container inputs.

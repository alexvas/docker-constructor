# Phase 4 Verification — Host Output Configuration

Change: `improve-host-build-observability`
Phase: 4 (tasks 4.1–4.7)
Date: 2026-09-19
Scope: host-only `[output]` local-companion configuration and its confinement
to facade presentation construction.

## Deliverables

- `docker/versioning/build_output.py` — `parse_local_output_policy`, the sole
  owner of `[output]` field validation and defaults.
- `docker/versioning/model.py`
  - `LocalOutputPolicy` — immutable host-only presentation policy
    (`host_heartbeat`, `show_network_hosts`).
  - `BuildLocalInputs` — the only local-companion projection allowed into build
    planning (`corporate_trust_enabled`, `cache_dir`, `network_proxy_url`,
    `network_proxy_no_proxy`); it deliberately excludes `[output]`.
- `docker/versioning/local_project_configuration.py` — registers `[output]` in
  the closed aggregate table set.
- `docker/constructor_cli.py` — one shared configuration transaction per
  build/run/verify command; `[output]` reaches only `_HostEventRenderer`
  construction.
- `docker/versioning/readonly_service.py` — update discovery receives the
  cache-owned slice (`_local_cache`), never the aggregate `LocalConfig`.
- `docker/versioning/build_orchestration.py` — `plan_build`/`orchestrate_build`
  accept `inventory` + `BuildLocalInputs`; no aggregate and no output policy.

## Production-path coverage

Every boundary below is exercised through the **real dispatch/orchestration
path** with external effects faked. No boundary fixture is a hand-built
policy-free object: the project fixture writes a companion with nondefault
output (`host_heartbeat = "off"`, `show_network_hosts = true`) and a valid
`[cache] dir`, and reloads it through the production
`load_project_configuration`.

| Boundary | Production path exercised | External effects faked | Captured production request |
| --- | --- | --- | --- |
| Build planning + execution | `orchestrate_build` | artifact materializer, Pi materializer, transport factory, snapshot, `docker build` runner | rendered `docker build` argv, materializer kwargs, Pi/assembler kwargs, published `EffectiveBuildProjection`, `HostNetworkPolicy` |
| Build-context confinement | `plan_build_context_confinement` + `materialize_build_context_confinement` inside `execute_build` | none (real confinement, generated files inspected before cleanup) | confinement plan and generated Dockerfile-specific ignore body |
| Run | `orchestrate_run` (dry-run renders the full vector) | none for rendering; project state pre-created | rendered `docker run` argv, effective runtime projection |
| Verify | facade `constructor_cli.main(["verify", ...])` | `verify_build`/`verify_runtime` Docker boundaries | `VerifyBuildRequest`, `VerifyRuntimeRequest` |
| Reviewed serialization / update discovery | `serialize_effective_inventory`, `load_inventory_raw`, `build_update_targets` | none | serialized inventory, raw document, update targets |
| Read-only update discovery | `readonly_service.dispatch(..., "check-updates")` | `check_updates`, `build_transports` | handler arguments and transport kwargs |

## Explicit string, argument, and exclusion assertions

`assert_no_presentation_policy` walks `Mapping`/`__dict__`/sequences and skips
strings, so it cannot detect settings or companion paths leaked into rendered
text. It is therefore always paired with `assert_no_policy_strings`, which
rejects (case-insensitively) `host_heartbeat`, `show_network_hosts`,
`host-heartbeat`, `show-network-hosts`, `[output]`, and
`docker-constructor.local.toml` in:

- the rendered `docker build` argument vector and the rendered `docker run`
  argument vector (including `--mount`/`--file`/`--build-arg` values),
- the published effective build projection (`serialize_effective_build`) and
  the effective runtime projection (`to_plain_data`), JSON-serialized; these
  two additionally reject the companion's cache directory and `[cache]` table
  so no companion content (not only its output table) is reproduced,
- the reviewed inventory serialization, raw inventory document, and update
  targets,
- `repr()` of the captured `VerifyBuildRequest`/`VerifyRuntimeRequest`.

Additional structural assertions:

- The build renders the **generated** Dockerfile copy (`--file` target differs
  from the project Dockerfile) and never the project Dockerfile path.
- `plan.relative_documents`, `confinement_ignore_rules(plan)`, and the
  generated `<Dockerfile>.dockerignore` body all name
  `docker-constructor.local.toml`, so the companion is excluded from the
  effective context.
- Neither build nor run argv contains the companion path, and the verify
  request does not reference it.
- `test_no_cli_or_environment_alias_exists_for_output_settings` proves the
  reviewed surface has no CLI or environment alias: the parser rejects
  `--host-heartbeat`/`--show-network-hosts`, and a polluted environment
  (`HOST_HEARTBEAT=off`, `SHOW_NETWORK_HOSTS=true`) still yields the immutable
  defaults.

## Event-production independence

`test_domain_event_production_is_independent_of_output_policy` runs two full
`orchestrate_build` transactions whose companions differ only in `[output]`
(`off`/`true` versus `lines`/`false`). Both succeed, the resolved policies
differ, and the captured structured `HostPhaseEvent` sequences are identical.
Presentation remains separately controlled: orchestration only ever receives
`inventory` + `BuildLocalInputs` and an output-free event sink, while the facade
constructs `_HostEventRenderer(output_policy=...)` (covered by
`TestFacadeOutputPolicyDispatch`).

## Assertion sensitivity

Each boundary assertion is proven sensitive by deliberately introducing a leak
at that production boundary and confirming the assertion fails:

| Test | Injected leak |
| --- | --- |
| `test_build_argv_checker_detects_a_companion_mount` | `render_build_vector` appends a companion bind mount |
| `test_build_argv_checker_detects_policy_settings` | `render_build_vector` appends `HOST_HEARTBEAT=off` |
| `test_serialized_projection_checker_detects_output_settings` | `serialize_effective_build` injects an `output` mapping |
| `test_confinement_checker_detects_a_dropped_companion_exclusion` | `plan_build_context_confinement` drops the companion exclusion |
| `test_run_argv_checker_detects_a_companion_mount` | `render_run_vector` appends a companion bind mount |
| `test_verify_checker_detects_companion_forwarding` | `_resolve_verify_corporate_network` forwards the companion path as `proxy_url` |

## Read-only update-discovery regression (RED → GREEN)

`readonly_service.py` previously stored the aggregate `LocalConfig` in
`handler_args["_local_config"]`, which carried `[output]` into update discovery.
It now stores only the cache-owned slice in `handler_args["_local_cache"]`;
`_handle_check_updates` reads `_local_cache` and `build_transports` receives
only `LocalCacheConfig`.

RED evidence (old wiring restored temporarily):

```
$ python -m unittest tests.test_output_configuration.TestReadOnlyUpdateDiscoveryConfinement
KeyError: '_local_cache'
Ran 1 test in 0.013s
FAILED (errors=1)
```

GREEN evidence (current wiring):

```
$ python -m unittest tests.test_output_configuration.TestReadOnlyUpdateDiscoveryConfinement
Ran 1 test in 0.010s
OK
```

`test_companion_read_reaches_update_discovery_only_as_a_cache_slice` loads a
companion with a valid `[cache] dir` and nondefault `[output]`, then asserts
that `_local_config` is absent, `_local_cache` equals
`LocalCacheConfig(<companion cache dir>)` (cache behaviour preserved), and
neither handler arguments nor `build_transports` inputs contain a `LocalConfig`,
`LocalOutputPolicy`, or either output setting.

## Commands and results

```
$ python -m unittest tests.test_output_configuration
Ran 26 tests in 0.599s
OK

$ python -m unittest \
    tests.test_output_configuration tests.test_constructor_readonly \
    tests.test_constructor_check_updates_acceptance tests.test_version_readonly_no_effect \
    tests.test_version_updates tests.test_constructor_build_projection \
    tests.test_constructor_runtime_projection tests.test_host_diagnostic_projection \
    tests.test_build_context_confinement tests.test_container_workspace_contract \
    tests.test_constructor_run_vector tests.test_constructor_build_orchestration \
    tests.test_local_project_configuration_phase2 tests.test_malformed_local_state_phase5
Ran 630 tests in 4.258s
OK

$ python -m unittest \
    tests.test_constructor_buildkit_cache tests.test_cache_release_ordering_phase3 \
    tests.test_cache_root_ownership_phase3 tests.test_constructor_build_cache_paths \
    tests.test_constructor_build_cache_permissions tests.test_constructor_cache_contracts \
    tests.test_version_cache
Ran 141 tests in 1.098s
OK (skipped=1)

$ python -m unittest discover -s tests -p 'test_*.py'
Ran 3756 tests in 29.598s
OK (skipped=13)

$ ty check docker --python-version 3.14 --output-format concise
All checks passed!

$ git diff HEAD --check
(clean)

$ openspec validate improve-host-build-observability --json
valid: true, issues: []
```

## Confinement guarantees recorded

- Defaults: `host_heartbeat = "interactive"`, `show_network_hosts = false`.
- Closed surface: `host_heartbeat` accepts exactly `interactive`, `lines`, `off`;
  `show_network_hosts` must be boolean.
- Reviewed `docker-constructor.toml` rejects both output keys before effects.
- No CLI option, environment variable, or runtime projection alias exists for
  either setting.
- The companion, `[output]` table, and derived policy are never serialized,
  discovered, projected, mounted, or forwarded into a domain request.
- The companion file is excluded from the effective build context through the
  generated Dockerfile-specific ignore file.

## Files changed (this verification pass)

- `docker/versioning/readonly_service.py` (cache-slice boundary; see the
  RED → GREEN section)
- `tests/test_output_configuration.py`
- `openspec/changes/improve-host-build-observability/verification-phase4.md`

No specification file was edited. Phase 4 tasks 4.1–4.7 remain checked because
the promised boundaries above are now all exercised by passing tests.

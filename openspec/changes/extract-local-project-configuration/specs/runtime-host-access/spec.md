## REMOVED Requirements

### Requirement: Store constructor-project machine-local state separately
**Reason**: Aggregate ownership of `docker-constructor.local.toml` mixes host-access behavior with cache and corporate-network concerns and incorrectly places a host-only project input under a runtime capability.

**Migration**: `local-project-configuration` owns companion resolution, closed composition, aggregate validation, reviewed-state isolation, and host-only confinement. Cache clauses move to `user-cache-storage` and reviewed/cache source separation remains in `docker-build-reproducibility`; this capability retains only `[host-access]` semantics and explicitly authorized derived launch values.

## ADDED Requirements

### Requirement: Consume host-access state through the host-only local companion
Runtime host-access planning SHALL consume machine-local address state only from `[host-access]` in the host-only companion defined by `local-project-configuration`. Reviewed `[runtime.host-access]` SHALL continue to own portable enablement, mode, and proxy-port policy. Local host-access state SHALL NOT override reviewed policy or require cache or corporate-network configuration.

The system MAY derive and pass only `HOST_ACCESS_ADDRESS`, `HOST_PROXY_PORT`, host mappings, and launch arguments explicitly authorized by the runtime host-access contract. It SHALL NOT copy, mount, publish, serialize, or otherwise expose `docker-constructor.local.toml`, its path, its original representation, or unrelated local tables to the runtime container or effective runtime dependency projection.

#### Scenario: Resolving enabled launch state
- **WHEN** reviewed runtime host access is enabled and valid local address state exists
- **THEN** launch planning SHALL derive only the mapping and environment values authorized by the selected host-access mode
- **AND** SHALL NOT expose the companion or unrelated local configuration to the container

#### Scenario: Host access is disabled
- **WHEN** reviewed runtime host access is disabled
- **THEN** local `[host-access]` state SHALL produce no host mapping or host-access environment variables
- **AND** cache and corporate-network settings SHALL remain independently usable by their owning capabilities

#### Scenario: Runtime projection is produced
- **WHEN** an effective runtime dependency projection is serialized or mounted
- **THEN** it SHALL exclude host-access mode, host address, proxy port, local companion paths, and aggregate local state
- **AND** the local companion SHALL NOT be mounted or copied separately

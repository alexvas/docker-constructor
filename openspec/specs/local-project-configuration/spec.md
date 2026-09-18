# Capability: local-project-configuration

## Purpose

Define the single host-side machine-local configuration companion for a constructor project, including fixed resolution, closed composition of domain-owned tables, aggregate validation, reviewed-state isolation, and confinement outside build and runtime containers.

## Requirements

### Requirement: Resolve one project-local configuration companion
The system SHALL resolve `docker-constructor.local.toml` directly beneath the selected constructor project directory as the sole machine-local configuration companion. It SHALL NOT resolve the companion from the tool installation, an ancestor directory, a workspace, or a custom inventory basename. The companion MAY be absent, and consumers SHALL apply their domain-owned defaults without requiring its creation.

#### Scenario: Resolving the selected project's companion
- **WHEN** a constructor project directory is selected from CWD or explicit `--project-directory`
- **THEN** the local companion SHALL be `<project-directory>/docker-constructor.local.toml`
- **AND** no other directory or basename SHALL be searched

#### Scenario: Local companion is absent
- **WHEN** the selected project has no `docker-constructor.local.toml`
- **THEN** local configuration resolution SHALL succeed with domain-owned defaults
- **AND** SHALL NOT create the companion

### Requirement: Compose closed domain-owned local tables
The local companion SHALL accept only `[host-access]`, `[cache]`, `[corporate-trust]`, and `[network.proxy]`. Each accepted table's fields, defaults, validation, and behavioral effects SHALL remain governed by its owning capability. The aggregate resolver SHALL compose those closed schemas without introducing cross-table dependencies: cache, corporate-trust, and proxy configuration SHALL remain usable without enabled host access, and corporate-network settings SHALL neither derive an application proxy URL from `HOST_ACCESS_ADDRESS` or `HOST_PROXY_PORT` nor alter reviewed host-access policy.

#### Scenario: Independent local concerns are composed
- **WHEN** a valid companion contains any supported combination of host-access, cache, corporate-trust, and proxy tables
- **THEN** the aggregate resolver SHALL validate each table under its owning capability
- **AND** one table SHALL NOT require another unless its owning capability explicitly requires that dependency

#### Scenario: Corporate network configuration is independent from host access
- **WHEN** host access is disabled and the companion declares valid corporate trust or an external `[network.proxy]` endpoint
- **THEN** build and run planning SHALL accept the corporate-network configuration
- **AND** SHALL NOT emit host-access mappings, require gateway diagnosis, or derive an application proxy URL from host-access values

### Requirement: Reject invalid aggregate local configuration before effects
After successful shared document parsing, the aggregate local resolver SHALL reject unknown top-level tables, unknown or misplaced fields, and any table value rejected by its owning capability. Parsing, diagnostics, and validation-before-effects SHALL conform to `configuration-document-validation`; local validation SHALL NOT implement a second TOML syntax or duplicate-definition boundary.

#### Scenario: Aggregate local schema is invalid
- **WHEN** the parsed companion contains an unknown top-level table, an unknown or misplaced field, or a value rejected by its owning capability
- **THEN** resolution SHALL fail before network access, cache mutation, artifact materialization, container execution, or Docker execution
- **AND** the error SHALL identify the local document path and invalid field without exposing unrelated values

#### Scenario: Local TOML syntax is invalid
- **WHEN** the companion cannot be parsed or contains a duplicate TOML definition
- **THEN** the shared configuration-document boundary SHALL reject it before aggregate or domain validation
- **AND** the local resolver SHALL NOT duplicate parser-level validation

### Requirement: Keep local configuration separate from reviewed state
The local companion SHALL remain machine-local and SHALL NOT override reviewed dependency, update, artifact, host-access policy, or cache-TTL fields. Its path, original TOML representation, and aggregate contents SHALL NOT be included in reviewed inventory serialization, update discovery, or effective build or runtime dependency projections. A table's owning capability MAY derive only the minimal host-side policy, build input, or launch value explicitly authorized by that capability.

#### Scenario: Producing reviewed and effective projections
- **WHEN** reviewed inventory serialization, update discovery, effective build projection, or effective runtime projection is produced
- **THEN** the local companion path, original representation, and aggregate contents SHALL be absent
- **AND** only values explicitly authorized by an owning capability MAY affect subsequent host-side planning

### Requirement: Keep the local companion host-only
The resolved local companion SHALL remain a host-side Constructor input. The system SHALL NOT copy, mount, publish, serialize, add to a build context, or otherwise expose the companion file or its aggregate contents to a build or runtime container. A consuming capability MAY pass only a minimal derived value or separate project-owned input explicitly authorized by that capability; it SHALL NOT expose the companion path, unrelated tables, or original TOML representation.

#### Scenario: Building with local configuration
- **WHEN** host-side build planning consumes cache or corporate-network settings from the companion
- **THEN** the companion SHALL NOT be copied, mounted, or added to the build context or build container
- **AND** only capability-authorized derived inputs or separate fixed project-owned files MAY be passed onward

#### Scenario: Launching with local host-access state
- **WHEN** runtime host-access planning consumes machine-local address state
- **THEN** it MAY pass separately authorized host mappings, `HOST_ACCESS_ADDRESS`, `HOST_PROXY_PORT`, or launch arguments
- **AND** SHALL NOT expose the companion or unrelated local settings to the runtime container

#### Scenario: Publishing an effective runtime projection
- **WHEN** the effective runtime projection is serialized or mounted
- **THEN** it SHALL exclude the companion path, aggregate local configuration, cache paths, and unrelated machine-local values

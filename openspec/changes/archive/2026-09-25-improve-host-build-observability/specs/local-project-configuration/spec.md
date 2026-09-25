## MODIFIED Requirements

### Requirement: Compose closed domain-owned local tables
The local companion SHALL accept only `[host-access]`, `[cache]`, `[corporate-trust]`, `[network.proxy]`, and `[output]`. Each accepted table's fields, defaults, validation, and behavioral effects SHALL remain governed by its owning capability. `[output]` field validation and semantics SHALL remain owned by `docker-build-output`. The aggregate resolver SHALL compose those closed schemas without introducing cross-table dependencies: cache, corporate-trust, proxy, and output configuration SHALL remain usable without enabled host access, and corporate-network settings SHALL neither derive an application proxy URL from `HOST_ACCESS_ADDRESS` or `HOST_PROXY_PORT` nor alter reviewed host-access policy. Every other top-level table SHALL remain unknown and SHALL be rejected.

#### Scenario: Independent local concerns are composed
- **WHEN** a valid companion contains any supported combination of host-access, cache, corporate-trust, proxy, and output tables
- **THEN** the aggregate resolver SHALL validate each table under its owning capability
- **AND** one table SHALL NOT require another unless its owning capability explicitly requires that dependency

#### Scenario: Corporate network configuration is independent from host access
- **WHEN** host access is disabled and the companion declares valid corporate trust or an external `[network.proxy]` endpoint
- **THEN** build and run planning SHALL accept the corporate-network configuration
- **AND** SHALL NOT emit host-access mappings, require gateway diagnosis, or derive an application proxy URL from host-access values

#### Scenario: Valid output configuration is accepted by the aggregate resolver
- **WHEN** the local companion declares a valid `[output]` table
- **THEN** the aggregate local resolver SHALL accept the table
- **AND** SHALL delegate its field validation and semantics to `docker-build-output`

#### Scenario: Output configuration remains independent
- **WHEN** the local companion declares valid `[output]` configuration with or without any supported host-access, cache, corporate-trust, or proxy table
- **THEN** the aggregate local resolver SHALL accept each declared table independently under its owning capability
- **AND** `[output]` SHALL NOT require or alter another local table
- **AND** another local table SHALL NOT require or alter `[output]`

#### Scenario: Unknown top-level tables remain rejected
- **WHEN** the parsed local companion contains a top-level table other than `[host-access]`, `[cache]`, `[corporate-trust]`, `[network.proxy]`, or `[output]`
- **THEN** the aggregate local resolver SHALL reject the unknown table before effects

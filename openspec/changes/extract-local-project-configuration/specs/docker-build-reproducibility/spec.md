## ADDED Requirements

### Requirement: Validate reviewed inventory through the shared document boundary
The reviewed `docker-constructor.toml` SHALL be parsed and diagnosed through `configuration-document-validation` before the reviewed inventory schema is applied. The reviewed inventory owner SHALL retain responsibility for required-document handling, accepted sections and fields, value semantics, defaults, and effective projections; it SHALL NOT implement an independent TOML syntax, duplicate-definition, or parse-error projection path.

#### Scenario: Reviewed inventory TOML is malformed
- **WHEN** the selected `docker-constructor.toml` cannot be parsed
- **THEN** the shared configuration-document boundary SHALL reject it with a typed error containing the reviewed document role and path, fixed `malformed_toml` classification, and available numeric line/column coordinates
- **AND** SHALL NOT expose the raw parser message or source-document excerpt
- **AND** reviewed inventory schema validation and external effects SHALL NOT begin

#### Scenario: Reviewed inventory field is invalid
- **WHEN** TOML parsing succeeds and the reviewed schema rejects a field
- **THEN** the error SHALL identify the reviewed document path and invalid field without exposing unrelated values

## MODIFIED Requirements

### Requirement: Keep cache directory paths machine-local
The reviewed `docker-constructor.toml` SHALL NOT accept `cache.dir`; a custom constructor cache root SHALL be selected only from `[cache].dir` in the resolved host-only companion. Cache-root defaults, normalization, safety validation, permissions, and child containment SHALL conform to `user-cache-storage`. Neither the local cache root nor reviewed `cache.ttl` SHALL enter an effective build or runtime dependency projection. Reviewed `cache.ttl` SHALL remain supported in `docker-constructor.toml` as portable HTTP cache policy.

#### Scenario: Migrating a reviewed cache directory
- **WHEN** validation encounters `cache.dir` in `docker-constructor.toml`
- **THEN** it SHALL reject the retired field with an instruction to move the value to the corresponding local companion
- **AND** SHALL NOT silently copy, merge, or prefer the reviewed path

#### Scenario: Resolving cache settings from separate sources
- **WHEN** reviewed `cache.ttl` and local `[cache].dir` are both configured
- **THEN** cache consumers SHALL use the reviewed TTL and the normalized validated local cache root together
- **AND** neither value SHALL enter an effective build or runtime projection

#### Scenario: Separating cache formats under a local root
- **WHEN** a local `[cache].dir` is configured
- **THEN** update-discovery HTTP cache data SHALL use its `versioning` child
- **AND** verified runtime artifacts SHALL use its `runtime-artifacts/blobs` child

#### Scenario: Rejecting the removed HTTP-only command-line cache directory
- **WHEN** a user supplies unsupported `check-updates --cache-dir`
- **THEN** command-line parsing SHALL reject the supplied option as unsupported
- **AND** SHALL NOT interpret it, select a cache path, or migrate cache data

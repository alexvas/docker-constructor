# Spec Delta

## Purpose

Provide a complete, profile-aware local inventory of tests with durable identities and reproducible source coordinates.

## ADDED Requirements

### Requirement: Profile-aware complete inventory
The catalog SHALL register all tests collected in declared profiles, including skipped and parametrized cases, independently of semantic review status. It SHALL report collection completeness per profile.

#### Scenario: Initial collection
- **WHEN** a profile is successfully collected
- **THEN** every collected case is registered with its profile and a logical-test relationship where applicable, initially marked unreviewed

#### Scenario: Collection failure or profile difference
- **WHEN** collection fails or a test is absent from another profile
- **THEN** the catalog records incomplete or profile-specific presence without declaring the test deleted

### Requirement: Durable identity and versioned addresses
The catalog SHALL assign stable internal IDs and retain address history including path, qualified class/method/function when available, nodeid and source ranges. Addresses SHALL identify the source state, file content hash and Git commit/blob when available.

#### Scenario: Uncommitted source
- **WHEN** a test is registered from a dirty working tree
- **THEN** its address includes the base commit and working-state fingerprint without falsely attributing that content to a commit

#### Scenario: Ambiguous rename
- **WHEN** a possible rename cannot be uniquely established
- **THEN** the catalog preserves historical identity and requests confirmation rather than silently merging records

### Requirement: Durable project-local storage
The catalog SHALL reside in project-local SQLite excluded from Git, including sidecar files. Cards SHALL not require Git-review. Versioned migrations, consistent backup and portable export/import SHALL preserve IDs, observations and review metadata.

#### Scenario: Storage upgrade and recovery
- **WHEN** storage is upgraded or restored
- **THEN** the operation preserves existing records or fails without partially replacing the usable catalog

#### Scenario: Concurrent writers
- **WHEN** independent commands attempt incompatible updates
- **THEN** writes are serialized or fail explicitly without corrupting data or reporting an incomplete write as successful

### Requirement: Independent review dimensions
Cards SHALL distinguish discovery, semantic review progress, relevance and evidence freshness. Missing descriptions or Scenario links SHALL be explicit unknowns rather than evidence of obsolescence.

#### Scenario: Newly discovered test
- **WHEN** no semantic analysis exists for a registered test
- **THEN** it remains in the complete catalog with unreviewed and unknown relevance states

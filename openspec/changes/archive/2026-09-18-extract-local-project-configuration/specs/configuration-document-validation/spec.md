## Purpose

Provide one consistent validation boundary for the two fixed constructor-project TOML documents, including parsing, document identity, safe error projection, and rejection before externally visible effects.

## ADDED Requirements

### Requirement: Validate both project configuration documents through one boundary
The system SHALL process both `docker-constructor.toml` and `docker-constructor.local.toml` through one configuration-document validation boundary before applying their document-specific schemas. The boundary SHALL retain the affected document's role and resolved path, decode and parse it with Python's standard-library `tomllib`, and return no parsed or partially validated document when parsing fails. Document owners SHALL continue to define whether a document is required or optional and SHALL own its allowed fields, value semantics, defaults, and cross-field constraints.

#### Scenario: Parsing the reviewed inventory
- **WHEN** a command reads `docker-constructor.toml`
- **THEN** it SHALL route the resolved reviewed-document path through the shared validation boundary
- **AND** SHALL apply the reviewed inventory schema only after TOML parsing succeeds

#### Scenario: Parsing the local companion
- **WHEN** a command reads an existing `docker-constructor.local.toml`
- **THEN** it SHALL route the resolved local-document path through the same validation boundary
- **AND** SHALL apply local aggregate and domain schemas only after TOML parsing succeeds

### Requirement: Expose typed field-or-syntax configuration errors
The configuration-document boundary MAY inspect the original parser exception internally but SHALL expose only a typed configuration error containing the affected document role and path, a fixed error classification, and either the invalid field when one is available or numeric line and column coordinates when `tomllib` provides them. When no field or numeric syntax location is available, the typed error SHALL retain only the fixed `malformed_toml` classification. It SHALL NOT expose the parser's raw message, source line, token, parsed value, document excerpt, original exception as publishable diagnostic data, or unrelated configuration values.

TOML-path uniqueness and table/value structural consistency SHALL be enforced by `tomllib`; Constructor SHALL NOT accept a lossy last-write-wins representation. This requirement SHALL NOT require project-owned duplicate-detection logic or exhaustive repetition of standard-library parser conformance tests. Callers SHALL own text/JSON/SDK presentation and MAY apply additional defense-in-depth redaction, but SHALL render only the typed error fields exposed by this boundary.

#### Scenario: Reviewed document has malformed syntax
- **WHEN** `docker-constructor.toml` cannot be parsed and no field path is available
- **THEN** the typed error SHALL identify the reviewed document role and path, fixed `malformed_toml` classification, and numeric line/column when available
- **AND** SHALL NOT claim an invalid field or expose the raw parser message, source excerpt, token, value, or unrelated data

#### Scenario: Local document has malformed syntax
- **WHEN** `docker-constructor.local.toml` cannot be parsed and no field path is available
- **THEN** the typed error SHALL identify the local document role and path, fixed `malformed_toml` classification, and numeric line/column when available
- **AND** SHALL NOT claim an invalid field or expose the raw parser message, source excerpt, token, value, or unrelated data

#### Scenario: Document schema identifies an invalid field
- **WHEN** parsing succeeds and document-specific validation rejects a known field
- **THEN** the typed error SHALL identify the affected document role, path, fixed schema-error classification, and invalid field
- **AND** SHALL NOT expose the rejected value or unrelated data

#### Scenario: TOML contains a duplicate definition
- **WHEN** either project configuration document contains a representative duplicate TOML definition
- **THEN** `tomllib` SHALL reject the document before a lossy mapping is returned
- **AND** the failure SHALL expose only the typed document role, path, fixed classification, and available numeric line/column fields

#### Scenario: Caller presents a configuration error
- **WHEN** a CLI, JSON, SDK, or logging caller presents a typed configuration error
- **THEN** it SHALL render only fields exposed by the configuration-document boundary
- **AND** MAY apply additional defense-in-depth redaction
- **AND** SHALL NOT recover or publish the raw parser exception or source document

### Requirement: Reject invalid project configuration before effects
Parsing and applicable document-specific validation SHALL complete before a command performs network access, cache mutation, artifact materialization, container execution, or Docker execution. An invalid reviewed document or invalid present local document SHALL prevent all such effects; validation SHALL NOT publish a valid result for one document while an applicable second document remains invalid.

#### Scenario: Reviewed configuration is invalid
- **WHEN** the reviewed document fails parsing or applicable schema validation
- **THEN** the command SHALL fail before network access, cache mutation, artifact materialization, container execution, or Docker execution

#### Scenario: Local configuration is invalid
- **WHEN** a present local document fails parsing or applicable aggregate or domain validation
- **THEN** the command SHALL fail before network access, cache mutation, artifact materialization, container execution, or Docker execution

#### Scenario: One of two applicable documents is invalid
- **WHEN** one applicable project configuration document is valid and the other is invalid
- **THEN** the command SHALL release no complete configuration result to effectful consumers
- **AND** SHALL perform no external effect

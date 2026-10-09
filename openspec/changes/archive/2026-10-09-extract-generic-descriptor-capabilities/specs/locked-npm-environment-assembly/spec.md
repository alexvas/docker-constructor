## ADDED Requirements

### Requirement: Preserve assembler descriptor ownership and primary failures
Assembler namespace preparation, staging management, manifest construction, and tree verification SHALL use no-follow descriptor authority with explicit ownership transfer and at-most-once release. An ordinary descriptor-close failure during propagation of a domain operation failure SHALL remain secondary to that operation failure; a failed parent-to-child handoff SHALL neither leak the child descriptor nor retry an ambiguously failed parent close. Domain path validation, namespace layout, recursive traversal, cleanup policy, and `LockedNpmError` mapping SHALL remain owned by the assembler.

#### Scenario: Staging setup fails before descriptor release
- **WHEN** staging creation, validation, or permission establishment fails and release of an owned directory descriptor also reports an ordinary close failure
- **THEN** the staging failure SHALL remain the authoritative domain failure
- **AND** the close failure SHALL remain observable as secondary diagnostic context
- **AND** each owned descriptor SHALL receive at most one close attempt

#### Scenario: Secure assembler walk cannot release a parent
- **WHEN** assembler storage opens and validates a child directory but release of its retained parent fails
- **THEN** the child SHALL not be returned or leaked
- **AND** the parent close SHALL not be retried

#### Scenario: Tree traversal fails while releasing a directory
- **WHEN** manifest construction, verification, or staging removal fails while an owned directory still requires release
- **THEN** the traversal or removal failure SHALL remain authoritative over an ordinary close failure
- **AND** assembler-owned recursive traversal and domain error mapping SHALL remain unchanged

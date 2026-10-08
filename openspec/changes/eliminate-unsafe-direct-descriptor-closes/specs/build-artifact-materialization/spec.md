## ADDED Requirements

### Requirement: Preserve build-state descriptor ownership and release precedence
Build-context confinement, artifact-cache traversal, build-cache preparation, snapshot operations, artifact materialization, effective-inventory rendering, persistent cache writing, and external project-state lifecycles SHALL explicitly own every adopted descriptor until transfer or one terminal release attempt. Raw descriptors returned by `tempfile.mkstemp()` or wrapped by `os.fdopen()` SHALL NOT transfer release authority implicitly to a file object. An active build, validation, publication, containment, or integrity failure SHALL remain authoritative over an ordinary close failure. Independent descriptors SHALL each receive one release attempt, and parent-to-child handoff SHALL neither retry an ambiguously failed parent close nor leak an already-open child. Existing project namespaces, cache identities, modes, lock scopes, publication order, retention, durability, and domain error contracts SHALL remain unchanged.

#### Scenario: Private build-context write and close both fail
- **WHEN** writing or validating a private build-context file fails
- **AND** descriptor release reports an ordinary close failure
- **THEN** the build-context failure SHALL remain authoritative
- **AND** the close failure SHALL remain observable as secondary diagnostic context
- **AND** the descriptor SHALL not receive another close attempt

#### Scenario: Releasing several build-cache descriptors
- **WHEN** a build-cache state object owns several independent live descriptors
- **AND** one or more release attempts fail
- **THEN** every owned descriptor SHALL still receive exactly one release attempt
- **AND** repeated lifecycle cleanup SHALL issue no additional close operation
- **AND** final failure selection SHALL follow the shared cleanup precedence

#### Scenario: Build-cache handoff cannot release its parent
- **WHEN** build-cache traversal has opened and validated a child descriptor
- **AND** release of the retained parent fails
- **THEN** parent release SHALL not be retried
- **AND** the child SHALL receive exactly one release attempt
- **AND** the child SHALL not leak or be returned

#### Scenario: Cleanup precedes a build-domain rejection
- **WHEN** an opened descriptor fails type, ownership, containment, or lock-parent validation
- **AND** releasing that descriptor also fails ordinarily
- **THEN** the intended build-domain rejection SHALL remain authoritative
- **AND** the close failure SHALL remain observable as secondary diagnostic context

#### Scenario: Unify a single successful consumer release
- **WHEN** snapshot, artifact-cache, or project-state work succeeds with one descriptor still owned
- **THEN** release SHALL occur through the shared owned-descriptor lifecycle
- **AND** a close failure SHALL remain the sole failure and the lifecycle SHALL become terminal

#### Scenario: Temporary file object fails while its descriptor is owned
- **WHEN** artifact materialization, effective-inventory rendering, or persistent cache writing wraps an owned temporary descriptor as a file object
- **THEN** the wrapper SHALL use `closefd=False` and the shared owner SHALL remain the sole descriptor-release authority
- **WHEN** writing, flushing, or closing that file object fails and descriptor release also fails ordinarily
- **THEN** the file-object operation failure SHALL remain authoritative
- **AND** descriptor release SHALL remain terminal and observable as secondary context
- **AND** publication SHALL NOT occur after the failure

#### Scenario: Preserve primitive and ownership-handoff boundaries
- **WHEN** an existing release is a raw adapter primitive or an ownership handoff already satisfying terminal-parent, at-most-once, and child-cleanup guarantees
- **THEN** migration SHALL preserve its established failure semantics
- **AND** SHALL NOT add a cleanup accumulator around the handoff close solely to eliminate the textual occurrence of a raw close call

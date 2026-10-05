## ADDED Requirements

### Requirement: Preserve cache-storage descriptor ownership and primary failures
Cache-root and descendant preparation SHALL perform secure directory traversal and basename-only child operations through a lightweight descriptor capability boundary that does not import the aggregate durable transaction substrate. Every adopted descriptor SHALL have an explicit owner, receive at most one close attempt, and be released if it cannot be returned. An ordinary close failure SHALL not replace an active cache validation, ownership, mode, creation, or publication failure and SHALL remain available as secondary diagnostic context.

#### Scenario: Cache descendant validation and close both fail
- **WHEN** validation of an opened cache descendant fails and releasing its descriptor reports an ordinary close failure
- **THEN** cache validation SHALL remain the authoritative failure with its existing actionable domain mapping
- **AND** the close failure SHALL remain observable as secondary diagnostic context
- **AND** the descriptor SHALL not be closed again

#### Scenario: Cache walk opens a child but cannot release its parent
- **WHEN** cache traversal opens a child and release of the retained parent fails
- **THEN** the operation SHALL not return or leak the child descriptor
- **AND** both descriptors SHALL receive at most one release attempt

#### Scenario: Cache storage retains its dependency boundary
- **WHEN** cache storage adopts shared descriptor capabilities
- **THEN** it SHALL depend only on a lightweight domain-neutral filesystem foundation
- **AND** SHALL NOT import the aggregate transaction package, transaction locking, regular-file transaction contracts, or another cache consumer

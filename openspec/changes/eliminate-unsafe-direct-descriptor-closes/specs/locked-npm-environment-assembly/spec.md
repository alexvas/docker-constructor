## ADDED Requirements

### Requirement: Preserve npm file-operation failures across descriptor release
Assembler lockfile-input writing and canonical tree-file hashing SHALL own each opened regular-file descriptor through an explicit at-most-once lifecycle. An ordinary descriptor-close failure SHALL NOT replace an active write, stat, type-validation, read, hashing, or npm domain failure, and every descriptor SHALL be released once when it cannot be returned. Existing lockfile bytes, tree manifests, digests, no-follow checks, domain reasons, and diagnostic wording SHALL remain unchanged.

#### Scenario: Lockfile writing and close both fail
- **WHEN** writing the exact lockfile input fails
- **AND** releasing its descriptor reports an ordinary close failure
- **THEN** the original write `OSError` SHALL remain authoritative without conversion to `LockedNpmError`
- **AND** the close failure SHALL remain observable as secondary diagnostic context
- **AND** the descriptor SHALL not be closed again

#### Scenario: Opening a tree-file entry fails
- **WHEN** the no-follow open of a tree-file entry reports `OSError`
- **THEN** `_hash_file_entry()` SHALL preserve the existing `LockedNpmError` with reason `tree_type_mismatch`
- **AND** the original `OSError` SHALL remain its cause

#### Scenario: Tree-file raw operation and close both fail
- **WHEN** tree-file stat or read reports `OSError`, or hashing reports another operation exception
- **AND** releasing the file descriptor reports an ordinary close failure
- **THEN** the exact stat, read, or hashing exception SHALL propagate unchanged without conversion to `LockedNpmError`
- **AND** the close failure SHALL remain observable as secondary diagnostic context
- **AND** canonical traversal, manifest bytes, and tree digest semantics SHALL remain unchanged

#### Scenario: Unsafe tree-file type and close both fail
- **WHEN** tree-file type validation rejects an unsafe entry with `LockedNpmError`
- **AND** releasing the file descriptor reports an ordinary close failure
- **THEN** that exact `LockedNpmError` SHALL remain authoritative
- **AND** the close failure SHALL remain observable as secondary diagnostic context

#### Scenario: Sole npm file release fails
- **WHEN** lockfile writing or tree-file hashing otherwise succeeds
- **AND** its sole descriptor release fails
- **THEN** the exact close `OSError` SHALL propagate unchanged without conversion to `LockedNpmError`
- **AND** the release attempt SHALL remain terminal

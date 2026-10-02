## MODIFIED Requirements

### Requirement: Retain one committed build live set
After and only after Docker successfully builds the image and the constructor successfully removes that transaction's snapshot, the constructor SHALL durably publish the complete selected build-input digest set as a new immutable committed-manifest generation. The newest valid generation SHALL be the sole authoritative committed build live set. The immediately previous generation MAY remain only as durable evidence for incomplete superseded-blob cleanup; it SHALL NOT be treated as an additional live set.

After committing a new generation, the constructor SHALL remove uncommitted markers for every blob admitted to the authoritative generation as one batch and SHALL successfully synchronize the marker directory before superseded cleanup. Marker reconciliation failure SHALL NOT roll back the generation, but SHALL return an operational failure, preserve any existing predecessor, and prevent further cleanup and build work. Discovery SHALL repeat this reconciliation before admitting a build, including marker-directory synchronization when all authoritative-generation markers are already absent.

After authoritative-generation marker reconciliation, the constructor SHALL attempt to remove every blob present in the previous generation and absent from the current generation. It SHALL attempt every candidate even if an earlier deletion fails. Cleanup SHALL batch removals by containing directory and SHALL synchronize each affected existing blob directory once after all removals assigned to it and the shared marker directory once after all marker removals; it SHALL NOT require one directory synchronization per candidate. A candidate SHALL be complete only after its blob and uncommitted marker are absent and those post-batch synchronizations have succeeded, including when recovery finds all entries in a batch already absent. The constructor SHALL durably remove the previous manifest only after every candidate is durably absent. An incomplete cleanup SHALL preserve the previous manifest, report all candidate unlink and synchronization failures, and return an operational failure with process exit code 4 without rolling back the successfully built image or newest committed generation.

Before accepting any discovered newest generation as authoritative, the constructor SHALL successfully synchronize the generation directory under the project build lock. This SHALL complete a generation that survived a failure after no-clobber publication but before parent-directory synchronization; synchronization failure SHALL preserve all generations and blobs, return an operational failure with process exit code 4, and prevent cleanup and build work. Before admitting a later build, the constructor SHALL recover any retained previous generation by repeating the same idempotent cleanup under the project build lock. Recovery failure SHALL return an operational failure with process exit code 4 and SHALL prevent artifact materialization, snapshot creation, Docker execution, or publication of another generation. Malformed or ambiguous generation state SHALL fail closed without deleting blobs or generation manifests. The legacy `committed-build.json` name SHALL be outside generation-state discovery and SHALL NOT be inspected, adopted, rejected, or deleted at runtime.

Snapshot cleanup failure SHALL prevent committed-generation publication and superseded-blob cleanup, preserve the prior authoritative generation and its blobs, and leave newly verified blobs reusable as uncommitted entries tracked by their existing durable markers. Such blobs SHALL remain subject to the fixed 30-day uncommitted TTL unless a later committed generation references them. Shared XDG runtime artifacts SHALL have separate ownership and SHALL NOT be consulted, protected, or deleted by build-cache retention. Existing Docker images SHALL remain independent of source artifact retention, and no image label SHALL be required.

#### Scenario: Committing a successful changed build transaction
- **WHEN** Docker successfully builds an image with a new selected artifact set
- **AND** the constructor successfully removes that transaction's snapshot
- **THEN** the constructor SHALL durably publish that set as the next immutable committed-manifest generation
- **AND** SHALL make the new generation authoritative only after successfully synchronizing its containing directory and before deleting any superseded blob
- **AND** SHALL remove markers for blobs admitted to the new authoritative generation and synchronize the marker directory once
- **AND** SHALL attempt every blob present only in the previous generation only after authoritative-generation marker reconciliation succeeds
- **AND** SHALL batch candidate blob and marker removals by containing directory
- **AND** SHALL synchronize each affected existing blob directory and the shared marker directory once after their complete removal batches
- **AND** SHALL durably remove the previous manifest only after every candidate is durably absent

#### Scenario: Recovering authoritative-generation marker cleanup
- **WHEN** a new generation is authoritative and one or more blobs in it retain uncommitted markers, or their earlier marker removals may not have been durably synchronized
- **THEN** the constructor SHALL remove all such markers as one batch
- **AND** SHALL synchronize the shared marker directory once even when all such markers are already absent
- **AND** SHALL preserve any existing predecessor and prevent superseded cleanup and build work if reconciliation fails

#### Scenario: Reporting incomplete post-commit cleanup
- **WHEN** a new committed-manifest generation is authoritative
- **AND** one or more superseded blobs cannot be removed
- **THEN** the constructor SHALL attempt every remaining cleanup candidate
- **AND** SHALL preserve the previous manifest as recovery evidence
- **AND** SHALL report every candidate failure
- **AND** SHALL return an operational failure with process exit code 4
- **AND** SHALL NOT roll back the successfully built image or newest committed generation

#### Scenario: Completing a visible generation after interrupted publication
- **WHEN** a later invocation discovers a complete generation entry that may have become visible before its publishing parent-directory synchronization completed
- **THEN** it SHALL successfully synchronize the generation directory before accepting the newest generation as authoritative
- **AND** synchronization failure SHALL preserve all generations and blobs and prevent cleanup and build work

#### Scenario: Recovering cleanup before another build
- **WHEN** a later invocation finds an authoritative generation and one retained previous generation
- **THEN** it SHALL retry every still-applicable cleanup candidate under the project build lock
- **AND** SHALL synchronize each relevant existing blob directory and the shared marker directory once per recovery batch even when all assigned candidate entries are already absent
- **AND** SHALL treat candidates in that batch as successfully cleaned only after their durable absence is established
- **AND** SHALL not start build work until every candidate is durably absent and the previous manifest has been durably removed

#### Scenario: Blocking a build after recovery failure
- **WHEN** pre-build recovery cannot remove one or more cleanup candidates
- **THEN** it SHALL attempt every remaining candidate and report all failures
- **AND** SHALL preserve the previous manifest
- **AND** SHALL return an operational failure with process exit code 4
- **AND** SHALL NOT materialize artifacts, create a snapshot, invoke Docker, or publish another generation

#### Scenario: Rejecting ambiguous committed state
- **WHEN** generation state contains a malformed generation, more than one previous generation, or otherwise ambiguous authority
- **THEN** the constructor SHALL fail without deleting any blob or manifest
- **AND** SHALL NOT start build work

#### Scenario: Preserving the prior set after Docker failure
- **WHEN** Docker build fails or is interrupted before a new generation is committed
- **THEN** the prior authoritative committed-manifest generation and its blobs SHALL remain intact
- **AND** newly verified blobs SHALL remain uncommitted

#### Scenario: Preserving the prior set after snapshot cleanup failure
- **WHEN** Docker successfully builds an image
- **AND** removal of that transaction's snapshot fails
- **THEN** the constructor SHALL NOT publish a new committed-manifest generation or garbage-collect superseded blobs
- **AND** the prior authoritative generation and its blobs SHALL remain intact
- **AND** newly verified blobs SHALL remain reusable as uncommitted entries tracked by their durable markers
- **AND** SHALL remain eligible for fixed 30-day uncommitted-TTL collection unless a later committed generation references them

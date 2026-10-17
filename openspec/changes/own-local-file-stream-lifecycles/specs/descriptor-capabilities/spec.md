## ADDED Requirements

### Requirement: Prepare acquisition of directly owned streams
The stream lifecycle SHALL support acquisition with release capacity prepared before the factory runs. The actual returned fd-owning stream SHALL become the sole shared owner's target before fallible use or inspection. A close-delegating proxy, a second raw-descriptor owner or native-context double release SHALL NOT satisfy direct adoption. Already-open constructor rejection SHALL leave caller ownership unchanged.

#### Scenario: Preparation fails before acquisition
- **WHEN** label, factory or synchronization preparation fails
- **THEN** the resource factory SHALL NOT run and no stream SHALL be acquired or closed
- **AND** an interruption SHALL retain its exact identity

#### Scenario: Factory returns a file object
- **WHEN** the prepared acquisition factory returns an fd-owning file object
- **THEN** that exact object SHALL be directly retained by the shared stream owner before parsing, reading or interface inspection
- **AND** no replacement owner, close proxy, independent file context manager or raw-fileno adoption SHALL receive release authority

#### Scenario: Acquisition or post-acquisition setup fails
- **WHEN** the factory raises without exposing a stream
- **THEN** the prepared lifecycle SHALL NOT fabricate or release a stream
- **WHEN** an actual stream is returned and subsequent setup or transfer instrumentation fails
- **THEN** the same prepared authority SHALL account for it and attempt its release once under shared precedence
- **AND** consumer fallback close or self-cleaning transfer mocks SHALL NOT substitute for ownership

### Requirement: Preserve local-file failures through terminal release
Local-file cleanup SHALL use shared precedence: first process-control interruption, unexpected cleanup defect, active ordinary operation, then ordinary cleanup failure. The selected error SHALL retain exact identity before domain translation, with displaced errors retained secondarily. Release SHALL be terminal and at most once through repeated, reentrant or concurrent cleanup; every independent safe release SHALL still be attempted.

#### Scenario: Read interruption and ordinary close failure
- **WHEN** reading or parsing is interrupted and closing the file raises an ordinary error
- **THEN** the original interruption SHALL propagate unchanged after cleanup
- **AND** the close error SHALL remain secondary rather than converting the interruption to a cache miss or ordinary domain failure

#### Scenario: Sole release failure crosses a domain boundary
- **WHEN** opening and reading succeed but the sole close fails
- **THEN** existing domain-specific close-error propagation, translation or cache-miss behavior SHALL remain unchanged
- **AND** process-control close interruptions SHALL propagate unchanged

#### Scenario: Another cleanup caller loses the terminal claim
- **WHEN** cleanup is repeated, reenters or races an existing close
- **THEN** the actual file SHALL receive no second underlying close attempt
- **AND** a terminal claim alone SHALL NOT be represented as proof that the winning close has completed
- **AND** independent file cleanup SHALL still run if the winning close fails

### Requirement: Prove local-file ownership across the production tree
Local-file ownership enforcement SHALL inventory the complete production tree, including new modules and aliased builtin, I/O-module and path-based file factories. It SHALL associate each acquired file with its receiving owner, borrowed uses and terminal release. Unresolved acquisition or release origins SHALL require classification rather than a historical-site or name-based exemption.

#### Scenario: A new local-file consumer uses an alias
- **WHEN** an aliased factory, assigned close callable or context-manager binding appears in any production module
- **THEN** the same acquisition and ownership rules SHALL apply as to a directly spelled file open
- **AND** borrowed close, proxy adoption, delayed transfer, duplicate ownership and native double release SHALL be rejected

#### Scenario: The repository ledger detects a lifecycle violation
- **WHEN** acquisition, direct transfer, wrapping or release is omitted, duplicated, retried or loses a winning outcome
- **THEN** executable ledger coverage SHALL fail for that violation
- **AND** positive cases SHALL identify the actual returned stream and its one final owner across success, failure and interruption

### Requirement: Preserve local-file domain and image compatibility
Ownership correction SHALL preserve reader signatures, encoding, bytes, paths, file validation, digest calculations and established domain error mappings except for shared cleanup precedence. Runtime-image imports SHALL resolve lifecycle dependencies without the source repository. Network response/server lifecycles and raw-fd wrappers SHALL retain their separately owned contracts.

#### Scenario: Read a projection, metadata, cached response or cached artifact
- **WHEN** an existing local-file reader succeeds or reports an open, parse, read or sole-close failure
- **THEN** its characterized result and domain mapping SHALL remain compatible
- **AND** lifecycle correction SHALL NOT change cache identity, file permissions, integrity policy or publication behavior

#### Scenario: Import from the installed image layout
- **WHEN** runtime readers execute outside the repository from the packaged image files
- **THEN** all required lifecycle modules SHALL resolve without ambient repository imports
- **AND** no process or domain dependency SHALL be introduced into the filesystem foundation

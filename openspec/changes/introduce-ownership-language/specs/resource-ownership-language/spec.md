## Purpose

Define a bounded architectural language for resource responsibility, making common cleanup paths mechanically checkable without claiming universal application termination or eventual resource release.

## ADDED Requirements

### Requirement: Confine raw descriptor authority
Application code SHALL exchange owners or borrowed capabilities instead of raw descriptors. Raw acquisition, extraction, storage and syscall access SHALL remain inside the runtime ownership package. Its implementation functions MAY exchange raw primitives; application restrictions SHALL apply at the public boundary and consumers, not internal calls. Classification SHALL follow symbol origins rather than spelling, module names or integer annotations alone.

#### Scenario: Descriptor escapes through an indirect channel
- **WHEN** application code obtains a raw fd and passes, returns or stores it through a field, collection, closure, generator or callback
- **THEN** validation SHALL reject that escape, including aliased operations and newly added modules
- **AND** a callback that receives a raw fd SHALL NOT constitute an approved application access API

#### Scenario: Application performs file work
- **WHEN** application code needs descriptor-backed read, write or metadata operations
- **THEN** it SHALL invoke owner or borrowed-capability operations without obtaining the fd number

#### Scenario: Standard library fully encapsulates a child
- **WHEN** synchronous subprocess execution retains all child and pipe authority inside the standard library and returns only completed output values
- **THEN** validation SHALL record it as library-owned rather than require a process-owner migration
- **AND** caller-supplied resources SHALL retain their separate ownership obligations

### Requirement: Isolate the runtime ownership library
Runtime ownership mechanisms SHALL form an independently installable library project in the same repository, with its own build/dependency metadata, source layout, environment, tests and public contract. Filesystem and process coordination SHALL remain distinct internal layers without application-domain dependencies. The analyzer SHALL stay outside the library. Internal raw access SHALL NOT waive lifecycle safety or allow raw public exports.

#### Scenario: Internal helper exchanges a descriptor
- **WHEN** runtime implementation functions pass a raw fd internally without exporting it
- **THEN** application raw-primitive escape validation SHALL NOT reject that internal call
- **AND** the runtime's own tests SHALL verify its ownership, release and reader-safety contracts

#### Scenario: Consumer uses package internals
- **WHEN** a consumer accesses an internal constructor, descriptor field or raw-returning helper, directly or through an alias or compatibility facade
- **THEN** the boundary gate SHALL reject the access rather than treating the consumer as runtime implementation

#### Scenario: Runtime is tested independently
- **WHEN** the runtime's dedicated suite is collected and run
- **THEN** it SHALL NOT collect consumer suites or import application-domain test fixtures
- **AND** consumer tests SHALL depend only on the public runtime contract

#### Scenario: Library is built without the application
- **WHEN** the library project is built and its wheel installed in an isolated environment outside the application checkout
- **THEN** its imports and own tests SHALL work without application sources, test fixtures or PYTHONPATH injection
- **AND** its source distribution SHALL contain the declared inputs needed to rebuild the wheel independently

#### Scenario: Application consumes the library
- **WHEN** the application is installed for development or release
- **THEN** its installation workflow SHALL install the declared library dependency
- **AND** local editable installation MAY support development, but release acceptance SHALL also exercise the built wheel
- **AND** application test collection SHALL NOT implicitly collect the library's separate test tree

### Requirement: Select tests by ownership boundary changes
Development test selection SHALL distinguish runtime internals, consumers, public contracts and analyzer/tooling changes. Runtime-only edits SHALL run runtime suites; consumer-only edits SHALL run affected consumer suites. Public-contract changes SHALL also run dependent contract checks. Ambiguous changes SHALL select conservatively. Releases SHALL retain full integration checks.

#### Scenario: Runtime implementation changes without a contract change
- **WHEN** only runtime implementation changes and its public contract is unchanged
- **THEN** selection SHALL run its own unit, fault and public-contract tests without collecting unrelated consumer suites

#### Scenario: Consumer changes without a runtime change
- **WHEN** only consumer code changes
- **THEN** selection SHALL run affected consumer tests and boundary checks without running runtime unit or fault suites

#### Scenario: Public behavior changes without a signature change
- **WHEN** exported errors, documented behavior or ownership effects change even though signatures stay the same
- **THEN** selection SHALL include the runtime suite and dependent consumer contract checks
- **AND** uncertain compatibility SHALL NOT be treated as an internal-only change

#### Scenario: Release validation runs
- **WHEN** a release gate executes
- **THEN** it SHALL explicitly run both projects' test suites and full integration regardless of development test selection
- **AND** running only the application test root SHALL NOT count as testing the independent library

### Requirement: Assign an explicit responsibility form
Each acquired owner SHALL have a declared lexical, transferred, managed-storage or deferred-cleanup responsibility form. A borrow SHALL grant neither release nor transfer authority and SHALL remain subordinate to its owner's lifetime. Unknown forms SHALL be reported as not automatically proven rather than accepted from the presence of a close method.

#### Scenario: Borrowed operation crosses a function boundary
- **WHEN** a helper receives a borrowed capability
- **THEN** it SHALL be able to perform only its permitted resource operations
- **AND** it SHALL NOT close, transfer, extract or persist access beyond the owning lifetime

#### Scenario: Responsibility crosses a function boundary
- **WHEN** a function returns ownership or accepts responsibility for an owner
- **THEN** its contract SHALL distinguish that transfer from borrowing
- **AND** the former authority SHALL NOT remain usable as an independent owner
- **AND** a failed receiving operation SHALL leave exactly one protected release responsibility

### Requirement: Prove cleanup on finite scope exit
Automatic scope proofs SHALL establish a protected release attempt or valid transfer on normal and exceptional exits in the supported control-flow subset. They SHALL NOT assert termination of arbitrary work, success of OS close, recovery after abrupt process death or eventual stopping of arbitrary readers.

#### Scenario: Work raises before a trailing close
- **WHEN** a resource is acquired and only an unprotected later close is present
- **THEN** validation SHALL reject the lexical proof because the exceptional exit lacks cleanup

#### Scenario: Work remains inside a protected scope
- **WHEN** acquisition and cleanup are structurally protected but the work does not terminate
- **THEN** the report SHALL describe cleanup-on-exit, not guaranteed eventual release

### Requirement: Root managed responsibility in a protected lifecycle
Stored owners SHALL enter an approved cleanup container or an explicitly reviewed lifecycle. Registration SHALL handle partial construction failure safely. A mechanically accepted responsibility chain SHALL terminate in a protected root lifecycle, not merely a class exposing close.

#### Scenario: Aggregate cleanup encounters a failure
- **WHEN** one stored owner's release fails
- **THEN** every independent safe owner SHALL still receive its sole release attempt under shared precedence
- **AND** repeated cleanup SHALL NOT repeat underlying release

#### Scenario: Object offers close but has no responsible caller
- **WHEN** an object stores owners and defines close without a protected or reviewed root responsibility
- **THEN** automatic validation SHALL NOT classify those resources as structurally discharged

### Requirement: Retain responsibility during incomplete cleanup
Deferred cleanup SHALL preserve the same release owners and expose incomplete status until safe release attempts and winning outcomes settle. Reader-stop safety and action-resource settlement SHALL remain specialized lifecycle obligations, not inferred from scope syntax or local child exit.

#### Scenario: Reader exceeds a bounded shutdown allowance
- **WHEN** a reader remains able to access a resource after bounded shutdown
- **THEN** the lifecycle SHALL retain its owner and report incomplete cleanup
- **AND** validation SHALL NOT demand unsafe immediate release or claim unconditional eventual release

### Requirement: Separate automatic proof from reviewed acceptance
Validation SHALL generate a complete-tree report separating automatically proven forms, valid review-approved cases, and unresolved violations. Review records SHALL identify the affected source, contracts, responsible release path, rationale and supporting tests. Relevant changes SHALL invalidate approval. LLM assistance SHALL NOT be labeled automatic proof.

#### Scenario: Unsupported ownership construction appears
- **WHEN** analysis encounters dynamic dispatch or another construct outside its supported proof subset
- **THEN** it SHALL report the unproven site and reason
- **AND** CI SHALL reject it unless an explicit valid review covers the exact ownership boundary

#### Scenario: Reviewed lifecycle changes
- **WHEN** reviewed code, relied-on ownership contracts or cleanup dependencies change
- **THEN** the stored approval SHALL become stale and SHALL require renewed review
- **AND** unrelated source changes SHALL NOT silently broaden its scope

#### Scenario: Validation completes successfully
- **WHEN** the ownership gate reports success
- **THEN** every inventoried site SHALL be automatically proven, explicitly library-owned or covered by a valid review
- **AND** the report SHALL keep those categories distinct and include limitations of each proof

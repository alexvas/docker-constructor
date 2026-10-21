# Spec Delta

## Purpose

Make semantic test maintenance reviewable through source-bound evidence, model proposals and explicit human decisions.

## ADDED Requirements

### Requirement: Evidence-specific conclusions
Reviews SHALL distinguish measured execution, successful checks, model interpretation and human confirmation. Semantic conclusions SHALL cite current Scenario text, implementation and assertions, identify limitations and retain actor/model identity and source fingerprints.

#### Scenario: Insufficient assertions
- **WHEN** a test executes relevant code but does not assert a required outcome
- **THEN** review identifies the gap rather than treating coverage or a passing run as conformance proof

### Requirement: Descriptions and relevance review
The tool SHALL propose missing descriptions and revalidate existing descriptions against tests, product code and specifications. Discovery, relevance, work progress and freshness SHALL remain separate states.

#### Scenario: Missing mapping
- **WHEN** a test has no known Scenario link
- **THEN** its relevance is not automatically classified as obsolete

#### Scenario: Local rationale remains necessary
- **WHEN** catalog descriptions are maintained
- **THEN** essential local explanations of concurrency, regression intent and lint suppressions are not automatically removed from test code

### Requirement: Explicit backend and transmission policy
Semantic analysis SHALL use a configurable backend with an explicit remote transmission policy. The initial policy SHALL permit sending repository code and specifications; unavailable or failed analysis SHALL remain unresolved, not approved.

#### Scenario: Backend failure
- **WHEN** the configured model cannot complete a review
- **THEN** the task retains failure and retry information without issuing a successful semantic conclusion

### Requirement: Source-bound human decisions
Removal, assertion weakening and requirement changes SHALL require explicit human approval. Decision cards SHALL include reasons, affected guarantees, replacement coverage or approved requirement change, alternatives, uncertainties and source fingerprints. Approvals SHALL retain actor and timestamp in local storage without Git-review.

#### Scenario: Evidence changes after approval
- **WHEN** an approved proposal's source or specification evidence changes
- **THEN** the approval cannot authorize application without renewed validation

#### Scenario: Ambiguous optimization
- **WHEN** several replacements have different semantic risks
- **THEN** the tool presents alternatives and trade-offs for human selection rather than silently selecting weaker coverage

# Spec Delta

## Purpose

Coordinate recurring traceability analysis and test optimization using evaluated model capabilities, bounded work and preserved human authority.

## ADDED Requirements

### Requirement: Evaluated model routing
The tool SHALL route semantic tasks using task-specific evaluation of configured local openjev, local b27, less capable remote and remote frontier backends. It SHALL retain model/configuration identity, escalation reasons and costs; names alone SHALL not establish suitability.

#### Scenario: Uncertain local conclusion
- **WHEN** a task exceeds evaluated capability or yields conflicting conclusions
- **THEN** routing escalates within configured budget or leaves the review unresolved for a human

### Requirement: Evidence-aware cache and budgets
Cached conclusions SHALL be reusable only while all relevant inputs, prompt/policy versions and model configuration remain applicable. Exhausted budgets SHALL leave explicit pending work rather than successful conclusions.

#### Scenario: Scenario changes after cached review
- **WHEN** a referenced Scenario changes
- **THEN** the previous conclusion is historical and cannot satisfy the new review through a cache hit

### Requirement: Nightly bounded workflow
An externally scheduled command SHALL coordinate sequential worker modes 1, 2, 8, 16 and 32, reconciliation, separate coverage and semantic queues. It SHALL record partial failures, prevent conflicting measurements and preserve actual statuses.

#### Scenario: One matrix mode fails
- **WHEN** a worker-mode run fails
- **THEN** its failure remains visible even if independent stages finish successfully

### Requirement: Priority without starvation
The workflow SHALL select the union of per-mode top-100 slow tests as optimization candidates while retaining all tests in the catalog. It SHALL also prioritize regressions, stale evidence and unreviewed tests, including tests that leave the slow ranking.

#### Scenario: Many unreviewed fast tests
- **WHEN** nightly slow-test candidates repeatedly occupy the queue
- **THEN** the scheduling policy still allocates work to unreviewed tests and reports backlog age

### Requirement: Approval-bound optimization workflow
The tool SHALL present alternatives, guarantee risks and repeated before/after measurements for optimizations. Patch preparation SHALL require separate authorization; deletion, weakened assertions and requirement changes SHALL require fresh human approval. Final candidate verification SHALL include the mandatory green gate.

#### Scenario: Approved optimization changes its own evidence
- **WHEN** a prepared patch differs from the approved source-bound proposal
- **THEN** the tool requires renewed review before applying risky changes and does not reuse stale approval

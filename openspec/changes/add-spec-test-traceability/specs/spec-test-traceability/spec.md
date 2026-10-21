# Spec Delta

## Purpose

Connect versioned product code, tests and OpenSpec Scenarios through explainable evidence and source-change revalidation.

## ADDED Requirements

### Requirement: Typed versioned graph
The graph SHALL distinguish code-implements-Scenario, test-checks-Scenario and test-executes-code links. Nodes SHALL address files, qualified symbols and ranges, and main or change-specific Requirements/Scenarios at identified source states. Links SHALL retain provenance, evidence and review state.

#### Scenario: Execution is not semantic proof
- **WHEN** coverage connects a test to implementation associated with a Scenario
- **THEN** the graph does not automatically assert that the test checks the Scenario

#### Scenario: Scenario lifecycle
- **WHEN** a Scenario belongs to an active change rather than a main specification
- **THEN** the graph exposes its full capability path, Requirement, Scenario, revision and change context without declaring it implemented

### Requirement: Source-bound execution evidence
Per-test coverage SHALL retain actual executed line sets and phase/context attribution where supported. Unsupported subprocess, fixture or external-language attribution SHALL be explicit. Coverage SHALL be collected separately from performance samples.

#### Scenario: Historical map after collection failure
- **WHEN** fresh coverage collection fails
- **THEN** the previous map remains historical and is not relabeled current

### Requirement: Explainable incremental impact
For product, test and specification changes, analysis SHALL use old and new coordinates and known direct/transitive links to produce revalidation tasks with reasons. Historical coverage SHALL select mandatory known tests without claiming an exhaustive affected set.

#### Scenario: Product function changes
- **WHEN** a changed range belongs to a function linked to tests and Scenarios
- **THEN** the queue includes targeted tests, renewed timings and semantic review of directly and indirectly related Scenarios with explanation paths

#### Scenario: Fixture or helper changes before precise dependency support
- **WHEN** dependencies cannot be determined reliably
- **THEN** the tool expands verification scope conservatively and reports uncertainty rather than reporting no impact

#### Scenario: Specification changes
- **WHEN** a Scenario changes
- **THEN** linked implementation and tests are queued for conformance review

### Requirement: Freshness and verification boundaries
Changes to evidence inputs SHALL mark dependent conclusions as requiring revalidation while preserving history. Targeted execution and passing tests SHALL not prove semantic conformance or replace the full mandatory green gate.

#### Scenario: Green tests but unresolved semantics
- **WHEN** targeted tests pass while conformance review is unresolved
- **THEN** execution is recorded as successful and semantic conformance remains unresolved

### Requirement: Full reconciliation
The tool SHALL provide an externally schedulable reconciliation command reporting inventory completeness, unknown mappings, stale evidence and pending work without requiring phase C.

#### Scenario: Missed incremental update
- **WHEN** reconciliation discovers source changes absent from prior incremental analysis
- **THEN** it creates the missing revalidation work and reports its discovery

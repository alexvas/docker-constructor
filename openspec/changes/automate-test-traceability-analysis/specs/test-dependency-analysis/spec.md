# Spec Delta

## Purpose

Represent shared test preparation and dependencies explicitly so change-impact analysis can follow fixture and helper relationships.

## ADDED Requirements

### Requirement: First-class fixture graph
The graph SHALL represent versioned fixture definitions, fixture-to-fixture and fixture-to-test relationships, scope, parametrization and observed setup/teardown contexts. Static possibilities and observed dependencies SHALL be distinguishable.

#### Scenario: Shared fixture changes
- **WHEN** a session fixture used through another fixture changes
- **THEN** dependent tests and their related Scenarios are selected for revalidation with the dependency path shown

#### Scenario: Dynamic dependency uncertainty
- **WHEN** a dynamically requested fixture cannot be resolved exhaustively
- **THEN** observed edges are retained and the unresolved scope triggers conservative expansion

### Requirement: Shared-cost attribution
Reports SHALL distinguish directly attributed test durations from shared fixture costs and SHALL not charge a shared observation independently to every dependent test as if each incurred it.

#### Scenario: One setup serves multiple tests
- **WHEN** a shared fixture is initialized once for several tests
- **THEN** its observed cost and attribution limits remain explicit without multiplying suite cost

### Requirement: Dependency and identity reconciliation
The tool SHALL use source history and dependency evidence to propose symbol/test/fixture rename or move mappings. Ambiguous matches SHALL require confirmation and unresolved helper dependencies SHALL remain visible.

#### Scenario: Method moves between files
- **WHEN** evidence suggests continuity despite changed paths
- **THEN** the tool offers a provenance-backed identity reconciliation without discarding historical coordinates or silently accepting an ambiguous match

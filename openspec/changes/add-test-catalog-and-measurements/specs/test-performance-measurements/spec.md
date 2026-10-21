# Spec Delta

## Purpose

Provide repeatable performance evidence for optimization candidates without confusing environment changes or incomplete runs with improvements.

## ADDED Requirements

### Requirement: Repeated candidate measurements
The tool SHALL support configurable warmup and repeated measurements of a selected test, file or set against baseline and candidate source states. It SHALL retain each observation, setup/call/teardown durations, outcomes, warnings and actual runner exit status.

#### Scenario: Compare an optimization
- **WHEN** baseline and candidate are measured repeatedly
- **THEN** the report includes sample counts, mean, median, dispersion and absolute/relative changes with the exact protocol and source states

#### Scenario: Failed or interrupted measurement
- **WHEN** a repetition fails or is interrupted
- **THEN** available observations are retained as incomplete and are not presented as successful comparable verification

### Requirement: Machine and resource provenance
Every run SHALL reference a machine identity and immutable resource/environment snapshot including allocated CPU cores, CPU model, RAM limit, OS, Python and runner versions, worker count, distribution mode and instrumentation.

#### Scenario: Resource allocation changes
- **WHEN** the same machine runs with a different CPU or RAM allocation
- **THEN** observations belong to distinct resource profiles and are not silently pooled

### Requirement: Comparable statistics
The tool SHALL compute comparisons only within declared comparable profiles, distinguish isolated candidate results from suite results and flag insufficient samples. Instrumented coverage measurements SHALL not be pooled with ordinary timings.

#### Scenario: Incompatible baseline
- **WHEN** baseline and candidate differ in worker count or instrumentation
- **THEN** the report explains the incompatibility instead of claiming a performance improvement

### Requirement: Rankings do not limit catalog membership
The tool SHALL rank tests by total and phase duration and files by aggregate observed duration. Rankings SHALL not exclude other tests from registration or prevent later observations of previously slow tests.

#### Scenario: Optimized test leaves ranking
- **WHEN** a known test no longer appears in the top 100
- **THEN** its identity and history remain available and new measurements can still be recorded

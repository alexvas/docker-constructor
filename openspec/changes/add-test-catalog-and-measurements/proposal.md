# Proposal

## Why

Slow-test optimization needs repeatable before/after evidence rather than isolated duration rankings. A complete local test catalog provides immediate value and the durable identities needed for later Spec-first traceability.

## What Changes

- Establish phase A of A → B → C: this change → `add-spec-test-traceability` → `automate-test-traceability-analysis`.
- Register every test collected in declared pytest profiles, including skipped and parametrized cases, independently of whether it has been semantically reviewed.
- Store identities, source coordinates, machine/resource snapshots, runs and immutable observations in project-local SQLite excluded from Git; cards do not undergo Git-review.
- Support repeated candidate measurements and comparable baseline/candidate reports, plus slow-test and file rankings.
- Preserve actual outcomes, collection completeness, warnings and process exit statuses.
- Exclude LLM analysis, coverage, semantic mapping, code rewriting and a built-in scheduler.

## Capabilities

### New Capabilities

- `test-catalog`: Complete profile-aware inventory, stable identities, versioned source coordinates and durable local storage.
- `test-performance-measurements`: Repeated candidate measurements, environment provenance and comparable performance reports.

### Modified Capabilities

None. Existing product requirements and mandatory verification gates remain unchanged.

## Impact

Development tooling under `scripts/`, pytest integration and tests, testing documentation, development dependencies and `.gitignore`. No production runtime or Dockerfile changes are intended. Implementation must preserve the existing pytest skip policy and full green gate. B may start implementation only after A's acceptance criteria and migration/export contract are satisfied.

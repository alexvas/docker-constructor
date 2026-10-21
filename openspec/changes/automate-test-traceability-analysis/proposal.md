# Proposal

## Why

The self-contained traceability base still requires manual prioritization and conservative dependency handling. Fixture-aware analysis and evaluated model orchestration can improve completeness and reduce review effort without weakening approval boundaries.

## What Changes

- Establish phase C, depending on accepted `add-spec-test-traceability`, transitively on A.
- Add first-class fixture nodes, fixture-to-fixture/test dependencies, scope, parametrization and versioned execution evidence.
- Improve rename/move reconciliation and helper/dependency impact analysis while retaining explicit uncertainty.
- Evaluate and route semantic tasks across local openjev, local b27, a less capable remote model and a remote frontier model; no capability ranking is assumed from names.
- Add budgets, evidence-aware caching, escalation and priority queues for unreviewed, stale and slow tests.
- Provide nightly orchestration for worker matrices 1, 2, 8, 16 and 32, reconciliation, analysis and decision reports.
- Support separately authorized patch preparation and verification; never silently delete tests or weaken assertions.

## Capabilities

### New Capabilities

- `test-dependency-analysis`: First-class fixture graph and evidence-backed dependency and identity reconciliation.
- `traceability-orchestration`: Evaluated model routing, bounded recurring analysis, prioritization and approval-bound optimization workflows.

### Modified Capabilities

None. B's evidence, review and approval contracts remain authoritative.

## Impact

Extends A/B's local SQLite schema and development commands, model adapters, scheduler-facing orchestration, tests and documentation. Remote data transmission remains permissive but explicit. No internal scheduling daemon, production deployment, mutation-testing feature or automatic approval is introduced.

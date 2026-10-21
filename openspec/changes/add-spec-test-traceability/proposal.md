# Proposal

## Why

Spec-first maintenance requires distinguishing observed execution from evidence that code implements a Scenario and tests assert its guarantees. A versioned graph can turn source changes into explainable revalidation work without pretending that passing tests prove semantic conformance.

## What Changes

- Establish phase B, depending on accepted `add-test-catalog-and-measurements`; B is a useful final system without C.
- Extend A's SQLite catalog with source symbols, OpenSpec Requirements/Scenarios, typed evidence-backed links, reviews and decisions.
- Collect per-test coverage separately from performance measurements.
- Analyze product, test and specification diffs, invalidate affected conclusions and produce explainable targeted verification queues.
- Add one configurable LLM backend for descriptions, mapping proposals, conformance analysis and optimization/removal proposals; remote transmission policy is explicitly permissive.
- Record human approvals in SQLite rather than Git-review; source changes invalidate approvals tied to changed evidence.
- Provide full reconciliation commands suitable for an external nightly scheduler, backups and portable export.
- Defer first-class fixture graph, multi-model routing and automated patch preparation to C; handle fixture/helper changes conservatively now.

## Capabilities

### New Capabilities

- `spec-test-traceability`: Versioned semantic and execution graph, evidence freshness and explainable change-impact analysis.
- `test-review-decisions`: LLM-assisted semantic review and source-bound human decisions with explicit authority boundaries.

### Modified Capabilities

None. This change extends the data model of A without replacing its catalog or performance contracts.

## Impact

Depends on A's catalog IDs, source-state fingerprints, SQLite migrations and measurements. Adds development-only coverage integration, source/spec indexing, backend configuration and review commands. Targeted checks supplement the mandatory full `scripts/check-green`. No production behavior changes or automatic test deletion are authorized.

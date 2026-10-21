# Design

## Context

See proposal.md. A supplies the complete profile-aware catalog, immutable observations, SQLite persistence and source-state identities. Implement this change only after `add-test-catalog-and-measurements` is accepted. B must remain deployable without C. Design is required for the graph schema, coverage integration and semantic authority boundary.

## Goals / Non-Goals

**Goals:** Explainable conservative impact analysis, source-bound evidence and a practical human review loop using one backend.

**Non-Goals:** Precise first-class fixture graph, automatic rename acceptance, multi-model routing, automatic source modification or proof of conformance from test success.

## Decisions

### Extend SQLite rather than introduce another store

Add entity versions, typed edges, evidence records, analysis tasks, reviews and decisions through A's migrations/export contract. Keep stable external catalog IDs for Requirements/Scenarios so existing specs need no mass ID edits. Resolve addresses by full capability path, Requirement and Scenario title plus source version; ambiguous rename/archive transitions require confirmation. Distinguish main specification, active change delta and historical source; location does not prove implementation or approval.

Use AST indexing for file, module/class bodies, decorators and qualified classes/methods/functions, including nested symbols. Preserve line sets rather than flattening sparse coverage to a continuous range. Every file version includes content hash, Git blob when applicable and observed repository source-state. Old coordinates remain queryable for deletions and moves.

### Evidence model and trust policy

Separate observed execution, successful verification, semantic interpretation and human confirmation. Each semantic edge retains cited assertions, implementation and Scenario evidence, actor/model identity, prompt/policy version and input fingerprints. Statuses include proposed, confirmed, rejected and requires-revalidation. Do not derive test-checks-Scenario from test-executes-code plus code-implements-Scenario.

A model conclusion is a proposal unless a specific policy accepts that class of conclusion; risky actions always need human approval. No single confidence number substitutes for evidence. Define a versioned evidence policy mapping claim types to mandatory citations and checks. Missing evidence yields unresolved, not false conformance or obsolescence.

### Coverage and attribution

Evaluate/pin Python-3.14-compatible coverage.py and pytest-cov versions using `--cov-context=test`, verify xdist context merging and phase semantics before accepting integration. Collect only in dedicated instrumented runs. Store raw line sets, context provenance and completeness, plus branch information only if explicitly supported. Treat import-time, shared fixture and subprocess attribution as separate/unknown unless verified by tests; Node/shell execution is outside Python coverage. Failed collection cannot refresh historical maps.

### Change-impact queue

Compare explicit base/current source states; use both old and new diff ranges and symbol versions. Traverse direct implementation links and test execution/semantic links, retaining a reason path for each task. Queue execution, renewed candidate timings, coverage and semantic review as separate tasks; task success cannot close a different evidence type. Deduplicate by claim/input-state/task type, not merely test name.

Known historical coverage selects mandatory known tests but is not exhaustive. For changed fixtures/helpers before C, use file/import evidence to expand scope; if reliable dependency scope is unavailable, select the full declared profile. New or unmapped product code raises unknown-mapping review. Specification diffs reverse-traverse implementation/test links. Full reconciliation catches missed incremental events and reports inventory/mapping gaps.

### LLM review and decisions

One configurable adapter accepts bounded task context and returns structured conclusions with source citations and limitations. Keep remote transmission policy explicitly permissive; retain backend/model/configuration and failure status, exclude credentials from exports/logs. Backend outages do not block deterministic collection or produce approval. No actual backend ranking is assumed; select an initial backend using a small project-specific evaluation covering lifecycle near-duplicates and weak assertions.

Descriptions explain conditions, action, asserted guarantee and important non-goals. Essential inline rationale stays in source, especially concurrency intent and lint suppression explanations. Relevance, review progress and freshness remain independent.

CLI review actions record human identity, timestamp, proposal fingerprint and approved action in SQLite, without Git-review. Decision cards state lost guarantees, replacement tests or approved requirement removal, alternatives and uncertainty. An approval is invalid for changed inputs. B prepares decisions, not patches or deletions.

### Resource boundary

All Git, runner and backend executable coordination uses OwnedProcess with launch-configured policy. Any newly acquired raw descriptor, directory or fd-owning stream uses its single OwnedDescriptor, DirectoryDescriptor or OwnedStream owner. Network/provider adapters explicitly release their native connections and use existing lifecycle-safe infrastructure where available; do not independently own library-internal descriptors. Test failure and interrupted-cleanup paths, not only successful replies.

## Risks / Trade-offs

- [Partial graph creates false certainty] → Expose unknowns, conservative scope and evidence-specific statuses in every report.
- [LLM hallucinates mapping] → Validate references, retain citations, evaluate known traps and require human approval for risky actions.
- [Coverage changes timing] → Separate instrumentation profiles and prohibit pooled performance comparisons.
- [Ignored DB contains only copies of decisions] → Extend A's backup/export with all review and approval metadata.
- [Fixture precision deferred] → Conservative B fallback remains a documented complete behavior, not an unimplemented placeholder.

## Migration Plan

Back up A storage, apply transactional additive graph migrations and verify preservation of A IDs/measurements. Index specs and code, mark existing cards unreviewed, then collect coverage explicitly. Rollback restores the pre-upgrade snapshot with A's matching tool version. Acceptance demonstrates product/test/specification changes, helper fallback, stale approvals, unavailable backend and full reconciliation without any C functionality. Full `scripts/check-green` remains mandatory on the final implementation state.

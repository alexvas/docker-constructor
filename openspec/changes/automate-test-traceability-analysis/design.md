# Design

## Context

See proposal.md. Implement only after accepted B (`add-spec-test-traceability`) and its A dependency. B already provides usable evidence, impact and human review workflows; C improves precision and automation rather than filling required B gaps. Design is required for fixture semantics, model orchestration and recurring execution.

## Goals / Non-Goals

**Goals:** First-class shared dependencies, evaluated model routing and bounded nightly progress over the complete catalog, preserving every prior evidence/approval contract.

**Non-Goals:** Autonomous requirement changes, unrestricted patch application, a scheduling daemon, production deployment or mutation testing.

## Decisions

### Fixture and helper model

Add versioned fixture definitions and parametrized instances, dependency edges and execution events with scope, worker and setup/teardown provenance. Combine pytest's collected fixture relationships with observed dynamic requests; label static possibilities versus observed facts. Include autouse fixtures, overrides, indirect parametrization and fixture-to-fixture dependencies in acceptance examples. Shared fixture costs are distinct observations, not independently multiplied into every dependent test's cost. Helpers remain code-symbol dependencies; unresolved dynamic behavior retains B's conservative expansion.

First-class nodes are preferable to copying fixture descriptions into test cards because one fixture change must invalidate all dependent evidence consistently. Source-history similarity can propose moves/renames but cannot silently merge ambiguous identities. Preserve source snapshots and reason paths.

### Model evaluation and routing

Expose configurable adapters for local openjev, local b27, less capable remote and remote frontier models. Evaluate on a versioned project corpus containing direct mappings, weak assertions, lifecycle near-duplicates, stale specs and shared-helper impact. Score citation validity and harmful false approvals as well as answer quality, latency and cost. Do not infer an ordering from model names. Routing is task-specific, escalates disagreement/insufficient context and stops as unresolved when no evaluated backend or budget is available.

Keep deterministic tasks outside LLM routing. Remote policy remains permissive and recorded. Store model/configuration, prompt/evidence-policy versions and referenced content fingerprints. Cache keys cover the complete analysis inputs; uncertain dependency closure invalidates conservatively. Model replacement or policy changes do not silently reuse conclusions.

### Recurring orchestration

Provide a one-shot orchestration command for cron/systemd/CI rather than a daemon. A persisted job state machine tracks pending, running, succeeded, failed, interrupted and blocked tasks separately from resource cleanup. Serialize measurements across worker modes 1/2/8/16/32 on a machine/resource profile, default `--dist loadfile`, and distinguish xdist one-worker from ordinary serial. Capture resource limits and reject unsupported capacity explicitly rather than quietly changing the matrix. Coverage remains separate.

Build the union of per-mode top-100 candidates, apply configurable minimum duration/priority thresholds and retain observations for known tests that leave rankings. Priorities include regressions, timing instability, stale evidence and unreviewed catalog entries. Reserve a configurable share for oldest unreviewed work to prevent starvation. Nightly discovery supplies candidates; repeated candidate measurements provide primary optimization evidence.

Use short SQLite transactions and exclusive logical job claims; no database transaction stays open across a model request or runner process. Resume interrupted work only after reconciling previous cleanup and input freshness, without replaying applied decisions. Aggregate status cannot hide a failed independent stage. Budgets bound model calls and wall time, not convert unfinished tasks into success.

### Optimization and authority

Prepare proposal alternatives with asserted guarantees, predicted trade-offs and an explicit measurement plan. Patch preparation requires a separate user authorization and uses an isolated candidate workspace; it does not reset/stage the user's tree. Compare baseline and candidate under the same A measurement profile and run the unchanged full gate on the final candidate. Record isolated and full-suite effects separately. Prepared artifacts do not authorize deletion, assertion weakening or requirement changes; those still require fresh source-bound human approval. Recheck proposal and approved patch fingerprints before any permitted application. No automatic commit is included.

### Resource ownership

Use OwnedProcess for each runner/model/Git child and launch-configured StopPolicy, total shutdown budget and retained incomplete cleanup continuation. Shared filesystem owners cover every application-acquired descriptor, directory and stream for candidate workspaces, logs and export. Backend-native connections and SQLite connections close explicitly without competing ownership of library-internal descriptors. Jobs may be logically finished while cleanup is incomplete; reporting must expose both.

## Risks / Trade-offs

- [Fixture graph appears exhaustive] → Distinguish observed and static edges and retain unknown-dynamic fallback.
- [32 workers distort measurements] → Record resource constraints, serialize jobs and never compare mismatched profiles.
- [Budget favors only slow tests] → Reserve review capacity for aging unreviewed/stale entries.
- [Cached stale interpretation] → Version complete inputs/policies and invalidate conservatively.
- [Automated patch loses guarantees] → Separate authorization, immutable approval binding, semantic review and full gate.

## Migration Plan

Back up B storage and add fixture, routing-evaluation and job records transactionally. Existing B links retain their evidence types and IDs. Initially run orchestration in report-only mode; enable authorized patch preparation separately after acceptance. Roll back using the B snapshot/tool version; exports retain all fixture/job/decision provenance. Validate interrupted-run recovery and absence of duplicate application before enabling nightly use.

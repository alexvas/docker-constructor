# Tasks

## 1. Dependency precision

- [ ] 1.1 Confirm accepted B and transitive A prerequisites; verify B catalog/evidence/approval fixtures survive C's transactional migration and export round trip.
- [ ] 1.2 Add first-class versioned fixtures and fixture-to-fixture/test edges with scope and parametrization; verify autouse, overrides, indirect parameters and chained session fixtures; document observed versus possible dependencies.
- [ ] 1.3 Capture dynamic requests and setup/teardown provenance with shared-cost records; verify uncertainty expands scope and shared cost is not multiplied across tests/workers; document attribution limitations.
- [ ] 1.4 Add helper dependency and rename/move proposals using Git/source evidence; verify ambiguous identity requires confirmation and history/reason paths remain intact.

## 2. Evaluated model orchestration

- [ ] 2.1 Add configurable adapters for local openjev, local b27, less capable remote and remote frontier; verify fake-adapter contract tests and documented permissive transmission policy without assumed rankings.
- [ ] 2.2 Build a versioned evaluation corpus for mappings, weak assertions, lifecycle near-duplicates, stale specs and helper changes; deliver task-specific citation/error/latency/cost results that justify routing policy.
- [ ] 2.3 Implement task-specific routing, disagreement escalation and budgets; verify budget exhaustion/unavailable models leave pending or unresolved work and deterministic tasks never invoke LLM.
- [ ] 2.4 Add evidence/model/prompt/policy-bound cache reuse; verify changed Scenario, source, policy or backend configuration invalidates applicability and preserve evaluation/configuration provenance in export; document routing and cache behavior.
- [ ] 2.5 For adapter/process resources, use OwnedProcess launch-configured StopPolicy plus single OwnedStream/OwnedDescriptor/DirectoryDescriptor owners for application-acquired streams/fds/directories; verify operation-plus-close, sole-close, interruption, repeat, independent cleanup and applicable handoff/concurrency tests.

## 3. Nightly work and prioritization

- [ ] 3.1 Add externally schedulable one-shot job orchestration with short transactions and exclusive claims; verify concurrent invocations cannot overlap conflicting measurements and interrupted work resumes without duplicate effects; document scheduler integration.
- [ ] 3.2 Run sequential 1/2/8/16/32 worker modes with separate coverage and immutable resource snapshots; verify partial matrix failure preserves actual statuses and unsupported capacity is explicit rather than silently reduced.
- [ ] 3.3 Implement per-mode top-100 union, configurable thresholds and regression/staleness/unreviewed priorities; verify ranking exit retains observations and oldest unreviewed work receives reserved capacity; document backlog policy.
- [ ] 3.4 Track job completion separately from cleanup completion using retained OwnedProcess continuation and one shutdown budget; verify interrupted/failed stages cannot report overall success while independent work proceeds.

## 4. Authorized optimization workflow

- [ ] 4.1 Produce alternatives with affected guarantees, replacement coverage, uncertainties and measurement plans; verify ambiguous choices create human decisions rather than weaker automatic replacements; document authority boundaries.
- [ ] 4.2 Add separately authorized patch preparation in an isolated candidate workspace using DirectoryDescriptor for directory capability and OwnedDescriptor/OwnedStream for new fd/stream acquisitions; verify user's tree/index are untouched and cleanup failure/interruption/repeat paths preserve primary failures.
- [ ] 4.3 Bind risky-action authorization to source/proposal/patch fingerprints; verify stale or changed proposals require renewed approval and retries cannot replay an already applied action; document approval/application separation.
- [ ] 4.4 Integrate repeated before/after measurements and final candidate full green gate; verify incompatible profiles and unsuccessful checks block claims of successful optimization and reports distinguish candidate from suite effects.

## 5. Integrated acceptance

- [ ] 5.1 Demonstrate report-only nightly execution with fixture impact, model escalation, stale-cache rejection, human decisions and backup/recovery; deliver self-contained source-bound evidence and documented opt-in patch preparation.
- [ ] 5.2 Run scripts/check-green after each cohesive phase and on the final candidate, capturing complete temporary output and actual status; require G and review all skips/xfails before declaring C complete.

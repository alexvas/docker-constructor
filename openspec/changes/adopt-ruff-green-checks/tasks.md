# Tasks — binding adoption contract

## 0. Authority, scope and execution rules

This document is the binding execution and acceptance contract for `adopt-ruff-green-checks`. proposal.md defines motivation; design.md explains decisions. If they contradict this contract or repository instructions, stop and request a planning correction; do not choose a convenient interpretation. Implementation requires a separate apply request. Do not stage, unstage, reset or commit without explicit authorization. The descriptor/process migration is archived and is not an outstanding prerequisite.

### Required outcomes

1. Development-only Ruff **0.17.1**, required version **==0.17.1**, target **py314**, explicitly selected rules **E4/E7/E9/F/I/B/SIM115/RUF100**. No ALL, preview, unsafe fixes, TID251, S603 or S607.
2. Shared, deterministic Python coverage for lint, formatting and compilation: all repository-owned `.py`/`.pyi` under `docker/`, `tests/`, `scripts/`, plus Python scripts in `scripts/` without those suffixes. Existing Python-shebang files are discovered automatically; any Python script without a recognizable shebang needs an explicit inventory entry. Shell scripts are not Python. Do not recurse into cache/generated/vendor directories or follow directory symlinks. Record each exclusion and its reason; an unreadable or unclassified candidate is an error, not a silent omission.
3. Separate reviewable deltas for preparation/tooling, formatting only, safe autofix, manual fixes and permanent checks. Formatting only is a separate intended commit boundary, not authorization to commit.
4. Mandatory local green checks after each major change and before committing; independent non-mutating GitHub push/PR verification.
5. No product behavior changes except correction of individually demonstrated defects. Preserve lifecycle ownership, primary-error handling, shutdown budgets, APIs, source fixtures, stress sizes and architecture enforcement. Do not format OpenSpec historical evidence, embedded documentation examples or unrelated files as part of the Python-formatting delta.

### Prohibited shortcuts

Do not reduce test collection, replace full pytest with a subset, suppress warnings, remove checks merely to pass, add regression-hiding skips/xfails, broaden Ruff exclusions, or add blanket `noqa`. A necessary intentional construct may have a specific code and a comment explaining why the construct is needed. RUF100 detects obsolete suppressions; it does not judge explanation quality. SIM115 is not an ownership proof. The agreed Ruff set does not replace ownership, process-budget, API, dependency-DAG, runtime-import or documentation tests.

### Atomic work and unknown findings

A task has one output and one acceptance criterion. A verification command's invocation, status capture and summary are one verification objective, not separate features. Within a phase, execute RED, GREEN, INTROSPECT, VALIDATE in that order. RED must record a real gap, not deliberately break working product code. Negative cases belong in isolated fixtures. If already satisfied, record the observed result; do not manufacture a failure.

Where findings are not knowable until tools run, use the **binding work-order protocol** below. Do not invent counts now or use one checkbox meaning “fix everything.” The templates create concrete atomic work items in a separate evidence ledger; they are part of this contract, not optional follow-up. Their acceptance criteria may not be weakened during apply.

- Before executing a work order, record its unique ID, one target, one action, prerequisite IDs, expected output and exact verification command or assertion. Every instance has one checkbox and a before/after result. No instance may contain “and other findings” or multiple unrelated fixes.
- One target means one independent diagnostic/root cause, one layout-sensitive assertion, one test coverage gap or one import-side-effect risk. Related diagnostic messages may share a root cause only if the ledger lists them and explains why one edit fixes them inseparably.
- Instantiate all RED work orders before GREEN work orders. Execute GREEN only after RED acceptance. INTROSPECT reviews the completed ledger; VALIDATE checks both the ledger and whole phase.
- Zero instances is valid only with the inventory/command result proving no applicable targets. Record “not applicable: zero targets”; never claim unexecuted tests passed.
- New findings discovered after GREEN reopen the phase at RED and invalidate affected validation; they do not authorize skipping the sequence. During apply the planning checklist changes only completion boxes; the separate ledger holds concrete instances.

### Evidence standard

Each phase delivers `verification/phase-N.md` and any named inventories/ledgers linked from it. Durable evidence is concise and self-contained: checked revision or source-state fingerprint, exact commands, relevant Python/tool/environment versions, actual exit statuses, result counts, significant warnings and limitations. Identify historical results as historical. Capture full stdout/stderr temporarily for diagnosis and summary preparation; permanent retention or committing full logs is **not required**. Do not use container-local temporary paths as durable evidence links. CI may use retention-limited artifacts. Never summarize a nonzero check into a pass.

### Commands and gate definitions

P1 produces `verification/check-manifest.md` with two distinct sections:

- **Existing required checks** (`existing-required-checks`): exact commands discovered in P1 from `scripts/`, `pyproject.toml`, `pi-green-loop.json`, `.github/workflows/validation.yml` and repository instructions. These are mandatory throughout P1–P7. Include `scripts/check-types` and the production ty command when currently configured; do not replace one with the other or narrow either scope. Serial unittest discovery is not required for this change; the full parallel pytest suite is the test gate.
- **New adoption checks** (`new-adoption-checks`): lint, formatting and compilation entry points added in P2, with exact invocations and activation phases. P1 creates this section as pending P2 entry points; its presence does not require future tooling in P1.

Provision prerequisites before executing a mandatory check. Missing tools are a blocked check, not success.

P2 documents concrete entry-point names and invocation syntax in the contributor guide and check manifest. Use those exact commands in later phases. Check entry points run from the repository root, safely handle spaces in filenames and include extensionless files explicitly; default Ruff recursion is not sufficient. Configuration exclusions may not silently override the shared inventory. Checks must return the underlying failure status, not that of `tee`, `grep` or artifact upload.

New adoption checks have the following activation schedule:

| New check | Required to pass |
| --- | --- |
| Shared compilation | P2 onward; P1 uses its one-off command |
| Full-inventory format check | P3 onward |
| Full-inventory Ruff lint | P5 onward |

Earlier diagnostic runs must record their actual statuses and findings as migration backlog, not passes. A not-yet-active check's diagnostic failure does not block phase exit solely for that backlog; it does not excuse failure of an existing required check or an already-active adoption check. In particular, format failures block P3 onward even while lint backlog remains permissible in P3–P4.

**R — regression gate**, required in every phase:

- Full `.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q`, exit 0. Capture complete stdout/stderr temporarily, preserve the actual runner status, then record the durable summary. Obtain skip reasons from the run report or an additional full run with reporting enabled; never substitute that additional run for the mandated command.
- Every command in the manifest’s `existing-required-checks` section succeeds, with no unresolved ty diagnostics. New adoption checks are required according to their activation phases. Execute every check mandatory for the current phase, not every listed command unconditionally; final acceptance executes all checks in both sections.
- Compile the entire source inventory without importing/executing application code or leaving bytecode in the source tree. Respect Python source encoding. P1 records a temporary one-off command; P2 onward uses the shared compilation entry point. `compileall` alone does not cover extensionless scripts.
- Skip review: record test ID, reason and applicability condition. Only documented environmental prerequisites or genuine non-applicability qualify. Missing installable CI prerequisites must be provisioned, not silently converted into skips. Unknown reasons, unexpected xfails/xpasses or a failed runner block acceptance pending resolution; do not freeze a platform-dependent skip count.

**G — final green gate** is R plus full-inventory Ruff check (zero unresolved diagnostics) and full-inventory Ruff format check (exit 0). P1–P4 may contain a recorded migration Ruff backlog; no Ruff error is called a pass. G is mandatory in P5–P7. The permanent pre-commit rule is activated only after P5; intermediate migration checkpoints are not exceptions to the final rule.

**Phase exit:** all concrete tasks and work-order instances accepted, all listed deliverables present, required gates successful for the phase's exit source state, and an exit report naming the passed criteria. A validation failure blocks downstream work. Fixing source after validation makes affected results stale; rerun them before exit.

## Phase dependency DAG

| Phase | Direct prerequisites | Required deliverables | Exit gate |
| --- | --- | --- | --- |
| P1 Baseline/preparation | None | source/check/prerequisite/skip inventories; enforcement map; assertion-preparation ledger; baseline/exit summary | R and accepted preparation ledger |
| P2 Shared tooling | P1 | Ruff dependency/config; selector; lint/format/compile entry points; focused tests; contributor guide | R and accepted tooling fixtures |
| P3 Formatting only | P2 | machine-generated format delta; source/AST baseline; equivalence and idempotence reports | R, format check, pure-delta review |
| P4 Safe autofix (3.1) | P3 | automatic-fix delta; import/semantic review ledger; remaining diagnostics | R, format check, accepted autofix review |
| P5 Manual resolution (3.2) | P4 | per-finding work orders; fixes/tests; explained suppressions; exception guidance | G and accepted resolution ledger |
| P6 Permanent control | P5 | AGENTS rule; README links; local wiring; CI workflow; gate/skip tests | G and accepted local/CI parity tests |
| P7 Final acceptance | P6 | contract-to-evidence matrix; final scope review; final acceptance report | G and all predecessor exits |

```text
P1 → P2 → P3 → P4 → P5 → P6 → P7
```

This forward-only chain is the chosen DAG. A phase may depend only on an earlier phase; dependencies on later phases and cycles are forbidden. P1 must not require the P2 implementation, P3 must not require a P5 repair, and P4 must not require P5 to fix an autofix regression. Reopen an earlier phase when necessary, keep its repair outside pure downstream deltas, and revalidate affected descendants in order. This is evidence invalidation, not a backward dependency. Phase transitions do not require commits; use fingerprints and temporary before-state copies rather than resetting the index.

## 1. P1 — Baseline and enforcement preparation

**Prerequisites:** none. **Deliverables/exit:** P1 row above. Product implementation is not changed here; only brittle enforcement assertions and their coverage may be prepared.

### RED

- [x] 1.1 Create the source inventory with selected paths and explicit exclusion reasons; accept when each scoped source root and extensionless Python candidate is classified and paths are sorted without duplicates.
- [x] 1.2 Create the two-section check manifest; accept when `existing-required-checks` records every P1-discovered configured command with exact invocation, source configuration and required scope, and `new-adoption-checks` is explicitly pending P2 entry points.
- [x] 1.3 Create the environment-prerequisite manifest; accept when every existing check has named tool/package/external-service prerequisites and a provisioning command or documented environmental limitation.
- [x] 1.4 Run the baseline mandated full pytest; accept when the durable summary records its actual status/counts/warnings without representing baseline failures as success.
- [x] 1.5 Create the baseline skip inventory; accept when every reported skip has its ID, reason and reviewed applicability condition or an explicitly blocking unresolved disposition.
- [x] 1.6 Inventory layout-sensitive assertions in `tests/enforcement/`; accept when each entry names the assertion, protected invariant and proposed harmless formatting mutation, including the secure-walker line expectation if still present.

- [x] 1.7 Instantiate preparation work orders from 1.6; accept when each assertion has concrete RED/GREEN instances below or an evidence-backed no-change disposition. Complete every RED instance before entering GREEN.

**Binding RED instance, one assertion per instance:** `P1-A<n>-RED`: add one focused test proving the invariant survives a harmless line/layout change and rejects its named invalid mutation; accept when the old assertion's brittleness is demonstrated or its existing robustness is recorded.

### GREEN

- [x] 1.8 Produce the enforcement-to-Ruff map; accept when every enforcement test is classified as retained or a demonstrably equivalent candidate, with concrete rules and negative fixtures. Do not enable additional rules or delete checks in this phase.

**Binding GREEN instance, one assertion per instance:** `P1-A<n>-GREEN`: replace that one brittle assertion with its structural equivalent; accept when the matching RED test and the original invalid-mutation fixture both pass. Do not weaken uniqueness, API, ownership or dependency constraints.

- [x] 1.9 Document the assertion-preparation changes; accept when every changed assertion links to an accepted work order and no unreviewed assertion change remains.

### INTROSPECT

- [x] 1.10 Review the P1 source diff; accept when no product behavior, stress sizes, test collection or lifecycle guarantee changed.
- [x] 1.11 Review environment/skip dispositions; accept when unresolved blockers are absent and the archived migration is not listed as pending.

### VALIDATE

- [x] 1.12 Run full pytest for R on the prepared state; accept only exit 0 and a complete durable result summary.
- [x] 1.13 Execute the manifest checks mandatory in P1: all `existing-required-checks` commands; accept only successful statuses and no unresolved ty diagnostics. Do not require P2 entry points here; task 1.14 supplies P1 compilation.
- [x] 1.14 Compile the P1 inventory using a recorded one-off command; accept only successful compilation of every selected file without source-tree bytecode.
- [x] 1.15 Review exit-run skips against 1.5; accept only justified dispositions with no hidden collection reduction.
- [x] 1.16 Publish the P1 exit report; accept when all P1 work orders and deliverables are accepted and R applies to the recorded exit fingerprint.

## 2. P2 — Shared tooling

**Prerequisite:** P1 exit. **Deliverables/exit:** P2 row above.

### RED

- [x] 2.1 Add source-selector contract tests; accept when fixtures cover nested `.py`/`.pyi`, extensionless Python, explicit non-shebang entries, shell exclusion, symlinks, unreadable candidates and spaces in filenames, with actual missing/failing behavior recorded.
- [x] 2.2 Add compilation contract tests; accept when fixtures specify valid extensionless Python, invalid syntax, source encoding and absence of application side effects/bytecode.
- [x] 2.3 Add Ruff inventory-parity tests; accept when fixtures expose a selected file silently omitted by an exclusion and a selected extensionless file omitted by default recursion.
- [x] 2.4 Add check-runner status tests; accept when deliberate lint/format/syntax failures have explicit expected nonzero outcomes and check mode must leave contents unchanged.

### GREEN

- [x] 2.5 Declare development Ruff 0.17.1; accept when clean-environment installation from the declared group reports 0.17.1 without changing the runtime inventory.
- [x] 2.6 Configure the fixed Ruff policy; accept when effective settings match the version, target and selected rules in section 0, without unsafe/preview activation.
- [x] 2.7 Implement shared source selection; accept when 2.1 tests pass and actual output reconciles with P1, including new repository-owned helpers/tests.
- [x] 2.8 Implement non-mutating lint checking; accept when lint cases in 2.3–2.4 pass using the shared inventory and repository configuration.
- [x] 2.9 Implement non-mutating format checking; accept when format cases in 2.3–2.4 pass without file modification.
- [x] 2.10 Implement shared syntax compilation; accept when 2.2 and syntax-status cases in 2.4 pass for every supported file type.
- [x] 2.11 Write the contributor-check guide; accept when it gives executable installation/check commands, exact source-selection rules and the temporary migration-backlog distinction.
- [x] 2.12 Record new entry-point commands in the manifest’s `new-adoption-checks` section; accept when each exact invocation matches its implementation and records activation as compilation P2, format check P3, lint P5. Preserve the `existing-required-checks` section without narrowing its commands or scope.

### INTROSPECT

- [x] 2.13 Review helper implementation against repository contracts; accept when the review records actual ownership paths and required tests, without prescribing shell versus Python merely to avoid those contracts.
- [x] 2.14 Capture initial full Ruff findings; accept when a complete rule/location inventory and actual status identify all current backlog, including SIM115, without calling it green.
- [x] 2.15 Review installed-version/configuration/source parity; accept when the documented commands cover the same inventory with no silent exclusion.

### VALIDATE

- [x] 2.16 Run focused tooling tests; accept only passing source, compilation, parity and status fixtures.
- [x] 2.17 Run full pytest for R; accept only exit 0 and its durable summary.
- [x] 2.18 Execute manifest checks mandatory in P2: all existing required checks and shared compilation; accept only successful statuses and no unresolved ty diagnostics. Record lint/format diagnostic failures as backlog, not passes; neither is required to pass yet.
- [x] 2.19 Run shared compilation for R; accept only exit 0 for the full inventory.
- [x] 2.20 Review phase-exit skips; accept only justified reasons under section 0.
- [x] 2.21 Publish the P2 exit report; accept when all P2 deliverables exist and R matches the exit fingerprint.

## 3. P3 — Formatting only

**Prerequisite:** P2 exit. **Deliverables/exit:** P3 row above. Keep evidence outside the pure source delta. No helper/test/config edits, import sorting or handwritten repairs belong in that delta.

### RED

- [ ] 3.1 Capture the pre-format full-inventory format-check result; accept when actual findings/status or an already-formatted result are recorded.
- [ ] 3.2 Capture the P2 exit source baseline; accept when temporary before-state contents and durable fingerprints identify every inventory file for AST/diff comparison.

### GREEN

- [ ] 3.3 Run Ruff formatting on the complete inventory; accept when the source delta is generated only by `ruff format` 0.17.1 with the agreed configuration.

### INTROSPECT

- [ ] 3.4 Compare changed-file ASTs ignoring locations; accept when every changed file is equivalent, including type comments where represented. If formatter changes type-ignore locations, normalize only location metadata, not the ignored code or tag.
- [ ] 3.5 Audit delta purity; accept when only formatter output appears and the delta is suitable for a separate explicitly authorized commit. Return source-sensitive test repairs to P1/P2 instead of hiding them here.

### VALIDATE

- [ ] 3.6 Run formatting a second time; accept when no contents change.
- [ ] 3.7 Run full-inventory format check; accept only exit 0.
- [ ] 3.8 Run full pytest for R; accept only exit 0 and its durable summary.
- [ ] 3.9 Execute manifest checks mandatory in P3: all existing required checks, shared compilation and full-inventory format check; accept only successful statuses. Full-inventory lint remains diagnostic backlog until P5.
- [ ] 3.10 Run shared compilation for R; accept only exit 0.
- [ ] 3.11 Review phase-exit skips; accept only justified reasons.
- [ ] 3.12 Publish the P3 exit report; accept when purity/equivalence reports and R identify the same formatted state.

## 4. P4 — Safe automatic fixes (3.1)

**Prerequisite:** P3 exit. **Deliverables/exit:** P4 row above. This phase contains no manual source repairs or new regression-test implementation.

### RED

- [ ] 4.1 Capture post-format Ruff findings; accept when diagnostics and source fingerprints identify the exact autofix input.

### GREEN

- [ ] 4.2 Run full-inventory `ruff check --fix` without unsafe fixes; accept when the exact command/generated delta/status are recorded and any residual findings are explicitly unresolved, not a successful lint check.
- [ ] 4.3 Reformat autofix-changed files; accept when the additional delta is exclusively Ruff formatter output.

### INTROSPECT

- [ ] 4.4 Create an automatic-change review ledger; accept when each changed import block or independent semantic fix has a target-specific behavior/side-effect assessment and named verification.
- [ ] 4.5 Instantiate one review work order per ledger target; accept when each `P4-R<n>` has one target, evidence of preserved behavior and a passing named check. An unverified fix blocks P4. If additional test/repair is needed, reopen preparation and revalidate descendants; do not rely on P5 to repair P4.
- [ ] 4.6 Capture residual Ruff findings; accept when the complete remaining location/rule inventory is available to P5 and no rejected automatic change is concealed.

### VALIDATE

- [ ] 4.7 Execute the accepted review-ledger checks; accept only successful results for every concrete instance.
- [ ] 4.8 Run full-inventory format check; accept only exit 0.
- [ ] 4.9 Run full pytest for R; accept only exit 0 and its durable summary.
- [ ] 4.10 Execute manifest checks mandatory in P4: all existing required checks, shared compilation and full-inventory format check; accept only successful statuses. Full-inventory lint remains diagnostic backlog until P5.
- [ ] 4.11 Run shared compilation for R; accept only exit 0.
- [ ] 4.12 Review phase-exit skips; accept only justified reasons.
- [ ] 4.13 Publish the P4 exit report; accept when the automatic-only delta, accepted review ledger, residual backlog and R refer to the same source state.

## 5. P5 — Manual resolution (3.2)

**Prerequisite:** P4 exit. **Deliverables/exit:** P5 row above. No checkbox authorizes a batch of unidentified edits.

### RED

- [ ] 5.1 Create the diagnostic disposition ledger; accept when each residual root cause has a unique ID, affected messages/locations, classification (defect/simplification/intentional construct) and a single-target verification plan.
- [ ] 5.2 Instantiate RED work orders from the ledger; accept when every target has the applicable instance below or an evidence-backed zero-target result.

**Binding RED instances, separately checkable:**

- `P5-D<n>-RED`: add one regression test for one real defect; accept when it fails for the demonstrated defect on the pre-fix state, not an unrelated setup failure.
- `P5-S<n>-RED`: record one awkward construct and one behavior-preservation assertion; accept when current behavior and the exact proposed equivalent transformation are identified.
- `P5-N<n>-RED`: record one necessary intentional construct; accept when the exact diagnostic and necessity rationale show why replacing the construct would violate the named requirement.

### GREEN

- [ ] 5.3 Instantiate GREEN work orders for the accepted RED targets; accept when each has one applicable action below and no target lacks a concrete work item.

**Binding GREEN instances, executed independently:**

- `P5-D<n>-GREEN`: correct the one defect from the matching RED instance; accept when its regression test passes and the targeted Ruff finding disappears.
- `P5-S<n>-GREEN`: simplify the one construct from the matching RED instance; accept when its behavior assertion passes and the targeted Ruff finding disappears.
- `P5-N<n>-GREEN`: add one specific suppression with a substantive inline or adjacent rationale; accept when the necessary behavior is unchanged and Ruff accepts that code without an unused suppression. Do not suppress unrelated messages.

- [ ] 5.4 Format the manual-fix delta; accept when it contains only the accepted work-order edits and their formatter output.
- [ ] 5.5 Document recurring justified exceptions in the contributor guide; accept when each documented example links to an accepted necessity rationale, or explicitly record that no recurring exception exists.

### INTROSPECT

- [ ] 5.6 Review the resolution ledger; accept when every original finding maps to an accepted instance and no finding was removed by weakening configuration/tests.
- [ ] 5.7 Review suppression explanations; accept when each names the necessary construct rather than merely stating that Ruff complained, with no blanket or obsolete suppression.
- [ ] 5.8 Review manual-change scope; accept when lifecycle/domain guarantees and stress fixtures remain protected by their named tests.

### VALIDATE

- [ ] 5.9 Run all named work-order regression/equivalence checks; accept only successful results for every instance.
- [ ] 5.10 Run full-inventory Ruff check; accept only exit 0 with no unresolved findings, including SIM115 and RUF100.
- [ ] 5.11 Run full-inventory format check; accept only exit 0.
- [ ] 5.12 Run full pytest for G; accept only exit 0 and its durable summary.
- [ ] 5.13 Execute all manifest checks mandatory in P5: both existing required checks and every new adoption check are now active; accept only successful statuses with no unresolved ty diagnostics.
- [ ] 5.14 Run shared compilation for G; accept only exit 0.
- [ ] 5.15 Review phase-exit skips; accept only justified reasons.
- [ ] 5.16 Publish the P5 exit report; accept when the resolution ledger and G identify the same final manual-fix state.

## 6. P6 — Permanent local and GitHub control

**Prerequisite:** P5 exit. **Deliverables/exit:** P6 row above. This phase activates the permanent pre-commit rule; all its own new Python sources must satisfy G.

### RED

- [ ] 6.1 Add local-integration contract tests; accept when deliberate isolated lint/format/syntax/type failures have named expected nonzero outcomes and omitted existing commands are detected.
- [ ] 6.2 Add workflow-contract tests; accept when fixtures detect dropped push/PR triggers, narrower source/test coverage, mutation commands and swallowed failure statuses.
- [ ] 6.3 Add skip-policy tests; accept when fixtures distinguish documented environmental/non-applicability conditions from unknown reasons, without a fixed numeric skip count.
- [ ] 6.4 Add log-retention/status tests; accept when a failed check must remain failed while diagnostic output is available temporarily or as a retention-limited artifact.

### GREEN

- [ ] 6.5 Add the AGENTS green-check rule; accept when it requires G after every major change and before committing without replacing existing lifecycle guidance. Define a major change as a completed implementation phase or cohesive behavior/refactor unit; any commit requires G regardless of size.
- [ ] 6.6 Add the contributor-guide link to README.md; accept when the link resolves and its Russian text describes the agreed rule.
- [ ] 6.7 Add the contributor-guide link to README.en.md; accept when the link resolves and its English text describes the same rule.
- [ ] 6.8 Add the contributor-guide link to README.zh.md; accept when the link resolves and its Chinese text describes the same rule.
- [ ] 6.9 Wire local green-loop checks; accept when 6.1 tests pass and every previously configured command remains required.
- [ ] 6.10 Implement skip-review enforcement for local/CI reports; accept when 6.3 tests reject unexplained skips and allow only explicitly documented applicability conditions.
- [ ] 6.11 Declare CI development/test provisioning; accept when a clean Python 3.14 environment installs the declared Ruff group and all P1 prerequisites, with no separate drifting Ruff version declaration.
- [ ] 6.12 Extend Validation workflow check execution; accept when 6.2 tests confirm non-mutating Ruff/format/compilation, existing type commands and the full mandated pytest under retained push/PR triggers.
- [ ] 6.13 Add CI diagnostic retention; accept when 6.4 tests pass and artifact-upload success cannot replace a check's failure status. Permanent full-log storage is not required.
- [ ] 6.14 Document permanent check operation; accept when executable commands distinguish local-before-commit checks from post-push/PR verification and state that hooks/automatic commits/branch protection are not implemented here.

### INTROSPECT

- [ ] 6.15 Compare local/CI coverage; accept when the comparison shows identical Ruff version/rules/source scope and no narrowed mandatory test/type commands.
- [ ] 6.16 Audit workflow non-mutation; accept when no check uses write-mode fixes/formatting, commits changes or hides nonzero tool status.
- [ ] 6.17 Audit evidence behavior; accept when durable summaries are self-contained and no permanent full-log requirement or temporary-path durable link was introduced.

### VALIDATE

- [ ] 6.18 Run focused integration tests; accept only all local/workflow/skip/retention contract fixtures passing.
- [ ] 6.19 Run full-inventory Ruff check; accept only exit 0.
- [ ] 6.20 Run full-inventory format check; accept only exit 0.
- [ ] 6.21 Run full pytest for G; accept only exit 0 and its durable summary.
- [ ] 6.22 Execute all manifest checks mandatory in P6: every command in both sections; accept only successful statuses.
- [ ] 6.23 Run shared compilation for G; accept only exit 0.
- [ ] 6.24 Review phase-exit skips; accept only justified reasons.
- [ ] 6.25 Publish the P6 exit report; accept when implemented local/CI policy, focused tests and G apply to the same exit state.

## 7. P7 — Final acceptance

**Prerequisite:** P6 exit. **Deliverables/exit:** P7 row above. No basic implementation/testing/documentation from earlier phases may be deferred here.

### RED

- [ ] 7.1 Produce a contract-to-evidence gap matrix; accept when every required outcome, phase deliverable and gate has a named evidence source or an explicitly missing item. Do not manufacture test failures for absent documentation.

### GREEN

- [ ] 7.2 Assemble the final acceptance index; accept when the matrix has no missing deliverable and distinguishes historical phase results from current final-state checks.

### INTROSPECT

- [ ] 7.3 Review migration boundaries; accept when source fingerprints/diffs prove separate preparation, formatter-only, autofix, manual and control deltas without requiring commits or index resets.
- [ ] 7.4 Review final scope; accept when no runtime inventory, historical verification artifact, stress construction or lifecycle/architecture guarantee was weakened.
- [ ] 7.5 Review evidence freshness; accept when stale predecessor verification is identified and all affected required checks are scheduled for final rerun rather than represented as current.

### VALIDATE

- [ ] 7.6 Run final full-inventory Ruff check; accept only exit 0 with zero unresolved findings.
- [ ] 7.7 Run final full-inventory format check; accept only exit 0.
- [ ] 7.8 Verify final formatter idempotence; accept when a second formatter pass changes no source contents.
- [ ] 7.9 Run final full mandated pytest; accept only exit 0 and the self-contained durable summary of counts/warnings/environment/status.
- [ ] 7.10 Execute the complete final check manifest: every command in both sections is mandatory in P7; accept only successful status for every command and zero unresolved ty diagnostics.
- [ ] 7.11 Run final shared compilation; accept only successful compilation for the recorded final source inventory.
- [ ] 7.12 Review final skip reasons; accept only justified environmental/non-applicability dispositions and no hidden collection reductions.
- [ ] 7.13 Execute CI-equivalent commands in the provisioned environment; accept only successful actual results, explicitly distinguishing local simulation from a remotely observed GitHub run. Lack of a remote run must be disclosed, not invented.
- [ ] 7.14 Publish final acceptance; accept only if all P1–P7 work orders/exits and final G pass for the final fingerprint. Otherwise publish BLOCKED with unresolved IDs and leave the task unchecked. Confirm no unauthorized version-control operation occurred.

## Workflow follow-up

- Obtain explicit authorization before any staging or commits; preserve the separately requested formatting-only commit boundary.
- Request review before archive. Archive only on a separate request; no product delta specs require sync.

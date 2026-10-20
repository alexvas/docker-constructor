# P7 contract-to-evidence matrix and acceptance index

Scope: tasks.md §0 outcomes and P1–P7 deliverables/gates. P1–P6 results are historical, not current verification. [phase-7.md](phase-7.md) records the completed boundary/scope/freshness reviews and fresh final local G (tasks 7.3–7.12). The table preserves the initial RED gap assessment. **Final status: no unresolved items.** [p7-final-acceptance.md](p7-final-acceptance.md) records the user's hosted CI acceptance, provenance limits and completion of 7.13/7.14. The local privilege limitation and initial RED gaps below remain historical observations, not current blockers.

| Contract / deliverable | Evidence | Gap at P7 RED |
| --- | --- | --- |
| Development-only Ruff 0.17.1; py314; exact rules; no unsafe/preview | pyproject.toml; phase-2.md §§2.5–2.6; tooling policy tests | None |
| Deterministic complete source coverage, explicit exclusions, extensionless scripts, encoding/no execution/bytecode, parity | source-inventory.md; source-inventory.txt; source-inventory-p2.txt; phase-2.md; tests/enforcement/test_python_{source_selection,syntax_compilation,ruff_parity,check_status,check_lifecycle}.py | None |
| P1 inventories, baseline failures, skips, assertion preparation and enforcement map | phase-1.md; check-manifest.md; environment-prerequisites.md; skip-inventory.md; layout-assertion-inventory.md; preparation-ledger.md; enforcement-ruff-map.md | None |
| P1 accepted RED/GREEN and R exit | phase-1.md; preparation-ledger.md | None; historical |
| P2 tooling, fixtures, guide, ownership review and initial backlog | phase-2.md; p2-exclusion-correction.md; ruff-findings-p2.txt; docs/python-checks.md | None |
| P2 focused fixtures and R exit | phase-2.md | None; historical |
| P3 formatter-only delta and baseline | phase-3.md; p3-source-before.sha256; p3-source-after.sha256; p3-delta-purity.md | None |
| P3 AST/type-comment equivalence, idempotence, format and R | p3-ast-equivalence.md; p3-idempotence.md; phase-3.md | None; historical |
| P4 automatic delta, rejected fixes, target reviews, residual diagnostics | phase-4.md; p4-review-ledger.md; p4-source-{before,after}.sha256; p4-ruff-findings-{before,residual}.txt | None |
| P4 accepted review checks, format and R exit | phase-4.md | None; historical |
| P5 all residual findings, atomic dispositions, preserved exports, suppressions and exception guidance | phase-5.md; p5-resolution-ledger.md; p5-ledger-check.txt; p5-target-collect.txt; p5-ruff-findings-before.txt; p5-source-{before,after}.sha256; docs/python-checks.md | None |
| P5 named verification, zero backlog and G exit | phase-5.md | None; historical |
| P6 local mandatory rule, multilingual README links, non-mutating gate, failure/retention/skip contracts | AGENTS.md; README.md; README.en.md; README.zh.md; docs/python-checks.md; scripts/check-green; scripts/green_checks.py; tests/green_skip_policy.py; p6-contract-ledger.md | None |
| P6 CI provisioning, push/PR, full coverage, status retention and worker parity | .github/workflows/validation.yml; docs/testing.md; p6-exit-results.md; P6 repair ledgers/results linked from phase-6.md and verification directory | None; hosted provisioning accepted by user, not independently observed |
| P6 fixture/process repairs retain lifecycle guarantees | p6-ci-repair-ledger.md; p6-ci2-repair-ledger.md; p6-live-join-ledger.md; p6-bootstrap-ownership-ledger.md; corresponding results; p6-ci4-fixture-results.md; p6-ci5-descriptorless-snapshot-results.md; p6-ci6-scheduling-observation-results.md | None |
| P6 focused contracts and current-at-exit G | phase-6.md; p6-exit-results.md | None; historical |
| Separate preparation / format / automatic / manual / control deltas | P1–P6 reports, per-file manifests and repository revision boundaries; final review in phase-7.md | Final review pending |
| Runtime inventory, archives, stress fixtures, API/lifecycle/architecture preservation | P1–P6 scope reviews; retained full-suite enforcement; final review in phase-7.md | Final review pending |
| P7 final lint, format, idempotence, full mandated pytest, types, compile, skip review | phase-7.md | Fresh execution pending; no predecessor result substituted |
| P7 CI-equivalent execution and provenance disclosure | phase-7.md | Fresh local CI-profile execution pending; no remote observation claimed |
| P7 final acceptance, source binding and version-control disclosure | phase-7.md | Final decision pending successful checks |

No predecessor deliverable is missing. P7 execution/review gaps above are the pending P7 tasks, not deferred earlier-phase implementation. This file indexes the existing evidence without changing historical reports or asserting that historical passes apply to current source.

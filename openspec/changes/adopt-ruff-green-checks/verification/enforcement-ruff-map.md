# P1 enforcement-to-Ruff map

P1 enables no Ruff rules and deletes no enforcement checks. The intended set
(P2) is `E4`, `E7`, `E9`, `F`, `I`, `B`, `SIM115`, `RUF100` with target
`py314`; no `ALL`, preview, unsafe fixes, TID251, S603 or S607.

## Classification rule

A check is **retained** unless an enabled rule with its negative fixtures
demonstrably proves the same invariant. `I` sorts imports but does not enforce
architecture; `SIM115` suggests a context manager for file opening but is not
an ownership/transfer proof; `F` detects unused/undefined names but not
dependency direction or retained owners; `B` covers common bug patterns but not
process budgets or lifecycle outcomes. Therefore every lifecycle/architecture
assertion is retained.

## Module/contract map

Every test function in a listed module inherits that module/contract
classification; no individual test in the module is reclassified or exempted.

| Enforcement contract | Tests / helper | Classification | Enabled-rule overlap and concrete rules | Negative fixtures retained |
| --- | --- | --- | --- | --- |
| Descriptor ownership, direct-close classification, transfer/handoff | `test_descriptor_close_convergence.py` (ownership ledger classes, `NpmDomainAuthorityTests`, phase-2 seams, evasion fixtures) | **Retained** | `SIM115` (context-managed file opening only), `B` (bugbear patterns). Neither proves single-owner release or primary-error precedence. | inline unowned/multi-close/handoff bodies, borrowed file-wrapper case, new-module/reverse-import fixtures |
| Foundation surface, compatibility re-exports, consumer imports, dependency direction, sole secure walker, migrated raw closes | `test_filesystem_convergence.py` (`RepositoryWideAstContractTests`, static/runtime audits) | **Retained** | `I` only sorts; `F` only unused names. `E4/E7/E9` are syntax/whitespace. No rule enforces a public surface, allowed import set or DAG. | intentionally violating fixture (`test_contract_detects_intentionally_violating_fixture`), second-walker fixture, raw-close allowlist fixtures, runtime module-load matrix |
| Executable lifecycle guide, cross-references, project guidance | `test_filesystem_lifecycle_docs.py`, `test_process_guidance.py`, `test_process_contract.py::test_executable_process_guide_and_links`, `test_process_guidance.py::test_every_readme_resolves_both_ownership_guides_and_agent_rule` | **Retained** | No Ruff rule executes documented examples or resolves links. | forbidden process examples, missing/invalid guide links, guidance token/index checks |
| Guide test links (prepared) | `test_lifecycle_guide_links.py` | **Retained**, prepared by `P1-A2` | No Ruff equivalent for named-test link resolution. | missing file, repository escape, wrong/missing/duplicate symbol, line fragment, directory link, document anchor |
| Native prepared-file seam | `test_native_file_contract.py` | **Retained** | `SIM115` may flag file opening but cannot prove prepared-before-acquisition or sole close delegate. | reordered ownership, missing close, double close, fake import, five-reader inventory |
| Process public API, signatures, result fields, transfer/release structure, policy budgets, reader barriers | `test_process_contract.py` (API class, `process_proof`, `policy_proof`, mutation classes) | **Retained**, mutation sites prepared by `P1-A3`…`P1-A7` | `B` covers generic bugbear patterns; `SIM115` only file opening. No rule encodes escrow slots, prepared owners, deadline budgets or reader barriers. | ownership/import evasion, inherited coordinator, second shutdown, raw-child control, unsafe liveness, action/budget evasions, reader-barrier mutations |
| NPM adapter delegation, strict handoff, diagnostics provenance, executable guide | `test_npm_environment_process_contract.py` | **Retained**, diagnostic mutations prepared by `P1-A9` | `B`/`F`/`I` do not prove delegation shape or bounded sanitization. | adapter evasion, alias/coordinator additions, raw notes/guessed labels, standalone collector boundary |
| Complete-tree process origin/prepared ownership/pipe inventory | `process_convergence.py`, `test_process_integrated_convergence.py` | **Retained**, mutation/inventory sites prepared by `P1-A3`…`P1-A10` | No rule detects process factories, PIPE aliases, dependency DAG or prepared-pipe ledger. | new-module/alias authority, dynamic import/relative reverse import, DAG mutations, budget/retention mutations, canonical inventory check |
| Whole-tree implicit release/API inventory | `release_inventory.py`, `test_release_inventory.py` | **Retained** | `SIM115` is not an ownership proof; no rule inventories implicit factories/handoffs. | new-module fdopen/mkstemp fixture; count/origin/domain-field assertions |
| Layout-independence preparation coverage | `test_layout_independence.py`, `source_layout.py` (new) | **Retained** | Not a lint replacement; protects the enforcement suite from formatter layout coupling. | harmless layout variants + named invalid mutations per work order |

## Conclusion

- **Retained:** every module above; no enforcement test is deleted or
  downgraded in P1.
- **Demonstrably equivalent candidate:** none. No rule in the agreed set
  replaces the ownership, process-budget, API, dependency-DAG, runtime-import,
  inventory or documentation contracts, including their negative fixtures.
- Enabling/removal decisions and any future `noqa` review belong to P5; P1 only
  records this map and prepares layout-robust assertions.

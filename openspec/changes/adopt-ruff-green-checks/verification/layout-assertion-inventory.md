# P1 layout-sensitive assertion inventory

Scope: `tests/enforcement/`. An entry names the assertion or expectation, the
invariant it protects, the harmless layout mutation that could move it, the
preparation work order and the disposition. "Harmless" means AST-equivalent
reformatting (`ruff format` 0.17.1, target `py314`) or adding unrelated lines.

## Migration entries (work orders)

| Work order | Assertion site | Protected invariant | Harmless mutation | Disposition |
| --- | --- | --- | --- | --- |
| `P1-A0` | `test_process_integrated_convergence.py::test_generated_architecture_and_runtime_packaging_boundary` inventory path | current architecture/pipe inventory is checked against a live derived report | archived change directory renamed to `openspec/changes/archive/2026-10-18-…` | replaced the stale path with the single test-owned inventory at `tests/fixtures/process-architecture-inventory.json` (`P1-A14`); raw hashes and ledger lines replaced by AST fingerprints and line-free rows (`P1-A10`) |
| `P1-A1` | `_static_violations` secure-walker expectation (`docker/filesystem/descriptors.py:239`) | exactly one `open_secure_path` definition, in the foundation module only | insert a blank line/comment above the definition (line shift), or wrap its signature | redundant: replaced by a module-level structural check; uniqueness and second-walker rejection retained |
| `P1-A2` | `test_lifecycle_guide_links.py::_check_local_link` plus `#L<n>` links in `docs/{process-lifecycle,npm-environment-assembler,filesystem-lifecycle}.md` | each guide links a real test with the named qualified test already present | add/remove lines above a linked test, or reformat the test module | line fragments removed; the checker resolves the Markdown-title symbol in the target AST, requires exactly one definition, and rejects any fragment |
| `P1-A3` | `test_process_contract.py::test_policy_wait_and_one_time_deadline_ast_semantics` and integrated deadline entry | deadline/request state is written only inside the guarded block | wrap the following `if` condition in parentheses/newlines | complete statement-sequence targets located by AST; mutation inserts the unguarded write and is still rejected |
| `P1-A4` | `test_process_contract.py::test_stop_policy_ownership_and_budget_evasions_are_rejected` (protected action call and `ordinary=` keyword list) | configured child observation and unexpected-defect classification | explode the call arguments over lines with a trailing comma | AST call/argument/keyword locators; both mutations still rejected |
| `P1-A5` | `test_process_contract.py::test_new_module_evasions_fail_proof` (`self._safe…` boolop, `record` statement) | descriptorless release still requires the callable reader check; transfer failure stays classified | wrap the `if` test in parentheses; wrap the `record` call | complete expression/statement targets; mutations still rejected |
| `P1-A6` | `test_process_contract.py::test_reader_barrier_predicate_ast_semantics` | sole `_safe` predicate grants reader-stop authority | wrap/rewrap the `return` expression | exact text replaced by structural Return target; probe showed this snippet formatter-stable, conversion recorded as extra robustness |
| `P1-A7` | `test_process_contract.py::test_release_and_completion_ast_semantics` (release `if`, completion `Return`) | release requires the prepared-owner/reader barrier; `_complete` must not report false completion | wrap the `if` test and the `return` expression | full `If` statements and full `Return` targets located by AST; all ten mutations still rejected |
| `P1-A8` | `test_process_integrated_convergence.py::test_integrated_transfer_budget_and_retention_mutations`, `test_foundation_transfer_and_synchronized_release_remain_required`, `test_policy_clock_precedes_action_start` | same process/foundation invariants exercised through `convergence_report` | formatter wrapping, argument explosion, and line insertion | converted to the shared structural locator; header insertion uses `insert_before`; coverage count preserved |
| `P1-A9` | `test_npm_environment_process_contract.py::test_raw_notes_and_guessed_legacy_labels_are_rejected` (`_render_close_failure` call, cleanup-prefix `BinOp`) | notes remain bounded/sanitized and provenance is not guessed | explode call arguments; wrap the binary expression | expression targets located by AST; both mutations still rejected |
| `P1-A10` | architecture inventory equality (`test_process_integrated_convergence.py` and the new focused test) | generated signatures, dependency DAG, process surface, adapters and ledger are current | any formatting of production source changes raw hashes and absolute ledger lines | new canonical projection: AST fingerprints with docstring text preserved (only the common formatter-owned margin normalized) and line-free ledger rows; semantic edits—including docstring content (`P1-A12`)—still change the report and are rejected |
| `P1-A11` | archived inventory path (baseline failure) | the equality fixture must exist | archive move | replaced by the test-owned canonical fixture at `tests/fixtures/process-architecture-inventory.json`, removing dependence on both the historical archive and the active change directory (`P1-A14`) |

## Recorded no-change dispositions

| Site | Why no edit is needed |
| --- | --- |
| `test_descriptor_close_convergence.py` source mutations (`test_prepared_policy_drain_budget_helper_and_deferred_evasions`, ~line 3014) | mutation strings are single-line statements/expressions with stable token text under the probed formatter; the proof itself is AST-based. Probe passed. |
| `test_native_file_contract.py` mutations | proof compares `ast.dump(..., include_attributes=False)`; the mutation snippets are short statements/expressions that the probe left intact. |
| `test_process_guidance.py` `FORBIDDEN` entries | source examples are short statements and the audit is AST/token based; probe passed. |
| `test_npm_environment_process_contract.py` adapter/protocol evasions | exact snippets remained present in the formatted probe; the proof is AST-based. |
| `test_process_contract.py::_body_matches` and `process_proof` token checks | compare parsed ASTs or `ast.unparse` output, not source layout. |
| `process_convergence.py`, `release_inventory.py` line-order comparisons | compare *relative* statement order or report line values without an exact line fixture; formatting preserves statement order and indentation. `test_release_inventory.py` compares path sets/counts, not absolute lines. |
| `docs/filesystem-lifecycle.md`, `docs/process-lifecycle.md`, `docs/npm-environment-assembler.md` executable examples and token checks | examples execute as isolated snippets; token presence is not line-coupled. No formatting of documentation examples is part of P3. |

## Assertion-preparation documentation (1.9)

- Every changed assertion above links to `P1-A0`…`P1-A14` in
  [`preparation-ledger.md`](preparation-ledger.md).
- No enforcement check was deleted or weakened: secure-walker uniqueness,
  release authority, process budgets, dependency-DAG, API, runtime-import and
  documentation tests remain.
- No unreviewed assertion change remains; the diff is limited to the sites in
  the ledger plus the shared `source_layout.py` helper and the focused
  `test_layout_independence.py` coverage.

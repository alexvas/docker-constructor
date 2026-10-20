# P1 preparation work-order ledger

Binding protocol: every instance has one target, one action, prerequisites,
expected output, a named verification and before/after results. All `RED`
instances were executed before `GREEN`. Evidence logs were temporary and are
summarized here; no container-local path is used as a durable link.

Instances `P1-A13`/`P1-A14` were added when P1 preparation was revised: the
inner completion-predicate negative fixture was restored and the authoritative
architecture inventory moved to the test-owned `tests/fixtures/` directory.
Current gate results for that revised state are recorded in `phase-1.md`
(re-verification section); all earlier counts remain historical.

## RED evidence shared by the instances below

1. **Baseline full suite** (before preparation):
   `.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q`
   → exit **1**: **1 failed, 7898 passed, 13 skipped, 11 warnings, 18026
   subtests passed**. The failure was `FileNotFoundError` for
   `openspec/changes/eliminate-unsafe-direct-descriptor-closes/phase6bd-process-inventory.json`
   (archived path). Recorded as an unsuccessful baseline, not a pass.
2. **Harmless-layout probe**: a temporary copy of the working tree was formatted
   with `ruff 0.17.1 format --target-version py314 docker tests scripts`
   (**367 files reformatted, 19 unchanged**; diagnostic only, not an activated
   check). Before P1 preparation, `pytest tests/enforcement` on that copy →
   exit **1**, **25 failed, 271 passed, 669 subtests passed**. The 25 failures
   were 24 layout-coupling failures plus the same stale-inventory-path failure.
3. **Focused RED tests** were then added with the initial exact-match helper:
   `pytest tests/enforcement/test_layout_independence.py -q` → exit **1**,
   **7 failed** (secure walker line shift, guide line anchor, deadline guard,
   stop-policy call/keyword, release guard, completion return, npm diagnostic).
   These failures demonstrate the old assertions' brittleness; they were not
   manufactured by breaking product code.

## Instances

### P1-A0 — stale archived inventory path

- Prerequisite: none.
- Target: the inventory fixture path in
  `test_process_integrated_convergence.py`.
- Action: stop depending on the archived change directory; use the single
  authoritative, test-owned fixture
  `tests/fixtures/process-architecture-inventory.json` (see `P1-A10`/`P1-A14`).
- Expected output: the equality test finds exactly one fixture and compares the
  current report.
- Verification: `pytest tests/enforcement/test_process_integrated_convergence.py -q`.
- RED: baseline `FileNotFoundError` (exit 1).
- GREEN: test passes and `P1-A10` adds layout independence; formatted probe
  passes.

### P1-A10 — architecture inventory layout coupling

- Prerequisite: `P1-A0`.
- Target: raw `source_sha256` plus absolute `ledger[*].line` equality.
- Action: add `canonical_architecture_report` with AST fingerprints and
  line-free ledger rows; write the canonical
  [`tests/fixtures/process-architecture-inventory.json`](../../../../tests/fixtures/process-architecture-inventory.json)
  from the current source. The fingerprint preserves docstring text and
  normalizes only the formatter-owned common indentation margin and surrounding
  blank lines (see `P1-A12`). The relocated, test-owned location is recorded in
  `P1-A14`.
- Expected output: formatter-only source changes keep the report equal; a
  semantic edit, including a docstring-content edit, changes it.
- Verification:
  `pytest tests/enforcement/test_layout_independence.py::test_architecture_inventory_ignores_layout_and_rejects_semantic_change tests/enforcement/test_layout_independence.py::test_architecture_inventory_detects_docstring_content_change -q`.
- RED: in the formatted probe the raw-hash/line equality failed.
- GREEN: both tests pass; the same fixture matches both the unformatted working
  tree and the formatted probe (all 106 production fingerprints identical).

### P1-A12 — docstring content was erased from the fingerprint

- Prerequisite: `P1-A10`.
- Target: `_syntax_fingerprint` docstring handling.
- Action: replace blanking every docstring with `inspect.cleandoc`, which keeps
  the text while normalizing the common indentation margin the formatter may
  rewrite. Regenerate the canonical fixture.
- Expected output: changing a docstring word invalidates the fixture; a
  formatter-only docstring reindent does not.
- Verification:
  `pytest tests/enforcement/test_layout_independence.py::test_architecture_inventory_detects_docstring_content_change -q`.
- RED: with blanking, the mutated `docker/filesystem/streams.py` docstring and
  the original produced equal fingerprints (checked directly: `blanked:
  original == changed: True`).
- GREEN: `cleandoc` keeps them different (`False`); the focused test passes and
  the formatted probe reports 0 fingerprint mismatches.

### P1-A11 — archived fixture location

- Prerequisite: `P1-A0`.
- Target: fixture location.
- Action: replace dependence on the historical archive with the single
  test-owned canonical inventory at `tests/fixtures/` (`P1-A14`).
- Expected output: no reference to a mutable/historical archive path and no
  reference to the active OpenSpec change directory.
- Verification: `git grep -n "eliminate-unsafe-direct-descriptor-closes/phase6bd-process-inventory" -- tests` returns no current-test match.
- RED: baseline failure (entry above).
- GREEN: no match; `P1-A10` supplies the fixture.

### P1-A13 — restored inner completion-predicate negative fixture

- Prerequisite: `P1-A7`.
- Target: `_complete`'s action predicate
  `result.done and result.cleanup_complete` in
  `test_release_and_completion_ast_semantics`.
- Action: restore the separate `replace_snippet` case that flips only the inner
  `and` to `or`. `_COMPLETE_OR_THIRD` flips a top-level conjunction (the third
  ` and ` in the return), so it does not cover this predicate; both cases are
  retained.
- Expected output: `process_proof` reports `false completed cleanup` for the
  inner `or` mutation as well as the retained top-level cases.
- Verification:
  `pytest tests/enforcement/test_process_contract.py::test_release_and_completion_ast_semantics -q`.
- RED: the case was absent, leaving the `done and cleanup_complete` conjunction
  unprotected by an independent `or` mutation.
- GREEN: all ten cases pass, including the restored inner-`or` case and the
  unchanged `_COMPLETE_OR_THIRD` case.

### P1-A14 — canonical fixture relocated out of the active change

- Prerequisite: `P1-A0`, `P1-A10`.
- Target: the authoritative architecture inventory location
  `openspec/changes/adopt-ruff-green-checks/verification/process-architecture-inventory.json`.
- Action: move the single authoritative fixture to the test-owned
  `tests/fixtures/process-architecture-inventory.json`, delete the old copy and
  point `test_process_integrated_convergence.py` and
  `test_layout_independence.py` at it. Document the regeneration command in
  `tests/fixtures/README.md`.
- Expected output: the fixture is still byte-identical to
  `canonical_architecture_report(_sources())`, and archiving the OpenSpec change
  cannot turn the tests into a missing-fixture failure.
- Verification:
  `pytest tests/enforcement/test_process_integrated_convergence.py::test_generated_architecture_and_runtime_packaging_boundary tests/enforcement/test_layout_independence.py -q`.
- RED: after archiving/moving the change directory the old path no longer
  exists; the location was coupled to change lifecycle rather than test state.
- GREEN: 10 focused tests pass (the fixture equality test plus all 9 layout
  tests); no path under `openspec/changes/` is referenced by the tests.

### P1-A1 — secure-walker line expectation

- Prerequisite: none.
- Target: `_static_violations` `expected_walker` (exact `descriptors.py:239`).
- Action: compare defining module names and occurrence count instead of a
  line.
- Expected output: a two-line shift still passes; a second walker in any module
  fails.
- Verification: `pytest tests/enforcement/test_layout_independence.py::test_secure_walker_survives_harmless_line_shift tests/enforcement/test_filesystem_convergence.py -q`.
- RED: with a harmless comment/blank line inserted above the definition the old
  check reported `['…descriptors.py:241'] != ['…descriptors.py:239']`.
- GREEN: passes; the second-walker fixture still produces a `secure walkers`
  violation. The pre-existing intentionally-violating fixture continues to be
  rejected.

### P1-A2 — guide test-line anchors

- Prerequisite: none.
- Target: `_check_local_link` `#L<n>` validation and 12 guide links.
- Action: require a Markdown-title symbol, resolve it uniquely in the target
  AST, and reject any line fragment.
- Expected output: a link without a fragment to an existing named test passes;
  wrong/duplicate/missing names and any `#L…` fragment fail.
- Verification: `pytest tests/enforcement/test_lifecycle_guide_links.py -q`.
- RED: the no-fragment link failed with `missing/invalid test line`; the
  formatted probe failed all three guides after tests moved.
- GREEN: 16 tests pass, including the new positive, duplicate-name and negative
  cases; the focused layout test passes.

### P1-A3 — one-time deadline guard

- Prerequisite: `P1-A1` (shared structural helper).
- Target: `_request.set()` + following guard block in `docker/process.py`.
- Action: locate the complete statement sequence structurally and insert the
  unguarded deadline/request write; apply the same mutation in the integrated
  suite.
- Expected output: `one-time request/deadline guard` is reported.
- Verification: `pytest tests/enforcement/test_process_contract.py::test_policy_wait_and_one_time_deadline_ast_semantics tests/enforcement/test_layout_independence.py::test_deadline_guard_mutation_survives_wrapped_condition -q`.
- RED: exact lookup failed on the wrapped condition in the probe.
- GREEN: all six contract cases and the focused wrapped-case test pass.

### P1-A4 — stop-policy call and keyword classification

- Prerequisite: `P1-A1`.
- Target: `self._action(observe_returncode, …)` and the `ordinary=()` keyword
  list.
- Action: locate calls/arguments/keywords by AST; mutation still changes the
  protected action or classification.
- Expected output: `process_proof` reports errors for both mutations.
- Verification: `pytest tests/enforcement/test_process_contract.py::test_stop_policy_ownership_and_budget_evasions_are_rejected tests/enforcement/test_layout_independence.py::test_stop_policy_call_and_keyword_mutations_survive_exploded_arguments -q`.
- RED: exact lookup failed on the exploded call and multiline keywords.
- GREEN: all twenty contract cases and the focused test pass.

### P1-A5 — new-module evasions (boolop and record)

- Prerequisite: `P1-A1`.
- Target: `self._safe(…) or … or not callable(…)` and
  `self._ledger.record(…)` in `docker/process.py`.
- Action: full-expression/statement structural targets.
- Expected output: mutations stay rejected.
- Verification: `pytest tests/enforcement/test_process_contract.py::test_new_module_evasions_fail_proof -q`.
- RED: exact lookup failed on the reformatted boolop.
- GREEN: all eight cases pass.

### P1-A6 — reader-barrier predicate

- Prerequisite: `P1-A1`.
- Target: `return not self._started[index] or self._stopped[index].is_set()`.
- Action: structural Return mutation.
- Expected output: every replacement still reports
  `no tracked reader-stop authority`.
- Verification: `pytest tests/enforcement/test_process_contract.py::test_reader_barrier_predicate_ast_semantics -q`.
- RED: probe left the snippet intact (formatter-stable), so no RED failure was
  observed for this site; existing robustness is recorded. The conversion adds
  protection against future wrapping without changing coverage.
- GREEN: five cases pass.

### P1-A7 — release barrier and completion predicate

- Prerequisite: `P1-A1`.
- Target: release `if` statement and `_complete` `Return`.
- Action: full `If`/`Return` structural targets, including nested mutations.
- Expected output: `release lacks prepared-owner/reader barrier` and
  `false completed cleanup` remain reported.
- Verification: `pytest tests/enforcement/test_process_contract.py::test_release_and_completion_ast_semantics tests/enforcement/test_layout_independence.py::test_release_guard_mutations_survive_parenthesized_test tests/enforcement/test_layout_independence.py::test_completion_predicate_mutation_survives_wrapped_return -q`.
- RED: exact lookup failed on the wrapped `if` and `return`.
- GREEN: all ten contract cases and both focused tests pass.

### P1-A8 — integrated transfer/budget/foundation mutations

- Prerequisite: `P1-A3`…`P1-A7`.
- Target: `test_integrated_transfer_budget_and_retention_mutations`,
  `test_foundation_transfer_and_synchronized_release_remain_required`,
  `test_policy_clock_precedes_action_start`.
- Action: use the shared structural locator; add `insert_before` for header
  insertion; keep the same number of parametrized cases plus one dedicated
  owner-reset test replacing the removed prepend case.
- Expected output: the same mutants are rejected on wrapped/exploded source.
- Verification: `pytest tests/enforcement/test_process_integrated_convergence.py -q`.
- RED: four integrated cases failed in the formatted probe.
- GREEN: 71 tests pass; formatted probe passes.

### P1-A9 — npm diagnostic provenance

- Prerequisite: `P1-A1`.
- Target: `_render_close_failure(…)` and
  `_process_cleanup_prefix(failure) + ": "`.
- Action: expression structural targets.
- Expected output: `diagnostic_proof` reports the unsanitized/guessed-label
  mutation.
- Verification: `pytest tests/enforcement/test_npm_environment_process_contract.py::test_raw_notes_and_guessed_legacy_labels_are_rejected tests/enforcement/test_layout_independence.py::test_npm_diagnostic_mutation_survives_exploded_call -q`.
- RED: exact lookup failed on the exploded call.
- GREEN: all four contract cases and the focused test pass.

## Formatted-probe GREEN result

After preparation, the same reformatted copy (with the prepared tests, docs and
canonical fixture copied in) ran
`pytest tests/enforcement -n 8 --dist loadfile --tb=short -q`
→ exit **0**: **305 passed, 669 subtests passed**. This proves the prepared
assertions survive the harmless layout change; it is a diagnostic probe, not a
substitute for P3 formatting or its activation gates.

## Summary

| Instance | RED observed | GREEN |
| --- | --- | --- |
| `P1-A0`, `P1-A11` | baseline `FileNotFoundError` (exit 1) | fixed via current canonical fixture |
| `P1-A1` | line-shift mismatch in focused test | structural module check; second walker rejected |
| `P1-A2` | no-fragment link rejected; 3 guide failures in probe | symbol resolution; 16 link tests pass |
| `P1-A3`…`P1-A9` | exact lookups failed on wrapped/exploded source in probe/focused tests | structural mutations; probes and focused tests pass |
| `P1-A10` | raw hash/line equality failed in probe | AST fingerprints + line-free ledger; both states match |
| `P1-A12` | blanked docstrings made a content edit invisible | docstring text preserved; content mutation rejected; 0 probe mismatches |
| `P1-A13` | inner completion-predicate `or` mutation was unguarded after the parametrized rewrite | restored separate `replace_snippet` case; 10 contract cases pass |
| `P1-A14` | authoritative fixture lived under the active OpenSpec change | moved once to `tests/fixtures/`; 10 focused tests pass |
| `P1-A6` | no failure (formatter-stable) | robustness recorded and conversion validated |

No instance was accepted by weakening uniqueness, API, ownership, dependency or
lifecycle constraints.

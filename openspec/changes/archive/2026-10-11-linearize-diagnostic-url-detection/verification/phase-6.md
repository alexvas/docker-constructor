# Phase 6 — final acceptance

**Status: PASS.** Change `linearize-diagnostic-url-detection`, schema
`spec-driven`. Revised P1 → P1A → P2 → P3 → P4 → P5 → P6 gates are satisfied.
All nine P6 tasks are complete; overall implementation progress is 72/72.
No outstanding blocking findings or accepted behavior exceptions remain.
Sync/archive still requires a separate explicit request.

## Evidence and task closure

| Task | Deliverable / actual evidence |
| --- | --- |
| 6.1 | `phase6_matrix.md` indexes all 12 current requirements and 107 scenarios, with validated executable references and phase deliverables. `phase6_matrix.py` / `.log` verify heading/reference completeness. Missing final artifacts were recorded honestly, not manufactured as behavioral failures. |
| 6.2 | `phase6_checks.md` discovers CI/local type commands and configured unittest discovery. No Python lint/build command is configured; unchanged Dockerfile lint is unrelated. |
| 6.3 | `phase6_experiment.py`, `phase6_comparison.py`, `phase6_performance.md`; before/after JSON, complete experiment/fixture logs, five-repeat medians, 200-call per-mode profiles, separate allocation samples and current deterministic work/state evidence. |
| 6.4 | `phase6_validation.log` runs the exact mandated full eight-worker loadfile command, without exclusions or warning suppression, with successful runner status. |
| 6.5 | `phase6_types.log`, `phase6_configured_types.log`, `phase6_unittest.log`; every applicable configured command actually passes. |
| 6.6 | `phase6_scope.md`, `phase6_scope_audit.py` / `.log`: exact unchanged stress constructions/feed arguments, bytewise protected semantics/configuration, planning unchanged since approved revision, task edits limited to boxes. Separately authorized earlier registry harness correction is disclosed. |
| 6.7 | `phase6_reconciliation.md`, current core/recognizer/all-instance ownership-work/differential audits; all findings resolved without unrestricted suffix equivalence or whole-pipeline linearity claims. |
| 6.8 | `phase6_final_pytest.log`, `phase6_final_types.log`, `phase6_final_state.json`, `phase6_final_validation.log`: repeat full verification and unchanged final source/test/configuration hashes. No P6 production/test fix was needed. |
| 6.9 | This final gate, predecessor review below and all successful current-state evidence. |

## Reviewed predecessor gates

- `phase-1.md`: revised supported-only models, exact scheme set and raw
  coordinates; historical baseline applicable only to unchanged experiment
  inputs, not obsolete UNKNOWN behavior.
- `phase-1a.md`: generic streaming core, genuinely immutable registration and
  payload-free independent cursors; post-gate immutability evidence supersedes
  the initial convention-only implementation.
- `phase-2.md`: supported-only/depth/secret coverage plus all later corrections
  for registration-bounded maintenance, pending transducers and committed
  delayed emissions. Current P2 tests and allocation audit pass.
- `phase-3.md`: persistent coordinator, one raw owner, prompt source-order
  commitment, crossing-prefix matching/mismatch/clean-EOF non-match, bounded
  recovery and cumulative all-instance work. Current audits and full-suite
  verification confirm these guarantees; P3 need not be reopened.
- `phase-4.md`: policy markers, decoded scope and normal/EOF/structured/fetch
  fact gates. Its canonical masked-host fetch finding was resolved in P5,
  not waived as an acceptance exception.
- `phase-5.md`: local/SDK/single-tail isolation, ordinary/fetch grouping and
  user documentation, including the follow-up unsupported-fetch/literal-secret
  regression. Display products, not arbitrary changed text, supply fetch keys.

Historical RED logs and obsolete gates remain intact and are not counted as
current successful verification. Each revised obligation is exercised in the
current unfiltered full suite; non-test ownership/performance/scope evidence
is separately named above.

## Final-state commands and actual statuses

All commands ran from the repository root. Each named log preserves complete
stdout/stderr and its actual runner/process status. No pip/npm dependency was
installed, no tests were excluded, and no warnings were suppressed.

| Command | Log | Result | Exit |
| --- | --- | --- | --- |
| `.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q` | `phase6_validation.log` | 6407 passed, 13 skipped, 11 warnings, 17579 subtests passed | 0 |
| Same mandated command, final revalidation | `phase6_final_pytest.log` | 6407 passed, 13 skipped, 11 warnings, 17579 subtests passed | 0 |
| `./scripts/check-types` | `phase6_types.log`, `phase6_final_types.log` | All checks passed | 0 |
| `ty check docker --python-version 3.14 --output-format concise` | `phase6_configured_types.log` | All checks passed | 0 |
| `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'` | `phase6_unittest.log` | Ran 6308 tests; OK, skipped=13 | 0 |
| One-off before/after experiment, five repeats | `phase6_before_experiment.log`, `phase6_after_experiment.log` | Complete; profiles/medians/allocations saved | 0 each |
| Unchanged historical fourteen-fixture experiment, five repeats | `phase6_before_fixtures.log`, `phase6_after_fixtures.log` | Complete; all scenarios retained | 0 each |
| Current P1A core audit | `phase6_core_audit.log` | Core audit PASS | 0 |
| Current P2 allocation/state/work audit | `phase6_recognizer_audit.log` | Allocation audit PASS | 0 |
| Current P3 all-instance ownership/lifetime audit | `phase6_ownership_work.log` | Ownership/lifetime audit PASS | 0 |
| Current P3 partition corpus | `phase6_differential.log` | 33558 comparisons of text and ordered facts | 0 |
| Matrix/reference validation, scope/input audit, comparison rendering | `phase6_matrix.log`, `phase6_scope_audit.log`, `phase6_comparison.log` | PASS | 0 each |
| Source-state verification, `git diff --check`, strict OpenSpec validation, matrix revalidation | `phase6_final_validation.log` | PASS | 0 each |

The initial `.venv/bin/ty` invocation returned 127 because that path does not
exist. Its full output remains in `phase6_configured_types.log`; the actual
configured PATH command was then run and passed. This is a corrected command
error, not a nonzero typecheck accepted as success. Expected parser errors
inside unittest tests and existing fork DeprecationWarnings remain in logs.
Pytest and unittest have different collection/counting conventions; both
unfiltered commands were executed as configured.

## Performance / bounds interpretation

Fresh pre-change isolated scan medians for 1/2/4/8/16 KiB were approximately
0.0132/0.0529/0.2118/0.8576/3.4308 seconds. Current production recognizer
medians, including construction and finalization, were approximately
0.0081/0.0162/0.0329/0.0656/0.1304 seconds. One-character and 1024-character
continuation measurements and common-projector chunked comparisons are
recorded alongside. The two original unchanged retained-tail bottlenecks
improved from about 7.8 seconds to 2.6 seconds.

Short safe-mode capture and several previously fast fixtures are slower and
have higher transient allocations. These results are explicitly disclosed in
`phase6_performance.md`; no all-input performance improvement is claimed.
The specified acceptance property is removal of quadratic candidate scanning
and bounded replay-free state/work, not an unspecified absolute throughput
threshold. No unrelated optimization was silently added to mask these costs.

All-instance deterministic counters account for speculative/maintenance work,
commits, finalization and repeated overflow recovery. Single raw ownership and
whole-pending 8 KiB bounds pass current audits/tests; line bounds remain
64 KiB. Registration storage depends on configured patterns; fixed-depth
search and cumulative work are output-sensitive. None of this establishes
whole-pipeline linearity, zero allocations, a universal unknown-URL hiding
contract or a security boundary for revealing local exact mode.

## Acceptance decision

The matrix, revised predecessor evidence, final suite/configured checks,
unchanged-stress scope audit and current lifetime/state/partition evidence
agree. No specified behavior was dropped, narrowed or deferred. P6 introduces
no production/test edits and no new dependencies, configuration, persistence,
identity, argv, deadlines, scheduler or shutdown-budget changes. Apply-time
spec edits remain completion boxes only. Final acceptance is **PASS**.

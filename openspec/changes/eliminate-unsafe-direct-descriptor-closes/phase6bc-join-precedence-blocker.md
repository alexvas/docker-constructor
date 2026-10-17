# Phase 6BC GREEN progress and join-precedence decision

Status: **resolved by user choice 1**; the investigation and original logs below
are historical. See the resolution section at the end for current evidence.

## Completed RED, partial GREEN

6BC.4/5 are complete only as RED deliverables, documented separately in
`phase6bc-reporter-red-verification.md`. No GREEN, introspection or validation
task is checked. Phase 6BD/7 and the staging/index state were not changed.

Partial implementation in `docker/process.py` adds typed per-stream terminal
outcomes, immutable failure snapshots, launch-selected callback reporting,
fixed-safe default/nonrecursive fallback, notification accounting and
invocation-local late control-flow handling. Tail copies/updates synchronize
under the state lock, while callbacks/reporters run outside it. Collection
notification precedes delivery; later subscriptions do not call shutdown or
rewrite the cleanup ledger. The public stream/capture result is still the
existing ProcessResult, with callback_errors removed.

`docker/npm_environment/lifecycle.py::run_captured` now delegates launch/capture
to the generic owner and projects the supported failure snapshot. Its returned
handle is conditional on availability. This is partial migration, not accepted
compatibility: old factory seams/tests, complete helper deadline behavior,
legacy returned-pipe observations and obsolete helpers still need work.
Streaming execution/domain reporting, executable guides, API/AST and import
baseline updates remain pending.

## Observed blocking conflict

The existing generic coordinator deliberately uses `ordinary=()` for reader
joins in `_continue_cleanup`. Therefore even OSError from `_join_reader` is an
unexpected cleanup defect and outranks an ordinary active ValueError. This is
unchanged by the partial GREEN implementation.

The earlier checked RED matrix in
`tests/test_process_captured_action_faults.py::test_main_and_captured_helper_faults_share_precedence_and_single_owners`
always injects `OSError("main join")`, but its expected winner is the active
ValueError whenever helper_kind is OSError. Once the helper actually delegates
to generic ownership, those 12 cases reach their winner assertion and expose
the inconsistency: actual authority is main_join, expected authority is primary.
All independent releases, helper delegation and local cleanup reached the
assertion; this is not justification for suppressing or excluding cases.

Decision requested before continuing:

1. Preserve the existing explicit join-defect classification and reconcile the
   erroneous RED winner matrix with that contract (recommended). Include
   explicit regressions for active ordinary primary plus join OSError and
   interrupted primary plus join OSError; do not drop independent-cleanup or
   identity assertions.
2. Explicitly authorize treating join OSError as ordinary, including the
   corresponding contract/baseline review and targeted RED-to-GREEN revision.

Neither option has been applied. This is a workflow pause for a precedence
choice, not a CLI-controlled missing-artifact blocked state.

## Verification and retained logs

Generic command:

```text
.venv/bin/python -m pytest tests/test_process_callback_reporting.py tests/test_process_callback_interruption.py tests/test_process_stream_outcomes.py tests/test_process_failure_delivery.py tests/test_process_lifecycle.py tests/test_process_stop_policy.py tests/test_process_stop_policy_faults.py -n 8 --dist loadfile --tb=short -q
```

`/tmp/docker-constructor-6bc-generic-green.G9Vt4k.log`: exit **0**,
**364 passed**, 2.45s. This is a scoped runtime gate, not full API/docs/adapter
acceptance.

Captured/main-helper command:

```text
.venv/bin/python -m pytest tests/test_npm_environment_phase6bc_captured.py tests/test_process_captured_action_faults.py tests/test_process_failure_snapshot.py -n 8 --dist loadfile --tb=short -q
```

`/tmp/docker-constructor-6bc-captured-green.DgeFX7.log`: exit **1**,
**12 failed, 93 passed**, 1.34s. The 12 failures are the join-authority conflict
above. Do not interpret the remaining passes as completed 6BC.9 acceptance.

Complete suite:

```text
.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q
```

`/tmp/docker-constructor-pytest.6Qyrcj.log`: exit **1**, **47 failed,
7023 passed, 13 skipped, 11 warnings, 17777 subtests passed**, 30.99s.
The complete failure section was extracted and read from
`/tmp/6bc-current-failures.log`. Besides the 12 conflict cases, failures cover
unfinished streaming delegation/removal/preparation, captured compatibility
seams/cleanup and owner-return tests, approved-but-not-yet-reconciled public
API/AST/guide checks and the newly introduced process import baseline.
Warnings are the existing multiprocessing/fork deprecation warnings; none were
suppressed. Full-suite verification is unsuccessful and remains required.

An intermediate typecheck reported missing annotations for new private state;
those annotations were added. Final static checks in
`/tmp/docker-constructor-6bc-pause-static.dN6UbW.log`: typecheck, diff check and
strict OpenSpec validation all exit **0**. Static success does not supersede
runtime failures or complete pending tasks.

## Resolution — preserve join-defect classification

The user explicitly selected option 1. Production classification is unchanged:
reader joins still use `ordinary=()`. The matrix now expects main_join rather
than the ordinary operation primary when the helper supplies an ordinary
OSError. An already active KeyboardInterrupt still wins with exact identity.
No fault case, independent-cleanup assertion or ownership check was removed.

Added four explicit regressions (both reader positions, active ValueError or
KeyboardInterrupt) requiring the chosen authority, both independent join
attempts, exactly one child reap, both sole pipe releases, retention of the
primary/join/close failures, and stable repeated cleanup/diagnostics.

Focused regression command:

```text
.venv/bin/python -m pytest tests/test_process_captured_action_faults.py tests/test_npm_environment_phase6bc_captured.py tests/test_process_failure_snapshot.py tests/test_process_callback_reporting.py tests/test_process_callback_interruption.py tests/test_process_stream_outcomes.py tests/test_process_failure_delivery.py tests/test_process_lifecycle.py tests/test_process_stop_policy.py tests/test_process_stop_policy_faults.py -n 8 --dist loadfile --tb=short -q
```

Complete stdout/stderr `/tmp/docker-constructor-6bc-join-resolution.VaSWnZ.log`,
runner exit **0**, **473 passed**, 2.46s. This includes all previously conflicting
matrix cases and the generic reporter/outcome/failure-snapshot regressions.

Static log `/tmp/docker-constructor-6bc-join-static.uaeZSP.log`: typecheck and
`git diff --check` each exit **0**. Only test expectations, explicit regressions
and this evidence were changed for the resolution; no production code or
specification contract was changed. No commit or staging operation was run.

The prior full-suite failure remains historical and unsuccessful; the complete
suite has not been rerun for this resolution. The scoped passes do not complete
the unfinished adapter, docs, introspection or validation tasks. The precedence
blocker is resolved and Phase 6BC implementation can continue.

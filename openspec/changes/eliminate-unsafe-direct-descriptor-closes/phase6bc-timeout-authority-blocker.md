# Phase 6BC: secondary-note progress and timeout authority blocker

## Scope/status

Repo-local spec-driven change; apply state ready; 122/145 complete, Phase 6BC
5/11. Work only continued on GREEN 6BC.7–9. No new checkbox was checked and no
Phase 6BD/7, specification, index or commit changes were made in this session.
The prior secondary-prefix decision remains resolved by explicit user choice 2.
This is a distinct primary-authority issue, not a request to restore old source
labels.

## Implemented/verified corrections

- Reconciled four legacy secondary-note prefix assertions in
  tests/test_npm_environment_phase3_deadline.py, retaining primary-error,
  failure-presence, masking and detail assertions.
- When the partial npm adapter maps a generic ordinary cleanup error into the
  domain timeout, retain that generic error as a secondary note instead of
  accidentally dropping it because it was the generic authority. The existing
  URL/secret-masking tests now pass, with once-only secondary projection also
  checked by the new release-side paired regression.
- An interruption escaping the coordinator phase Event.wait previously bypassed
  generic collection delivery and lacked process_result. Retain it through the
  same shared ledger before resuming the original remaining coordination
  allowance; no new shutdown/budget, owner or release authority is created.
  Ordinary unexpected wait defects stop the wait rather than being retried.
- Added 48 deterministic cases across capture/stream, both release positions,
  KeyboardInterrupt/SystemExit/GeneratorExit and absent/ordinary/defective/
  interrupted release. Test barriers hold close until after wait interruption
  retention; all workers are released in finally. Exact authority, immutable
  snapshot reference, cleanup completion, independent sole releases and
  once-only secondary retention are checked.

## Blocker: selected cleanup classification is lost at the domain boundary

The generic ledger deliberately classifies reader joins with ordinary=(), so
join OSError is an unexpected defect. Pipe-release OSError is ordinary. The
adapter must preserve that distinction when projecting a timed-out operation:

1. Deadline + ordinary pipe-close OSError: existing AssemblyTimeoutError remains
   primary, with a sanitized secondary cleanup note.
2. Deadline + unexpected join OSError: the exact join exception must retain
   authority under the existing shared precedence contract, not be disguised as
   AssemblyTimeoutError.

Added the paired test
`test_timeout_projection_preserves_unexpected_join_authority_but_not_ordinary_release`
in tests/test_npm_environment_phase6bc_streaming.py. Both cases use the same
OSError type/message and controlled child; release passes and join fails. The
current `snapshot.timed_out` branch wraps the join winner as AssemblyTimeoutError.
The generic owner correctly selected the OSError: this is a domain projection
information gap, not justification to change join ordinary=() or generic
precedence.

The supported result contains timed_out, cleanup.error/failures, stream outcomes
and reader_failure_stream=None, but no classification of the selected error.
An OSError type check cannot distinguish an unexpected join from an ordinary
release. Secondary note normalization does not solve this, and traceback/text
or private-ledger inference is prohibited. A complete solution should also
preserve selected classification while propagating separately owned helper
failures through managed-action observations; classifying the helper's selected
OSError anew as ordinary would lose its original unexpected authority.

Decision needed: authorize and reconcile a minimal supported, bounded
winning-failure classification contract at the generic result/action boundary,
without a per-error source ledger or restoring cleanup-stage prefixes. Agree its
exact fields and delivery/late-observation semantics before implementation. No
such field/API or heuristic was added in this session. Alternatively provide a
reviewed design that preserves both cases using the supported boundary.
Changing primary precedence/types/messages, weakening the paired test or making
join OSError ordinary is not covered by the approved wording-only exception.

A legacy signal race test also expects timeout to beat a later KeyboardInterrupt
and asserts an `also interrupted` note, conflicting with the current shared
interruption-first contract. Its assertions were not silently rewritten; the
complete deadline file remains unsuccessful and requires reconciliation against
the controlling precedence contract, not merely prefix replacement.

## Verification (full logs/statuses retained)

Python commands used .venv/bin/python -m pytest, -n 8 --dist loadfile --tb=short
-q, with complete stdout/stderr redirected and runner status preserved.

- `/tmp/docker-constructor-6bc-deadline.QG5F7f.log`: exit 2, 7 failed/18 passed;
  complete deadline file before the coordinator-wait and secondary-retention
  corrections. Includes an unhandled signal race stopping a worker, missing
  timeout secondary notes, legacy cleanup-stage labels, and unreapable helper
  detail failures. This is unsuccessful verification; no whole-file acceptance
  is claimed after the narrower corrections.
- `/tmp/docker-constructor-6bc-wait-interruption-red.V3X1KF.log`: exit 1,
  48 failed/31 passed. All 48 failures were missing process_result on the exact
  interruption escaping coordination wait. Complete failure log was read.
- `/tmp/docker-constructor-6bc-timeout-priority-red.71GrVw.log`: exit 1,
  1 failed/80 passed. Command selected the new paired timeout test and the full
  test_process_adapter_compatibility.py. All 48 coordination-wait regressions
  now pass; only the timeout + unexpected join case fails, with
  AssemblyTimeoutError instead of the exact join OSError.
- `/tmp/docker-constructor-6bc-secondary-notes-scoped.1NsPb4.log`: exit 0,
  **609 passed**, 5.75s. Explicit scoped command below; this excludes neither
  tests dynamically nor suppresses warnings, but is not complete adapter/
  deadline/full-tree acceptance and does not cover the known failing paired
  join case. That RED result above remains outstanding.
- `/tmp/docker-constructor-6bc-secondary-notes-static.7gpRZQ.log`: exit 0;
  typecheck, git diff --check and strict OpenSpec validation each exit 0.

Scoped command:

```text
.venv/bin/python -m pytest tests/test_process_adapter_compatibility.py tests/test_process_lifecycle.py tests/test_process_stop_policy.py tests/test_process_stop_policy_faults.py tests/test_process_reader_failure_source.py tests/test_process_stream_outcomes.py tests/test_npm_environment_phase3_deadline.py::TestDeadline tests/test_npm_environment_phase3_deadline.py::TestForcedEofFinalization tests/test_npm_environment_phase3_deadline.py::TestUrlFreeFailureNotes tests/test_npm_environment_phase6bc_captured.py tests/test_process_callback_reporting.py tests/test_process_callback_interruption.py tests/test_process_failure_snapshot.py tests/test_process_failure_delivery.py tests/test_process_captured_action_faults.py tests/test_process_compatibility_action_gate.py -n 8 --dist loadfile --tb=short -q
```

Full-tree tests were not rerun after these corrections; earlier full-suite exit-2
results remain unsuccessful historical evidence. The 609 scoped passes do not
complete any atomic adapter, documentation, introspection or integrated gate.

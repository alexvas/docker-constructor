# Phase 6BC strict migration: cross-owner snapshot question

## Authorized planning change

The user explicitly selected strict ActionResult error/metadata pairing and full
migration, not a legacy compatibility mode. Proposal, design, generic/npm delta
specs and pending Phase 6BC tasks now require explicit valid FailureInfo for
returned errors and no metadata without an error. All producers, helper
handoffs, fixtures, examples and API assertions must migrate. Returned-error
kind inference, normalization fallbacks, compatibility aliases/modes and shims
are rejected. Directly raised action-method errors still enter the coordinator's
known operation boundary with its explicit ordinary-error policy; that is not a
fallback for returned data. Existing checked tasks were not changed.

Apply is repo-local, spec-driven, ready; 122/146 complete, Phase 6BC 5/12.
No production implementation change, staging/index operation, commit or Phase
6BD/7 work was performed during this session. The full code migration is NOT
complete. New 6BC.6 remains unchecked and its full RED matrix is still pending.

## Blocking question found before changing core

The planned contracts together require:

1. Helper and parent propagate the exact same authoritative exception object.
2. Each owner delivers its own bounded immutable error.process_result snapshot,
   with its owner-relative FailureInfo and captured output.
3. An already-delivered exception does not receive a replacement snapshot.

A single exception object has only one process_result attribute. When the
parent delivers the same error, it cannot point simultaneously to the earlier
helper snapshot and the distinct parent snapshot.

`tests/test_process_cross_owner_failure_handoff.py` isolates this without a
legacy ActionResult or any kind inference: a helper capture delivers an exact
OSError with its bounded snapshot; a parent receives that same error as active
operation authority, then captures. Both deliveries preserve exception identity,
all four controlled streams receive one release and both owners finish. Parent
capture replaces error.process_result with the parent snapshot. The saved helper
snapshot value remains unchanged, but the earlier caught exception reference
now exposes the parent snapshot. The same unconditional attachment path applies
to a managed action returning the helper's exact error.

This is a passing characterization of existing behavior, not acceptance of the
unresolved FailureInfo contract or a weakened immutability test. No metadata API,
legacy fallback, wrapper or alternative retrieval authority was implemented.

## Decision requested

Recommended: explicitly scope exception-attribute immutability to an owner's
already-delivered invocation/late cleanup. Permit a distinct parent-owner
operation to re-anchor the same exception's process_result on its own delivery,
while retaining the earlier helper snapshot as a separate immutable value before
handoff. This needs no wrapper, error ledger or new lifecycle method. Strict
ActionResult pairing and kind preservation remain mandatory. Same-owner late
cleanup/subscriptions must still never replace an earlier snapshot.

Alternative: require the exception attribute to remain globally fixed after the
first owner delivery. Then parent snapshots need a different supported retrieval
contract or a different exception object; that conflicts with the currently
chosen surface/identity requirements and needs a separately reviewed design.

No option was silently selected. Resume the full strict migration after this
cross-owner delivery rule is confirmed.

## Verification

All stdout/stderr and runner status were preserved:

- `/tmp/docker-constructor-6bc-cross-owner.9cJUSu.log`: exit 0, 111 passed,
  2.38s. Command:

```text
.venv/bin/python -m pytest tests/test_process_cross_owner_failure_handoff.py tests/test_process_failure_snapshot.py tests/test_process_failure_delivery.py tests/test_process_adapter_compatibility.py -n 8 --dist loadfile --tb=short -q
```

- `/tmp/docker-constructor-6bc-strict-static.gGX17i.log`: typecheck,
  git diff --check and strict OpenSpec validation each exit 0.

This scoped result verifies the characterization and existing snapshot/wait
regressions, NOT strict ActionResult implementation or full-suite acceptance.
Earlier failed full-suite and deadline gates remain unsuccessful historical
evidence; no failing test was suppressed or relaxed for the scoped result.

# Phase 6BC.4 clarification: failures in a later outcome-only invocation

## Required contracts read together

The change's design and durable-filesystem-transactions delta require:

1. stream accepts a new explicit on_stream_end invocation after an earlier
   capture/stream has delivered an exception, without rereading/replaying output
   or restarting cleanup.
2. Every such invocation attempts its retained terminal notifications once.
3. Ordinary end-callback failures are isolated in bounded callback_errors;
   control-flow interruptions participate in shared precedence.
4. The earlier delivered exception must keep the same process_result object
   and diagnostics; no replacement snapshot, exception wrapper, extra result
   type, output property or subscription/lifecycle API is allowed.

The artifacts do not explicitly specify the return/raise behavior of the later
outcome-only invocation after a previously delivered failure, or whether its
new interruption authority is invocation-local or shared with the already
immutable previous operation authority. Existing `_collect` always raises the
retained cleanup failure; `_shutdown` does not merge new operation primaries
once `_delivered` is true. That existing behavior cannot simply report new
callback errors through the earlier exception snapshot without replacing or
mutating it.

## Concrete scenario

- First capture raises exact OSError A, with A.process_result.callback_errors
  empty and immutable cleanup/outcomes/output.
- A later stream invocation supplies a new end callback.
- That callback raises ordinary ValueError B.

If the later call rethrows A with its original process_result, B cannot be
reported through that frozen callback_errors field. Replacing A.process_result
violates isolation. Reporting B as a new authoritative exception violates
ordinary callback isolation. A fresh ProcessResult returned from the later
outcome-only invocation can carry its new callback_errors without altering A,
but that selects return-after-prior-failure semantics not explicitly stated.

A second example makes precedence scope significant: the first operation
raised KeyboardInterrupt K1, and the later callback raises KeyboardInterrupt
K2. Must the new invocation raise K2 with its own immutable snapshot, or preserve
K1 as the process's first interruption? In the latter case attaching a fresh
snapshot/secondary diagnostic to K1 would violate its already delivered state.

## Proposed decision

Treat later explicit outcome-only invocations as new serialized operation
invocations, not re-delivery of the original operation failure:

- return a fresh existing ProcessResult containing retained cleanup/outcome
  observations and invocation-local callback_errors;
- do not automatically rethrow an already delivered operation failure merely
  for observing its terminal outcomes;
- a new callback control-flow interruption is authoritative for that new
  invocation and receives its own ProcessResult, leaving the previous error,
  snapshot, diagnostics and cleanup execution unchanged;
- no additional API, result type, readers, cleanup request or byte callbacks.

This is a proposed clarification, not implemented behavior or changed planning
artifacts. If instead repeated original-error delivery is intended, the plan
needs to explain where later callback failures and interruption diagnostics
are observable within the existing immutable API constraints.

## Current status

Tasks 6BC.1/6BC.2/6BC.3 are complete RED deliverables; 6BC.4 remains unchecked.
The later-failure/no-failing-callback test accepts either a fresh existing
ProcessResult or rethrow of the original error, binding notifications/no replay/
immutability rather than silently deciding this missing semantic rule. No
GREEN adapter/API production work was started. Phase 6BD and Phase 7 are untouched.
Await a user decision before completing the remaining RED case and proceeding
to GREEN.

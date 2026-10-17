# Phase 6BC compatibility-plan blocker

**Resolved by explicit user authorization:** revise the 6BB poll-failure
expectations and retain failed observation under shared precedence without
suppressing independent action initiation. The implementation now supplies
`StopContext.returncode=None` after failed polling; no new API or opt-in stage
was added. See `phase6bc-poll-verification.md` for current evidence. The
analysis below is historical, not a current reason to pause Phase 6BC.

## Conflicting required behavior

6BC.3/6BC.4/6BC.7 require independent domain removal despite a local action
failure, preserving terminate-CLI → domain removal → local wait/reap through
the generic coordinator. No consumer-owned main signals/waits, duplicated
coordinator, new post-delivery domain requests or silent compatibility
exceptions are permitted.

`docker/process.py::_run_policy` currently calls `self._proc.poll()` to build
StopContext inside each action's initiation. If that observation raises, the
coordinator records the failure, cancels prepared action capacity, and skips
`ManagedAction.start()`. With persistent polling failure every configured
action is skipped even though local wait/kill/reap and pipe release proceed.
`local_stop_before=True` does not fix this: it performs another poll before
termination and action-context construction still depends on a successful
poll.

The completed predecessor test
`tests/test_process_stop_policy_faults.py::test_persistent_stage_poll_failure_still_reaps_and_releases`
explicitly requires both actions to have only `prepare,cancel`, without
`start`. `test_stage_child_poll_failure_advances_and_preserves_cleanup` also
requires this on a single action-context observation failure. Starting a
required compatibility removal action on that same failure would change a
completed 6BB regression contract. Merely using the current stage plan would
silently drop required domain cleanup in the Phase 6BC fault matrix.

Starting domain removal from action.cancel instead is not an established
solution: cancel also runs for failed pre-acquisition preparation/factory
paths and is the prepared action's resource-release request. It does not
receive StopContext or generic main-acquisition information. No change to that
contract was made. Running removal after generic delivery would violate the
specified pre-delivery action ordering, and adding consumer raw-child access
or a second coordinator would violate ownership constraints.

## Deterministic demonstration

Added `tests/test_process_compatibility_action_gate.py`, using the existing
6BB fake child and nonblocking action fixtures with a persistent OSError from
poll and a retained domain primary. A valid bounded stage selects
local_stop_before. Generic resource cleanup succeeds and retains the exact
primary, but the required independent action is never started.

Command:

```text
.venv/bin/python -m pytest tests/test_process_compatibility_action_gate.py tests/test_process_stop_policy_faults.py -k 'required_compatibility_action or persistent_stage_poll_failure' -n 8 --dist loadfile --tb=short -q
```

Complete stdout/stderr: `/tmp/docker-constructor-6bc-action-conflict.Hpepvm.log`.
Exit status: **1**. Result: **1 failed, 3 passed**, 0.56s. This is focused
conflict evidence, not the full Phase 6BC gate. The three existing baseline
cases pass; the new required-action assertion is RED. Test-controlled owners
finish cleanup and no supported workers survive.

## Decision needed

Before GREEN work, select a supported way for required compatibility actions
to initiate despite a failed main-child observation while preserving default
6BB behavior, first-interruption precedence, bounded phase/total budgets and
pre-acquisition safety. One candidate is an explicitly opt-in
observation-independent action-stage contract with nullable known returncode;
that would need authorization and coherent planning/API/regression updates.
Alternatively authorize revising the existing context-observation-failure
contract. No such policy/API change or narrowing was implemented.

Phase 6BC is paused for this design decision. Only 6BC.2's RED deliverable was
marked complete. Production, Phase 6BD and Phase 7 remain untouched.

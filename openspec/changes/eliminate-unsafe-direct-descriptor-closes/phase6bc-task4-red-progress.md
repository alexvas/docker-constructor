# Phase 6BC.4 partial RED evidence (not task completion)

## Added tests

- Domain stdout clean EOF before stderr read failure: controlled registration/
  read gates require stdout DiagnosticStream.finish(abort=False) promptly,
  stderr finish(abort=True), clean unresolved prefix `to` versus abort-safe
  `<redacted>` for secret `token`, one selected domain tail/chunk per stream,
  and bounded URL/secret-free error projection. All domain behavior assertions
  pass against the historical collector; the new delegation/snapshot assertion
  is RED (`collect.call_count == 0`). Raw snapshots must not be replayed.
- Real streaming incomplete cleanup: a generic reader barrier retains its
  descriptor, independent child/sibling cleanup continues, stdout's assigned
  pending secret prefix is finalized abort-safely once, incomplete reporting
  and sanitized delivered notes remain stable, and late close error is only
  observed in generic cleanup without new sinks/notes or snapshot replacement.
- Local terminate failure plus helper close failure requires named removal
  before main wait, primary identity, bounded sanitized secondary diagnostics,
  once-only removal marker and pipe release. The assembly variant also binds
  existing executor_failure mapping/staging removal/cache/output preservation,
  without changing the outer domain policy or retrying removal.
- Custom DiagnosticStream.finish ordinary failure must remain in generic
  callback_errors, with once-only attempts and EOF retained rather than
  READ_FAILED. A legacy collector guard prevents its known unhandled-finish
  deadlock from stranding a test worker on the RED implementation.
- Captured real-reader incomplete outcome now injects an actual late close
  error and requires retained-owner observation without adding it to an already
  delivered LifecycleOutcome/errors list.
- Generic isolated chunk/end failure plus sole cleanup failure requires both
  callback_errors preserved in the immutable delivered error.process_result,
  distinct from cleanup/read failures and with no callback retry.

Every controlled reader/caller/action worker is released in finally. Unlaunched
helper fixture streams remain caller-owned and are explicitly released once
by a separate fixture finalizer, not an adoption mock or production fallback.
An initial RED run exposed finalizer reliance for those never-launched BytesIO
fixtures; this was fixed, not warning-suppressed.

## Logs (all complete stdout/stderr retained)

- Initial task-4 partial run `/tmp/docker-constructor-6bc-task4-red.XQP8JT.log`,
  exit **1**, 59 failed/2 passed, 0.96s; two unraisable finalizer warnings were
  observed for never-launched helper fixtures (not acceptable acceptance).
- After explicit fixture cleanup `/tmp/docker-constructor-6bc-task4-red.drRaim.log`,
  exit **1**, 59 failed/2 passed, 1.02s; no warning summary or collection errors.
- Current combined 6BC.1/2/3 plus partial-4 RED gate:

```text
.venv/bin/python -m pytest tests/test_process_captured_action_faults.py tests/test_process_stream_outcomes.py tests/test_process_failure_delivery.py tests/test_npm_environment_phase6bc_captured.py tests/test_process_failure_snapshot.py tests/test_npm_environment_phase6bc_streaming.py tests/test_npm_environment_phase6b_streams.py -n 8 --dist loadfile --tb=short -q
```

Full log `/tmp/docker-constructor-6bc-red-current.oGbvBN.log`, exit **1**,
**156 failed, 42 passed, 12 subtests passed**, 2.11s. These remain explicit
unsuccessful RED checks. Failures identify absent typed/snapshot APIs, absent
adapter delegation/ownership/preparation and changed required compatibility
ordering. No warnings, collection errors or controlled-worker cleanup failures
were reported in the current combined gate. None were excluded for acceptance.

## Static checks and progress

`ty check docker --python-version 3.14 --output-format concise` and
`git diff --check` both exit **0**; complete log
`/tmp/docker-constructor-6bc-red-static.4MFsiI.log`. This does not make the RED
suite green. CLI apply state remains **ready**, schema **spec-driven**, progress
**120/144**, with **3/10** Phase 6BC RED deliverables checked (6BC.1/2/3).
The pause is a requested semantic clarification, not a CLI blocked state.
No new production code was edited in this RED session; the earlier explicitly
authorized polling correction remains intact.

## Remaining clarification

Task 6BC.4 is not complete. Its later explicit subscription with a newly failing
end callback needs the return/raise and interruption-authority decision in
`phase6bc-late-subscription-question.md`. Production GREEN cannot silently choose
that contract or add another result/exception/subscription API.

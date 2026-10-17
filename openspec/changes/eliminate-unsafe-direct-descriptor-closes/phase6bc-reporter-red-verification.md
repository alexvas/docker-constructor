# Phase 6BC.4–6BC.5 revised RED deliverables

This is RED evidence, not GREEN acceptance or completed adapter migration.
Historical checked 6BC.1/3 and previous task-4 progress retain their original
scope. The reporter reconciliation in the approved artifacts supersedes their
callback_errors and permissive late-return assertions.

## Test changes

- `test_process_callback_reporting.py`: launch reporter validation before stream
  preparation/factory acquisition; structured exception-object reporting on the
  coordinating thread outside state locks; hostile exception formatting;
  chunk/end isolation on success and close-failure snapshots; explicit raw and
  masked policies; fixed-safe default/fallback; independently failing reporter
  and fallback, including an active wait failure; no recursive diagnostics or
  callback-error result store.
- The same suite binds later explicit subscriptions after prior OSError and
  KeyboardInterrupt: ordinary end failures report once and return fresh results;
  callback/reporter/fallback KeyboardInterrupt, SystemExit and GeneratorExit
  select the first new invocation-local interruption with exact identity and a
  new immutable process_result. Earlier notes, snapshot identity, cleanup
  status/ledger and read/wait/close counters remain unchanged. No shutdown or
  reread is permitted. Existing cleanup is a read-only observation in the new
  snapshot, not rewritten to the new interruption.
- `test_process_callback_interruption.py`: initial chunk/end interruptions from
  each of callback/reporter/fallback, including independent close failure,
  identity, remaining terminal notifications, once-only cleanup and no late
  notification/reporting.
- `test_process_stream_outcomes.py`: supersede stored callback errors with
  reporter observations, require fresh late results rather than permit earlier
  exception redelivery, and extend completion-before-registration/racing and
  serialized-concurrent invocation cases with ordinary end failures. Notification
  and reporting counts remain per invocation, without byte replay.
- `test_npm_environment_phase6bc_streaming.py`: domain finalization errors now
  require bounded secret/URL-safe reporting, no raw traceback/stack context,
  no result error store, once-only finalization and retained EOF outcomes.
- Revise preliminary result-field and real typed-chunk/callback-isolation
  assertions in the existing process contract/lifecycle tests.

Together with the existing snapshot/delivery, captured-client and domain suites,
this completes the previously partial 6BC.4 RED deliverable: independent clean
EOF/abort finalization and unresolved-prefix tails, sanitized incomplete/late
cleanup reporting, immutable delivered snapshots, retained live-reader owners,
removal and staging cleanup despite independent local failures, and no raw
snapshot replay or post-delivery domain notifications. Every controlled worker
is released in finally; no new descriptor acquisition or ownership authority is
introduced by these reporter tests.

## Verification

Initial revised RED run: `/tmp/docker-constructor-6bc-reporter-red.jwWCXn.log`,
exit **1**, **121 failed, 3 passed** (1.13s).

Final combined RED command:

```text
.venv/bin/python -m pytest tests/test_process_callback_reporting.py tests/test_process_callback_interruption.py tests/test_process_stream_outcomes.py tests/test_process_failure_snapshot.py tests/test_process_failure_delivery.py tests/test_process_captured_action_faults.py tests/test_npm_environment_phase6bc_captured.py tests/test_npm_environment_phase6bc_streaming.py tests/test_npm_environment_phase6b_streams.py -n 8 --dist loadfile --tb=short -q
```

Full stdout/stderr: `/tmp/docker-constructor-6bc-reporter-red-final.Lbiq6o.log`.
Runner exit **1**: **210 failed, 42 passed, 12 subtests passed**, 2.23s.
Failure sections were inspected: missing reporter/typed/outcome/snapshot APIs,
missing generic adapter delegation/preparation and required removal ordering.
No collection errors, warning summary or controlled-worker cleanup failures.
This verification is explicitly unsuccessful as a production acceptance check;
no failures were excluded or suppressed.

Static command log `/tmp/docker-constructor-6bc-red-static.mqftuE.log`:
`ty check docker --python-version 3.14 --output-format concise` exit **0**;
`git diff --check` exit **0**. These do not make RED tests green.

Only RED tests and this evidence were changed for these two deliverables;
production GREEN remains pending. Phase 6BD/7 and historical logs are unchanged.

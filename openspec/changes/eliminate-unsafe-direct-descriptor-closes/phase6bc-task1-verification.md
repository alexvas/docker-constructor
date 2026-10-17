# Phase 6BC.1 RED characterization and return/delivery contracts

## Passing current behavior (not migration acceptance)

Executed the existing execution, deadline, lifecycle, streaming and diagnostic
collection suites before adapter production edits:

```text
.venv/bin/python -m pytest tests/test_npm_environment_execution.py tests/test_npm_environment_phase3_deadline.py tests/test_npm_environment_phase4_lifecycle.py tests/test_npm_environment_streaming.py tests/test_npm_diagnostic_collection_phase8.py -n 8 --dist loadfile --tb=short -q
```

Full stdout/stderr `/tmp/docker-constructor-6bc-characterization.QqT7mW.log`,
exit **0**, **269 passed, 411 subtests passed**, 13.39s. This preserves legacy
input signatures, result fields, replacement decoding, bounded diagnostic
tails, callback isolation, Docker identity/absence interpretation, independent
cleanup/error diagnostics, URL/secret redaction and path-specific deadline/
shutdown characterization. In particular the existing deadline suite binds
terminate-CLI/remove/reap, unreapable rm/client bounds and already-absent removal.
Legacy captured missing-stream success is historical only; the explicit RED
missing-stream rejection contracts supersede it. Generic strict missing-stream
rejection has not been altered; normal new factory fixtures provide both pipes.

## Added/completed task 6BC.1 RED coverage

Follow-up clarified the conditional launch/setup observation contract: generic
completed rejection need not attach `.cleanup`, and the adapter returns None
in that case. Actual incomplete setup carries its original handle; no generic
launch API expansion is required. Latest combined RED log and subsequent
coverage are recorded in `phase6bc-task4-red-progress.md`.

- `tests/test_npm_environment_phase6bc_captured.py`: unchanged argv/keyword-only
  policy and LifecycleOutcome fields; exact runtime owner identity on success,
  operation/reader/wait/release failure, retained incomplete setup failure,
  completed setup/factory failures without observation access, no-owner failure
  and real-reader incomplete cleanup. Binary PIPE factory
  options, bounded replacement decoding, one capture, no returned raw pipes,
  one release per fixture pipe and immutable delivered outcome are required.
- `tests/test_npm_environment_phase6bc_streaming.py`: one generic launch and
  stream delegation, required end callback for independent finalization,
  unchanged domain result fields/redacted output and once-only launch hook.
- `tests/test_process_failure_snapshot.py`: generic sole release failure and
  interruption snapshots, bounded output/known returncode, exact authority,
  immutable ProcessResult; precollection factory-error snapshot absence;
  captured adapter output/owner projection and stdout/stderr/both missing
  stream rejection with each present pipe released once.
- `tests/test_process_failure_delivery.py`: collection operation, reader and
  wait ordinary/defective/interrupted failures through capture and stream,
  exact exception identity plus supported bounded ProcessResult. Real-pipe
  incomplete cleanup/late release barriers prove copied output, unchanged
  delivered snapshot/notes and no late callbacks while retaining safe owners.
- `tests/test_process_stream_outcomes.py`: exact enum members and keyword-only
  on_stream_end/capture signature, typed chunk/end identities, immutable ordered
  successful outcomes, retained terminal failure outcomes, ordinary chunk/end
  callback isolation versus read failure, once-only pre-registration/racing
  notification, prompt independent clean EOF and outcome-only later invocation.

Final RED command:

```text
.venv/bin/python -m pytest tests/test_npm_environment_phase6bc_captured.py tests/test_npm_environment_phase6bc_streaming.py tests/test_process_failure_snapshot.py tests/test_process_failure_delivery.py tests/test_process_stream_outcomes.py -n 8 --dist loadfile --tb=short -q
```

Full stdout/stderr `/tmp/docker-constructor-6bc-task1-red.wIuACN.log`, exit **1**,
**57 failed, 2 passed**, 1.02s, no collection errors. All failure nodes correspond
to absent generic snapshot/typed-outcome contracts or absent adapter delegation/
returned owner. This is intentionally unsuccessful RED verification, not GREEN
acceptance. Earlier intermediate logs `/tmp/docker-constructor-6bc-captured-red.6GXYC9.log`
(exit 1, 35 failed/2 passed) and `/tmp/docker-constructor-6bc-task1-red.qaceTq.log`
(exit 1, 48 failed/2 passed) are retained as historical partial coverage.

Task 6BC.1's deliverable is RED tests and passing characterization, not production
implementation. It can now be marked complete for that deliverable only. The
combined helper/main concurrency/fault/publication matrices and domain-specific
late/sanitized/independent finalization coverage of 6BC.3/6BC.4 remain pending;
those are not inferred from this task's narrower evidence. No adapter production
edit, Phase 6BD work or Phase 7 work was performed in this RED step.

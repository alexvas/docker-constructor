# Phase 6BC compatibility corrections and diagnostic-provenance decision

Status: **resolved by explicit user choice 2**. The npm process adapter may use
one bounded sanitized `stream cleanup failed` secondary-note prefix. Primary
errors, shared precedence and reader_failure_stream remain unchanged; no new
generic cleanup-provenance API is authorized for restoring legacy labels.
Proposal, design, npm delta spec and pending 6BC.8/11/12/13 tasks record this
wording-only exception. The investigation/logs below remain historical;
implementation/test reconciliation and Phase 6BC acceptance are still pending.

## Progress and scope

Continued only Phase 6BC after rereading the local spec-driven change and apply
context. CLI remains ready, 122/145 complete, Phase 6BC 5/11. No new checkbox is
checked: 6BC.7/8/9 are still partial, followed by pending docs/introspection/
validation. No specification, index, commit or Phase 6BD/7 edits were made.

Runtime corrections:

- A successful generic cleanup with timed_out=True is translated into the
  existing AssemblyTimeoutError with the retained domain capture.
- EOF returned by an already-cancelled blocking compatibility reader becomes
  ABORTED, preventing forced EOF from flushing an unresolved secret/URL prefix.
- Genuine descriptor-less streams whose fileno rejects access may use their
  sole owner to unblock read. Explicit prepared descriptor-less tracking is
  separate from unknown fd state; real/unknown descriptor readers retain their
  stop-before-release barrier. An intermediate _fds-is-None shortcut failed
  existing live-reader gates and was replaced, not accepted.
- Legacy descriptor-less text factories normalize to bytes at the generic read
  boundary; real Popen requests remain binary and no stream proxy/second owner
  is introduced.
- Successful bounded wait return codes remain observable even for injected
  factories without a returncode attribute.
- A prepared setup-completion barrier prevents a failed first reader from
  requesting cleanup/releasing its sibling before launch setup has settled.
  Reads remain possible during on_launched; setup failure opens the barrier
  before shared cleanup. Both successful and failed launch paths release it.

Added tests/test_process_adapter_compatibility.py (31 cases) for both release
positions, operation+ordinary/defect/interrupted close, sole close, repeated
cleanup, independent release, wait-only factories, text normalization, the
first-reader/setup race, and the diagnostic-observation characterization below.

## Remaining contract conflict

The existing domain adapter distinguishes diagnostic notes such as
`supervisor cleanup failed` and `pipe close failed`. The partial new projection
uses `stream cleanup failed`. The plan requires preservation of public domain
diagnostics, so silently weakening the old assertions or accepting new wording
is not authorized by scoped runtime passes.

The first remaining compatibility failure is:

```text
TestSupervisorFailure.test_unexpected_wait_failure_is_primary_and_cleans_up
expected note containing: supervisor cleanup failed
actual: stream cleanup failed: docker rm -f exited 1: ...
```

ActionResult can identify some managed-helper errors, but the supported public
snapshot does not generally distinguish local wait/reap failure from pipe
release failure. reader_failure_stream solves authoritative reader projection,
not provenance for arbitrary cleanup diagnostics.

The passing characterization
`test_wait_and_release_errors_do_not_expose_distinct_diagnostic_provenance`
constructs an ordinary wait failure and an ordinary release failure with the
same error type/message. Their supported observations are identical: bytes,
returncode, timed_out, stream outcomes, reader_failure_stream=None, complete
cleanup, failure type/args and empty actions. Each case verifies exact generic
exception identity and both sole releases. This is evidence of missing
projection information, not acceptance of changed diagnostic labels.

Decision needed before further migration:

1. Preserve the strict existing diagnostic distinctions and authorize a bounded
   supported generic cleanup-provenance contract, with planning/API/fault tests
   agreed before implementing it. No public raw-process authority or mutable
   ledger should be exposed.
2. Explicitly permit one generic sanitized secondary-note prefix in the npm
   adapter, while preserving authoritative error selection, existing domain
   result fields, error messages, output/tails, redaction and cleanup behavior;
   reconcile planning/tests for that narrowly authorized wording change.

No new provenance API, traceback/text heuristic, name-based source inference,
misleading dual-prefix notes or relaxed legacy diagnostic assertion was added.
The earlier join and reader_failure_stream decisions remain intact.

## Logs and verification

All Python commands used .venv Python, eight workers and loadfile distribution,
with complete stdout/stderr redirected to the named log and runner status
preserved. Intermediate diagnostic runs are unsuccessful, not acceptance:

- `/tmp/docker-constructor-6bc-compat-red.kz3Aj0.log`: exit 1,
  24 failed/4 passed (descriptor-less unblocking RED).
- `/tmp/docker-constructor-6bc-compat-green.olC2Rm.log`: exit 1,
  1 failed/43 passed (forced-EOF abort-finalization RED).
- `/tmp/docker-constructor-6bc-setup-race-red.lbj4Xh.log`: exit 1,
  1 failed/29 passed (deterministic launch/setup race RED).
- `/tmp/docker-constructor-6bc-compat-green.xJvTy1.log`: exit 1,
  11 failed/346 passed (unknown-fd safety regression, subsequently corrected).
- `/tmp/docker-constructor-6bc-compat-green.NYUcci.log`: exit 2,
  1 failed/356 passed; explicit -x diagnostic run stopped at the secondary-note
  compatibility failure above. No remaining test is claimed covered by it.

Final scoped runtime command:

```text
.venv/bin/python -m pytest tests/test_process_adapter_compatibility.py tests/test_process_lifecycle.py tests/test_process_stop_policy.py tests/test_process_stop_policy_faults.py tests/test_process_reader_failure_source.py tests/test_process_stream_outcomes.py tests/test_npm_environment_phase3_deadline.py::TestDeadline tests/test_npm_environment_phase3_deadline.py::TestForcedEofFinalization tests/test_npm_environment_phase6bc_streaming.py tests/test_npm_environment_phase6bc_captured.py tests/test_process_callback_reporting.py tests/test_process_callback_interruption.py tests/test_process_failure_snapshot.py tests/test_process_failure_delivery.py tests/test_process_captured_action_faults.py tests/test_process_compatibility_action_gate.py -n 8 --dist loadfile --tb=short -q
```

`/tmp/docker-constructor-6bc-compat-final.mrjznu.log`: exit 0,
**566 passed**, 4.46s. This is an explicitly scoped regression gate, not the
whole deadline suite or complete-tree acceptance. The known failing diagnostic
case was neither changed nor suppressed to obtain this scoped result.

`/tmp/docker-constructor-6bc-compat-static.rFTQhX.log`: typecheck,
`git diff --check` and strict OpenSpec validation each exit 0.

The complete suite has not been rerun after these corrections; its previous
exit-2 failure in phase6bc-reader-source-verification.md remains unsuccessful
historical evidence. Full acceptance, old fixture/returned-pipe migration,
obsolete-helper removal, complete managed-action lifecycle coverage,
API/AST/import proofs and executable documentation remain pending.

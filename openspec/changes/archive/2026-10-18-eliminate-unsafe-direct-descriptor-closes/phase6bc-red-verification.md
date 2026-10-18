# Phase 6BC revised RED evidence

This records partial RED work only. Production adapters and `docker.process`
remain unchanged. It is not Phase 6BC acceptance and does not permit skipping
remaining RED obligations or claiming the migration gate passed.

The earlier `phase6bc-investigation.md` remains historical. Its missing
per-stream public observation blocker is addressed by the current design and
tasks authorizing ProcessStream, StreamOutcome and keyword-only on_stream_end;
it is not a current blocker.

## Deliverables added

- `tests/test_process_stream_outcomes.py`: exact enum members and signatures;
  successful immutable ordered results; completion before registration and
  controlled registration/completion overlap; chunk-before-end order; prompt
  independent stdout EOF before a later stderr read failure; ordinary chunk/end
  callback isolation; explicit subsequent outcome-only subscription without
  rereading, byte replay or a fresh cleanup budget.
- `tests/test_process_failure_delivery.py`: operation OSError, ValueError and
  KeyboardInterrupt identity on capture/stream; bounded output and known
  returncode on error.process_result; descriptor-backed live-reader retention,
  controlled incomplete delivery, eventual safe close, late close failure and
  immutable delivered output/cleanup/notes/callback observations. Real children
  are acquired only through OwnedProcess.launch. Test-controlled readers are
  released in finally and cleanup is observed before each test finishes.
- `tests/test_npm_environment_phase6b_streams.py`: removed all five self-cleaning
  rejected-adoption mocks. Replacement faults occur at generic first/second
  transfer without closing or mutating candidates. Added first/second actual
  owner lock-construction failures before main launch for both adapters,
  launch-configured managed-action preparation failure before streaming main
  launch, and first ordinary transfer followed by second interruption for both
  adapters. Assertions require exact authority, both transfer attempts, one
  release per acquired pipe and reaping. Normal fixture processes now provide
  read-only poll observation as well as both requested streams.

The existing `tests/test_process_failure_snapshot.py` was present at session
start and has been retained unchanged.

## Runs and complete logs

Commands use `.venv/bin/python -m pytest <files> -n 8 --dist loadfile --tb=short -q`
with complete stdout/stderr redirected before inspecting the runner status.

| Files | Full log | Exit status | Result |
| --- | --- | --- | --- |
| failure_snapshot, stream_outcomes, npm_environment_phase6b_streams | `/tmp/docker-constructor-6bc-red.s1QNk2.log` | 1 | 31 failed, 40 passed, 12 subtests passed; initial RED |
| preceding files plus failure_delivery | `/tmp/docker-constructor-6bc-red.sI2BvU.log` | 1 | 31 failed, 40 passed, 1 collection error, 12 subtests passed; unsuccessful run; corrected package-qualified test-support import |
| preceding four files, after import and factory-spy corrections | `/tmp/docker-constructor-6bc-red.1pVJbo.log` | 1 | 39 failed, 40 passed, 12 subtests passed; no collection errors; 2.04s |

The complete failure section of the final log was inspected. Failures are
missing process_result, absent typed enums, the legacy raw-Popen captured
return/missing-stream success, absent generic transfer attempts and main launch
preceding generic preparation. No fixture timeout or leaked controlled-worker
failure was reported. The 39 failures include six failing subtest cases.

## Task tracking

6BC.2's RED deliverable is complete: self-cleaning mocks are removed, genuine
pre-transfer construction/transfer/prepared-policy faults are present, and the
adapter violations have been demonstrated RED before production edits. This
checkbox does not mean adapters have migrated or those contracts pass.

6BC.1, 6BC.3 and 6BC.4 remain incomplete. The additions do not yet cover every
requested adapter characterization, combined main/helper fault matrix,
publication gap, concurrent invocation, sanitization, abort-safe domain-tail
comparison and negative ownership/API proof. No GREEN, INTROSPECT or VALIDATE
Phase 6BC task is complete. Phase 6BD and Phase 7 were not changed.

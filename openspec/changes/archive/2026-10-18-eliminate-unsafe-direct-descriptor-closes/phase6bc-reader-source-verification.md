# Phase 6BC authorized reader_failure_stream extension

The user authorized adding `reader_failure_stream: ProcessStream | None = None`
to the existing immutable ProcessResult. Proposal, design, generic/domain delta
specs and pending 6BC.8/12/13 requirements now explicitly describe the extension.
No historical checkbox was rewritten and no additional task was checked.
Overall progress remains 122/145, Phase 6BC 5/11. Phase 6BD/7 and the index were
not changed.

## Implementation

- OwnedProcess retains bounded provenance for the two read operations and
  selects it under the delivery lock against the exact authoritative exception.
  The original generic exception is not wrapped and preserves type/identity.
- A read-chunk error records its stream; unrelated errors from polling or other
  reader machinery do not obtain read provenance merely from READ_FAILED.
- Non-reader cleanup/action reuse of the same exception removes its read-source
  claim. Concurrent reader failures use the actual selected exception and first
  registration, not stdout-first inference. Active operation authority remains
  distinct from reader provenance.
- Success, normal subsequent outcome-only invocations and new invocation-local
  interruptions have None even if retained cleanup still observes a reader
  failure. Previously delivered snapshots retain their original field.
- The streaming adapter uses the supported field for its existing sanitized
  StreamReaderFailure projection. A winning join defect remains non-reader;
  control-flow exceptions are handled before ordinary domain projection.
- The process guide documents these field semantics. The existing API field-set
  assertion includes the authorized field/default; broader pending signature,
  AST and documentation reconciliation is not represented as complete.

## Focused verification

Added `tests/test_process_reader_failure_source.py` (19 cases), covering capture
and stream, each reader, OSError/RuntimeError/KeyboardInterrupt exact identity,
immutable snapshots, later success/new interruption, join competition in either
position, identical-object read/join reuse, both competing reader winners and
successful default None. The previously RED domain paired regression now
passes and proves that reader projection does not disguise join OSError.

An initial focused attempt (`/tmp/docker-constructor-6bc-reader-source.BUK4Za.log`,
exit 1, 1 failed/184 passed) exposed a launch/setup race when a fixture allowed
its first reader to fail before sibling setup. The new post-collection provenance
fixtures now release their read-failure gates from on_launched, making the
intended collection boundary deterministic. This is not a claim that the
separate generic setup race is fixed; existing setup/fault suites and complete
compatibility verification remain pending.

Final focused command:

```text
.venv/bin/python -m pytest tests/test_process_reader_failure_source.py tests/test_npm_environment_phase6bc_streaming.py tests/test_npm_environment_phase6bc_captured.py tests/test_process_callback_reporting.py tests/test_process_callback_interruption.py tests/test_process_failure_snapshot.py tests/test_process_failure_delivery.py tests/test_process_stream_outcomes.py tests/test_process_captured_action_faults.py tests/test_process_compatibility_action_gate.py tests/test_process_stop_policy.py tests/test_process_stop_policy_faults.py -n 8 --dist loadfile --tb=short -q
```

Complete log `/tmp/docker-constructor-6bc-reader-source.tB0ZoP.log`, runner exit
**0**, **411 passed**, 2.57s. Controlled owners/readers are released in finalizers;
no expected-failure exclusion or suppression was introduced.

## Static and full-suite results

`/tmp/docker-constructor-6bc-reader-source-static.uFCiCT.log`: typecheck,
`git diff --check` and strict OpenSpec validation each exit **0**.

Full required command:

```text
.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q
```

Complete stdout/stderr `/tmp/docker-constructor-pytest.oX75gi.log`, runner exit
**2**, **90 failed, 7002 passed, 13 skipped, 11 warnings, 17769 subtests passed**,
63.74s. xdist stopped after a worker keyboard-interrupt in the legacy
interruption fixture. The failure section was extracted to
`/tmp/6bc-reader-source-full-failures.log`.

Full verification is unsuccessful. It still includes incomplete adapter
compatibility (legacy ownership/reader/supervisor seams, timeout ordering,
diagnostic wording and forced-finalization behavior), returned-raw-pipe test
expectations, controlled-worker retention in failing old fixtures, API/AST and
old executable documentation checks and import baselines. The provenance
extension and scoped passes do not complete those obligations or the Phase 6BC
migration. No failure or warning was silently excluded, and no old full-suite
result is reused as new acceptance.

# Phase 6BC initial RED and per-stream finalization blocker

This is partial implementation evidence, not Phase 6BC acceptance. No task is checked off by this evidence. Production adapters and the generic process implementation remain unchanged.

## Initial RED

Added `tests/test_process_failure_snapshot.py` for exact release-error identity on capture/stream (ordinary failure, defect and interruption), immutable bounded existing ProcessResult snapshots, no replay/release retry, pre-collection factory failure, captured owner/output projection and controlled missing-stream rejection.

Command: `.venv/bin/python -m pytest tests/test_process_failure_snapshot.py -n 8 --dist loadfile --tb=short -q`

Full stdout/stderr: `/tmp/6bc-snapshot-red.g1iYpe.log`; runner exit status **1**, **10 failed, 1 passed**. These are expected RED failures, not successful verification. This does not satisfy all of 6BC.1–6BC.4.

## Blocking public streaming seam

The existing `collect_streams` finalizes each DiagnosticStream independently on its reader's EOF, with abort determined at that boundary. `OwnedProcess.stream(callback)` supplies only `(name, bytes)` data chunks; it supplies neither per-stream EOF nor per-stream clean/aborted completion observation. ProcessResult/error.process_result bounded tails are not callback transcripts and must not be replayed. An adapter cannot determine from these public observations whether a stream reached clean EOF before a sibling failed. Running collect_streams over new blocking queues would duplicate reader coordination and introduce unowned external-reader lifecycle work, rather than migrate it to generic ownership.

Deterministic investigation: `/tmp/6bc-stream-eof-investigation.py`, full stdout/stderr `/tmp/6bc-stream-eof-investigation.Sg9TJC.log`, exit status **0**. stdout supplies `b"to"` and clean EOF with configured secret `"token"`; stderr fails only after stdout's DiagnosticStream has finalized. Existing collector records `[("stdout", False), ("stderr", True)]` and retains stdout `"to"`. Finalizing both streams as aborted at aggregate failure instead retains `"<redacted>"`. Thus conservative aggregate abort changes required output semantics; treating both as clean could expose an unresolved prefix on a genuinely aborted stream. Waiting for aggregate settlement also cannot preserve prompt per-stream finalized diagnostics while a sibling remains active.

Implementation is paused for authorization of a narrow generic per-stream completion contract (or another supported public seam preserving that distinction). No public API extension beyond the authorized failure snapshot has been implemented, and no private reader/output state is used by an adapter. Phase 6BD, Phase 7 and Docker wrapper implementation remain untouched. No staging or commit was performed.

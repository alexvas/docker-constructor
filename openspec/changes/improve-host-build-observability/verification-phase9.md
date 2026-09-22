# Phase 9 verification — facade presentation

Scope: tasks 9.1–9.16 of `improve-host-build-observability`
(interactive/line/off renderers, formatting, hostname policy, diagnostic
coalescing in one facade worker, the two-lane bounded mailbox, shared
contextual host-failure rendering). Recorded separately from the specification
files, per the project convention.

## Deliverables

New module `docker/versioning/host_presentation.py`:

- `HostPresentationMode` (`interactive`/`lines`/`off`) and
  `select_presentation()` → `PresentationPlan` (mode + live-sink
  authorization). JSON never authorizes a live sink; noninteractive text needs
  explicit `lines`; interactive text authorizes all three modes.
- `PresentationMailbox`: independently bounded reliable-control and
  best-effort-telemetry lanes, monotonic sequence assignment and cross-lane
  merge, derived `control_reservation()`, replaceable heartbeat/progress
  supersession, saturating omission accounting, and a capacity-independent
  idempotent emergency stop with a distinct worker-stopped acknowledgement.
- `HostPresentationState`: deterministic per-session state for interactive
  mutable slot, `lines` fixed one-second exact-repeat windows, conservative
  interactive numeric-variant replacement, hostname-display policy,
  first-status/30-second/silence-transition rendering, and the ordered
  `finalize → clear → durable → clear slot → conditional restore` sequence.
- `PresentationWorker`: the single worker thread; drives state from injected
  clock/mailbox timeouts, finalizes before barrier acknowledgement, keeps
  servicing admitted control after renderer failure, and marks worker-stopped
  on every exit.
- `HostPresentationSession`: facade-owned mailbox+worker plus a
  `GuardedHostEventSink` adapter; `shutdown()` admits the shutdown barrier
  under the guard and only then waits/joins outside it (idempotent, never
  masks the primary result).
- `TerminalHostRenderer` and formatting (`format_elapsed`,
  `format_heartbeat`, `format_progress`, `format_failure_report`,
  `format_diagnostic_text`).

Structured failure context: `HostFailureContext` in `host_progress.py`,
`BuildResult.host_failure`, `_host_failure_context()` in
`build_orchestration.py`, and facade rendering of the contextual report.

Facade wiring: `constructor_cli._HostEventRenderer` now extends
`TerminalHostRenderer`; `_make_presentation_session` selects the plan and
creates the session; the build path installs `session.sink` and shuts down in
`finally`; `BuildRequest.host_presentation_complete` runs the session shutdown
before native Docker output.

## RED evidence

`tests/test_host_presentation_phase9.py` was written before the module
existed. Running it against the baseline failed with:

```
ModuleNotFoundError: No module named 'docker.versioning.host_presentation'
Ran 1 test in 0.000s
FAILED (errors=1)
```

## Green evidence

- `python -m unittest tests.test_host_presentation_phase9` →
  **46 tests OK** (0.12s).
- Focused host/presentation run (12 modules: phase 9, phase 7 grouping and
  identity, operational events, activity monitor, host download, output
  configuration, constructor host progress, build output, build output
  acceptance, build orchestration, locked-assembly phase 8) →
  **Ran 333 tests, OK**.
- `scripts/check-types` → `All checks passed!`.
- Full suite → **Ran 3966 tests in 48.153s, OK (skipped=13)**.
- `openspec validate improve-host-build-observability --type change` →
  `Change 'improve-host-build-observability' is valid`.

## INTROSPECT (9.15) findings

Reviewed the phase diff against each listed defect class; no outstanding
defect after the following confirmations:

- Classification never bypasses coalescing (covering warnings/errors/retries/
  timeouts); a single-occurrence group emits no summary; a different
  diagnostic flushes the pending summary first.
- Fingerprints/session keys stay on `DiagnosticIdentity` only; they are never
  rendered, retained, persisted, or reused. `lines` mode never applies numeric
  grouping; interactive numeric replacement resets the exact-repeat count and
  the canonical suffix is restored only for exact repeats.
- `N` counts admitted occurrences only; omission notices are separate,
  non-coalesced, ordered before the next admitted diagnostic/terminal, and
  saturate to a lower bound. No producer-side identity map exists.
- Lanes share no capacity; the control reservation is derived from the bounded
  protocol transition set and cannot be consumed by diagnostics. Reliable
  admission is non-blocking; emergency signalling is capacity-independent,
  idempotent, wake-and-discard, and acknowledged by worker-stopped.
- Shutdown/emergency waits and joins happen only outside guarded
  serialization; an unadmitted barrier is not awaited. Exactly one presentation
  worker exists per live session and is joined before return.
- Renderer failure discards pending presentation, disables rendering, and the
  worker keeps draining admitted control. No render occurs after shutdown or
  worker-stopped acknowledgement.
- Output policy remains facade-only; host facts are normalized upstream and
  applied only by the facade renderer.

One documented limitation: `host_presentation_complete` is invoked at the
Docker-transition boundary in `build_orchestration`, so the session join occurs
before native Docker output on the normal path; early failure paths rely on the
facade `finally` shutdown (idempotent).

## VALIDATE (9.16) results

Recorded above (focused 333, full 3966, typecheck, openspec). All 46 new
presentation tests assert no `host-presentation` thread survives. No
presentation test claims a live `N` includes dropped diagnostics.

## POST-VALIDATE HARDENING — reliable admission, notice delivery, worker termination, structural attribution

Four correctness fixes on top of the Phase 9 deliverables, with regression
tests in `tests/test_host_presentation_phase9.py` (61 tests total).

### 1. Reliable admission failure (`PresentationMailbox`)
- `admit_control()` no longer silently returns `False` when the mailbox lock is
  busy. Every reliable-admission failure (lock contention, exhausted control
  capacity, already-signalled emergency) routes through the single
  `_fail_reliable_admission()` path, which atomically sets `_admission_failed`
  and `_emergency` and wakes the worker.
- The worker now parks on a lock-independent `_wake` event instead of a
  `Condition` tied to the mailbox lock, so an emergency set while the lock was
  held is still observed promptly. The queue is re-checked on every wakeup, so
  clearing the event cannot lose an admitted event.
- Tests: `TestReliableAdmissionFailure` (3).

### 2. Omission-notice delivery (`PresentationWorker`)
- A pending omission notice is no longer consumed by every dequeued event.
  `_consumes_omission_notice()` consumes it only for the next admitted
  structured/legacy diagnostic or a terminal step/phase state; not for
  heartbeats, progress, or started events.
- `_terminalize()` finalizes the current group, renders the notice as its own
  durable line, then renders the terminal state.
- Tests: `TestOmissionNoticeOrdering` (2).

### 3. Guaranteed worker termination (`HostPresentationSession.shutdown`)
- If the admitted shutdown barrier is not acknowledged within
  `WORKER_JOIN_SECONDS`, the capacity-independent emergency stop is signalled
  and `worker_stopped` is awaited. A failed admission skips barrier
  acknowledgement and goes straight to the emergency path.
- `_ensure_worker_stopped()` moves all waiting and the `join()` outside
  `_guard_lock` and never returns while the worker is alive.
- Tests: `TestShutdownTermination` (4).

### 4. Structural failure attribution
- Removed the broad exception-class mappings in `_host_failure_context()`.
- Added `attach_host_failure()` / `lookup_host_failure()` and
  `HostFailureContext` carriers in `docker/versioning/host_progress.py`. The
  context is attached on the exception at the boundary where the phase/step is
  known (`HostAssemblyActivity.step`, `materialize_build_artifacts`,
  `materialize_pi` acquisition and derived validation) and retrieved through the
  cause/context chain, so it survives wrapping. Carrier assignment uses
  `object.__setattr__` so `LockedNpmError`'s frozen value fields stay frozen; a
  weak registry is the fallback for exceptions without an instance `__dict__`.
- `_host_failure_context()` now builds the report from the preserved phase,
  step, safe logical resource, normalized hosts, bounded sanitized tail, and
  exception-type chain, with no class or message-wording inspection.
- Tests: `TestStructuralFailureAttribution` (6) and
  `TestFailureBoundaryAttachment` (3, exercising the real `HostAssemblyActivity`
  boundaries).

### RED evidence
- Reverting the three `host_presentation.py` fixes produced **7 failures** in
  `TestReliableAdmissionFailure` / `TestOmissionNoticeOrdering` /
  `TestShutdownTermination`.
- Reverting `_host_failure_context()` to the broad mappings produced **10
  failures** in `TestStructuralFailureAttribution`.

### Green evidence
- `tests.test_host_presentation_phase9` → **61 tests OK**, run 20× with no
  flakiness.
- Focused host/build/session run (11 modules) → **332 tests OK**, 3×.
- Full suite → **3981 tests OK (skipped=13)**, 5 clean runs.
- `scripts/check-types` → `All checks passed!`.

## POST-VALIDATE HARDENING 2 — telemetry drop accounting (`PresentationMailbox`)

- Added `_is_diagnostic()` identifying `HostStructuredDiagnostic` and
  `HostDiagnosticEvent`; `_consumes_omission_notice()` now reuses it.
- `admit_telemetry()` only increments the diagnostic omission counter for a
  dropped diagnostic. Capacity drops of replaceable heartbeats or
  transport-progress updates still increment `dropped` but no longer produce a
  false "diagnostics omitted" notice.
- The lock-contention path (`_lock.acquire(blocking=False)` failing) still
  returns immediately without waiting for the mailbox lock, but now records a
  dropped diagnostic through `_record_omission()`.
- The omission counter is guarded by a dedicated `_omission_lock`, never the
  mailbox lock, so contention-path accounting is thread-safe and prompt while
  keeping the fixed-width saturating limit at `OMISSION_COUNTER_LIMIT` (9999).
- `consume_omission_notice()` reads and resets the counter under
  `_omission_lock`, so combined contention and capacity drops render as one
  notice; the count is only cleared when the notice is produced.

### RED evidence
Reverting the two counting behaviours produced **4 failures** in
`TestOmissionDropAccounting`.

### Green evidence
- `tests.test_host_presentation_phase9` → **67 tests OK**, run 15× with no
  flakiness.
- Full suite → **3987 tests OK (skipped=13)**, 3 clean runs.
- `scripts/check-types` → `All checks passed!`.

### New focused tests (`TestOmissionDropAccounting`, 6)
- Contended structured diagnostic → exactly one `[1 diagnostics omitted]`
  notice, then `None`.
- Capacity structured diagnostic → increments the same counter.
- Contention drop renders before the next admitted diagnostic (worker-level).
- Capacity heartbeat + progress drops → `dropped` increments, no notice.
- Contended heartbeat + progress drops → no notice.
- Combined capacity + contention drops saturate at "at least 9999", the next
  batch is a fresh non-coalesced notice, and contention-only drops also
  saturate the shared counter.

## POST-VALIDATE HARDENING 3 — non-blocking, order-preserving omission accounting

- `_record_omission()` no longer takes `_omission_lock` with a blocking `with`.
  It only *tries* the lock non-blockingly; a producer reporting a dropped
  diagnostic never waits on any lock, so `admit_telemetry()` stays prompt even
  when both the mailbox lock and the omission lock are held.
- If the exact counter cannot be taken immediately, the drop is recorded on a
  lock-independent lower-bound counter (`_omission_lower`) and the batch is
  marked inexact (`_omission_inexact`), both updated with plain atomic
  assignments. `take_omission_notice()` combines the exact and lower-bound
  counts, renders an exact notice only when the count is known, and otherwise
  renders `[at least N diagnostics omitted]`. The lower bound saturates at
  `OMISSION_COUNTER_LIMIT`.
- Each drop records an ordering boundary: the mailbox sequence at which the
  first diagnostic omission occurred (`_omission_boundary`, first-wins). The
  worker now consumes via `take_omission_notice(item.sequence)`, which returns
  `None` and keeps the batch pending while the event's sequence is below the
  boundary. A queued diagnostic admitted *before* the drop no longer surfaces
  the notice; the first diagnostic or terminal event admitted *after* the drop
  renders it immediately beforehand. `consume_omission_notice()` remains as a
  force-consume wrapper for direct callers/tests.
- Additional drops in the same batch accumulate and saturate; the batch is only
  cleared when the notice is actually produced.

### RED evidence
Reverting the lower-bound fallback (drops lost) and the boundary check (notice
surfaced too early) produced **6 failures** in `TestOmissionDropAccounting`.

### Green evidence
- `tests.test_host_presentation_phase9` → **73 tests OK**, run 12× with no
  flakiness.
- Full suite → **3993 tests OK (skipped=13)**, 3 clean runs.
- `scripts/check-types` → `All checks passed!`.

### New focused tests (6, in `TestOmissionDropAccounting`)
- Queued diagnostic A, contended dropped B, admitted C → rendered order
  `A, [1 diagnostics omitted], C`.
- A queued before the boundary does not consume; only the event at/after the
  boundary does.
- Producer admission is prompt with the omission lock held, and the drop
  surfaces as `[at least 1 diagnostics omitted]`.
- Both the mailbox lock and the omission lock held → still prompt, drop
  preserved as a lower bound.
- Multiple contended drops after one boundary → one `[at least 3 ...]` notice,
  then `None`.
- Contended-only drops saturate at the fixed-width `OMISSION_COUNTER_LIMIT`.

## POST-VALIDATE HARDENING 4 — genuinely thread-safe omission accounting

Supersedes the lock-free fallback writes of hardening 3. The plain-integer
`_omission_lower`, `_omission_inexact`, and lock-free `_omission_boundary`
writes could lose increments, reset a newly recorded omission, or corrupt the
boundary when a producer raced `take_omission_notice()`.

- `_record_omission()` now only mutates `_omissions`/`_omission_boundary` while
  holding `_omission_lock`, tried non-blockingly. On contention it does not
  write any protected field directly: it appends a conservative ordering marker
  to a thread-safe FIFO (`deque.append`, atomic) and then sets a lock-independent
  `threading.Event`. Publishing the marker before the event guarantees a
  consumer that observes the signal also observes the marker.
- It deliberately does **not** count contended drops exactly; the batch is
  reported as a conservative lower bound instead.
- `_merge_fallback_locked()` (called under `_omission_lock` by
  `take_omission_notice()`) always drains every marker — even when the event is
  not set — so a marker appended concurrently with a previous reset is never
  lost. It lowers `_omission_boundary` to the minimum marker, records the batch
  as fallback in the protected `_omission_batch_fallback` flag, and only then
  clears the event. Events admitted before the omission therefore cannot
  consume its notice.
- The batch fallback flag persists across a boundary-deferred consume, so the
  conservative lower bound survives until the notice is actually produced.
  Exact count, boundary, and fallback flag are reset together only on produce.
- Result: while the mailbox or omission lock is contended, producer admission
  returns immediately; the consumer reports an exact notice for exact drops and
  `[at least N diagnostics omitted]` for contended drops, never losing a batch
  or rendering it before its causal boundary.

### RED evidence
Mutating `_merge_fallback_locked()` to discard the lock-free fallback state
without transferring it produced **7 failures** in `TestOmissionDropAccounting`.

### Green evidence
- `tests.test_host_presentation_phase9` → **77 tests OK**, run 15× with no
  flakiness.
- Full suite → **3997 tests OK (skipped=13)**, 3 clean runs.
- `scripts/check-types` → `All checks passed!`.

### New tests (4, in `TestOmissionDropAccounting`)
- Fallback boundary protects an already-queued diagnostic (sequence 0 blocked,
  sequence 1 consumes).
- Every fallback round (50×) produces a notice — no batch is reset away.
- 200× race of fallback recording against notice reset; exactly one consume
  transfers the batch and no state remains.
- 3 producers + a lock-contending holder + a consumer run concurrently; every
  observed batch produces an exact or lower-bound notice, all fallback state is
  transferred and cleared, and no producer blocks.

## POST-VALIDATE HARDENING 5 — single authoritative fallback marker queue

Corrects hardening 4. The split fallback signal (the `_omission_fallback`
event set *after* appending a marker) could duplicate one omission: if the
consumer drained the marker and cleared the batch before the producer set the
event, the orphaned set event later produced a second, marker-less fallback
notice.

- Removed `_omission_fallback` entirely: no `.set()`, `.clear()`, or `.is_set()`
  remains associated with omission accounting, and the defensive
  "signal without marker" branch in `_merge_fallback_locked()` is gone.
- `_omission_markers` is now the **sole** record of contention-path omissions.
  `_record_omission()` appends the marker and returns when `_omission_lock`
  cannot be acquired; publication of the marker is the whole fallback step, so
  nothing can arrive after it was already consumed.
- `_merge_fallback_locked()` drains only the markers currently available, treats
  the batch as containing a fallback omission iff at least one marker was
  drained, uses the smallest drained sequence as the boundary, and sets
  `_omission_batch_fallback` for the "at least N" rendering. When no marker is
  available the protected omission state is left unchanged, and the queue is
  never cleared, so a marker appended after draining is left for the next batch.
- Only the exact count, the active batch boundary, and `_omission_batch_fallback`
  remain protected by `_omission_lock`.

### RED evidence
Reintroducing an append-then-set split signal (plus a marker-less defensive
branch) makes the deterministic interleaving test fail — a second notice is
produced after the producer returns: **1 failure**
(`test_marker_append_then_consume_does_not_duplicate`).

### Green evidence
- `tests.test_host_presentation_phase9` → **78 tests OK**, run 15× with no
  flakiness.
- Full suite → **3998 tests OK (skipped=13)**, 3 clean runs.
- Focused host suite → **295 tests OK**.
- `scripts/check-types` → `All checks passed!`.

### New/changed tests (`TestOmissionDropAccounting`, now 17)
- `test_marker_append_then_consume_does_not_duplicate` — deterministic race
  regression: a `_HookedDeque` pauses the producer immediately after appending a
  marker; the consumer drains it and produces the only notice before the
  producer is resumed/returns; a final consume finds no second notice and no
  fallback state remains.
- `test_raced_fallback_is_never_reset_or_lost` now asserts the batch transfers
  **exactly once** and leaves `_omission_batch_fallback` false with an empty
  marker queue.
- `test_concurrent_fallback_accounting_loses_no_batch` retains multi-producer +
  consumer stress coverage and asserts the marker queue is empty and the batch
  flag cleared after the forced final drain (markers neither lost nor
  duplicated).

## POST-VALIDATE HARDENING 6 — step-scoped presentation state

Two pieces of `HostPresentationState` leaked across operational steps.

- **Transport progress leak.** `_received_bytes` is now set to `None` in
  `observe_step()` on `HostStepState.STARTED` before any rendering. A new step
  that has not observed progress can no longer display byte counts acquired by
  an earlier artifact/download step in its heartbeat.
- **Diagnostic-silence latch.** `observe_step()` on `STARTED` also resets
  `_silence_announced = False`, and `observe_heartbeat()` resets it whenever
  `diagnostic_silence_seconds` is absent or below `DIAGNOSTIC_SILENCE_SECONDS`.
  Previously only the first 120-second silence transition in the entire session
  was ever rendered; each step and each output-then-silence cycle now announces
  its own transition.
- **Cadence preserved.** The 120-second transition still restarts the line
  interval via `_status_since = now`, and recovery does **not** reset
  `_status_since`, so ordinary statuses keep their existing 30-second cadence
  and the next silence transition renders exactly when silence reaches 120
  seconds.

### RED evidence
Reverting both production changes makes exactly the three new tests fail:
**3 failures** in `TestLinesState`
(`test_step_start_discards_previous_step_byte_progress`,
`test_step_start_resets_silence_transition`,
`test_repeated_silence_transitions_each_render_once`).

### Green evidence
- `tests.test_host_presentation_phase9` → **81 tests OK**, run 15× with no
  flakiness.
- Full suite → **4001 tests OK (skipped=13)**, 3 clean runs.
- Focused host suite → **298 tests OK**.
- `scripts/check-types` → `All checks passed!`.

### New tests (3, in `TestLinesState`)
- `test_step_start_discards_previous_step_byte_progress` — step A observes 4096
  bytes; step B starts and heartbeats with no step-B progress; step B's rendered
  output never contains step A's byte count.
- `test_step_start_resets_silence_transition` — two consecutive steps each
  render their own distinct 120s/150s silence notice.
- `test_repeated_silence_transitions_each_render_once` — a 120s silence notice,
  a recovery heartbeat (silence absent), then a second 120s notice; exactly two
  notices render and the second restarts the 30s line interval.

## POST-VALIDATE HARDENING 7 — deadline servicing, window finalization, bounded omission fallback, per-step cadence

Four independent presentation-state hardening fixes in
`docker/versioning/host_presentation.py`, each with RED evidence.

### 1. Fixed deadline scheduling
- `admit_diagnostic` no longer resets a deadline unconditionally.  The
  `lines` window starts when its group begins (`NEW_GROUP`/`NUMERIC_VARIANT`)
  and stays fixed across `EXACT_REPEAT`; an already-scheduled interactive
  refresh is preserved rather than postponed.
- `PresentationWorker._run` now calls `state.due(now=clock())` at the top of
  every loop iteration, so a mailbox that is never empty can no longer starve
  the fixed window or the interactive refresh.

### 2. Lines window finalization
- `due()` emits the summary only when `exact_count >= 2`, then clears the
  completed group (`_coalescer.finalize()`), resets the group hostnames, and
  clears the deadline.  The next diagnostic starts a fresh group and terminal
  events/shutdown find nothing to replay.  The obsolete
  `_lines_flushed_count` field and its bookkeeping were removed.

### 3. Bounded, saturating omission fallback
- Removed the unbounded `_omission_markers` deque.  Contended drops now use
  fixed-size accounting: a monotonic saturating `_omission_fallback_total`
  (capped at `OMISSION_COUNTER_LIMIT`) plus a first-wins
  `_omission_fallback_boundary`, and a consumer-only
  `_omission_fallback_consumed` watermark.  Producers publish the boundary
  before the total as lock-free single assignments, so admission never blocks
  and any consumer that observes the count also observes the boundary.  The
  consumer only advances its watermark, so a concurrent increment is preserved
  for the next batch; contended drops report `[at least N diagnostics omitted]`.

### 4. Per-step status cadence
- `observe_step()` on `STARTED` now resets `_status_since = None`, so each
  step's first heartbeat status is eligible at that step's three-second mark
  instead of being suppressed by the preceding step's cadence.  The subsequent
  30-second cadence and the 120-second silence-transition restart still apply.

### RED evidence (each targeted test fails with its behavior reverted)
- lines window postponed on repeats → `test_continuous_repeats_do_not_postpone_the_lines_window` FAILED
- interactive refresh postponed on repeats → `test_continuous_repeats_do_not_postpone_the_interactive_refresh` FAILED
- worker skips top-of-loop `due()` → `test_continuously_busy_mailbox_does_not_starve_the_window` FAILED
- window leaves the group behind → `test_window_expiry_then_another_diagnostic_does_not_replay_summary` and
  `test_window_expiry_summary_is_not_replayed_at_terminal_or_shutdown` FAILED
- step start does not reset `_status_since` → `test_consecutive_steps_each_render_first_status_at_three_seconds` FAILED
- fallback total not saturating → `test_contended_fallback_storage_saturates_without_a_queue` FAILED

### Green evidence
- `tests.test_host_presentation_phase9` → **87 tests OK**, run 15× with no flakiness.
- Full suite → **4007 tests OK (skipped=13)**, 3 clean runs.
- Focused host suite (9 modules) → **400 tests OK**.
- `scripts/check-types` → `All checks passed!`; no stale `_omission_markers` /
  `_lines_flushed_count` references remain.

### New / adjusted tests
- `TestInteractiveState.test_continuous_repeats_do_not_postpone_the_interactive_refresh`
- `TestLinesState.test_continuous_repeats_do_not_postpone_the_lines_window`
- `TestLinesState.test_window_expiry_then_another_diagnostic_does_not_replay_summary`
- `TestLinesState.test_window_expiry_summary_is_not_replayed_at_terminal_or_shutdown`
- `TestLinesState.test_consecutive_steps_each_render_first_status_at_three_seconds`
- `TestWorkerDeadlineServicing.test_continuously_busy_mailbox_does_not_starve_the_window`
  (uses `_NeverEmptyMailbox` for a deterministic, non-timing-dependent starve probe)
- `TestOmissionDropAccounting.test_contended_fallback_storage_saturates_without_a_queue`
- Updated the two sustained-contention regression tests to assert bounded,
  saturating fallback storage instead of the removed marker deque.

## POST-VALIDATE HARDENING 8 — reusable omission accounting, lines-summary hostname policy, emergency queue cleanup

Three correctness fixes in `docker/versioning/host_presentation.py`, each with
RED evidence.

### 1. Reusable, bounded fallback omission accounting
- Removed the lifetime-saturating `_omission_fallback_total` /
  `_omission_fallback_consumed` / `_omission_fallback_boundary` design, which
  made every contended drop after the saturated total was consumed permanently
  invisible.
- Contended producers now publish an ordering marker with a single atomic
  `deque.append` onto `_omission_fallback_markers`, a fixed-size
  (`OMISSION_FALLBACK_MARKERS = 64`) deque.  Admission never blocks, storage
  never grows with the number of drops, and concurrent drops cannot be lost or
  duplicated.
- `_drain_fallback_locked()` (called under `_omission_lock`) folds the markers
  into a reusable `_omission_fallback_pending` count and lowers the batch
  boundary with the smallest drained marker; markers appended during the drain
  are left for the next batch.  `take_omission_notice()` clears both the exact
  count and the pending count only when a notice is produced, so subsequent
  batches are always visible.  Contended drops are reported as
  `[at least N diagnostics omitted]` (`N` capped at `OMISSION_COUNTER_LIMIT`,
  and at the bounded marker capacity after saturation).

### 2. Hostname policy on every lines finalization path
- `admit_diagnostic()` no longer renders raw `finalized_text` in `lines` mode.
  Both the flush triggered by a *different* diagnostic and the flush triggered
  by an *omission notice* now apply
  `_attach_hosts(finalized_text, previous_hostnames)` before rendering, matching
  the deadline (`due()`) and terminal (`_finalize_group()`) paths.

### 3. Emergency queue cleanup
- The worker's emergency branch now discards both mailbox queues
  (`self._mailbox.drain()`) as well as pending presentation state before the
  `mark_worker_stopped()` acknowledgement runs in `finally`.  No queued event is
  rendered and no unprocessed barrier is acknowledged.  Cleanup stays on the
  worker thread, outside guarded producer callbacks.

### RED evidence (each targeted test fails with its behavior reverted)
- lifetime-saturating fallback (markers published only until the first drain) →
  `test_bounded_fallback_is_reusable_after_saturation` FAILED (later batch invisible)
- raw `lines` summaries without hostname policy →
  `test_hostnames_are_consistent_across_every_finalization_path` FAILED for
  `path='different'` and `path='omission'`
- emergency branch without `drain()` →
  `test_terminal_admission_failure_discards_queued_events` and
  `test_shutdown_admission_failure_discards_queued_events` FAILED (queues non-empty)

### Green evidence
- `tests.test_host_presentation_phase9` → **93 tests OK**, 20 consecutive runs
  with no flakiness.
- Focused host suite (10 modules) → **437 tests OK**.
- Full suite → **4013 tests OK (skipped=13)**, 3 clean runs.
- `scripts/check-types` → `All checks passed!`; `openspec validate
  improve-host-build-observability --type change` → valid.
- No stale references to the removed saturation fields remain.

### New / adjusted tests
- `TestOmissionDropAccounting.test_bounded_fallback_is_reusable_after_saturation`
  — bounded deque, saturation, consumption, and a visible later batch.
- `TestOmissionDropAccounting.test_markers_published_across_several_consumes_are_never_lost`
  — 40 producer/consumer interleavings, each batch counted once.
- Updated `test_multiple_contended_drops_are_one_bounded_notice`,
  `test_contended_drops_report_a_conservative_lower_bound`,
  `test_raced_fallback_is_never_reset_or_lost`, and
  `test_concurrent_fallback_accounting_loses_no_batch` (post-race batch).
- `TestLinesHostnameFinalization` — enabled/disabled hostname assertions across
  the `deadline`, `different`, `terminal`, and `omission` finalization paths.
- `TestEmergencyQueueCleanup` — terminal-admission, shutdown-admission, and
  post-rendering emergency terminations assert empty queues, worker stop, no
  event rendering, and no unprocessed-barrier acknowledgement.

## POST-VALIDATE HARDENING 9 — overflow-safe omission boundary, emergency check before deadlines

Two ordering fixes in `docker/versioning/host_presentation.py`, each with RED
evidence.

### 1. Preserve the earliest pending omission boundary across overflow
- `_omission_fallback_markers` is a fixed-size deque, so once it overflows it
  evicts the earliest marker — and with it the batch's earliest ordering
  boundary, which delayed the omission notice past the next admitted
  diagnostic.
- Added a bounded, race-safe `_omission_fallback_earliest` field.  Contended
  producers set it first-wins before appending their marker; producers remain
  non-blocking.  `_drain_fallback_locked()` folds it into the batch boundary
  before the drained markers, so the earliest boundary survives overflow.
- When a notice is produced, `_omission_fallback_earliest` is re-seeded from
  the minimum still-unreported marker (`min(remaining)` or `None`), so the next
  batch keeps a correct lower bound and a stale boundary is never reused.
  Contended drops still report a conservative `[at least N diagnostics omitted]`.

### 2. Emergency check before servicing deadlines
- `PresentationWorker._run()` now checks emergency state both before and after
  mailbox waiting via the shared `_stop_on_emergency()` helper.  An
  already-signalled emergency discards queued events and pending presentation
  state and terminates *without* calling `HostPresentationState.due()`, so an
  expired repeat summary is never emitted.  The post-wait check is retained for
  a stop signalled while parked, and cleanup still runs on the worker thread
  before the worker-stopped acknowledgement, acknowledging no discarded
  barrier.

### RED evidence
- bounded-deque-only boundary (`smallest` from drained markers, no earliest
  retention) → `test_overflow_preserves_the_earliest_boundary` FAILED (notice
  never surfaced before the admitted diagnostic).
- no pre-`due()` emergency check → `test_emergency_before_servicing_an_expired_deadline_renders_nothing`
  FAILED (emitted `retry (repeated 2 times)`).

### Green evidence
- `tests.test_host_presentation_phase9` → **95 tests OK**, 15 consecutive runs.
- Focused host suite (10 modules) → **439 tests OK**.
- Full suite → **4015 tests OK (skipped=13)**, 3 clean runs.
- `scripts/check-types` → `All checks passed!`; `openspec validate
  improve-host-build-observability --type change` → valid.

### New tests
- `TestOmissionDropAccounting.test_overflow_preserves_the_earliest_boundary`
  — an initial drop, an admitted diagnostic, then enough later drops to
  overflow the deque; the notice appears before that admitted diagnostic (and
  before a later one).
- `TestEmergencyQueueCleanup.test_emergency_before_servicing_an_expired_deadline_renders_nothing`
  — a pending repeated-diagnostic group with an expired window, emergency
  signalled first: no summary rendered, queues emptied, barrier unacknowledged,
  worker stopped.

## POST-VALIDATE HARDENING 10 — race-safe omission rollover and ordered telemetry supersession

### 1. Distinct fallback batches across notice consumption
- Removed notice-reset reconstruction of `_omission_fallback_earliest` from
  `min(remaining)`.  A drained fallback batch now immediately rolls the
  first-wins slot to `None` while `_omission_lock` is held.
- While that batch's count/boundary is pending, `take_omission_notice()` does
  not drain newly published markers.  Contended drops arriving after the
  drain therefore create a distinct next batch with their own earliest
  boundary; marker overflow cannot replace that boundary with a later marker.
- Storage remains fixed-size (`deque(maxlen=OMISSION_FALLBACK_MARKERS)`), and
  producers still use only non-blocking lock acquisition plus bounded atomic
  marker publication.

### 2. Fresh sequence for superseded telemetry
- Telemetry supersession no longer replaces an entry in place while retaining
  its obsolete sequence.  It deletes the obsolete heartbeat/progress entry and
  appends the replacement with `_next_sequence()`.
- Queue length remains unchanged, capacity bounds and `superseded` accounting
  are preserved, and cross-lane merge order now places the replacement after
  terminal/start barriers admitted before it.

### Deterministic RED evidence
- Restoring unconditional fallback draining plus notice-reset
  `min(remaining)` reconstruction caused
  `test_rollover_drop_keeps_its_boundary_after_overflow` to ERROR because the
  second batch's first boundary was evicted and no notice preceded its first
  eligible diagnostic.
- Restoring replacement-in-place with `item.sequence` caused all three new
  supersession ordering tests to FAIL: the replacement appeared before the
  terminal and subsequent start events.

### New regression coverage
- `TestOmissionDropAccounting.test_rollover_drop_keeps_its_boundary_after_overflow`
  pauses consumption after the first batch drain, publishes a new drop plus
  enough later drops to overflow fallback storage, completes the first notice,
  and proves the second notice renders before its first eligible diagnostic.
- `TestMailboxLanes.test_superseded_heartbeat_follows_terminal_and_restart`.
- `TestMailboxLanes.test_superseded_progress_follows_terminal_and_restart`.
- `TestMailboxLanes.test_superseded_progress_follows_repeated_acquisition_barriers`
  covers successive `rustup` and `uv` acquisition steps sharing the same
  supersession key.

### Green evidence
- `tests.test_host_presentation_phase9` → **99 tests OK**, 20 consecutive runs.
- Focused presentation/host lifecycle suite (10 modules) → **443 tests OK**.
- `npx pi-green-loop check --affected docker/versioning/host_presentation.py,tests/test_host_presentation_phase9.py`
  → typecheck and tests passed.
- Full suite → **4019 tests OK (skipped=13)**, 3 clean runs.
- `scripts/check-types` → `All checks passed!`.
- `openspec validate improve-host-build-observability --type change` → valid.
- Proposal, design, specs, and `tasks.md` were unchanged.

## POST-VALIDATE HARDENING 11 — atomic fallback-generation handoff

### Fix
- Replaced the single fallback-boundary rollover slot with two fixed generation
  slots and generation-tagged markers in the existing bounded deque.
- `_drain_fallback_locked()` flips the active generation before removing the
  detached generation's first marker, captures and clears only that detached
  generation's first-wins boundary, and drains only markers tagged for it.
- A producer arriving after the handoff therefore writes its marker and
  earliest boundary into the other generation.  The old consumer cannot clear
  that slot, and later marker overflow cannot remove its separately retained
  boundary.
- Marker storage remains `deque(maxlen=OMISSION_FALLBACK_MARKERS)`; producers
  continue to use only non-blocking omission-lock acquisition and bounded
  lock-free fallback publication.

### Deterministic RED evidence
- Added
  `TestOmissionDropAccounting.test_inside_drain_rollover_cannot_clear_the_next_boundary`.
  Its mailbox test seam pauses inside `_drain_fallback_locked()` after the
  detached generation observes its deque empty.  While paused it publishes a
  drop at sequence 2 and enough sequence-3 drops to overflow marker storage.
- Moving generation handoff/reset back after that pause reproduced the defect:
  `take_omission_notice(2)` returned `None` instead of
  `[at least 64 diagnostics omitted]`.
- The previous `test_rollover_drop_keeps_its_boundary_after_overflow` remains
  unchanged and continues to cover publication after `_drain_fallback_locked()`
  returns but before the first notice reset.

### Green evidence
- `tests.test_host_presentation_phase9` → **100 tests OK**, 20 consecutive runs.
- Focused omission/mailbox/deadline/emergency and host lifecycle suite →
  **185 tests OK**.
- Focused presentation/host integration suite (10 modules) → **444 tests OK**.
- `npx pi-green-loop check --affected docker/versioning/host_presentation.py,tests/test_host_presentation_phase9.py`
  → typecheck and tests passed.
- Full suite → **4020 tests OK (skipped=13)**, 3 clean runs.
- `scripts/check-types` → `All checks passed!`.
- `openspec validate improve-host-build-observability --type change` → valid.
- Proposal, design, specs, and `tasks.md` were unchanged.

## POST-VALIDATE HARDENING 12 — detached omission evidence survives shared-deque eviction

### Fix
- Removed the assumption that `drained == 0` proves the detached fallback
  generation contained no omissions.
- At generation handoff, `_drain_fallback_locked()` captures the detached
  generation's first-wins earliest boundary in a local value before clearing
  only that generation's slot.  This captured value is independent of the
  shared marker deque and cannot be overwritten by new-generation producers.
- If new-generation overflow evicts every detached marker, a nonempty captured
  boundary contributes a conservative lower-bound count of one, preserves its
  ordering boundary, and emits `[at least 1 diagnostics omitted]` rather than
  silently discarding the batch.
- The two generation slots, generation-tagged `deque(maxlen=64)`, non-blocking
  producer lock attempt, and bounded fallback publication remain unchanged.

### Deterministic RED evidence
- Added
  `TestOmissionDropAccounting.test_detached_boundary_survives_new_generation_overflow`.
  It records sequence 1, pauses immediately after the generation flip and
  before marker drain, publishes enough sequence-2 drops to evict all detached
  markers, then resumes.
- Restoring the old `if drained == 0: return 0` behavior made the test fail:
  the old notice was `None` instead of `[at least 1 diagnostics omitted]`.
- Green behavior reports the old batch at sequence 1, defers the new batch at
  sequence 1, and reports the bounded new batch at sequence 2.
- Both earlier rollover tests remain present: one pauses after the drain, and
  one pauses after the detached generation observes an empty deque.

### Green evidence
- `tests.test_host_presentation_phase9` → **101 tests OK**, 20 consecutive runs.
- Focused omission/mailbox/deadline/emergency and host lifecycle suite →
  **186 tests OK**.
- Focused presentation/host integration suite (10 modules) → **445 tests OK**.
- `npx pi-green-loop check --affected docker/versioning/host_presentation.py,tests/test_host_presentation_phase9.py`
  → typecheck and tests passed.
- Full suite → **4021 tests OK (skipped=13)**, 3 clean runs.
- `scripts/check-types` → `All checks passed!`.
- `openspec validate improve-host-build-observability --type change` → valid.
- Proposal, design, specs, and `tasks.md` were unchanged.

## POST-VALIDATE HARDENING 13 — atomic fallback publication and exact retirement

### Fix
- Removed the separately published `_omission_fallback_earliest` evidence.
  Producers no longer mutate a boundary slot and then append a marker as two
  independently observable operations.
- Fallback storage is now two fixed generation-local
  `deque(maxlen=OMISSION_FALLBACK_MARKERS)` instances.  Each marker atomically
  publishes `(sequence, cumulative_boundary)` in one `append`; before that
  append the consumer sees no omission evidence.
- The cumulative boundary carried by retained markers preserves the earliest
  known sequence after same-generation overflow.  The generation flip detaches
  one deque before draining, so new-generation producers cannot evict or alter
  detached evidence.
- A producer delayed across retirement appends only one marker to its selected
  generation.  That marker is drained once when that generation next detaches;
  no independently reported boundary remains to duplicate it.
- Total fallback storage remains fixed (two deques of 64 markers), exact
  accounting still uses only a non-blocking lock attempt, and fallback producer
  publication remains non-blocking.

### Deterministic RED evidence
- Added
  `TestOmissionDropAccounting.test_paused_producer_publication_is_consumed_exactly_once`.
  It forces fallback accounting, pauses immediately before the marker append,
  lets the consumer attempt notice consumption, resumes publication, then
  consumes repeatedly.
- Splitting publication into visible boundary evidence followed by the delayed
  marker made the test fail: the pre-resume consumer already returned
  `[at least 1 diagnostics omitted]`, demonstrating the duplicate-notice race.
- Green behavior returns no notice before append and exactly one notice after
  the producer resumes; all marker storage is then empty.

### Retained coverage
- All prior rollover tests remain: post-drain publication, inside-drain empty
  observation, generation-flip flooding, saturation, reusable batches,
  omission ordering, supersession ordering, prompt return, emergency cleanup,
  and worker lifecycle.

### Green evidence
- `tests.test_host_presentation_phase9` → **102 tests OK**, 20 consecutive runs.
- Focused omission/mailbox/deadline/emergency and host lifecycle suite →
  **187 tests OK**.
- Focused presentation/host integration suite (10 modules) → **446 tests OK**.
- `npx pi-green-loop check --affected docker/versioning/host_presentation.py,tests/test_host_presentation_phase9.py`
  → typecheck and tests passed.
- Full suite → **4022 tests OK (skipped=13)**, 3 clean runs.
- `scripts/check-types` → `All checks passed!`.
- `openspec validate improve-host-build-observability --type change` → valid.
- Proposal, design, specs, and `tasks.md` were unchanged.

## Regression verification — failure replay and terminal wrapping

Corrected two Phase 9 defects without changing specification artifacts or task
checkboxes:

- Locked-npm failures now carry a concise summary and retained diagnostics as
  separate fields. The facade report owns both paths and no longer appends a
  message that embeds the same tail.
- Presentation state records whether a diagnostic was actually written
  durably. Final failure rendering suppresses replay in interactive/`lines`
  sessions, preserves the tail when default noninteractive output produced no
  live diagnostics, and permits timeout replay only under the explicit
  `Retained diagnostics (timeout context)` label.
- Interactive terminal text is clipped to less than the available physical-row
  width using Unicode display-cell width; durable diagnostics remain complete.

Verification:

- `.venv/bin/python -m unittest tests.test_host_failure_output_regression -v`
  → **8 tests OK** (real npm exit/timeout construction, orchestration-to-CLI
  conversion for interactive, explicit `lines`, and default noninteractive
  output, durable replay tracking, and narrow-terminal wide-Unicode state).
- `.venv/bin/python -m unittest tests.test_host_presentation_phase9 tests.test_host_failure_output_regression tests.test_constructor_build_orchestration tests.test_constructor_facade -q`
  → **360 tests OK**.
- `npx pi-green-loop check --affected docker/constructor_cli.py,docker/versioning/build_orchestration.py,docker/versioning/host_presentation.py,docker/versioning/host_progress.py,docker/npm_environment/errors.py,docker/npm_environment/execution.py,tests/test_host_failure_output_regression.py --feedback`
  → all checks passing.
- `.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -q`
  → **4031 tests OK (skipped=13)**.
- `openspec validate improve-host-build-observability --strict` → valid.

## Regression verification — partial diagnostic delivery

Replaced the session-wide durable-write boolean with a bounded deque of at most
512 non-presentable diagnostic identities. Each identity includes phase, step,
stdout/stderr stream, and a fixed-width digest of the sanitized line. Records
are added only after a durable renderer call succeeds. Failure rendering uses
occurrence counts from those structured identities to omit only matching lines
from the failing operation's retained tail; old records that fall out of the
bound fail open by allowing replay rather than hiding unseen output.

Interactive pending identities follow coalescing state: exact repeats remain
pending until finalization, numeric replacement discards replaced mutable
values, and only the latest successfully finalized value is recorded. Renderer
failure and telemetry omission therefore leave unseen retained lines eligible
for the failure report. Timeout context continues to replay the complete tail
under its explicit label.

Structured npm errors now distinguish a missing legacy `diagnostic_tail` field
from an authoritative empty string. Empty exit and timeout failures render one
summary and no diagnostic section.

Verification:

- `.venv/bin/python -m unittest tests.test_host_presentation_phase9 tests.test_host_failure_output_regression tests.test_constructor_build_orchestration tests.test_npm_environment_execution -q`
  → **220 tests OK**.
- Partial-delivery regressions cover a dropped final error, renderer failure
  after one write, output from a previous operation, interactive numeric
  replacement, and empty-output exit/timeout failures.
- `.venv/bin/python -m unittest tests.test_constructor_facade tests.test_host_failure_output_regression -q`
  → **176 tests OK**, including orchestration-to-CLI failure conversion.
- `npx pi-green-loop check --affected docker/constructor_cli.py,docker/npm_environment/errors.py,docker/npm_environment/execution.py,docker/versioning/build_orchestration.py,docker/versioning/host_presentation.py,docker/versioning/host_progress.py,tests/test_host_failure_output_regression.py,tests/test_host_presentation_phase9.py --feedback`
  → all checks passing.
- `openspec validate improve-host-build-observability --strict` → valid.
- `git diff --check` → passed; no files were staged or unstaged by this work.

# Phase 9 architecture migration verification

Scope: revised tasks 9.9–9.10 and 9.13–9.23 of
`improve-host-build-observability`. This evidence is separate from the
historical `verification-phase9.md` report for the superseded architecture.

## Implemented migration

- Replaced the physical control/telemetry lanes, sequence merge, try-lock
  emergency path, and omission fallback generations with one mutex-owned FIFO.
  Telemetry and control retain independent admission budgets.
- Replaced the queued shutdown marker with capacity-independent, idempotent
  close, completion signalling, and one cumulative five-second deadline for
  completion and join.
- Renderer exceptions disable later output while allowing normal drain. A
  stalled renderer permits bounded return with a daemon actor; cancellation is
  checked before any later renderer operation after the in-flight write.
- Removed delivery digests, delivery-history reconciliation, and partial-tail
  filtering. Every nonempty retained tail is rendered once per report beneath
  `Retained diagnostics (may repeat live output):`.
- Added an explicit marked facade streaming capability. Only that known path
  bypasses `SinkDispatcher`; arbitrary callbacks retain dispatcher isolation.
- Routed live-session final failure reports through the presentation actor and
  suppressed generic CLI retry regardless of admission/render success.
- Removed the obsolete Phase 1 mailbox implementation and shutdown DTO.

## Obsolete-to-replacement coverage

| Superseded expectation | Replacement coverage |
| --- | --- |
| Physical lanes and sequence merge | `TestOrderedInbox.test_single_fifo_preserves_control_and_telemetry_admission_order` |
| Try-lock emergency on contention | `TestOrderedInbox.test_short_mutex_contention_waits_for_lock_not_emergency` |
| Omission fallback generations | Bounded exact/saturated notice coverage in `TestOrderedInbox` and existing state tests |
| Shutdown sentinel and missing acknowledgement | `TestOrderedInbox.test_close_is_capacity_independent_idempotent_and_rejects_late_admission` |
| Unconditional worker termination | `TestCapacityIndependentSessionClose.test_stalled_renderer_uses_one_budget_and_starts_no_later_write` |
| Delivery hashes and unseen-tail filtering | `TestRetainedFailureContext` |
| Always-dispatched stream callback | `TestFacadeDirectEnqueuePath` |
| Generic CLI failure writer during a live session | `TestLiveFinalReportOwnership` |

Normal interactive/lines/off, cadence, hostname privacy, URL identity,
coalescing, empty-tail, safe failure context, SDK isolation, and narrow-terminal
coverage remains active.

## Commands and results

- `python -m unittest tests.test_host_presentation_phase9 tests.test_host_failure_output_regression tests.test_npm_environment_streaming` — **119 tests OK**.
- Focused host presentation/streaming/orchestration run — **263 tests OK**
  (**8 skipped**).
- `scripts/check-types` — **All checks passed**.
- `python -m unittest` — **3991 tests OK** (**13 skipped**) in 42.559s.
- `npx pi-green-loop check --since HEAD` — typecheck and complete test checks passed.
- `openspec validate improve-host-build-observability --strict` — change valid.
- `git diff --check` — no whitespace errors.

The gated-renderer test proves shutdown returns before releasing the renderer,
then releases and joins the actor and verifies no subsequent renderer call.
Healthy and renderer-exception sessions drain and terminate. Full-suite tests
preserve primary success/failure/timeout/cancellation semantics, bounded safe
capture, and existing cleanup behavior.

## Control transcript and stream-width follow-up

- Replaced the enum-relative reservation test with independent supported-build
  transcripts: the 40-control success transcript and the 41-control late
  failure/final-report transcript both remain admissible with a stalled
  consumer.
- Verified deliberate control exhaustion still aborts presentation separately.
- Verified the production terminal renderer queries its own stream descriptor
  when stdout is redirected and stderr is an eight-column terminal; transient
  output remains clipped with the one-cell margin and clears without a wrapped
  stale row.
- `.venv/bin/python -m unittest tests.test_host_presentation_phase9 tests.test_host_failure_output_regression tests.test_constructor_build_orchestration tests.test_constructor_pi_assembly tests.test_npm_environment_streaming` — **238 tests OK**.
- `ty check docker --python-version 3.14 --output-format concise` — **All checks passed**.
- `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'` — **4006 tests OK** (**13 skipped**) in 44.290s.

## Fixed control hard-limit follow-up

- Replaced the transcript-derived 41-control reservation with the fixed
  `CONTROL_CAPACITY = 1024` bounded-memory/runaway guard. Production mailbox
  construction uses this default; explicit smaller capacities remain test seams.
- Independently constructed supported 40-control success and 41-control
  failure/final-report transcripts remain admissible while the consumer is
  stalled. A separate boundary test admits exactly 1024 controls, confirms the
  1025th disables presentation without another queue slot, rejects later
  admission, leaves close available, and preserves the primary result.
- `python -m unittest tests.test_host_presentation_phase9 tests.test_host_failure_output_regression tests.test_constructor_build_orchestration tests.test_constructor_pi_assembly tests.test_npm_environment_streaming tests.test_host_operational_events tests.test_constructor_host_progress` — **259 tests OK** in 3.673s.
- `ty check docker --python-version 3.14 --output-format concise` — **All checks passed**.
- `python -m unittest discover -s tests -p 'test_*.py'` — **4007 tests OK** (**13 skipped**) in 43.177s.
- `npx pi-green-loop check --since HEAD` — typecheck and complete test checks passed (107 ms typecheck; 43.463 s tests).
- `openspec validate improve-host-build-observability --strict` — change valid.
- `git diff --check` — no whitespace errors.

## Corrected control-transcript and result-isolation coverage

- Replaced the synthetic transcript with independently constructed exact
  producer-order success and failure/final-report transcripts. Build-artifact
  acquisition/cache events precede Pi release-acquisition lifecycle start;
  Pi asset steps remain within that lifecycle pair. Docker failure reporting
  retains the emitted Docker-transition started/succeeded pair followed by the
  facade final report. The representative release-acquisition failure contains
  prior build-artifact events, release phase start, the first Pi asset step
  start and failure, release phase failure, and its final report. The test
  asserts sequence and multiplicity after admitting each transcript to a
  stalled default mailbox, without deriving either transcript from
  `CONTROL_CAPACITY`.
- Retained the literal-1024 mailbox boundary test. Added a facade integration
  test that stalls the real presentation actor, drives the real event adapter
  through the build facade until the 1025th control disables presentation,
  verifies later rejection and capacity-independent close, and confirms the
  returned `BuildResult` is unchanged.
- `python -m unittest tests.test_host_presentation_phase9 tests.test_host_failure_output_regression tests.test_constructor_build_orchestration tests.test_constructor_pi_assembly tests.test_npm_environment_streaming tests.test_host_operational_events tests.test_constructor_host_progress` — **260 tests OK** in 3.665s.
- `ty check docker --python-version 3.14 --output-format concise` — **All checks passed**.
- `python -m unittest discover -s tests -p 'test_*.py'` — **4008 tests OK** (**13 skipped**) in 43.511s.
- `npx pi-green-loop check --since HEAD` — typecheck and complete test checks passed (105 ms typecheck; 43.612 s tests).
- `openspec validate improve-host-build-observability --strict` — change valid.
- `git diff --check` — no whitespace errors.

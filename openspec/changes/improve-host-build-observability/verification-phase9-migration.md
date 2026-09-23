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

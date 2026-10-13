# Configured-stage child polling regression verification

## Failure and fix

Both `_run_policy` polls previously escaped shared failure accounting and killed
its continuation before fallback, stream release and publication. Context polling
now runs inside the initiation `CleanupFailures` action. Configured child waiting
uses `_action` with explicit ordinary `OSError` classification; failed observation
is distinct from a live child and ends the stage immediately. Both paths cancel
prepared stage resources once, advance to subsequent stages or local fallback,
and preserve the original deadline and shared precedence. Persistent poll failure
still permits protected wait/kill/reap and independent stream release.

API, default policy, consumer modules and specification bodies are unchanged.
The structural proof now requires both protected observation paths.

## RED

`/tmp/policy-poll-red.uwTlY5.log`, exit 1: **27 failed** before the fix,
reproducing uncaught OSError, defect and interruption at context and wait polls.

## Coverage

Final regression coverage adds **57 tests**:

- Context observation, action-backed child waiting and wait-only stages.
- OSError, RuntimeError and KeyboardInterrupt, each with no primary, ordinary
  operation primary or earlier interruption primary.
- Next-stage progression and direct local fallback after the final stage.
- Independent stream-release failure accounting and successful sibling release.
- Exact winning exception identity, retained secondary failures, at-most-once
  action cleanup/stream release, completed retained cleanup and repeat shutdown
  without new polling or changed diagnostics.
- Persistent polling faults in both stages and local termination observation:
  protected wait/kill/reap still completes and releases both streams.

## Validation

- Initial focused gate: `/tmp/policy-poll-focused.uAV2WX.log`, exit 0;
  240 passed (before extending to direct-fallback/persistent-fault cases).
- Initial full gate: `/tmp/docker-constructor-pytest.7aRHcD.log`, exit 1;
  1 failed, 6794 passed, 13 skipped. An existing precedence-only test depended on
  all stages starting within an 80 ms wall-clock budget under worker contention.
  Its clock is now deterministic; all original assertions remain intact.
- Final full gate: `/tmp/docker-constructor-pytest.emmRkk.log`, exit 0;
  **6795 passed, 13 skipped, 11 warnings, 17775 subtests passed**, 30.79s.
- Typecheck: `/tmp/policy-poll-typecheck.2wpaxE.log`, exit 0; all checks passed.
- Configured unittest discovery: `/tmp/policy-poll-unittest.w2LUHV.log`, exit 0;
  **6426 tests**, OK, 13 skipped, 142.379s.
- Whitespace: `/tmp/policy-poll-whitespace.wQC7BS.log`, exit 0; clean.

Commands used the project virtual environment; pytest used eight workers and
loadfile distribution. Complete stdout/stderr and runner exit statuses were
preserved. Existing warnings were not suppressed. The configured checks were
run directly, as the green-loop launcher was already unavailable locally.

No task scope, specification body or Git index was modified.

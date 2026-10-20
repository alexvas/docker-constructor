# Live join interruption — authorized RED/GREEN ledger

User explicitly requests separation of join attempt and completion, strict
reader/constructor settlement, bounded normal dispatcher finalization, genuine
live-worker regressions and refreshed verification. HEAD remains
`c2bc02371f539f7e3998fcf2a3c6225a7db8e576`, with the previous CI2/bootstrap
repairs already present and externally staged. No index operations are allowed.
The previous bootstrap report is historical and its attempted-join caching does
not prove settlement after an interrupted actual join.

## Work orders established before implementation

| ID | Target / action | Verification |
| --- | --- | --- |
| J-C | Distinguish requested cleanup, first join attempt, observed completion and final cached result | actual live-worker join failures; repeat observations |
| J-R | Retain reader owner and observe completion before tail reads/capture, preserving primary authority | both readers, sole/combined OSError/KeyboardInterrupt, late data and independent dispatcher cleanup |
| J-D | Settle dispatcher constructor rollback; retain one original deadline for normal arbitrary callbacks | constructor failures plus live join interruption; blocked normal callback and remaining-budget checks |
| J-V | Fresh focused/full gates and generated fingerprint; no spec changes | final manifests and independent verification report |

RED command:
`.venv/bin/python -m pytest tests/enforcement/test_ci_lifecycle_scheduling.py -k 'live_join_interruption or normal_dispatcher_interrupted_join' --tb=short -q`
Exit **1**, **36 failed, 70 deselected**, 0.60 s, no warning summary.
Complete stdout/stderr was captured temporarily with the runner's actual status.

The 32 strict-settlement cases interrupt Thread.join **before calling the real
join**, after proving the native worker is alive and blocked. Combined reader
cases hold back actual reader data; sole reader cases delay native exit after
logical EOF so collection can legitimately reach join. Constructor cases delay
dispatcher native exit. Both retained-tail reads and capture attachment assert
all acquired workers completed. Half inject another interrupt while observing
completion. All assert original authority, independent cleanup and no renewed
Thread.join/release on repeat observations.

Four normal dispatcher cases hold the SDK callback blocked, inject join failure
while it is live, and use logical budget accounting: 20 ms already consumed of
50 ms, so only 30 ms may remain for completion observation. They require bounded
return with the same owner retained, no fresh repeat budget, and explicit test
teardown of the blocked worker. Logical time isolates budget semantics from OS
scheduling; real waits/guarded teardown retain genuine worker liveness.

- [x] J-C/J-R/J-D RED
- [x] J-C GREEN — join attempt, retained deadline and actual native completion are separate; repeated settled observations are cached.
- [x] J-R GREEN — actual reader settlement precedes every tested tail read/capture, with original authority and independent cleanup.
- [x] J-D GREEN — constructor rollback settles; every secondary type is retained; normal callbacks consume only the original remaining allowance.
- [x] J-V GREEN — fresh focused/full checks, canonical inventory and final manifests verified.

## Secondary-context refinement before final verification

Initial implementation passed all 106 focused scheduling cases (exit 0,
1.47 s). Strengthened assertions then exposed constructor rollback reporting
only the first cleanup exception type, losing visibility of a later completion-
observation interrupt. This is within J-D/J-C authority preservation, not a new
policy. Before its fix:
`.venv/bin/python -m pytest tests/enforcement/test_ci_lifecycle_scheduling.py -k 'live_join_interruption and npm-sink-dispatcher and True' --tb=short -q`
Exit **1**, **4 failed, 102 deselected**, 0.31 s. The original exception must
retain one safe type-only note per observed secondary failure; no SDK/private
error contents may be exposed at the constructor boundary. Final checks must
be rerun after that correction.

## Final verification

All 70 previous scheduling cases remain, plus 36 new live-join / bounded-callback
cases. Fresh final focused run: **286 passed / 459 subtests**, exit 0, 13.66 s.
`scripts/check-types` exited 0. Complete gates on final source:
`scripts/check-green` exit 0, **8157 passed, 13 reviewed skips, 18028 subtests,
11 warnings**, 53.88 s; `taskset -c 0,1 scripts/check-green` exit 0, same counts,
118.99 s, still using eight pytest workers. Another **180** controlled cases
passed on two CPUs. Generated inventory probe has zero errors and unchanged
non-hash fields; only streaming.py's source AST hash was refreshed.

Exact final commands, current fingerprints, warnings and limitations are in
`p6-live-join-results.md`. No specification or task-box edits occurred; previous
reports remain historical rather than proof of the repaired join path.

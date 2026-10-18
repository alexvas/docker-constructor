# Second CI repair — authorized RED/GREEN ledger

Base source: `c2bc02371f539f7e3998fcf2a3c6225a7db8e576`. User explicitly
authorized the production collector startup/interruption repair as well as
fixture determinization. Historical baseline/remote evidence is in
`p6-ci2-diagnosis.md`; those runs are not checks of the repaired source.

All work orders below were defined and made RED before implementation:

| ID | Single target / action | Prerequisites | Verification |
| --- | --- | --- | --- |
| C2-S | Guard partial collector reader startup with existing interruption cleanup/capture path | approved production scope; diagnosed startup gap | 16 before/after-start, first/second-reader, RuntimeError/KeyboardInterrupt, with/without unblock-failure combinations |
| C2-J | Attempt independent joins and dispatcher finalization without replacing operation authority | C2-S cleanup path | 4 join-failure cases, alone or with primary startup failure |
| C2-R | Preserve CLI retained report when SIGINT lands during second-reader startup | C2-S; real CLI boundary | original failed-renderer test under controlled startup pause |
| C2-A | Make pure action-authority fixture independent of OS scheduling, preserving every original authority/independent-action assertion | approval; diagnosed tiny wall-clock assumptions | original matrix plus controlled 150 ms scheduler pause |
| C2-P | Establish exited-parent / live inherited-pipe precondition before measuring short drain budget | approval; diagnosed interpreter startup inclusion | original real-process test plus controlled 200 ms startup delay |

RED command:
`.venv/bin/python -m pytest tests/enforcement/test_ci_lifecycle_scheduling.py --tb=short -q`
Exit **1**, **23 failed**, 0.64 s, no warning summary. Full stdout/stderr was
captured temporarily. These are expected assertions on missing cleanup/capture
or invalid synchronous/startup assumptions, not missing-tool errors.

Implementation constraints: no skips, no reduced parallelism, no production
shutdown-budget change, no accepted -15 outcome in the exited-parent test.
Pure authority fixtures may virtualize policy time, while real deadline tests
continue using wall time. Borrowed child/pipes must remain under the original
OwnedProcess; no independent child wait/reap/close or re-adoption is allowed.

- [x] C2-S RED
- [x] C2-J RED
- [x] C2-R RED
- [x] C2-A RED
- [x] C2-P RED
- [x] C2-S GREEN — partial reader startup preserves authority, unblocks and joins acquired readers, attaches capture.
- [x] C2-J GREEN — independent joins/dispatcher finish, sole and combined cleanup failures.
- [x] C2-R GREEN — original CLI renderer assertion survives real startup-window SIGINT.
- [x] C2-A GREEN — original authority matrix retained with logical policy time and bounded fixture watchdog.
- [x] C2-P GREEN — owner establishes parent exit/live pipes before unchanged short drain policy and elapsed assertion.

## Startup refinement before final GREEN

The first affected run passed **255 tests / 459 subtests** (13.81 s, exit 0),
including all initial 23 regressions. Inspection then found the same startup
window in the collector's sink dispatcher constructor. That result is not
final verification. Additional hook-interruption and sole/combined dispatcher-
finish regressions cover the existing independent-finalization work order.

**C2-D** extends C2-S to the existing dispatcher thread: protect constructor
startup, release through that dispatcher exactly once, include its construction
inside the collector's guarded startup, and make repeat finish observational.
This is the same authorized collector startup/cleanup target, not a new process
policy. Its eight before/after-start, RuntimeError/KeyboardInterrupt,
with/without cleanup-failure cases were made RED before its implementation:
`.venv/bin/python -m pytest tests/enforcement/test_ci_lifecycle_scheduling.py -k dispatcher_partial_start --tb=short -q`
Exit **1**, **8 failed, 35 deselected**, 0.23 s, no warning summary.

- [x] C2-D RED
- [x] C2-D GREEN — dispatcher owns constructor-failure cleanup once; repeated finish performs no further join.

## Architecture-fingerprint refresh

After the dispatcher refinement the affected run passed **263 tests / 459
subtests**, 13.81 s, exit 0. The first complete gate then exited **1**:
**3 failed, 8091 passed, 13 reviewed skips, 18028 subtests, 11 fork warnings**,
53.52 s. All three failures are the canonical architecture inventory's expected
source AST fingerprint mismatch after the authorized production change, not a
new ownership violation. Lint/format/compile/types passed.

**C2-I** refreshes only that generated fingerprint. Before editing, the
canonical report was regenerated with `canonical_architecture_report(_sources())`;
assertions verified zero architecture errors, identical non-hash report fields,
and exactly one changed source hash (`docker/npm_environment/streaming.py`).
Dependency edges, domain fields, ledgers and enforcement rules are unchanged.
This is generated test data, not a specification amendment or a relaxed check.

- [x] C2-I RED: three full-suite inventory mismatch failures
- [x] C2-I GREEN — canonical generated inventory matches; only authorized source AST hash changed.

Final source-state fingerprints, commands, results and limitations are in
`p6-ci2-repair-results.md`. Fresh complete gates pass on both original affinity
and two CPUs, retaining eight pytest workers. All 43 added regressions pass.
Neither the RED records nor the earlier passing unmodified baseline prove the
repair; final fresh runs do. Hosted rerun/acceptance remains unobserved.

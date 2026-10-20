# Bootstrap ownership follow-up — RED/GREEN ledger

User explicitly requests pending-native-start ownership in streaming.py, both
reader/dispatcher regressions before Python ident assignment, focused/full
checks and refreshed evidence. Specifications and runtime budgets must not
change. HEAD: `c2bc02371f539f7e3998fcf2a3c6225a7db8e576`; the previous CI2 repair
was already present and externally staged. The index is not an instruction to
change it and will be preserved. Earlier CI2 evidence is historical: its
ident-based startup inference is unsafe and is not accepted by this follow-up.

## Work orders defined before implementation

| ID | Target/action | Prerequisite | Acceptance |
| --- | --- | --- | --- |
| B-S | Explicit prepared/no-native/pending-bootstrap/started/completed coordination | authorized startup fix | distinguish committed native acquisition from Python ident publication; settle startup before valid join |
| B-R | Reader rollback via that coordination | B-S | original startup authority and hook, settle readers and independent dispatcher before capture |
| B-D | Dispatcher constructor/finish via same coordination | B-S | no abandoned pending worker, single native join; repeat finish observational |
| B-T | Keep existing startup tests; add real pending-bootstrap regressions | user reproduction | both readers and dispatcher; ordinary/control-flow startup errors; interruption during settlement; raising unblock hook |
| B-V | Refresh only generated source fingerprint as required; fresh full gate and durable final fingerprints | all repairs | no changed spec, skip, assertion or shutdown policy |

RED command:
`.venv/bin/python -m pytest tests/enforcement/test_ci_lifecycle_scheduling.py -k native_worker_pending --tb=short -q`
Exit **1**, **24 failed, 43 deselected**, 0.67 s, no warning summary.
Complete stdout/stderr was captured temporarily; the runner status was retained.
The native bootstrap is gated before `_bootstrap_inner` assigns Python ident;
the original Thread.start runs and is interrupted in its actual started-event
wait. Tests explicitly prove native handle acquisition while ident is None.
Their finally blocks release bootstrap and readers, wait for valid startup,
and join any remaining native worker even on RED assertions.

Implementation must not substitute an unconditional Thread.join (invalid
before the started event). The runtime-specific native-acquisition probe must
be explicit and prepared before native creation, without an ident fallback.
No descriptors or fd-owning streams are newly acquired by this work.

- [x] B-T RED
- [x] B-S GREEN — explicit native-handle/bootstrap-event coordination and state-transition tests.
- [x] B-R GREEN — delayed native bootstrap, interruption hook/secondary failures, capture after settled readers.
- [x] B-D GREEN — pending dispatcher liveness/constructor rollback, native settlement and observational repeat finish.
- [x] B-T GREEN — original 43 cases retained; 24 delayed-bootstrap combinations plus three state/repeat checks.
- [x] B-V GREEN — generated inventory matches; fresh focused and full gates pass on final manifests.

## Intermediate checks / generated fixture

Initial focused implementation run: 250 passed / 459 subtests, exit 0, 13.73 s.
`scripts/check-types` then exited 1 with two unresolved private Thread attributes
(the runtime CPython capabilities are absent from public typeshed). No ignore
or relaxed checker scope was added: a precise CPython startup capability
Protocol/cast now documents that runtime boundary. The fresh type check exits
0. Fresh final focused command:
`.venv/bin/python -m pytest tests/enforcement/test_ci_lifecycle_scheduling.py tests/test_npm_environment_streaming.py tests/test_npm_environment_phase6bc_streaming.py tests/test_retained_tail_phase6.py -n 8 --dist loadfile --tb=short -q`
Exit 0, **250 passed / 459 subtests**, 13.82 s, no warning summary.

Generated inventory refresh was probed before editing: zero canonical report
errors, identical non-hash fields, exactly streaming.py's AST hash changed:
`0a58eb47bfdd7a10405e5d3ca618dcd6fed049b5714dc08c4bce1c5cf6bf5095` →
`c7f7fc6f4227e630e9b110615c0b32d624dce8dc8a9a350baa546d36ff001b9d`.
No architecture graph, ledger, domain field, enforcement rule or spec changed.
Fresh complete checks were then run after the generated-data refresh:
`scripts/check-green` exited 0 (8121 passed, 13 reviewed skips, 18028 subtests,
11 warnings; 53.79 s); `taskset -c 0,1 scripts/check-green` exited 0 with the
same counts (119.31 s), both retaining eight pytest workers. A final focused
rerun on this exact exit state passed 250 tests / 459 subtests in 14.17 s.
240 additional real pending-bootstrap injections passed on two CPUs.

Final exact commands, fingerprints, limitations and warnings are recorded in
`p6-bootstrap-ownership-results.md`. Existing index contents were not changed
by assistant staging operations. No specifications or task boxes were edited.

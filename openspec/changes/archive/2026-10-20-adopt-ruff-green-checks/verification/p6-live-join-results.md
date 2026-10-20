# Actual join interruption — final verification

## Checked state

Base HEAD: `c2bc02371f539f7e3998fcf2a3c6225a7db8e576`, with the previously
present/staged CI2 and bootstrap repairs plus this explicitly requested
follow-up. No assistant staging, unstaging, reset or commit occurred. The
existing index was not treated as permission to change it.

This report supersedes the previous bootstrap report's acceptance of cached
join-attempt results as settlement. Its historical tests did not interrupt a
join while the worker was still live. The native-start coordination remains;
this repair separates its join request/attempt from observed worker completion.
Historical reports have not been rewritten.

Current final **worktree**, not index, fingerprints:

| Manifest | Entries | SHA-256 |
| --- | --- | --- |
| [p6-live-join-source.sha256](p6-live-join-source.sha256) | 407 Python sources | `ac37ceab4e03a28288e8b4d684095fcf8bccd453fcf3086729301d710a2e4d95` |
| [p6-live-join-controls.sha256](p6-live-join-controls.sha256) | 14 controls/docs/generated JSON | `25b3af5d795eb53bf87ef4b615d5503d6ca47c45b46a2ef815f24fce5bc890f6` |

Source paths come from `.venv/bin/python scripts/python_checks.py list`; each
manifest entry is its file's `sha256sum`. Controls include the unchanged gate/
CI entry points and refreshed generated architecture JSON. Final code was not
changed after the checks below. Specification requirements/task boxes, skip
policy, workflow, Dockerfile, process StopPolicy stages/reserves and P7 remain
untouched.

Environment: Linux container, CPython **3.14.7**, Ruff **0.17.1**, ty **0.0.73**,
pytest **9.1.1**, pytest-xdist **3.8.0**. Original affinity 0–31; constrained runs
use CPUs 0,1. Existing Node/npm prerequisites are unchanged. Version probe,
exit 0:
`.venv/bin/python -c 'import sys; from importlib.metadata import version; print(sys.version); print({n: version(n) for n in ("ruff", "ty", "pytest", "pytest-xdist")})'`.
CPython 3.14.8 and repaired hosted CI were not run here. The existing explicit
CPython 3.14 native-handle/bootstrap capability bridge remains runtime-specific.

## Implementation

`_ThreadStartup` now distinguishes:

- cleanup requested with its **one original deadline**;
- one attempted Python Thread.join;
- native worker completion and completion of the retained handle's join;
- accumulated cleanup failures / settled cached observations.

An interrupted or timed-out Python join no longer sets settlement. The same
owner observes native completion using a prepared polling event. Further
interruptions of that observation are recorded, not allowed to abandon the
worker. Once native exit is observed, a zero-time operation completes the same
CPython handle's once-serialized join; no new Thread.join, worker or owner is
created. Repeated settled observations return the same failures without new
join/poll work. An unsettled bounded observation retains its owner and the
original deadline, even if called again with a larger timeout.

Reader cleanup and dispatcher constructor rollback require settlement, so
native completion is observed before propagation. The collector reads retained
buffers and attaches capture only after acquired readers settle. An original
startup/operation exception stays primary; a sole cleanup failure is propagated
after settlement. Additional interrupts remain secondary context. Constructor
rollback now records each observed secondary exception type, not just the first,
without exposing raw SDK/private error contents.

Normal dispatcher finalization **does not become unbounded**: initial join and
subsequent completion observation share the original remaining allowance. A
blocked arbitrary SDK callback can outlive that bound under the pre-existing
unsupported/unsettled callback contract. Its owner remains retained, liveness
is observable, and truncation is reported. Repeated finish does not restart
cleanup or buy a new budget. The tests explicitly release such callbacks in
teardown; no universal cancellation guarantee is claimed.

No raw fd, fd-owning stream or subprocess acquisition/release was added. The
new event is thread coordination, not a descriptor owner; existing filesystem/
process release authorities are unchanged.

## Regression evidence

All **70** prior scheduling cases are retained. **36** new cases cover:

- **32 strict-settlement combinations**: both readers plus dispatcher
  constructor rollback; sole/combined operation failures; OSError or
  KeyboardInterrupt at join entry while the native worker is provably alive;
  with/without a second interrupt during completion observation.
- **4 bounded normal-dispatcher cases**: sole/combined operation failure and
  OSError/KeyboardInterrupt while the SDK callback remains blocked. Logical
  time explicitly spends 20 ms of a 50 ms allowance in the interrupted first
  join: observation may use only the remaining 30 ms. Repeated observations
  cannot renew it. These are fixture parameters, not production budget changes.

The interrupted join wrapper verifies actual worker liveness/native-not-done
**before** raising, without first performing the real join. Combined reader
cases withhold actual data until completion observation releases them. Sole
reader cases gate native exit after logical EOF, allowing collection to
legitimately reach join while the native thread remains live. Both retained
**tail reads** and capture attachment assert acquired workers completed;
late data, original exception identity, independent dispatcher cleanup, all
secondary failure types and observational repeated cleanup are checked.
Teardown releases and joins blocked workers even on failing assertions.

[The ledger](p6-live-join-ledger.md) retains unsuccessful verification:
initial RED **36 failed / 70 deselected** (exit 1), and strengthened constructor
context RED **4 failed / 102 deselected** (exit 1). Neither is reclassified as
successful. The intermediate implementation's passing tests did not establish
final acceptance; fresh final checks follow.

## Fresh final verification

Complete stdout/stderr was captured temporarily for every runner and its actual
exit status preserved. Logs were not filtered for acceptance. Full logs need
not be permanent artifacts; no temporary paths are durable evidence links.

Focused command, exit **0**, **286 passed / 459 subtests**, 13.66 s,
no warning summary:

```bash
.venv/bin/python -m pytest tests/enforcement/test_ci_lifecycle_scheduling.py tests/test_npm_environment_streaming.py tests/test_npm_environment_phase6bc_streaming.py tests/test_retained_tail_phase6.py -n 8 --dist loadfile --tb=short -q
```

| Complete command | Exit | Full pytest result |
| --- | --- | --- |
| `scripts/check-green` | 0 | **8157 passed, 13 reviewed skips, 18028 subtests, 11 warnings**, 53.88 s |
| `taskset -c 0,1 scripts/check-green` | 0 | **8157 passed, 13 reviewed skips, 18028 subtests, 11 warnings**, 118.99 s |

Each gate independently passed lint, format (**407 files already formatted**),
syntax compilation, production type checking and the unchanged full command:
`.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q`.
Standalone `scripts/check-types` also exited 0. Eight pytest workers remain
enabled even with two-CPU affinity.

The +36 cases account for 8121 → 8157 local passes. Reviewed skips remain three
missing docker-dev cases, one missing-sudo case and nine explicit standalone
Docker opt-ins. Counts are not a skip allowance. The **11 warnings** remain
unsuppressed multiprocessing/fork DeprecationWarnings in constructor build
generation/transaction and transaction-locking tests. No skip, xfail, exclusion,
warning suppression or checker-scope reduction was added.

Controlled repeats, exit **0**, **180 cases passed on two CPUs**:

```bash
taskset -c 0,1 .venv/bin/python - <<'PY'
import sys
sys.dont_write_bytecode = True
from itertools import product
import pytest
from tests.enforcement.test_ci_lifecycle_scheduling import (
    test_live_join_interruption_settles_before_buffers_and_capture,
    test_normal_dispatcher_interrupted_join_keeps_original_bound,
)
cases = [(target, primary) for target in ('npm-stdout-reader', 'npm-stderr-reader')
         for primary in (None, RuntimeError, KeyboardInterrupt)]
cases += [('npm-sink-dispatcher', primary) for primary in (RuntimeError, KeyboardInterrupt)]
count = 0
for _ in range(5):
    for (target, primary), error, interrupt in product(cases, (OSError, KeyboardInterrupt), (False, True)):
        with pytest.MonkeyPatch.context() as patch:
            test_live_join_interruption_settles_before_buffers_and_capture(target, primary, error, interrupt, patch)
        count += 1
    for primary, error in product((None, RuntimeError), (OSError, KeyboardInterrupt)):
        with pytest.MonkeyPatch.context() as patch:
            test_normal_dispatcher_interrupted_join_keeps_original_bound(primary, error, patch)
        count += 1
print(f'{count} live-join and bounded-callback cases passed on two CPUs')
PY
```

## Generated inventory / integrity

Only streaming.py's generated AST hash changes from the previous bootstrap
exit: `c7f7fc6f4227e630e9b110615c0b32d624dce8dc8a9a350baa546d36ff001b9d` →
`d37378d9d6bd20e88bdad5c958daef850683e33f58f67be4f674021146c7ac18`.
No graph, ownership ledger, domain field, enforcement rule or specification was
changed. Final canonical probe exited **0**:

```bash
.venv/bin/python - <<'PY'
import json
from pathlib import Path
from scripts.python_checks import run_captured
from tests.enforcement.green_fixtures import read_text
from tests.enforcement.process_convergence import canonical_architecture_report
from tests.enforcement.test_filesystem_convergence import _sources
path = 'tests/fixtures/process-architecture-inventory.json'
base = run_captured(['git', 'show', 'c2bc02371f539f7e3998fcf2a3c6225a7db8e576:' + path], root=Path.cwd())
assert base.clean and base.returncode == 0
old = json.loads(base.stdout)
new = canonical_architecture_report(_sources())
assert new == json.loads(read_text(Path(path)))
assert new['errors'] == []
assert {k: v for k, v in old.items() if k != 'source_ast_sha256'} == {k: v for k, v in new.items() if k != 'source_ast_sha256'}
assert [p for p in old['source_ast_sha256'] if old['source_ast_sha256'][p] != new['source_ast_sha256'][p]] == ['docker/npm_environment/streaming.py']
print('Canonical inventory matches; all non-hash fields unchanged; zero architecture errors')
PY
```

`git diff --check`, `git diff --cached --check`, and both manifest verification
commands exited 0. Dockerfile is unchanged; Hadolint is inapplicable, not
represented as executed. No repaired hosted result, P6 hosted acceptance
(6.11/6.25), P7 work or specification amendment is claimed.

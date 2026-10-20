# Pending native startup ownership — final verification

## Checked state / scope

HEAD: `c2bc02371f539f7e3998fcf2a3c6225a7db8e576`, plus the previously present
CI2 repair and this explicitly requested bootstrap follow-up. The previous
repair was externally staged at entry; no assistant staging, unstaging, reset
or commit occurred. This follow-up modifies streaming.py, its scheduling
regressions and one generated architecture hash. Specification requirements,
task boxes, workflow, skip policy and production process budgets are unchanged.

This report **supersedes the CI2 report's acceptance of ident-based startup
inference**. Those historical passing checks did not cover acquired native
workers before Python ident publication and do not prove startup ownership.
The existing historical reports were not rewritten.

Final worktree fingerprints (not a claim about index contents):

| Manifest | Entries | Manifest SHA-256 |
| --- | --- | --- |
| [p6-bootstrap-source.sha256](p6-bootstrap-source.sha256) | 407 Python sources | `bb27abccfcdadf275d74045ddae400e15e05e05e5c6dab78985a83234cb60f6a` |
| [p6-bootstrap-controls.sha256](p6-bootstrap-controls.sha256) | 14 controls/docs/generated JSON | `d4e62bc7aa3c4f829c554472ba88f6ab4ffebf56797b2fcd924594998ac38f28` |

The source list comes from `.venv/bin/python scripts/python_checks.py list`;
manifest entries are `sha256sum` of each listed file. Controls include the
unchanged shared entry points and refreshed generated architecture JSON.
Manifest generation/probes exited 0. All final verification below covers this
state; no code changed after those checks.

Environment: Linux; CPython **3.14.7**, Ruff **0.17.1**, ty **0.0.73**, pytest
**9.1.1**, pytest-xdist **3.8.0**; existing Node **v24.18.0** / npm **11.16.0**.
Original affinity 0–31; constrained commands use CPUs 0,1. Interpreter/package
versions were checked using `.venv/bin/python -c 'import sys; from importlib.metadata import version; print(sys.version); print({n: version(n) for n in ("ruff", "ty", "pytest", "pytest-xdist")})'`
(exit 0). CPython 3.14.8 / hosted execution was not run here.

## Implementation

`_ThreadStartup` explicitly distinguishes prepared, failed-before-native,
native-bootstrap-pending, Python-started and native-completed states. It
prepares references to the native handle and bootstrap event **before** start.
Native acquisition is established by the committed native handle, never by
`Thread.ident is None` or `Thread.is_alive()`.

The bridge deliberately uses CPython 3.14's `_os_thread_handle` / `_started`
capabilities, isolated behind precise Protocols. Public Thread observations
cannot distinguish this startup window. Missing capabilities fail during
preparation rather than falling back to the unsafe heuristic. The narrow cast
documents private runtime capabilities omitted by public typeshed; no type
ignore, checker-scope reduction or lint exemption was added. Other interpreter
implementations are not claimed supported by this runtime bridge.

Cleanup waits for pending bootstrap readiness before valid Python Thread.join.
An interruption while observing bootstrap is retained as secondary failure and
observation resumes, without restarting native startup or cleanup. If a native
worker completed without publishing Python startup, its committed native handle
is joined instead; Python's invalid pre-start join is never attempted. Native
join attempts/results are cached, so repeated cleanup does not repeat joining.

Readers use the same coordinator for start and cleanup. The original exception
remains authoritative, the interruption hook runs, independent reader/dispatcher
cleanup occurs, then retained capture is attached and the original exception
re-raised. Dispatcher constructor rollback uses the same coordination and
settles a pending acquired worker. Dispatcher liveness now includes pending
native bootstrap. Repeated finish remains observational.

Normal dispatcher callback/drain allowances are unchanged. Failed construction
has not published the dispatcher/admitted SDK chunks through its API: rollback
must await its bootstrap/stop worker before propagating rather than return a
pending acquisition. This is not a new subprocess StopPolicy budget. The
pre-existing unsupported indefinitely non-returning SDK callback contract is
unchanged. No raw fd, fd-owning stream, subprocess or additional OS descriptor
acquisition/release was introduced; existing filesystem/process owners remain.

## Regression coverage

All **43** original before-native / after-start-return and prior cleanup tests
are retained. **27** cases are added:

- **24 real native bootstrap cases**: each reader and dispatcher; RuntimeError
  or KeyboardInterrupt in Thread.start's actual started-event wait; with/without
  a second interruption during settlement; with/without an unblock-hook failure.
  `_bootstrap_inner` is gated before Python ident assignment. Tests explicitly
  prove native handle acquisition with ident still None and pending coordinator
  state. Before attaching capture they require every acquired native worker
  complete, no live worker, one join per acquired thread and independent
  dispatcher finalization. Original exception identity and selected retained
  data are checked. Repeat finish must cause no new join.
- **Three state/repeat checks**: no native acquisition, native completion without
  Python-start publication (explicit handle stub), and real started-to-completed
  worker transition with observational repeated join. The early-native-completion
  stub is not represented as a real failing CPython bootstrap experiment.

The regression file therefore contains **70 cases**. Test teardown also settles
real pending native workers on failed assertions; it does not infer absence of
acquisition from Python ident or rely on daemon/GC exit.

[The ledger](p6-bootstrap-ownership-ledger.md) records the RED run (**24 failed**,
43 deselected, exit 1), intermediate focused checks and an unsuccessful type
check (two private-attribute diagnostics) before the explicit capability bridge.
Those failures remain failures, not accepted verification.

## Fresh final checks

Complete stdout/stderr was captured temporarily for every runner and its actual
exit status preserved. No filtered pipeline was used for acceptance. Permanent
full logs are unnecessary; no temporary paths are durable evidence links.

Final focused command, exit **0**, **250 passed / 459 subtests**, 14.17 s,
no warning summary:

```bash
.venv/bin/python -m pytest tests/enforcement/test_ci_lifecycle_scheduling.py tests/test_npm_environment_streaming.py tests/test_npm_environment_phase6bc_streaming.py tests/test_retained_tail_phase6.py -n 8 --dist loadfile --tb=short -q
```

| Complete gate command | Exit | Full pytest result |
| --- | --- | --- |
| `scripts/check-green` | 0 | **8121 passed, 13 reviewed skips, 18028 subtests, 11 warnings**, 53.79 s |
| `taskset -c 0,1 scripts/check-green` | 0 | **8121 passed, 13 reviewed skips, 18028 subtests, 11 warnings**, 119.31 s |

Both gates independently passed lint, format (**407 files already formatted**),
syntax compilation, production type checking and the unchanged complete command:
`.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q`.
The standalone fresh `scripts/check-types` also exited 0. Eight workers remain
enabled even for the two-CPU gate.

The +27 cases explain the local increase from 8094 to 8121 passes. Existing
reviewed skips remain three missing docker-dev cases, one missing-sudo case and
nine explicit standalone Docker opt-ins; the count is not a skip allowance.
All **11 warnings** remain unsuppressed multiprocessing/fork DeprecationWarnings
in constructor build generation/transaction and transaction-locking tests. No
new skip, xfail, xpass, exclusion or warning suppression was introduced.

Controlled-repeat command, exit **0**, **240 cases passed on two CPUs**:

```bash
taskset -c 0,1 .venv/bin/python - <<'PY'
import sys
sys.dont_write_bytecode = True
from itertools import product
import pytest
from tests.enforcement.test_ci_lifecycle_scheduling import test_native_worker_pending_before_ident_is_settled_before_capture
count = 0
for _ in range(10):
    for target, error, wait_interrupt, hook_error in product(
        ('npm-stdout-reader', 'npm-stderr-reader', 'npm-sink-dispatcher'),
        (RuntimeError, KeyboardInterrupt), (False, True), (False, True)
    ):
        with pytest.MonkeyPatch.context() as patch:
            test_native_worker_pending_before_ident_is_settled_before_capture(
                target, error, wait_interrupt, hook_error, patch
            )
        count += 1
print(f'{count} delayed-native-bootstrap cases passed on two CPUs')
PY
```

## Generated inventory / final integrity

Only streaming.py's generated AST fingerprint changed from the prior CI2 exit:
`0a58eb47bfdd7a10405e5d3ca618dcd6fed049b5714dc08c4bce1c5cf6bf5095` →
`c7f7fc6f4227e630e9b110615c0b32d624dce8dc8a9a350baa546d36ff001b9d`.
No dependency graph, ownership ledger, domain fields, enforcement policy or
specification requirement was changed. Final canonical probe, exit **0**:

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
changed = [p for p in old['source_ast_sha256'] if old['source_ast_sha256'][p] != new['source_ast_sha256'][p]]
assert changed == ['docker/npm_environment/streaming.py'], changed
print('Canonical fixture matches; only streaming.py source hash differs; no architecture errors')
PY
```

`git diff --check`, `git diff --cached --check` and both manifest verification
commands exited 0. Dockerfile is unchanged, so Hadolint is inapplicable, not
represented as performed. No repaired remote CI success, final P6 hosted
acceptance (6.11/6.25), P7 work or specification amendment is claimed.

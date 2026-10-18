# Second CI repair — final local verification

## Source state and authorization

Base revision: `c2bc02371f539f7e3998fcf2a3c6225a7db8e576`. The user explicitly
authorized production collector startup/interruption cleanup and the related
fixture determinization after the diagnosis. P7 and specification text remain
untouched. No staging, unstaging, reset or commit was performed.

Current checked state:

| Manifest | Entries | SHA-256 of manifest |
| --- | --- | --- |
| [p6-ci2-source.sha256](p6-ci2-source.sha256) | 407 Python sources | `ecef70c8026e53687c837930f8bd1f706d4391d12c7d10325ff21bdc6cfff295` |
| [p6-ci2-controls.sha256](p6-ci2-controls.sha256) | 14 controls/docs/generated inventory | `002fc7d0fb6a7147dca13bb5d3cea589dc9ec53550d7d229499d5e55a6b91695` |

The source manifest uses the full `scripts/python_checks.py list` inventory;
the controls include the unchanged gate/CI entry points and the refreshed
architecture JSON fixture. Hashes were collected after all source edits.
Earlier reports, including the successful pre-repair two-CPU baseline, describe
historical states only. See [p6-ci2-diagnosis.md](p6-ci2-diagnosis.md) for remote
provenance and [p6-ci2-repair-ledger.md](p6-ci2-repair-ledger.md) for all failed
RED/intermediate runs and work-order refinements.

Environment: Linux container; CPython **3.14.7**, Ruff **0.17.1**, ty **0.0.73**,
pytest **9.1.1**, pytest-xdist **3.8.0**, Node **v24.18.0**, npm **11.16.0**.
Original affinity **0–31**; constrained checks explicitly use CPUs **0,1**.
Python/package versions were checked with `.venv/bin/python --version` and
`.venv/bin/python -c 'from importlib.metadata import version; print({name: version(name) for name in ("ruff", "ty", "pytest", "pytest-xdist")})'`.
All version probes exited 0. Hosted Python **3.14.8** and privilege provisioning
were not reproduced here; no repaired hosted-run success is claimed.

## Implementation and maintained contracts

Only `docker/npm_environment/streaming.py` changes in production:

- Collector reader startup is inside the existing protected interruption
  path. First/second-reader failure, including start-then-raise, unblocks through
  the existing hook, joins acquired readers and attaches retained diagnostics
  before propagating the **same** operation exception.
- Every independent reader join and dispatcher finalization is attempted once.
  Ordinary cleanup failures/interruption cannot replace existing operation
  authority. A sole cleanup failure is raised, never silently converted to
  success. Unstarted threads are not joined; start-then-raise detection follows
  the existing OwnedProcess reader-start seam's `Thread.ident` convention.
- SinkDispatcher owns its worker even if its constructor raises after start.
  Constructor-failure cleanup goes through that dispatcher; repeated finish
  does not retry native join. Dispatcher construction is also within the
  collector's protected startup/capture path. Cleanup notes expose exception
  types rather than unredacted SDK error contents at the constructor boundary.
- No raw descriptor or fd-owning stream acquisition, adoption, wrapper,
  subprocess launch or release was introduced. Existing process/stream owners
  and descriptor release authorities remain unchanged. The new work concerns
  thread startup/join ordering over existing borrowed reader callbacks.

Fixture changes preserve the original assertions:

- `test_process_compatibility_action_gate.py` retains its complete 27-case
  authority/error matrix and independent-start, context, sole-close and repeat
  cleanup assertions. These pure fake-child cases now use logical policy time,
  so OS scheduling does not select which injected failures are exercised.
  A five-second event watchdog guards fixture progress; it is not a changed
  production shutdown allowance. Real deadline/budget tests remain wall-clock.
- The real parent/descendant test establishes parent exit **0** and both stream
  outcomes **PENDING** through the original owner's observation seam before
  activating supervision. Its unchanged **0.1 s** policy and **<0.5 s** elapsed
  assertion measure the intended post-parent pipe-drain behavior, not Python
  interpreter startup. Child/pipes are not independently waited, reaped or
  closed. The existing original owner performs all local release.
- The original CLI failed-renderer test still demands one report attempt,
  explicit presentation ownership, no fallback/retry, stopped presentation
  worker and released build lock. It now also passes when real SIGINT lands
  during a controlled second-reader startup pause.

`tests/enforcement/test_ci_lifecycle_scheduling.py` adds **43** regressions:
24 partial-reader startup/unblock combinations; 8 dispatcher partial-start /
cleanup / repeat-finish cases; 4 sole/combined join failures; 4 sole/combined
dispatcher-finalization failures; and 3 original-boundary scheduling cases.
No skip, xfail, exclusion, warning suppression or weaker expected outcome was
added. No stop stage, total shutdown budget, local reserve or collection
parallelism changed. The arbitrary indefinitely non-returning SDK sink remains
unsupported as already documented; this repair does not claim to cancel it.

The generated `process-architecture-inventory.json` changes **only** the AST
hash for streaming.py:
`ca498d2fedf0f4564fe0136cf9ab74a77bbc3b985423205f808a2250fc032a7e` →
`0a58eb47bfdd7a10405e5d3ca618dcd6fed049b5714dc08c4bce1c5cf6bf5095`.
The canonical report has zero errors and identical non-hash fields to base:
no dependency edges, domain fields, ownership ledger or enforcement rules were
changed to accept the repair.

## Fresh final verification

Complete stdout/stderr was captured temporarily for every runner, preserving
its actual exit status. Failures are recorded in the ledger and not relabelled
successful. Full logs are not required permanent artifacts; no temporary paths
are used as durable evidence links.

Affected command (exit **0**, **263 passed, 459 subtests**, 13.81 s, no warning
summary):

```bash
.venv/bin/python -m pytest tests/enforcement/test_ci_lifecycle_scheduling.py tests/test_retained_tail_phase6.py tests/test_process_compatibility_action_gate.py tests/test_npm_environment_phase4_lifecycle.py tests/test_npm_environment_streaming.py tests/test_npm_environment_phase6bc_streaming.py -n 8 --dist loadfile --tb=short -q
```

After the generated fixture refresh, both **complete** gates passed on the
same final source state:

| Command | Exit | Full pytest result |
| --- | --- | --- |
| `scripts/check-green` | 0 | **8094 passed, 13 reviewed skips, 18028 subtests, 11 warnings**, 53.49 s |
| `taskset -c 0,1 scripts/check-green` | 0 | **8094 passed, 13 reviewed skips, 18028 subtests, 11 warnings**, 119.78 s |

Each gate independently completed all mandatory commands successfully:
`scripts/check-python-lint`, `scripts/check-python-format` (**407 files already
formatted**), `scripts/check-python-compile`, `scripts/check-types`, and the
unchanged full suite:

```bash
.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q
```

The 43 added cases explain 8051 → 8094 local passes. All 13 skips retain the
existing individually reviewed applicability: three missing docker-dev cases,
one missing-sudo case, nine explicit standalone Docker opt-ins. Counts are not
a skip allowance. The 11 warnings remain unsuppressed CPython multiprocessing
fork DeprecationWarnings in constructor build generation/transaction and
transaction-locking tests. No unexpected skip/xfail/xpass occurred.

Additional repeated controlled-fault command, exit **0**, **60 cases passed**
on two CPUs (20 each action/parent/real CLI SIGINT):

```bash
taskset -c 0,1 .venv/bin/python - <<'PY'
import sys
sys.dont_write_bytecode = True
from tests.enforcement.test_ci_lifecycle_scheduling import (
    test_action_authority_fixture_does_not_charge_scheduler_pause_to_stage_cap,
    test_parent_exit_fixture_establishes_exit_before_short_drain_budget,
    test_startup_sigint_retains_report_through_the_original_cli_boundary,
)
for _ in range(20):
    test_action_authority_fixture_does_not_charge_scheduler_pause_to_stage_cap()
    test_parent_exit_fixture_establishes_exit_before_short_drain_budget()
    test_startup_sigint_retains_report_through_the_original_cli_boundary()
print('60 controlled-delay cases passed on two CPUs: 20 each action, parent, CLI SIGINT')
PY
```

Canonical architecture probe, exit **0**, verifies exact generated fixture and
one changed source hash without relaxing any report fields:

```bash
.venv/bin/python - <<'PY'
import json
from pathlib import Path
from scripts.python_checks import run_captured
from tests.enforcement.green_fixtures import read_text
from tests.enforcement.process_convergence import canonical_architecture_report
from tests.enforcement.test_filesystem_convergence import _sources
path = 'tests/fixtures/process-architecture-inventory.json'
base = run_captured(['git', 'show', 'HEAD:' + path], root=Path.cwd())
assert base.clean and base.returncode == 0
old = json.loads(base.stdout)
new = canonical_architecture_report(_sources())
assert new == json.loads(read_text(Path(path)))
assert new['errors'] == []
assert {k: v for k, v in old.items() if k != 'source_ast_sha256'} == {k: v for k, v in new.items() if k != 'source_ast_sha256'}
changed = [p for p in old['source_ast_sha256'] if old['source_ast_sha256'][p] != new['source_ast_sha256'][p]]
assert changed == ['docker/npm_environment/streaming.py'], changed
print('Canonical inventory matches; only streaming.py AST hash differs from base; no architecture errors')
PY
```

`git diff --check` and `git diff --cached --check` exited **0**. The Dockerfile
is unchanged, so Hadolint is inapplicable, not represented as executed/passed.

## Remaining acceptance

This report establishes current **local** repair verification, not remote
acceptance. Tasks **6.11 and 6.25 remain unchecked** pending a successful pushed
hosted provisioning/full gate for this exact repair state. The supplied failed
artifact belongs to the base revision, not to these edits. No P7 work or final
OpenSpec change acceptance is claimed.

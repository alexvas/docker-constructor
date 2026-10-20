# CI run 39340202214 — fixture scheduling and truthful observations

## Checked source and limits

Base: `5671c503399c86a152b3ed5423d924bb8f3dc2f3`, initially clean.
Final Python source is the base plus these files (SHA-256):

| File | SHA-256 |
| --- | --- |
| `tests/test_process_captured_action_faults.py` | `be4dadf43bc4044a9e236e3c29dceaeaf27255f6f8d2a0a79022a06f4153c168` |
| `tests/test_npm_environment_phase4_lifecycle.py` | `78ca7c832bbae06c2e18abf038e1550c68edaed6560883cf4cd108337b5e0f95` |
| `tests/npm_http_logging_research.py` | `ad9e671e3fb157a81a72f0efde31549a03528149bf4d725c106a3d250babf670` |
| `tests/test_npm_http_logging_research.py` | `9c4e64badad7c8667220850ab2de9f61169f1cb4339f21c415f621b6564cdd0c` |
| `tests/enforcement/test_ci_lifecycle_scheduling_followup.py` | `0db95fceb10306df32474cab8bf1ddec20e52e1db6ebafcd71a4e1915261fc9d` |

Fingerprint command (exit 0): `sha256sum tests/test_process_captured_action_faults.py tests/test_npm_environment_phase4_lifecycle.py tests/npm_http_logging_research.py tests/test_npm_http_logging_research.py tests/enforcement/test_ci_lifecycle_scheduling_followup.py`.

Environment: Linux container, CPython **3.14.7**, Ruff **0.17.1**, ty **0.0.73**,
pytest **9.1.1**, pytest-xdist **3.8.0**, Node **24.18.0**, npm **11.16.0**.
Version commands (exit 0):
`.venv/bin/python -c 'import sys; from importlib.metadata import version; print(sys.version); print({n: version(n) for n in ("ruff", "ty", "pytest", "pytest-xdist")})'`,
`node --version`, `npm --version`.

Reported artifact:
https://github.com/alexvas/docker-constructor/actions/runs/39340202214/artifacts/12228786289
The supplied excerpt reports **3 failed, 8198 passed, 9 skips, 18027 subtests,
11 warnings**, CPython **3.14.8**, 208.48 s. Command
`gh api repos/alexvas/docker-constructor/actions/artifacts/12228786289` exited
**4**, requiring GitHub CLI authentication. The artifact was not downloaded;
diagnosis uses the supplied excerpt, source inspection and local reproduction.
Hosted acceptance and verification on CPython 3.14.8 remain outstanding.

Production code, operation/cleanup budgets, npm command/environment, skips,
architecture inventory, specifications and task completion boxes are unchanged.
No staging, unstaging, reset or commit commands were executed. No P7 or
architecture migration is included. Historical evidence applies only to its
recorded source state, not this correction.

## Causes and corrections

### Captured helper retention

The fixture expected actual helper readers to exist after a 100 ms stop stage.
A delayed worker may instead observe its already-expired stage and never launch
a helper; bounded main shutdown can also return before helper startup. Neither
outcome establishes the retained-helper precondition this fixture tests.

The main policy continuation now uses logical time in this one test; before
advancing that time, a pause seam requires real helper reader entry and the
captured action's initial completion. The action's stage-time observation uses
the same logical clock. Helper process lifecycle time remains real. A five-second
fixture-only phase observation watchdog permits establishment of these real
startup preconditions; it is not a production shutdown-budget change. The
existing 0.2 s grace, 0.1 s stage and 0.08 s local reserve remain unchanged.

All existing assertions remain: actual delegated helper ownership, incomplete
main and helper results, both children exited, main pipes released, live helper
stdout retained, no premature close, subsequent late release error, settlement,
immutable earlier results/callback diagnostics and no surviving workers. Main
and helper remain distinct nested sole owners; no independent reap or release
was added.

### Deadline policy versus host scheduling

The old wall-clock assertion charged launch and thread scheduling to a 120 ms
bound. Hosted elapsed time was 355 ms, which does not establish a policy delay.
The fixture now advances a local logical clock by the supervisor's requested
waits. It still requires timeout, termination and complete cleanup, requires
actual supervisor waits, rejects any wait exceeding the 20 ms deadline and
retains the original total-wait bound below 120 ms. A separate mutation injects
a 200 ms poll request and must fail that policy assertion. OS scheduling is not
represented as a production real-time guarantee.

### npm live-output measurement

A final npm error burst can be read only after npm exits. The existing harness
correctly returned `confirmed_live_output=False`; requiring every measured
notice/http run to be live was an invalid universal assertion, not a lost-output
or production streaming failure. Making that flag always true would falsify
the research. This repair explicitly changes that test expectation rather than
claiming to retain the impossible scheduling guarantee.

Research now records the return code sampled at the first read. Each real npm
measurement must have output and a flag consistent with that sampled state:
live observations precede the recorded exit observation, while post-exit
observations retain the actual final return code. All ten scenario/loglevel
runs, outcome/cache/network/redaction/volume checks and existing 15 s bounds
remain. The measurement schema gains `first_observation_returncode`; historical
research results are not rewritten or presented as fresh measurements.

A positive control makes a finite child wait for a directory marker created
only after reader liveness sampling, proving actual live observation. The
negative control gates reading on the runner's already-published return code,
not a 200 ms timer or an independent reap. It proves exact output and correct
post-exit classification. Mutations forcing a false flag in either direction
must fail these controls. The research acquisition/release code is unchanged;
new seams delegate reads and observe borrowed child state, without adding a
signal/wait/reap or descriptor-release authority.

## RED and intermediate evidence

Initial controlled reproduction, before any fixture correction:

`.venv/bin/python -m pytest tests/enforcement/test_ci_lifecycle_scheduling_followup.py --tb=short -q`

Exit **1**, **3 failed**, 6.30 s. Cases inject a 300 ms helper-worker delay,
a 300 ms pre-launch delay, and post-exit reads for all real npm measurements.
The last case reproduces ten failed live-output subtests, including timeout/notice.

After the first two corrections, `-k 'not research'` on that file exited **0**:
**2 passed, 1 deselected**, 0.87 s. An intermediate four-file focused run passed
**102 tests and 20 subtests**, 11.97 s, but lint and format checks were independently
unsuccessful (exit **1** each: one import-order issue and three unformatted files).
Those issues were corrected before final verification; that intermediate state
was not considered green.

The final regression source was also run against the three original test
functions loaded from the base into memory, without modifying the worktree or
index. It again exited **1**: **3 failed, 3 deselected**, 6.01 s. Original function
ASTs were read through `tests.enforcement.green_fixtures.read_text`, compiled
with their module globals and substituted with `mock.patch.object`; pytest args
were `tests/enforcement/test_ci_lifecycle_scheduling_followup.py -k 'startup_before_stage or excludes_unrelated or preserves_truthful' --tb=short -q`.
These are expected RED results, not successful verification of the originals.

## Fresh final-source verification

Complete stdout/stderr was captured temporarily with runner exit status
preserved. Full logs need not be committed and are not used as durable links.

| Exact command | Exit | Result |
| --- | --- | --- |
| `.venv/bin/ruff format tests/enforcement/test_ci_lifecycle_scheduling_followup.py tests/test_npm_environment_phase4_lifecycle.py tests/test_process_captured_action_faults.py tests/npm_http_logging_research.py tests/test_npm_http_logging_research.py` | 0 | Three files formatted, two unchanged |
| `.venv/bin/ruff check tests/enforcement/test_ci_lifecycle_scheduling_followup.py tests/test_npm_environment_phase4_lifecycle.py tests/test_process_captured_action_faults.py tests/npm_http_logging_research.py tests/test_npm_http_logging_research.py` | 0 | All checks passed |
| `.venv/bin/python -m pytest tests/enforcement/test_ci_lifecycle_scheduling_followup.py tests/test_npm_environment_phase4_lifecycle.py tests/test_process_captured_action_faults.py tests/test_npm_http_logging_research.py -n 8 --dist loadfile --tb=short -q` | 0 | **105 passed, 20 subtests**, 12.02 s |
| `scripts/check-green` | 0 | **8203 passed, 13 skips, 18028 subtests, 11 warnings**, 55.03 s |
| `taskset -c 0,1 scripts/check-green` | 0 | **8203 passed, 13 skips, 18028 subtests, 11 warnings**, 123.61 s |
| `git diff --check` | 0 | No whitespace errors |
| `git diff --cached --check` | 0 | No staged whitespace errors |

Both full gates independently passed lint, formatting, compilation and types,
and ran the unchanged canonical suite with all eight workers:
`.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q`.
No new skips, xfails, exclusions, warning suppression, production-budget increases
or failure-status filtering. The 13 local skips are three docker-dev prerequisites,
one missing sudo and nine Docker opt-ins; hosted CI has four fewer privilege
skips. All 11 multiprocessing/fork DeprecationWarnings remain visible. Dockerfile
is unchanged; Hadolint was not run and is not applicable to this test-only repair.

Additional two-CPU final-source repetitions exited **0**: **53 controlled cases**
(10 delayed helpers, 10 delayed launches, 10 excessive-poll mutations, 20 false
liveness mutations and three complete post-exit research contracts). The latter
execute **36 real npm launches**, including six cache seeds. Exact command:

```bash
taskset -c 0,1 .venv/bin/python - <<'PY'
from tests.enforcement.test_ci_lifecycle_scheduling_followup import (
    test_captured_helper_fixture_establishes_startup_before_stage_expiry as helper,
    test_deadline_fixture_excludes_unrelated_launch_scheduling as launch,
    test_deadline_fixture_rejects_excessive_supervisor_poll_wait as poll,
    test_research_controls_reject_false_liveness_claims as claims,
    test_research_contract_preserves_truthful_post_exit_observations as research,
)
for _ in range(10):
    helper()
    launch()
    poll()
    claims('test_output_read_while_child_waits_for_observation_is_confirmed_live', False)
    claims('test_output_read_after_child_exit_is_not_confirmed_live', True)
for _ in range(3):
    research()
print('53 controlled cases passed on two CPUs: 10 delayed helpers, 10 delayed launches, 10 excessive-poll mutations, 20 false-liveness mutations, 3 complete post-exit research contracts (36 npm launches)')
PY
```

These checks cover the reproduced assumptions and explicit counterexamples,
not absence of every scheduling race. Fresh successful hosted verification is
still required for Phase 6 acceptance.

# Second hosted failure — diagnosis, no implementation acceptance

## Checked state / provenance

Checked source revision: `c2bc02371f539f7e3998fcf2a3c6225a7db8e576`.
Working tree was clean before this investigation. No runtime/test/workflow
source was edited; this report is the only intended new file. No staging,
unstaging, reset or commit was performed.

Artifact metadata command:
`curl -L --fail --silent --show-error https://api.github.com/repos/alexvas/docker-constructor/actions/artifacts/12140643714 -o /tmp/p6-ci2-artifact.json`
exited **0**. Metadata identifies run **39139709510**, the checked revision,
artifact `green-check-diagnostics`, size 5146 bytes, unexpired, digest
`sha256:dd2b49d46435fdcd558ae073770db8307be6350672cb51cb59efd9613eec56b0`.
The ZIP request
`curl --fail --silent --show-error https://api.github.com/repos/alexvas/docker-constructor/actions/artifacts/12140643714/zip -o /tmp/p6-ci2-artifact.zip`
exited **22**, HTTP **401**. The artifact contents were **not** downloaded.
Remote failure traces/counts below are from the user's supplied excerpt.

Remote: Python **3.14.8**, **3 failed, 8052 passed, 9 reviewed skips,
18028 subtests passed, 11 fork DeprecationWarnings**, 246.42 s. This is a failed
run, regardless of successful provisioning or previously green local runs.

## Fresh baseline checks (unchanged source)

Local environment: Linux, CPython **3.14.7**, existing project venv; original
CPU affinity 0–31. Complete stdout/stderr was captured temporarily and runner
exit statuses preserved. Full logs are not durable evidence dependencies.

1. `.venv/bin/python -m pytest tests/test_retained_tail_phase6.py::TestInterruptionPresentationOwnership::test_failed_renderer_never_falls_back_or_retries tests/test_process_compatibility_action_gate.py tests/test_npm_environment_phase4_lifecycle.py::TestLifecyclePrimitives::test_parent_exit_with_descendant_holding_pipes_is_bounded -n 8 --dist loadfile --tb=short -q`
   — exit **0**, **30 passed**, 0.87 s, no warning summary.
2. `taskset -c 0,1 scripts/check-green` — exit **0**, lint/format/compile/types
   successful; full unchanged pytest command ran with **eight** workers on two
   CPUs: **8051 passed, 13 reviewed skips, 18028 subtests passed, 11 warnings**,
   116.40 s. Local privilege prerequisites remain unavailable (four additional
   reviewed skips versus hosted execution). The warnings are unchanged
   multiprocessing/fork DeprecationWarnings. No warning/skip policy changed.

This two-CPU run did not reproduce the remote failures by load alone. It does
not invalidate them, prove Python 3.14.8 success or establish a repair.

## Findings

### Renderer failure exposes a production startup interruption gap

`collect_streams` in `docker/npm_environment/streaming.py` starts both reader
threads **before** its interruption-handler `try` block. The existing fixture
sends SIGINT after the first reader serves data plus an arbitrary 50 ms sleep.
A scheduling pause during second-reader startup lets SIGINT escape before
capture attachment and normal reader-unblocking/join cleanup.

A controlled pause in that exact startup window reproduces the **original
renderer test's `AssertionError: 1 != 0`**. A separate direct helper probe also
confirmed the resulting KeyboardInterrupt has neither `retained_capture` nor
`diagnostic_tail`. `_interruption_host_failure` deliberately returns None when
that capture is absent, explaining why no final report reaches the renderer.
This is not evidence of a renderer retry/fallback defect. The failed test must
not be made green simply by avoiding the collector's exposed startup window.

Exact original-test reproduction command (exit **0** means the diagnostic
successfully asserted the expected failure, **not** that the test passed):

```bash
.venv/bin/python - <<'PY'
import sys
sys.dont_write_bytecode = True
import threading
import unittest
from unittest import mock
from tests import test_retained_tail_phase6 as fixture
start, reader = threading.Thread.start, fixture._ServeOnceThenBlock
releases, threads = [], []

def observed_reader(data, released, served):
    releases.append(released)
    return reader(data, released, served)

def delayed_start(thread):
    start(thread)
    if thread.name in ('npm-stdout-reader', 'npm-stderr-reader'):
        threads.append(thread)
    if thread.name == 'npm-stderr-reader':
        threading.Event().wait(2)

result = unittest.TestResult()
try:
    with mock.patch.object(fixture, '_ServeOnceThenBlock', observed_reader), mock.patch.object(threading.Thread, 'start', delayed_start):
        fixture.TestInterruptionPresentationOwnership('test_failed_renderer_never_falls_back_or_retries').run(result)
    assert not result.errors and len(result.failures) == 1, (result.errors, result.failures)
    assert 'AssertionError: 1 != 0' in result.failures[0][1]
    print('Original renderer test reproduced:', result.failures[0][1].splitlines()[-1])
finally:
    for release in releases:
        release.set()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()
PY
```

The diagnostic explicitly releases and joins the fixture reader threads; it
does not rely on their daemon status or garbage collection. No new OS
file/stream acquisition was added by this injection.

### Compatibility-action assertion assumes synchronous cleanup

The fake-child test uses total grace **80 ms**, two **10 ms** stages and local
reserve **25 ms**, yet asserts `shutdown(...).complete` immediately. Scheduling
can return an incomplete snapshot. Waiting for retained cleanup alone is not a
complete fixture fix: expired stages may never initiate, so all candidate
errors/action-start expectations also need deterministic scheduling semantics,
without removing independent-cleanup or authority assertions.

Exploratory injection of a 150 ms pause inside fixture `Action.start` reproduced
`complete=False` with the sole RuntimeError('context poll'); the local phase was
WAIT, whereas the remote excerpt reports COORDINATION. This demonstrates the
same incomplete-snapshot assumption, **not** an exact phase-for-phase remote
replay or a demonstrated production process defect. The existing test's finally
block observed the retained owner cleanup. No shutdown budget was modified.

### Parent/descendant fixture includes interpreter startup in its deadline

The real-process test requests `LifecyclePolicy(0.1)`. The adapter passes that
as the operation deadline; it is not merely a post-parent-exit pipe-drain
budget. The Python parent must start an interpreter, import subprocess and
spawn another interpreter before exiting. If startup takes longer than 100 ms,
SIGTERM / returncode **-15** is a legitimate operation-deadline outcome, not
proof of failed descendant-pipe cleanup.

Exploratory injection of a 200 ms delay at the beginning of the existing parent
program reproduced the original test's exact `AssertionError: -15 != 0`.
The factory still ran exclusively under OwnedProcess.launch; no independent
child release/wait/signal was introduced. This demonstrates the invalid startup
assumption, not definitive proof of the remote scheduler's exact sequence.
A repair must establish the intended exited-parent precondition separately
from the short drain deadline, and retain sole ownership/bounded cleanup;
accepting -15 or arbitrarily enlarging the budget would weaken the test.

## Scope / next decision

The startup gap is in **production collector lifecycle code**, beyond the
previously authorized fixture/provisioning repairs. Implementation is paused
for explicit authorization to repair collector startup/interruption cleanup
and make the related fixtures deterministic. Production changes require their
own startup/interruption/independent-cleanup regressions and fresh full checks.
No specification files or task boxes changed. P6 hosted acceptance (6.11/6.25)
remains open; P7 is untouched. Earlier repair evidence remains historical.

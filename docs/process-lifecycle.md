# Prepared process lifecycle

`docker.process.OwnedProcess` owns **one direct child**, captured binary
stdout/stderr, and its internally created readers. Dependency direction is
consumer → `docker.process` → explicit `docker.filesystem` submodules.
Filesystem stream owners supply terminal release, not process coordination.
See [filesystem-lifecycle.md](filesystem-lifecycle.md) for stream/raw-fd rules
and [npm-environment-assembler.md](npm-environment-assembler.md) for domain policy.
Existing npm clients are not yet migrated by the generic-owner phase.

## Public API

Import `OwnedProcess`, `ProcessResult`, `CleanupResult`, and `CleanupIncomplete`
from `docker.process`. The additive policy surface is `StopPolicy`, `StopStage`,
`StopContext`, `ManagedAction` (a protocol), and immutable `ActionResult`.

- `OwnedProcess.launch(argv, *, grace_seconds=1.0, deadline_seconds=None,
  capture_limit=65536, factory=subprocess.Popen, popen_kwargs=None,
  on_launched=None, stop_policy=None)` validates configuration and prepares ownership and
  continuation capacity **before** calling the factory. `argv` is a nonempty
  string sequence; grace and optional deadline are finite positive seconds.
  `capture_limit` bounds each retained byte tail. Launch options may set cwd,
  env, etc., but cannot override stdout/stderr, enable text mode, or add stdin.
  `factory` is an injectable binary-pipe factory, not existing-process adoption.
  Missing pipes fail setup while the present sibling remains accounted for.
- `capture()` drains both streams concurrently and returns bounded tails,
  returncode, `timed_out`, and a cleanup snapshot in immutable `ProcessResult`.
- `stream(callback)` supplies `(name, bytes)` chunks in per-stream order;
  cross-stream order depends on scheduling. The bounded spool preserves early
  chunks and applies backpressure. Callbacks run on the calling thread, receive
  no pipes, and ordinary callback exceptions are isolated (at most 32 retained
  in `callback_errors`). Interruptions initiate shared cleanup. Callbacks must
  return promptly; arbitrary user callback execution cannot be forcibly bounded.
- `shutdown(*, primary=None)` requests coordinated cancellation/termination and
  returns `CleanupResult`. It is an outcome API, not an exception-raising API.
  Repeated/concurrent/reentrant requests share one shutdown budget and ledger.
- `cleanup_status` observes an immutable snapshot. `wait_cleanup(timeout)` waits
  a finite nonnegative number of seconds and returns the current snapshot;
  it does not initiate shutdown, retry release, or restart termination budgets.
- `returncode` is borrowed process observation, not shutdown/release authority.
- Context exit uses shutdown and raises its authoritative failure. Incomplete
  exit without an active failure raises `CleanupIncomplete`. Incomplete exit
  with an active failure preserves precedence. Errors expose `.cleanup`, the
  retained owner with bounded observation access, including failed launches
  that cannot return an owner. Capture/stream similarly raise known cleanup
  errors or controlled incomplete cleanup instead of reporting false success.

`CleanupResult.complete` means reaped child, confirmed stopped readers, terminal
pipe release attempts, settled managed actions and their owned local resources,
and published winning outcomes. `actions` retains immutable `ActionResult`
snapshots in configured stage order; stages with no acquired action resources
are settled. This is local-resource/request settlement, not proof that any
externally controlled work stopped. It does **not** promise
successful closes; inspect `error` and `failures`. `shutdown_incomplete` is the
opposite state. A delivered snapshot never changes, even if later observation
returns a completed snapshot containing late failures.

## Ownership and states

The lifecycle is prepared → launched → running → shutting down → cleanup
complete, or shutdown incomplete → cleanup complete.

Labels, stream slots, locks/events, bounded output storage, reader tracking, and
a ready cleanup continuation are prepared before launch. Private escrow binding
accounts for both returned pipes before fallible transfer/configuration. Failed
transfer never reconstructs a constructor or closes a rejected candidate.
Each pipe has exactly one `OwnedStream`; its `fileno()` is borrowed only for
nonblocking reads, never adopted as another integer-fd owner.

Readers use cancellation-aware nonblocking descriptor reads and explicit stop
barriers. A timed-out join, process exit, or `is_alive()` inference does not
permit releasing a pipe still used by a reader. Joins, terminate/kill/reap,
and safe independent release are accumulated under shared precedence:
original/first interruption → unexpected defect → original ordinary operation
failure → ordinary cleanup failure. Release `OSError` is ordinary; join defects
are not silently classified ordinary. A later transfer interruption outranks
an earlier ordinary transfer failure by exact identity.

The grace budget bounds coordinated shutdown waiting; the ready continuation
also supervises the optional deadline. Omitting `stop_policy` retains the default
local reader-cancel/terminate/kill/reap behavior and its existing total bound. Descendant-held write ends are drained
for a bounded grace after direct-child exit; the mechanism does not kill those
descendants. Descriptor-less injected streams may need close to unblock read;
this compatibility seam is not proof of real-descriptor safety.

When a reader or release cannot finish within the caller budget, the prepared
per-process continuation retains the **same** owners, reader references, and
winning outcomes. Independently safe cleanup continues. No GC authority or
global ownership registry is used. Once a reader really stops, its designated
owner receives one release attempt. Observation never retries a failed close.
A reader that never stops retains ownership indefinitely and is **not cleaned
up**. Python cannot forcibly stop an arbitrary injected blocking `read()` or
bound arbitrary injected `close()` execution.

Late failures are available only in subsequent cleanup snapshots: they do not
mutate delivered results/errors, attach late notes, or invoke domain callbacks.
Generic internal exceptions are not sanitized; consumer adapters must bound and
sanitize public diagnostics. Docker removal, npm errors, redaction, staging,
cache and publication remain outside this module.

## Launch-selected stop policies

`StopPolicy(stages, local_cleanup_seconds, drain=False)` uses a tuple of
`StopStage(seconds, action=None, wait_child=False, run_on_normal=False,
local_stop_before=False)`. There is no prescribed stage count or domain-specific
soft/hard role. Every cap and the local-cleanup reserve must be finite, positive,
and together fit `grace_seconds`; invalid plans fail before the main factory.
The one absolute monotonic shutdown deadline begins on the first request.
Repeated callers do not restart it. A phase clock begins **before** initiation;
action execution/observation and optional natural child waiting share its cap.
Failure advances immediately; expiry advances to the next stage or local
fallback. Early completion never enlarges another stage's cap. The local reserve
is unavailable to stages and remains available for coordinator-owned reader
cancellation, joins, terminate/kill/reap and safe release. `local_stop_before`
permits the existing local-stop → managed cleanup → local wait/reap ordering,
without handing signal or wait authority to the action.

A managed action implements four **nonblocking** methods:

- `prepare()` establishes readiness before acquisition, returns `None`, or raises.
  A partially prepared action is already accounted for and receives cleanup.
- `start(context)` initiates the stage at most once. The frozen `StopContext`
  contains only `reason`, a returncode observation and the absolute `deadline`;
  no raw main process, pipe, signal, wait/reap or release authority.
- `observe()` returns `ActionResult(done, cleanup_complete, error=None)`. Request
  settlement and owned-resource settlement are distinct. Errors retain exact
  identity under shared precedence; observations are cumulative (do not generate
  fresh errors on each poll). Each action retains its own winning error across
  `start`, `cancel` and observation, independently of an aggregate operation
  primary. Successful settlement cannot clear a prior action failure. Observer
  diagnostics are bounded per precedence class, allowing later escalation.
- `cancel(deadline)` requests settlement of locally owned action resources once,
  including prepared-but-unstarted actions. It must not wait. It is not permission
  to initiate a new domain control request or release main pipes.

Explicit shutdown, deadline, context exit, setup and reader failure all select
this same launch-time plan. Normal capture/context finalization skips stop-only
stages; `run_on_normal=True` selects required finalization even after child exit.
Only the generic coordinator signals/waits/reaps the main child. Do not subclass
`OwnedProcess` to replace shutdown or build a second coordinator.

An auxiliary child must use its **own default-policy OwnedProcess**, with one
internal stream owner per pipe. Its execution deadline plus local grace must
fit the remaining parent/stage allowance, computed from `context.deadline`;
no fresh budget or recursive external policy. Retain its observation handle if
setup/cleanup remains incomplete. Domain commands and outcome interpretation
remain outside the generic mechanism.

With `drain=True`, operation cancellation suppresses user callbacks promptly
but readers keep draining bounded tails until the configured stage/wait boundary.
They bypass the bounded callback spool after the stop request, so a full queue
or slow consumer cannot stall draining. Reader cancellation follows that boundary;
failed readers do not prevent sibling cleanup. The actual reader-stop barrier
still controls descriptor release. Without draining, readers cancel promptly.

All protocol boundaries must return promptly. Arbitrary blocking Python policy
callbacks, factories and injected I/O cannot be forcibly stopped; they are not a
supported bounded-action implementation. The protocol deliberately does not
accept an arbitrary whole-shutdown callback. After delivery the continuation
only observes already-started actions and finishes safe local release: it starts
no new policy stage, domain request or user-facing callback, resets no budget and
never retries cancellation or close. Late errors appear only in new observation
snapshots; delivered outcomes and exception diagnostics remain unchanged.

## Executable examples

Capture bounded tails with context cleanup:

```python
import sys
from docker.process import OwnedProcess

with OwnedProcess.launch(
    [sys.executable, "-c", "print('hello')"], capture_limit=64
) as child:
    result = child.capture()
assert result.stdout == b"hello\n"
assert result.returncode == 0 and result.cleanup.complete
```

Stream bytes without transferring release authority:

```python
import sys
from docker.process import OwnedProcess

chunks = []
with OwnedProcess.launch([sys.executable, "-c", "print('bytes')"]) as child:
    result = child.stream(lambda name, chunk: chunks.append((name, chunk)))
assert b"".join(chunk for name, chunk in chunks if name == "stdout") == b"bytes\n"
assert result.cleanup.complete
```

Observe bounded cleanup without retrying it:

```python
import sys
from docker.process import OwnedProcess

child = OwnedProcess.launch([sys.executable, "-c", "import time; time.sleep(10)"])
initial = child.shutdown()
observed = child.wait_cleanup(1.0)
assert observed.complete
assert child.shutdown() is observed
assert initial.complete or initial.shutdown_incomplete
```

For a controlled incomplete context or failed launch, retain the exception's
`.cleanup` and inspect `wait_cleanup(timeout)`. A timeout returns another
incomplete snapshot; it is not permission to close a borrowed pipe yourself.
Fault tests in `tests/test_process_lifecycle.py` use deterministic stalled
reader barriers, release those barriers in `finally`, and verify late outcomes
without claiming that permanent reader retention is completed cleanup.

A separately owned auxiliary operation with a bounded main-child drain:

```python
import sys
import time
from docker.process import OwnedProcess, StopPolicy, StopStage, ActionResult

class Auxiliary:
    def prepare(self):
        self.child = None
    def start(self, context):
        remaining = context.deadline - time.monotonic()
        if remaining <= 0:
            raise OSError("action allowance expired")
        # Both helper execution and cleanup fit the parent's stage allowance.
        self.child = OwnedProcess.launch(
            [sys.executable, "-c", "pass"],
            grace_seconds=remaining / 4, deadline_seconds=remaining / 4,
        )  # default local policy, never the parent's external policy
    def observe(self):
        if self.child is None:
            return ActionResult(True, True)
        status = self.child.cleanup_status
        return ActionResult(status.complete, status.complete, status.error)
    def cancel(self, deadline):
        # Nonblocking: the already-configured helper deadline drives cleanup.
        pass

action = Auxiliary()
policy = StopPolicy((StopStage(.3, action, wait_child=True),), .2, drain=True)
child = OwnedProcess.launch(
    [sys.executable, "-c", "import time;time.sleep(.08);print('tail',flush=True)"],
    grace_seconds=.6, stop_policy=policy,
)
status = child.shutdown()
assert child.wait_cleanup(1).complete
assert status.actions[0].cleanup_complete
assert action.child.wait_cleanup(1).complete
```

Reject invalid budgets and attempts to obtain raw ownership before acquisition:

```python
from dataclasses import FrozenInstanceError
from docker.process import OwnedProcess, StopPolicy, StopStage, StopContext

called = []
def forbidden(*args, **kwargs):
    called.append(True)
    raise AssertionError("must not launch")

for policy in (StopPolicy((StopStage(.9),), .2),
               StopPolicy((StopStage(.1, object()),), .2)):
    try:
        OwnedProcess.launch(["unused"], grace_seconds=1, stop_policy=policy,
                            factory=forbidden)
    except (ValueError, TypeError):
        pass
    else:
        raise AssertionError("invalid policy accepted")
assert not called
context = StopContext("cancel", None, 1.0)
assert not hasattr(context, "process") and not hasattr(context, "terminate")
try:
    context.deadline = 999.0
except FrozenInstanceError:
    pass
else:
    raise AssertionError("policy can restart its deadline")
```

## Exclusions

No process trees/groups, stdin ownership, PTY, arbitrary external reader
threads, existing-process adoption, runtime-installer migration, raw-fileno
release fallback, domain cleanup policy, or unconditional eventual-release
guarantee. The public `OwnedStream` constructor remains unchanged; private
preparation/binding is not a new filesystem factory or process API.

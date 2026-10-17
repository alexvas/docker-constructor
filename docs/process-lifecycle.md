# Process lifecycle ownership

`docker.process.OwnedProcess` launches one direct child, owns one
`docker.filesystem.streams.OwnedStream` per captured pipe, and coordinates
readers, deadlines and bounded cleanup. Consumers import explicit modules;
filesystem ownership never imports process or domain policy.

The examples below execute against the supported API through
`tests/enforcement/`. They use Python subprocesses and require no
Docker installation.

## Capture a command

Use `launch` to acquire the child and `capture` to collect bounded stdout/stderr.
The context manager retains cleanup responsibility on every exit path.

```python
import sys

from docker.process import OwnedProcess

with OwnedProcess.launch([sys.executable, "-c", "print('hello')"]) as owner:
    result = owner.capture()

assert result.stdout == b"hello\n"
assert result.stderr == b""
assert result.returncode == 0
assert result.lifecycle.complete
assert result.lifecycle.failures == ()
```

Each owner accepts exactly one validated `capture()` or
`stream(callback, *, on_stream_end=None)` attempt. A second, mixed or racing
attempt raises RuntimeError immediately, including after failure or
interruption. Invalid callback arguments do not consume the claim. There is no
late subscription, output replay or cleanup restart.

Launch prepares synchronization, stream slots and continuation readiness before
acquisition. Do not adopt an already-running Popen, reconstruct owners, close
borrowed pipes or adopt a pipe's fileno. Supported factories provide both
captured streams; missing streams are controlled setup failures, not empty
success.

## Stream output

Chunk callbacks receive `(ProcessStream, bytes)` on the collecting thread,
outside state locks. They must return promptly. An optional end callback receives
`(ProcessStream, StreamOutcome)` once per acquired stream, after its assigned
chunks. No callback begins after collection delivers its result or interruption.

```python
import sys

from docker.process import OwnedProcess, ProcessStream, StreamOutcome

received = bytearray()
endings = {}

def chunk(stream, data):
    if stream is ProcessStream.STDOUT:
        received.extend(data)

def end(stream, outcome):
    endings[stream] = outcome

with OwnedProcess.launch([sys.executable, "-c", "print('hello')"]) as owner:
    result = owner.stream(chunk, on_stream_end=end)

assert bytes(received) == b"hello\n"
assert endings[ProcessStream.STDOUT] is StreamOutcome.EOF
assert endings[ProcessStream.STDERR] is StreamOutcome.EOF
assert result.lifecycle.complete and not result.lifecycle.failures
```

Each stream transitions from PENDING to one terminal outcome:

- **EOF** — clean completion; a sibling failure does not revise it.
- **READ_FAILED** — an actual read failure, not a callback error.
- **ABORTED** — collection discontinued; this does not prove the reader has
  stopped or that releasing its descriptor is safe.

Clean EOF is reported promptly. Completion before callback registration or
racing dispatch cannot lose or duplicate the terminal attempt. Domain collectors
own incremental redaction and separate clean-EOF versus abort finalization;
never replay generic raw output tails through a diagnostic sink.

Ordinary callback exceptions are diagnostic-only. An optional launch `reporter`
receives the exception plus typed `stream` and `callback_kind` keyword context,
without eager stringification. Domains supply bounded secret/URL sanitization.
Without a reporter, only a fixed safe message is logged. Ordinary reporter
failure invokes an independent fixed-safe fallback once; ordinary fallback
failure is best effort, never recursive. These paths do not store callback
errors or change operation authority. Non-Exception control flow from a
callback, reporter or fallback is not diagnostic-only: it propagates after
cleanup.

## Interpret results and failures

The frozen records have exactly these fields:

- `FailureInfo(error: BaseException, kind: FailureKind, phase: FailurePhase,
  stream: ProcessStream | None = None)`.
- `ActionResult(done: bool, cleanup_complete: bool,
  failures: tuple[FailureInfo, ...] = ())`.
- `LifecycleResult(complete: bool, failures: tuple[FailureInfo, ...] = ())`.
- `ProcessResult(stdout: bytes, stderr: bytes, returncode: int | None,
  timed_out: bool, lifecycle: LifecycleResult, stream_outcomes=())`.
  `stream_outcomes` is an immutable tuple of `(ProcessStream, StreamOutcome)`
  pairs in STDOUT/STDERR order.

These dimensions are independent:

- `returncode` describes the child exit, not cleanup success. A nonzero exit is
  not itself a lifecycle failure.
- `timed_out` records that the execution deadline fired; it does not necessarily
  identify the authoritative failure.
- `lifecycle.complete` means ownership cleanup has settled, not that the command
  or every close succeeded.
- `lifecycle.failures` is the single aggregate failure sequence, with selected
  authority first and each exact exception object retained once.

Ordinary failures and unexpected defects return ProcessResult rather than
raising from collection. Adapters decide whether to project or raise them. The
precedence is first interruption, unexpected defect, original ordinary operation
failure, then ordinary cleanup failure. Generic results never grant independent
release authority.

A custom factory can fail without acquiring a child. The prepared owner still
provides the same result contract:

```python
from docker.process import FailureKind, FailurePhase, OwnedProcess

def unavailable(*args, **kwargs):
    raise OSError("process service unavailable")

with OwnedProcess.launch(["worker"], factory=unavailable) as owner:
    result = owner.capture()

failure = result.lifecycle.failures[0]
assert failure.kind is FailureKind.ORDINARY
assert failure.phase is FailurePhase.SETUP
assert result.returncode is None
assert result.lifecycle.complete
```

Validation/preparation may raise before a usable prepared owner exists. After
that boundary, ordinary launch/setup failures return the same failed owner;
its sole collection returns available data. A failed factory has no pipes and
no fabricated child. Context exit does not rethrow failures already returned
as values.

FailureKind has ORDINARY, UNEXPECTED and INTERRUPTION. FailurePhase has SETUP,
READ, JOIN, RELEASE, WAIT, ACTION, CALLBACK, REPORTER and COORDINATION. Context
validation follows these rules:

| phase | stream context |
| --- | --- |
| READ, CALLBACK, REPORTER | required |
| SETUP, JOIN, RELEASE | a stream or None |
| WAIT, ACTION, COORDINATION | None |

Release OSError is ordinary; reader join uses `ordinary=()`, so join OSError is
unexpected. InterruptedError follows OSError policy, not automatic control-flow
classification. Invalid enum, phase/stream or kind/control-flow pairings are
rejected. Returned classifications are explicit data, not inferred from the
exception's Python type.

Immutability applies to record fields, bytes and tuples, not the internals of
shared exception objects. There are no generic CleanupResult aliases, redundant
error/failure_info fields, ProcessResult.cleanup, callback-error stores or
exception-attached output snapshots.

## Handle interruption

KeyboardInterrupt, SystemExit, GeneratorExit and other non-Exception
BaseException propagate after bounded independent cleanup, preserving the exact
first-interruption object. Keep access to the owner for cleanup observation;
there is no generic interruption output snapshot or `owner.result` accessor.

```python
import sys

from docker.process import OwnedProcess

control = KeyboardInterrupt("caller interruption")

def interrupt(stream, data):
    raise control

with OwnedProcess.launch([sys.executable, "-c", "print('ready')"]) as owner:
    try:
        owner.stream(interrupt)
    except KeyboardInterrupt as caught:
        assert caught is control
    else:
        raise AssertionError("interruption was not propagated")

assert owner.wait_cleanup(.1).complete
```

Domains retain their own bounded sanitized capture for CLI/host reporting;
that capture is independent of generic ownership and exception propagation.

## Shutdown and observe cleanup

`shutdown()` requests bounded cleanup without collecting output. Repeated or
concurrent shutdown calls share the same request, ownership and clocks. They do
not restart work or obtain new budgets.

```python
import sys

from docker.process import OwnedProcess

with OwnedProcess.launch(
    [sys.executable, "-c", "import time; time.sleep(60)"],
    grace_seconds=1,
) as owner:
    status = owner.shutdown()
    if not status.complete:
        status = owner.wait_cleanup(1)

assert status.complete
assert owner.cleanup_status.complete
```

`cleanup_status`, `wait_cleanup(timeout)` and `shutdown()` expose immutable
LifecycleResult observations. An incomplete result retains the same owners and
prepared continuation. Waiting is bounded; it does not retry a close or restart
shutdown. Waiting longer is observation, not a second collection attempt.

Completion requires child reap, actual reader-stop barriers, terminal release
attempts and publication of winning outcomes—not successful closes. A timed
join or child exit alone is not permission to release a descriptor beneath a
live reader. If a reader never stops, its owner remains retained; completion is
not promised. Descriptor-less compatibility streams may need sole-owner release
to unblock; they never justify fd-reuse risks.

Late failures appear in new read-only cleanup observations. They do not mutate
delivered results/errors, replay output, create domain diagnostics or invoke
new callback subscriptions. On context exit without collection, controlled
incomplete cleanup carries observation access; an active body failure uses the
shared precedence rules.

## Advanced: managed actions and budgets

Use a launch-selected StopPolicy when cleanup includes domain work in addition
to local child termination. It consists of StopStage caps, a
`local_cleanup_seconds` reserve and optional draining. Every cap/reserve must be
finite, positive and fit the total `grace_seconds`.

Each phase clock starts before action initiation. Action work and configured
child waiting share that cap and the remaining total deadline. Early completion
does not enlarge another cap. Local-stop-before-action plans preserve adapter
ordering; configured draining delays reader cancellation only within its cap.

The action protocol separates initiation, observation and resource settlement:

- `prepare()`, `start(context)` and `cancel(deadline)` return None.
- Only `observe()` returns ActionResult.
- `done` means the request settled, not that it succeeded.
- `cleanup_complete` describes helper resource settlement independently.
- Failures can be known before `done`; `done` can be true while cleanup remains
  incomplete.

Protocol boundaries must be nonblocking. Actions cannot override shutdown,
control the raw main child or release its pipes. Auxiliary children use separate
default-policy OwnedProcess owners; their execution and cleanup must fit the
parent's remaining allowance.

Transfer helper failures explicitly: preserve each FailureInfo.error and kind,
and rebase context to ACTION/None. Never infer helper kind from Python type,
later mutable status or text. The helper retains its original context; the
parent contains aggregate failures once, not nested ActionResults. Bare errors
and missing classifications are not supported action transport. Local cleanup
completion does not prove external work succeeded.

Docker/container status interpretation remains domain policy; see the
[npm adapter guide](npm-environment-assembler.md).

## Executable contract scenarios

Keep fault injection in tests rather than in usage examples. Concrete scenarios:

- [Helper failure handoff](../tests/test_process_strict_actions_and_reporting.py#L122 "test_helper_snapshot_and_parent_context_are_orthogonal"): preserve identity/kind and rebase parent context.
- [Rejected second collection](../tests/test_process_single_collection.py#L63 "test_reject_repeated_and_mixed_collection_without_work"): no callbacks, replay or repeated cleanup.
- [Immutable late observation](../tests/test_process_single_collection.py#L147 "test_late_cleanup_is_a_new_observation_not_a_changed_collection_value"): retain the owner without changing the delivered value.
- [Terminal notification race](../tests/test_process_strict_actions_and_reporting.py#L405 "test_completion_racing_dispatch_has_once_only_end_attempt"): chunks precede the once-only end attempt.
- [Reporter/default/fallback](../tests/test_process_strict_actions_and_reporting.py#L304 "test_reporting_defaults_are_fixed_safe_and_nonrecursive"): fixed-safe, nonrecursive diagnostics.
- [Rejected legacy transport](../tests/test_process_orthogonal_results.py#L141 "test_no_legacy_field_conversion"): no conversion of obsolete result fields.

## Exclusions and ownership rules

No process-tree/group management, stdin, PTY, arbitrary external reader ownership,
existing-process adoption, Docker/npm/redaction/staging/publication authority or
runtime-installer dependency is introduced. Never subclass to replace shutdown,
retry ambiguous close, reconstruct owners or assign independent stage budgets.
The public standalone npm collect_streams byte-collector is distinct: subprocess
adapters use OwnedProcess and domain-only incremental collectors instead.

See [filesystem-lifecycle.md](filesystem-lifecycle.md) for single-owner release and
[npm-environment-assembler.md](npm-environment-assembler.md) for domain policy.

# Filesystem descriptor and stream lifecycle

This guide is the developer contract for acquiring, transferring, and
releasing operating-system descriptors in `docker/`. It exists so a new
descriptor-owning code path selects an existing lifecycle wrapper instead of
inventing another close/retry state machine. The rules are enforced by the
production-tree convergence gates in
`tests/test_descriptor_close_convergence.py`,
`tests/test_filesystem_convergence.py`, and
`tests/test_filesystem_lifecycle_docs.py`.

The foundation lives in `docker/filesystem/`. Import the explicit submodule
you use (`docker.filesystem.descriptors`,
`docker.filesystem.operations`, `docker.filesystem.cleanup`, or
`docker.filesystem.streams`); never import the aggregate `docker.filesystem`
package, and never re-export its names from the package initializer. The
foundation is domain-neutral: it derives no paths, performs no recursion,
takes no locks, publishes nothing, and defines no npm, versioning, or
transaction authority.

## Ownership checklist

Before writing a descriptor-owning branch, answer every question below.

1. **What did I acquire?**
   - A raw integer descriptor (`os.open`, `os.dup`, `os.pipe`,
     `tempfile.mkstemp`, or a factory returning an fd) → adopt it
     **immediately** into `OwnedDescriptor` with constructor inputs prepared
     before the acquisition.
   - An already fd-owning stream (`subprocess.Popen(..., PIPE)` stdout/stderr,
     an opened file object) → adopt it into `OwnedStream`. Do **not** also
     adopt its `fileno()`.
   - A directory capability → use `DirectoryDescriptor.open_secure_path` /
     `.adopt` / the child methods, which validate owner, type, and no-follow
     before any mutation.
2. **Do I need a Python file object for a raw fd?** Use
   `os.fdopen(owner.fd, ..., closefd=False)`. The `OwnedDescriptor` stays the
   sole descriptor-release authority. Never use the default
   `closefd=True`, and never let a file object own an fd that an owner still
   holds.
3. **Who releases it?** Exactly one owner. `OwnedDescriptor.close()`,
   `OwnedStream.close()`, and directory-capability `close()` are terminal and
   at-most-once: the release claim is made before the underlying close, so a
   repeat, concurrent, or reentrant call issues no second close.
4. **Is there an active primary failure?** Release with
   `CleanupFailures(primary)` so the active operation/domain failure stays
   authoritative and an ordinary close failure is attached as secondary
   diagnostic context. Without a primary, a sole close failure propagates.
5. **Are there several independent resources?** Submit each owner's `close`
   to one `CleanupFailures` accumulator; every independent release is
   attempted once even when an earlier one fails.
6. **Am I handing off parent authority to a child?** Track the child before
   the potentially failing parent release, make the parent terminal before
   its close, and never retry an ambiguously failed parent close.
7. **Am I transferring ownership out?** `OwnedDescriptor.detach()` returns the
   raw fd and marks the owner transferred. The receiver becomes responsible;
   a detached owner issues no close.
8. **Do readers share the resource?** Stop and join descriptor readers before
   the final owner release. Never close a raw descriptor number while a
   worker can still use it.

## Direct-close categories

A textual `close` call in production is permitted only in one of three
categories:

- **primitive** — `PosixDescriptorOps.close` / `PosixFileOps.close` delegate
  straight to the raw POSIX close;
- **shared-owner internal release** — `OwnedDescriptor.close()`,
  `OwnedStream.close()`, or an equivalent shared owner invokes its injected
  close only after making ownership terminal (the stream owner also
  synchronizes the release claim);
- **ownership handoff** — a direct close whose releasing parent is already
  terminal and whose opened child is already tracked for failure cleanup.

Every other consumer-owned explicit or implicit release (including
`os.fdopen(..., closefd=True)` and an unadopted `tempfile.mkstemp()` fd) must
delegate to a shared owner or a protected cleanup accumulator.

For prepared generic subprocess coordination, see
[process-lifecycle.md](process-lifecycle.md). That mechanism is separate from
filesystem stream ownership; its consumer adapters are a later migration.

## Stream transfer contract

`OwnedStream(stream, label=...)` validates the non-empty string label and
callable `close` interface and prepares synchronization **before transfer**.
Invalid inputs or lock-construction failure leave the caller-owned stream
untouched: rejection never attempts close, even if close would fail or interrupt.
The caller retains responsibility after rejected adoption. Borrowed access
never grants release authority; successful adoption still uses the same
terminal synchronized claim and outside-lock release.

## Subprocess stream ownership

`subprocess.Popen(..., stdout=PIPE, stderr=PIPE)` creates fd-owning streams.
Adopt each returned file object into one `OwnedStream` right after launch,
before callbacks, reader startup, or supervisor startup. Retain the same owner
pair for the entire process lifecycle and reuse it from every cleanup branch;
reconstructing an owner per cleanup resets terminal state and is forbidden.

- Release is synchronized: exactly one caller invokes the underlying close,
  even when it fails or is interrupted.
- Reader coordination stays in the subprocess layer (`execution.py`,
  `lifecycle.py`, and only the necessary `streaming.py` seams). Use
  cancellation-aware reads and a coordinated stop/join before the final owner
  release; never close a raw fd under a live reader.
- Keep process terminate/remove/reap ordering, deadline/grace bounds, output
  tails, decoding, and bounded URL/secret redaction in the domain layer.
  Foundation diagnostics do not sanitize domain notes.
- Missing streams and descriptor-less injected test streams follow the same
  owner lifecycle; a descriptor-less double is not evidence that production
  fd reuse is safe.

## Executable examples

The following examples execute against the supported API. They are extracted
and run by `tests/test_filesystem_lifecycle_docs.py`.

```python
import os
import tempfile

from docker.filesystem.descriptors import OwnedDescriptor
from docker.filesystem.operations import PosixDescriptorOps

path = tempfile.mktemp()
fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
owner = OwnedDescriptor(PosixDescriptorOps(), fd, label="temporary file")
try:
    os.write(owner.fd, b"payload")
finally:
    owner.close()
os.unlink(path)
```

```python
import os
import tempfile

from docker.filesystem.descriptors import OwnedDescriptor
from docker.filesystem.operations import PosixDescriptorOps

path = tempfile.mktemp()
fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
owner = OwnedDescriptor(PosixDescriptorOps(), fd, label="temporary file")
with os.fdopen(owner.fd, "wb", closefd=False) as handle:
    handle.write(b"payload")
    handle.flush()
owner.close()
os.unlink(path)
```

```python
import io

from docker.filesystem.streams import OwnedStream

captured_stdout = io.BytesIO(b"npm output")
owner = OwnedStream(captured_stdout, label="stdout")
assert owner.stream.read() == b"npm output"
owner.close()
assert owner.released
owner.close()
assert captured_stdout.closed
```

```python
import io

from docker.filesystem.streams import OwnedStream

candidate = io.BytesIO(b"caller owned")
try:
    OwnedStream(candidate, label="")
except ValueError:
    pass
else:
    raise AssertionError("invalid label accepted")
assert not candidate.closed
assert candidate.getvalue() == b"caller owned"
# The caller still owns the rejected candidate and can adopt it correctly.
with OwnedStream(candidate, label="validated") as owner:
    assert owner.stream.read() == b"caller owned"
assert candidate.closed
```

```python
import tempfile

from docker.filesystem.descriptors import DirectoryDescriptor
from docker.filesystem.operations import PosixDescriptorOps

with tempfile.TemporaryDirectory() as directory:
    with DirectoryDescriptor.open_secure_path(
        PosixDescriptorOps(), directory, label="workspace"
    ) as root:
        assert root.list_names() == ()
```

```python
from docker.filesystem.cleanup import CleanupFailures
from docker.filesystem.streams import OwnedStream


class _Stream:
    def __init__(self, fail: bool) -> None:
        self.fail = fail
        self.closed = False

    def close(self) -> None:
        self.closed = True
        if self.fail:
            raise OSError("injected close failure")


stdout = _Stream(False)
stderr = _Stream(True)
cleanup = CleanupFailures(None)
cleanup.run(OwnedStream(stdout, label="stdout").close, ordinary=(OSError,))
cleanup.run(OwnedStream(stderr, label="stderr").close, ordinary=(OSError,))
failure = cleanup.complete()
assert isinstance(failure, OSError)
assert stdout.closed and stderr.closed
```

## Anti-patterns (forbidden)

The patterns below are rejected by the convergence gate. They are shown only
to make the prohibition concrete.

- **Implicit `fdopen` ownership.** `os.fdopen(fd, "w")` (omitted or
  `closefd=True`) transfers descriptor release to the file object. Use
  `os.fdopen(owner.fd, "w", closefd=False)` with a live owner instead.
- **Double raw-fileno ownership.** Adopting `pipe.fileno()` into
  `OwnedDescriptor` while the file object still owns the fd creates two
  release authorities. Adopt the stream itself into `OwnedStream`.
- **Repeated owner reconstruction.** Calling
  `OwnedStream(proc.stdout, label="stdout").close()` from each cleanup hook
  resets the terminal claim and can close twice. Create one owner after launch
  and reuse it.
- **Borrowed release.** `owner.stream.close()` (or closing a stream borrowed
  from an owner) bypasses the owner's synchronized terminal claim. Only the
  owner may release.

## Failure precedence summary

`CleanupFailures` classifies every outcome in this order: a process-control
interruption (`KeyboardInterrupt`/cancellation) is authoritative; otherwise
an unexpected defect; otherwise the original primary `Exception`; otherwise
the first ordinary close failure. Process-control objects retain their
identity and are never wrapped. Independent cleanup actions always run, and
their ordinary failures are attached once as bounded secondary context.

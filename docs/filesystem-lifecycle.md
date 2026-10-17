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
[process-lifecycle.md](process-lifecycle.md). Process coordination builds on
filesystem stream ownership; it does not introduce another release authority.

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
For supported subprocess execution, use `docker.process.OwnedProcess`: it
prepares one `OwnedStream` per pipe and retains those same owners through
setup, collection and cleanup. Consumers must not adopt the pipes again.

- Release is synchronized: exactly one caller invokes the underlying close,
  even when it fails or is interrupted.
- `OwnedProcess` coordinates cancellation-aware readers, local signals/reap,
  deadline/grace budgets and stop/join before final stream release. Never close
  a raw fd under a live reader.
- Domain adapters select cleanup actions and their ordering, decode output,
  retain diagnostic tails and sanitize URL/secret-bearing notes. Filesystem
  ownership itself supplies no Docker policy or domain sanitization.
- Missing streams and descriptor-less injected test streams follow the same
  owner lifecycle; a descriptor-less double is not evidence that production
  fd reuse is safe.

## Executable examples

These three examples cover the ownership choice: raw descriptor, existing
stream, or directory capability. They execute against the supported API through
`tests/test_filesystem_lifecycle_docs.py`.

### Write through a Python wrapper without transferring the fd

Prepare constructor inputs before `os.open`, then adopt its result immediately.
The inner file object provides buffering, not descriptor-release authority:
`closefd=False` leaves that authority with the outer OwnedDescriptor. Nesting
ensures buffering is flushed before the descriptor is released, including when
an operation raises. Temporary-directory cleanup happens last.

```python
import os
import tempfile

from docker.filesystem.descriptors import OwnedDescriptor
from docker.filesystem.operations import PosixDescriptorOps

ops = PosixDescriptorOps()
label = "payload file"
flags = os.O_CREAT | os.O_WRONLY | os.O_EXCL
with tempfile.TemporaryDirectory() as directory:
    path = os.path.join(directory, "payload")
    fd = os.open(path, flags, 0o600)
    with OwnedDescriptor(ops, fd, label=label) as owner:
        with os.fdopen(owner.fd, "wb", closefd=False) as handle:
            handle.write(b"payload")
            handle.flush()
        # Closing the Python wrapper did not close the descriptor.
        assert os.fstat(owner.fd).st_size == len(b"payload")
```

For unbuffered operations, use `os.read`/`os.write` with `owner.fd` inside the
same owner scope; a second acquisition example is unnecessary. Do not use
`tempfile.mktemp()` to reserve a filename: it reserves nothing.

### Adopt an existing stream once

A stream already owns its descriptor, so wrap the stream itself, not fileno().
This in-memory example uses the same ownership interface without requiring a
subprocess or an external file. Read through the borrowed `owner.stream` view;
only the owner releases it when the scope ends.

```python
import io

from docker.filesystem.streams import OwnedStream

source = io.BytesIO(b"captured output")
with OwnedStream(source, label="captured output") as owner:
    assert owner.stream.read() == b"captured output"

assert source.closed
assert owner.released
# A repeat cleanup does not invoke the underlying close again.
owner.close()
```

Rejected adoption is different from release: invalid labels/interfaces or
synchronization-preparation failure leave the stream with its original caller.
That caller must retain it for valid adoption or its existing cleanup path.
Successful adoption transfers release authority; keeping a reference to the
original stream does not permit calling its close directly.

### Work relative to a directory capability

Open a validated root once, then use child methods instead of constructing new
ambient paths. A child capability has its own scope and is released before its
parent. The temporary directory owns the example's storage lifetime; descriptor
release does not delete directory entries.

```python
import tempfile

from docker.filesystem.descriptors import DirectoryDescriptor
from docker.filesystem.operations import PosixDescriptorOps

ops = PosixDescriptorOps()
with tempfile.TemporaryDirectory() as directory:
    with DirectoryDescriptor.open_secure_path(
        ops, directory, label="workspace"
    ) as root:
        with root.open_or_create_directory("output", mode=0o700) as output:
            assert output.list_names() == ()
        # Releasing a capability closes its fd, not the directory itself.
        assert root.list_names() == ("output",)
```

For several independent owners, collect their release attempts with
`CleanupFailures(primary)` rather than sequential unprotected close calls.
Call `cleanup.run(owner.close, ordinary=(OSError,))` for each owner, then
`cleanup.complete()` to select the authoritative failure. Every independent
release is attempted even if an earlier one fails. Preserve an active operation
failure over ordinary close errors; do not retry a failed close. Interruption,
rejected transfer and operation-plus-close fault matrices belong in the
filesystem lifecycle tests, not in usage examples.

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

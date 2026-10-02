# Phase 2 Validation Record — Owner-Private Advisory Locks

## Run

Date: 2026-10-02T12:25:58Z

Reconciled: 2026-10-02T13:38:28Z. The authoritative design was updated
through the OpenSpec update workflow to distinguish a pre-existing
owner-inaccessible lock from an inode created by the current acquisition, and
`FAIL_FAST` contention now classifies both portable nonblocking errnos
(`EAGAIN`/`EWOULDBLOCK` and `EACCES`). The counts below are from the fresh
post-reconciliation run.

Change: `add-durable-filesystem-transactions`

Production files changed:

```text
docker/transactions/errors.py      (lock stages, LockError, LockContention)
docker/transactions/posix.py       (L0 flock delegation)
docker/transactions/locking.py     (new: LockPolicy, LockCapability)
docker/transactions/__init__.py    (exports)
```

Test files changed:

```text
tests/test_transactions_locking.py     (new: Phase 2 tasks 2.1-2.4)
tests/transactions_test_support.py     (InjectedOps.flock recording)
```

Commands:

```text
python -m unittest tests.test_transactions_locking
python -m unittest discover -s tests -p 'test_transactions*.py'
ty check docker --python-version 3.14 --output-format concise
python -m unittest discover -s tests -p 'test_*.py'
```

## Results

- RED state confirmed before implementation: `tests.test_transactions_locking`
  failed at collection with `ModuleNotFoundError: No module named
  'docker.transactions.locking'`.
- Focused Phase 2 module after implementation: **56 tests, OK**.
- Focused Phase 1+2 suite: **182 tests, OK**.
- Typecheck: `All checks passed!`.
- Complete repository suite (regression guard): **4687 tests, OK (skipped=13)**, exit
  code `0`. The lock module is additive and imports only `PosixFileOps`,
  `DirectoryCapability`, and the shared error types.

## Scope delivered (tasks 2.5-2.7)

`docker/transactions/locking.py` implements the shared lock substrate:

- `LockPolicy.BLOCK` / `LockPolicy.FAIL_FAST`; `policy` is a required
  keyword-only argument, so there is no implicit contention default. Omission
  raises `TypeError`; an invalid value raises `LockError`.
- `LockCapability.acquire(ops, directory, name, *, namespace, policy)` prepares
  and validates one lock entry beneath a live `DirectoryCapability`:
  1. `openat(dir_fd, name, O_RDWR|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC|O_NONBLOCK,
     0600)`; success marks the inode as created by this acquisition.  `EEXIST`
     opens the existing entry with `O_RDWR`, falling back to `O_RDONLY` then
     `O_WRONLY` (without `O_CREAT`) when the owner cannot open it for read-write;
     `EEXIST` never adopts or mutates the competing entry before locking it
  2. `flock(LOCK_EX)` or `flock(LOCK_EX|LOCK_NB)`
  3. `fstat` and validate no-follow type, effective owner, single link, and
     owner read/write accessibility from `st_mode` (`S_IRUSR`/`S_IWUSR`) — the
     mode check is authoritative and independent of whether the open succeeded;
     a known-new inode is exempt because a restrictive umask, not the owner,
     produced its mode
  4. reopen the basename and compare `(st_dev, st_ino)` against the locked fd
  5. repair a safe owner-owned single-link regular file to exactly `0600` with
     `fchmod`, using the fresh probe stat and only after exclusive acquisition;
     a pre-existing entry whose `st_mode` grants the owner neither read nor write
     is rejected before this step is reached, while a known-new inode is repaired
     even when the umask stripped it to `0000`
- `LockCapability.assert_authorizes(directory=..., namespace=...)` rejects a
  released capability, a released directory capability, a different root, or a
  different namespace with `CapabilityError` before any protected mutation.
- `LockCapability.close()` (and `__exit__`) unconditionally attempt `LOCK_UN`
  and `close` exactly once each, including when unlock raises a process-control
  interruption, so a released capability never leaks its descriptor.  Ordinary
  failures are attached as secondary diagnostics to an existing primary
  failure, and only a release with no primary raises a staged `LockError`.

## Boundary coverage (task 2.9)

| Boundary | Behavior verified |
| --- | --- |
| missing entry | created owner-private `0600`, single link, owner `euid` |
| missing entry under restrictive umask | created `O_EXCL` (mode stripped by the umask), `flock` obtained, then repaired to exactly `0600`; `flock` precedes `fchmod` |
| safe wrong mode | repaired to exactly `0600`, and only after `flock` succeeded (`flock` precedes `fchmod`); interrupted acquisition performs no repair |
| read-only or write-only mode | opened with the strongest available access (`O_RDONLY`/`O_WRONLY`), locked, then repaired to exactly `0600`; `flock` precedes `fchmod` |
| owner-inaccessible mode, unprivileged open denial | all access-mode opens return `EACCES`; fails closed with `LockError(STAGE_LOCK_PREPARE)` before `flock`/`fchmod`; mode unchanged |
| owner-inaccessible mode, elevated fast-open success | `O_RDWR` succeeds despite mode `0000`; the lock is acquired, `st_mode` validation rejects with `LockError(STAGE_LOCK_VALIDATE)`, no `fchmod`, and the acquired descriptor is unlocked and closed; mode unchanged |
| symlink entry | rejected via `O_NOFOLLOW` `ELOOP`; link and target unchanged |
| directory entry | rejected (`EISDIR`); directory unchanged |
| FIFO entry | opened but rejected as non-regular (`O_NONBLOCK` prevents a blocking open); FIFO unchanged |
| foreign-owned entry | rejected via `fstat` ownership; no `fchmod`; real owner/mode unchanged |
| multiply linked entry | rejected via `st_nlink`; both links and mode unchanged |
| bootstrap race (replace / unlink) | probe reopen detects a replaced or missing entry and rejects without repair |
| link added after first validation | fresh probe stat is re-validated and rejected without repair |
| unrelated sibling/ancestor | sibling file and containing directory mode unchanged |
| same namespace, `FAIL_FAST` | second holder rejected with `LockContention`; failed descriptor closed |
| same namespace, `BLOCK` | second holder blocks until the first releases, then acquires |
| different namespace | both holders proceed concurrently |
| released capability | `assert_authorizes` raises `CapabilityError` before mutation |
| cross-namespace capability | rejected before mutation |
| wrong-root capability | rejected before mutation |
| released directory capability | rejected before mutation |
| success / ordinary exception | release runs on every path; body exception stays primary |
| interruption / cancellation | the exact `KeyboardInterrupt`/`_Cancellation` instance escapes; release still attempted and the descriptor is closed exactly once even though unlock was interrupted |
| validation failure | descriptor closed, no repair |
| contention failure | `LockContention` raised (both portable nonblocking contention errnos: `EAGAIN`/`EWOULDBLOCK` and `EACCES`), descriptor closed |
| unlock failure | `LockError(STAGE_UNLOCK)` when no primary; raw `OSError` attached as secondary when a primary exists; close still attempted |
| interrupted unlock + close failure | the exact `KeyboardInterrupt`/cancellation instance escapes with the `OSError` attached as secondary; descriptor closed exactly once |
| close failure | `LockError(STAGE_CLOSE)` when no primary; raw `OSError` attached as secondary when a primary exists |
| both unlock and close fail | first failure is primary, the other is preserved as secondary |
| repeated release | idempotent; no second system call |

Restrictive-mode repair (final contract in `design.md` and the
`Coordinate owner-private state through validated file locks` requirement).
Acquisition first tries an exclusive `O_RDWR|O_CREAT|O_EXCL` creation, and for
an existing entry tries `O_RDWR`, then `O_RDONLY`, then `O_WRONLY` without
`O_CREAT`:

| Entry mode | Result |
| --- | --- |
| `0400` (read-only) | opened `O_RDONLY`, `flock` obtained, then `fchmod` to `0600` |
| `0200` (write-only) | opened `O_WRONLY`, `flock` obtained, then `fchmod` to `0600` |
| `0000` created by this acquisition (restrictive umask) | `O_CREAT|O_EXCL` created the inode, `flock` obtained, then `fchmod` to exactly `0600` (known-new inode) |
| `0000` / execute-only, unprivileged | every access open returns `EACCES`; fails closed with `LockError(STAGE_LOCK_PREPARE)`, no `flock`, no `fchmod`, mode unchanged |
| `0000` / execute-only, elevated open | fast `O_RDWR` open succeeds (root / `CAP_DAC_OVERRIDE`), `flock` is obtained, then `st_mode` validation rejects with `LockError(STAGE_LOCK_VALIDATE)`; descriptor unlocked and closed, no `fchmod`, mode unchanged |
| restrictive symlink | `O_NOFOLLOW` rejects it before any repair |
| restrictive foreign-owned | opened, then rejected by `fstat` ownership before any repair |
| restrictive multiply linked | opened, then rejected by `st_nlink` before any repair |
| restrictive interrupted acquisition | `KeyboardInterrupt` on `flock` propagates; no repair; mode unchanged |

Successful opening is never treated as proof of owner accessibility.  For a
pre-existing entry, only `st_mode` bits `S_IRUSR`/`S_IWUSR` permit repair, so
the elevated `0000` row acquires the lock and still rejects it, and the
unprivileged row never reaches the lock; both avoid `fchmod`, leave the mode
unchanged, and release any descriptor that was acquired.  The sole exception is
an inode created by the current acquisition (`O_CREAT|O_EXCL`): a restrictive
process umask may have stripped its owner bits, so a known-new `0000` entry is
repaired to exactly `0600` after the lock is held.  A pre-existing `0000` entry
is never treated as known-new and remains fail-closed under the same umask.

The two cases are never described generically.  The authoritative design
(`design.md`, "Make lock capability explicit and namespace-bound") states that a
pre-existing `0000` lock is rejected without mutation, while a `0000` inode
created by the current acquisition because of the umask is acquired, fully
validated, and only then repaired to exactly `0600`.  The requirement and its
`Handling a safe lock with the wrong mode` scenario in
`specs/durable-filesystem-transactions/spec.md` state the same distinction and
explicitly scope the created-entry repair to the current acquisition.

The ordering guarantee is the same for every repairable mode: `flock` is
always attempted before `fchmod`, asserted through the recorded L0 call order.
The suite forces the `EACCES` that an unprivileged owner sees by injecting it,
so the fallback is proven even when the process runs with elevated privileges,
and it also exercises real kernel permission denial on the supported
unprivileged runner.  Those tests pin *open* behavior; owner accessibility
itself is authoritative from `st_mode` and is proven independently by
`test_mode_0000_is_rejected_even_when_fast_open_succeeds`, which runs under
root and non-root without skipping.

Nonblocking (`FAIL_FAST`) contention is classified from `flock`'s errno, not
from the Python exception subclass.  POSIX permits either `EAGAIN`
(`BlockingIOError`) or `EACCES` (`PermissionError`) when a nonblocking `flock`
finds the lock held, so both map to `LockContention` while any other `OSError`
(for example `EIO`) maps to `LockError(STAGE_LOCK_ACQUIRE)`:

| Injected `flock` error (`FAIL_FAST`) | Result |
| --- | --- |
| real held lock (`EAGAIN`/`EWOULDBLOCK`) | `LockContention`, descriptor released, no `fchmod` |
| `OSError(EAGAIN)` | `LockContention`, cause preserved, descriptor released, no `fchmod` |
| `OSError(EACCES)` | `LockContention`, cause preserved, descriptor released, no `fchmod` |
| `OSError(EIO)` | `LockError(STAGE_LOCK_ACQUIRE)`, cause preserved |

Process-level tests (task 2.2/2.9) use `multiprocessing.get_context("fork")` and
real `flock`, with events that prove `BLOCK` waits and `FAIL_FAST` rejects:
same-namespace exclusion, same-namespace blocking, and different-namespace
concurrency.

## INTROSPECT review (task 2.8)

1. **Pathname/descriptor TOCTOU** — the lock entry is opened by basename
   relative to the live directory descriptor; no pathname is reconstructed from
   the descriptor. The only pathname is the domain-provided basename, which
   `DirectoryCapability.child_basename` constrains to one canonical component.
2. **Lock replacement** — a post-acquisition `O_PATH|O_NOFOLLOW` probe reopens
   the basename and requires the same `(st_dev, st_ino)` and re-runs the
   type/owner/link validation on the fresh stat, so an entry unlinked, replaced,
   or hard-linked between open and acquisition is rejected before any repair.
   A residual window after the probe exists only for a same-user actor that
   mutates the namespace outside the cooperating protocol; that boundary is the
   same one Phase 1 documents and is out of contract.
3. **Descriptor inheritance** — lock and probe opens carry `O_CLOEXEC`, so the
   descriptors do not survive `exec` (for example a Docker child) and cannot pin
   the lock past the adopter's lifetime. `flock` state is inherited across
   `fork` through the shared open file description, so a forked child must
   acquire its own capability and must not perform protected mutation without
   one; the domain adapters already re-acquire per process.
4. **Capability reuse** — a capability records the live directory token and the
   namespace at acquisition; `assert_authorizes` re-checks both the capability's
   and the directory's liveness and compares root and namespace, so a released
   or mismatched capability cannot authorize mutation. Construction is gated by
   a module-private authority sentinel.
5. **Lock-order inversion** — the shared layer acquires exactly one lock at a
   time and exposes no nested-acquisition API, so it cannot itself invert lock
   order. Adopters that need more than one lock own that ordering; this is
   recorded as a domain responsibility, not a shared guarantee.
6. **Ancestor mutation** — preparation touches only the lock basename inside the
   already-validated directory. The layer never creates, chmods, or repairs a
   directory or an ancestor; directory bootstrap remains a domain adapter.
7. **Implicit policy** — `policy` is required keyword-only with no default;
   omission is rejected (`TypeError`) and an invalid value is rejected
   (`LockError`) before any descriptor is opened.
8. **Interruption conversion** — only `OSError` is wrapped or collected.
   `KeyboardInterrupt` and cancellation-style exceptions raised during
   acquisition, validation, or release propagate unchanged and replace any
   primary failure, matching the Phase 1 cleanup policy.  Release is
   unconditional even under interruption: an unlock that raises
   `KeyboardInterrupt`, cancellation, or any other `BaseException` still falls
   through to a single `close` attempt, the original instance is re-raised
   unchanged, and an ordinary `OSError` raised by that close is attached as
   secondary context rather than replacing the interruption.  Unlock and close
   are never retried.
9. **Cleanup masking** — release attaches ordinary unlock/close failures as
   secondary diagnostics to an existing primary failure and raises a staged
   `LockError` only when no primary exists; when both release steps fail with no
   primary, the second failure is preserved as secondary rather than masked.
10. **Namespace uniqueness** — the shared layer binds both the root and the
    namespace, so two lock entries can no longer authorize each other by sharing
    a directory inode (the defect that caused the earlier partial layer to be
    reverted). The namespace must be domain-selected and unique per protected
    critical section; this is stated as an adopter contract because the shared
    layer cannot know the domain's scope identity.
11. **Restrictive safe mode acquisition (narrowed contract)** — the initial
    `O_RDWR|O_CREAT` open fails with `EACCES` for a safe owner-owned entry whose
    mode omits read or write. The acquisition path now falls back to
    `O_RDONLY` then `O_WRONLY` so the entry can still be opened, exclusively
    locked, and only then repaired to `0600`. `fchmod` requires ownership, not
    the descriptor's access mode, so a read-only descriptor repairs correctly.
    Owner accessibility is decided from the locked descriptor's `st_mode`
    (`S_IRUSR`/`S_IWUSR`), never from whether an open succeeded: a root or
    `CAP_DAC_OVERRIDE` process can open an entry whose mode denies its owner
    access, so a mode that grants the owner neither read nor write is rejected
    after acquisition and before any repair with `LockError` and left
    unmodified. This closes the elevated-privilege hole where a successful
    `O_RDWR` open would otherwise have repaired a `0000` lock, and is proven by
    `test_mode_0000_is_rejected_even_when_fast_open_succeeds`, which forces the
    fast open to succeed on a real `0000` entry and asserts no `fchmod` runs and
    the mode stays `0000`. For an unprivileged owner the same mode cannot be
    opened at all, so it fails closed earlier with
    `LockError(STAGE_LOCK_PREPARE)`. This narrows the earlier design claim that
    every safe wrong mode is repairable to every safe mode that grants the owner
    read or write, and is recorded in `design.md`. The behavior is deterministic
    under the test suite through injected `EACCES` as well as real unprivileged
    permission checks.
12. **Non-Linux probe fallback (documented limitation)** — the post-acquisition
    probe uses `O_PATH` where available (Linux) so identity can be checked
    without read permission. On a platform without `O_PATH` the fallback is
    `O_RDONLY`, which suffices for the read-only/writable modes that can be
    acquired and for pre-existing no-access entries (rejected before the probe),
    but a known-new inode stripped to `0000` by a restrictive umask is repaired
    on Linux only. This substrate targets POSIX/Linux, matching the existing
    `dir_fd`, `O_NOFOLLOW`, `linkat`, and directory-fsync requirements.
13. **Restrictive-umask creation** — a missing entry is created with
    `O_CREAT|O_EXCL` at mode `0600`, so a process umask may strip the on-disk
    mode to `0000` even though the caller requested an owner-private lock.  The
    acquisition records that it created the inode and, after `flock` and full
    identity validation, repairs that known-new inode to exactly `0600`
    (`flock` precedes `fchmod`).  Because creation is exclusive, a concurrent
    creator surfaces as `EEXIST` and follows the existing-entry path instead,
    which never mutates or adopts the competing entry before locking and
    validating it.  A pre-existing entry must set `S_IRUSR`/`S_IWUSR` itself, so
    a pre-existing `0000` lock is still rejected without `fchmod` under the same
    umask.  This is proven by
    `test_creates_lock_repair_with_restrictive_umask` and
    `test_restrictive_umask_does_not_repair_preexisting_inaccessible_lock`, and
    does not weaken the fail-closed rule for pre-existing inaccessible entries
    stated in `design.md` and in the `Coordinate owner-private state through
    validated file locks` requirement.
14. **Portable nonblocking contention errno** — `FAIL_FAST` acquisition
    previously treated only `BlockingIOError` as contention, but `flock(2)`
    also permits `EACCES` for a nonblocking request that finds the lock held;
    Python surfaces that as `PermissionError`, which the old check reported as
    a generic `LockError(STAGE_LOCK_ACQUIRE)`.  The check now classifies any
    `OSError` with errno `EACCES` or `EAGAIN` as `LockContention` when the
    policy is `FAIL_FAST`, preserving the original exception as both `cause`
    and `__cause__`, while every other `OSError` still maps to `LockError`.
    Blocking acquisition and release handling are unchanged.  Proven by
    `test_nonblocking_eacces_is_contention`,
    `test_nonblocking_eagain_is_contention`, and
    `test_unrelated_acquire_error_is_lock_error`; the `EACCES` test fails if the
    errno check is reverted to the `BlockingIOError`-only form.

No unresolved findings remain.

## Dependency boundary

Phase 2 is additive: it does not modify the Phase 1 L1/L2 contracts. The L2
regular-file operations remain standalone leaf contracts, and an adopting
domain calls `LockCapability.assert_authorizes` for its critical section before
invoking them. Later phases (build generations, project metadata, runtime and
npm adapters) perform the migrations that require this capability.

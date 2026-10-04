"""Owner-private advisory locks with an explicit contention policy.

A :class:`LockCapability` is bound to a domain-selected normalized
``namespace`` and to the live directory capability that contains the lock
entry.  Acquisition rejects a symlink, non-regular entry, foreign-owned file,
or multiply linked file without mutating it, verifies that the entry was not
replaced between the open and the exclusive acquisition, and repairs a safe
owner-owned single-link regular file to exactly ``0600`` only after the lock
has been acquired.  Consumers must select ``BLOCK`` or ``FAIL_FAST``; there is
no implicit default.

A missing entry is created atomically with ``O_CREAT|O_EXCL`` at mode
``0600``.  Such a known-new inode is repaired to exactly ``0600`` after the
exclusive acquisition even when the process umask stripped its owner read and
write bits.  A safe existing lock whose mode grants the owner read or write
access is opened with the strongest access the owner currently has (falling
back from ``O_RDWR`` to ``O_RDONLY`` or ``O_WRONLY``) so it can still be locked
and then repaired.  Owner accessibility is decided from the descriptor's
``st_mode``, not from whether the open succeeded: a privileged process (root or
``CAP_DAC_OVERRIDE``) can open an entry whose mode denies its owner access, so
such an existing entry is rejected after the exclusive acquisition and before
any repair.  A pre-existing mode that grants the owner neither read nor write
cannot be exclusively acquired before repair by an unprivileged process; that
case also fails closed and is left unmodified.

Release is unconditional: unlock and close are both attempted, ordinary
failures are attached as secondary diagnostics to an existing primary failure,
and process-control interruptions propagate unchanged.
"""
from __future__ import annotations

import enum
import errno
import fcntl
import os
import stat

from .capabilities import DirectoryCapability
from .errors import (
    STAGE_CLOSE,
    STAGE_LOCK_ACQUIRE,
    STAGE_LOCK_MODE,
    STAGE_LOCK_PREPARE,
    STAGE_LOCK_STAT,
    STAGE_LOCK_VALIDATE,
    STAGE_UNLOCK,
    CapabilityError,
    LockContention,
    LockError,
    attach_secondary,
)
from .posix import PosixFileOps

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
# O_PATH lets the post-acquisition probe identify the entry without requiring
# read permission.  It is only an identity probe: owner accessibility is always
# decided from the locked descriptor's st_mode, never from an open result.
_O_PATH = getattr(os, "O_PATH", 0)

_CREATE_FLAGS = os.O_RDWR | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC | _NONBLOCK
# Access modes tried, in order, when an existing entry cannot be opened for
# read-write because its mode restricts the owner.  These are used without
# O_CREAT (the entry already exists) so a missing file still fails closed.
_EXISTING_FLAGS = os.O_RDWR | _NOFOLLOW | _CLOEXEC | _NONBLOCK
_ACCESS_FALLBACK_FLAGS = (
    os.O_RDONLY | _NOFOLLOW | _CLOEXEC | _NONBLOCK,
    os.O_WRONLY | _NOFOLLOW | _CLOEXEC | _NONBLOCK,
)
if _O_PATH:
    _PROBE_FLAGS = _O_PATH | _NOFOLLOW | _CLOEXEC
else:  # pragma: no cover - non-Linux fallback
    _PROBE_FLAGS = os.O_RDONLY | _NOFOLLOW | _CLOEXEC

_LOCK_MODE = 0o600

# A lock capability carries authority only when it was returned by a validated
# acquisition.  The sentinel is module-private so callers cannot construct an
# unvalidated capability that appears to hold a live lock.
_AUTHORITY = object()
_DENIED = object()


class LockPolicy(enum.Enum):
    """Explicit contention behavior for :meth:`LockCapability.acquire`."""

    BLOCK = "block"
    FAIL_FAST = "fail-fast"


def _open_lock_entry(
    ops: PosixFileOps, directory: DirectoryCapability, base: str
) -> tuple[int, bool]:
    """Open or exclusively create the lock entry, reporting which happened.

    Returns ``(fd, created)``.  ``created`` is true only when this call
    atomically created a new ``0600`` inode; that is the sole case in which a
    mode stripped to no owner access (for example by a restrictive process
    umask) may still be repaired after the exclusive acquisition.

    Creation uses ``O_CREAT|O_EXCL`` with mode ``0600`` so a concurrent creator
    surfaces as ``EEXIST`` instead of being silently adopted.  An entry that
    already exists is opened with the strongest access its owner currently has
    (``O_RDWR`` then ``O_RDONLY``/``O_WRONLY``); open success only yields a
    descriptor, not proof of owner accessibility, which :func:`_validate_lock`
    decides from ``st_mode``.  An existing entry whose owner has neither read
    nor write access fails closed and is never repaired.
    """
    failure: OSError
    denied: OSError | None = None
    try:
        return ops.openat(directory.fd, base, _CREATE_FLAGS, _LOCK_MODE), True
    except OSError as create_error:
        failure = create_error
        if create_error.errno == errno.EEXIST:
            # A pre-existing entry (or a concurrent creator): fall through and
            # open it without mutating or adopting it blindly.
            pass
        elif create_error.errno == errno.EACCES:
            denied = create_error
        else:
            raise LockError(
                STAGE_LOCK_PREPARE, f"cannot create lock {base!r}", cause=create_error
            ) from create_error
    for flags in (_EXISTING_FLAGS, *_ACCESS_FALLBACK_FLAGS):
        try:
            return ops.openat(directory.fd, base, flags, 0), False
        except OSError as exc:
            if exc.errno == errno.EACCES:
                denied = exc
                continue
            if exc.errno == errno.ENOENT:
                # The entry does not exist and the exclusive create was denied
                # (for example an unwritable directory); report the create
                # failure rather than the missing entry, which the existing
                # entry path never intended to create.
                cause = denied if denied is not None else failure
                raise LockError(
                    STAGE_LOCK_PREPARE, f"cannot create lock {base!r}", cause=cause
                ) from cause
            raise LockError(
                STAGE_LOCK_PREPARE, f"cannot open lock {base!r}", cause=exc
            ) from exc
    assert denied is not None
    raise LockError(
        STAGE_LOCK_PREPARE,
        f"lock {base!r} cannot be opened for read or write by its owner",
        cause=denied,
    ) from denied


def _validate_lock(info: os.stat_result, name: str, *, created: bool = False) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise LockError(STAGE_LOCK_VALIDATE, f"lock {name!r} is not a regular file")
    if info.st_uid != os.geteuid():
        raise LockError(STAGE_LOCK_VALIDATE, f"lock {name!r} is not owned by the invoking user")
    if info.st_nlink != 1:
        raise LockError(STAGE_LOCK_VALIDATE, f"lock {name!r} has multiple hard links")
    # Owner accessibility is authoritative from st_mode, never from whether the
    # open succeeded: root or a CAP_DAC_OVERRIDE process can open an entry whose
    # mode denies its owner read and write.  Reject before any repair so a
    # pre-existing no-access lock can never be chmodded while its owner cannot
    # open it.  A known-new inode is exempt because its restricted mode is an
    # artifact of the process umask, not a state the owner chose; it is repaired
    # to exactly 0600 after the exclusive acquisition.
    if not created and not info.st_mode & (stat.S_IRUSR | stat.S_IWUSR):
        raise LockError(
            STAGE_LOCK_VALIDATE,
            f"lock {name!r} grants its owner neither read nor write access",
        )


def _verify_unchanged(
    ops: PosixFileOps, directory: DirectoryCapability, name: str, info: os.stat_result
) -> os.stat_result:
    """Reject an entry replaced between the open and the exclusive acquisition.

    A concurrent creator can unlink and recreate the lock entry while the
    winner holds a descriptor to the previous inode; both processes would then
    "hold" different inodes at the same pathname.  Reopening the basename and
    comparing ``(st_dev, st_ino)`` closes that window for cooperating writers.
    The fresh stat is returned so the caller can re-validate and repair the
    mode of the same inode it actually locked.
    """
    try:
        probe = ops.openat(directory.fd, name, _PROBE_FLAGS, 0)
    except OSError as exc:
        raise LockError(
            STAGE_LOCK_VALIDATE, f"lock {name!r} changed while acquiring", cause=exc
        ) from exc
    try:
        probe_info = ops.fstat(probe)
    except BaseException as exc:
        try:
            ops.close(probe)
        except OSError as close_exc:
            attach_secondary(exc, [close_exc])
        raise
    try:
        ops.close(probe)
    except OSError as exc:
        raise LockError(STAGE_CLOSE, f"cannot close lock probe {name!r}", cause=exc) from exc
    if (probe_info.st_dev, probe_info.st_ino) != (info.st_dev, info.st_ino):
        raise LockError(STAGE_LOCK_VALIDATE, f"lock {name!r} was replaced while acquiring")
    return probe_info


def _release_descriptor(ops: PosixFileOps, fd: int, primary: BaseException | None) -> None:
    """Unlock and close one not-yet-owned descriptor, preserving *primary*.

    Release is unconditional: unlock and close are each attempted exactly once,
    and the descriptor is still closed when unlock raises a process-control
    interruption (``KeyboardInterrupt``, cancellation, or any other
    ``BaseException``).  Ordinary ``OSError`` failures are attached as secondary
    diagnostics to *primary*; an interruption propagates unchanged and carries
    any ordinary close failure as secondary context.
    """
    failures: list[tuple[str, OSError]] = []
    interruption: BaseException | None = None
    try:
        ops.flock(fd, fcntl.LOCK_UN)
    except OSError as exc:
        failures.append((STAGE_UNLOCK, exc))
    except BaseException as exc:  # interruption during unlock must not skip close
        interruption = exc
    try:
        ops.close(fd)
    except OSError as exc:
        failures.append((STAGE_CLOSE, exc))
    except BaseException as exc:
        if interruption is None:
            interruption = exc
    if interruption is not None:
        attach_secondary(interruption, [exc for _, exc in failures])
        raise interruption
    if primary is not None:
        attach_secondary(primary, [exc for _, exc in failures])
        return
    if failures:
        stage, exc = failures[0]
        error = LockError(stage, "cannot release lock capability", cause=exc)
        # Preserve a secondary unlock/close failure instead of masking it
        # behind the first reported failure.
        attach_secondary(error, [other for _, other in failures[1:]])
        raise error from exc


class LockCapability:
    """A namespace-bound live advisory lock on one private regular file."""

    __slots__ = (
        "_ops",
        "_fd",
        "_directory_token",
        "_namespace",
        "_name",
        "_policy",
        "_closed",
    )

    def __init__(
        self,
        ops: PosixFileOps,
        fd: int,
        directory: DirectoryCapability,
        namespace: str,
        name: str,
        policy: LockPolicy,
        *,
        authority: object = _DENIED,
    ) -> None:
        if authority is not _AUTHORITY:
            raise CapabilityError("LockCapability must be created by acquire()")
        self._ops = ops
        self._fd = fd
        self._directory_token = directory.token
        self._namespace = namespace
        self._name = name
        self._policy = policy
        self._closed = False

    @classmethod
    def acquire(
        cls,
        ops: PosixFileOps,
        directory: DirectoryCapability,
        name: str,
        *,
        namespace: str,
        policy: LockPolicy,
    ) -> "LockCapability":
        """Prepare, acquire, and validate one owner-private lock entry.

        ``policy`` is a required keyword: there is no implicit contention
        behavior.  ``namespace`` is the domain-selected scope identity and must
        be unique per protected critical section.
        """
        base = directory.child_basename(name)
        if not isinstance(namespace, str) or not namespace or "\x00" in namespace:
            raise LockError(STAGE_LOCK_PREPARE, "lock namespace must be a non-empty string")
        if not isinstance(policy, LockPolicy):
            raise LockError(
                STAGE_LOCK_PREPARE,
                "lock contention policy must be LockPolicy.BLOCK or LockPolicy.FAIL_FAST",
            )

        fd, created = _open_lock_entry(ops, directory, base)

        operation = fcntl.LOCK_EX
        if policy is LockPolicy.FAIL_FAST:
            operation |= fcntl.LOCK_NB
        try:
            ops.flock(fd, operation)
        except BaseException as exc:
            # POSIX allows either EACCES or EAGAIN for a nonblocking flock that
            # finds the lock held (Linux raises EAGAIN/EWOULDBLOCK, some systems
            # EACCES).  Classify both as contention for FAIL_FAST; ``flock``
            # only takes the nonblocking path when LOCK_NB was requested.
            if (
                policy is LockPolicy.FAIL_FAST
                and isinstance(exc, OSError)
                and exc.errno in (errno.EACCES, errno.EAGAIN)
            ):
                primary: BaseException = LockContention(
                    f"lock {base!r} is already held", cause=exc
                )
            elif isinstance(exc, OSError):
                primary = LockError(
                    STAGE_LOCK_ACQUIRE, f"cannot acquire lock {base!r}", cause=exc
                )
            else:
                primary = exc
            _release_descriptor(ops, fd, primary)
            if primary is exc:
                raise
            raise primary from exc

        try:
            try:
                info = ops.fstat(fd)
            except OSError as exc:
                # An operational descriptor-stat failure, not a containment
                # rejection: classify it under its own stage so consumers can
                # preserve the raw ``OSError`` instead of reporting an unsafe
                # entry.  Entry-shape and identity checks keep raising
                # ``STAGE_LOCK_VALIDATE`` below.
                raise LockError(
                    STAGE_LOCK_STAT, f"cannot stat lock {base!r}", cause=exc
                ) from exc
            _validate_lock(info, base, created=created)
            fresh = _verify_unchanged(ops, directory, base, info)
            # Re-validate the fresh stat so a link added between the first
            # validation and the probe is rejected, and base the mode repair on
            # the inode that is actually locked.
            _validate_lock(fresh, base, created=created)
            if stat.S_IMODE(fresh.st_mode) != _LOCK_MODE:
                try:
                    ops.fchmod(fd, _LOCK_MODE)
                except OSError as exc:
                    raise LockError(
                        STAGE_LOCK_MODE, f"cannot repair lock mode for {base!r}", cause=exc
                    ) from exc
        except BaseException as exc:
            _release_descriptor(ops, fd, exc)
            raise
        return cls(
            ops, fd, directory, namespace, base, policy, authority=_AUTHORITY
        )

    @property
    def fd(self) -> int:
        if self._closed:
            raise CapabilityError(f"lock capability {self._name!r} is released")
        return self._fd

    @property
    def namespace(self) -> str:
        return self._namespace

    @property
    def name(self) -> str:
        return self._name

    @property
    def policy(self) -> LockPolicy:
        return self._policy

    @property
    def closed(self) -> bool:
        return self._closed

    def assert_authorizes(self, *, directory: DirectoryCapability, namespace: str) -> None:
        """Fail before protected mutation unless this capability is live and matching.

        A released capability, a released directory capability, a capability
        acquired against another root, or a capability for another namespace is
        rejected with :class:`CapabilityError`.
        """
        _ = self.fd
        _ = directory.fd
        if directory.token is not self._directory_token:
            raise CapabilityError(f"lock capability {self._name!r} belongs to a different root")
        if namespace != self._namespace:
            raise CapabilityError(
                f"lock capability {self._name!r} is for a different namespace"
            )

    def close(self) -> None:
        """Release the lock: unlock and close are both attempted exactly once."""
        self._release(primary=None)

    def _release(self, primary: BaseException | None) -> None:
        # Transition to released before attempting the system calls: the state
        # means "release attempted", and a failed release is not retried because
        # POSIX does not guarantee the descriptor stays open.
        if self._closed:
            return
        self._closed = True
        _release_descriptor(self._ops, self._fd, primary)

    def __enter__(self) -> "LockCapability":
        _ = self.fd
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # Preserve an active exception; returning normally re-raises it as the
        # primary failure.  Process-control interruptions raised by release are
        # not caught and propagate unchanged.
        self._release(primary=exc)

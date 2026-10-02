"""L1 descriptor capabilities with live, basename-only authority.

A directory capability retains a validated open directory descriptor.  A
regular-file capability retains one no-follow descriptor that was validated
for type, ownership, link count, and (optionally) mode before any bytes are
read.  A capability never reconstructs a pathname from its descriptor, and a
released or cross-directory capability is rejected before mutation.
"""
from __future__ import annotations

import errno
import os
import stat

from .errors import (
    STAGE_READ,
    STAGE_VALIDATE,
    CapabilityError,
    TransactionError,
    UnsafeFileError,
    attach_secondary,
)
from .posix import PosixFileOps

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)

_DIR_FLAGS = os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC
# O_NONBLOCK prevents a FIFO leaf from blocking the open before the
# descriptor can be validated as a non-regular file; it is a no-op for
# regular files and directories.
_READ_FLAGS = os.O_RDONLY | _NOFOLLOW | _CLOEXEC | _NONBLOCK

_READ_CHUNK = 64 * 1024

# Capabilities are only valid when they come from a factory that validated a
# live descriptor (and, for a file capability, a validated leaf).  The sentinel
# is deliberately module-private: callers cannot construct an unvalidated
# capability that appears to carry authority.
_AUTHORITY = object()
_DENIED = object()


def _validate_directory(info: os.stat_result, label: str) -> None:
    if not stat.S_ISDIR(info.st_mode):
        raise CapabilityError(f"directory capability {label!r} is not a directory")
    if info.st_uid != os.geteuid():
        raise CapabilityError(f"directory capability {label!r} is not owned by the invoking user")


def _validate_regular(
    info: os.stat_result,
    label: str,
    *,
    allowed_mode: int | None,
    require_single_link: bool,
) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise UnsafeFileError(STAGE_VALIDATE, f"{label!r} is not a regular file")
    if info.st_uid != os.geteuid():
        raise UnsafeFileError(STAGE_VALIDATE, f"{label!r} is not owned by the invoking user")
    if require_single_link and info.st_nlink != 1:
        raise UnsafeFileError(STAGE_VALIDATE, f"{label!r} has multiple hard links")
    if allowed_mode is not None and stat.S_IMODE(info.st_mode) != allowed_mode:
        raise UnsafeFileError(STAGE_VALIDATE, f"{label!r} has a forbidden mode")


class DirectoryCapability:
    """A validated directory retaining a live descriptor."""

    __slots__ = ("_ops", "_fd", "_label", "_token", "_closed")

    def __init__(self, ops: PosixFileOps, fd: int, label: str, *, authority: object = _DENIED) -> None:
        if authority is not _AUTHORITY:
            raise CapabilityError(
                "DirectoryCapability must be created by from_path() or from_fd()"
            )
        self._ops = ops
        self._fd = fd
        self._label = label
        self._token = object()
        self._closed = False

    @classmethod
    def from_path(cls, ops: PosixFileOps, path, *, label: str | None = None) -> "DirectoryCapability":
        name = os.fspath(path)
        try:
            fd = ops.openat(None, name, _DIR_FLAGS, 0)
        except OSError as exc:
            raise CapabilityError(f"cannot open directory capability {name!r}: {exc}") from exc
        primary: BaseException | None = None
        try:
            _validate_directory(ops.fstat(fd), name)
        except BaseException as exc:
            primary = exc
            try:
                ops.close(fd)
            except OSError as close_exc:
                # Ordinary cleanup failure: attach it as a secondary
                # diagnostic without replacing the primary validation error.
                # A process-control interruption (KeyboardInterrupt or a
                # cancellation-style BaseException) is not caught here and
                # propagates unchanged.  A failed close is never retried
                # because the descriptor state is then ambiguous.
                attach_secondary(primary, [close_exc])
            raise
        return cls(ops, fd, label if label is not None else name, authority=_AUTHORITY)

    @classmethod
    def from_fd(cls, ops: PosixFileOps, fd: int, label: str) -> "DirectoryCapability":
        """Adopt a caller-provided descriptor after validating it.

        Ownership transfers only on success; a failed validation raises
        without closing *fd*, so the caller remains responsible for it.
        """
        _validate_directory(ops.fstat(fd), label)
        return cls(ops, fd, label, authority=_AUTHORITY)

    @property
    def fd(self) -> int:
        if self._closed:
            raise CapabilityError(f"directory capability {self._label!r} is released")
        return self._fd

    @property
    def label(self) -> str:
        return self._label

    @property
    def token(self) -> object:
        return self._token

    @property
    def closed(self) -> bool:
        return self._closed

    def child_basename(self, name) -> str:
        """Return *name* only when it is a single canonical path component."""
        if (
            not isinstance(name, str)
            or not name
            or name in (".", "..")
            or "/" in name
            or "\x00" in name
            or os.sep in name
            or os.altsep is not None and os.altsep in name
        ):
            raise CapabilityError(f"unsafe child basename: {name!r}")
        return name

    def open_regular(
        self,
        name: str,
        *,
        allowed_mode: int | None = None,
        require_single_link: bool = True,
    ) -> "FileCapability":
        """Open and validate one regular-file leaf through a no-follow descriptor."""
        base = self.child_basename(name)
        try:
            fd = self._ops.openat(self.fd, base, _READ_FLAGS, 0)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise UnsafeFileError(
                    STAGE_VALIDATE, f"{base!r} is not a safe regular file", cause=exc
                ) from exc
            raise TransactionError(
                STAGE_READ, f"cannot open {base!r} for validated read", cause=exc
            ) from exc
        primary: BaseException | None = None
        try:
            try:
                info = self._ops.fstat(fd)
            except OSError as exc:
                raise TransactionError(
                    STAGE_VALIDATE, f"cannot stat {base!r}", cause=exc
                ) from exc
            _validate_regular(
                info, base,
                allowed_mode=allowed_mode,
                require_single_link=require_single_link,
            )
        except BaseException as exc:
            primary = exc
            try:
                self._ops.close(fd)
            except OSError as close_exc:
                # Ordinary cleanup failure: attach it as a secondary
                # diagnostic without replacing the primary validation error.
                # A process-control interruption (KeyboardInterrupt or a
                # cancellation-style BaseException) is not caught here and
                # propagates unchanged.  A failed close is never retried
                # because the descriptor state is then ambiguous.
                attach_secondary(primary, [close_exc])
            raise
        return FileCapability(self._ops, fd, self, info, base, authority=_AUTHORITY)

    def close(self) -> None:
        # Release the capability before the descriptor close is attempted: the
        # transition means "close attempted", not "close definitely succeeded".
        # A failed close is not retried (POSIX does not guarantee the
        # descriptor stays open, so a retry could close a reused descriptor),
        # and a second close is therefore a no-op that issues no system call.
        if not self._closed:
            self._closed = True
            self._ops.close(self._fd)

    def __enter__(self) -> "DirectoryCapability":
        _ = self.fd
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc is not None:
            # Preserve an active exception: an ordinary close failure is
            # attached as secondary context and returning normally lets Python
            # re-raise the original exception.  A process-control interruption
            # from close is not caught and propagates unchanged.
            try:
                self.close()
            except OSError as close_error:
                attach_secondary(exc, [close_error])
            return
        # No active exception: a close failure is the only failure and
        # propagates.
        self.close()


class FileCapability:
    """A validated regular-file leaf retaining one live no-follow descriptor."""

    __slots__ = ("_ops", "_fd", "_directory_token", "_info", "_label", "_closed")

    def __init__(
        self,
        ops: PosixFileOps,
        fd: int,
        directory: DirectoryCapability,
        info: os.stat_result,
        label: str,
        *,
        authority: object = _DENIED,
    ) -> None:
        if authority is not _AUTHORITY:
            raise CapabilityError(
                "FileCapability must be created by DirectoryCapability.open_regular()"
            )
        self._ops = ops
        self._fd = fd
        self._directory_token = directory.token
        self._info = info
        self._label = label
        self._closed = False

    @property
    def fd(self) -> int:
        if self._closed:
            raise CapabilityError(f"file capability {self._label!r} is released")
        return self._fd

    @property
    def info(self) -> os.stat_result:
        return self._info

    @property
    def closed(self) -> bool:
        return self._closed

    def assert_owned_by(self, directory: DirectoryCapability) -> None:
        # Reject a released file capability, then a released directory
        # capability, and only then compare the live directory identity.  Each
        # released case raises CapabilityError without touching the other side.
        _ = self.fd
        _ = directory.fd
        if directory.token is not self._directory_token:
            raise CapabilityError(
                f"capability {self._label!r} does not belong to {directory.label!r}"
            )

    def read_all(self) -> bytes:
        fd = self.fd
        parts: list[bytes] = []
        while True:
            chunk = self._ops.read(fd, _READ_CHUNK)
            if not chunk:
                break
            parts.append(chunk)
        return b"".join(parts)

    def close(self) -> None:
        # Release the capability before the descriptor close is attempted: the
        # transition means "close attempted", not "close definitely succeeded".
        # A failed close is not retried (POSIX does not guarantee the
        # descriptor stays open, so a retry could close a reused descriptor),
        # and a second close is therefore a no-op that issues no system call.
        if not self._closed:
            self._closed = True
            self._ops.close(self._fd)

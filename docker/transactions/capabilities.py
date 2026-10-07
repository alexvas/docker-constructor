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
from typing import cast

from docker.filesystem.descriptors import (
    DescriptorError,
    DirectoryDescriptor,
    UnsafeDescriptorError,
    _STAGE_OPEN as _FOUNDATION_OPEN_STAGE,
    _STAGE_VALIDATE as _FOUNDATION_VALIDATE_STAGE,
)
from docker.filesystem.operations import DescriptorOps

from .cleanup import CleanupFailures
from .errors import (
    STAGE_READ,
    STAGE_VALIDATE,
    CapabilityError,
    TransactionError,
    UnsafeFileError,
    carry_secondary_diagnostics,
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


def _descriptor_ops(ops: PosixFileOps) -> DescriptorOps:
    """Present the transaction backend as the foundation injection protocol.

    ``PosixFileOps`` preserves the historical ``dir_fd`` keyword for existing
    callers, while the foundation protocol names the same positional parameter
    ``directory_fd``.  The two are call-compatible because the foundation only
    ever passes each descriptor positionally, so the adapter boundary makes
    that compatibility explicit instead of renaming the public backend method.
    """
    return cast(DescriptorOps, ops)


# Capabilities are only valid when they come from a factory that validated a
# live descriptor (and, for a file capability, a validated leaf).  The sentinel
# is deliberately module-private: callers cannot construct an unvalidated
# capability that appears to carry authority.
_AUTHORITY = object()
_DENIED = object()

# The shared descriptor owner requires a non-empty ``str`` internal label.
# The transaction contract, however, has always accepted ``label=""`` and
# freely preserved whatever the caller supplied (including out-of-contract
# non-string values) through :attr:`DirectoryCapability.label`.  Normalizing
# the private diagnostic label here keeps the shared owner's invariant intact
# without altering the caller-supplied transaction label, and guarantees the
# post-detach transfer construction can never reject the internal label.
_INTERNAL_LABEL = "<directory-capability>"


def _internal_label(label: object) -> str:
    """Return the non-empty ``str`` label handed to the shared descriptor owner.

    A non-empty string is preserved so diagnostics stay meaningful; an empty
    string or any non-string value falls back to :data:`_INTERNAL_LABEL`.  This
    runs before ``from_secure_path`` detaches the validated leaf, so the
    foundation can never reject the internal label after ownership has already
    moved away from the secure walk.
    """
    if isinstance(label, str) and label:
        return label
    return _INTERNAL_LABEL


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
    """A validated directory retaining a live descriptor.

    Directory ownership, release state, and secure path walking are delegated
    to the shared :class:`docker.filesystem.descriptors.DirectoryDescriptor`;
    this class adds only the transaction authority token, the permanent
    validated-transfer seam used by :meth:`from_fd`, and the L1 regular-file
    capability factory.  It defines no second ownership state machine and no
    second component walker.
    """

    __slots__ = ("_ops", "_descriptor", "_label", "_token")

    def __init__(
        self, ops: PosixFileOps, fd: int, label: str, *, authority: object = _DENIED
    ) -> None:
        if authority is not _AUTHORITY:
            raise CapabilityError(
                "DirectoryCapability must be created by from_path(), from_fd(), or "
                "from_secure_path()"
            )
        self._ops = ops
        # The transaction label is preserved verbatim -- including the empty
        # string -- while the shared descriptor owner receives a valid
        # non-empty internal label.
        self._label = label
        self._descriptor = DirectoryDescriptor._transfer_validated(
            _descriptor_ops(ops), fd, label=_internal_label(label)
        )
        self._token = object()

    @classmethod
    def _adopt(cls, ops: PosixFileOps, fd: int, label: str) -> "DirectoryCapability":
        """Validate an already-open candidate descriptor and adopt it.

        Ownership transfers only on success.  On any failure after the
        descriptor was opened -- directory validation or validated-transfer
        construction -- the still caller-owned descriptor is closed exactly
        once and an ordinary close failure is attached as a secondary
        diagnostic without replacing the primary failure.  A process-control
        interruption is not caught.
        """
        try:
            _validate_directory(ops.fstat(fd), label)
            return cls._transfer_validated(ops, fd, label)
        except BaseException as exc:
            failures = CleanupFailures(exc)
            failures.run(lambda: ops.close(fd), ordinary=(OSError,))
            result = failures.complete()
            if result is not None:
                raise result
            raise

    @classmethod
    def _transfer_validated(
        cls, ops: PosixFileOps, fd: int, label: str
    ) -> "DirectoryCapability":
        """Permanent module-internal validated-transfer seam.

        The caller must have already validated directory type and requested
        effective ownership of *fd*.  Ownership transfers exactly once to the
        shared descriptor owner without a repeated validation pass and without
        the consuming release path of ``DirectoryDescriptor.adopt()``, so a
        pre-transfer validation failure never closes a caller-owned descriptor.
        """
        return cls(ops, fd, label, authority=_AUTHORITY)

    @classmethod
    def from_path(cls, ops: PosixFileOps, path, *, label: str | None = None) -> "DirectoryCapability":
        name = os.fspath(path)
        target_label = label if label is not None else name
        try:
            fd = ops.openat(None, name, _DIR_FLAGS, 0)
        except OSError as exc:
            raise CapabilityError(f"cannot open directory capability {name!r}: {exc}") from exc
        return cls._adopt(ops, fd, target_label)

    @classmethod
    def from_secure_path(
        cls, ops: PosixFileOps, path, *, label: str | None = None,
    ) -> "DirectoryCapability":
        """Open *path* through the shared secure descriptor walk.

        The lightweight foundation owns the component walk, the retained
        parent/leaf handoff, and the exactly-once release precedence.  This
        adapter resolves the transaction label, restores the raw ``OSError``
        for a final-leaf validation-stat failure, and maps every remaining
        bounded foundation failure back to the pre-migration transaction
        wording and raw cause.  A walk open failure (root or child, including
        a symlink/ELOOP rejection) reports the full requested path with the
        raw ``OSError`` as its direct cause; a relative-path refusal, a
        non-directory final leaf, and a foreign-owned final leaf keep the
        established ``CapabilityError`` text with no cause.  The validated
        leaf then transfers to the transaction capability exactly once.
        """
        name = os.fspath(path)
        target_label = label if label is not None else name
        try:
            descriptor = DirectoryDescriptor.open_secure_path(
                _descriptor_ops(ops), name, label=_internal_label(target_label)
            )
        except UnsafeDescriptorError as exc:
            # A walk open rejection (a symlink/ELOOP leaf) keeps the
            # transaction wording with the raw open ``OSError`` as its direct
            # cause.  A final directory-type or effective-owner rejection
            # carries no cause; its message is rebuilt from the caller-facing
            # transaction label so the foundation's normalized internal label
            # is never exposed.  Either way the foundation's retained close
            # diagnostics are carried onto the translated capability error so
            # cleanup failures are not lost.
            cause = exc.cause
            if exc.stage == _FOUNDATION_OPEN_STAGE and cause is not None:
                error = CapabilityError(
                    f"cannot open directory capability {name!r}: {cause}"
                )
                carry_secondary_diagnostics(error, exc)
                raise error from cause
            message = str(exc)
            prefix = f"directory capability {_internal_label(target_label)!r} "
            if message.startswith(prefix):
                message = (
                    f"directory capability {target_label!r} "
                    f"{message[len(prefix):]}"
                )
            error = CapabilityError(message)
            carry_secondary_diagnostics(error, exc)
            raise error from None
        except DescriptorError as exc:
            # A failed validation stat of the final leaf is the one foundation
            # failure the transaction contract has always surfaced as the raw
            # ``OSError``: the previous ``_adopt`` path let ``ops.fstat``
            # propagate unchanged, with its cleanup diagnostics.  Unwrap the
            # validation-stage wrapper back to that original error and carry
            # the foundation's secondary close/parent diagnostics onto it.
            cause = exc.cause
            if exc.stage == _FOUNDATION_VALIDATE_STAGE and isinstance(cause, OSError):
                carry_secondary_diagnostics(cause, exc)
                raise cause from None
            # A relative-path refusal and every ordinary walk open failure keep
            # the transaction-specific wording produced by the old
            # implementation: the relative-path message already matches, and
            # an open failure reports the full requested path with its raw
            # ``OSError`` cause.  The translated capability error retains the
            # foundation's close diagnostics.
            if cause is None:
                error = CapabilityError(str(exc))
                carry_secondary_diagnostics(error, exc)
                raise error from None
            error = CapabilityError(
                f"cannot open directory capability {name!r}: {cause}"
            )
            carry_secondary_diagnostics(error, exc)
            raise error from cause
        return cls._transfer_validated(ops, descriptor.detach(), target_label)

    @classmethod
    def from_fd(cls, ops: PosixFileOps, fd: int, label: str) -> "DirectoryCapability":
        """Adopt a caller-provided descriptor after validating it.

        Ownership transfers only on success; a failed validation raises
        without closing *fd*, so the caller remains responsible for it.
        """
        _validate_directory(ops.fstat(fd), label)
        return cls._transfer_validated(ops, fd, label)

    @property
    def fd(self) -> int:
        try:
            return self._descriptor.fd
        except DescriptorError:
            # The pre-migration property raised a bare ``CapabilityError``;
            # keep that contract by suppressing the foundation cause.
            raise CapabilityError(
                f"directory capability {self._label!r} is released"
            ) from None

    @property
    def label(self) -> str:
        return self._label

    @property
    def token(self) -> object:
        return self._token

    @property
    def closed(self) -> bool:
        return self._descriptor.released

    def child_basename(self, name) -> str:
        """Return *name* only when it is a single canonical path component."""
        try:
            return self._descriptor.child_basename(name)
        except DescriptorError as exc:
            # Preserve the transaction-facing message without exposing the
            # foundation error as the ``__cause__`` (the old implementation
            # raised a bare ``CapabilityError``).
            raise CapabilityError(str(exc)) from None

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
            # Ordinary cleanup failure: attach it as a secondary diagnostic
            # without replacing the primary validation error.  A process-control
            # interruption from close is authoritative, and an unexpected close
            # defect is retained instead of being skipped.
            failures = CleanupFailures(exc)
            failures.run(lambda: self._ops.close(fd), ordinary=(OSError,))
            result = failures.complete()
            if result is not None:
                raise result
            raise
        return FileCapability(self._ops, fd, self, info, base, authority=_AUTHORITY)

    def close(self) -> None:
        # Delegate the irreversible, at-most-once release to the shared
        # descriptor so directory release state has a single implementation.
        self._descriptor.close()

    def __enter__(self) -> "DirectoryCapability":
        _ = self.fd
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc is not None:
            # Preserve an active exception: an ordinary close failure is
            # attached as secondary context and returning normally lets Python
            # re-raise the original exception.  A process-control interruption
            # from close is authoritative and is not converted.
            failures = CleanupFailures(exc)
            failures.run(self.close, ordinary=(OSError,))
            result = failures.complete()
            if result is not None:
                raise result
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

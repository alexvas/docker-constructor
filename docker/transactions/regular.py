"""L2 complete regular-file contracts.

Distinct operations rather than one configurable generic write:

* :meth:`RegularFileContracts.atomic_no_clobber` makes complete bytes with
  the final mode visible at a new destination without replacing an existing
  entry and claims no persistence after power loss.
* :meth:`RegularFileContracts.durable_no_clobber` additionally flushes the
  file before publication and the parent directory before success.
* :meth:`RegularFileContracts.durable_replace` flushes complete private
  sibling state, validates any existing destination, atomically replaces the
  destination with one ``renameat`` commit, and flushes the parent directory
  before success.
* :meth:`RegularFileContracts.validated_read` reads through one retained
  no-follow descriptor after validating the leaf.
* :meth:`RegularFileContracts.durable_unlink` removes one validated owned
  entry and flushes the parent directory before success.

A temporary-name collision is retried with a fresh private sibling name at
most three times and never reads, deletes, or replaces the collided entry.
Only the final no-clobber commit produces the typed :class:`DestinationExists`
outcome.  Ordinary cleanup and close failures never replace a primary failure,
while process-control interruptions (``KeyboardInterrupt`` and cancellation)
raised by cleanup propagate unchanged, and a private temporary name is cleared
only after it is safely committed or confirmed removed.

Single-file concurrency
-----------------------

L2 supplies the complete single-file mechanics: each operation validates the
entries it touches through retained descriptors and commits with one atomic
``renameat`` or ``unlinkat`` relative to the directory descriptor.  L2 does
not itself serialize concurrent side-effecting consumers.  An adopting layer
that replaces or removes entries alongside other writers must coordinate
through the design's Phase 2 namespace lock, which is intentionally not part
of this Phase 1 change.  Portable POSIX cannot conditionally replace a
previously validated inode, so L2 makes no race-free claim against a
same-user actor that mutates the namespace outside the adopting layer's
coordination.
"""
from __future__ import annotations

import errno
import itertools
import os
import stat
from dataclasses import dataclass

from .capabilities import DirectoryCapability
from .errors import (
    STAGE_ALLOCATE,
    STAGE_CLOSE,
    STAGE_COMMIT,
    STAGE_FSYNC_DIRECTORY,
    STAGE_FSYNC_FILE,
    STAGE_MODE,
    STAGE_READ,
    STAGE_REPLACE,
    STAGE_UNLINK,
    STAGE_VALIDATE,
    STAGE_VALIDATE_DESTINATION,
    STAGE_WRITE,
    DestinationExists,
    TransactionError,
    UnsafeFileError,
    attach_secondary,
)
from .posix import PosixFileOps

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)

_TEMP_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC
# O_NONBLOCK prevents a FIFO destination or unlink target from blocking the
# open before the descriptor can be validated as a non-regular file; it is a
# no-op for regular files and directories.
_VALIDATE_FLAGS = os.O_RDONLY | _NOFOLLOW | _CLOEXEC | _NONBLOCK

_TEMP_ATTEMPTS = 3
_TEMP_COUNTER = itertools.count()


def _temp_name() -> str:
    return f".transaction-{os.urandom(16).hex()}-{next(_TEMP_COUNTER)}"


@dataclass
class _Publication:
    fd: int = -1
    temp_name: str | None = None


class RegularFileContracts:
    """The complete L2 regular-file contracts over descriptor capabilities."""

    def __init__(self, ops: PosixFileOps | None = None) -> None:
        self._ops = ops if ops is not None else PosixFileOps()

    # -- atomic no-clobber ------------------------------------------------
    def atomic_no_clobber(
        self, directory: DirectoryCapability, name: str, data: bytes, mode: int
    ) -> None:
        base = directory.child_basename(name)
        state = _Publication()
        stage = STAGE_ALLOCATE
        try:
            state.fd, state.temp_name = self._allocate_temporary(directory)
            stage = STAGE_MODE
            self._establish_mode(state.fd, mode, base)
            stage = STAGE_WRITE
            self._write_all(state.fd, data, base)
            stage = STAGE_COMMIT
            self._close_fd(directory, state)
            self._link_no_clobber(directory, state, base)
            self._unlink_temporary(directory, state)
        except OSError as exc:
            primary = TransactionError(stage, f"cannot publish {base!r}", cause=exc)
            self._discard(directory, state, primary)
            raise primary from exc
        except BaseException as exc:
            self._discard(directory, state, exc)
            raise

    # -- durable no-clobber ----------------------------------------------
    def durable_no_clobber(
        self, directory: DirectoryCapability, name: str, data: bytes, mode: int
    ) -> None:
        base = directory.child_basename(name)
        state = _Publication()
        stage = STAGE_ALLOCATE
        try:
            state.fd, state.temp_name = self._allocate_temporary(directory)
            stage = STAGE_MODE
            self._establish_mode(state.fd, mode, base)
            stage = STAGE_WRITE
            self._write_all(state.fd, data, base)
            stage = STAGE_FSYNC_FILE
            self._fsync_file(directory, state)
            stage = STAGE_COMMIT
            self._close_fd(directory, state)
            self._link_no_clobber(directory, state, base)
            self._unlink_temporary(directory, state)
            stage = STAGE_FSYNC_DIRECTORY
            self._fsync_directory(directory, base)
        except OSError as exc:
            primary = TransactionError(stage, f"cannot publish {base!r}", cause=exc)
            self._discard(directory, state, primary)
            raise primary from exc
        except BaseException as exc:
            self._discard(directory, state, exc)
            raise

    # -- durable replacement ---------------------------------------------
    def durable_replace(
        self,
        directory: DirectoryCapability,
        name: str,
        data: bytes,
        mode: int,
    ) -> None:
        """Atomically replace *name* with complete durable *data*.

        Any existing destination is validated (regular, invoking-user-owned,
        single link) before the commit, and the commit is one atomic
        ``renameat``, so the destination name is never observably absent.
        """
        base = directory.child_basename(name)
        state = _Publication()
        stage = STAGE_ALLOCATE
        try:
            state.fd, state.temp_name = self._allocate_temporary(directory)
            stage = STAGE_MODE
            self._establish_mode(state.fd, mode, base)
            stage = STAGE_WRITE
            self._write_all(state.fd, data, base)
            stage = STAGE_FSYNC_FILE
            self._fsync_file(directory, state)
            stage = STAGE_VALIDATE_DESTINATION
            self._close_fd(directory, state)
            self._validate_destination(directory, base)
            stage = STAGE_REPLACE
            self._rename_basename(directory, state, base)
            stage = STAGE_FSYNC_DIRECTORY
            self._fsync_directory(directory, base)
        except OSError as exc:
            primary = TransactionError(stage, f"cannot replace {base!r}", cause=exc)
            self._discard(directory, state, primary)
            raise primary from exc
        except BaseException as exc:
            self._discard(directory, state, exc)
            raise

    # -- validated read --------------------------------------------------
    def validated_read(
        self,
        directory: DirectoryCapability,
        name: str,
        *,
        allowed_mode: int | None = None,
        require_single_link: bool = True,
    ) -> bytes:
        base = directory.child_basename(name)
        capability = None
        primary: BaseException | None = None
        try:
            capability = directory.open_regular(
                base,
                allowed_mode=allowed_mode,
                require_single_link=require_single_link,
            )
            try:
                return capability.read_all()
            except OSError as exc:
                raise TransactionError(
                    STAGE_READ, f"cannot read {base!r}", cause=exc
                ) from exc
        except BaseException as exc:
            primary = exc
            raise
        finally:
            if capability is not None:
                # Only I/O close failures are attached or translated; a
                # KeyboardInterrupt or cancellation propagates unchanged.  A
                # failed close is never retried: POSIX does not guarantee the
                # descriptor remains open, so a retry could close an unrelated
                # reused descriptor.
                try:
                    capability.close()
                except OSError as close_exc:
                    if primary is not None:
                        attach_secondary(primary, [close_exc])
                    else:
                        raise TransactionError(
                            STAGE_CLOSE, f"cannot close {base!r}", cause=close_exc
                        ) from close_exc

    # -- durable unlink --------------------------------------------------
    def durable_unlink(
        self,
        directory: DirectoryCapability,
        name: str,
        *,
        allow_absent: bool = False,
        allowed_mode: int | None = None,
    ) -> None:
        """Remove one validated owned entry.

        Validation happens through a retained no-follow descriptor that stays
        open until the basename is unlinked.  There is no restoration path: a
        failed unlink leaves whatever currently occupies the name untouched.
        """
        base = directory.child_basename(name)
        fd = -1
        primary: BaseException | None = None
        try:
            try:
                fd = self._ops.openat(directory.fd, base, _VALIDATE_FLAGS, 0)
            except FileNotFoundError as exc:
                if allow_absent:
                    self._fsync_directory(directory, base)
                    return
                raise TransactionError(
                    STAGE_VALIDATE, f"{base!r} is absent", cause=exc
                ) from exc
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise UnsafeFileError(
                        STAGE_VALIDATE, f"{base!r} is not a safe regular file", cause=exc
                    ) from exc
                raise TransactionError(
                    STAGE_READ, f"cannot open {base!r}", cause=exc
                ) from exc
            try:
                info = self._ops.fstat(fd)
            except OSError as exc:
                raise TransactionError(
                    STAGE_VALIDATE, f"cannot stat {base!r}", cause=exc
                ) from exc
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or (allowed_mode is not None and stat.S_IMODE(info.st_mode) != allowed_mode)
            ):
                raise UnsafeFileError(
                    STAGE_VALIDATE, f"{base!r} is not a safe owned regular file"
                )
            self._unlink_basename(directory, base)
            self._fsync_directory(directory, base)
        except BaseException as exc:
            primary = exc
            raise
        finally:
            if fd >= 0:
                # One close attempt only; see validated_read for why a failed
                # close is not retried.  Non-I/O exceptions propagate unchanged.
                try:
                    self._ops.close(fd)
                except OSError as close_exc:
                    if primary is not None:
                        attach_secondary(primary, [close_exc])
                    else:
                        raise TransactionError(
                            STAGE_CLOSE, f"cannot close {base!r}", cause=close_exc
                        ) from close_exc

    # -- private mechanics -----------------------------------------------
    def _allocate_temporary(self, directory: DirectoryCapability) -> tuple[int, str]:
        last: BaseException | None = None
        for _ in range(_TEMP_ATTEMPTS):
            temp_name = _temp_name()
            try:
                fd = self._ops.openat(directory.fd, temp_name, _TEMP_CREATE_FLAGS, 0o600)
            except FileExistsError as exc:
                # A collided private name belongs to someone else: never read,
                # unlink, or replace it; retry with a fresh unpredictable name.
                last = exc
                continue
            except OSError as exc:
                raise TransactionError(
                    STAGE_ALLOCATE,
                    f"cannot allocate private entry in {directory.label!r}",
                    cause=exc,
                ) from exc
            return fd, temp_name
        raise TransactionError(
            STAGE_ALLOCATE,
            f"exhausted private allocation attempts in {directory.label!r}",
            cause=last,
        )

    def _establish_mode(self, fd: int, mode: int, base: str) -> None:
        try:
            self._ops.fchmod(fd, mode)
        except OSError as exc:
            raise TransactionError(
                STAGE_MODE, f"cannot establish mode for {base!r}", cause=exc
            ) from exc

    def _write_all(self, fd: int, data: bytes, base: str) -> None:
        try:
            self._ops.write_all(fd, data)
        except OSError as exc:
            raise TransactionError(
                STAGE_WRITE, f"cannot fully write {base!r}", cause=exc
            ) from exc

    def _fsync_file(self, directory: DirectoryCapability, state: _Publication) -> None:
        try:
            self._ops.fsync(state.fd)
        except OSError as exc:
            raise TransactionError(
                STAGE_FSYNC_FILE, f"cannot flush private state in {directory.label!r}", cause=exc
            ) from exc

    def _fsync_directory(self, directory: DirectoryCapability, base: str) -> None:
        try:
            self._ops.fsync(directory.fd)
        except OSError as exc:
            raise TransactionError(
                STAGE_FSYNC_DIRECTORY,
                f"cannot synchronize {directory.label!r} for {base!r}",
                cause=exc,
            ) from exc

    def _link_no_clobber(
        self, directory: DirectoryCapability, state: _Publication, base: str
    ) -> None:
        assert state.temp_name is not None
        try:
            self._ops.linkat(directory.fd, state.temp_name, directory.fd, base)
        except FileExistsError as exc:
            raise DestinationExists(base, cause=exc) from exc
        except OSError as exc:
            raise TransactionError(
                STAGE_COMMIT, f"cannot publish {base!r}", cause=exc
            ) from exc

    def _rename_basename(
        self, directory: DirectoryCapability, state: _Publication, base: str
    ) -> None:
        assert state.temp_name is not None
        try:
            self._ops.renameat(directory.fd, state.temp_name, directory.fd, base)
        except OSError as exc:
            raise TransactionError(
                STAGE_REPLACE, f"cannot replace {base!r}", cause=exc
            ) from exc
        # The private name is only cleared once the atomic commit succeeded;
        # before that _discard still owns and cleans it.
        state.temp_name = None

    def _unlink_basename(self, directory: DirectoryCapability, base: str) -> None:
        try:
            self._ops.unlinkat(directory.fd, base)
        except OSError as exc:
            raise TransactionError(
                STAGE_UNLINK, f"cannot remove {base!r}", cause=exc
            ) from exc

    def _unlink_temporary(self, directory: DirectoryCapability, state: _Publication) -> None:
        if state.temp_name is None:
            return
        try:
            self._ops.unlinkat(directory.fd, state.temp_name)
        except FileNotFoundError:
            pass
        except OSError as exc:
            # Leave state.temp_name set so _discard can retry cleanup.
            raise TransactionError(
                STAGE_COMMIT, "cannot remove private temporary entry", cause=exc
            ) from exc
        state.temp_name = None

    def _validate_destination(self, directory: DirectoryCapability, base: str) -> None:
        """Reject an unsafe existing destination; absence is allowed."""
        try:
            fd = self._ops.openat(directory.fd, base, _VALIDATE_FLAGS, 0)
        except FileNotFoundError:
            return
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise UnsafeFileError(
                    STAGE_VALIDATE_DESTINATION,
                    f"destination {base!r} is not a safe owned regular file",
                    cause=exc,
                ) from exc
            raise TransactionError(
                STAGE_VALIDATE_DESTINATION,
                f"cannot validate destination {base!r}",
                cause=exc,
            ) from exc
        primary: BaseException | None = None
        try:
            try:
                info = self._ops.fstat(fd)
            except OSError as exc:
                raise TransactionError(
                    STAGE_VALIDATE_DESTINATION,
                    f"cannot stat destination {base!r}",
                    cause=exc,
                ) from exc
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
            ):
                raise UnsafeFileError(
                    STAGE_VALIDATE_DESTINATION,
                    f"destination {base!r} is not a safe owned regular file",
                )
        except BaseException as exc:
            primary = exc
            raise
        finally:
            # One close attempt only; see validated_read for why a failed close
            # is not retried.  Non-I/O exceptions propagate unchanged.
            try:
                self._ops.close(fd)
            except OSError as close_exc:
                if primary is not None:
                    attach_secondary(primary, [close_exc])
                else:
                    raise TransactionError(
                        STAGE_CLOSE,
                        f"cannot close destination {base!r}",
                        cause=close_exc,
                    ) from close_exc

    def _close_fd(self, directory: DirectoryCapability, state: _Publication) -> None:
        if state.fd < 0:
            return
        # Mark the descriptor released before the attempt: the transition means
        # "close attempted", not "close definitely succeeded".  A failed close
        # is not retried because POSIX does not guarantee the descriptor stays
        # open, so a retry could close an unrelated reused descriptor.
        fd, state.fd = state.fd, -1
        try:
            self._ops.close(fd)
        except OSError as exc:
            raise TransactionError(
                STAGE_CLOSE, f"cannot close private state in {directory.label!r}", cause=exc
            ) from exc

    def _discard(
        self, directory: DirectoryCapability, state: _Publication, primary: BaseException
    ) -> None:
        """Best-effort cleanup that preserves the primary failure.

        Ordinary ``OSError`` failures from ``close`` or ``unlinkat`` are
        collected and attached as secondary diagnostics to *primary*.  A
        process-control interruption (``KeyboardInterrupt`` or a
        cancellation-style ``BaseException``) is never consumed: it replaces
        *primary* and propagates unchanged.  A failed close is not retried,
        so the tracked descriptor is cleared before the attempt.
        """
        secondary: list[BaseException] = []
        if state.fd >= 0:
            fd, state.fd = state.fd, -1
            try:
                self._ops.close(fd)
            except OSError as exc:
                secondary.append(exc)
        if state.temp_name is not None:
            try:
                self._ops.unlinkat(directory.fd, state.temp_name)
            except FileNotFoundError:
                state.temp_name = None
            except OSError as exc:
                secondary.append(exc)
            else:
                state.temp_name = None
        attach_secondary(primary, secondary)

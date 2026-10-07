"""Generic owned and directory descriptor lifecycle.

``OwnedDescriptor`` exclusively owns one live descriptor until it transfers
that ownership with :meth:`OwnedDescriptor.detach` or attempts release with
:meth:`OwnedDescriptor.close`.  Release is an irreversible at-most-once
attempt: the capability marks itself released before invoking the underlying
close, never retries, and issues no operation once released or transferred.

``DirectoryDescriptor`` adds secure absolute-path walking, validated
adoption, and single-basename child directory operations on top of that
lifecycle.  It is not directly constructible: directory authority is granted
only through ``open_secure_path()`` or ``adopt()``.

``DescriptorError`` and ``UnsafeDescriptorError`` are the bounded foundation
errors carrying a stage and an optional raw cause.  The module is
domain-neutral: it derives no descendant paths, performs no recursion, and
defines no durability, locking, transaction, cache, or npm authority.  It
depends only on the standard library and the foundation's cleanup primitives.
"""
from __future__ import annotations

import errno
import os
import stat
from collections.abc import Callable
from typing import TYPE_CHECKING, Self

from docker.filesystem.cleanup import CleanupFailures

if TYPE_CHECKING:
    from docker.filesystem.operations import DescriptorOps

_STAGE_CONSTRUCTION = "construct-descriptor"
_STAGE_OPEN = "open-descriptor"
_STAGE_CREATE = "create-descriptor"
_STAGE_REOPEN = "reopen-descriptor"
_STAGE_RELEASE = "release-descriptor"
_STAGE_TRANSFER = "transfer-descriptor"
_STAGE_VALIDATE = "validate-descriptor"

_LIVE = "live"
_TRANSFERRED = "transferred"
_RELEASED = "released"

# The unexported authority token gates directory construction.  Only the
# module-internal seams hold it, so a direct ``DirectoryDescriptor(...)`` call
# is rejected before ownership transfers.
_AUTHORITY = object()
_DENIED = object()

_DIR_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


class DescriptorError(Exception):
    """A bounded descriptor operation or validation failure."""

    def __init__(
        self,
        stage: str,
        message: str,
        *,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.cause = cause
        if cause is not None:
            self.__cause__ = cause


class UnsafeDescriptorError(DescriptorError):
    """A descriptor is a symlink, non-directory, foreign-owned, or otherwise unsafe."""


def _validate_label(label: object) -> str:
    """Return *label* when it is a non-empty string, else raise.

    Factories resolve their effective label before touching the filesystem and
    call this first, so a rejected label can never strand an already-opened
    descriptor.  The public :meth:`DirectoryDescriptor.adopt` seam keeps its
    own semantics: an invalid label is refused before ownership transfers, so
    the caller retains the descriptor it passed in.
    """
    if not isinstance(label, str) or not label:
        raise DescriptorError(
            _STAGE_CONSTRUCTION,
            f"descriptor label must be a non-empty string, not {label!r}",
        )
    return label


class OwnedDescriptor:
    """Exclusively own one live descriptor until transfer or release."""

    __slots__ = ("_ops", "_fd", "_label", "_state")

    def __init__(self, ops: DescriptorOps, fd: int, *, label: str) -> None:
        if isinstance(fd, bool) or not isinstance(fd, int) or fd < 0:
            raise DescriptorError(
                _STAGE_CONSTRUCTION,
                f"descriptor fd must be a non-negative integer, not {fd!r}",
            )
        if not isinstance(label, str) or not label:
            raise DescriptorError(
                _STAGE_CONSTRUCTION,
                f"descriptor label must be a non-empty string, not {label!r}",
            )
        # A successful validation transfers ownership unconditionally.  No
        # filesystem validation happens here: this is the supported lifecycle
        # seam, and the directory capability performs its own checks.
        self._ops = ops
        self._fd = fd
        self._label = label
        self._state = _LIVE

    @property
    def fd(self) -> int:
        if self._state is not _LIVE:
            raise DescriptorError(
                _STAGE_RELEASE, f"descriptor {self._label!r} is not live"
            )
        return self._fd

    @property
    def label(self) -> str:
        return self._label

    @property
    def released(self) -> bool:
        return self._state is _RELEASED

    def close(self) -> None:
        # Mark release before invoking close: the transition means "close
        # attempted", not "close definitely succeeded".  POSIX does not
        # guarantee a failed close kept the descriptor open, so a retry could
        # close a reused descriptor; a second close is therefore a no-op that
        # issues no operation.
        if self._state is _LIVE:
            self._state = _RELEASED
            self._ops.close(self._fd)

    def detach(self) -> int:
        """Transfer the raw live descriptor to the caller without closing it."""
        if self._state is not _LIVE:
            raise DescriptorError(
                _STAGE_TRANSFER, f"descriptor {self._label!r} is not live"
            )
        self._state = _TRANSFERRED
        return self._fd

    def __enter__(self) -> Self:
        _ = self.fd
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc is not None:
            # Preserve the active failure: an ordinary close failure is
            # attached as secondary diagnostic context and returning normally
            # lets Python re-raise the original exception.  A process-control
            # interruption raised by close is authoritative and is not
            # converted.
            failures = CleanupFailures(exc)
            failures.run(self.close, ordinary=(OSError,))
            result = failures.complete()
            if result is not None:
                raise result
            return
        # No active exception: a close failure is the only failure and
        # propagates.
        self.close()


def _attempt_release(action: Callable[[], object], primary: BaseException) -> None:
    """Attempt *action* once, preserving *primary* under shared precedence.

    Returns normally when *primary* stays authoritative.  Raises when a
    process-control interruption or unexpected defect recorded while releasing
    outranks it, matching the Phase 1 cleanup policy.
    """
    failures = CleanupFailures(primary)
    failures.run(action, ordinary=(OSError,))
    result = failures.complete()
    if result is not None:
        raise result


def _open_failure(
    name: str, cause: OSError, *, stage: str = _STAGE_OPEN
) -> DescriptorError:
    """Translate an ordinary open failure into a bounded foundation error.

    *stage* records which generic operation issued the open (an initial or
    existing child open, or the reopen that follows an exclusive create), so a
    domain consumer can distinguish operations without inspecting the raw
    ``OSError``.
    """
    if cause.errno == errno.ELOOP:
        return UnsafeDescriptorError(
            stage, f"{name!r} is not a safe directory", cause=cause
        )
    return DescriptorError(stage, f"cannot open directory {name!r}", cause=cause)


class DirectoryDescriptor(OwnedDescriptor):
    """A validated directory that owns one live no-follow descriptor.

    Unlike :class:`OwnedDescriptor`, it is not directly constructible.  Its
    only public construction seams are :meth:`open_secure_path` and
    :meth:`adopt`; the initializer is guarded by an unexported authority
    token.  Every child operation accepts exactly one canonical basename and
    never reconstructs a descendant path.
    """

    __slots__ = ()

    def __init__(
        self,
        ops: DescriptorOps,
        fd: int,
        *,
        label: str,
        _authority: object = _DENIED,
    ) -> None:
        if _authority is not _AUTHORITY:
            raise TypeError(
                "DirectoryDescriptor must be created by open_secure_path() or adopt()"
            )
        # The ordinary argument validation lives in the base initializer and
        # runs only after the authority token is accepted, so a rejected direct
        # call never takes ownership of any descriptor.
        super().__init__(ops, fd, label=label)

    @classmethod
    def open_secure_path(
        cls,
        ops: DescriptorOps,
        path: str | os.PathLike[str],
        *,
        label: str | None = None,
        require_owner: bool = True,
    ) -> DirectoryDescriptor:
        """Open an absolute path by walking components without following links."""
        name = os.fspath(path)
        if not os.path.isabs(name):
            raise DescriptorError(
                _STAGE_OPEN, f"directory capability {name!r} requires an absolute path"
            )
        components = [part for part in name.split(os.sep) if part]
        # Reject an unusable label before the first descriptor is opened: a
        # late rejection by ``adopt`` would leak the already-opened root/leaf.
        target_label = _validate_label(label if label is not None else name)
        try:
            root_fd = ops.openat(None, os.sep, _DIR_FLAGS, 0)
        except OSError as exc:
            # No descriptor was returned, so there is nothing to release; the
            # failure is translated (and cause-chained) exactly like a child
            # open failure so callers never observe a raw ``OSError``.
            raise _open_failure(os.sep, exc) from exc
        if not components:
            return cls.adopt(
                ops, root_fd, label=target_label, require_owner=require_owner
            )
        # The retained parent is an ``OwnedDescriptor`` so every intermediate
        # release is explicit, at-most-once, and never retried.
        retained = OwnedDescriptor(ops, root_fd, label=os.sep)
        capability: DirectoryDescriptor | None = None
        primary: BaseException | None = None
        try:
            for index, component in enumerate(components):
                try:
                    child_fd = ops.openat(retained.fd, component, _DIR_FLAGS, 0)
                except OSError as exc:
                    raise _open_failure(component, exc) from exc
                if index == len(components) - 1:
                    # Adoption owns the child; on validation failure it closes
                    # the child once without retrying the retained parent.
                    capability = cls.adopt(
                        ops, child_fd, label=target_label, require_owner=require_owner
                    )
                else:
                    child = OwnedDescriptor(ops, child_fd, label=component)
                    previous, retained = retained, child
                    previous.close()
        except BaseException as exc:
            primary = exc
            raise
        finally:
            # The retained parent and the adopted leaf are independent
            # exactly-once releases.  The leaf is returnable only when the
            # parent closed cleanly and no primary occurred; otherwise it is
            # released once so it cannot leak.
            failures = CleanupFailures(primary)
            parent_closed = False

            def close_parent() -> None:
                nonlocal parent_closed
                retained.close()
                parent_closed = True

            failures.run(close_parent, ordinary=(OSError,))
            if capability is not None and (primary is not None or not parent_closed):
                failures.run(capability.close, ordinary=(OSError,))
            result = failures.complete()
            if result is not None:
                raise result
        if capability is None:  # pragma: no cover - components is non-empty
            raise AssertionError("secure walk must terminate by adopting a leaf")
        return capability

    @classmethod
    def adopt(
        cls,
        ops: DescriptorOps,
        fd: int,
        *,
        label: str,
        require_owner: bool = True,
    ) -> DirectoryDescriptor:
        """Consume *fd* and return its sole validated directory owner.

        Ownership transfers before directory and ownership validation.  If
        validation fails, the adopted descriptor receives exactly one release
        attempt under shared cleanup precedence and is not returned.
        """
        descriptor = cls(ops, fd, label=label, _authority=_AUTHORITY)
        try:
            descriptor._validate(require_owner=require_owner)
        except BaseException as exc:
            _attempt_release(descriptor.close, exc)
            raise
        return descriptor

    @classmethod
    def _transfer_validated(
        cls,
        ops: DescriptorOps,
        fd: int,
        *,
        label: str,
    ) -> DirectoryDescriptor:
        """Permanent module-internal validated-transfer seam.

        The caller must have already validated directory type and requested
        effective ownership of ``fd``.  Ownership transfers exactly once to the
        shared descriptor owner without a repeated validation pass and without
        the consuming release path of :meth:`adopt`, so a compatibility layer
        whose pre-transfer failure contract keeps ``fd`` caller-owned is never
        surprised by a consuming ``adopt()`` close.  Only directory
        compatibility adapters may call this seam.
        """
        return cls(ops, fd, label=label, _authority=_AUTHORITY)

    def _validate(self, *, require_owner: bool) -> None:
        try:
            info = self._ops.fstat(self.fd)
        except OSError as exc:
            raise DescriptorError(
                _STAGE_VALIDATE,
                f"cannot stat directory {self.label!r}",
                cause=exc,
            ) from exc
        if not stat.S_ISDIR(info.st_mode):
            raise UnsafeDescriptorError(
                _STAGE_VALIDATE,
                f"directory capability {self.label!r} is not a directory",
            )
        if require_owner and info.st_uid != os.geteuid():
            raise UnsafeDescriptorError(
                _STAGE_VALIDATE,
                f"directory capability {self.label!r} is not owned by the invoking user",
            )

    def child_basename(self, name: str) -> str:
        """Return *name* only when it is one canonical path component."""
        if (
            not isinstance(name, str)
            or not name
            or name in (".", "..")
            or "/" in name
            or "\x00" in name
            or os.sep in name
            or (os.altsep is not None and os.altsep in name)
        ):
            raise DescriptorError(_STAGE_VALIDATE, f"unsafe child basename: {name!r}")
        return name

    def _open_child_fd(self, base: str) -> int:
        """Open one no-follow child descriptor, leaving failures untranslated."""
        return self._ops.openat(self.fd, base, _DIR_FLAGS, 0)

    def _secure_and_adopt(
        self,
        fd: int,
        *,
        label: str,
        require_owner: bool,
        secure_mode: int | None,
    ) -> DirectoryDescriptor:
        """Validate an already-opened child, then secure and adopt it.

        Directory type and requested ownership are validated by adoption
        *before* any mode mutation, so a rejected child is never ``fchmod``'d.
        Adoption owns the descriptor once it succeeds; a failed adoption
        releases it once and a failed securing releases the adopted
        descriptor exactly once under shared cleanup precedence.
        """
        descriptor = DirectoryDescriptor.adopt(
            self._ops, fd, label=label, require_owner=require_owner
        )
        if secure_mode is not None:
            try:
                self._ops.fchmod(descriptor.fd, secure_mode)
            except OSError as exc:
                failure = DescriptorError(
                    _STAGE_OPEN,
                    f"cannot secure directory {label!r} to mode {secure_mode:#o}",
                    cause=exc,
                )
                _attempt_release(descriptor.close, failure)
                raise failure from exc
            except BaseException as exc:  # noqa: BLE001 - precedence is the point
                # A process-control interruption (KeyboardInterrupt/SystemExit)
                # or an unexpected defect while securing the child stays
                # primary, but the adopted descriptor is still released exactly
                # once through the shared cleanup precedence.
                _attempt_release(descriptor.close, exc)
                raise
        return descriptor

    def _open_validated_child(
        self,
        base: str,
        label: str,
        *,
        require_owner: bool,
        secure_mode: int | None,
        open_stage: str = _STAGE_OPEN,
    ) -> DirectoryDescriptor:
        try:
            fd = self._open_child_fd(base)
        except OSError as exc:
            raise _open_failure(base, exc, stage=open_stage) from exc
        return self._secure_and_adopt(
            fd,
            label=label,
            require_owner=require_owner,
            secure_mode=secure_mode,
        )

    def open_directory(
        self,
        name: str,
        *,
        label: str | None = None,
        require_owner: bool = True,
    ) -> DirectoryDescriptor:
        """Open one existing no-follow child directory."""
        base = self.child_basename(name)
        target_label = _validate_label(base if label is None else label)
        return self._open_validated_child(
            base,
            target_label,
            require_owner=require_owner,
            secure_mode=None,
        )

    def create_directory(
        self,
        name: str,
        *,
        mode: int,
        label: str | None = None,
        require_owner: bool = True,
    ) -> DirectoryDescriptor:
        """Create one child directory exclusively and validate it.

        The exclusive ``mkdirat`` failure is reported at the create stage and
        the open that follows a successful create at the reopen stage, so a
        consumer can tell creation apart from the subsequent open without
        inspecting the raw ``OSError``.
        """
        base = self.child_basename(name)
        target_label = _validate_label(base if label is None else label)
        try:
            self._ops.mkdirat(self.fd, base, mode)
        except OSError as exc:
            raise DescriptorError(
                _STAGE_CREATE, f"cannot create directory {base!r}", cause=exc
            ) from exc
        return self._open_validated_child(
            base,
            target_label,
            require_owner=require_owner,
            secure_mode=mode,
            open_stage=_STAGE_REOPEN,
        )

    def open_or_create_directory(
        self,
        name: str,
        *,
        mode: int,
        label: str | None = None,
        require_owner: bool = True,
    ) -> DirectoryDescriptor:
        """Open or create one child, securing it to *mode*, and validate it.

        Only an ``ENOENT`` from the initial child open triggers the creation
        fallback.  Any later failure while securing, validating, or releasing
        the opened child propagates unchanged, so a missing-entry error from
        ``fstat`` or ``fchmod`` is never mistaken for an absent child.
        """
        base = self.child_basename(name)
        target_label = _validate_label(base if label is None else label)
        try:
            fd = self._open_child_fd(base)
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise _open_failure(base, exc) from exc
            return self.create_directory(
                base,
                mode=mode,
                label=target_label,
                require_owner=require_owner,
            )
        return self._secure_and_adopt(
            fd,
            label=target_label,
            require_owner=require_owner,
            secure_mode=mode,
        )

    def stat_child(
        self, name: str, *, follow_symlinks: bool = False
    ) -> os.stat_result:
        """Stat one child entry under the requested symlink policy."""
        base = self.child_basename(name)
        return self._ops.statat(self.fd, base, follow_symlinks=follow_symlinks)

    def list_names(self) -> tuple[str, ...]:
        """Return the directory's entry names as a tuple."""
        return tuple(self._ops.listdir(self.fd))

    def unlink_child(self, name: str) -> None:
        """Unlink exactly one validated child entry."""
        base = self.child_basename(name)
        self._ops.unlinkat(self.fd, base)

    def remove_child_directory(self, name: str) -> None:
        """Remove exactly one validated empty child directory."""
        base = self.child_basename(name)
        self._ops.rmdirat(self.fd, base)

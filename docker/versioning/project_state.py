"""Private descriptor-relative external state keyed by a canonical project path."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from docker.transactions.capabilities import DirectoryCapability
from docker.transactions.errors import (
    DestinationExists,
    TransactionError,
    attach_secondary,
)
from docker.transactions.posix import PosixFileOps
from docker.transactions.regular import RegularFileContracts

from .cache_storage import prepare_default_root

_METADATA_VERSION = 1
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)


class ProjectStateError(ValueError):
    """Project state is absent, unsafe, or identifies another project."""


@dataclass(frozen=True)
class ProjectState:
    project_path: Path
    cache_root: Path
    namespace: Path
    identity: str
    generated_root: Path
    runtime_root: Path
    evidence_root: Path
    build_artifacts_root: Path
    transactions_root: Path


class ValidatedProjectState:
    """A re-verified ``ProjectState`` whose namespace stays open no-follow.

    The retained ``namespace_fd`` is kept open until :meth:`close` (or the
    end of a ``with`` block) so the caller can open namespace children
    descriptor-relatively without ever trusting the ``namespace`` pathname
    again.
    """

    def __init__(self, state: ProjectState, namespace_fd: int):
        self.state = state
        self.namespace_fd = namespace_fd
        self._closed = False

    def __enter__(self) -> "ValidatedProjectState":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # A namespace-close failure must never mask an active validation or
        # publication failure: it is attached as a secondary diagnostic and the
        # original exception keeps propagating.  ``_closed`` is set before the
        # POSIX close, so a failed close is never retried.  Only I/O close
        # failures are caught; a process-control interruption during close
        # propagates unchanged.
        if exc is None:
            self.close()
            return
        try:
            self.close()
        except OSError as close_exc:
            attach_secondary(exc, [close_exc])

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            os.close(self.namespace_fd)


_CHILD_NAMES = ("generated", "runtime", "evidence", "build-artifacts", "transactions")
"""Namespace children validated before any missing child is created."""

_CHILD_ATTRS = ("generated_root", "runtime_root", "evidence_root",
                "build_artifacts_root", "transactions_root")
"""``ProjectState`` field names, aligned with ``_CHILD_NAMES``."""


def _safe_basename(value: str) -> str:
    rendered = "".join(
        c if ("A" <= c <= "Z") or ("a" <= c <= "z") or ("0" <= c <= "9") or c in "._-" else "-"
        for c in value
    )
    return rendered.strip(".-") or "project"


def _project_identity(project: Path) -> str:
    """Authoritative identity: SHA-256 of the canonical path encoded as UTF-8."""
    return hashlib.sha256(str(project).encode("utf-8")).hexdigest()


def _metadata(project: Path, identity: str) -> bytes:
    return (json.dumps({"version": _METADATA_VERSION, "canonical_path": str(project),
                        "sha256": identity}, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _open_private_dir_at(parent_fd: int | None, name: str, *, label: str) -> int:
    """Open an existing private directory and validate it without repairing.

    The entry must already exist and be a directory owned by the invoking
    user with exactly mode ``0700``.  When ``parent_fd`` is ``None``,
    ``name`` is opened as an absolute path; otherwise it is a single path
    component opened no-follow relative to the verified parent descriptor.
    A missing entry raises :class:`FileNotFoundError`; any other open or
    validation failure raises :class:`ProjectStateError`.  The descriptor is
    closed on validation failure and returned on success.  Nothing is ever
    created, chmodded, or otherwise repaired.
    """
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise ProjectStateError(f"unsafe project state {label}") from exc
    try:
        st = os.fstat(fd)
    except OSError as exc:
        os.close(fd)
        raise ProjectStateError(f"unsafe project state {label}") from exc
    if (not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid()
            or stat.S_IMODE(st.st_mode) != 0o700):
        os.close(fd)
        raise ProjectStateError(
            f"project state {label} must be invoking-user-owned and mode 0700")
    return fd


def _ensure_private_dir_at(parent_fd: int, name: str, *, label: str) -> tuple[int, bool]:
    """Open or create a private directory, validating without repair.

    Existing entries are validated without modification.  A missing entry is
    created with mode ``0700`` (pinned explicitly so the ambient umask cannot
    strip owner permissions), then opened and validated like any other entry.
    Returns the validated descriptor and whether this call created the entry;
    pre-existing entries are never chmodded or otherwise repaired.
    """
    try:
        return _open_private_dir_at(parent_fd, name, label=label), False
    except FileNotFoundError:
        pass
    created = False
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass  # a concurrent resolver created it; validated below
    else:
        created = True
    if created:
        try:
            # Pin the freshly created entry to exactly 0700 before opening;
            # os.mkdir's mode is masked by the ambient umask, which can strip
            # owner permissions and leave the new directory unopenable.
            os.chmod(name, 0o700, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise ProjectStateError(f"unsafe project state {label}") from exc
    fd = _open_private_dir_at(parent_fd, name, label=label)
    if created:
        # Explicit owner-only mode on the retained descriptor, independent of
        # the process umask; pre-existing entries are never repaired.
        os.fchmod(fd, 0o700)
    return fd, created


def _read_metadata(namespace_fd: int, expected: bytes, label: str) -> None:
    try:
        fd = os.open("project.json", _FILE_FLAGS, dir_fd=namespace_fd)
    except FileNotFoundError:
        raise ProjectStateError(f"missing project identity metadata {label}/project.json") from None
    except OSError as exc:
        raise ProjectStateError(f"unsafe project identity metadata {label}/project.json") from exc
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid()
                or stat.S_IMODE(st.st_mode) != 0o600):
            raise ProjectStateError(f"unsafe project identity metadata {label}/project.json")
        parts: list[bytes] = []
        while chunk := os.read(fd, 64 * 1024):
            parts.append(chunk)
        if b"".join(parts) != expected:
            raise ProjectStateError(f"project identity metadata does not match {label}")
    finally:
        os.close(fd)


def _publish_metadata(namespace_fd: int, expected: bytes, label: str) -> None:
    """Publish new metadata without ever replacing an existing entry.

    The complete durable no-clobber mechanics — private sibling allocation,
    exact-mode establishment, file flush before publication, and parent
    directory flush before success — are delegated to the shared L2 contract.
    Only a typed final-destination collision is treated as the concurrent
    winner; the caller then reopens and verifies the winner's exact bytes, so
    an unsafe or mismatched entry is still rejected.  A collision whose own
    private-temporary cleanup failed is not a clean winner and is surfaced as
    :class:`ProjectStateError` with the collision and its cleanup diagnostics
    preserved.  Unexpected operational failures are surfaced as
    :class:`ProjectStateError` while the raw cause and exception chaining
    stay observable.
    """
    ops = PosixFileOps()
    try:
        directory = DirectoryCapability.from_fd(ops, namespace_fd, label)
        RegularFileContracts(ops).durable_no_clobber(
            directory, "project.json", expected, 0o600
        )
    except DestinationExists as exc:
        # A simultaneous resolver may have published the same immutable
        # metadata first.  A clean collision is the concurrent winner: the
        # caller reopens and verifies the winner's exact bytes.  A collision
        # whose private-temporary cleanup also failed must not be reported as
        # success - the leaked temporary entry would be silently discarded -
        # so it is surfaced as the publication diagnostic with the collision
        # and its attached cleanup diagnostics preserved as the cause.
        if exc.secondary:
            raise ProjectStateError(
                f"cannot publish project identity metadata {label}"
            ) from exc
        return
    except TransactionError as exc:
        raise ProjectStateError(
            f"cannot publish project identity metadata {label}"
        ) from exc
    except OSError as exc:
        # Capability creation (an operational ``fstat`` failure) is part of
        # the publication boundary: surface it as the domain diagnostic while
        # preserving the raw cause and its chaining.
        raise ProjectStateError(
            f"cannot publish project identity metadata {label}"
        ) from exc
    # The namespace descriptor is owned by the caller; the transient
    # capability is intentionally not closed here.


def resolve_project_state(project_root: str | Path, *, cache_root: str | Path | None = None,
                          create: bool = True, validate_children: bool = True) -> ProjectState:
    """Resolve project state through retained no-follow directory descriptors."""
    project = Path(project_root).resolve(strict=True)
    if not project.is_dir():
        raise ProjectStateError(f"constructor project is not a directory: {project}")
    identity = _project_identity(project)
    root = Path(cache_root) if cache_root is not None else prepare_default_root(
        os.environ.get("XDG_CACHE_HOME"), home=Path.home())
    namespace_name = f"{_safe_basename(project.name)}-{identity[:16]}"
    namespace = root / "projects" / namespace_name
    result = ProjectState(project, root, namespace, identity, namespace / "generated",
                          namespace / "runtime", namespace / "evidence",
                          namespace / "build-artifacts", namespace / "transactions")
    try:
        root_fd = _open_private_dir_at(None, os.fspath(root), label=f"cache root {root}")
    except FileNotFoundError:
        if not create:
            return result
        raise ProjectStateError(f"missing constructor cache root {root}") from None
    fds = [root_fd]
    try:
        try:
            projects_fd = _open_private_dir_at(root_fd, "projects", label=str(root / "projects"))
        except FileNotFoundError:
            if not create:
                return result
            projects_fd, _ = _ensure_private_dir_at(root_fd, "projects", label=str(root / "projects"))
        fds.append(projects_fd)
        namespace_created = False
        try:
            namespace_fd = _open_private_dir_at(projects_fd, namespace_name, label=str(namespace))
        except FileNotFoundError:
            if not create:
                return result
            namespace_fd, namespace_created = _ensure_private_dir_at(projects_fd, namespace_name, label=str(namespace))
        fds.append(namespace_fd)
        expected = _metadata(project, identity)
        if namespace_created:
            _publish_metadata(namespace_fd, expected, str(namespace))
        for attempt in range(20):
            try:
                _read_metadata(namespace_fd, expected, str(namespace))
                break
            except ProjectStateError:
                if attempt == 19:
                    raise
                time.sleep(0.005)
        if validate_children:
            missing: list[str] = []
            # First pass: inspect every existing child without creating
            # anything.  Symlinks, unsafe types, foreign ownership, or modes
            # other than 0700 are rejected before any creation occurs.
            for name in _CHILD_NAMES:
                try:
                    child_fd = _open_private_dir_at(
                        namespace_fd, name, label=str(namespace / name))
                except FileNotFoundError:
                    if create:
                        missing.append(name)
                        continue
                    raise
                fds.append(child_fd)
            # Second pass: only after every existing child passed validation,
            # create the missing children descriptor-relatively and no-follow.
            for name in missing:
                child_fd, _ = _ensure_private_dir_at(
                    namespace_fd, name, label=str(namespace / name))
                fds.append(child_fd)
        return result
    finally:
        for fd in reversed(fds):
            os.close(fd)


def validate_project_state(state: ProjectState) -> ValidatedProjectState:
    """Re-derive and re-verify a supplied ``ProjectState``.

    The constructor-project path is canonicalized, its complete SHA-256
    identity and safe-basename namespace name are recomputed, and every
    ``ProjectState`` field — project path, identity, cache root, namespace,
    and each direct namespace child — must match the recomputed values.
    Every path component from the cache root through ``projects`` and the
    recomputed namespace is then opened descriptor-relatively and no-follow,
    each required to be an invoking-user-owned ``0700`` directory, so a
    symlinked or replaced ancestor cannot redirect the namespace.
    ``project.json`` is re-read relative to the validated namespace
    descriptor (regular, invoking-user-owned, ``0600``, exact schema
    version/canonical path/complete identity).  On success the validated
    namespace descriptor is retained for the caller; ancestor descriptors
    are closed.
    """
    project = Path(state.project_path).resolve(strict=True)
    if not project.is_dir():
        raise ProjectStateError(f"constructor project is not a directory: {project}")
    if state.project_path != project:
        raise ProjectStateError(
            f"project state path {state.project_path!r} is not canonical ({project!r})")

    identity = _project_identity(project)
    if state.identity != identity:
        raise ProjectStateError(
            f"project state identity {state.identity!r} does not match {identity!r}")

    expected_name = f"{_safe_basename(project.name)}-{identity[:16]}"
    cache_root = Path(state.cache_root).resolve()
    if state.cache_root != cache_root:
        raise ProjectStateError(
            f"project state cache root {state.cache_root!r} is not canonical ({cache_root!r})")

    expected_namespace = cache_root / "projects" / expected_name
    if state.namespace != expected_namespace:
        raise ProjectStateError(
            f"project state namespace {state.namespace!r} does not match "
            f"the expected {expected_namespace!r}")

    for name, attr in zip(_CHILD_NAMES, _CHILD_ATTRS):
        expected_child = state.namespace / name
        actual_child = getattr(state, attr)
        if actual_child != expected_child:
            raise ProjectStateError(
                f"project state child {attr} {actual_child!r} is not the "
                f"expected namespace child {expected_child!r}")

    root_fd = projects_fd = namespace_fd = None
    try:
        root_fd = _open_private_dir_at(
            None, os.fspath(cache_root), label=f"cache root {cache_root}")
        projects_fd = _open_private_dir_at(
            root_fd, "projects", label=str(cache_root / "projects"))
        namespace_fd = _open_private_dir_at(
            projects_fd, expected_name, label=str(expected_namespace))
        _read_metadata(namespace_fd, _metadata(project, identity), str(state.namespace))
    except FileNotFoundError:
        if root_fd is None:
            raise ProjectStateError(f"missing constructor cache root {cache_root}") from None
        if projects_fd is None:
            raise ProjectStateError(f"missing project state {cache_root / 'projects'}") from None
        raise ProjectStateError(f"missing project namespace {expected_namespace}") from None
    except BaseException:
        if namespace_fd is not None:
            os.close(namespace_fd)
        raise
    finally:
        if projects_fd is not None:
            os.close(projects_fd)
        if root_fd is not None:
            os.close(root_fd)
    return ValidatedProjectState(state, namespace_fd)

"""Protected Runtime Extension Installer (Stage 10).

Reads the mounted effective runtime projection
(``/run/pi-cli/docker-constructor.runtime.toml``), verifies artifact
integrity, and installs Pi extensions idempotently into the mounted
Pi home directory.

This module SHALL NOT read:
  - ``docker-constructor.toml`` (reviewed inventory)
  - effective build projection
  - update providers
  - override policy
  - ``docker/versions.py``
"""

from __future__ import annotations

import base64
import enum
import hashlib
import hmac
import os
import stat
import sys
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from docker.filesystem.descriptors import OwnedDescriptor
from docker.filesystem.operations import PosixDescriptorOps


# ═══════════════════════════════════════════════════════════════════════
# Mounted artifact lookup (host-side, read-only)
# ═══════════════════════════════════════════════════════════════════════

# Single fixed root for content-addressed artifact blobs materialized on
# the host.  The installer SHALL NOT accept any other root — no env var,
# no constructor argument, no projection field.
_MOUNTED_ARTIFACT_ROOT: str = "/run/pi-cli/runtime-artifacts"


def _mounted_artifact_path(artifact_id: str) -> str:
    """Return the absolute host path for *artifact_id* under the
    single fixed :data:`_MOUNTED_ARTIFACT_ROOT`.

    The result is ``<root>/<artifact_id>``.  *artifact_id* MUST
    be a relative, non-traversal path matching the canonical
    ``<algo>/<digest>.tgz`` form.  Any violation — absolute path,
    ``..`` component, empty string — raises :class:`ProjectionError`
    before touching the filesystem.
    """
    if not artifact_id:
        raise ProjectionError("artifact_id must not be empty")
    if os.path.isabs(artifact_id):
        raise ProjectionError(
            f"artifact_id must be relative, got {artifact_id!r}",
        )
    # Reject ".." as a path component (normalised or not).
    parts = artifact_id.split(os.sep)
    if ".." in parts:
        raise ProjectionError(
            f"artifact_id must not contain '..': {artifact_id!r}",
        )
    # Reject a leading ".." without a separator ("../etc").
    if artifact_id.startswith(".."):
        raise ProjectionError(
            f"artifact_id must not start with '..': {artifact_id!r}",
        )
    return os.path.join(_MOUNTED_ARTIFACT_ROOT, artifact_id)


@runtime_checkable
class MountedBlobReader(Protocol):
    """Read-only, verified access to a materialized artifact blob.

    Implementations SHALL: derive the blob path from the single
    fixed root, open the file with ``O_NOFOLLOW``, verify the
    integrity digest against the streamed bytes, and return the
    complete blob contents as :class:`bytes`.  Any failure —
    missing blob, symlink, wrong permissions, digest mismatch —
    SHALL raise :class:`InstallError`.
    """

    def open_verified(
        self, *, artifact_id: str, integrity: str,
    ) -> bytes:
        """Open, verify, and return the blob identified by
        *artifact_id* and *integrity*.

        The caller SHALL NOT supply a root — the implementation
        uses :data:`_MOUNTED_ARTIFACT_ROOT` exclusively.
        """
        ...


class _MountInspection(Protocol):
    """Inspect whether a path resides on a read-only filesystem
    mount (bind-mount or otherwise).  Checking only Unix permission
    bits on the blob is insufficient — a ``0o600`` file on a
    writable mount can still be mutated by its owner."""

    def is_read_only_mount(self, path: str) -> bool:
        """Return ``True`` if *path* is on a read-only mount."""
        ...


class _StatvfsMountInspection:
    """Production mount inspection using :func:`os.statvfs`.

    Checks ``ST_RDONLY`` in the filesystem flags for the mount
    containing *path*."""

    def is_read_only_mount(self, path: str) -> bool:
        try:
            flags = os.statvfs(path).f_flag
        except OSError:
            return False
        return bool(flags & os.ST_RDONLY)


class RuntimeArtifactReader:
    """Production :class:`MountedBlobReader` that reads
    materialized blobs from :data:`_MOUNTED_ARTIFACT_ROOT`.

    Accepts an optional *mount_inspection* boundary for testing;
    the production default uses :class:`_StatvfsMountInspection`.
    """

    def __init__(
        self,
        *,
        mount_inspection: _MountInspection | None = None,
    ) -> None:
        self._mount_inspection: _MountInspection = (
            mount_inspection or _StatvfsMountInspection()
        )

    def open_verified(
        self, *, artifact_id: str, integrity: str,
    ) -> bytes:
        """Open, verify, and return the blob at the fixed root.

        Checks (in strict order):

        1. Derive and validate *artifact_id* → path.
        2. Identity agreement — *artifact_id* MUST match the
           canonical derivation from *integrity*.
        3. **Reject writable mount** — the blob path MUST reside
           on a read-only filesystem.
        4. Open once with ``O_NOFOLLOW``, verify regular file +
           owner-only permissions, accumulate every byte while
           streaming through SRI digest, verify the digest,
           then **return the same accumulated bytes** — no
           second open, no TOCTOU gap.

        All failures are raised as :class:`InstallError` or
        :class:`IntegrityError`; no generic ``AttributeError``,
        ``NotImplementedError``, or ``ValueError`` escapes.
        """
        _path = _mounted_artifact_path(artifact_id)

        # ── 1. Parse integrity (never leak ValueError) ───────
        algo: str
        raw_digest: str
        try:
            algo, raw_digest = integrity.split("-", 1)
        except ValueError:
            raise IntegrityError(
                f"malformed integrity string: {integrity!r}",
                algorithm="<none>",
                expected=integrity,
                actual="<malformed>",
            ) from None
        if algo not in ("sha256", "sha384", "sha512"):
            raise IntegrityError(
                f"unsupported integrity algorithm: {algo!r}",
                algorithm=algo,
                expected=integrity,
                actual="<unsupported>",
            )

        # ── 2. Identity agreement ─────────────────────────────
        safe_digest = raw_digest.replace("+", "-").replace("/", "_")
        canonical_id = f"{algo}/{safe_digest}.tgz"
        if not hmac.compare_digest(artifact_id, canonical_id):
            raise ProjectionError(
                f"artifact_id {artifact_id!r} does not match "
                f"integrity {integrity!r} (expected "
                f"{canonical_id!r})",
            )

        # ── 3. Reject writable mount ──────────────────────────
        if not self._mount_inspection.is_read_only_mount(_path):
            raise InstallError(
                "artifact mount is not read-only: " + _path,
            )

        # ── 4. Single open, verify, accumulate, return ────────
        blob_flags: int = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        _noatime: int = getattr(os, "O_NOATIME", 0)
        if not _noatime:
            raise InstallError(
                "O_NOATIME unavailable on this platform",
            )

        try:
            blob_fd = os.open(_path, blob_flags | _noatime)
        except PermissionError:
            # O_NOATIME requires ownership or CAP_FOWNER — in a
            # rootless container the host-owned blob may not be
            # owned by 'dev'.  Fall back to a plain O_RDONLY open;
            # the read-only bind mount already prevents atime
            # persistence.
            try:
                blob_fd = os.open(_path, blob_flags)
            except OSError as exc2:
                raise InstallError(
                    f"cannot open artifact at {_path}: {exc2}",
                ) from exc2
        except OSError as exc:
            raise InstallError(
                f"cannot open artifact at {_path}: {exc}",
            ) from exc

        ops = PosixDescriptorOps()
        owner = OwnedDescriptor(ops, blob_fd, label=f"runtime artifact {_path}")
        try:
            with owner:
                try:
                    blob_st = os.fstat(owner.fd)
                except OSError as exc:
                    raise InstallError(
                        f"cannot stat artifact at {_path}: {exc}",
                    ) from exc

                # ── regular file only ──
                if not stat.S_ISREG(blob_st.st_mode):
                    raise InstallError(
                        f"artifact at {_path} is not a regular file",
                    )

                # ── no group/world write ──
                if blob_st.st_mode & 0o022:
                    raise InstallError(
                        f"artifact at {_path} has group/world write bits",
                    )

                # ── accumulate + hash in a single pass ──
                hasher: "hashlib._Hash" = hashlib.new(algo)
                chunks: list[bytes] = []
                while True:
                    try:
                        chunk = os.read(owner.fd, 64 * 1024)
                    except OSError as exc:
                        raise InstallError(
                            f"read error on artifact at {_path}: {exc}",
                        ) from exc
                    if not chunk:
                        break
                    hasher.update(chunk)
                    chunks.append(chunk)

                actual_raw = base64.b64encode(hasher.digest()).decode("ascii")
                if not hmac.compare_digest(actual_raw, raw_digest):
                    raise IntegrityError(
                        f"integrity check failed for artifact at {_path}",
                        algorithm=algo,
                        expected=integrity,
                        actual=f"{algo}-{actual_raw}",
                    )

                # ── reassemble verified bytes ──
                verified = b"".join(chunks)
                if not verified:
                    raise InstallError(
                        f"artifact at {_path} is empty",
                    )
                return verified
        except OSError as exc:
            # A sole close failure on an otherwise verified artifact is
            # mapped at this domain boundary so callers that catch only
            # ``InstallError`` and the CLI/result path report a controlled
            # installation failure.  Active operation failures and
            # process-control interruptions never enter this branch.
            raise InstallError(
                f"cannot close artifact at {_path}: {exc}"
            ) from exc


# ═══════════════════════════════════════════════════════════════════════
# Shared URL identity validator
# ═══════════════════════════════════════════════════════════════════════


def _reject_path_traversal(value: str, *, field_name: str) -> None:
    """Validate that *value* is a safe relative path with no traversal.

    Rejects:

    * absolute paths (leading ``/`` or platform-absolute)
    * backslashes
    * empty segments (``//``, trailing ``/``, leading ``/``)
    * segments that are exactly ``.`` (same-dir)
    * segments that are exactly ``..`` (parent-dir)
    * normalized paths that escape the logical root (e.g. ``a/../../b``)

    The check is component-aware — ``".."`` is rejected only when it
    appears as a standalone path *segment*, not when it appears inside
    a longer name like ``"a..b"``.
    """
    import os.path

    exc: type[MetadataValidationError | ProjectionError]
    if field_name == "metadata_file":
        exc = MetadataValidationError
    else:
        exc = ProjectionError

    if not value or value.isspace():
        raise exc(f"{field_name} must not be empty")

    if os.path.isabs(value) or value.startswith("/"):
        raise exc(f"{field_name} must not be absolute: {value!r}")

    if "\\" in value:
        raise exc(
            f"{field_name} must not contain backslashes: {value!r}"
        )

    segments = value.split("/")
    for seg in segments:
        if seg == "":
            raise exc(
                f"{field_name} must not contain empty path segments "
                f"(leading/trailing/double slash): {value!r}"
            )
        if seg in (".", ".."):
            raise exc(
                f"{field_name} contains reserved path segment "
                f"{seg!r}: {value!r}"
            )

    # Final: normalized path must remain beneath its logical root.
    normalized = os.path.normpath(value)
    if normalized.startswith("..") or os.path.isabs(normalized):
        raise exc(
            f"{field_name} normalizes to escape root "
            f"({value!r} → {normalized!r})"
        )


# ═══════════════════════════════════════════════════════════════════════
# DTOs
# ═══════════════════════════════════════════════════════════════════════


class InstallStatus(enum.Enum):
    """Per-extension outcome."""
    OK = "ok"                       # installed and verified
    ALREADY_INSTALLED = "already_installed"  # no-op, already present
    FAILED = "failed"               # installation or verification failed
    PLANNED = "planned"             # dry-run: would install or reinstall


@dataclass(frozen=True)
class ExtensionResult:
    """Structured outcome for a single Pi extension."""
    package: str
    version: str
    status: InstallStatus
    detail: str | None = None       # error detail when status == FAILED


@dataclass(frozen=True)
class InstallResult:
    """Structured outcome of :func:`install_extensions`."""
    results: tuple[ExtensionResult, ...]
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return all(
            r.status != InstallStatus.FAILED
            for r in self.results
        )


@dataclass(frozen=True)
class ProjectionEntry:
    """A single extension entry read from the runtime projection."""
    package: str
    version: str
    artifact_id: str
    artifact_integrity: str
    metadata_file: str

    def __post_init__(self) -> None:
        import base64
        import re

        # -- package ---------------------------------------------------
        pkg = self.package
        _reject_path_traversal(pkg, field_name="package")
        if not pkg:
            raise ProjectionError("package name must not be empty")
        if pkg.startswith("@"):
            if not re.fullmatch(_SAFE_SCOPED_RE, pkg, re.VERBOSE):
                raise ProjectionError(
                    f"invalid scoped package name: {pkg!r}"
                )
        else:
            if "/" in pkg:
                raise ProjectionError(
                    f"unscoped package name must not contain '/': {pkg!r}"
                )
            if not re.fullmatch(_SAFE_PACKAGE_RE, pkg, re.VERBOSE):
                raise ProjectionError(
                    f"invalid package name: {pkg!r}"
                )

        # -- version ---------------------------------------------------
        ver = self.version
        from docker.versioning.semver import (
            SemverError,
            validate as _validate_semver,
        )
        try:
            _validate_semver(ver)
        except SemverError as exc:
            raise ProjectionError(f"version {exc} — want exact semver") from exc

        # -- artifact_id -----------------------------------------------
        art_id = self.artifact_id
        if not art_id:
            raise ProjectionError("artifact_id must not be empty")
        if os.path.isabs(art_id):
            raise ProjectionError(
                f"artifact_id must be relative: {art_id!r}",
            )
        parts = art_id.split(os.sep)
        if ".." in parts or art_id.startswith(".."):
            raise ProjectionError(
                f"artifact_id must not contain '..': {art_id!r}",
            )
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ProjectionError(
                f"artifact_id must be '<algo>/<digest>.tgz': {art_id!r}",
            )

        # -- artifact_integrity ----------------------------------------
        integ = self.artifact_integrity
        m = re.fullmatch(
            r"(sha(?:256|384|512))-(.+)", integ,
        )
        if not m:
            raise ProjectionError(
                f"artifact integrity must be 'shaNNN-<base64>': "
                f"{integ!r}"
            )
        try:
            digest = base64.b64decode(m.group(2), validate=True)
        except Exception:
            raise ProjectionError(
                f"artifact integrity has invalid base64: {integ!r}"
            ) from None
        expected_len = {"sha256": 32, "sha384": 48, "sha512": 64}
        if len(digest) != expected_len[m.group(1)]:
            raise ProjectionError(
                f"artifact integrity digest length {len(digest)} "
                f"!= {expected_len[m.group(1)]} for {m.group(1)}: "
                f"{integ!r}"
            )

        # -- metadata_file ---------------------------------------------
        _reject_path_traversal(self.metadata_file, field_name="metadata_file")


# -- package-name safety regexes (module level, not DTO fields) ---------

_SAFE_PACKAGE_RE = r"""\A
    (?!\.)                          # no leading dot
    [a-z0-9_\-](?:[a-z0-9_\-.]*[a-z0-9_\-])?  # unscoped-npm-name
    \Z
    """

_SAFE_SCOPED_RE = r"""\A
    @
    (?!\.)                          # scope: no leading dot
    [a-z0-9_\-](?:[a-z0-9_\-.]*[a-z0-9_\-])?  # scope-name
    /
    (?!\.)                          # name: no leading dot
    [a-z0-9_\-](?:[a-z0-9_\-.]*[a-z0-9_\-])?  # package-name
    \Z
    """


# ═══════════════════════════════════════════════════════════════════════
# Injectable boundaries (Protocols)
# ═══════════════════════════════════════════════════════════════════════


class MountChecker(Protocol):
    """Verify that *path* is a mount point."""

    def is_mount(self, path: str) -> bool:
        ...


class PackageInstaller(Protocol):
    """Install a Pi extension from already-verified artifact bytes.

    *artifact_bytes* contains the exact bytes whose integrity was
    verified against the projection.  The installer MUST NOT download
    bytes, query registries, or re-open a filesystem path that could
    have been replaced between verification and installation.

    Returns ``None`` on success or raises :class:`InstallError`.
    """

    def install(self, *, package: str, artifact_bytes: bytes) -> None:
        ...


class MetadataReader(Protocol):
    """Read installed package metadata from disk.

    *metadata_file* is the validated relative path from the
    projection (e.g. ``"package.json"``, ``"nested/pkg/package.json"``).
    The caller resolves it against *pi_home*.

    Raises:
        :class:`MetadataNotFoundError`: The package is genuinely
            absent — no metadata file exists.  This is the *only*
            error that triggers a fresh install.
        :class:`InstallError`: Read, parse, or permission failure.
            These are surfaced immediately as installation failures
            and never trigger a download.
    """

    def read(self, *, pi_home: str, metadata_file: str, package: str) -> dict[str, object]:
        ...


class PrivilegeContext(Protocol):
    """Execution-identity and ownership-validation boundary.

    The installer runs as ``dev`` inside the container and must
    verify that identity before performing any mutation.  After
    installing files into *pi_home* it performs a *read-only*
    validation that the installed artefacts are owned by
    ``dev:dev``.  Actual ``chown`` is the responsibility of the
    root entrypoint (Stage 10.4) and happens before the privilege
    drop — ``dev`` cannot mutate ownership.
    """

    def verify_user(self, expected: str) -> None:
        """Raise :class:`InstallError` if the current process owner
        does not match *expected*."""
        ...

    def validate_owner(self, path: str, expected_owner: str) -> None:
        """Read-only check: the file at *path* is owned by *expected_owner*.

        Called after installation (and after the root entrypoint has
        repaired ownership).  Must not attempt ``chown`` — the
        installer process cannot change ownership.

        Raises :class:`InstallError` if ownership does not match.
        """
        ...


@dataclass(frozen=True)
class InstallContext:
    """Injectable boundary implementations for the installer."""

    mount_check: MountChecker
    blob_reader: MountedBlobReader
    installer: PackageInstaller
    metadata: MetadataReader
    privilege: PrivilegeContext

    # ------------------------------------------------------------------
    # Real implementations (used when not injected by tests)
    # ------------------------------------------------------------------

    @staticmethod
    def real_mount_check() -> MountChecker:
        import os
        import stat as st

        class _RealMountCheck:
            @staticmethod
            def is_mount(path: str) -> bool:
                """Return True if *path* is a mount point, False otherwise.

                Uses ``mountpoint -q`` because bind mounts share
                ``st_dev`` with their parent, making device-ID
                comparison unreliable.
                """
                import subprocess
                result = subprocess.run(
                    ["mountpoint", "-q", "--", path],
                    capture_output=True,
                )
                return result.returncode == 0

        return _RealMountCheck()

    @staticmethod
    def real_installer() -> PackageInstaller:
        import io
        import os
        import tarfile

        class _RealPackageInstaller:
            def install(self, package: str, artifact_bytes: bytes) -> None:
                """Install an npm package from verified tarball bytes.

                Extracts the gzipped-tar *artifact_bytes* directly
                into the npm node_modules tree under the fixed Pi
                home.  Npm tarballs use ``package/`` as the
                top-level directory — that prefix is stripped so
                files land at ``<pi_home>/agent/npm/node_modules/
                <package>/...``.

                No temporary files, subprocess, or mutable disk
                staging — the bytes flow from memory through
                :mod:`tarfile` into the final directory in a single
                pass.
                """
                target_dir = os.path.join(
                    _FIXED_PI_HOME, "agent", "npm",
                    "node_modules", package,
                )
                os.makedirs(target_dir, exist_ok=True)
                try:
                    with tarfile.open(
                        fileobj=io.BytesIO(artifact_bytes),
                        mode="r:gz",
                    ) as tf:
                        for member in tf.getmembers():
                            # Npm tarballs are rooted at 'package/';
                            # strip that prefix so files land directly
                            # in <target_dir>.
                            parts = member.name.split("/", 1)
                            if len(parts) < 2 or parts[0] != "package":
                                continue
                            member.name = parts[1]
                            tf.extract(member, target_dir, filter="data")
                except (tarfile.TarError, OSError) as exc:
                    raise InstallError(
                        f"failed to extract {package!r}: {exc}"
                    ) from exc

        return _RealPackageInstaller()

    @staticmethod
    def real_metadata() -> MetadataReader:
        import json
        import os

        class _RealMetadataReader:
            def read(
                self,
                *,
                pi_home: str,
                metadata_file: str,
                package: str,
            ) -> dict[str, object]:
                """Read and parse installed package metadata.

                Raises :class:`MetadataNotFoundError` when the
                metadata file genuinely does not exist.
                Raises :class:`InstallError` for read, parse,
                or permission failures.
                """
                npm_root = os.path.join(
                    pi_home, "agent", "npm", "node_modules",
                )
                pkg_dir = os.path.join(npm_root, package)
                meta_path = os.path.join(pkg_dir, metadata_file)
                try:
                    with open(meta_path, "rb") as fh:
                        raw = json.loads(fh.read())
                except FileNotFoundError:
                    raise MetadataNotFoundError(
                        f"metadata not found for {package!r} at {meta_path!r}"
                    ) from None
                except json.JSONDecodeError as exc:
                    raise InstallError(
                        f"invalid JSON in {meta_path!r}: {exc}"
                    ) from exc
                except PermissionError as exc:
                    raise InstallError(
                        f"permission denied reading {meta_path!r}: {exc}"
                    ) from exc
                except OSError as exc:
                    raise InstallError(
                        f"failed to read {meta_path!r}: {exc}"
                    ) from exc
                if not isinstance(raw, dict):
                    raise InstallError(
                        f"metadata at {meta_path!r} is not a JSON object"
                    )
                return raw

        return _RealMetadataReader()

    @staticmethod
    def real_privilege() -> PrivilegeContext:
        import os
        import pwd
        import stat as st

        class _RealPrivilegeContext:
            def verify_user(self, expected: str) -> None:
                """Raise :class:`InstallError` if the current process
                user does not match *expected*."""
                try:
                    pw = pwd.getpwuid(os.getuid())
                except KeyError:
                    raise InstallError(
                        f"current uid {os.getuid()} has no passwd entry"
                    )
                if pw.pw_name != expected:
                    raise InstallError(
                        f"running as {pw.pw_name!r}, must be {expected!r}"
                    )

            def validate_owner(self, path: str, expected_owner: str) -> None:
                """Read-only check: the file at *path* is owned by
                *expected_owner* (format ``"user:group"``)."""
                try:
                    st_result = os.lstat(path)
                except OSError as exc:
                    raise InstallError(
                        f"cannot stat {path!r}: {exc}"
                    ) from exc

                user, _, group = expected_owner.partition(":")

                import grp
                try:
                    owner_name = pwd.getpwuid(st_result.st_uid).pw_name
                except KeyError:
                    owner_name = str(st_result.st_uid)
                try:
                    group_name = grp.getgrgid(st_result.st_gid).gr_name
                except KeyError:
                    group_name = str(st_result.st_gid)

                actual = f"{owner_name}:{group_name}"
                if actual != expected_owner:
                    raise InstallError(
                        f"{path!r} is owned by {actual!r}, "
                        f"expected {expected_owner!r}"
                    )

        return _RealPrivilegeContext()

    @staticmethod
    def make_real() -> "InstallContext":
        """Create an :class:`InstallContext` wired to real system
        boundaries (mount-point check, mounted-artifact reader,
        pi install, user/group introspection)."""
        return InstallContext(
            mount_check=InstallContext.real_mount_check(),
            blob_reader=RuntimeArtifactReader(),
            installer=InstallContext.real_installer(),
            metadata=InstallContext.real_metadata(),
            privilege=InstallContext.real_privilege(),
        )


# ═══════════════════════════════════════════════════════════════════════
# Domain errors
# ═══════════════════════════════════════════════════════════════════════


class InstallError(RuntimeError):
    """A recoverable installation failure with structured detail."""


class MetadataNotFoundError(InstallError):
    """The requested package is not installed — no metadata file exists.

    This is distinct from read/parse/permission failures:
    it signals that a fresh install is expected and safe.  All
    other :class:`InstallError` subclasses during metadata
    pre-check are surfaced immediately without invoking
    download or install."""


class IntegrityError(InstallError):
    """Checksum verification failed.

    Fields:
        algorithm: ``"sha256"``, ``"sha384"``, or ``"sha512"``.
        expected:  Hex-encoded expected digest.
        actual:    Hex-encoded actual digest.
    """
    def __init__(
        self,
        message: str,
        *,
        algorithm: str,
        expected: str,
        actual: str,
    ) -> None:
        super().__init__(message)
        self.algorithm = algorithm
        self.expected = expected
        self.actual = actual


class ProjectionError(InstallError):
    """The mounted projection TOML is structurally invalid."""


class MetadataValidationError(InstallError):
    """The projection ``metadata_file`` path is unsafe."""


# ═══════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════


def _require_str(value: object, section: str, field: str) -> str:
    """Require *value* to be a :class:`str`, raising
    :class:`ProjectionError` with a focused diagnostic otherwise."""
    if not isinstance(value, str):
        raise ProjectionError(
            f"[extensions.{section}].{field} must be "
            f"a string, not {type(value).__name__}"
        )
    return value


def read_projection(path: str) -> list[ProjectionEntry]:
    """Read and validate the effective runtime projection from *path*.

    Returns a list of :class:`ProjectionEntry` objects in
    deterministic (sorted-by-name) order.

    Raises:
        ProjectionError: the TOML is structurally invalid.
        MetadataValidationError: a ``metadata_file`` value is unsafe.
        IntegrityError: an SRI integrity string is malformed or
            uses an unsupported algorithm.
    """
    import sys

    if sys.version_info >= (3, 11):
        import tomllib
    else:
        import tomli as tomllib  # type: ignore[no-redef]

    raw: dict[str, object]
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except FileNotFoundError:
        raise ProjectionError(f"projection file not found: {path!r}") from None
    except Exception as exc:
        raise ProjectionError(
            f"failed to parse projection TOML: {exc}"
        ) from None

    # ── closed schema: root table ─────────────────────────────────
    allowed_root = {"extensions"}
    unknown_root = set(raw) - allowed_root
    if unknown_root:
        raise ProjectionError(
            f"unknown top-level key(s) in runtime projection: "
            f"{sorted(unknown_root)!r}"
        )

    ext_table = raw.get("extensions")
    if not isinstance(ext_table, dict):
        raise ProjectionError(
            "[extensions] must be a TOML table"
        )
    ext_table = raw["extensions"]

    # ── allowed per-extension and per-artifact keys ────────────────
    # The host projection serializer is authoritative and writes the
    # selected artifact identity as flat extension fields.  Retain the
    # old nested shape as a compatibility input for already-published
    # projections, but never allow both representations in one entry.
    flat_artifact_fields = {"artifact_id", "integrity"}
    allowed_ext = {
        "package", "version", "metadata_file", "artifact",
        *flat_artifact_fields,
    }
    allowed_artifact = flat_artifact_fields

    entries: list[ProjectionEntry] = []
    for ext_name, ext_val in ext_table.items():
        if not isinstance(ext_val, dict):
            raise ProjectionError(
                f"[extensions.{ext_name!s}] must be a TOML table"
            )

        unknown_ext = set(ext_val) - allowed_ext
        if unknown_ext:
            raise ProjectionError(
                f"[extensions.{ext_name!s}]: unknown key(s) "
                f"{sorted(unknown_ext)!r}"
            )

        # ── validate selected artifact representation ─────────────
        has_legacy_artifact = "artifact" in ext_val
        has_flat_artifact = bool(set(ext_val) & flat_artifact_fields)
        if has_legacy_artifact and has_flat_artifact:
            raise ProjectionError(
                f"[extensions.{ext_name!s}] must use either flat "
                "artifact_id/integrity fields or legacy artifact table, "
                "not both"
            )
        if has_legacy_artifact:
            artifact_raw = ext_val["artifact"]
            if not isinstance(artifact_raw, dict):
                raise ProjectionError(
                    f"[extensions.{ext_name!s}].artifact must be a TOML inline table"
                )
            unknown_artifact = set(artifact_raw) - allowed_artifact
            if unknown_artifact:
                raise ProjectionError(
                    f"[extensions.{ext_name!s}].artifact: unknown key(s) "
                    f"{sorted(unknown_artifact)!r}"
                )
            artifact_prefix = "artifact."
        elif has_flat_artifact:
            artifact_raw = ext_val
            artifact_prefix = ""
        else:
            raise ProjectionError(
                f"[extensions.{ext_name!s}]: missing artifact_id/integrity"
            )

        # ── construct validated DTO ───────────────────────────────
        try:
            section = str(ext_name)

            # Require every projected value to already be a string —
            # silently coercing ints, floats, etc. would mask TOML
            # authoring mistakes.
            pkg = _require_str(
                ext_val["package"], section, "package",
            )
            ver = _require_str(
                ext_val["version"], section, "version",
            )
            meta = _require_str(
                ext_val["metadata_file"], section, "metadata_file",
            )
            art_id = _require_str(
                artifact_raw["artifact_id"], section,
                f"{artifact_prefix}artifact_id",
            )
            integrity = _require_str(
                artifact_raw["integrity"], section,
                f"{artifact_prefix}integrity",
            )

            entry = ProjectionEntry(
                package=pkg,
                version=ver,
                artifact_id=art_id,
                artifact_integrity=integrity,
                metadata_file=meta,
            )
        except KeyError as exc:
            raise ProjectionError(
                f"[extensions.{ext_name!s}]: missing required key {exc}"
            ) from exc

        entries.append(entry)

    entries.sort(key=lambda e: e.package)

    # ── reject duplicate package identities ────────────────────────
    for i in range(1, len(entries)):
        if entries[i].package == entries[i - 1].package:
            raise ProjectionError(
                f"duplicate package in projection: "
                f"{entries[i].package!r}"
            )

    return entries


def install_extensions(
    ctx: InstallContext,
    *,
    entries: list[ProjectionEntry],
    pi_home: str,
    dry_run: bool = False,
) -> InstallResult:
    """Install Pi extensions into *pi_home*.

    Operates in strict order per entry:
      1. mount check + user-identity verification (once, before any work)
      2. for each extension:
         a. metadata check (skip if already correctly installed)
         b. download (unverified bytes)
         c. **integrity verification** (installer-owned, over file bytes)
         d. install from verified local artifact path
         e. post-install metadata match
         f. read-only owner validation (dev:dev)

    Integrity verification is performed by this module between
    download and install — the downloader never sees the expected
    checksum and the installer never fetches bytes.

    Reads no files beyond *entries* and *pi_home*.  All I/O goes
    through the injected *ctx* boundaries.

    The temp workspace is cleaned up on **all** exit paths,
    including :class:`KeyboardInterrupt`, :class:`SystemExit`,
    and any other :class:`BaseException`.

    Returns a structured :class:`InstallResult`.
    """

    results: list[ExtensionResult] = []
    workspace_dir: str | None = None

    # ── 1. Mount check + user identity ───────────────────────
    if not ctx.mount_check.is_mount(pi_home):
        raise InstallError(
            f"pi-home is not a mount point: {pi_home!r}"
        )
    ctx.privilege.verify_user("dev")

    if dry_run:
        planned: list[ExtensionResult] = []
        for e in entries:
            try:
                installed = ctx.metadata.read(
                    pi_home=pi_home,
                    metadata_file=e.metadata_file,
                    package=e.package,
                )
            except MetadataNotFoundError:
                # Package genuinely absent — would install fresh.
                planned.append(ExtensionResult(
                    package=e.package,
                    version=e.version,
                    status=InstallStatus.PLANNED,
                ))
                continue
            except InstallError:
                # Read/parse/permission failure — surface without
                # mutation; do not swallow.
                raise

            pkg_name = installed.get("name")
            pkg_version = installed.get("version")
            if pkg_name == e.package and pkg_version == e.version:
                # Name + version match — validate ownership before
                # reporting ALREADY_INSTALLED.
                metadata_path = _npm_metadata_path(
                    pi_home, e.package, e.metadata_file,
                )
                try:
                    ctx.privilege.validate_owner(
                        metadata_path, "dev:dev",
                    )
                except InstallError as exc:
                    raise InstallError(
                        f"owner validation failed for {e.package}: {exc}"
                    ) from exc
                planned.append(ExtensionResult(
                    package=e.package,
                    version=e.version,
                    status=InstallStatus.ALREADY_INSTALLED,
                ))
            else:
                # Name/version mismatch — would reinstall.
                planned.append(ExtensionResult(
                    package=e.package,
                    version=e.version,
                    status=InstallStatus.PLANNED,
                ))
        return InstallResult(results=tuple(planned), dry_run=True)

    # ── 2. Install each extension via mounted blob reader ────
    for entry in entries:
        try:
            result = _install_one(ctx, entry, pi_home)
            results.append(result)
        except InstallError as exc:
            results.append(ExtensionResult(
                package=entry.package,
                version=entry.version,
                status=InstallStatus.FAILED,
                detail=f"[{entry.package}@{entry.version}] {exc}",
            ))
            # First failure stops subsequent mutations
            break

    return InstallResult(results=tuple(results))


def _install_one(
    ctx: InstallContext,
    entry: ProjectionEntry,
    pi_home: str,
) -> ExtensionResult:
    """Install (or skip) a single extension via the mounted
    blob reader — no download, no workspace, no mutable file path."""

    # ── 2a. Metadata pre-check ────────────────────────────────────
    try:
        installed = ctx.metadata.read(
            pi_home=pi_home,
            metadata_file=entry.metadata_file,
            package=entry.package,
        )
    except MetadataNotFoundError:
        pass  # not installed — proceed to fresh install
    except InstallError:
        # Read / parse / permission failures — surface immediately.
        raise
    else:
        pkg_name = installed.get("name")
        pkg_version = installed.get("version")
        if pkg_name == entry.package and pkg_version == entry.version:
            metadata_path = _npm_metadata_path(
                pi_home, entry.package, entry.metadata_file,
            )
            try:
                ctx.privilege.validate_owner(metadata_path, "dev:dev")
            except InstallError as exc:
                raise InstallError(
                    f"owner validation failed for {entry.package}: {exc}"
                ) from exc
            return ExtensionResult(
                package=entry.package,
                version=entry.version,
                status=InstallStatus.ALREADY_INSTALLED,
            )
        # Name/version mismatch — fall through to reinstall

    # ── 2b. Open verified blob from read-only mount ───────────────
    content = ctx.blob_reader.open_verified(
        artifact_id=entry.artifact_id,
        integrity=entry.artifact_integrity,
    )

    # ── 2c. Install from verified bytes ──────────────────────────
    try:
        ctx.installer.install(
            package=entry.package,
            artifact_bytes=content,
        )
    except InstallError:
        raise
    except Exception as exc:
        raise InstallError(
            f"install failed for {entry.package} ({entry.version}): {exc}"
        ) from exc

    # ── 2d. Post-install metadata verification ───────────────────
    try:
        new_meta = ctx.metadata.read(
            pi_home=pi_home,
            metadata_file=entry.metadata_file,
            package=entry.package,
        )
    except InstallError as exc:
        raise InstallError(
            f"post-install metadata read failed for {entry.package} "
            f"({entry.version}): {exc}"
        ) from exc

    new_name = new_meta.get("name")
    new_version = new_meta.get("version")
    if new_name != entry.package or new_version != entry.version:
        raise InstallError(
            f"post-install metadata mismatch for {entry.package}: "
            f"expected name={entry.package!r} version={entry.version!r}, "
            f"got name={new_name!r} version={new_version!r}"
        )

    # ── 2e. Owner validation ─────────────────────────────────────
    metadata_path = _npm_metadata_path(
        pi_home, entry.package, entry.metadata_file,
    )
    try:
        ctx.privilege.validate_owner(metadata_path, "dev:dev")
    except InstallError as exc:
        raise InstallError(
            f"owner validation failed for {entry.package}: {exc}"
        ) from exc

    return ExtensionResult(
        package=entry.package,
        version=entry.version,
        status=InstallStatus.OK,
    )


def _npm_metadata_path(pi_home: str, package: str, metadata_file: str) -> str:
    """Resolve the installed metadata path in the Pi CLI npm layout."""
    import os
    return os.path.join(
        pi_home, "agent", "npm", "node_modules", package, metadata_file,
    )


# ═══════════════════════════════════════════════════════════════════════
# Exit-code mapping (shell wrapper owned)
# ═══════════════════════════════════════════════════════════════════════

_EXIT_OK = 0
_EXIT_MOUNT = 10
_EXIT_PROJECTION = 11
_EXIT_INTEGRITY = 12
_EXIT_INSTALL = 13
_EXIT_VERIFY = 14
_EXIT_USAGE = 2


def exit_code_for(result: InstallResult | InstallError | Exception) -> int:
    """Map an installer outcome to a process exit code.

    Shell wrappers call this after :func:`install_extensions`.
    """
    if isinstance(result, InstallResult):
        return _EXIT_OK if result.ok else _EXIT_INSTALL
    if isinstance(result, InstallError):
        return _EXIT_INSTALL
    return _EXIT_USAGE


# ═══════════════════════════════════════════════════════════════════════
# CLI entry point
# ═══════════════════════════════════════════════════════════════════════

_FIXED_PROJECTION = "/run/pi-cli/docker-constructor.runtime.toml"
_FIXED_PI_HOME = "/home/dev/.pi"


def main(argv: list[str] | None = None) -> int:
    """Thin CLI entry point invoked by the shell wrapper.

    Accepts the fixed runtime projection path and Pi home;
    exposes ``--dry-run`` for pre-flight inspection.
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Protected runtime extension installer",
    )
    parser.add_argument(
        "command",
        choices=("install",),
        default="install",
        nargs="?",
        help="Command (default: install)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate projection and inspect metadata; do not download or install",
    )

    args = parser.parse_args(argv)

    try:
        entries = read_projection(_FIXED_PROJECTION)
    except ProjectionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return _EXIT_PROJECTION

    if not entries:
        print("Nothing to install.", file=sys.stderr)
        return _EXIT_OK

    ctx = InstallContext.make_real()

    try:
        result = install_extensions(
            ctx,
            entries=entries,
            pi_home=_FIXED_PI_HOME,
            dry_run=args.dry_run,
        )
    except InstallError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return _EXIT_INSTALL

    if result.dry_run:
        ok_count = sum(
            1 for r in result.results
            if r.status == InstallStatus.ALREADY_INSTALLED
        )
        planned = sum(
            1 for r in result.results
            if r.status == InstallStatus.PLANNED
        )
        print(
            f"Dry-run: {ok_count} already installed, "
            f"{planned} would be installed",
        )
        return _EXIT_OK if result.ok else _EXIT_INSTALL

    if not result.ok:
        for r in result.results:
            if r.status == InstallStatus.FAILED:
                print(f"FAILED: {r.detail}", file=sys.stderr)
        return _EXIT_INSTALL
    return _EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

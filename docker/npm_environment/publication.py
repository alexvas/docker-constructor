"""Locking, non-authoritative lookup, and atomic immutable publication.

This module is the Phase 5 orchestration layer above the Phase 2/3/4
primitives:

* :func:`identity_coordination_lock` holds a private input-identity lock
  across lookup, assembly, validation, and publication;
* the input-identity index is non-authoritative — membership alone never
  establishes a cache hit, and index corruption never fails assembly;
* :func:`verify_output` fully re-verifies a candidate (no-follow tree
  verification plus recomputation of the input identity, tree digest,
  evidence digest, and output identity) before reuse;
* :func:`publish_environment` validates the assembled tree, strips write
  bits, rebuilds the canonical tree manifest, derives evidence and the
  assembled output identity, and atomically publishes the immutable
  environment under its output identity; and
* :func:`assemble_environment` ties it together behind the injected Docker
  executor, reusing a fully verified cache hit without npm or network
  execution.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import stat as _stat
import uuid
from pathlib import Path
from typing import Iterator, Sequence

from .assembler import npm_policy_flags
from .errors import LockedNpmError
from .evidence import (
    AssemblerEvidence,
    AssemblerEvidenceBody,
    AssemblyResult,
    compute_assembled_output_identity,
    evidence_body_digest,
    parse_evidence,
    serialize_evidence,
)
from .execution import (
    CleanupFailure,
    _attach_cleanup_notes,
    _remove_staging_safely,
    assemble,
)
from .identity import AssemblerIdentity, compute_assembler_input_identity
from .model import ValidatedAssemblyInput
from .network import CorporateNetworkPolicy
from .observability import NULL_ACTIVITY, AssemblyActivity
from .run_vector import recheck_assembler_bindings
from .storage import (
    AssemblerNamespace,
    prepare_assembler_namespace,
    prepare_identity_lock,
    remove_staging_workspace,
)
from .tree import (
    TreeManifest,
    build_tree_manifest,
    parse_manifest,
    serialize_manifest,
    verify_tree,
)
from .validation import make_tree_read_only, validate_assembled_tree

TREE_CHILD = "tree"
MANIFEST_FILE = "manifest.json"
EVIDENCE_FILE = "evidence.json"

_DIR_FLAGS = (
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
)
_HEX = frozenset("0123456789abcdef")


def _is_hex64(value: str) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        c in _HEX for c in value
    )


class _AlreadyPublished(Exception):
    """Internal signal that the output identity already exists."""


def output_path(namespace: AssemblerNamespace, output_identity: str) -> Path:
    """Return the lexical immutable-output path for *output_identity*."""
    if not _is_hex64(output_identity):
        raise LockedNpmError(
            "unsafe_output_path", f"invalid output identity {output_identity!r}"
        )
    return namespace.outputs / output_identity


def index_path(namespace: AssemblerNamespace, input_identity: str) -> Path:
    """Return the lexical non-authoritative index file path."""
    if not _is_hex64(input_identity):
        raise LockedNpmError(
            "unsafe_index_path", f"invalid input identity {input_identity!r}"
        )
    return namespace.index / f"{input_identity}.json"


@contextlib.contextmanager
def identity_coordination_lock(
    namespace: AssemblerNamespace, input_identity: str
) -> Iterator[None]:
    """Hold a private exclusive input-identity lock for one assembly.

    The lock file is prepared owner-private ``0600`` beneath the namespace
    lock directory and then locked with ``flock`` so concurrent assemblies
    of the same input identity serialize lookup, assembly, validation, and
    publication.
    """
    lock_path = prepare_identity_lock(namespace, input_identity)
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(lock_path), flags)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ── durable filesystem helpers ──────────────────────────────────────────


def _durable_write(path: Path, data: bytes, mode: int = 0o444) -> None:
    """Write *data* in full to a new *path*, fsync-ing only once complete.

    ``os.write`` may perform a partial write, so the payload is written in a
    loop until every byte has been handed to the kernel; ``fsync`` runs only
    after the complete payload has been written.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path), flags, mode)
    try:
        remaining = memoryview(data)
        while remaining:
            written = os.write(fd, remaining)
            if written == 0:
                raise OSError("os.write() returned 0 bytes")
            remaining = remaining[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: str | Path) -> None:
    fd = os.open(str(path), _DIR_FLAGS)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_tree(root: Path, manifest: TreeManifest) -> None:
    """fsync every regular file and directory (and the root) before rename."""
    for entry in manifest.entries:
        path = root / entry.path
        if entry.kind == "file":
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(str(path), flags)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        elif entry.kind == "directory":
            fd = os.open(str(path), _DIR_FLAGS)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    _fsync_dir(root)


def _make_writable(root: Path, manifest: TreeManifest) -> None:
    """Restore write bits so a read-only published tree can be removed."""
    for entry in manifest.entries:
        if entry.kind not in ("file", "directory"):
            continue
        path = root / entry.path
        try:
            st = os.lstat(path)
        except OSError:
            continue
        add = 0o300 if entry.kind == "directory" else 0o200
        os.chmod(
            path,
            _stat.S_IMODE(st.st_mode) | add,
            follow_symlinks=False,
        )
    try:
        st = os.lstat(root)
        os.chmod(
            root,
            _stat.S_IMODE(st.st_mode) | 0o300,
            follow_symlinks=False,
        )
    except OSError:
        pass


def _remove_redundant_tree(root: Path, manifest: TreeManifest) -> None:
    """Delete a redundant read-only source tree left after a collision.

    ``make_tree_read_only`` has already stripped write bits, so owner
    write/search permissions are restored first (using the manifest-aware
    helper), then only *root* is removed.  The committed immutable output is
    never touched.
    """
    _make_writable(root, manifest)
    shutil.rmtree(str(root))


def _quarantine_corrupt_output(
    namespace: AssemblerNamespace, output_identity: str
) -> Path | None:
    """Move a corrupt committed output aside for inspection, or ``None``.

    Only the single ``outputs/<output_identity>`` directory is moved; its
    bytes are never deleted, followed, or replaced.  The identity is
    validated via :func:`output_path` and every filesystem operation is
    no-follow (``lstat`` rejects symlinks, ``rename`` acts on the directory
    entry itself).  Returns the quarantine path on success, or ``None`` when
    the path is absent or not a plain directory (left untouched).

    Callers must hold the input-identity coordination lock, which
    :func:`assemble_environment` guarantees across publication.
    """
    final = output_path(namespace, output_identity)  # validates identity
    try:
        st = os.lstat(str(final))
    except OSError:
        return None
    if not _stat.S_ISDIR(st.st_mode):  # rejects symlinks (lstat) and files
        return None
    quarantine = namespace.outputs / (
        f".corrupt-{output_identity[:16]}-{uuid.uuid4().hex[:8]}"
    )
    try:
        os.rename(str(final), str(quarantine))
    except OSError:
        return None
    try:
        _fsync_dir(namespace.outputs)
    except OSError:
        pass
    return quarantine


def _atomic_publish(
    namespace: AssemblerNamespace,
    output_identity: str,
    tree_root: Path,
    manifest: TreeManifest,
    evidence_bytes: bytes,
) -> Path:
    """Durably publish *tree_root* at ``outputs/<output_identity>``.

    The tree, manifest, and evidence are fully written and fsync-ed inside a
    temporary sibling directory before a single atomic rename publishes the
    immutable output.  A pre-existing output is never overwritten: it is
    reported as :class:`_AlreadyPublished` for collision verification.
    """
    final = output_path(namespace, output_identity)
    if os.path.lexists(final):
        raise _AlreadyPublished()

    outputs_fd = os.open(str(namespace.outputs), _DIR_FLAGS)
    try:
        tmp_name = f".tmp-{output_identity[:16]}-{uuid.uuid4().hex[:8]}"
        try:
            os.mkdir(tmp_name, 0o700, dir_fd=outputs_fd)
        except FileExistsError as exc:
            raise LockedNpmError(
                "unsafe_output_path",
                f"temporary publication directory {tmp_name!r} already exists",
            ) from exc
        tmp = namespace.outputs / tmp_name
        try:
            os.rename(str(tree_root), str(tmp / TREE_CHILD))
            os.chmod(str(tmp / TREE_CHILD), 0o555)
            _durable_write(tmp / MANIFEST_FILE, serialize_manifest(manifest))
            _durable_write(tmp / EVIDENCE_FILE, evidence_bytes)
            _fsync_tree(tmp / TREE_CHILD, manifest)
            os.chmod(str(tmp), 0o555)
            _fsync_dir(tmp)
            try:
                os.rename(str(tmp), str(final))
            except OSError as exc:
                if os.path.lexists(final):
                    raise _AlreadyPublished() from exc
                raise
            os.fsync(outputs_fd)
            return final
        except BaseException:
            if os.path.lexists(tmp):
                os.chmod(str(tmp), 0o700)
                _make_writable(tmp / TREE_CHILD, manifest)
                shutil.rmtree(str(tmp), ignore_errors=True)
            raise
    finally:
        os.close(outputs_fd)


# ── non-authoritative input index ───────────────────────────────────────


def read_index(namespace: AssemblerNamespace, input_identity: str) -> tuple[str, ...]:
    """Return the output identities referenced for *input_identity*.

    The index is non-authoritative: a missing or malformed file returns an
    empty tuple rather than failing, because index membership alone never
    establishes a cache hit.
    """
    path = index_path(namespace, input_identity)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return ()
    try:
        entries = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ()
    if not isinstance(entries, list):
        return ()
    seen: list[str] = []
    for entry in entries:
        if _is_hex64(entry) and entry not in seen:
            seen.append(entry)
    return tuple(seen)


def _append_index(
    namespace: AssemblerNamespace, input_identity: str, output_identity: str
) -> None:
    """Best-effort append of *output_identity* to the input-identity index.

    Failure never fails publication: the index is advisory and a missing
    entry only costs a later recomputation.
    """
    entries = list(read_index(namespace, input_identity))
    if output_identity not in entries:
        entries.append(output_identity)
    entries.sort()
    payload = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    path = index_path(namespace, input_identity)
    tmp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex[:8]}")
    try:
        _durable_write(tmp, payload, mode=0o600)
        os.rename(str(tmp), str(path))
        _fsync_dir(namespace.index)
    except OSError:
        try:
            if os.path.lexists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


# ── verification and cache-hit selection ────────────────────────────────


def _make_result(
    evidence: AssemblerEvidence, environment_root: Path, evidence_path: Path
) -> AssemblyResult:
    assembler = evidence.input_identity.assembler
    return AssemblyResult(
        environment_root=environment_root,
        evidence_path=evidence_path,
        output_identity=evidence.output_identity,
        input_identity=evidence.input_identity,
        tree_digest=evidence.tree_digest,
        evidence_digest=evidence.evidence_digest,
        roots=evidence.input_identity.roots,
        root_metadata=evidence.body.root_metadata,
        packages=evidence.body.packages,
        omitted_optionals=evidence.body.omitted_optionals,
        integrity_less=evidence.body.integrity_less,
        image_digest=assembler.image_digest,
        node_version=assembler.node_version,
        npm_version=assembler.npm_version,
        script_digest=assembler.script_digest,
        policy_digest=assembler.policy_digest,
        platform=assembler.platform,
        lockfile_digest=evidence.input_identity.lockfile_digest,
        npm_policy_flags=evidence.body.npm_policy_flags,
        tree_entries=evidence.body.tree_entries,
    )


def verify_output(
    namespace: AssemblerNamespace,
    output_identity: str,
    *,
    input_identity,
) -> AssemblyResult | None:
    """Fully verify one candidate output identity and return it, or ``None``.

    A candidate is reused only after the stored evidence and manifest parse
    without substitution, the evidence binds the requested input identity,
    the no-follow tree verification passes, and the tree digest, evidence
    digest, and output identity all recompute.  Any corruption, substitution,
    or mismatch returns ``None`` (a cache miss) — never a failed assembly.
    """
    if not _is_hex64(output_identity):
        return None
    out = output_path(namespace, output_identity)
    evidence_path = out / EVIDENCE_FILE
    manifest_path = out / MANIFEST_FILE
    tree = out / TREE_CHILD
    try:
        evidence = parse_evidence(evidence_path.read_bytes())
        manifest = parse_manifest(manifest_path.read_bytes())
    except (OSError, LockedNpmError, ValueError):
        return None
    if evidence.output_identity != output_identity:
        return None
    if evidence.input_identity.digest != input_identity.digest:
        return None
    if evidence.input_identity != input_identity:
        return None
    if evidence.tree_digest != manifest.digest:
        return None
    if evidence.body.tree_entries != manifest.entries:
        return None
    try:
        verify_tree(tree, manifest)
    except LockedNpmError:
        return None
    recomputed = compute_assembled_output_identity(
        input_identity, evidence.tree_digest, evidence.evidence_digest
    )
    if recomputed.digest != output_identity:
        return None
    return _make_result(evidence, tree, evidence_path)


def find_cached_result(
    *, namespace: AssemblerNamespace, input_identity
) -> AssemblyResult | None:
    """Select and fully verify one indexed output identity, if any.

    Index membership is non-authoritative: each candidate is completely
    verified and only a fully matching output is returned.  Candidates that
    are corrupted or substituted are skipped.
    """
    for output_identity in read_index(namespace, input_identity.digest):
        result = verify_output(
            namespace, output_identity, input_identity=input_identity
        )
        if result is not None:
            return result
    return None


# ── publication ─────────────────────────────────────────────────────────


def publish_environment(
    *,
    validated: ValidatedAssemblyInput,
    tree_root: str | Path,
    namespace: AssemblerNamespace,
    input_identity,
    activity: AssemblyActivity | None = None,
) -> AssemblyResult:
    """Validate, seal, and atomically publish one assembled environment.

    Returns the consumer-neutral result.  A collision (an already-published
    output with the same identity) is never overwritten in place: the
    existing output is fully verified and returned, its input index healed,
    and the redundant source tree removed.  A corrupt collision (an output
    that fails verification) is quarantined — never followed or deleted —
    and the validated reconstruction is republished at the same identity;
    if the quarantine cannot be performed safely or the replacement fails,
    the corrupt bytes are preserved and publication fails without
    overwriting any committed output.
    """
    root = Path(tree_root)
    scoped = activity if activity is not None else NULL_ACTIVITY
    with scoped.step("validation"):
        pre_manifest = validate_assembled_tree(validated, root)
        make_tree_read_only(root, pre_manifest)
        final_manifest = build_tree_manifest(root)

    body = AssemblerEvidenceBody(
        input_identity=input_identity,
        tree_digest=final_manifest.digest,
        tree_entries=final_manifest.entries,
        packages=validated.packages,
        omitted_optionals=validated.omitted_optionals,
        integrity_less=validated.integrity_less,
        root_metadata=validated.root_metadata,
        npm_policy_flags=npm_policy_flags(),
    )
    evidence_digest = evidence_body_digest(body)
    output_identity = compute_assembled_output_identity(
        input_identity, final_manifest.digest, evidence_digest
    )
    evidence = AssemblerEvidence(
        output_identity=output_identity.digest,
        input_identity=input_identity,
        tree_digest=final_manifest.digest,
        evidence_digest=evidence_digest,
        body=body,
    )
    evidence_bytes = serialize_evidence(evidence)

    def _publish_once() -> AssemblyResult:
        try:
            published = _atomic_publish(
                namespace, output_identity.digest, root, final_manifest, evidence_bytes
            )
        except _AlreadyPublished:
            existing = verify_output(
                namespace, output_identity.digest, input_identity=input_identity
            )
            if existing is not None:
                # Verified collision: heal the non-authoritative input index so
                # a later lookup can find this output, then drop the redundant
                # staging tree.  The committed immutable output is untouched.
                _append_index(
                    namespace, input_identity.digest, output_identity.digest
                )
                if os.path.lexists(root):
                    try:
                        _remove_redundant_tree(root, final_manifest)
                    except BaseException as exc:
                        raise LockedNpmError(
                            "collision_cleanup_failed",
                            f"existing output {output_identity.digest!r} verified "
                            "successfully, but removing the redundant staging tree "
                            f"{root} failed: {type(exc).__name__}: {exc}; mutable or "
                            "redundant residue may remain",
                        ) from exc
                return existing

            # Corrupt collision: the existing bytes do not verify.  Quarantine
            # only that single output directory, then republish the already
            # validated reconstruction.  If quarantine cannot be performed
            # safely, the corrupt bytes stay in place and publication fails
            # without overwriting anything.
            if (
                _quarantine_corrupt_output(namespace, output_identity.digest)
                is None
            ):
                raise LockedNpmError(
                    "output_collision_corrupt",
                    f"output identity {output_identity.digest!r} already exists "
                    "but does not verify and could not be safely quarantined; "
                    "immutable outputs are never overwritten",
                ) from None
            # The reconstruction is already validated.  If this replacement
            # fails, the quarantined corrupt bytes remain preserved.
            published = _atomic_publish(
                namespace, output_identity.digest, root, final_manifest, evidence_bytes
            )

        _append_index(namespace, input_identity.digest, output_identity.digest)
        return _make_result(
            evidence, published / TREE_CHILD, published / EVIDENCE_FILE
        )

    with scoped.step("publication"):
        return _publish_once()

    raise AssertionError("publication scope must return or raise")


# ── orchestration ───────────────────────────────────────────────────────


def assemble_environment(
    *,
    validated: ValidatedAssemblyInput,
    assembler: AssemblerIdentity,
    cache_root: str | Path,
    executor,
    uid: int | None = None,
    gid: int | None = None,
    secrets: Sequence[str] = (),
    corporate_network: CorporateNetworkPolicy | None = None,
    sink=None,
    stream_factory=None,
    tail_projector=None,
    activity: AssemblyActivity | None = None,
) -> AssemblyResult:
    """Assemble (or reuse) one locked npm environment and publish it.

    Rechecks all bindings before effects, holds the input-identity
    coordination lock across non-authoritative lookup, assembly, validation,
    and publication, and returns the consumer-neutral result.  A fully
    verified cache hit returns without npm, network, or Docker execution.

    On any publication failure (including cancellation), the staging
    workspace is removed.  A cleanup failure — including cancellation
    during cleanup — is recorded as a structured :class:`CleanupFailure`
    and attached as a note on the primary publication exception, which is
    always re-raised unchanged.  Previously committed immutable outputs are
    never touched.
    """
    recheck_assembler_bindings(assembler)
    input_identity = compute_assembler_input_identity(validated, assembler)
    namespace = prepare_assembler_namespace(cache_root, assembler.digest)
    staging_name = input_identity.digest

    policy_secrets = (
        corporate_network.secrets() if corporate_network is not None else ()
    )
    effective_secrets = tuple(secrets) + policy_secrets
    scoped = activity if activity is not None else NULL_ACTIVITY

    with contextlib.ExitStack() as lock_stack:
        # Enter the coordination lock while the lock-wait activity is active,
        # then end LOCK_WAIT immediately after acquisition and retain the lock
        # for the remaining non-authoritative lookup, assembly, validation, and
        # publication work.  The retained lock is released when that work ends.
        with scoped.step("lock_wait"):
            lock_stack.enter_context(
                identity_coordination_lock(namespace, input_identity.digest)
            )

        with scoped.step("cache_lookup"):
            cached = find_cached_result(
                namespace=namespace, input_identity=input_identity
            )
        if cached is not None:
            scoped.cache_reuse()
            return cached

        # Securely replace any abandoned same-input staging left by a prior
        # interrupted run: no-follow removal under the lock, never adopted
        # as a completed environment.  Unsafe entries fail closed.
        with scoped.step("stale_stage_cleanup"):
            remove_staging_workspace(namespace, staging_name)

        run = assemble(
            validated=validated,
            assembler=assembler,
            cache_root=cache_root,
            executor=executor,
            uid=uid,
            gid=gid,
            secrets=secrets,
            corporate_network=corporate_network,
            sink=sink,
            stream_factory=stream_factory,
            tail_projector=tail_projector,
            activity=activity,
        )
        try:
            return publish_environment(
                validated=validated,
                tree_root=run.staging,
                namespace=namespace,
                input_identity=input_identity,
                activity=activity,
            )
        except BaseException as exc:
            # ``publish_environment`` moves the staging tree on success; on
            # failure the staging workspace may still exist.  Remove it using
            # the same structured cleanup model as container execution: the
            # cleanup failure (if any) is attached as a note, and the
            # original publication failure stays primary.
            failure = _remove_staging_safely(
                namespace,
                staging_name,
                run.staging,
                effective_secrets,
                tail_projector=tail_projector,
            )
            if failure is not None:
                _attach_cleanup_notes(exc, [failure])
            raise

    raise AssertionError("locked-assembly scope must return or raise")

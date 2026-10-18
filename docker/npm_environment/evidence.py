"""Canonical assembler evidence, assembled output identity, and result DTOs.

After output validation, the assembler derives three digests:

* the canonical output-tree digest (already produced by the tree manifest);
* the canonical assembler-evidence-body digest — computed over an
  output-identity-free evidence body; and
* the assembled output identity, canonically derived from the tuple
  ``(assembler input identity, tree digest, evidence-body digest)``.

The immutable evidence envelope and the consumer-neutral result bind the
input identity, the tree digest, the evidence digest, and the output
identity.  Every DTO is frozen, and serialization is canonical (key-sorted
JSON) so the same assembled content always yields the same evidence bytes.
Parsing re-verifies every digest and rejects substituted tree/evidence
pairs rather than trusting the serialized claims.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .assembler import npm_policy_flags
from .errors import LockedNpmError
from .identity import (
    AssemblerIdentity,
    AssemblerInputIdentity,
    compute_assembler_identity,
    input_identity_digest,
)
from .model import (
    IntegrityLessNode,
    LockPackage,
    OmittedOptional,
    ReviewedRootMetadata,
    RootSpec,
)
from .tree import TreeEntry, canonical_tree_digest

_HEX = frozenset("0123456789abcdef")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _to_jsonable(value: object) -> object:
    """Convert a frozen DTO graph into plain JSON-able values."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _to_jsonable(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, tuple):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, list):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    raise TypeError(f"cannot canonically serialize {type(value).__name__}")


def canonical_json_bytes(obj: object) -> bytes:
    """Return the canonical, key-sorted JSON encoding of *obj*."""
    return json.dumps(_to_jsonable(obj), sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


@dataclass(frozen=True)
class AssemblerEvidenceBody:
    """Output-identity-free evidence content.

    This is the canonical body whose SHA-256 digest is the
    ``assemblerEvidenceDigest``.  It deliberately has no output-identity
    field so the digest derivation is acyclic.
    """

    input_identity: AssemblerInputIdentity
    """The exact assembler input identity this evidence was produced from."""

    tree_digest: str
    """Canonical output-tree digest."""

    tree_entries: tuple[TreeEntry, ...]
    """Canonical output-tree hashes (every published entry)."""

    packages: tuple[LockPackage, ...]
    """Complete validated package closure."""

    omitted_optionals: tuple[OmittedOptional, ...]
    """Explicit platform-inapplicable optional omissions."""

    integrity_less: tuple[IntegrityLessNode, ...]
    """Explicit integrity-less registry-node records."""

    root_metadata: tuple[ReviewedRootMetadata, ...]
    """Keyed reviewed-root ``bin``/``engines.node`` metadata."""

    npm_policy_flags: tuple[str, ...]
    """The fixed npm policy flags used by the assembler."""


def evidence_body_digest(body: AssemblerEvidenceBody) -> str:
    """Return the canonical SHA-256 digest of *body*."""
    return _sha256_hex(canonical_json_bytes(body))


@dataclass(frozen=True)
class AssemblerEvidence:
    """Immutable evidence envelope for one published environment."""

    output_identity: str
    """The assembled output identity digest (hex)."""

    input_identity: AssemblerInputIdentity
    """The assembler input identity bound by this evidence."""

    tree_digest: str
    """Canonical output-tree digest."""

    evidence_digest: str
    """Canonical assembler-evidence-body digest."""

    body: AssemblerEvidenceBody
    """The output-identity-free evidence body."""


@dataclass(frozen=True)
class AssembledOutputIdentity:
    """The content-derived identity of one assembled environment."""

    input_identity: AssemblerInputIdentity
    tree_digest: str
    evidence_digest: str
    digest: str
    """Canonical SHA-256 over ``(input identity, tree, evidence)``."""


def compute_assembled_output_identity(
    input_identity: AssemblerInputIdentity,
    tree_digest: str,
    evidence_digest: str,
) -> AssembledOutputIdentity:
    """Derive an :class:`AssembledOutputIdentity` from its three bindings."""
    payload = {
        "input_identity_digest": input_identity.digest,
        "tree_digest": tree_digest,
        "evidence_digest": evidence_digest,
    }
    digest = _sha256_hex(canonical_json_bytes(payload))
    return AssembledOutputIdentity(
        input_identity=input_identity,
        tree_digest=tree_digest,
        evidence_digest=evidence_digest,
        digest=digest,
    )


@dataclass(frozen=True)
class AssemblyResult:
    """Consumer-neutral result of one successful assembly.

    Carries every immutable path and evidence value a consumer needs to
    construct its own layout, and nothing consumer-specific: no generated
    executable link, Pi layout, extension settings, build-context, CLI-guard,
    or lock-refresh behavior.
    """

    environment_root: Path
    """Immutable published environment tree path."""

    evidence_path: Path
    """Immutable evidence envelope path."""

    output_identity: str
    """Assembled output identity digest."""

    input_identity: AssemblerInputIdentity
    tree_digest: str
    evidence_digest: str
    roots: tuple[RootSpec, ...]
    root_metadata: tuple[ReviewedRootMetadata, ...]
    packages: tuple[LockPackage, ...]
    omitted_optionals: tuple[OmittedOptional, ...]
    integrity_less: tuple[IntegrityLessNode, ...]
    image_digest: str
    node_version: str
    npm_version: str
    script_digest: str
    policy_digest: str
    platform: str
    lockfile_digest: str
    npm_policy_flags: tuple[str, ...]
    tree_entries: tuple[TreeEntry, ...]


# ── serialization ───────────────────────────────────────────────────────


def serialize_evidence(evidence: AssemblerEvidence) -> bytes:
    """Return the canonical, deterministic evidence-envelope bytes."""
    return canonical_json_bytes(evidence)


def serialize_result(result: AssemblyResult) -> bytes:
    """Return the canonical, deterministic consumer-neutral result bytes."""
    return canonical_json_bytes(result)


# ── parsing helpers ─────────────────────────────────────────────────────


def _malformed(context: str, detail: str) -> LockedNpmError:
    return LockedNpmError("evidence_malformed", f"{context}: {detail}")


def _require_dict(value: object, context: str) -> dict:
    if not isinstance(value, dict):
        raise _malformed(context, "expected an object")
    return value


def _check_keys(d: dict, expected: frozenset[str], context: str) -> None:
    extra = sorted(set(d) - expected)
    if extra:
        raise _malformed(context, f"unexpected field(s) {extra}")


def _str(d: dict, key: str, context: str) -> str:
    value = d.get(key)
    if not isinstance(value, str):
        raise _malformed(context, f"{key!r} must be a string")
    return value


def _int(d: dict, key: str, context: str) -> int:
    value = d.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise _malformed(context, f"{key!r} must be an integer")
    return value


def _bool(d: dict, key: str, context: str) -> bool:
    value = d.get(key)
    if not isinstance(value, bool):
        raise _malformed(context, f"{key!r} must be a boolean")
    return value


def _list(d: dict, key: str, context: str) -> list:
    value = d.get(key)
    if not isinstance(value, list):
        raise _malformed(context, f"{key!r} must be a list")
    return value


def _parse_pairs(value: object, context: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list):
        raise _malformed(context, "expected a list of [name, range] pairs")
    pairs: list[tuple[str, str]] = []
    for item in value:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not isinstance(item[0], str)
            or not isinstance(item[1], str)
        ):
            raise _malformed(context, "expected a list of [name, range] pairs")
        pairs.append((item[0], item[1]))
    return tuple(sorted(pairs))


def _parse_str_list(value: object, context: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise _malformed(context, "expected a list of strings")
    return tuple(sorted(value))


def _parse_root_spec(d: object, context: str) -> RootSpec:
    d = _require_dict(d, context)
    _check_keys(d, frozenset({"name", "version"}), context)
    return RootSpec(
        name=_str(d, "name", context),
        version=_str(d, "version", context),
    )


def _parse_tree_entry(d: object, context: str) -> TreeEntry:
    d = _require_dict(d, context)
    _check_keys(
        d,
        frozenset({"path", "kind", "digest", "target", "mode", "uid", "gid"}),
        context,
    )
    return TreeEntry(
        path=_str(d, "path", context),
        kind=_str(d, "kind", context),
        digest=_str(d, "digest", context),
        target=_str(d, "target", context),
        mode=_int(d, "mode", context),
        uid=_int(d, "uid", context),
        gid=_int(d, "gid", context),
    )


def _canonical_tree_entries(
    entries: tuple[TreeEntry, ...], context: str
) -> tuple[TreeEntry, ...]:
    """Reject tree entries that are not canonical or duplicate a path.

    Serialized tree entries are canonical: ordered like ``sorted(entries)``
    (path first, then the remaining dataclass fields) and free of duplicate
    paths.  Any other encoding is malformed.
    """
    if entries != tuple(sorted(entries)):
        raise _malformed(context, "tree entries are not in canonical order")
    paths = [entry.path for entry in entries]
    if len(paths) != len(set(paths)):
        raise _malformed(context, "duplicate tree entry path")
    return entries


def _parse_optional_str_list(value: object, context: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise _malformed(context, "expected a list of strings or null")
    return tuple(value)


def _parse_lock_package(d: object, context: str) -> LockPackage:
    d = _require_dict(d, context)
    _check_keys(
        d,
        frozenset(
            {
                "path",
                "name",
                "version",
                "resolved",
                "integrity",
                "dependencies",
                "optional_dependencies",
                "dev_dependencies",
                "peer_dependencies",
                "optional_peers",
                "dev",
                "optional",
                "peer",
                "has_install_script",
                "os",
                "cpu",
                "applicable",
            }
        ),
        context,
    )
    return LockPackage(
        path=_str(d, "path", context),
        name=_str(d, "name", context),
        version=_str(d, "version", context),
        resolved=_str(d, "resolved", context),
        integrity=_str(d, "integrity", context),
        dependencies=_parse_pairs(d.get("dependencies"), f"{context}.dependencies"),
        optional_dependencies=_parse_pairs(
            d.get("optional_dependencies"), f"{context}.optional_dependencies"
        ),
        dev_dependencies=_parse_pairs(
            d.get("dev_dependencies"), f"{context}.dev_dependencies"
        ),
        peer_dependencies=_parse_pairs(
            d.get("peer_dependencies"), f"{context}.peer_dependencies"
        ),
        optional_peers=_parse_str_list(
            d.get("optional_peers"), f"{context}.optional_peers"
        ),
        dev=_bool(d, "dev", context),
        optional=_bool(d, "optional", context),
        peer=_bool(d, "peer", context),
        has_install_script=_bool(d, "has_install_script", context),
        os=_parse_optional_str_list(d.get("os"), f"{context}.os"),
        cpu=_parse_optional_str_list(d.get("cpu"), f"{context}.cpu"),
        applicable=_bool(d, "applicable", context),
    )


def _parse_omitted_optional(d: object, context: str) -> OmittedOptional:
    d = _require_dict(d, context)
    _check_keys(
        d,
        frozenset({"parent_path", "name", "range", "reason", "platform"}),
        context,
    )
    return OmittedOptional(
        parent_path=_str(d, "parent_path", context),
        name=_str(d, "name", context),
        range=_str(d, "range", context),
        reason=_str(d, "reason", context),
        platform=_str(d, "platform", context),
    )


def _parse_integrity_less(d: object, context: str) -> IntegrityLessNode:
    d = _require_dict(d, context)
    _check_keys(d, frozenset({"name", "path", "version", "resolved"}), context)
    return IntegrityLessNode(
        name=_str(d, "name", context),
        path=_str(d, "path", context),
        version=_str(d, "version", context),
        resolved=_str(d, "resolved", context),
    )


def _parse_reviewed_root_metadata(d: object, context: str) -> ReviewedRootMetadata:
    d = _require_dict(d, context)
    _check_keys(
        d,
        frozenset({"package_name", "lock_path", "bin", "engines_node"}),
        context,
    )
    engines_node = d.get("engines_node")
    if engines_node is not None and not isinstance(engines_node, str):
        raise _malformed(context, "'engines_node' must be a string or null")
    return ReviewedRootMetadata(
        package_name=_str(d, "package_name", context),
        lock_path=_str(d, "lock_path", context),
        bin=_parse_pairs(d.get("bin"), f"{context}.bin"),
        engines_node=engines_node,
    )


def _parse_assembler_identity(d: object, context: str) -> AssemblerIdentity:
    d = _require_dict(d, context)
    _check_keys(
        d,
        frozenset(
            {
                "image_digest",
                "node_version",
                "npm_version",
                "script_digest",
                "policy_digest",
                "platform",
                "digest",
            }
        ),
        context,
    )
    digest = _str(d, "digest", context)
    recomputed = compute_assembler_identity(
        image_digest=_str(d, "image_digest", context),
        node_version=_str(d, "node_version", context),
        npm_version=_str(d, "npm_version", context),
        script_digest=_str(d, "script_digest", context),
        policy_digest=_str(d, "policy_digest", context),
        platform=_str(d, "platform", context),
    )
    if recomputed.digest != digest:
        raise _malformed(context, "assembler digest does not match components")
    return recomputed


def _parse_assembler_input_identity(d: object, context: str) -> AssemblerInputIdentity:
    d = _require_dict(d, context)
    _check_keys(
        d,
        frozenset(
            {"roots", "lockfile_digest", "assembler", "digest", "package_digest"}
        ),
        context,
    )
    roots = tuple(
        _parse_root_spec(item, f"{context}.roots[{i}]")
        for i, item in enumerate(_list(d, "roots", context))
    )
    lockfile_digest = _str(d, "lockfile_digest", context)
    assembler = _parse_assembler_identity(d.get("assembler"), f"{context}.assembler")
    digest = _str(d, "digest", context)
    package_digest = d.get("package_digest")
    if package_digest is not None and not isinstance(package_digest, str):
        raise _malformed(context, "'package_digest' must be a string or null")
    recomputed = input_identity_digest(
        roots=roots,
        lockfile_digest=lockfile_digest,
        assembler_digest=assembler.digest,
        package_digest=package_digest,
    )
    if recomputed != digest:
        raise _malformed(context, "input identity digest does not match components")
    return AssemblerInputIdentity(
        roots=roots,
        lockfile_digest=lockfile_digest,
        assembler=assembler,
        digest=digest,
        package_digest=package_digest,
    )


def _parse_evidence_body(d: object, context: str) -> AssemblerEvidenceBody:
    d = _require_dict(d, context)
    _check_keys(
        d,
        frozenset(
            {
                "input_identity",
                "tree_digest",
                "tree_entries",
                "packages",
                "omitted_optionals",
                "integrity_less",
                "root_metadata",
                "npm_policy_flags",
            }
        ),
        context,
    )
    tree_entries = _canonical_tree_entries(
        tuple(
            _parse_tree_entry(item, f"{context}.tree_entries[{i}]")
            for i, item in enumerate(_list(d, "tree_entries", context))
        ),
        context,
    )
    tree_digest = _str(d, "tree_digest", context)
    if canonical_tree_digest(tree_entries) != tree_digest:
        raise _malformed(context, "tree digest does not match tree entries")
    flags = tuple(_list(d, "npm_policy_flags", context))
    if flags != npm_policy_flags():
        raise _malformed(context, "npm policy flags do not match the fixed policy")
    return AssemblerEvidenceBody(
        input_identity=_parse_assembler_input_identity(
            d.get("input_identity"), f"{context}.input_identity"
        ),
        tree_digest=tree_digest,
        tree_entries=tree_entries,
        packages=tuple(
            _parse_lock_package(item, f"{context}.packages[{i}]")
            for i, item in enumerate(_list(d, "packages", context))
        ),
        omitted_optionals=tuple(
            _parse_omitted_optional(item, f"{context}.omitted_optionals[{i}]")
            for i, item in enumerate(_list(d, "omitted_optionals", context))
        ),
        integrity_less=tuple(
            _parse_integrity_less(item, f"{context}.integrity_less[{i}]")
            for i, item in enumerate(_list(d, "integrity_less", context))
        ),
        root_metadata=tuple(
            _parse_reviewed_root_metadata(item, f"{context}.root_metadata[{i}]")
            for i, item in enumerate(_list(d, "root_metadata", context))
        ),
        npm_policy_flags=flags,
    )


def parse_evidence(data: bytes) -> AssemblerEvidence:
    """Parse and verify an evidence envelope, rejecting substitution."""
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _malformed("evidence", "not valid UTF-8 JSON") from exc
    d = _require_dict(raw, "evidence")
    _check_keys(
        d,
        frozenset(
            {
                "output_identity",
                "input_identity",
                "tree_digest",
                "evidence_digest",
                "body",
            }
        ),
        "evidence",
    )
    output_identity = _str(d, "output_identity", "evidence")
    input_identity = _parse_assembler_input_identity(
        d.get("input_identity"), "evidence.input_identity"
    )
    tree_digest = _str(d, "tree_digest", "evidence")
    evidence_digest = _str(d, "evidence_digest", "evidence")
    body = _parse_evidence_body(d.get("body"), "evidence.body")

    if body.input_identity != input_identity:
        raise _malformed("evidence", "input identity does not match evidence body")
    if body.tree_digest != tree_digest:
        raise _malformed("evidence", "tree digest does not match evidence body")
    if evidence_body_digest(body) != evidence_digest:
        raise _malformed("evidence", "evidence digest does not match evidence body")
    recomputed = compute_assembled_output_identity(
        input_identity, tree_digest, evidence_digest
    )
    if recomputed.digest != output_identity:
        raise _malformed("evidence", "output identity does not match bindings")

    return AssemblerEvidence(
        output_identity=output_identity,
        input_identity=input_identity,
        tree_digest=tree_digest,
        evidence_digest=evidence_digest,
        body=body,
    )


def parse_result(data: bytes) -> AssemblyResult:
    """Parse and verify a consumer-neutral result, rejecting substitution."""
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _malformed("result", "not valid UTF-8 JSON") from exc
    d = _require_dict(raw, "result")
    _check_keys(
        d,
        frozenset(
            {
                "environment_root",
                "evidence_path",
                "output_identity",
                "input_identity",
                "tree_digest",
                "evidence_digest",
                "roots",
                "root_metadata",
                "packages",
                "omitted_optionals",
                "integrity_less",
                "image_digest",
                "node_version",
                "npm_version",
                "script_digest",
                "policy_digest",
                "platform",
                "lockfile_digest",
                "npm_policy_flags",
                "tree_entries",
            }
        ),
        "result",
    )
    input_identity = _parse_assembler_input_identity(
        d.get("input_identity"), "result.input_identity"
    )
    tree_digest = _str(d, "tree_digest", "result")
    evidence_digest = _str(d, "evidence_digest", "result")
    tree_entries = _canonical_tree_entries(
        tuple(
            _parse_tree_entry(item, f"result.tree_entries[{i}]")
            for i, item in enumerate(_list(d, "tree_entries", "result"))
        ),
        "result",
    )
    if canonical_tree_digest(tree_entries) != tree_digest:
        raise _malformed("result", "tree digest does not match tree entries")
    flags = tuple(_list(d, "npm_policy_flags", "result"))
    if flags != npm_policy_flags():
        raise _malformed("result", "npm policy flags do not match the fixed policy")
    roots = tuple(
        _parse_root_spec(item, f"result.roots[{i}]")
        for i, item in enumerate(_list(d, "roots", "result"))
    )
    # Redundant result fields must agree with the bound input identity, so a
    # substituted field cannot pass through a serialized result even when the
    # identities and digests are left unchanged.
    lockfile_digest = _str(d, "lockfile_digest", "result")
    image_digest = _str(d, "image_digest", "result")
    node_version = _str(d, "node_version", "result")
    npm_version = _str(d, "npm_version", "result")
    script_digest = _str(d, "script_digest", "result")
    policy_digest = _str(d, "policy_digest", "result")
    platform = _str(d, "platform", "result")

    if roots != input_identity.roots:
        raise _malformed("result", "roots do not match the input identity")
    if lockfile_digest != input_identity.lockfile_digest:
        raise _malformed("result", "lockfile digest does not match the input identity")
    assembler = input_identity.assembler
    if image_digest != assembler.image_digest:
        raise _malformed("result", "image digest does not match the input identity")
    if node_version != assembler.node_version:
        raise _malformed("result", "node version does not match the input identity")
    if npm_version != assembler.npm_version:
        raise _malformed("result", "npm version does not match the input identity")
    if script_digest != assembler.script_digest:
        raise _malformed("result", "script digest does not match the input identity")
    if policy_digest != assembler.policy_digest:
        raise _malformed("result", "policy digest does not match the input identity")
    if platform != assembler.platform:
        raise _malformed("result", "platform does not match the input identity")
    packages = tuple(
        _parse_lock_package(item, f"result.packages[{i}]")
        for i, item in enumerate(_list(d, "packages", "result"))
    )
    omitted_optionals = tuple(
        _parse_omitted_optional(item, f"result.omitted_optionals[{i}]")
        for i, item in enumerate(_list(d, "omitted_optionals", "result"))
    )
    integrity_less = tuple(
        _parse_integrity_less(item, f"result.integrity_less[{i}]")
        for i, item in enumerate(_list(d, "integrity_less", "result"))
    )
    root_metadata = tuple(
        _parse_reviewed_root_metadata(item, f"result.root_metadata[{i}]")
        for i, item in enumerate(_list(d, "root_metadata", "result"))
    )
    # Rebuild the evidence body from the flat fields and verify the
    # evidence digest and output identity so a substituted tree/evidence
    # pair can never pass through a serialized result.
    body = AssemblerEvidenceBody(
        input_identity=input_identity,
        tree_digest=tree_digest,
        tree_entries=tree_entries,
        packages=packages,
        omitted_optionals=omitted_optionals,
        integrity_less=integrity_less,
        root_metadata=root_metadata,
        npm_policy_flags=flags,
    )
    if evidence_body_digest(body) != evidence_digest:
        raise _malformed("result", "evidence digest does not match result fields")
    output_identity = _str(d, "output_identity", "result")
    recomputed = compute_assembled_output_identity(
        input_identity, tree_digest, evidence_digest
    )
    if recomputed.digest != output_identity:
        raise _malformed("result", "output identity does not match bindings")

    return AssemblyResult(
        environment_root=Path(_str(d, "environment_root", "result")),
        evidence_path=Path(_str(d, "evidence_path", "result")),
        output_identity=output_identity,
        input_identity=input_identity,
        tree_digest=tree_digest,
        evidence_digest=evidence_digest,
        roots=roots,
        root_metadata=root_metadata,
        packages=packages,
        omitted_optionals=omitted_optionals,
        integrity_less=integrity_less,
        image_digest=image_digest,
        node_version=node_version,
        npm_version=npm_version,
        script_digest=script_digest,
        policy_digest=policy_digest,
        platform=platform,
        lockfile_digest=lockfile_digest,
        npm_policy_flags=flags,
        tree_entries=tree_entries,
    )

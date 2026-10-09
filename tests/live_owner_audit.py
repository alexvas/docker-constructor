"""Independent live visual-owner enumeration and header checks.

The live visual-header contract must not trust the production update-target
enumeration to define its own expected membership: if production omitted an
owner, a production-derived expectation would omit it too and the omission
would escape detection.  These helpers enumerate owners directly from the
parsed TOML using the schema's ownership rules, and expose the production
enumeration separately so a test can compare the two and detect omissions.
"""
from __future__ import annotations

import tomllib
from collections.abc import Mapping, Sequence

from docker.versioning.updates import (
    build_update_targets,
    group_targets_by_owner,
    path_segments,
)

__all__ = [
    "independent_display_path",
    "independent_update_owners",
    "owner_header_violations",
    "production_update_owners",
]


def independent_update_owners(raw: Mapping[str, object]) -> tuple[str, ...]:
    """Enumerate replaceable owner blocks directly from parsed TOML.

    Schema ownership rule: a table that declares an ``update`` subtable is a
    replaceable owner unless an ancestor already owns it (the nested rustup
    bootstrap belongs to its parent rust block).  Each optional Pi extension
    table owns its own block.  This never consults production target or
    grouping code.
    """
    owners: list[str] = []

    def walk(node: object, path: list[str], ancestor_owner: bool) -> None:
        if not isinstance(node, Mapping):
            return
        is_owner = isinstance(node.get("update"), Mapping) and not ancestor_owner
        if is_owner:
            owners.append(".".join(path))
        for key, value in node.items():
            if isinstance(value, Mapping):
                walk(value, [*path, key], ancestor_owner or is_owner)

    walk(raw, [], False)
    return tuple(owners)


def production_update_owners(
    inventory: object,
    *,
    omit_paths: Sequence[str] = (),
) -> tuple[str, ...]:
    """Return production replacement owners, optionally omitting targets.

    *omit_paths* simulates a production enumeration regression (an owner
    silently dropped) so tests can prove the independent enumeration still
    catches it.
    """
    omitted = set(omit_paths)
    targets = tuple(
        target
        for target in build_update_targets(inventory)
        if target.path not in omitted
    )
    return tuple(owner for owner, _ in group_targets_by_owner(targets))


def independent_display_path(owner: str) -> str:
    """Return the display path by stripping one leading schema prefix.

    Mirrors the documented display rule without calling production
    ``display_path``; tests cross-check the two.
    """
    for prefix in ("build.stages.", "runtime."):
        if owner.startswith(prefix):
            return owner[len(prefix):]
    return owner


def _table_header_segments(line: str) -> tuple[str, ...] | None:
    """Parse *line* as a TOML table header and return its key segments.

    Uses :mod:`tomllib` so quoted keys (``runtime.pi-extensions."foo.bar"``)
    and bare keys are decoded into their logical segments independently of
    any production header rendering.  Returns ``None`` for non-header lines.
    """
    stripped = line.strip()
    if not stripped.startswith("[") or stripped.startswith("[["):
        return None
    try:
        parsed = tomllib.loads(stripped + "\n")
    except tomllib.TOMLDecodeError:
        return None
    segments: list[str] = []
    node: object = parsed
    while isinstance(node, dict) and len(node) == 1:
        key = next(iter(node))
        segments.append(key)
        node = node[key]
    if not isinstance(node, dict) or node:
        return None
    return tuple(segments)


def owner_header_violations(
    document_text: str,
    owners: Sequence[str],
) -> list[tuple[str, str]]:
    """Return ``(owner, reason)`` for owner tables missing their header.

    The owner table is located by parsing each candidate header into its
    TOML key segments (so a dotted extension name stays one quoted key) and
    comparing those segments to :func:`updates.path_segments` of the owner.
    The expected comment uses :func:`independent_display_path`, so the check
    does not derive its expectation from production header rendering.
    """
    lines = document_text.splitlines()
    violations: list[tuple[str, str]] = []
    for owner in owners:
        segments = path_segments(owner)
        index = next(
            (
                position
                for position, line in enumerate(lines)
                if _table_header_segments(line) == segments
            ),
            None,
        )
        if index is None:
            violations.append(
                (owner, f"missing owner table for key segments {segments}"),
            )
            continue
        expected = f"# --- {independent_display_path(owner)} ---"
        if index == 0 or lines[index - 1] != expected:
            found = lines[index - 1] if index > 0 else "<start of document>"
            violations.append(
                (
                    owner,
                    f"expected {expected!r} before {segments}, found {found!r}",
                ),
            )
    return violations

"""Aggregate owner for the host-side local configuration companion.

Resolves the single fixed ``docker-constructor.local.toml`` companion for the
selected constructor project, routes it through the shared
:mod:`~docker.versioning.configuration_document_validation` boundary exactly
once per transaction, and composes the four closed domain-owned tables into an
immutable :class:`~docker.versioning.model.LocalConfig` aggregate.

This module owns only aggregate concerns: fixed companion resolution, the
closed top-level table registry, dispatch of recognized tables to their domain
parsers, and assembly of the immutable aggregate. Per-table field, type,
default, and semantic validation live with the owning capability modules, and
their parsers also supply the default for an absent table:

- ``[host-access]`` -- :mod:`~docker.versioning.host_access` (runtime host-access)
- ``[cache]`` -- :mod:`~docker.versioning.cache_storage` (user-cache-storage)
- ``[corporate-trust]`` and ``[network.proxy]`` --
  :mod:`~docker.versioning.corporate_network` (corporate-network)
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .cache_storage import parse_local_cache_config
from .configuration_document_validation import (
    DocumentIdentity,
    DocumentRole,
    ProjectedOwnerResult,
    capture_owner_result,
    parse_configuration_document,
    release_owner_result,
)
from .corporate_network import (
    parse_local_corporate_trust,
    parse_local_network_proxy,
)
from .errors import InventoryError
from .host_access import parse_local_host_access
from .model import LocalConfig

LOCAL_COMPANION_BASENAME = "docker-constructor.local.toml"
"""The one fixed machine-local companion basename."""


def resolve_local_companion_path(inventory_path: Path) -> Path:
    """Return the fixed companion beside the selected project inventory.

    The companion basename is fixed. No ancestor directory, workspace,
    tool-installation root, alternate basename, or caller-supplied path is ever
    consulted, and the selected project directory is the inventory's directory.
    """
    return Path(inventory_path).with_name(LOCAL_COMPANION_BASENAME)


# ---------------------------------------------------------------------------
# Closed aggregate registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class _LocalDomainTable:
    """One recognized top-level local table and its domain-owned parser."""

    toml_table: str
    config_field: str
    parse: Callable[[object, "str | None"], object]


_LOCAL_DOMAIN_TABLES: tuple[_LocalDomainTable, ...] = (
    _LocalDomainTable("host-access", "host_access", parse_local_host_access),
    _LocalDomainTable("cache", "cache", parse_local_cache_config),
    _LocalDomainTable(
        "corporate-trust", "corporate_trust", parse_local_corporate_trust
    ),
    _LocalDomainTable("network", "network_proxy", parse_local_network_proxy),
)

LOCAL_TABLE_NAMES: frozenset[str] = frozenset(
    table.toml_table for table in _LOCAL_DOMAIN_TABLES
)
"""The closed set of accepted top-level local companion tables."""


def validate_local_document(
    raw: Mapping[str, object],
    *,
    host_access_mode: str | None = None,
) -> LocalConfig:
    """Compose the four closed domain tables into an immutable aggregate.

    This function owns only aggregate closure, dispatch, and assembly: it
    rejects unknown top-level tables -- including a future ``[output]`` --
    before any domain parser runs, then dispatches every registered table to
    its domain-owned parser. Absent tables are dispatched as ``None`` so each
    owning capability supplies its own default; the aggregate never constructs
    a domain default itself.
    """
    if not isinstance(raw, dict):
        raise InventoryError("local: expected TOML table", field=None)
    unknown = set(raw) - LOCAL_TABLE_NAMES
    if unknown:
        key = sorted(unknown)[0]
        raise InventoryError(
            f"local.{key}: unknown key; use only [host-access], [cache], "
            f"[corporate-trust], or [network.proxy]",
            field=f"local.{key}",
        )
    selected: dict[str, Any] = {}
    for table in _LOCAL_DOMAIN_TABLES:
        selected[table.config_field] = table.parse(
            raw.get(table.toml_table), host_access_mode
        )
    return LocalConfig(**selected)


# ---------------------------------------------------------------------------
# Boundary-routed loaders
# ---------------------------------------------------------------------------

def load_local_project_configuration(
    path: Path,
    *,
    host_access_mode: str | None = None,
) -> LocalConfig:
    """Parse and validate one local companion through the Phase 1 boundary."""
    identity = DocumentIdentity(DocumentRole.LOCAL, Path(path))
    return release_owner_result(_load_local_document(identity, host_access_mode))


def load_optional_local_project_configuration(
    inventory_path: Path,
    *,
    host_access_mode: str | None = None,
) -> LocalConfig:
    """Load the fixed companion beside *inventory_path*, or return defaults.

    An absent optional companion yields the same owner-supplied defaults as an
    empty companion, routed through the domain parsers, and never creates or
    parses a file.
    """
    companion = resolve_local_companion_path(inventory_path)
    if not companion.exists():
        return validate_local_document({}, host_access_mode=host_access_mode)
    return load_local_project_configuration(companion, host_access_mode=host_access_mode)


def _load_local_document(
    identity: DocumentIdentity,
    host_access_mode: str | None,
) -> ProjectedOwnerResult[LocalConfig]:
    """Validate a parsed local document without retaining it on failure."""
    document = parse_configuration_document(identity)
    return capture_owner_result(
        identity,
        lambda: validate_local_document(
            dict(document.data), host_access_mode=host_access_mode
        ),
    )

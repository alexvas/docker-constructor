"""Runtime host-access ownership for machine-local ``[host-access]`` state.

This is the domain owner for the ``[host-access]`` table in the host-only
``docker-constructor.local.toml`` companion. It owns the table's accepted
fields, defaults, types, and semantic validation (IPv4/IPv6 address form and
the reviewed ``docker-gateway`` mode exception) and returns the immutable
:class:`~docker.versioning.model.LocalHostAccess` state consumed by runtime
host-access planning.

The parser operates on an already-parsed TOML table. It performs no file I/O
and never parses TOML itself; the aggregate local companion boundary owns
resolution, the single parse, and error projection.
"""
from __future__ import annotations

import ipaddress

from .errors import InventoryError
from .model import LocalHostAccess


def parse_local_host_access(
    host_raw: object,
    host_access_mode: str | None,
) -> LocalHostAccess:
    """Parse ``[host-access]`` into validated machine-local address state.

    An absent table (``None``) yields the owner-defined default; a present but
    non-table value is still rejected.
    """
    if host_raw is None:
        return LocalHostAccess()
    if not isinstance(host_raw, dict):
        raise InventoryError(
            "local.host-access: expected table; use [host-access].address",
            field="local.host-access",
        )
    unknown_host = set(host_raw) - {"address"}
    if unknown_host:
        key = sorted(unknown_host)[0]
        raise InventoryError(
            f"local.host-access.{key}: unknown key; use only local.host-access.address",
            field=f"local.host-access.{key}",
        )
    address = host_raw.get("address")
    if address is not None:
        if not isinstance(address, str):
            raise InventoryError(
                "local.host-access.address: expected string; set an IPv4 or IPv6 address",
                field="local.host-access.address",
            )
        if address == "host-gateway" and host_access_mode == "docker-gateway":
            pass
        else:
            try:
                ipaddress.ip_address(address)
            except ValueError as exc:
                raise InventoryError(
                    "local.host-access.address: expected IPv4 or IPv6 address; correct [host-access].address",
                    field="local.host-access.address",
                ) from exc
    return LocalHostAccess(address)

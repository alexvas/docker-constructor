"""Corporate-network ownership for machine-local trust and proxy state.

This is the domain owner for the ``[corporate-trust]`` and ``[network.proxy]``
tables in the host-only ``docker-constructor.local.toml`` companion. It owns
their accepted fields, defaults, types, and semantic validation (the
credential-free proxy URL shape) and returns the immutable
:class:`~docker.versioning.model.LocalCorporateTrust` and
:class:`~docker.versioning.model.LocalNetworkProxy` state consumed by
corporate-network planning.

The parsers operate on an already-parsed TOML table. They perform no file I/O
and never parse TOML themselves; the aggregate local companion boundary owns
resolution, the single parse, and error projection.
"""
from __future__ import annotations

import urllib.parse

from .errors import InventoryError
from .model import LocalCorporateTrust, LocalNetworkProxy


def parse_local_corporate_trust(
    trust_raw: object,
    host_access_mode: str | None,
) -> LocalCorporateTrust:
    """Parse ``[corporate-trust]`` into validated local state.

    An absent table (``None``) yields the owner-defined default; a present but
    non-table value is still rejected.
    """
    del host_access_mode
    if trust_raw is None:
        return LocalCorporateTrust()
    if not isinstance(trust_raw, dict):
        raise InventoryError(
            "local.corporate-trust: expected table; use [corporate-trust].enabled",
            field="local.corporate-trust",
        )
    unknown = set(trust_raw) - {"enabled"}
    if unknown:
        key = sorted(unknown)[0]
        raise InventoryError(
            f"local.corporate-trust.{key}: unknown key; "
            f"use only local.corporate-trust.enabled",
            field=f"local.corporate-trust.{key}",
        )
    enabled = trust_raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise InventoryError(
            "local.corporate-trust.enabled: expected boolean; "
            "set enabled = true or enabled = false",
            field="local.corporate-trust.enabled",
        )
    return LocalCorporateTrust(enabled)


def parse_local_network_proxy(
    network_raw: object,
    host_access_mode: str | None,
) -> LocalNetworkProxy:
    """Parse ``[network.proxy]`` into validated local state.

    An absent table (``None``) yields the owner-defined default; a present but
    non-table value is still rejected.
    """
    del host_access_mode
    if network_raw is None:
        return LocalNetworkProxy()
    if not isinstance(network_raw, dict):
        raise InventoryError(
            "local.network: expected table; use [network.proxy]", field="local.network"
        )
    unknown_network = set(network_raw) - {"proxy"}
    if unknown_network:
        key = sorted(unknown_network)[0]
        raise InventoryError(
            f"local.network.{key}: unknown key; use only [network.proxy]",
            field=f"local.network.{key}",
        )
    has_proxy = "proxy" in network_raw
    proxy_raw = network_raw.get("proxy")
    if has_proxy:
        if not isinstance(proxy_raw, dict):
            raise InventoryError(
                "local.network.proxy: expected table; use [network.proxy]",
                field="local.network.proxy",
            )
        unknown_proxy = set(proxy_raw) - {"url", "no_proxy"}
        if unknown_proxy:
            key = sorted(unknown_proxy)[0]
            raise InventoryError(
                f"local.network.proxy.{key}: unknown key; "
                f"use only [network.proxy].url and [network.proxy].no_proxy",
                field=f"local.network.proxy.{key}",
            )
        url = proxy_raw.get("url")
        if url is None:
            raise InventoryError(
                "local.network.proxy.url: missing required key; set [network.proxy].url",
                field="local.network.proxy.url",
            )
        if not isinstance(url, str):
            raise InventoryError(
                "local.network.proxy.url: expected string; set a credential-free "
                "http/socks5/socks5h URL",
                field="local.network.proxy.url",
            )
        _validate_proxy_url(url)
        no_proxy = proxy_raw.get("no_proxy")
        if no_proxy is not None and not isinstance(no_proxy, str):
            raise InventoryError(
                "local.network.proxy.no_proxy: expected string; set a comma-separated "
                "bypass list",
                field="local.network.proxy.no_proxy",
            )
        return LocalNetworkProxy(url, no_proxy)
    return LocalNetworkProxy()


def _validate_proxy_url(url: str) -> None:
    """Validate a credential-free proxy URL; raise on any unsupported shape."""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise InventoryError(
            f"local.network.proxy.url: malformed URL {url!r}",
            field="local.network.proxy.url",
        ) from exc
    if parsed.scheme not in {"http", "socks5", "socks5h"}:
        raise InventoryError(
            f"local.network.proxy.url: unsupported scheme {parsed.scheme!r}; "
            f"use http, socks5, or socks5h",
            field="local.network.proxy.url",
        )
    if parsed.username is not None or parsed.password is not None:
        raise InventoryError(
            "local.network.proxy.url: credentials are not allowed; "
            "use a credential-free URL",
            field="local.network.proxy.url",
        )
    if parsed.fragment:
        raise InventoryError(
            "local.network.proxy.url: fragments are not allowed",
            field="local.network.proxy.url",
        )
    if parsed.query:
        raise InventoryError(
            "local.network.proxy.url: query strings are not allowed",
            field="local.network.proxy.url",
        )
    if parsed.path:
        raise InventoryError(
            "local.network.proxy.url: URL paths are not allowed",
            field="local.network.proxy.url",
        )
    if not parsed.hostname:
        raise InventoryError(
            "local.network.proxy.url: missing host", field="local.network.proxy.url"
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise InventoryError(
            f"local.network.proxy.url: invalid port in {url!r}",
            field="local.network.proxy.url",
        ) from exc
    if port is None:
        raise InventoryError(
            "local.network.proxy.url: missing port", field="local.network.proxy.url"
        )
    if not 1 <= port <= 65535:
        raise InventoryError(
            f"local.network.proxy.url: port {port} out of range 1..65535",
            field="local.network.proxy.url",
        )

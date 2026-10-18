"""Resolved credential-free corporate network policy for the assembler.

The assembler never loads, resolves, or validates machine-local corporate
configuration itself.  The caller resolves the local companion once and
hands the assembler an immutable :class:`CorporateNetworkPolicy`.  This
module re-validates only the credential-free and path-shape invariants
needed to keep the assembler boundary closed:

* a proxy endpoint must be a credential-free ``http``, ``socks5``, or
  ``socks5h`` URL with a host and an in-range port (no userinfo, path,
  query, or fragment);
* a proxy bypass list is allowed only together with a proxy URL; and
* the corporate trust bundle must be an absolute host path.

No filesystem or ambient-environment access happens here; the trust bundle
contents and proxy reachability remain the caller's responsibility.
"""

from __future__ import annotations

import os
import urllib.parse
from dataclasses import dataclass

from .errors import LockedNpmError

_ALLOWED_PROXY_SCHEMES = frozenset({"http", "socks5", "socks5h"})


def _invalid_proxy(detail: str) -> LockedNpmError:
    return LockedNpmError("invalid_proxy_policy", detail)


def validate_credential_free_proxy_url(url: str) -> None:
    """Reject any proxy URL that is not credential-free and well-formed."""
    if not isinstance(url, str) or not url.strip():
        raise _invalid_proxy("proxy_url must be a non-empty string")
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise _invalid_proxy(f"malformed proxy URL {url!r}") from exc
    if parsed.scheme not in _ALLOWED_PROXY_SCHEMES:
        raise _invalid_proxy(
            f"unsupported proxy scheme {parsed.scheme!r}; use http, socks5, or socks5h"
        )
    if parsed.username is not None or parsed.password is not None:
        raise _invalid_proxy(
            "proxy credentials are not allowed; use a credential-free URL"
        )
    if parsed.fragment:
        raise _invalid_proxy("proxy URL fragments are not allowed")
    if parsed.query:
        raise _invalid_proxy("proxy URL query strings are not allowed")
    if parsed.path:
        raise _invalid_proxy("proxy URL paths are not allowed")
    if not parsed.hostname:
        raise _invalid_proxy("proxy URL is missing a host")
    try:
        port = parsed.port
    except ValueError as exc:
        raise _invalid_proxy(f"invalid port in proxy URL {url!r}") from exc
    if port is None:
        raise _invalid_proxy("proxy URL is missing a port")
    if not 1 <= port <= 65535:
        raise _invalid_proxy(f"proxy port {port} out of range 1..65535")


@dataclass(frozen=True)
class CorporateNetworkPolicy:
    """Resolved credential-free proxy/trust policy for assembler execution.

    ``None`` fields mean "disabled": no proxy environment and no trust
    override are emitted.  When the trust bundle path is set, the assembler
    mounts it read-only at the fixed system trust path before npm network
    access.
    """

    proxy_url: str | None = None
    """Configured credential-free proxy URL, or ``None`` when disabled."""

    proxy_no_proxy: str | None = None
    """Optional bypass list, allowed only together with *proxy_url*."""

    corporate_trust_bundle: str | None = None
    """Absolute host path to the fixed corporate trust bundle, or ``None``
    when corporate trust is disabled."""

    def __post_init__(self) -> None:
        if self.proxy_url is not None:
            validate_credential_free_proxy_url(self.proxy_url)
        if self.proxy_no_proxy is not None:
            if self.proxy_url is None:
                raise _invalid_proxy(
                    "proxy_no_proxy requires proxy_url to be configured"
                )
            if not isinstance(self.proxy_no_proxy, str):
                raise _invalid_proxy("proxy_no_proxy must be a string")
        if self.corporate_trust_bundle is not None:
            if (
                not isinstance(self.corporate_trust_bundle, str)
                or not self.corporate_trust_bundle
                or not os.path.isabs(self.corporate_trust_bundle)
            ):
                raise LockedNpmError(
                    "invalid_trust_policy",
                    "corporate_trust_bundle must be an absolute host path",
                )

    def secrets(self) -> tuple[str, ...]:
        """Return the policy values that must never be persisted."""
        values = (self.proxy_url, self.proxy_no_proxy, self.corporate_trust_bundle)
        return tuple(v for v in values if v)

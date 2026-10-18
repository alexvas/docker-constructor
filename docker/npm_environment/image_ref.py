"""Canonical immutable Docker image-reference validation.

The assembler runs exactly one pinned Node image, so the reference must be
immutable before any cache, staging, executor, or Docker activity.  Three
forms are accepted:

* a bare content digest ``sha256:<64 lowercase hex>``; or
* a repository-qualified digest without a tag
  (``<registry>/<repository>@sha256:<64 lowercase hex>``); or
* a repository-qualified digest with a descriptive tag
  (``<registry>/<repository>:<tag>@sha256:<64 lowercase hex>``).

The digest is authoritative in repository-qualified forms; a tag, when
present, is descriptive only.  In repository-qualified references the
registry component (before the first ``/``) must itself be canonical: a
multi-label DNS hostname, ``localhost``, or a bracketed IPv6 literal, each
with an optional numeric port.  Tag-only references, malformed digests,
uppercase or non-hex digest characters, userinfo (``user[:pass]@host``),
URL schemes, whitespace/Unicode host characters, empty hostname/port,
non-numeric or out-of-range ports, extra or misplaced colons, empty
registry/repository components, malformed repository names, and any other
mutable reference are rejected.
"""

from __future__ import annotations

import ipaddress
import re

from .errors import LockedNpmError

#: ``sha256:`` followed by exactly 64 lowercase hex characters.
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")

#: A non-empty Docker tag: word character first, then word/dot/dash.
_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")

#: One repository path segment: lowercase alphanumeric with single
#: ``.``/``_``/``-`` separators between alphanumerics (Docker distribution
#: naming).  Repository paths are segments joined by ``/``.
_REPO_SEGMENT_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")

#: One DNS hostname label: alphanumeric, optional internal hyphens, no
#: leading/trailing hyphen (Docker distribution naming).
_DNS_LABEL_RE = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?$")

#: A numeric port: ASCII digits only.
_PORT_RE = re.compile(r"^[0-9]+$")


def _invalid(reference: object) -> LockedNpmError:
    return LockedNpmError(
        "invalid_image_reference",
        "expected an immutable image reference "
        "(sha256:<64 lowercase hex>, "
        "<registry>/<repository>@sha256:<64 lowercase hex>, or "
        "<registry>/<repository>:<tag>@sha256:<64 lowercase hex>), "
        f"got {reference!r}",
    )


def _valid_repository_path(name: str) -> bool:
    """Return whether *name* is a valid ``/``-joined repository path."""
    parts = name.split("/")
    return bool(parts) and all(_REPO_SEGMENT_RE.fullmatch(part) for part in parts)


def _valid_port(port: str) -> bool:
    """Return whether *port* is a numeric in-range TCP port (1..65535)."""
    if not _PORT_RE.fullmatch(port):
        return False
    return 1 <= int(port) <= 65535


def _valid_dns_hostname(host: str) -> bool:
    """Return whether *host* is a multi-label DNS hostname."""
    parts = host.split(".")
    if len(parts) < 2:
        return False
    return all(_DNS_LABEL_RE.fullmatch(part) for part in parts)


def _valid_registry(registry: str) -> bool:
    """Return whether *registry* is a canonical registry reference.

    Accepted forms are a (multi-label) DNS hostname, ``localhost``, or a
    bracketed IPv6 literal, each with an optional numeric port.  URL
    schemes, whitespace/Unicode, empty hostname/port, non-numeric or
    out-of-range ports, userinfo, and extra or misplaced colons are
    rejected.
    """
    if not registry:
        return False

    # Bracketed IPv6, optionally with a port: ``[::1]`` or ``[::1]:5000``.
    if registry.startswith("["):
        close = registry.find("]")
        if close == -1:
            return False
        try:
            ipaddress.IPv6Address(registry[1:close])
        except ValueError:
            return False
        rest = registry[close + 1 :]
        if not rest:
            return True
        if rest.startswith(":"):
            return _valid_port(rest[1:])
        return False

    # Hostname or ``localhost``, optionally with a port.
    host, sep, port = registry.partition(":")
    if sep:
        if not _valid_port(port):
            return False
    return host == "localhost" or _valid_dns_hostname(host)


def validate_image_reference(reference: str) -> None:
    """Raise unless *reference* is a canonical immutable image reference."""
    if not isinstance(reference, str) or not reference:
        raise _invalid(reference)

    # Bare digest form.
    if _DIGEST_RE.fullmatch(reference):
        return

    # Repository-plus-digest form.  Exactly one '@' is allowed and it must
    # introduce the digest, so userinfo (``user[:pass]@host``), tag-only
    # mutable references, and multiple '@' separators are rejected here.
    if reference.count("@") != 1:
        raise _invalid(reference)
    name, digest = reference.split("@", 1)
    if not _DIGEST_RE.fullmatch(digest):
        raise _invalid(reference)

    # The name half must be ``registry/repository`` (tag is optional and
    # lives at the end of the repository half).
    if "/" not in name:
        raise _invalid(reference)
    registry, repository = name.split("/", 1)
    if not registry or not repository:
        raise _invalid(reference)
    if not _valid_registry(registry):
        raise _invalid(reference)

    # The repository half MAY carry exactly one trailing ``:tag``.
    repo_path = repository
    if ":" in repository:
        repo_path, tag = repository.rsplit(":", 1)
        if not repo_path or not tag or ":" in repo_path:
            raise _invalid(reference)
        if not _TAG_RE.fullmatch(tag):
            raise _invalid(reference)

    if not _valid_repository_path(repo_path):
        raise _invalid(reference)

"""Reviewed Pi GitHub-release asset URLs and strict SHA256SUMS parsing.

Pi is delivered through its official release contract: one immutable base URL
``https://github.com/<release_repository>/releases/download/<tag>/`` with three
exact-name assets — ``SHA256SUMS``, ``pi-coding-agent-install-package.json``,
and ``pi-coding-agent-install-package-lock.json``.  Asset names are never
inferred from npm metadata and no alternate naming or alias discovery exists.
"""
from __future__ import annotations

import hashlib
import re
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Callable, Iterable, Protocol, overload

from .model import PiReleaseSource
from .build_materialization import StreamingTransport

#: Receives the cumulative received bytes after every yielded body chunk.
ProgressCallback = Callable[[int], None]

#: One per-asset observation scope yielding an optional progress callback.
AssetActivityFactory = Callable[[str], AbstractContextManager["ProgressCallback | None"]]


class ProgressAwareDownload(Protocol):
    """A download callable that also accepts the optional progress observer.

    The production adapter treats *progress* as optional, so it stays callable
    with just a URL when no activity scope exists and with both arguments
    inside one.  A callback that *requires* two arguments does not satisfy this
    protocol, because it cannot serve the no-activity branch.
    """

    def __call__(
        self, url: str, progress: ProgressCallback | None = None
    ) -> bytes: ...


class PiReleaseError(RuntimeError):
    """Pi release-asset selection, parsing, or integrity failure."""


SHA256SUMS_FILENAME = "SHA256SUMS"
INSTALL_PACKAGE_FILENAME = "pi-coding-agent-install-package.json"
INSTALL_PACKAGE_LOCK_FILENAME = "pi-coding-agent-install-package-lock.json"
_REQUIRED_FILENAMES = (INSTALL_PACKAGE_FILENAME, INSTALL_PACKAGE_LOCK_FILENAME)

# sha256sum text mode uses exactly two spaces; binary mode uses " *".
_SUM_LINE_RE = re.compile(r"^([0-9a-fA-F]{64})(?:  | \*)([^\s]+)$")


@dataclass(frozen=True)
class PiReleaseUrls:
    """Exact asset URLs for one reviewed Pi release version."""

    base: str
    sha256sums: str
    install_package: str
    install_package_lock: str


def derive_pi_release_urls(source: PiReleaseSource, version: str) -> PiReleaseUrls:
    """Derive the exact three asset URLs from the reviewed release contract.

    No npm metadata, asset-name inference, or alternate naming is involved.
    """
    base = (
        f"https://github.com/{source.release_repository}/releases/download/"
        f"{source.release_tag_prefix}{version}"
    )
    return PiReleaseUrls(
        base=base,
        sha256sums=f"{base}/{SHA256SUMS_FILENAME}",
        install_package=f"{base}/{INSTALL_PACKAGE_FILENAME}",
        install_package_lock=f"{base}/{INSTALL_PACKAGE_LOCK_FILENAME}",
    )


def _validate_asset_name(name: str, lineno: int) -> None:
    """Reject absolute, escaping, empty, or otherwise unsafe asset names."""
    if not name:
        raise PiReleaseError(f"SHA256SUMS line {lineno} has an empty filename")
    if name.startswith("/") or name.endswith("/") or "\\" in name:
        raise PiReleaseError(
            f"SHA256SUMS line {lineno} has an unsafe filename {name!r}"
        )
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise PiReleaseError(
            f"SHA256SUMS line {lineno} has an unsafe filename {name!r}"
        )


def parse_sha256sums(raw: bytes) -> dict[str, str]:
    """Strictly parse ``sha256sum``-format ``SHA256SUMS`` bytes.

    Returns ``{filename: lowercase-hex-digest}``.  Blank lines, malformed
    lines, unsafe filenames, duplicate filenames, and an empty file are all
    rejected.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PiReleaseError("SHA256SUMS is not valid UTF-8") from exc

    entries: dict[str, str] = {}
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line:
            raise PiReleaseError(f"SHA256SUMS line {lineno} is blank")
        match = _SUM_LINE_RE.match(line)
        if not match:
            raise PiReleaseError(f"SHA256SUMS line {lineno} is malformed")
        digest = match.group(1).lower()
        filename = match.group(2)
        _validate_asset_name(filename, lineno)
        if filename in entries:
            raise PiReleaseError(
                f"SHA256SUMS contains duplicate filename {filename!r}"
            )
        entries[filename] = digest
    if not entries:
        raise PiReleaseError("SHA256SUMS is empty")
    return entries


def required_install_digests(entries: dict[str, str]) -> tuple[str, str]:
    """Require both installation assets and return their hex digests.

    Returns ``(install_package_digest, install_package_lock_digest)``.
    """
    missing = [f for f in _REQUIRED_FILENAMES if f not in entries]
    if missing:
        raise PiReleaseError(
            "SHA256SUMS is missing required asset(s): " + ", ".join(missing)
        )
    return (entries[INSTALL_PACKAGE_FILENAME], entries[INSTALL_PACKAGE_LOCK_FILENAME])


def _verify_digest(data: bytes, expected_hex: str, filename: str) -> None:
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected_hex.lower():
        raise PiReleaseError(
            f"SHA256 mismatch for {filename!r}: expected {expected_hex.lower()}, "
            f"got {actual}"
        )


def download_bytes(
    transport: StreamingTransport,
    url: str,
    *,
    progress: ProgressCallback | None = None,
) -> bytes:
    """Stream *url* through *transport* (a ``.stream()`` provider) into memory.

    When *progress* is supplied it receives the cumulative received bytes after
    every chunk the existing streaming boundary already yields, without adding
    a request, buffering the whole body twice, or changing transport semantics.
    """
    chunks: list[bytes] = []
    received = 0
    try:
        for chunk in transport.stream(url):
            if not isinstance(chunk, bytes):
                raise PiReleaseError("release-asset transport yielded non-byte data")
            chunks.append(chunk)
            received += len(chunk)
            if progress is not None:
                progress(received)
    except PiReleaseError:
        raise
    except Exception as exc:
        # Retain the cause so the shared projection can report its bounded
        # exception-type chain without evaluating any message.
        raise PiReleaseError(
            f"release-asset transport failed ({type(exc).__name__})"
        ) from exc
    return b"".join(chunks)


# ``download`` has three supported shapes.  The legacy one-argument callable is
# used when no activity scope is supplied.  A definite activity scope always
# calls ``download(url, progress)``, so a callback with a mandatory progress
# argument is valid there.  An optional activity value may take either runtime
# branch, so it requires the ``ProgressAwareDownload`` protocol whose progress
# parameter is defaulted.  The overloads reject asking for observation with a
# callback that can receive neither a URL alone nor a URL with the observer.
@overload
def acquire_install_assets(
    urls: PiReleaseUrls,
    download: Callable[[str], bytes],
    *,
    activity: None = None,
) -> tuple[bytes, bytes]: ...


@overload
def acquire_install_assets(
    urls: PiReleaseUrls,
    download: Callable[[str, ProgressCallback | None], bytes],
    *,
    activity: AssetActivityFactory,
) -> tuple[bytes, bytes]: ...


@overload
def acquire_install_assets(
    urls: PiReleaseUrls,
    download: ProgressAwareDownload,
    *,
    activity: AssetActivityFactory | None = None,
) -> tuple[bytes, bytes]: ...


def acquire_install_assets(
    urls: PiReleaseUrls,
    download: Callable[..., bytes],
    *,
    activity: AssetActivityFactory | None = None,
) -> tuple[bytes, bytes]:
    """Download and checksum-verify the two installation assets.

    SHA256SUMS is fetched and strictly parsed first; both required entries
    must be present.  Each installation asset is then downloaded and its
    SHA-256 must match the parsed digest.  Returns
    ``(install_package_bytes, install_package_lock_bytes)``.  Missing assets,
    digest mismatches, and transport failures raise :class:`PiReleaseError`.

    When *activity* is supplied it opens one observation scope per closed
    logical asset; the scope yields an optional progress callback passed to
    *download*, and a failure inside the scope (including digest verification)
    is attributed to that asset.  Without *activity* the legacy one-argument
    ``download(url)`` contract is used, so existing callers that cannot accept
    a progress callback keep working.
    """
    def fetch(
        name: str, url: str, verify: Callable[[bytes], object] | None = None,
    ) -> bytes:
        if activity is None:
            data = download(url)
        else:
            with activity(name) as progress:
                data = download(url, progress)
                if verify is not None:
                    verify(data)
                return data
        if verify is not None:
            verify(data)
        return data

    required: list[tuple[str, str]] = []

    def _parse_and_require(raw: bytes) -> None:
        required.append(required_install_digests(parse_sha256sums(raw)))

    fetch(SHA256SUMS_FILENAME, urls.sha256sums, _parse_and_require)
    package_digest, lock_digest = required[0]
    package = fetch(
        INSTALL_PACKAGE_FILENAME, urls.install_package,
        lambda data: _verify_digest(data, package_digest, INSTALL_PACKAGE_FILENAME),
    )
    lock = fetch(
        INSTALL_PACKAGE_LOCK_FILENAME, urls.install_package_lock,
        lambda data: _verify_digest(data, lock_digest, INSTALL_PACKAGE_LOCK_FILENAME),
    )
    return package, lock


__all__ = [
    "INSTALL_PACKAGE_FILENAME",
    "INSTALL_PACKAGE_LOCK_FILENAME",
    "PiReleaseError",
    "PiReleaseUrls",
    "ProgressAwareDownload",
    "ProgressCallback",
    "SHA256SUMS_FILENAME",
    "acquire_install_assets",
    "derive_pi_release_urls",
    "download_bytes",
    "parse_sha256sums",
    "required_install_digests",
]
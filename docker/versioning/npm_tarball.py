"""Focused npm tarball URL identity validation.

This module is dependency-free beyond the stdlib.  Both
:mod:`docker.versioning.model` and :mod:`docker.runtime_installer`
import it so that a single canonical validator governs npm tarball
URL identity at every boundary.

Each boundary maps the neutral :class:`NpmTarballUrlError` to its own
exception hierarchy (``InvalidArtifactKey`` in the model,
``ProjectionError`` in the installer).
"""

from __future__ import annotations

from urllib.parse import urlparse


class NpmTarballUrlError(ValueError):
    """A URL does not conform to the npm tarball identity contract."""


def validate(url: str, package: str, version_key: str) -> None:
    """Validate *url* is an exact npm registry tarball for *package*.

    Expected format::

        https://registry.npmjs.org/<package>/-/<pkg_name>-<version>.tgz

    where ``<pkg_name>`` is the last path segment of *package* and
    ``<version>`` is *version_key* with any ``+build`` metadata
    stripped (npm tarball filenames never include build metadata).
    The host must be exactly ``registry.npmjs.org`` (no other host,
    port, or userinfo) and the scheme must be HTTPS.

    Raises :class:`NpmTarballUrlError` for any violation.
    """
    parsed = urlparse(url)

    # -- scheme ----------------------------------------------------
    if parsed.scheme != "https":
        raise NpmTarballUrlError(f"npm tarball URL must use HTTPS, got {url!r}")

    # -- host ------------------------------------------------------
    if parsed.netloc != "registry.npmjs.org":
        raise NpmTarballUrlError(
            f"npm tarball URL host must be registry.npmjs.org, got {url!r}"
        )

    url_path = parsed.path

    # Package stem: /@scope/name/-/  or  /name/-/
    expected_stem = f"/{package}/-/"
    if not url_path.startswith(expected_stem):
        raise NpmTarballUrlError(f"expected npm tarball for {package!r}, got {url!r}")

    # Extract the part after /-/
    _, _, tarball_name = url_path.partition(expected_stem)
    if not tarball_name:
        raise NpmTarballUrlError(f"missing tarball filename after /-/, got {url!r}")

    # Tarball must end with .tgz
    if not tarball_name.endswith(".tgz"):
        raise NpmTarballUrlError(f"expected .tgz tarball, got {tarball_name!r}")

    # Strip build metadata for filename matching
    # (npm tarballs never include it)
    base_version = version_key.split("+", 1)[0]

    # The last path segment of the package
    # (e.g. "pi-read" from "@arcanemachine/pi-read")
    pkg_name = package.rsplit("/", 1)[-1]

    # Expected filename: <pkg_name>-<base_version>.tgz
    expected_filename = f"{pkg_name}-{base_version}.tgz"
    if tarball_name != expected_filename:
        raise NpmTarballUrlError(
            f"expected tarball {expected_filename!r} "
            f"for {package!r} version {base_version!r}, "
            f"got {tarball_name!r}"
        )

    # No query / fragment allowed — prevents version-leak via
    # ?ref=1.2.3
    if parsed.query or parsed.fragment:
        raise NpmTarballUrlError(
            f"query/fragment not allowed in reviewed artifact URL, got {url!r}"
        )

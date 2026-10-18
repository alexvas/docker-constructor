"""Focused semver identity validation and parsing.

This module is dependency-free beyond the stdlib.  Both
:mod:`docker.versioning.model` and :mod:`docker.runtime_installer`
import it so that a single canonical semver contract governs version
identity at every boundary.

Each boundary maps the neutral :class:`SemverError` to its own
exception hierarchy (``InvalidArtifactKey`` in the model,
``ProjectionError`` in the installer).

:func:`parse` returns a :class:`SemanticVersion` with full ordering
support, replacing bespoke regexes throughout the codebase.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# ---------------------------------------------------------------------------
# Moving tags — rejected by :func:`validate` / :func:`parse`.
# ---------------------------------------------------------------------------

_MOVING_VERSION_TAGS: frozenset[str] = frozenset(
    {"latest", "stable", "next", "dev", "canary", "nightly"}
)

# ---------------------------------------------------------------------------
# Semver patterns from https://semver.org.
# ---------------------------------------------------------------------------

# Validation regex — no groups needed (.match for truthiness).
_SEMVER_RE: re.Pattern[str] = re.compile(
    r"""
    ^
    (0|[1-9]\d*)               # major
    \.
    (0|[1-9]\d*)               # minor
    \.
    (0|[1-9]\d*)               # patch
    (?:
        -                      # prerelease hyphen
        (?:                     # prerelease identifiers
            (?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)
            (?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*
        )
    )?
    (?:
        \+                     # build hyphen
        [0-9a-zA-Z-]+          # build identifiers
        (?:\.[0-9a-zA-Z-]+)*
    )?
    $
    """,
    re.VERBOSE,
)

# Parse regex — same pattern with named capturing groups.
_PARSE_RE: re.Pattern[str] = re.compile(
    r"""
    ^
    (?P<major>0|[1-9]\d*)
    \.
    (?P<minor>0|[1-9]\d*)
    \.
    (?P<patch>0|[1-9]\d*)
    (?:
        -
        (?P<prerelease>
            (?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)
            (?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*
        )
    )?
    (?:
        \+
        (?P<build>
            [0-9a-zA-Z-]+
            (?:\.[0-9a-zA-Z-]+)*
        )
    )?
    $
    """,
    re.VERBOSE,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class SemverError(ValueError):
    """A version string is not a valid, non-moving semver."""


@dataclass(frozen=True, order=False)
class SemanticVersion:
    """Strict semver.org 2.0.0 version.

    Ordering follows semver precedence:

    * numeric identifiers compare numerically
    * numeric prerelease identifier is lower than non-numeric
    * stable (no prerelease) is higher than any prerelease
    * build metadata does **not** affect ordering
    * leading zero in numeric prerelease is invalid (caught at parse time)
    """

    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()
    build: tuple[str, ...] = ()

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            base += "-" + ".".join(self.prerelease)
        if self.build:
            base += "+" + ".".join(self.build)
        return base

    @property
    def is_prerelease(self) -> bool:
        return bool(self.prerelease)

    @property
    def is_stable(self) -> bool:
        return not self.prerelease

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SemanticVersion):
            return NotImplemented
        if self.major != other.major:
            return self.major < other.major
        if self.minor != other.minor:
            return self.minor < other.minor
        if self.patch != other.patch:
            return self.patch < other.patch
        # Prerelease: stable > any prerelease
        if not self.prerelease and other.prerelease:
            return False  # self is stable, other is prerelease → self > other
        if self.prerelease and not other.prerelease:
            return True  # self is prerelease, other is stable → self < other
        if not self.prerelease and not other.prerelease:
            return False  # equal components, both stable
        # Both have prerelease: compare identifiers pairwise
        return _compare_prerelease(self.prerelease, other.prerelease) < 0

    def __le__(self, other: object) -> bool:
        return self == other or self < other

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, SemanticVersion):
            return NotImplemented
        return other < self

    def __ge__(self, other: object) -> bool:
        return self == other or self > other

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SemanticVersion):
            return NotImplemented
        return (
            self.major == other.major
            and self.minor == other.minor
            and self.patch == other.patch
            and self.prerelease == other.prerelease
        )

    def __hash__(self) -> int:
        return hash((self.major, self.minor, self.patch, self.prerelease))


# ---------------------------------------------------------------------------
# Prerelease ordering helpers (semver.org §11)
# ---------------------------------------------------------------------------


def _compare_prerelease(a: tuple[str, ...], b: tuple[str, ...]) -> int:
    """Compare two prerelease identifier tuples per semver.org precedence.

    Returns negative if a < b, 0 if equal, positive if a > b.
    """
    for pa, pb in zip(a, b):
        result = _compare_prerelease_identifier(pa, pb)
        if result != 0:
            return result
    if len(a) < len(b):
        return -1
    if len(a) > len(b):
        return 1
    return 0


def _compare_prerelease_identifier(a: str, b: str) -> int:
    """Compare two prerelease identifiers per semver.org rules.

    * Both numeric: compare numerically
    * One numeric, one non-numeric: numeric sorts lower
    * Both non-numeric: compare lexically
    """
    a_int = _try_int(a)
    b_int = _try_int(b)
    if a_int is not None and b_int is not None:
        if a_int < b_int:
            return -1
        if a_int > b_int:
            return 1
        return 0
    if a_int is not None:
        return -1
    if b_int is not None:
        return 1
    if a < b:
        return -1
    if a > b:
        return 1
    return 0


def _try_int(s: str) -> Optional[int]:
    """Return int(s) if *s* consists entirely of digits."""
    if s.isdigit():
        return int(s)
    return None


# ---------------------------------------------------------------------------
# validate / parse
# ---------------------------------------------------------------------------


def validate(version: str) -> None:
    """Validate *version* as an exact, non-moving semver.

    Rejects:

    * Moving tags (``latest``, ``stable``, ``next``, …)
    * Ranges (``*``, ``^1.0.0``, ``~1``, ``>=1.0.0``, …)
    * Malformed versions (``v1.2.3``, ``1.2``, ``1.2.3-!!!``, …)

    Accepts:

    * Stable releases: ``1.2.3``
    * Prereleases: ``1.2.3-alpha.1``, ``1.0.0-rc.2``
    * Build metadata: ``1.2.3+build.20250101``
    * Both: ``1.2.3-beta.1+exp.sha.5114f85``

    Raises :class:`SemverError` on any violation.
    """
    v = version.strip()
    if not v or v.lower() in _MOVING_VERSION_TAGS:
        raise SemverError(f"is a moving tag or empty, not an exact semver: {version!r}")
    if not _SEMVER_RE.match(v):
        raise SemverError(f"invalid semver: {version!r}")


def parse(version: str) -> SemanticVersion:
    """Parse *version* into a :class:`SemanticVersion` with ordering.

    Accepts the same inputs as :func:`validate` — exact, non-moving
    semver, including prerelease and build metadata.

    Raises :class:`SemverError` on any violation.
    """
    v = version.strip()
    if not v or v.lower() in _MOVING_VERSION_TAGS:
        raise SemverError(f"is a moving tag or empty, not an exact semver: {version!r}")
    m = _PARSE_RE.match(v)
    if not m:
        raise SemverError(f"invalid semver: {version!r}")

    prerelease_str = m.group("prerelease")
    build_str = m.group("build")

    return SemanticVersion(
        major=int(m.group("major")),
        minor=int(m.group("minor")),
        patch=int(m.group("patch")),
        prerelease=tuple(prerelease_str.split(".")) if prerelease_str else (),
        build=tuple(build_str.split(".")) if build_str else (),
    )

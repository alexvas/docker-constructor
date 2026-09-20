"""Closed logical host-resource identities shared without circular imports.

Operational host events (:mod:`docker.versioning.host_progress`) and safe host
diagnostics (:mod:`docker.versioning.diagnostic_projection`) must accept only
the closed set of reviewed build-artifact names, authoritative Pi release asset
names, and the fixed ``npm-assembler-<digest-prefix>`` assembler container form.
Keeping the constants, the resource DTO, and the validation helpers here lets
both modules share one authoritative contract without ``host_progress``
importing the diagnostic projector (which would be circular).

This module deliberately depends only on the standard library and the
assembler digest-prefix length; it has no host-pipeline, transport, Docker, or
presentation dependencies.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from docker.npm_environment.identity import ASSEMBLER_DIGEST_PREFIX_LENGTH

#: Closed logical names of the reviewed build artifacts.
REVIEWED_ARTIFACT_NAMES = frozenset({"rustup", "uv", "rtk", "fd"})

#: Closed logical names of the authoritative Pi release assets.
PI_RELEASE_ASSET_NAMES = frozenset(
    {
        "SHA256SUMS",
        "pi-coding-agent-install-package.json",
        "pi-coding-agent-install-package-lock.json",
    }
)


class DiagnosticResourceKind(StrEnum):
    """Closed kind of a validated logical host resource."""

    REVIEWED_ARTIFACT = "reviewed_artifact"
    PI_RELEASE_ASSET = "pi_release_asset"
    ASSEMBLER_CONTAINER = "assembler_container"


_ASSEMBLER_CONTAINER_RE = re.compile(
    rf"npm-assembler-[0-9a-f]{{{ASSEMBLER_DIGEST_PREFIX_LENGTH}}}"
)


def is_approved_logical_resource_name(name: object) -> bool:
    """Return whether *name* is one of the closed safe logical asset names."""
    if not isinstance(name, str):
        return False
    return (
        name in REVIEWED_ARTIFACT_NAMES
        or name in PI_RELEASE_ASSET_NAMES
        or _ASSEMBLER_CONTAINER_RE.fullmatch(name) is not None
    )


def require_approved_logical_resource(value: object, label: str) -> None:
    """Validate that *value* is ``None`` or an approved logical asset name.

    ``None`` stays valid for steps that are not scoped to one closed asset.  A
    non-string is a type error; a string outside the closed names is a value
    error, mirroring :class:`DiagnosticLogicalResource`.
    """
    if value is None:
        return
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string or None")
    if not is_approved_logical_resource_name(value):
        raise ValueError(
            f"{label} {value!r} is not an approved logical resource"
        )


@dataclass(frozen=True, slots=True)
class DiagnosticLogicalResource:
    """Validated safe logical identity for a host operation or diagnostic.

    Only the closed reviewed artifact names, the closed Pi release asset
    names, and the fixed
    ``npm-assembler-<digest[:ASSEMBLER_DIGEST_PREFIX_LENGTH]>`` container form
    (the authoritative lowercase hexadecimal prefix produced by
    :mod:`docker.npm_environment.execution`) are accepted; arbitrary resource
    labels are rejected before they can enter an event.
    """

    kind: DiagnosticResourceKind
    name: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, DiagnosticResourceKind):
            raise TypeError("kind must be a DiagnosticResourceKind member")
        if not isinstance(self.name, str):
            raise TypeError("logical resource name must be a string")
        if self.kind is DiagnosticResourceKind.REVIEWED_ARTIFACT:
            if self.name not in REVIEWED_ARTIFACT_NAMES:
                raise ValueError(
                    f"reviewed artifact name {self.name!r} is not a closed asset"
                )
        elif self.kind is DiagnosticResourceKind.PI_RELEASE_ASSET:
            if self.name not in PI_RELEASE_ASSET_NAMES:
                raise ValueError(
                    f"Pi release asset name {self.name!r} is not a closed asset"
                )
        elif _ASSEMBLER_CONTAINER_RE.fullmatch(self.name) is None:
            raise ValueError(
                "assembler container name must be the fixed "
                "'npm-assembler-<digest-prefix>' form"
            )


__all__ = [
    "PI_RELEASE_ASSET_NAMES",
    "REVIEWED_ARTIFACT_NAMES",
    "DiagnosticLogicalResource",
    "DiagnosticResourceKind",
    "is_approved_logical_resource_name",
    "require_approved_logical_resource",
]

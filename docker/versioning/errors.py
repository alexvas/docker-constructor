"""Exception hierarchy for docker versioning.

All messages accept or construct dot-path identifiers so callers can
pinpoint the offending TOML key.
"""
from __future__ import annotations


class VersionConfigError(ValueError):
    """Base for all docker/versioning errors.

    ``field`` optionally carries safe, structured owner metadata (a dot-path)
    that schema projection may publish without exposing the private message.
    """

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        if field is not None or not hasattr(self, "field"):
            self.field = field


class InventoryError(VersionConfigError):
    """Inventory structure or value is invalid.

    ``field`` is structured owner metadata for safe diagnostic projection; the
    message remains private recovery guidance and must not be parsed.
    """


class VersionSyntaxError(VersionConfigError):
    """Version string cannot be parsed."""


class ConstraintSyntaxError(VersionConfigError):
    """Constraint string cannot be parsed or is contradictory."""


class EffectiveConfigError(VersionConfigError):
    """Base for effective-configuration errors."""


class UnknownPathError(EffectiveConfigError):
    """Requested path does not exist in the configuration."""


class UnsupportedOverrideError(EffectiveConfigError):
    """Override requested for a non-overrideable or unsupported path."""


class OverrideValidationError(EffectiveConfigError):
    """Override value does not satisfy the entry's policy."""


class UpdateError(VersionConfigError):
    """Base for update-discovery errors."""


class ProviderUnavailableError(UpdateError):
    """Provider cannot be reached or returned malformed data."""


class UnknownFilterError(VersionConfigError):
    """One or more --only filters matched nothing."""

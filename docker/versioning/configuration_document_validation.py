"""Shared, safe parsing boundary for fixed constructor TOML documents."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
import tomllib
from types import MappingProxyType
from typing import Callable, Generic, Mapping, TypeVar, cast

from .errors import InventoryError, VersionConfigError


class DocumentRole(StrEnum):
    """The two fixed project configuration document roles."""

    REVIEWED = "reviewed"
    LOCAL = "local"


class DocumentErrorClassification(StrEnum):
    """Closed set of published configuration-document failure classifications."""

    MALFORMED_TOML = "malformed_toml"
    SCHEMA_ERROR = "schema_error"


_CLASSIFICATION_LABELS: dict[DocumentErrorClassification, str] = {
    DocumentErrorClassification.MALFORMED_TOML: "malformed TOML",
    DocumentErrorClassification.SCHEMA_ERROR: "schema_error",
}


@dataclass(frozen=True, slots=True)
class DocumentIdentity:
    """Closed document identity retained across parsing and validation."""

    role: DocumentRole
    path: Path

    def __post_init__(self) -> None:
        # Normalize the role through the closed enum so only the two fixed
        # document roles can ever reach parsing or error projection.
        object.__setattr__(self, "role", DocumentRole(self.role))
        object.__setattr__(self, "path", Path(self.path).resolve())


class ConfigurationDocumentError(InventoryError):
    """Safe projection of a document parse failure.

    Deliberately does not retain the parser exception: parser text can contain
    source fragments, values, or tokens from a machine-local configuration.
    """

    def __init__(
        self,
        identity: DocumentIdentity,
        *,
        classification: DocumentErrorClassification,
        field: str | None = None,
        line: int | None = None,
        column: int | None = None,
    ) -> None:
        # Normalize through the closed enum so an arbitrary classification
        # string can never reach or be rendered by the published error.
        normalized = DocumentErrorClassification(classification)
        self.role = identity.role
        self.path = identity.path
        self.classification = normalized
        self.field = field
        self.line = line if isinstance(line, int) else None
        self.column = column if isinstance(column, int) else None
        role_label = "local companion" if self.role is DocumentRole.LOCAL else "reviewed"
        displayed_classification = _CLASSIFICATION_LABELS[normalized]
        detail = (
            f"{role_label} configuration {self.path}: "
            f"{displayed_classification} ({normalized.value})"
        )
        if field is not None:
            detail += f" (field {field})"
        elif self.line is not None and self.column is not None:
            detail += f" (line {self.line}, column {self.column})"
        super().__init__(detail)


def project_schema_error(
    identity: DocumentIdentity,
    field: str | None = None,
) -> ConfigurationDocumentError:
    """Project an owner-attributable schema failure without its rejected value."""
    return ConfigurationDocumentError(
        identity,
        classification=DocumentErrorClassification.SCHEMA_ERROR,
        field=field,
    )


@dataclass(frozen=True, slots=True)
class ParsedConfigurationDocument:
    """A successfully parsed fixed configuration document."""

    identity: DocumentIdentity
    data: Mapping[str, object]


def parse_configuration_document(identity: DocumentIdentity) -> ParsedConfigurationDocument:
    """Read and parse one document once, safely projecting syntax errors.

    Parsing happens in :func:`_parse_toml`, whose frame returns before this
    function raises, so the decoded source, the parsed mapping, the parser
    exception, and any raw token never appear in the published traceback.
    """
    raw, line, column = _parse_toml(identity)
    if raw is None:
        raise ConfigurationDocumentError(
            identity,
            classification=DocumentErrorClassification.MALFORMED_TOML,
            line=line,
            column=column,
        )
    return ParsedConfigurationDocument(identity, MappingProxyType(raw))


def _parse_toml(
    identity: DocumentIdentity,
) -> tuple[dict[str, object] | None, int | None, int | None]:
    """Parse one document without raising, in a frame the error never retains.

    Returns the parsed mapping on success, or ``(None, line, column)`` for a
    syntax or encoding failure. Returning instead of raising keeps the decoded
    source and the parser exception out of every retained frame; only safe
    scalar coordinates cross the boundary.
    """
    try:
        source = identity.path.read_bytes().decode("utf-8")
        raw = tomllib.loads(source)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return None, getattr(exc, "lineno", None), getattr(exc, "colno", None)
    if not isinstance(raw, dict):  # tomllib currently returns dict; keep boundary closed.
        return None, None, None
    return raw, None, None


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ProjectedOwnerResult(Generic[T]):
    """Either a released value or a safe projected owner-schema error."""

    value: T | None = None
    error: ConfigurationDocumentError | None = None


def capture_owner_result(
    identity: DocumentIdentity,
    operation: Callable[[], T],
) -> ProjectedOwnerResult[T]:
    """Invoke *operation* and capture a safe result or projected error.

    The callback runs in this frame, which returns before the projection is
    raised, so a callback that closes over parsed document data is never
    retained by the published error.
    """
    try:
        return ProjectedOwnerResult(value=operation())
    except ConfigurationDocumentError as exc:
        return ProjectedOwnerResult(error=exc)
    except VersionConfigError as exc:
        return ProjectedOwnerResult(
            error=project_schema_error(identity, getattr(exc, "field", None))
        )


def release_owner_result(outcome: ProjectedOwnerResult[T]) -> T:
    """Raise a captured projection or return its value from a data-free frame.

    This frame holds only the outcome object, never the parsed document, its
    mapping, or the schema-validation callback, so the published traceback
    cannot reach any of them.
    """
    if outcome.error is not None:
        raise outcome.error
    return cast(T, outcome.value)


def validate_configuration_documents(
    identities: tuple[DocumentIdentity, ...],
    release: Callable[[tuple[ParsedConfigurationDocument, ...]], T],
) -> T:
    """Parse all applicable documents before releasing any result to consumers."""
    documents = tuple(parse_configuration_document(identity) for identity in identities)
    return release(documents)

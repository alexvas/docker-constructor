"""Production build-context confinement for host-only constructor documents.

Docker transmits everything under the selected build context to the daemon
unless an applicable ``.dockerignore`` excludes it.  The reviewed
``docker-constructor.toml`` and its fixed machine-local companion
``docker-constructor.local.toml`` are host-only inputs and must never enter a
build context, regardless of where the selected project lives or which context
the caller chooses.

This module owns that enforcement.  It:

* derives the effective primary build context (default: the selected project
  root; or an explicit ``BuildRequest.context``);
* computes the exact context-relative path for each fixed document only when it
  is safely contained by that context, using each document's lexical context
  entry rather than the symlink target it may resolve to;
* reads the existing applicable ignore rules fail-closed; and
* materializes a transaction-owned Dockerfile copy plus the Dockerfile-specific
  ignore file Docker associates with it, so forced exclusions cannot be negated
  by later project rules.

The generated files live only in private constructor transaction state and are
never written into the selected project.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .errors import VersionConfigError
from .local_project_configuration import resolve_local_companion_path


class ConfinementError(VersionConfigError):
    """Host-only constructor documents cannot be safely confined."""


@dataclass(frozen=True, slots=True)
class BuildContextConfinement:
    """Planned confinement for one build transaction (no side effects)."""

    context: Path
    """Canonical effective primary build context root."""

    dockerfile_requested: Path | None
    """Lexical absolute requested Dockerfile; ``None`` when inactive.

    Docker associates a Dockerfile-specific ``.dockerignore`` with this path,
    so this is what ignore-rule selection must use.
    """

    dockerfile_source: Path | None
    """Canonical resolved Dockerfile whose bytes are copied; ``None`` when inactive."""

    relative_documents: tuple[str, ...]
    """Exact lexical context-relative paths of the fixed documents to exclude."""

    seed_rules: tuple[str, ...]
    """Existing applicable ignore rules, preserved verbatim."""

    @property
    def active(self) -> bool:
        """True when at least one fixed document is inside the context."""
        return bool(self.relative_documents)


@dataclass(frozen=True, slots=True)
class MaterializedConfinement:
    """Transaction-owned generated Dockerfile and ignore file."""

    directory: Path
    dockerfile: Path
    ignorefile: Path


def _canonical(path: Path) -> Path:
    """Return *path* with symlinks resolved, failing closed on error."""
    try:
        return Path(os.path.realpath(os.fspath(path)))
    except (OSError, TypeError, ValueError) as exc:  # pragma: no cover - defensive
        raise ConfinementError(f"cannot resolve path {path!r}: {exc}") from exc


def _lexical_absolute(path: Path) -> Path:
    """Return *path* absolute with lexical ``.``/``..`` normalization only.

    Unlike :func:`_canonical` this never follows symlinks, so the returned path
    names the exact context entry Docker would transmit even when the entry is
    a symlink whose target lives outside the context.
    """
    try:
        return Path(os.path.abspath(os.fspath(path)))
    except (OSError, TypeError, ValueError) as exc:
        raise ConfinementError(
            f"cannot derive a lexical path for {path!r}: {exc}"
        ) from exc


def _context_relative(path: Path, context: Path) -> str | None:
    """Return *path* relative to *context*, or ``None`` when it is outside."""
    try:
        relative = path.relative_to(context)
    except ValueError:
        return None
    text = relative.as_posix()
    if text in ("", "."):
        return None
    return text


def _resolve_dockerfile(context: Path, dockerfile: str | None) -> tuple[Path, Path]:
    """Return the lexical requested Dockerfile and its canonical content source.

    The requested path is kept lexical (symlinks are not followed) because
    Docker associates a Dockerfile-specific ``.dockerignore`` with the path the
    caller requested.  The canonical source is used only to read the Dockerfile
    bytes safely, and must be a regular file.
    """
    if dockerfile is None:
        requested_base = context / "Dockerfile"
    else:
        candidate = Path(dockerfile)
        requested_base = candidate if candidate.is_absolute() else context / candidate
    requested = _lexical_absolute(requested_base)
    source = _canonical(requested)
    if not source.is_file():
        raise ConfinementError(f"build Dockerfile cannot be copied safely: {requested}")
    return requested, source


def _read_seed_rules(context: Path, dockerfile_requested: Path) -> tuple[str, ...]:
    """Read the existing effective ignore rules, preserving them verbatim.

    A Dockerfile-specific ignore file takes precedence over the context-root
    ``.dockerignore``; when copies of the Dockerfile are made for confinement,
    discarding either would enlarge the transmitted context.  The Dockerfile-
    specific file is looked up beside the *requested* Dockerfile path, never
    beside a symlink target, because that is what Docker would apply.

    Filesystem and decoding failures fail closed; a malformed ignore file is
    never silently skipped nor replaced by the context-root rules.
    """
    candidates = (
        Path(os.fspath(dockerfile_requested) + ".dockerignore"),
        context / ".dockerignore",
    )
    for candidate in candidates:
        try:
            if candidate.is_file():
                return tuple(candidate.read_text(encoding="utf-8").splitlines())
        except (OSError, UnicodeError) as exc:
            raise ConfinementError(
                f"cannot read build ignore rules from {candidate}: {exc}"
            ) from exc
    return ()


def plan_build_context_confinement(
    *,
    inventory_path: Path,
    context: str | None,
    dockerfile: str | None,
) -> BuildContextConfinement:
    """Derive and validate build-context confinement without side effects.

    Returns an inactive plan when neither fixed document is contained by the
    effective context.  Raises :class:`ConfinementError` when containment
    cannot be determined or the Dockerfile/ignore rules cannot be read safely.

    Containment is decided from each document's *lexical* absolute context
    entry, never its resolved target: Docker transmits the entry Docker would
    see under the context, so a document symlinked to a target outside the
    context still needs its in-context entry excluded.  The context itself is
    resolved canonically so a symlinked context root cannot confuse the
    decision.
    """
    inventory = Path(inventory_path)
    raw_context = Path(context) if context is not None else inventory.parent
    context_root = _canonical(raw_context)
    # Docker is handed the context path the caller supplied, and entries are
    # named relative to that view.  Keep the canonical root for safety and the
    # lexical root so a symlinked context ancestor cannot make an in-context
    # entry look external.
    lexical_context = _lexical_absolute(raw_context)
    context_roots = (
        (context_root,)
        if lexical_context == context_root
        else (context_root, lexical_context)
    )

    documents: list[str] = []
    for candidate in (
        inventory,
        resolve_local_companion_path(inventory),
    ):
        entry = _lexical_absolute(candidate)
        relative: str | None = None
        for root in context_roots:
            relative = _context_relative(entry, root)
            if relative is not None:
                break
        if relative is not None and relative not in documents:
            documents.append(relative)

    if not documents:
        return BuildContextConfinement(context_root, None, None, (), ())

    dockerfile_requested, dockerfile_source = _resolve_dockerfile(
        context_root, dockerfile
    )
    seed_rules = _read_seed_rules(context_root, dockerfile_requested)
    return BuildContextConfinement(
        context_root,
        dockerfile_requested,
        dockerfile_source,
        tuple(documents),
        seed_rules,
    )


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:  # pragma: no cover - defensive
                raise ConfinementError(f"short write to {path}")
            view = view[written:]
    finally:
        os.close(fd)


def cleanup_build_context_confinement(
    materialized: MaterializedConfinement | None,
) -> None:
    """Remove generated confinement state using the snapshot cleanup facility."""
    if materialized is None:
        return
    from .build_snapshot import cleanup_artifact_snapshot

    cleanup_artifact_snapshot(materialized.directory)


def confinement_ignore_rules(confinement: BuildContextConfinement) -> tuple[str, ...]:
    """Return the generated ignore-file body: seed rules then forced exclusions.

    The forced exclusions are appended last so that Docker's last-match-wins
    semantics make them immune to earlier user negation patterns such as
    ``!docker-constructor.local.toml``.
    """
    rules = [*confinement.seed_rules, *confinement.relative_documents]
    return tuple(rule for rule in rules if rule.strip())


def materialize_build_context_confinement(
    confinement: BuildContextConfinement,
    *,
    generated_root: Path,
) -> MaterializedConfinement:
    """Publish the transaction-owned Dockerfile copy and ignore file.

    Raises :class:`ConfinementError` and removes any partial output when the
    Dockerfile or ignore file cannot be published securely.
    """
    if not confinement.active or confinement.dockerfile_source is None:
        raise ConfinementError("confinement is not active")
    try:
        directory = Path(tempfile.mkdtemp(prefix="build-context-", dir=generated_root))
    except OSError as exc:
        raise ConfinementError(
            f"cannot create build-context confinement state: {exc}"
        ) from exc
    try:
        os.chmod(directory, 0o700)
        requested = confinement.dockerfile_requested
        name = requested.name if requested is not None else confinement.dockerfile_source.name
        dockerfile = directory / name
        _write_private(dockerfile, confinement.dockerfile_source.read_bytes())
        ignorefile = directory / (dockerfile.name + ".dockerignore")
        body = "\n".join(confinement_ignore_rules(confinement)) + "\n"
        _write_private(ignorefile, body.encode("utf-8"))
        return MaterializedConfinement(directory, dockerfile, ignorefile)
    except (OSError, ConfinementError) as exc:
        shutil.rmtree(directory, ignore_errors=True)
        if isinstance(exc, ConfinementError):
            raise
        raise ConfinementError(
            f"cannot publish build-context confinement state: {exc}"
        ) from exc
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise

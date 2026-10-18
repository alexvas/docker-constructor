"""Deterministic Python source selection and repository check entry points.

One inventory feeds linting, formatting and syntax compilation:

* every ``.py``/``.pyi`` file under ``docker/``, ``tests/`` and ``scripts/``;
* every extensionless file under those roots whose first line is a recognized
  Python shebang (a direct interpreter path or ``/usr/bin/env`` naming
  ``python``, ``python3`` or a ``python3.<minor>`` interpreter);
* every explicit entry in :data:`EXPLICIT_PYTHON_SOURCES` (for a Python
  script that has no recognizable shebang).

The walk is deterministic (sorted), never follows a symlink, never enters a
vendor, generated or cache directory and records every exclusion with its
reason.  An unreadable or unclassified candidate raises
:class:`SourceSelectionError` instead of disappearing from coverage.
Extensionless sources are passed to Ruff explicitly because Ruff's own
recursion only discovers ``.py``/``.pyi``, and
:func:`inventory_parity_errors` rejects a configuration exclusion that
hides an inventoried module while ignoring Ruff discovery beneath directories
that the shared inventory deliberately excludes.  Before claiming success,
each Ruff mode also verifies that Ruff reports processing every selected path,
including extensionless scripts and paths affected by mode-specific or
``force-exclude`` configuration.

Compilation uses :func:`compile` on the raw source bytes, so a source-encoding
declaration is honoured while no application code executes and no bytecode is
written.  Ruff runs are read-only: ``check`` and ``format --check`` never
rewrite sources and run with the repository configuration and no cache.

Descriptor and subprocess ownership follow ``docs/filesystem-lifecycle.md`` and
``docs/process-lifecycle.md``: regular-file reads adopt one
``OwnedDescriptor`` per open, and every Ruff invocation is one
``OwnedProcess`` with one internal stream owner per captured pipe.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# Repository modules are imported below; never leave bytecode in the source tree.
sys.dont_write_bytecode = True

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from docker.filesystem.descriptors import (  # noqa: E402
    DescriptorError,
    DirectoryDescriptor,
    OwnedDescriptor,
)
from docker.filesystem.operations import PosixDescriptorOps  # noqa: E402
from docker.process import (  # noqa: E402
    OwnedProcess,
    ProcessResult,
    ProcessStream,
)

SOURCE_ROOTS = ("docker", "tests", "scripts")
SOURCE_SUFFIXES = (".py", ".pyi")
EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
        "__pycache__",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "generated",
        "vendor",
    }
)
EXPLICIT_PYTHON_SOURCES: Mapping[str, str] = {}

_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_READ_CHUNK = 64 * 1024
_CAPTURE_LIMIT = 16 * 1024 * 1024
_DISCOVERY_ARGS = (*SOURCE_ROOTS,)
_SHEBANG_PREFIX = b"#!"
_PYTHON_INTERPRETER = re.compile(r"python(?:3(?:\.\d+)*)?")


class CheckToolError(RuntimeError):
    """A required check tool is unavailable or cannot resolve its inputs."""


class RuffUnavailable(CheckToolError):
    """The development Ruff executable is not installed for this interpreter."""


class RuffDiscoveryError(CheckToolError):
    """Ruff cannot report the file set its own configuration would check."""


class SourceSelectionError(Exception):
    """A scoped source candidate cannot be classified or read."""


@dataclass(frozen=True)
class Exclusion:
    """One recorded path exclusion with the reason it is not Python source."""

    path: str
    reason: str


@dataclass(frozen=True)
class SourceSelection:
    """The deterministic selected inventory and its recorded exclusions."""

    selected: tuple[str, ...]
    exclusions: tuple[Exclusion, ...]


@dataclass(frozen=True)
class CommandResult:
    """One captured command outcome; ``clean`` is independent of the status.

    ``failures`` preserves the process owner's lifecycle failures in their
    authoritative order (the primary operation failure before a later release
    failure), so a wrapper never reduces a failed run to a bare status.
    """

    returncode: int | None
    stdout: bytes
    stderr: bytes
    clean: bool
    failures: tuple[BaseException, ...] = ()


def _read_entry(directory: DirectoryDescriptor, name: str, relative: str) -> bytes:
    """Read one regular-file entry through a single no-follow descriptor."""
    ops = PosixDescriptorOps()
    label = f"python source entry {relative}"
    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=directory.fd)
    except OSError as exc:
        raise SourceSelectionError(f"{relative}: cannot open candidate: {exc}") from exc
    owner = OwnedDescriptor(ops, fd, label=label)
    chunks: list[bytes] = []
    try:
        with owner:
            while True:
                try:
                    chunk = os.read(owner.fd, _READ_CHUNK)
                except OSError as exc:
                    raise SourceSelectionError(
                        f"{relative}: cannot read candidate: {exc}"
                    ) from exc
                if not chunk:
                    break
                chunks.append(chunk)
    except OSError as exc:
        raise SourceSelectionError(
            f"{relative}: cannot release candidate: {exc}"
        ) from exc
    return b"".join(chunks)


def _is_python_interpreter(name: str) -> bool:
    """Recognize ``python``, ``python3`` and versioned ``python3.<minor>`` names."""
    return _PYTHON_INTERPRETER.fullmatch(name) is not None


def _has_python_shebang(first_line: bytes) -> bool:
    """Recognize a direct or ``env``-mediated Python shebang without a shell."""
    if not first_line.startswith(_SHEBANG_PREFIX):
        return False
    try:
        text = first_line[len(_SHEBANG_PREFIX) :].decode("utf-8")
    except UnicodeDecodeError:
        return False
    parts = text.split()
    if not parts:
        return False
    interpreter = Path(parts[0]).name
    if interpreter == "env":
        return len(parts) >= 2 and _is_python_interpreter(Path(parts[1]).name)
    return _is_python_interpreter(interpreter)


def _classify_extensionless(
    directory: DirectoryDescriptor,
    name: str,
    relative: str,
    explicit: Mapping[str, str],
    visit: Callable[[str, DirectoryDescriptor, str], None],
    exclusions: list[Exclusion],
) -> None:
    """Classify one extensionless candidate by explicit entry or shebang."""
    if relative in explicit:
        visit(relative, directory, name)
        return
    first_line = _read_entry(directory, name, relative).split(b"\n", 1)[0]
    if _has_python_shebang(first_line):
        visit(relative, directory, name)
        return
    if first_line.startswith(_SHEBANG_PREFIX):
        exclusions.append(Exclusion(relative, "non-Python shebang; not Python source"))
        return
    raise SourceSelectionError(
        f"{relative}: extensionless file without a shebang is unclassified; "
        "add a Python shebang or list it in EXPLICIT_PYTHON_SOURCES"
    )


def _walk(
    directory: DirectoryDescriptor,
    prefix: str,
    explicit: Mapping[str, str],
    visit: Callable[[str, DirectoryDescriptor, str], None],
    exclusions: list[Exclusion],
) -> None:
    """Visit canonical entries beneath *directory* without following symlinks."""
    try:
        names = directory.list_names()
    except OSError as exc:
        raise SourceSelectionError(
            f"{prefix or '.'}: cannot list directory: {exc}"
        ) from exc
    for name in sorted(names):
        relative = f"{prefix}/{name}" if prefix else name
        try:
            info = directory.stat_child(name, follow_symlinks=False)
        except OSError as exc:
            raise SourceSelectionError(
                f"{relative}: cannot stat candidate: {exc}"
            ) from exc
        if stat.S_ISLNK(info.st_mode):
            if name.endswith(SOURCE_SUFFIXES):
                raise SourceSelectionError(
                    f"{relative}: symlinked Python source is not supported"
                )
            exclusions.append(Exclusion(relative, "symbolic link; not followed"))
            continue
        if stat.S_ISDIR(info.st_mode):
            if name in EXCLUDED_DIRECTORY_NAMES:
                exclusions.append(
                    Exclusion(
                        relative, "vendor, generated or cache directory; not scanned"
                    )
                )
                continue
            try:
                child = directory.open_directory(
                    name, label=relative, require_owner=False
                )
            except DescriptorError as exc:
                raise SourceSelectionError(
                    f"{relative}: cannot open directory: {exc}"
                ) from exc
            try:
                with child:
                    _walk(child, relative, explicit, visit, exclusions)
            except OSError as exc:
                raise SourceSelectionError(
                    f"{relative}: cannot release directory: {exc}"
                ) from exc
            continue
        if not stat.S_ISREG(info.st_mode):
            exclusions.append(Exclusion(relative, "not a regular file"))
            continue
        if name.endswith(SOURCE_SUFFIXES):
            visit(relative, directory, name)
            continue
        if Path(name).suffix:
            exclusions.append(
                Exclusion(relative, "non-Python extension; not Python source")
            )
            continue
        _classify_extensionless(directory, name, relative, explicit, visit, exclusions)


def _scan(
    root: str | os.PathLike[str],
    explicit: Mapping[str, str] | None,
    visit: Callable[[str, DirectoryDescriptor, str], None],
) -> SourceSelection:
    base = Path(root)
    if not base.is_absolute():
        raise ValueError("the source root must be an absolute path")
    mapping = EXPLICIT_PYTHON_SOURCES if explicit is None else explicit
    ops = PosixDescriptorOps()
    selected: list[str] = []
    exclusions: list[Exclusion] = []

    def record(relative: str, directory: DirectoryDescriptor, name: str) -> None:
        selected.append(relative)
        visit(relative, directory, name)

    for source_root in SOURCE_ROOTS:
        try:
            capability = DirectoryDescriptor.open_secure_path(
                ops, str(base / source_root), label=source_root, require_owner=False
            )
        except DescriptorError as exc:
            raise SourceSelectionError(
                f"source root {source_root!r} is not a readable directory: {exc}"
            ) from exc
        try:
            with capability:
                _walk(capability, source_root, mapping, record, exclusions)
        except OSError as exc:
            raise SourceSelectionError(
                f"source root {source_root!r}: cannot release directory: {exc}"
            ) from exc

    ordered = tuple(sorted(set(selected)))
    missing = tuple(sorted(path for path in mapping if path not in ordered))
    if missing:
        raise SourceSelectionError(
            "explicit Python sources were not found: " + ", ".join(missing)
        )
    exclusions.sort(key=lambda entry: entry.path)
    return SourceSelection(ordered, tuple(exclusions))


def _ignore(relative: str, directory: DirectoryDescriptor, name: str) -> None:
    """Selection needs the path only; contents stay unread."""


def select_sources(
    root: str | os.PathLike[str], *, explicit: Mapping[str, str] | None = None
) -> SourceSelection:
    """Return the deterministic shared Python source inventory."""
    return _scan(root, explicit, _ignore)


def _syntax_error_location(relative: str, error: SyntaxError) -> str:
    if error.lineno is None:
        return relative
    return f"{relative}:{error.lineno}"


def compile_sources(
    root: str | os.PathLike[str], *, explicit: Mapping[str, str] | None = None
) -> tuple[str, ...]:
    """Compile every selected source without importing or executing it."""
    errors: list[str] = []

    def visit(relative: str, directory: DirectoryDescriptor, name: str) -> None:
        try:
            source = _read_entry(directory, name, relative)
        except SourceSelectionError as exc:
            errors.append(str(exc))
            return
        try:
            compile(source, relative, "exec", dont_inherit=True)
        except SyntaxError as exc:
            errors.append(f"{_syntax_error_location(relative, exc)}: {exc.msg}")
        except ValueError as exc:
            errors.append(f"{relative}: {exc}")

    _scan(root, explicit, visit)
    return tuple(errors)


def find_ruff() -> str:
    """Return the Ruff executable of the running Python environment."""
    candidate = Path(sys.executable).parent / "ruff"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    located = shutil.which("ruff")
    if located is not None:
        return located
    raise RuffUnavailable(
        "ruff is not installed for this interpreter; run "
        "`uv pip install --python .venv/bin/python --group dev`"
    )


def _command_result(result: ProcessResult) -> CommandResult:
    return CommandResult(
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        clean=result.lifecycle.complete and not result.lifecycle.failures,
        failures=tuple(failure.error for failure in result.lifecycle.failures),
    )


def run_captured(argv: Sequence[str], *, root: str | os.PathLike[str]) -> CommandResult:
    """Run one command with captured pipes through the shared process owner."""
    with OwnedProcess.launch(
        list(argv),
        popen_kwargs={"cwd": str(root)},
        capture_limit=_CAPTURE_LIMIT,
    ) as owner:
        result = owner.capture()
    return _command_result(result)


def _forward(stream: ProcessStream, chunk: bytes) -> None:
    target = sys.stdout if stream is ProcessStream.STDOUT else sys.stderr
    buffer = getattr(target, "buffer", None)
    if buffer is None:
        target.write(chunk.decode("utf-8", errors="replace"))
    else:
        buffer.write(chunk)
    target.flush()


def run_streaming(
    argv: Sequence[str], *, root: str | os.PathLike[str]
) -> CommandResult:
    """Run one command, forwarding already-captured chunks as they arrive."""
    with OwnedProcess.launch(
        list(argv),
        popen_kwargs={"cwd": str(root)},
    ) as owner:
        result = owner.stream(_forward)
    return _command_result(result)


def _relative_ruff_paths(entries: Sequence[str], base: Path) -> tuple[str, ...]:
    """Normalize Ruff-reported paths to sorted repository-relative POSIX paths."""
    paths: list[str] = []
    for entry in entries:
        value = entry.strip()
        if not value:
            continue
        path = Path(value)
        if path.is_absolute():
            try:
                value = path.resolve().relative_to(base).as_posix()
            except ValueError:
                continue
        paths.append(value)
    return tuple(sorted(set(paths)))


def ruff_visible_files(
    root: str | os.PathLike[str], *, ruff: str | None = None
) -> tuple[str, ...]:
    """Return the Python files Ruff's own discovery would check under the roots."""
    executable = ruff if ruff is not None else find_ruff()
    result = run_captured(
        [executable, "check", "--show-files", "--no-cache", *_DISCOVERY_ARGS],
        root=root,
    )
    if result.returncode != 0 or not result.clean:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuffDiscoveryError(
            "cannot resolve Ruff discovery: "
            + (detail or f"exit status {result.returncode}")
        )
    visible = _relative_ruff_paths(
        result.stdout.decode("utf-8", errors="replace").splitlines(),
        Path(root).resolve(),
    )
    return tuple(entry for entry in visible if entry.endswith(SOURCE_SUFFIXES))


def ruff_processed_files(
    mode: str,
    root: str | os.PathLike[str],
    sources: Sequence[str],
    *,
    ruff: str | None = None,
) -> tuple[str, ...]:
    """Return every path the requested Ruff mode reports processing.

    Lint coverage uses ``check --show-files`` with the explicit inventory, so
    extensionless scripts are included and ``force-exclude`` is honoured.
    Format has no ``--show-files``; ``--check --verbose`` is read-only and
    reports each path the formatter actually processes.  A nonzero format
    status is expected for unformatted sources and does not prevent coverage
    validation.
    """
    executable = ruff if ruff is not None else find_ruff()
    if mode == "lint":
        result = run_captured(
            [
                executable,
                "check",
                "--show-files",
                "--no-cache",
                "--",
                *sources,
            ],
            root=root,
        )
        expected_statuses = (0,)
        output = result.stdout
        entries = output.decode("utf-8", errors="replace").splitlines()
    elif mode == "format":
        result = run_captured(
            [
                executable,
                "format",
                "--check",
                "--no-cache",
                "--verbose",
                "--",
                *sources,
            ],
            root=root,
        )
        expected_statuses = (0, 1)
        output = result.stderr
        marker = "[ruff::commands::format][DEBUG] format_path; path="
        entries = [
            line.split(marker, 1)[1]
            for line in output.decode("utf-8", errors="replace").splitlines()
            if marker in line
        ]
    else:
        raise ValueError(f"unsupported Ruff check mode: {mode!r}")
    if result.returncode not in expected_statuses or not result.clean:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuffDiscoveryError(
            f"cannot resolve Ruff {mode} coverage: "
            + (detail or f"exit status {result.returncode}")
        )
    return _relative_ruff_paths(entries, Path(root).resolve())


def _path_in_excluded_directory(relative: str) -> bool:
    """Return whether *relative* is beneath a deliberately excluded directory."""
    return any(
        part in EXCLUDED_DIRECTORY_NAMES for part in PurePosixPath(relative).parts[:-1]
    )


def inventory_parity_errors(
    selected: Sequence[str], visible: Sequence[str]
) -> tuple[str, ...]:
    """Report any mismatch between the shared inventory and Ruff discovery.

    Ruff may discover Python files beneath vendor, generated or cache
    directories even though the shared inventory deliberately omits those
    directories.  Such discovery is out of scope and is not a parity error.  A
    selected file missing from discovery remains an error, so a Ruff
    configuration exclusion cannot silently hide an inventoried module.
    """
    selection = set(selected)
    discovered = {path for path in visible if not _path_in_excluded_directory(path)}
    errors = [
        f"{path}: selected Python source is omitted by Ruff discovery"
        for path in sorted(
            path
            for path in selection
            if path.endswith(SOURCE_SUFFIXES) and path not in discovered
        )
    ]
    errors.extend(
        f"{path}: Ruff discovery reports a Python source outside the shared inventory"
        for path in sorted(discovered - selection)
    )
    return tuple(errors)


def ruff_coverage_errors(
    mode: str, selected: Sequence[str], processed: Sequence[str]
) -> tuple[str, ...]:
    """Report selected sources the requested Ruff mode did not process."""
    selection = set(selected)
    covered = set(processed)
    return tuple(
        f"{path}: selected Python source is omitted by Ruff {mode} coverage"
        for path in sorted(selection - covered)
    ) + tuple(
        f"{path}: Ruff {mode} coverage reports a source outside the shared inventory"
        for path in sorted(covered - selection)
    )


def _ruff_command(executable: str, mode: str, sources: Sequence[str]) -> list[str]:
    if mode == "lint":
        return [executable, "check", "--no-cache", "--", *sources]
    return [executable, "format", "--check", "--no-cache", "--", *sources]


def run_ruff_check(
    mode: str,
    root: str | os.PathLike[str],
    *,
    ruff: str | None = None,
) -> int:
    """Run one non-mutating Ruff mode over the complete shared inventory."""
    executable = ruff if ruff is not None else find_ruff()
    selection = select_sources(root)
    errors = inventory_parity_errors(
        selection.selected, ruff_visible_files(root, ruff=executable)
    )
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    coverage_errors = ruff_coverage_errors(
        mode,
        selection.selected,
        ruff_processed_files(mode, root, selection.selected, ruff=executable),
    )
    if coverage_errors:
        for error in coverage_errors:
            print(error, file=sys.stderr)
        return 1
    result = run_streaming(
        _ruff_command(executable, mode, selection.selected), root=root
    )
    if result.returncode is None or not result.clean:
        print("error: the Ruff check did not complete cleanly", file=sys.stderr)
        return 1
    return result.returncode


def _list_sources(root: str | os.PathLike[str]) -> int:
    selection = select_sources(root)
    for path in selection.selected:
        print(path)
    for entry in selection.exclusions:
        print(f"excluded {entry.path}: {entry.reason}", file=sys.stderr)
    return 0


def _compile_selection(root: str | os.PathLike[str]) -> int:
    errors = compile_sources(root)
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="mode", required=True)
    for mode, help_text in (
        ("list", "print the shared source inventory and its exclusions"),
        ("lint", "run the Ruff lint check over the shared inventory"),
        ("format", "run the Ruff format check over the shared inventory"),
        ("compile", "compile each shared source without executing it"),
    ):
        command = commands.add_parser(mode, help=help_text)
        command.add_argument(
            "--root",
            type=Path,
            default=_REPO_ROOT,
            help="repository root holding docker/, tests/ and scripts/",
        )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one check mode and return its process status."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    parsed = _parser().parse_args(arguments)
    root = parsed.root.resolve()
    try:
        if parsed.mode == "list":
            return _list_sources(root)
        if parsed.mode == "compile":
            return _compile_selection(root)
        return run_ruff_check(parsed.mode, root)
    except SourceSelectionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except RuffUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 127
    except CheckToolError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

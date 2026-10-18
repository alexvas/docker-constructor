"""Fixture helpers for the shared Python check tooling contracts.

The tooling lives in ``scripts/python_checks.py`` and is deliberately not an
importable package, so each contract module loads it by path.  Fixture trees
contain their own ``docker/``, ``tests/`` and ``scripts/`` roots because the
selector rejects a missing scoped root instead of silently shrinking coverage.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "scripts" / "python_checks.py"
SOURCE_ROOTS = ("docker", "tests", "scripts")


def load_check_module() -> Any:
    """Load the repository check tooling module by path."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    if not MODULE_PATH.is_file():
        raise FileNotFoundError(f"Python check tooling is missing: {MODULE_PATH}")
    spec = importlib.util.spec_from_file_location("python_checks_contract", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load Python check tooling from {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def prepare_roots(root: Path) -> None:
    """Create the three scoped source roots in a fixture repository."""
    for name in SOURCE_ROOTS:
        (root / name).mkdir(parents=True, exist_ok=True)


def write_source(
    root: Path,
    relative: str,
    content: str | bytes,
    *,
    mode: int = 0o644,
) -> Path:
    """Write one fixture file, creating parent directories."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    path.chmod(mode)
    return path


def source_digests(root: Path) -> dict[str, str]:
    """Return content digests for every regular fixture file."""
    digests: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        digests[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


def generated_bytecode(root: Path) -> tuple[str, ...]:
    """Return every bytecode cache directory or file beneath *root*."""
    generated: list[str] = []
    for path in sorted(root.rglob("*")):
        name = path.name
        if name == "__pycache__" or name.endswith((".pyc", ".pyo")):
            generated.append(path.relative_to(root).as_posix())
    return tuple(generated)

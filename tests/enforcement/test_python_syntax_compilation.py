"""Shared syntax compilation contracts (P2 task 2.2).

Compilation is a syntax check over the same shared inventory: it must accept a
valid extensionless Python script, honour a source-encoding declaration,
report invalid syntax with the repository-relative path, and neither execute
application code nor leave bytecode in the source tree.  The subprocess
contracts use a clean repository copy so imports made by the check tooling
cannot inherit bytecode caches from earlier in-process tests.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests.enforcement.python_check_fixtures import (
    REPO_ROOT,
    generated_bytecode,
    load_check_module,
    prepare_roots,
    source_digests,
    write_source,
)

VENV_PYTHON = REPO_ROOT / ".venv/bin/python"
_CLEAN_COPY_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")


def _clean_repository_copy(root):
    """Copy the check tooling and Docker package without bytecode caches."""
    repository = root / "repository"
    shutil.copytree(
        REPO_ROOT / "docker",
        repository / "docker",
        ignore=_CLEAN_COPY_IGNORE,
    )
    (repository / "tests").mkdir()
    (repository / "scripts").mkdir()
    for name in ("python_checks.py", "check-python-compile"):
        shutil.copy2(
            REPO_ROOT / "scripts" / name,
            repository / "scripts" / name,
        )
    venv_bin = repository / ".venv/bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").symlink_to(VENV_PYTHON)
    return repository


def _subprocess_environment():
    """Return an environment that cannot suppress bytecode writing."""
    environment = os.environ.copy()
    environment.pop("PYTHONDONTWRITEBYTECODE", None)
    environment.pop("PYTHONPYCACHEPREFIX", None)
    return environment


@pytest.fixture(scope="module")
def checks():
    return load_check_module()


def test_valid_extensionless_python_compiles(checks, tmp_path):
    prepare_roots(tmp_path)
    write_source(
        tmp_path,
        "scripts/python-tool",
        "#!/usr/bin/env python3\nvalue = 1\n",
        mode=0o755,
    )

    assert checks.compile_sources(tmp_path) == ()


def test_stub_sources_compile(checks, tmp_path):
    prepare_roots(tmp_path)
    write_source(tmp_path, "docker/package/module.pyi", "value: int\n")

    assert checks.compile_sources(tmp_path) == ()


def test_invalid_syntax_is_reported_with_the_relative_path(checks, tmp_path):
    prepare_roots(tmp_path)

    write_source(tmp_path, "docker/broken.py", "def broken(:\n")

    errors = checks.compile_sources(tmp_path)

    assert len(errors) == 1
    assert errors[0].startswith("docker/broken.py:")


def test_source_encoding_declaration_is_respected(checks, tmp_path):
    prepare_roots(tmp_path)
    write_source(
        tmp_path,
        "docker/latin_one.py",
        b"# -*- coding: latin-1 -*-\nvalue = '\xe9'\n",
    )

    assert checks.compile_sources(tmp_path) == ()


def test_compilation_has_no_application_side_effects(checks, tmp_path):
    prepare_roots(tmp_path)
    write_source(
        tmp_path,
        "docker/side_effect.py",
        (
            "from pathlib import Path\n"
            "Path(__file__).with_name('executed.marker').write_text('imported')\n"
            "value = 1\n"
        ),
    )

    assert checks.compile_sources(tmp_path) == ()
    assert not (tmp_path / "docker/executed.marker").exists()


def test_compilation_leaves_no_bytecode(checks, tmp_path):
    prepare_roots(tmp_path)
    write_source(tmp_path, "docker/module.py", "value = 1\n")
    write_source(
        tmp_path,
        "scripts/python-tool",
        "#!/usr/bin/env python3\nvalue = 2\n",
        mode=0o755,
    )
    before = source_digests(tmp_path)

    assert checks.compile_sources(tmp_path) == ()
    assert generated_bytecode(tmp_path) == ()
    assert source_digests(tmp_path) == before


@pytest.mark.parametrize("entry_point", ("wrapper", "direct"))
def test_clean_tree_compile_entry_point_creates_no_bytecode(tmp_path, entry_point):
    if not VENV_PYTHON.is_file():
        pytest.fail(
            "the project virtual environment is required to run the check entry "
            "points; see docs/testing.md"
        )
    repository = _clean_repository_copy(tmp_path)
    assert generated_bytecode(repository) == ()
    if entry_point == "wrapper":
        command = [str(repository / "scripts/check-python-compile")]
    else:
        command = [
            str(repository / ".venv/bin/python"),
            str(repository / "scripts/python_checks.py"),
            "compile",
        ]

    completed = subprocess.run(
        command,
        cwd=repository,
        env=_subprocess_environment(),
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert generated_bytecode(repository) == ()


def test_unreadable_selected_source_is_reported(checks, tmp_path):
    if os.geteuid() == 0:
        pytest.skip(
            "root can read a mode-000 source; this check needs an unprivileged user"
        )
    prepare_roots(tmp_path)
    source = write_source(tmp_path, "docker/private.py", "value = 1\n")
    source.chmod(0o000)

    errors = checks.compile_sources(tmp_path)

    assert len(errors) == 1
    assert errors[0].startswith("docker/private.py:")

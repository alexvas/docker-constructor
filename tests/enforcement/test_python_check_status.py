"""Check-runner status and non-mutation contracts (P2 task 2.4).

Deliberate lint, format and syntax defects must produce the underlying
nonzero check status, never a masked success.  Check modes are read-only: they
must not rewrite sources or leave caches or bytecode behind.
"""

from __future__ import annotations

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


@pytest.fixture(scope="module")
def checks():
    return load_check_module()


def test_lint_failure_returns_the_ruff_status_without_mutation(checks, tmp_path):
    prepare_roots(tmp_path)
    write_source(tmp_path, "docker/broken.py", "value = 1\nprint(missing_name)\n")
    before = source_digests(tmp_path)

    assert checks.main(["lint", "--root", str(tmp_path)]) == 1

    assert source_digests(tmp_path) == before
    assert not (tmp_path / ".ruff_cache").exists()


def test_lint_success_returns_zero(checks, tmp_path):
    prepare_roots(tmp_path)
    write_source(tmp_path, "docker/clean.py", 'print("clean")\n')

    assert checks.main(["lint", "--root", str(tmp_path)]) == 0


def test_format_failure_returns_the_ruff_status_without_mutation(checks, tmp_path):
    prepare_roots(tmp_path)
    write_source(
        tmp_path, "docker/unformatted.py", "def value( item ):\n  return  item\n"
    )
    before = source_digests(tmp_path)

    assert checks.main(["format", "--root", str(tmp_path)]) == 1

    assert source_digests(tmp_path) == before
    assert not (tmp_path / ".ruff_cache").exists()


def test_format_success_returns_zero(checks, tmp_path):
    prepare_roots(tmp_path)
    write_source(tmp_path, "docker/formatted.py", "def value(item):\n    return item\n")

    assert checks.main(["format", "--root", str(tmp_path)]) == 0


def test_syntax_failure_returns_nonzero_without_mutation(checks, tmp_path):
    prepare_roots(tmp_path)
    write_source(tmp_path, "docker/invalid.py", "def broken(:\n")
    before = source_digests(tmp_path)

    assert checks.main(["compile", "--root", str(tmp_path)]) == 1

    assert source_digests(tmp_path) == before
    assert generated_bytecode(tmp_path) == ()


def test_compile_success_returns_zero(checks, tmp_path):
    prepare_roots(tmp_path)
    write_source(tmp_path, "docker/valid.py", "value = 1\n")

    assert checks.main(["compile", "--root", str(tmp_path)]) == 0


def test_missing_ruff_is_reported_as_unavailable(checks, monkeypatch, tmp_path):
    prepare_roots(tmp_path)
    write_source(tmp_path, "docker/clean.py", "value = 1\n")

    def unavailable() -> str:
        raise checks.RuffUnavailable("injected missing Ruff")

    monkeypatch.setattr(checks, "find_ruff", unavailable)

    assert checks.main(["lint", "--root", str(tmp_path)]) == 127


def test_entry_point_preserves_the_underlying_failure_status(tmp_path):
    if not VENV_PYTHON.is_file():
        pytest.fail(
            "the project virtual environment is required to run the check entry "
            "points; see docs/testing.md"
        )
    prepare_roots(tmp_path)
    write_source(tmp_path, "docker/invalid.py", "def broken(:\n")
    entry_point = REPO_ROOT / "scripts/check-python-compile"

    completed = subprocess.run(
        [str(entry_point), "--root", str(tmp_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1, completed.stdout + completed.stderr

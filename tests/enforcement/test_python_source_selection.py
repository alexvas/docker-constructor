"""Shared Python source selection contracts (P2 task 2.1).

The selector is the single inventory used by lint, format and compilation
checks.  These fixtures pin its classification rules: nested ``.py``/``.pyi``
sources, shebang-identified extensionless Python, explicit extensionless
entries without a shebang, shell exclusion, vendor/generated/cache directory
exclusion, symlink policy, unreadable candidates and filenames containing
spaces.
"""

from __future__ import annotations

import os

import pytest

from tests.enforcement.python_check_fixtures import (
    load_check_module,
    prepare_roots,
    write_source,
)


@pytest.fixture(scope="module")
def checks():
    return load_check_module()


def selected(checks, root, **kwargs) -> tuple[str, ...]:
    return checks.select_sources(root, **kwargs).selected


def exclusions(checks, root, **kwargs) -> dict[str, str]:
    selection = checks.select_sources(root, **kwargs)
    return {entry.path: entry.reason for entry in selection.exclusions}


def test_nested_python_and_stub_sources_are_selected(checks, tmp_path):
    prepare_roots(tmp_path)
    write_source(tmp_path, "docker/package/module.py", "value = 1\n")
    write_source(tmp_path, "docker/package/module.pyi", "value: int\n")
    write_source(tmp_path, "scripts/tool.py", "value = 2\n")
    write_source(tmp_path, "tests/test_module.py", "value = 3\n")

    assert selected(checks, tmp_path) == (
        "docker/package/module.py",
        "docker/package/module.pyi",
        "scripts/tool.py",
        "tests/test_module.py",
    )


def test_extensionless_python_shebang_is_selected(checks, tmp_path):
    prepare_roots(tmp_path)
    write_source(
        tmp_path,
        "scripts/python-tool",
        "#!/usr/bin/env python3\nvalue = 1\n",
        mode=0o755,
    )

    assert selected(checks, tmp_path) == ("scripts/python-tool",)


@pytest.mark.parametrize(
    "first_line",
    [
        "#!/usr/bin/python",
        "#!/usr/bin/python3",
        "#!/usr/bin/python3.14",
        "#!/opt/venv/bin/python3.14 -u",
        "#!/usr/bin/env python",
        "#!/usr/bin/env python3",
        "#!/usr/bin/env python3.14",
        "#!/usr/bin/env python3.14 -u",
    ],
)
def test_recognized_python_shebangs_are_selected(checks, tmp_path, first_line):
    prepare_roots(tmp_path)
    write_source(
        tmp_path,
        "scripts/python-tool",
        first_line + "\nvalue = 1\n",
        mode=0o755,
    )

    assert selected(checks, tmp_path) == ("scripts/python-tool",)


@pytest.mark.parametrize(
    "first_line",
    [
        "#!/bin/notpython",
        "#!/usr/bin/python2",
        "#!/usr/bin/env notpython",
        "#!/usr/bin/env python3x",
        "#!/usr/bin/env -S python3",
        "#!/usr/bin/env",
        "#!",
    ],
)
def test_unrecognized_shebang_is_excluded_with_a_recorded_reason(
    checks, tmp_path, first_line
):
    prepare_roots(tmp_path)
    write_source(
        tmp_path,
        "scripts/not-python",
        first_line + "\nexit 0\n",
        mode=0o755,
    )

    assert selected(checks, tmp_path) == ()
    recorded = exclusions(checks, tmp_path)
    assert recorded["scripts/not-python"].strip()


def test_non_python_extensions_are_excluded_with_a_recorded_reason(checks, tmp_path):
    prepare_roots(tmp_path)
    write_source(tmp_path, "docker/package/data.json", "{}\n")
    write_source(tmp_path, "tests/fixture.txt", "text\n")
    write_source(tmp_path, "scripts/helper.py", "value = 1\n")
    write_source(tmp_path, "docker/package/module.py", "value = 2\n")

    assert selected(checks, tmp_path) == (
        "docker/package/module.py",
        "scripts/helper.py",
    )
    recorded = exclusions(checks, tmp_path)
    assert recorded["docker/package/data.json"].strip()
    assert recorded["tests/fixture.txt"].strip()


def test_shell_scripts_are_excluded_with_a_recorded_reason(checks, tmp_path):
    prepare_roots(tmp_path)
    write_source(tmp_path, "scripts/shell-tool", "#!/bin/sh\nexit 0\n", mode=0o755)
    write_source(
        tmp_path, "scripts/python-tool", "#!/usr/bin/env python3\n", mode=0o755
    )

    assert selected(checks, tmp_path) == ("scripts/python-tool",)
    recorded = exclusions(checks, tmp_path)
    assert recorded["scripts/shell-tool"].strip()


def test_explicit_extensionless_entry_is_selected_without_a_shebang(checks, tmp_path):
    prepare_roots(tmp_path)
    write_source(tmp_path, "scripts/plain-tool", "print('value')\n", mode=0o755)

    with pytest.raises(checks.SourceSelectionError):
        checks.select_sources(tmp_path)

    explicit = {"scripts/plain-tool": "extensionless Python without a shebang"}
    assert selected(checks, tmp_path, explicit=explicit) == ("scripts/plain-tool",)


def test_directory_symlink_is_not_followed(checks, tmp_path):
    prepare_roots(tmp_path)
    write_source(tmp_path, "vendor/library.py", "value = 1\n")
    os.symlink(tmp_path / "vendor", tmp_path / "docker" / "vendor")

    assert selected(checks, tmp_path) == ()
    recorded = exclusions(checks, tmp_path)
    assert recorded["docker/vendor"].strip()


def test_symlinked_python_source_is_an_error(checks, tmp_path):
    prepare_roots(tmp_path)
    write_source(tmp_path, "docker/real.py", "value = 1\n")
    os.symlink(tmp_path / "docker" / "real.py", tmp_path / "docker" / "alias.py")

    with pytest.raises(checks.SourceSelectionError, match="docker/alias.py"):
        checks.select_sources(tmp_path)


def test_unreadable_extensionless_candidate_is_an_error(checks, tmp_path):
    if os.geteuid() == 0:
        pytest.skip(
            "root can read a mode-000 candidate; this check needs an unprivileged user"
        )
    prepare_roots(tmp_path)
    candidate = write_source(
        tmp_path,
        "scripts/python-tool",
        "#!/usr/bin/env python3\nvalue = 1\n",
        mode=0o755,
    )
    candidate.chmod(0o000)

    with pytest.raises(checks.SourceSelectionError, match="scripts/python-tool"):
        checks.select_sources(tmp_path)


def test_filenames_with_spaces_are_selected(checks, tmp_path):
    prepare_roots(tmp_path)
    write_source(tmp_path, "docker/package/with space.py", "value = 1\n")
    write_source(
        tmp_path,
        "scripts/extensionless tool",
        "#!/usr/bin/env python3\nvalue = 2\n",
        mode=0o755,
    )

    assert selected(checks, tmp_path) == (
        "docker/package/with space.py",
        "scripts/extensionless tool",
    )


def test_cache_directories_are_excluded_with_a_recorded_reason(checks, tmp_path):
    prepare_roots(tmp_path)
    write_source(tmp_path, "docker/__pycache__/cached.py", "value = 1\n")
    write_source(tmp_path, "docker/module.py", "value = 2\n")

    assert selected(checks, tmp_path) == ("docker/module.py",)
    recorded = exclusions(checks, tmp_path)
    assert recorded["docker/__pycache__"].strip()


def test_vendor_and_generated_directories_are_excluded_with_recorded_reasons(
    checks, tmp_path
):
    prepare_roots(tmp_path)
    excluded_directories = tuple(
        f"{root}/{name}"
        for root in ("docker", "tests", "scripts")
        for name in ("vendor", "generated")
    )
    excluded_files: list[str] = []
    for directory in excluded_directories:
        for relative in (
            f"{directory}/module.py",
            f"{directory}/nested/deeper/module.pyi",
        ):
            write_source(tmp_path, relative, "value = 1\n")
            excluded_files.append(relative)
    write_source(tmp_path, "docker/module.py", "value = 2\n")
    write_source(tmp_path, "tests/test_module.py", "value = 3\n")
    write_source(tmp_path, "scripts/tool.py", "value = 4\n")

    selection = checks.select_sources(tmp_path)
    recorded = {entry.path: entry.reason for entry in selection.exclusions}

    assert selection.selected == (
        "docker/module.py",
        "scripts/tool.py",
        "tests/test_module.py",
    )
    for relative in excluded_files:
        assert relative not in selection.selected
    for directory in excluded_directories:
        assert recorded[directory].strip()


def test_selection_is_sorted_unique_and_repeatable(checks, tmp_path):
    prepare_roots(tmp_path)
    for name in ("b.py", "a.py", "c.pyi"):
        write_source(tmp_path, f"docker/{name}", "value = 1\n")

    first = selected(checks, tmp_path)
    second = selected(checks, tmp_path)

    assert first == second == tuple(sorted(first))
    assert len(first) == len(set(first))


def test_missing_source_root_is_an_error(checks, tmp_path):
    (tmp_path / "docker").mkdir()
    write_source(tmp_path, "docker/module.py", "value = 1\n")

    with pytest.raises(checks.SourceSelectionError, match="tests"):
        checks.select_sources(tmp_path)

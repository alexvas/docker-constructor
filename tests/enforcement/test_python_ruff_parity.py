"""Ruff inventory-parity and policy contracts (P2 tasks 2.3 and 2.6).

Ruff must never silently drop an inventoried file: a configuration exclusion
that hides a selected module, or default recursion that misses an
extensionless Python script, has to be visible to the check runner.  Ruff may
still discover Python files beneath the vendor, generated and cache
directories that the shared inventory deliberately excludes; parity must
ignore those out-of-scope discoveries without weakening the selected-file
check.  Mode-specific coverage must also prove that Ruff really processed
every selected path, including extensionless scripts and files hidden by
``force-exclude`` or formatter-specific exclusions.  The same module pins the
agreed Ruff version, target and rule selection so a drifting policy is caught
by the suite.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from tests.enforcement.python_check_fixtures import (
    REPO_ROOT,
    load_check_module,
    prepare_roots,
    source_digests,
    write_source,
)

SELECTED_RULES = ("E4", "E7", "E9", "F", "I", "B", "SIM115", "RUF100")
SELECTED_PREFIXES = ("E4", "E7", "E9", "F", "I", "B")


@pytest.fixture(scope="module")
def checks():
    return load_check_module()


def enabled_rule_codes(settings: str) -> set[str]:
    start = settings.index("linter.rules.enabled = [")
    end = settings.index("]", start)
    return set(re.findall(r"\(([A-Z]+[0-9]+)\)", settings[start:end]))


def test_real_repository_inventory_matches_ruff_discovery(checks):
    selection = checks.select_sources(REPO_ROOT)
    visible = checks.ruff_visible_files(REPO_ROOT)

    assert checks.inventory_parity_errors(selection.selected, visible) == ()


def test_selected_file_omitted_by_configuration_exclusion_is_rejected(checks, tmp_path):
    prepare_roots(tmp_path)
    write_source(
        tmp_path,
        "pyproject.toml",
        '[tool.ruff]\nexclude = ["docker/omitted.py"]\n',
    )
    write_source(tmp_path, "docker/covered.py", "value = 1\n")
    write_source(tmp_path, "docker/omitted.py", "value = 2\n")

    selection = checks.select_sources(tmp_path)
    assert "docker/omitted.py" in selection.selected

    visible = checks.ruff_visible_files(tmp_path)
    assert "docker/omitted.py" not in visible

    errors = checks.inventory_parity_errors(selection.selected, visible)
    assert any("docker/omitted.py" in error for error in errors)
    assert checks.main(["lint", "--root", str(tmp_path)]) == 1


def test_extensionless_source_is_omitted_by_recursion_but_checked(
    checks, tmp_path, capsys
):
    prepare_roots(tmp_path)
    write_source(
        tmp_path,
        "scripts/python-tool",
        "#!/usr/bin/env python3\nprint(missing_name)\n",
        mode=0o755,
    )

    selection = checks.select_sources(tmp_path)
    assert "scripts/python-tool" in selection.selected

    visible = checks.ruff_visible_files(tmp_path)
    assert "scripts/python-tool" not in visible
    assert checks.inventory_parity_errors(selection.selected, visible) == ()

    assert checks.main(["lint", "--root", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert "scripts/python-tool" in captured.out + captured.err


def test_force_exclude_cannot_hide_extensionless_script_from_lint(
    checks, tmp_path, capsys
):
    prepare_roots(tmp_path)
    write_source(
        tmp_path,
        "pyproject.toml",
        '[tool.ruff]\nforce-exclude = true\nexclude = ["scripts/python-tool"]\n',
    )
    write_source(
        tmp_path,
        "scripts/python-tool",
        "#!/usr/bin/env python3\nprint(missing_forced_name)\n",
        mode=0o755,
    )
    before = source_digests(tmp_path)

    selection = checks.select_sources(tmp_path)
    assert "scripts/python-tool" in selection.selected
    visible = checks.ruff_visible_files(tmp_path)
    assert "scripts/python-tool" not in visible
    assert checks.inventory_parity_errors(selection.selected, visible) == ()

    assert checks.main(["lint", "--root", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert "scripts/python-tool" in captured.out + captured.err
    assert "omitted by Ruff lint coverage" in captured.err
    assert source_digests(tmp_path) == before


def test_format_exclude_cannot_hide_selected_module_from_format_check(
    checks, tmp_path, capsys
):
    prepare_roots(tmp_path)
    write_source(
        tmp_path,
        "pyproject.toml",
        "[tool.ruff]\n"
        "force-exclude = true\n"
        "\n"
        "[tool.ruff.format]\n"
        'exclude = ["docker/unformatted.py"]\n',
    )
    write_source(tmp_path, "docker/covered.py", "value = 1\n")
    write_source(tmp_path, "docker/unformatted.py", "x=1\n")
    before = source_digests(tmp_path)

    selection = checks.select_sources(tmp_path)
    assert "docker/unformatted.py" in selection.selected
    visible = checks.ruff_visible_files(tmp_path)
    assert "docker/unformatted.py" in visible
    assert checks.inventory_parity_errors(selection.selected, visible) == ()

    assert checks.main(["format", "--root", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert "docker/unformatted.py" in captured.out + captured.err
    assert "omitted by Ruff format coverage" in captured.err
    assert source_digests(tmp_path) == before


def test_excluded_vendor_and_generated_files_are_ignored_by_ruff_parity(
    checks, tmp_path, capsys
):
    prepare_roots(tmp_path)
    write_source(tmp_path, "docker/covered.py", "value = 1\n")
    write_source(
        tmp_path,
        "docker/vendor/library.py",
        "print(missing_vendor_name)\n",
    )
    write_source(
        tmp_path,
        "docker/generated/nested/output.py",
        "print(missing_generated_name)\n",
    )
    write_source(
        tmp_path,
        "tests/vendor/test_fixture.py",
        "print(missing_test_vendor_name)\n",
    )
    write_source(
        tmp_path,
        "scripts/generated/tool.py",
        "print(missing_script_generated_name)\n",
    )

    selection = checks.select_sources(tmp_path)
    assert selection.selected == ("docker/covered.py",)

    visible = checks.ruff_visible_files(tmp_path)
    assert "docker/vendor/library.py" in visible
    assert "docker/generated/nested/output.py" in visible
    assert "tests/vendor/test_fixture.py" in visible
    assert "scripts/generated/tool.py" in visible

    assert checks.inventory_parity_errors(selection.selected, visible) == ()
    assert checks.main(["lint", "--root", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert "missing_vendor_name" not in captured.out + captured.err
    assert "missing_generated_name" not in captured.out + captured.err


def test_declared_ruff_policy_matches_the_agreement():
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["dependency-groups"]["dev"] == ["ruff==0.17.1"]
    ruff = project["tool"]["ruff"]
    assert ruff["required-version"] == "==0.17.1"
    assert ruff["target-version"] == "py314"
    assert ruff["lint"]["select"] == list(SELECTED_RULES)
    for forbidden in ("preview", "unsafe-fixes", "all"):
        assert forbidden not in ruff
        assert forbidden not in ruff["lint"]
    for forbidden in ("extend-select", "ignore"):
        assert forbidden not in ruff["lint"]


def test_effective_ruff_settings_match_the_declared_policy(checks):
    ruff = checks.find_ruff()
    result = checks.run_captured([ruff, "check", "--show-settings"], root=REPO_ROOT)

    assert result.returncode == 0, result.stderr
    settings = result.stdout.decode("utf-8")
    assert "linter.unresolved_target_version = 3.14" in settings
    assert "linter.preview = disabled" in settings
    assert "formatter.preview = disabled" in settings

    codes = enabled_rule_codes(settings)
    assert codes
    for code in codes:
        assert code in SELECTED_RULES or code.startswith(SELECTED_PREFIXES), code
    assert "SIM115" in codes
    assert "RUF100" in codes


def test_repository_pyproject_is_a_declared_configuration(checks):
    ruff = checks.find_ruff()
    result = checks.run_captured([ruff, "check", "--show-settings"], root=REPO_ROOT)

    assert result.returncode == 0
    settings = result.stdout.decode("utf-8")
    assert f'Settings path: "{REPO_ROOT / "pyproject.toml"}"' in settings
    assert Path(REPO_ROOT / "pyproject.toml").is_file()

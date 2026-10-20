"""Permanent local/CI gate contracts; negative mutations stay in fixtures."""

from __future__ import annotations

import importlib.util
import sys

import pytest

from tests.enforcement.green_fixtures import read_bytes, read_text
from tests.enforcement.python_check_fixtures import (
    REPO_ROOT,
    load_check_module,
    prepare_roots,
    source_digests,
    write_source,
)

EXPECTED_COMMANDS = (
    ("scripts/check-python-lint",),
    ("scripts/check-python-format",),
    ("scripts/check-python-compile",),
    ("scripts/check-types",),
    (
        ".venv/bin/python",
        "-m",
        "pytest",
        "tests",
        "-n",
        "8",
        "--dist",
        "loadfile",
        "--tb=short",
        "-q",
    ),
)


def load_gate():
    path = REPO_ROOT / "scripts/green_checks.py"
    assert path.is_file(), "permanent green gate is missing"
    spec = importlib.util.spec_from_file_location("green_checks_contract", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_local_plan_preserves_every_required_command():
    gate = load_gate()
    assert gate.COMMANDS == EXPECTED_COMMANDS


@pytest.mark.parametrize("omitted", range(len(EXPECTED_COMMANDS)))
def test_omitted_existing_or_adoption_command_is_detected(omitted):
    shortened = EXPECTED_COMMANDS[:omitted] + EXPECTED_COMMANDS[omitted + 1 :]
    assert shortened != load_gate().COMMANDS


@pytest.mark.parametrize("failed", range(len(EXPECTED_COMMANDS)))
def test_gate_preserves_failure_and_runs_independent_checks(failed):
    gate = load_gate()
    checks = load_check_module()
    calls = []

    def run(argv, *, root):
        calls.append(tuple(argv))
        return checks.CommandResult(
            7 if tuple(argv) == EXPECTED_COMMANDS[failed] else 0,
            b"",
            b"",
            True,
        )

    assert gate.main(run=run) == 7
    assert tuple(calls) == EXPECTED_COMMANDS


@pytest.mark.parametrize("workers", ["1", "2", "3", "8", "16", "1024", "0002"])
@pytest.mark.parametrize("status", [0, 7])
def test_worker_counts_preserve_full_scope_and_failure_status(workers, status):
    gate = load_gate()
    checks = load_check_module()
    calls = []

    def run(argv, *, root):
        calls.append(tuple(argv))
        return checks.CommandResult(status, b"", b"", True)

    assert gate.main(run=run, workers=workers) == status
    expected = list(EXPECTED_COMMANDS[-1])
    expected[expected.index("-n") + 1] = str(int(workers))
    assert tuple(calls) == (*EXPECTED_COMMANDS[:-1], tuple(expected))
    assert gate.COMMANDS == EXPECTED_COMMANDS


@pytest.mark.parametrize(
    "workers",
    ["", "0", "-1", "1025", "999999999999999999999", "1.5", "auto", "2 --ignore=tests"],
)
def test_invalid_worker_counts_fail_before_running_checks(workers, capsys):
    def unexpected_run(*args, **kwargs):
        pytest.fail("invalid worker setting must not execute checks")

    assert load_gate().main(run=unexpected_run, workers=workers) == 2
    assert "must be an integer from 1 to 1024" in capsys.readouterr().err


def test_gate_does_not_swallow_interruption():
    gate = load_gate()
    interruption = KeyboardInterrupt("injected")

    def interrupt(*args, **kwargs):
        raise interruption

    with pytest.raises(KeyboardInterrupt) as caught:
        gate.main(run=interrupt)
    assert caught.value is interruption


def test_entry_point_rejects_narrowing_and_mutation_arguments():
    result = load_check_module().run_captured(
        [str(REPO_ROOT / "scripts/check-green"), "--fix"], root=REPO_ROOT
    )
    assert result.clean
    assert result.returncode == 1
    assert b"does not support narrowing or mutation" in result.stderr


def test_unsettled_cleanup_is_not_green():
    gate = load_gate()
    checks = load_check_module()
    assert (
        gate.main(
            run=lambda *a, **kw: checks.CommandResult(
                0, b"", b"", False, (OSError("close failed"),)
            )
        )
        != 0
    )


@pytest.mark.parametrize(
    ("mode", "source"),
    [
        ("lint", "print(undefined_name)\n"),
        ("format", "value=  [1,2]\n"),
        ("compile", "def broken(:\n"),
    ],
)
def test_isolated_source_failures_are_nonzero_and_readonly(tmp_path, mode, source):
    checks = load_check_module()
    prepare_roots(tmp_path)
    write_source(tmp_path, "docker/broken.py", source)
    before = source_digests(tmp_path)
    assert checks.main([mode, "--root", str(tmp_path)]) == 1
    assert source_digests(tmp_path) == before


def test_isolated_type_failure_is_nonzero(tmp_path):
    checks = load_check_module()
    path = write_source(tmp_path, "broken.py", 'value: int = "not an integer"\n')
    result = checks.run_captured(
        [str(REPO_ROOT / ".venv/bin/python"), "-m", "ty", "check", str(path)],
        root=tmp_path,
    )
    assert result.clean
    assert result.returncode == 1, result.stderr + result.stdout
    assert b"invalid-assignment" in result.stdout + result.stderr


def workflow_errors(text):
    """Fail closed on changes to the approved gate/provision/logging wiring."""
    required = (
        "on:\n  push:\n  pull_request:",
        'python-version: "3.14"',
        "--group dev pytest pytest-xdist ty==0.0.73",
        "getent passwd docker-dev",
        "sudo -n runuser -u nobody -- id",
        "run: scripts/check-types",
        'scripts/check-green >"$log" 2>&1',
        '        env:\n          GREEN_CHECKS_WORKERS: "2"\n        shell: bash',
        "status=$?",
        'cat "$log"',
        'exit "$status"',
        "if: always()",
        "actions/upload-artifact@",
        "retention-days: 7",
        "path: ${{ runner.temp }}/green-checks.log",
    )
    errors = [entry for entry in required if entry not in text]
    expected_capture = '''          log="$RUNNER_TEMP/green-checks.log"
          set +e
          scripts/check-green >"$log" 2>&1
          status=$?
          set -e
          cat "$log"
          exit "$status"'''
    if expected_capture not in text:
        errors.append("capture block must preserve the actual gate status")
    for forbidden in (
        "--fix",
        "ruff format .",
        "continue-on-error",
        "|| true",
        "git commit",
        "git push",
        "paths:",
        "paths-ignore:",
        "branches:",
        "branches-ignore:",
    ):
        if forbidden in text:
            errors.append(forbidden)
    return errors


def test_validation_workflow_retains_full_nonmutating_gate():
    text = read_text(REPO_ROOT / ".github/workflows/validation.yml")
    assert workflow_errors(text) == []
    assert load_gate().COMMANDS == EXPECTED_COMMANDS


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("  push:", "  workflow_dispatch:"),
        ("  pull_request:", ""),
        ('GREEN_CHECKS_WORKERS: "2"', 'GREEN_CHECKS_WORKERS: "8"'),
        ("scripts/check-green", "scripts/check-python-lint docker"),
        ("run: scripts/check-types", "run: ty check docker"),
        ('exit "$status"', "exit 0"),
        ("          status=$?", "          exit 0\n          status=$?"),
        ('cat "$log"', "true"),
        ("if: always()", "if: success()"),
        ("retention-days: 7", "retention-days: 90"),
    ],
)
def test_workflow_mutations_are_rejected(old, new):
    text = read_text(REPO_ROOT / ".github/workflows/validation.yml")
    assert old in text
    assert workflow_errors(text.replace(old, new))


@pytest.mark.parametrize(
    "mutation", ["--fix", "continue-on-error: true", "|| true", "git commit", "paths:"]
)
def test_mutating_or_narrowing_workflow_is_rejected(mutation):
    text = read_text(REPO_ROOT / ".github/workflows/validation.yml")
    assert workflow_errors(text + "\n" + mutation)


@pytest.mark.parametrize("status", [0, 7])
def test_log_capture_keeps_complete_output_and_original_status(tmp_path, status):
    text = read_text(REPO_ROOT / ".github/workflows/validation.yml")
    start = text.index('          log="$RUNNER_TEMP/green-checks.log"')
    end = text.index('          exit "$status"', start) + len(
        '          exit "$status"'
    )
    block = "\n".join(line[10:] for line in text[start:end].splitlines())
    block = block.replace(
        "scripts/check-green",
        f"bash -c 'echo diagnostic-stdout; echo diagnostic-stderr >&2; exit {status}'",
    )
    result = load_check_module().run_captured(
        [
            "bash",
            "-c",
            'set -euo pipefail\nRUNNER_TEMP="$1"\n' + block,
            "fixture",
            str(tmp_path),
        ],
        root=tmp_path,
    )
    assert result.clean
    assert result.returncode == status
    expected = b"diagnostic-stdout\ndiagnostic-stderr\n"
    assert result.stdout == expected
    assert read_bytes(tmp_path / "green-checks.log") == expected

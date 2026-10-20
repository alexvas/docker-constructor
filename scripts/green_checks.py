"""Run the complete permanent green gate without narrowing source/test scope.

All child ownership and output forwarding delegate to python_checks.run_streaming
and its existing OwnedProcess path. This module acquires no descriptors or pipes.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

from python_checks import run_streaming  # noqa: E402 -- CLI support path is set first.

ROOT = Path(__file__).resolve().parents[1]
COMMANDS = (
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


def main(*, run=run_streaming, workers="8") -> int:
    """Attempt every independent check; preserve scope and first failing status."""
    try:
        worker_count = int(workers)
    except ValueError:
        worker_count = 0
    if not 1 <= worker_count <= 1024:
        print("GREEN_CHECKS_WORKERS must be an integer from 1 to 1024", file=sys.stderr)
        return 2
    pytest_command = list(COMMANDS[-1])
    pytest_command[pytest_command.index("-n") + 1] = str(worker_count)
    commands = (*COMMANDS[:-1], tuple(pytest_command))
    status = 0
    for argv in commands:
        print("\n$ " + " ".join(argv), flush=True)
        result = run(argv, root=ROOT)
        code = result.returncode
        if not result.clean or code is None:
            code = code or 1
            for failure in result.failures:
                print(f"check lifecycle failure: {failure}", file=sys.stderr)
        if code and not status:
            status = code if code > 0 else 128 - code
    return status


if __name__ == "__main__":
    if len(sys.argv) != 1:
        sys.exit("check-green does not support narrowing or mutation arguments")
    sys.exit(main(workers=os.environ.get("GREEN_CHECKS_WORKERS", "8")))

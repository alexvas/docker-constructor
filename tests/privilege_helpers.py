"""Narrowly scoped sudo delegation for credential-sensitive tests.

The Python test process always runs as the original invoking user. Only the
specific privileged operations below cross that boundary, and each delegates
exactly one operation to ``sudo``. Host user accounts are never created,
deleted, or modified.

``PRIVILEGED_HELPERS=1`` (set by ``scripts/validate-phase4``) selects
"privileged helper mode", in which an unavailable or failing privilege
boundary raises instead of skipping so the validator reports incomplete
validation. Without it, unavailable privilege degrades to an ordinary
``SkipTest`` so a local developer run stays green.
"""

from __future__ import annotations

import os
import pwd
import subprocess
import unittest
from typing import NoReturn

PRIVILEGED_HELPERS = os.environ.get("PRIVILEGED_HELPERS") == "1"


def _require(reason: str) -> NoReturn:
    if PRIVILEGED_HELPERS:
        raise AssertionError(f"incomplete validation: {reason}")
    raise unittest.SkipTest(reason)


def _sudo_run(*argv: str, purpose: str) -> subprocess.CompletedProcess:
    """Run one narrowly scoped sudo command, never prompting for a password."""
    try:
        return subprocess.run(
            ["sudo", "-n", "--", *argv],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        _require(f"sudo {purpose} unavailable: {exc}")


def _sudo(*argv: str, purpose: str) -> None:
    completed = _sudo_run(*argv, purpose=purpose)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        _require(f"sudo {purpose} failed ({completed.returncode}): {detail}")


def docker_dev_ids() -> tuple[int, int]:
    """Return the (uid, gid) of the existing docker-dev account."""
    try:
        entry = pwd.getpwnam("docker-dev")
    except KeyError:
        _require("an existing 'docker-dev' account is required")
    return entry.pw_uid, entry.pw_gid


def sudo_chown(path: str | os.PathLike[str], uid: int, gid: int) -> None:
    """Transfer ownership of one pathname to (uid, gid) via sudo chown."""
    _sudo("chown", f"{uid}:{gid}", "--", os.fspath(path), purpose="ownership transfer")


def sudo_chown_tree(path: str | os.PathLike[str], uid: int, gid: int) -> None:
    """Recursively transfer a tree to (uid, gid) via sudo chown -R."""
    _sudo(
        "chown",
        "-R",
        f"{uid}:{gid}",
        "--",
        os.fspath(path),
        purpose="ownership transfer",
    )


def sudo_maintain_tree(path: str | os.PathLike[str], uid: int, gid: int) -> None:
    """Recursive project-scoped chown/chmod maintenance delegated to sudo.

    Transfers the tree to (uid, gid) and grants the group read/write plus
    execute-where-already-executable bits (``g+rwX``), preserving existing
    execute bits and changing only group permissions — the reviewed
    host-operator maintenance applied between the two builds of the
    ownership-maintenance regression.
    """
    root = os.fspath(path)
    sudo_chown_tree(root, uid, gid)
    _sudo("chmod", "-R", "g+rwX", "--", root, purpose="ownership maintenance")


def differing_uid_read_status(
    path: str | os.PathLike[str], *, user: str = "nobody"
) -> int:
    """Return the exit status of ``test -r`` executed as *user* via sudo.

    ``0`` means the differing UID can read the path; nonzero means it cannot.
    """
    completed = _sudo_run(
        "runuser",
        "-u",
        user,
        "--",
        "test",
        "-r",
        os.fspath(path),
        purpose=f"runuser -u {user}",
    )
    if completed.returncode == 0:
        return 0
    if (completed.stderr or completed.stdout).strip():
        _require(
            f"sudo runuser -u {user} failed ({completed.returncode}): "
            f"{(completed.stderr or completed.stdout).strip()}"
        )
    # ``test -r`` returns nonzero with no output when the differing UID
    # cannot read the path.
    return completed.returncode

"""Focused regressions for P5 behavior-preservation claims."""

from __future__ import annotations

import ast
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "collect-docker-cancellation-evidence.py"
)


def test_cancellation_race_callback_binds_barrier_per_iteration() -> None:
    """The race callback closes over the barrier created for its own iteration."""
    tree = ast.parse(_SCRIPT.read_text(encoding="utf-8"))

    probe = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "probe"
    )
    action = next(
        node
        for node in ast.walk(probe)
        if isinstance(node, ast.FunctionDef) and node.name == "action"
    )

    assert [argument.arg for argument in action.args.args] == [
        "command",
        "target",
        "barrier",
    ]
    assert len(action.args.defaults) == 1
    default = action.args.defaults[0]
    assert isinstance(default, ast.Name)
    assert default.id == "barrier"

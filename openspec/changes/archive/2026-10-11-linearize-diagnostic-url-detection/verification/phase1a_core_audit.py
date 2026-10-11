"""One-off P1A core audit for ``linearize-diagnostic-url-detection``.

This script is **not** part of the automated test suite or CI.  It records the
deterministic, timing-free evidence for task 1A.9 (INTROSPECT) of the
standalone Aho-Corasick core:

* construction allocation: node count bounded by ``1 + sum(len(pattern))`` and
  caller input copied rather than aliased;
* actual compiled-state immutability: transition maps are read-only and
  compiled attributes cannot be reassigned or deleted;
* output-link handling: one own-output slot and one output link per node, so
  the complete inherited output list is never copied onto every node;
* payload-free cursors: the live state holds only integers and the shared
  automaton, never source/replay/history;
* cumulative work: counters survive :meth:`reset` and cannot be hidden by
  replacing a cursor;
* failure-path and output-work bounds on adversarial suffix-heavy input; and
* no production regex/library fallback: the module imports only an explicit
  standard-library allowlist.

Run from the repository root::

    .venv/bin/python \
      openspec/changes/linearize-diagnostic-url-detection/verification/phase1a_core_audit.py

It prints each finding and ``P1A CORE AUDIT PASS`` on success, exiting 0.

Note on proving immutability: copying the caller's pattern list and observing
that a search does not change the automaton are *isolation* properties.  They
are necessary but not sufficient: they cannot show that the compiled trie
rejects mutation.  :func:`audit_compiled_state_immutability` therefore drives
mutation through the published transition maps and compiled attributes and
requires an exception before re-checking shared-cursor matching.
"""
from __future__ import annotations

import ast
import pathlib
import sys
from types import MappingProxyType

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
_MODULE_PATH = _REPO_ROOT / "docker" / "versioning" / "aho_corasick.py"
sys.path.insert(0, str(_REPO_ROOT))

from docker.versioning.aho_corasick import AhoCorasick  # noqa: E402

#: Standard-library modules the core is permitted to import.  Anything else --
#: notably ``re`` or a third-party Aho-Corasick library -- would be a
#: production fallback and is rejected.
_ALLOWED_IMPORTS = frozenset(
    {"__future__", "collections", "dataclasses", "types"}
)


def _check(condition: bool, message: str) -> None:
    status = "ok" if condition else "FAIL"
    print(f"[{status}] {message}")
    if not condition:
        raise SystemExit(1)


def audit_construction_allocation() -> None:
    patterns = ["a" * length for length in range(1, 41)]
    patterns += [f"token{index}" for index in range(20)]
    matcher = AhoCorasick(patterns)
    bound = 1 + sum(len(pattern) for pattern in matcher.patterns)
    _check(
        matcher.node_count <= bound,
        f"node_count {matcher.node_count} <= 1 + sum(len(pattern)) {bound}",
    )
    source = ["ab"]
    aliasing = AhoCorasick(source)
    source.append("cd")
    # Isolation only: this shows the caller cannot reach compiled state, not
    # that compiled state itself rejects mutation (see
    # audit_compiled_state_immutability).
    _check(
        aliasing.patterns == ("ab",),
        "caller pattern input is copied, not aliased (isolation, not immutability)",
    )
    _check(
        len(aliasing._output) == aliasing.node_count
        and len(aliasing._output_link) == aliasing.node_count
        and len(aliasing._fail) == aliasing.node_count
        and len(aliasing._goto) == aliasing.node_count,
        "registration storage is O(nodes): one output/fail/link slot per node",
    )


def audit_output_links() -> None:
    # Suffix-heavy patterns: every node could inherit many outputs.  The core
    # stores one own output and one output link per node regardless.
    patterns = ["a" * length for length in range(1, 51)]
    matcher = AhoCorasick(patterns)
    own_outputs = sum(1 for value in matcher._output if value is not None)
    _check(
        own_outputs == len(patterns),
        f"exactly {len(patterns)} own-output nodes, got {own_outputs}",
    )
    _check(
        not any(isinstance(value, (list, tuple, set)) for value in matcher._output),
        "no node copies a transitive output list",
    )
    cursor = matcher.cursor()
    matches = cursor.feed("a" * 50)
    expected = sum(50 - length + 1 for length in range(1, 51))
    _check(
        len(matches) == expected,
        f"suffix-heavy matching emits {expected} matches, got {len(matches)}",
    )
    _check(cursor.counters.outputs == expected, "outputs counter equals matches")


def audit_payload_free_cursor() -> None:
    cursor = AhoCorasick(["TOKEN"]).cursor()
    cursor.feed("x" * 1000 + "TOKEN" + "y" * 1000)
    slots = type(cursor).__slots__
    payload_slots = [
        slot for slot in slots if slot != "_automaton" and isinstance(getattr(cursor, slot), str)
    ]
    _check(not payload_slots, f"cursor retains no string payload (slots={slots})")
    _check(
        hasattr(cursor, "state") and isinstance(cursor.state, int),
        "live state is a single integer node index",
    )


def audit_cumulative_work() -> None:
    cursor = AhoCorasick(["ab"]).cursor()
    for _ in range(5):
        cursor.feed("ab")
        cursor.reset()
    _check(
        cursor.counters.transitions == 10,
        f"lifetime transitions survive resets: {cursor.counters.transitions}",
    )
    _check(
        cursor.counters.outputs == 5,
        f"lifetime outputs survive resets: {cursor.counters.outputs}",
    )
    fresh = AhoCorasick(["ab"]).cursor()
    fresh.feed("ab")
    _check(
        fresh.counters.transitions < cursor.counters.transitions,
        "replacing a cursor cannot hide its cumulative work",
    )


def audit_compiled_state_immutability() -> None:
    # Isolation (copied input, search that does not write) is insufficient;
    # drive mutation through the published objects and require an exception.
    matcher = AhoCorasick(["ab", "bc"])
    _check(
        all(isinstance(node, MappingProxyType) for node in matcher._goto),
        "every compiled transition map is a read-only mappingproxy",
    )
    transition_rejections = 0
    try:
        matcher._goto[0]["z"] = 99
    except TypeError:
        transition_rejections += 1
    try:
        del matcher._goto[0]["a"]
    except TypeError:
        transition_rejections += 1
    try:
        matcher._goto[0] = {}
    except TypeError:
        transition_rejections += 1
    _check(
        transition_rejections == 3,
        "adding, deleting and replacing transitions each raise TypeError",
    )
    attribute_rejections = 0
    for name in ("_patterns", "_goto", "_fail", "_output", "_output_link"):
        try:
            setattr(matcher, name, object())
        except AttributeError:
            attribute_rejections += 1
        try:
            delattr(matcher, name)
        except AttributeError:
            attribute_rejections += 1
    _check(
        attribute_rejections == 10,
        "replacing or deleting any compiled attribute raises AttributeError",
    )
    _check(
        not hasattr(matcher, "__dict__"),
        "compiled automaton has no instance dict for attribute injection",
    )
    cursor = matcher.cursor()
    observed = [
        (match.pattern_index, match.start, match.end)
        for match in cursor.feed("abcb")
    ]
    _check(
        observed == [(0, 0, 2), (1, 1, 3)],
        "matching is unchanged after every rejected mutation attempt",
    )


def audit_failure_and_output_bounds() -> None:
    patterns = ["aab", "ab", "b", "baa"]
    cursor = AhoCorasick(patterns).cursor()
    matches = cursor.feed("a" * 2000 + "b" * 2000)
    counters = cursor.counters
    _check(counters.transitions == 4000, "one transition per input symbol")
    _check(
        counters.failures <= counters.transitions,
        f"failures {counters.failures} amortized within transitions {counters.transitions}",
    )
    _check(counters.outputs == len(matches), "outputs counter equals emitted matches")


def audit_no_library_fallback() -> None:
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    unexpected = imported - _ALLOWED_IMPORTS
    _check(
        not unexpected,
        f"module imports only stdlib allowlist {sorted(_ALLOWED_IMPORTS)}; "
        f"unexpected={sorted(unexpected)}",
    )
    _check(
        "re" not in imported,
        "no production regular-expression fallback",
    )


def main() -> int:
    audit_construction_allocation()
    audit_compiled_state_immutability()
    audit_output_links()
    audit_payload_free_cursor()
    audit_cumulative_work()
    audit_failure_and_output_bounds()
    audit_no_library_fallback()
    print("P1A CORE AUDIT PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

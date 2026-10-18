"""Validate guide links, including links to named executable tests.

Test links name the qualified test function in their Markdown title.  The
checker resolves that name in the target module's AST instead of binding a
line number, so harmless formatting or added test code cannot stale a guide.
"""
from __future__ import annotations

import ast
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]
GUIDES = {"process-lifecycle.md": 6, "npm-environment-assembler.md": 6,
          "filesystem-lifecycle.md": 1}
LINK = re.compile(r'\[([^\]]+)\]\(([^\s)]+)(?:\s+"([^"]+)")?\)')


def _test_definitions(source: str) -> dict[str, list[int]]:
    definitions: dict[str, list[int]] = {}

    def visit(node, parents=()):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, (*parents, child.name))
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if child.name.startswith("test_"):
                    name = ".".join((*parents, child.name))
                    definitions.setdefault(name, []).append(child.lineno)
                # Nested helper functions are not independent executable tests.
            else:
                visit(child, parents)

    visit(ast.parse(source))
    return definitions


def _resolve_test(definitions: dict[str, list[int]], symbol: str, url: str) -> None:
    lines = definitions.get(symbol)
    assert lines, f"test not found: {url} (expected {symbol})"
    assert len(lines) == 1, f"duplicate test name: {url} (expected {symbol})"


def _check_local_link(guide: Path, url: str, symbol: str | None) -> None:
    parsed = urlsplit(url)
    assert not parsed.scheme and not parsed.netloc, f"not a local link: {url}"
    target = (guide.parent / unquote(parsed.path)).resolve()
    assert target.is_relative_to(ROOT), f"link escapes repository: {url}"
    if target.is_dir():
        assert (target / "README.md").is_file(), f"directory link needs a README: {url}"
        assert not parsed.fragment and symbol is None, f"directory cannot name a test: {url}"
        return
    assert target.is_file(), f"missing link target: {url}"
    if target.is_relative_to(ROOT / "tests"):
        assert target.name.startswith("test_") and target.suffix == ".py", url
        assert not parsed.fragment, f"test link uses a layout-bound line anchor: {url}"
        assert symbol, f"test link needs its qualified name in the Markdown title: {url}"
        definitions = _test_definitions(target.read_text(encoding="utf-8"))
        _resolve_test(definitions, symbol, url)
    else:
        # These guides currently link whole local documents, not heading anchors.
        assert not parsed.fragment, f"unchecked document anchor: {url}"


@pytest.mark.parametrize("name", GUIDES)
def test_lifecycle_guide_links_resolve_to_documents_and_named_tests(name):
    guide = ROOT / "docs" / name
    links = LINK.findall(guide.read_text(encoding="utf-8"))
    assert links, f"no links found in {guide}"
    test_links = 0
    for label, url, symbol in links:
        assert label.strip(), f"empty link label: {url}"
        _check_local_link(guide, url, symbol or None)
        if symbol:
            test_links += 1
    assert test_links == GUIDES[name], f"missing contract scenarios in {guide}"


def test_guide_names_a_test_without_a_line_anchor():
    _check_local_link(
        ROOT / "docs/process-lifecycle.md",
        "../tests/test_process_single_collection.py",
        "test_reject_repeated_and_mixed_collection_without_work",
    )


@pytest.mark.parametrize("url,symbol", [
    ("../tests/test_missing_lifecycle_example.py", "test_missing"),
    ("../../outside-repository.py", "test_missing"),
    ("../tests/test_process_single_collection.py", "test_renamed"),
    ("../tests/test_process_single_collection.py", None),
    ("../tests/test_process_single_collection.py#L63", "test_reject_repeated_and_mixed_collection_without_work"),
    ("../tests/test_process_single_collection.py#L0", "test_reject_repeated_and_mixed_collection_without_work"),
    ("../tests/test_process_single_collection.py#L63", None),
    ("process-lifecycle.md#missing-heading", None),
    ("../tests/enforcement/", "test_missing"),
])
def test_broken_or_misdirected_guide_links_are_rejected(url, symbol):
    with pytest.raises(AssertionError):
        _check_local_link(ROOT / "docs/process-lifecycle.md", url, symbol)


def test_enforcement_directory_link_resolves():
    _check_local_link(ROOT / "docs/filesystem-lifecycle.md", "../tests/enforcement/", None)


def test_test_definition_lookup_handles_class_methods_but_not_nested_helpers():
    source = "class Examples:\n    def test_method(self):\n        def test_helper(): pass\n"
    assert _test_definitions(source) == {"Examples.test_method": [2]}


def test_duplicate_test_names_are_rejected():
    definitions = {"test_same": [2, 3]}
    with pytest.raises(AssertionError, match="duplicate test name"):
        _resolve_test(definitions, "test_same", "test_module.py")

"""Source-snippet mutation helpers for enforcement proofs.

Enforcement suites mutate production source to prove that their audits reject
invalid shapes.  Exact text replacement would bind those proofs to the current
formatter output, so mutation targets are identified structurally instead:
statements by their AST shape, expressions by their AST shape, and keyword
lists by their AST shape.  Whitespace, line wrapping and added or removed
redundant parentheses therefore do not move a mutation target.
"""

from __future__ import annotations

import ast
import textwrap

_Span = tuple[int, int, int, int]


def _is_true(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Constant)
        and node.test.value is True
    )


def _parse_statements(source: str) -> list[ast.stmt] | None:
    text = textwrap.dedent(source)
    try:
        return ast.parse(text).body
    except SyntaxError:
        pass
    # A snippet written at the start of a literal may omit the first line's
    # indentation while later lines keep it.  Restore that indentation before
    # dedenting so the relative structure is parseable.
    lines = text.splitlines()
    rest = [line for line in lines[1:] if line.strip()]
    if lines and lines[0] and not lines[0][:1].isspace() and rest:
        indent = min(len(line) - len(line.lstrip(" ")) for line in rest)
        normalized = textwrap.dedent("\n".join([" " * indent + lines[0], *lines[1:]]))
        try:
            return ast.parse(normalized).body
        except SyntaxError:
            pass
    # A snippet may begin inside an enclosing block and continue with its
    # siblings (for example the last statement of a ``with`` block followed by
    # the next function statement).  Supply the missing block so its relative
    # indentation is parseable, then drop that synthetic header.
    try:
        wrapped = ast.parse("if True:\n" + text)
    except SyntaxError:
        return None
    first = wrapped.body[0]
    if not _is_true(first) or not isinstance(first, ast.If):
        return None
    return [*first.body, *wrapped.body[1:]]


def _parse_expression(source: str) -> ast.expr | None:
    try:
        return ast.parse(textwrap.dedent(source), mode="eval").body
    except SyntaxError:
        return None


def _parse_arguments(source: str) -> list[ast.expr] | None:
    try:
        call = ast.parse(f"_f({textwrap.dedent(source)})", mode="eval").body
    except SyntaxError:
        return None
    if not isinstance(call, ast.Call) or not call.args or call.keywords:
        return None
    return call.args


def _parse_keywords(source: str) -> list[ast.keyword] | None:
    try:
        call = ast.parse(f"_f({textwrap.dedent(source)})", mode="eval").body
    except SyntaxError:
        return None
    if not isinstance(call, ast.Call) or call.args or not call.keywords:
        return None
    return call.keywords


def _offsets(source: str) -> list[int]:
    starts = []
    position = 0
    for line in source.split("\n"):
        starts.append(position)
        position += len(line) + 1
    return starts


def _absolute(source: str, starts: list[int], lineno: int, column: int) -> int:
    line = source.split("\n")[lineno - 1]
    character_column = len(line.encode("utf-8")[:column].decode("utf-8"))
    return starts[lineno - 1] + character_column


def _statement_matches(tree: ast.AST, patterns: list[ast.stmt]) -> list[_Span]:
    wanted = [ast.dump(node, include_attributes=False) for node in patterns]
    statements = sorted(
        (node for node in ast.walk(tree) if isinstance(node, ast.stmt)),
        key=lambda node: (node.lineno, node.col_offset),
    )
    matches: list[_Span] = []
    for index in range(len(statements) - len(wanted) + 1):
        window = statements[index : index + len(wanted)]
        if [ast.dump(node, include_attributes=False) for node in window] != wanted:
            continue
        matches.append(
            (
                window[0].lineno,
                window[0].col_offset,
                window[-1].end_lineno,
                window[-1].end_col_offset,
            )
        )
    return matches


def _expression_matches(tree: ast.AST, pattern: ast.expr) -> list[_Span]:
    wanted = ast.dump(pattern, include_attributes=False)
    return [
        (node.lineno, node.col_offset, node.end_lineno, node.end_col_offset)
        for node in ast.walk(tree)
        if isinstance(node, ast.expr)
        and ast.dump(node, include_attributes=False) == wanted
    ]


def _argument_matches(tree: ast.AST, patterns: list[ast.expr]) -> list[_Span]:
    wanted = [ast.dump(node, include_attributes=False) for node in patterns]
    matches: list[_Span] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        arguments = node.args
        for index in range(len(arguments) - len(wanted) + 1):
            window = arguments[index : index + len(wanted)]
            if [
                ast.dump(argument, include_attributes=False) for argument in window
            ] != wanted:
                continue
            matches.append(
                (
                    window[0].lineno,
                    window[0].col_offset,
                    window[-1].end_lineno,
                    window[-1].end_col_offset,
                )
            )
    return matches


def _keyword_matches(tree: ast.AST, patterns: list[ast.keyword]) -> list[_Span]:
    wanted = [ast.dump(node, include_attributes=False) for node in patterns]
    matches: list[_Span] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        keywords = node.keywords
        for index in range(len(keywords) - len(wanted) + 1):
            window = keywords[index : index + len(wanted)]
            if [
                ast.dump(keyword, include_attributes=False) for keyword in window
            ] != wanted:
                continue
            matches.append(
                (
                    window[0].lineno,
                    window[0].col_offset,
                    window[-1].end_lineno,
                    window[-1].end_col_offset,
                )
            )
    return matches


def _locate(source: str, tree: ast.AST, old: str) -> list[_Span] | None:
    statements = _parse_statements(old)
    if statements is not None:
        matches = _statement_matches(tree, statements)
        if matches:
            return matches
        if len(statements) == 1 and isinstance(statements[0], ast.Expr):
            matches = _expression_matches(tree, statements[0].value)
            if matches:
                return matches
    expression = _parse_expression(old)
    if expression is not None:
        matches = _expression_matches(tree, expression)
        if matches:
            return matches
    arguments = _parse_arguments(old)
    if arguments is not None:
        matches = _argument_matches(tree, arguments)
        if matches:
            return matches
    keywords = _parse_keywords(old)
    if keywords is not None:
        matches = _keyword_matches(tree, keywords)
        if matches:
            return matches
    return None


def _header_pattern(source: str) -> ast.If | ast.While | None:
    text = textwrap.dedent(source).strip()
    if "\n" in text or not text.endswith(":"):
        return None
    try:
        module = ast.parse(text + "\n    pass")
    except SyntaxError:
        return None
    if len(module.body) != 1:
        return None
    node = module.body[0]
    return node if isinstance(node, (ast.If, ast.While)) else None


def _header_matches(tree: ast.AST, pattern: ast.If | ast.While) -> list[_Span]:
    wanted = ast.dump(pattern.test, include_attributes=False)
    matches: list[_Span] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.If, ast.While)) or type(node) is not type(pattern):
            continue
        if ast.dump(node.test, include_attributes=False) != wanted:
            continue
        matches.append(
            (
                node.test.lineno,
                node.test.col_offset,
                node.test.end_lineno,
                node.test.end_col_offset,
            )
        )
    return matches


def replace_snippet(source: str, old: str, new: str, *, unique: bool = True) -> str:
    """Replace one structurally identified *old* snippet with *new*."""
    tree = ast.parse(source)
    matches = _locate(source, tree, old)
    replacement = _replacement(new)
    if matches is None:
        old_header = _header_pattern(old)
        new_header = _header_pattern(new)
        if old_header is not None and new_header is not None:
            matches = _header_matches(tree, old_header)
            replacement = ast.unparse(new_header.test)
        else:
            raise AssertionError(
                "mutation target is not a statement, expression, keyword list "
                f"or conditional header: {old!r}"
            )
    if not matches:
        raise AssertionError(f"mutation target not found: {old!r}")
    if unique and len(matches) != 1:
        raise AssertionError(
            f"expected exactly one mutation target, found {len(matches)}: {old!r}"
        )
    starts = _offsets(source)
    for start_line, start_column, end_line, end_column in sorted(matches, reverse=True):
        start = _absolute(source, starts, start_line, start_column)
        end = _absolute(source, starts, end_line, end_column)
        source = source[:start] + replacement + source[end:]
    return source


def _replacement(new: str) -> str:
    first, separator, rest = new.partition("\n")
    return first.lstrip(" \t") + separator + rest


def insert_before(
    source: str, header: str, statement: str, *, unique: bool = True
) -> str:
    """Insert *statement* immediately before the named conditional header."""
    pattern = _header_pattern(header)
    if pattern is None:
        raise AssertionError(f"not a conditional header: {header!r}")
    tree = ast.parse(source)
    matches = [
        (node.lineno, node.col_offset)
        for node in ast.walk(tree)
        if isinstance(node, (ast.If, ast.While))
        and type(node) is type(pattern)
        and ast.dump(node.test, include_attributes=False)
        == ast.dump(pattern.test, include_attributes=False)
    ]
    if not matches:
        raise AssertionError(f"header target not found: {header!r}")
    if unique and len(matches) != 1:
        raise AssertionError(
            f"expected exactly one header target, found {len(matches)}: {header!r}"
        )
    starts = _offsets(source)
    for lineno, column in sorted(matches, reverse=True):
        position = _absolute(source, starts, lineno, column)
        source = (
            source[:position]
            + _replacement(statement)
            + "\n"
            + " " * column
            + source[position:]
        )
    return source

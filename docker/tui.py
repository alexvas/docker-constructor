"""Shared curses TUI for interactive workspace selection.

Extracted from ``launch-pi.py`` so both the standalone launcher and
the constructor CLI facade can reuse the maintained selection behavior
without duplicating curses logic.

All functions are pure except for curses terminal manipulation.
No Docker, subprocess, or filesystem side effects live here.
"""

from __future__ import annotations

import curses
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ═══════════════════════════════════════════════════════════════════════
# Types
# ═══════════════════════════════════════════════════════════════════════


@dataclass
class TreeNode:
    """Filesystem directory node in the TUI tree."""

    path: Path
    name: str
    expanded: bool = False
    has_subdirs: bool = False
    children: list["TreeNode"] = field(default_factory=list)
    depth: int = 0


@dataclass
class FlatItem:
    """Flattened display item in the TUI list."""

    kind: str  # "ide" | "tree" | "separator"
    label: str
    indent: int = 0
    ide_index: int | None = None
    node: TreeNode | None = None


# ═══════════════════════════════════════════════════════════════════════
# Tree construction
# ═══════════════════════════════════════════════════════════════════════


def build_tree(
    root: Path,
    depth: int = 0,
    max_depth: int = 5,
) -> list[TreeNode]:
    """Return only subdirectories of *root*, sorted alphabetically."""
    if depth >= max_depth:
        return []
    entries: list[TreeNode] = []
    try:
        for p in sorted(root.iterdir(), key=lambda p: p.name):
            if not p.is_dir():
                continue
            has_subdirs = (
                any(q.is_dir() for q in p.iterdir()) if depth + 1 < max_depth else False
            )
            entries.append(
                TreeNode(
                    path=p,
                    name=p.name,
                    expanded=False,
                    has_subdirs=has_subdirs,
                    depth=depth,
                )
            )
    except PermissionError:
        pass
    return entries


# ═══════════════════════════════════════════════════════════════════════
# TUI rendering helpers
# ═══════════════════════════════════════════════════════════════════════


def _tui_draw_header(stdscr: Any, container_num: int) -> None:
    h, w = stdscr.getmaxyx()
    title = f"π Launcher — container: π-{container_num} "
    stdscr.addstr(0, max(0, (w - len(title)) // 2), title, curses.A_REVERSE)


def _tui_status(stdscr: Any, text: str, color: int) -> None:
    h, w = stdscr.getmaxyx()
    stdscr.addstr(h - 1, 0, text.ljust(w)[: w - 1], color)


def _selection_status_label(primary_name: str, extra_count: int) -> str:
    """Return the workspace-oriented TUI selection status label."""
    return (
        "  Space:cycle  Enter:primary  "
        "dbl-Enter/F5/r:launch  ←/→:collapse/expand  q:quit"
        f"    primary: {primary_name}"
        f"  extra: {extra_count}"
    )


def _item_key(item: FlatItem) -> tuple:
    if item.kind == "ide":
        return ("ide", item.ide_index)
    if item.kind == "tree" and item.node is not None:
        return ("tree", str(item.node.path))
    return ("separator", id(item))


def _add_subtree(
    result: list[FlatItem],
    node: TreeNode,
    indent: int,
) -> None:
    item = FlatItem(kind="tree", label=node.name, indent=indent, node=node)
    result.append(item)
    if node.expanded:
        for child in node.children:
            _add_subtree(result, child, indent + 1)


def _rebuild_flat(source: list[FlatItem]) -> list[FlatItem]:
    result: list[FlatItem] = []
    for item in source:
        result.append(item)
        if item.kind == "tree" and item.node is not None and item.node.expanded:
            for child in item.node.children:
                _add_subtree(result, child, item.indent + 1)
    return result


def _tree_indicator(node: TreeNode) -> str:
    if node.expanded:
        return "[-]"
    if node.has_subdirs:
        return "[+]"
    return "[ ]"


# ═══════════════════════════════════════════════════════════════════════
# Public TUI entry point
# ═══════════════════════════════════════════════════════════════════════


def run_tui(
    ide_workspaces: list[dict],
    tree_roots: list[TreeNode] | None = None,
    *,
    light_theme: bool = False,
    container_num: int = 1,
) -> tuple[FlatItem | None, list[FlatItem]]:
    """Interactive curses workspace-selection TUI.

    Parameters
    ----------
    ide_workspaces:
        Live IDE projects (each a dict with ``ideName``, ``port``,
        ``workspaceFolders``).
    tree_roots:
        Filesystem tree roots (from :func:`build_tree`).
    light_theme:
        Use light colour scheme.
    container_num:
        Container number displayed in the header (caller determines
        the next available pi-N number before invoking the TUI).

    Returns
    -------
    A ``(primary_item, extra_items)`` tuple.  *primary_item* is
    ``None`` when the user cancels (q/Esc).
    """
    num = container_num

    # Build source list (canonical items before expand/collapse)
    source: list[FlatItem] = []
    has_ide = len(ide_workspaces) > 0
    has_tree = tree_roots is not None and len(tree_roots) > 0

    if has_ide:
        for i, _proj in enumerate(ide_workspaces):
            source.append(FlatItem(kind="ide", label="", indent=0, ide_index=i))
    if has_ide and has_tree:
        source.append(FlatItem(kind="separator", label="Открыты в IDE", indent=0))
    if has_tree:
        source.append(
            FlatItem(
                kind="separator",
                label="В базовой директории",
                indent=0,
            )
        )
        for node in tree_roots:
            source.append(
                FlatItem(
                    kind="tree",
                    label=node.name,
                    indent=0,
                    node=node,
                )
            )

    flat_items = _rebuild_flat(source)

    # Selection state
    primary_key: tuple | None = None
    extra_keys: set[tuple] = set()
    cursor = 0
    viewport_offset = 0
    status_msg = ""
    status_ttl = 0
    last_enter_time = 0.0

    # Initialize: first item as primary
    if flat_items:
        first = flat_items[0]
        if first.kind != "separator":
            primary_key = _item_key(first)
            cursor = 0
        else:
            # Skip separators for initial selection
            for idx, item in enumerate(flat_items):
                if item.kind != "separator":
                    primary_key = _item_key(item)
                    cursor = idx
                    break

    def _current_primary_idx() -> int | None:
        if primary_key is None:
            return None
        for idx, item in enumerate(flat_items):
            if _item_key(item) == primary_key:
                return idx
        return None

    def _current_extra_set() -> set[int]:
        result: set[int] = set()
        for idx, item in enumerate(flat_items):
            if _item_key(item) in extra_keys:
                result.add(idx)
        return result

    def _selectable_indices() -> list[int]:
        return [i for i, item in enumerate(flat_items) if item.kind != "separator"]

    def _clamp_cursor() -> None:
        nonlocal cursor
        sel = _selectable_indices()
        if not sel:
            return
        if cursor not in sel:
            # Find nearest selectable
            cursor = min(sel, key=lambda i: abs(i - cursor))

    def _ensure_cursor_visible(list_height: int) -> None:
        nonlocal viewport_offset, cursor
        if not flat_items:
            viewport_offset = 0
            return
        # Shift viewport so cursor stays within the visible window
        if cursor < viewport_offset:
            viewport_offset = cursor
        elif cursor >= viewport_offset + list_height:
            viewport_offset = cursor - list_height + 1
        # Clamp to valid range
        max_offset = max(0, len(flat_items) - list_height)
        if viewport_offset > max_offset:
            viewport_offset = max_offset
        if viewport_offset < 0:
            viewport_offset = 0

    def draw(stdscr: Any) -> tuple[FlatItem | None, list[FlatItem]]:
        nonlocal \
            primary_key, \
            extra_keys, \
            cursor, \
            viewport_offset, \
            status_msg, \
            status_ttl, \
            last_enter_time
        nonlocal flat_items

        curses.curs_set(0)
        curses.start_color()
        curses.use_default_colors()

        if light_theme:
            curses.init_pair(1, curses.COLOR_BLUE, -1)
            curses.init_pair(2, curses.COLOR_GREEN, -1)
            curses.init_pair(3, curses.COLOR_BLACK, curses.COLOR_CYAN)
        else:
            curses.init_pair(1, curses.COLOR_YELLOW, -1)
            curses.init_pair(2, curses.COLOR_GREEN, -1)
            curses.init_pair(3, curses.COLOR_WHITE, curses.COLOR_BLUE)

        COLOR_PRIMARY = curses.color_pair(1) | curses.A_BOLD
        COLOR_EXTRA = curses.color_pair(2)
        COLOR_STATUS = curses.color_pair(3)
        COLOR_DIM = curses.A_DIM if hasattr(curses, "A_DIM") else curses.A_NORMAL

        while True:
            h, w = stdscr.getmaxyx()
            if h < 5:
                stdscr.clear()
                stdscr.addstr(0, 0, "terminal too small", curses.A_BOLD)
                stdscr.refresh()
                if stdscr.getch() == ord("q"):
                    sys.exit(1)
                continue

            stdscr.erase()
            _tui_draw_header(stdscr, num)

            # If the very first item is a separator, skip the blank row
            list_start = 1 if (flat_items and flat_items[0].kind == "separator") else 2
            list_height = h - 3

            # Ensure viewport keeps cursor visible
            _ensure_cursor_visible(list_height)

            primary_idx = _current_primary_idx()
            extra_set = _current_extra_set()

            visible_end = min(
                len(flat_items),
                viewport_offset + list_height,
            )
            for vi in range(viewport_offset, visible_end):
                item = flat_items[vi]
                row = list_start + (vi - viewport_offset)

                is_primary = vi == primary_idx
                is_extra = vi in extra_set
                is_cursor = vi == cursor

                if item.kind == "separator":
                    # Pseudographics separator
                    if has_ide and has_tree:
                        if item.label == "Открыты в IDE":
                            sep = f" ───═══ {item.label}: ═══───"
                        else:
                            sep = f" ───═══ {item.label} ═══───"
                    else:
                        sep = f" ───═══ {item.label} ═══───"
                    line = sep.ljust(w)[:w]
                    attr = COLOR_DIM
                    if is_cursor:
                        attr |= curses.A_REVERSE
                    stdscr.addstr(row, 0, line, attr)
                    continue

                # Build display line
                sel_marker = "★" if is_primary else ("✓" if is_extra else " ")

                if item.kind == "ide" and item.ide_index is not None:
                    proj = ide_workspaces[item.ide_index]
                    ide = proj["ideName"]
                    port_str = str(proj["port"])
                    ws_path = (
                        proj["workspaceFolders"][0] if proj["workspaceFolders"] else "?"
                    )
                    suffix = f"  :{port_str}  {ide}"
                    prefix = "  "
                    available = w - len(prefix) - len(suffix) - 4
                    if available < 10:
                        available = 10
                    if len(ws_path) > available:
                        ws_path = "..." + ws_path[-(available - 3) :]
                    left = prefix + ws_path
                    right = f"{sel_marker}  {suffix}"
                    right_aligned = (
                        right.rjust(w - len(left))
                        if len(left) + len(right) <= w
                        else right
                    )
                    line = left + right_aligned[len(left) :]
                elif item.kind == "tree":
                    indent_str = "    " * item.indent
                    indicator = _tree_indicator(item.node) if item.node else "[ ]"
                    left = f"{indent_str}{indicator} {item.label}/"
                    right_marker = f" {sel_marker}"
                    # Pad to right alignment column
                    if len(left) < w - 4:
                        line = left + " " * (w - 4 - len(left)) + right_marker
                    else:
                        line = left + right_marker
                else:
                    line = f"  {item.label}"

                line = line[:w]

                if is_primary:
                    attr = COLOR_PRIMARY
                elif is_extra:
                    attr = COLOR_EXTRA
                else:
                    attr = curses.A_NORMAL
                if is_cursor:
                    attr |= curses.A_REVERSE
                stdscr.addstr(row, 0, line, attr)

            # Status bar
            if status_ttl > 0:
                status_ttl -= 1
            else:
                status_msg = ""

            if status_msg:
                _tui_status(
                    stdscr,
                    f"  {status_msg}",
                    COLOR_STATUS,
                )
            else:
                primary_name = "?"
                if primary_idx is not None and primary_idx < len(flat_items):
                    mi = flat_items[primary_idx]
                    if mi.kind == "ide" and mi.ide_index is not None:
                        p = ide_workspaces[mi.ide_index]
                        primary_name = (
                            Path(p["workspaceFolders"][0]).name
                            if p["workspaceFolders"]
                            else "?"
                        )
                    elif mi.kind == "tree":
                        primary_name = mi.label
                footer = _selection_status_label(
                    primary_name,
                    len(extra_keys),
                )
                _tui_status(stdscr, footer, COLOR_STATUS)

            stdscr.refresh()
            key = stdscr.getch()

            if key in (ord("q"), 27):
                return None, []
            elif key in (curses.KEY_UP, ord("k")):
                sel = _selectable_indices()
                if sel:
                    try:
                        cur_pos = sel.index(cursor)
                        if cur_pos > 0:
                            cursor = sel[cur_pos - 1]
                    except ValueError:
                        cursor = sel[-1] if sel else 0
            elif key in (curses.KEY_DOWN, ord("j")):
                sel = _selectable_indices()
                if sel:
                    try:
                        cur_pos = sel.index(cursor)
                        if cur_pos < len(sel) - 1:
                            cursor = sel[cur_pos + 1]
                    except ValueError:
                        cursor = sel[0]
            elif key == curses.KEY_RIGHT:
                item = flat_items[cursor] if cursor < len(flat_items) else None
                if item is not None and item.kind == "tree" and item.node is not None:
                    node = item.node
                    if node.has_subdirs and not node.expanded:
                        node.children = build_tree(
                            node.path,
                            node.depth + 1,
                        )
                        node.expanded = True
                        flat_items = _rebuild_flat(source)
                        _clamp_cursor()
            elif key == curses.KEY_LEFT:
                item = flat_items[cursor] if cursor < len(flat_items) else None
                if item is not None and item.kind == "tree" and item.node is not None:
                    node = item.node
                    if node.expanded:
                        # Find descendant range in flat_items
                        start_idx = cursor
                        end_idx = cursor
                        for j in range(cursor + 1, len(flat_items)):
                            fj = flat_items[j]
                            if fj.kind == "tree" and fj.indent > node.depth:
                                end_idx = j
                            else:
                                break
                        node.expanded = False
                        flat_items = _rebuild_flat(source)
                        # If cursor was inside subtree, jump to parent
                        if cursor > start_idx and cursor <= end_idx:
                            for idx, fi in enumerate(flat_items):
                                if fi.kind == "tree" and fi.node is node:
                                    cursor = idx
                                    break
                        _clamp_cursor()
            elif key in (curses.KEY_ENTER, 10, 13):
                item = flat_items[cursor] if cursor < len(flat_items) else None
                if item is None or item.kind == "separator":
                    continue
                now = time.monotonic()
                if now - last_enter_time < 0.5 and primary_key is not None:
                    # Launch — double-enter
                    primary_idx_final = _current_primary_idx()
                    if primary_idx_final is not None:
                        primary_item = flat_items[primary_idx_final]
                        extra_items = [
                            flat_items[i]
                            for i in sorted(_current_extra_set())
                            if flat_items[i].kind != "separator"
                        ]
                        return primary_item, extra_items
                last_enter_time = now
                # Single Enter: set as primary
                primary_key = _item_key(item)
                extra_keys.discard(_item_key(item))
            elif key in (curses.KEY_F5, ord("r")):
                if primary_key is not None:
                    primary_idx_final = _current_primary_idx()
                    if primary_idx_final is not None:
                        primary_item = flat_items[primary_idx_final]
                        extra_items = [
                            flat_items[i]
                            for i in sorted(_current_extra_set())
                            if flat_items[i].kind != "separator"
                        ]
                        return primary_item, extra_items
            elif key == ord(" "):
                item = flat_items[cursor] if cursor < len(flat_items) else None
                if item is None or item.kind == "separator":
                    continue
                ik = _item_key(item)
                if ik == primary_key:
                    # primary → not selected
                    primary_key = None
                    extra_keys.discard(ik)
                elif ik in extra_keys:
                    if primary_key is not None:
                        # extra → primary (swap)
                        extra_keys.discard(ik)
                        if primary_key is not None:
                            extra_keys.add(primary_key)
                        primary_key = ik
                    else:
                        # extra → not selected
                        extra_keys.discard(ik)
                else:
                    if primary_key is not None:
                        # not selected → extra
                        extra_keys.add(ik)
                    else:
                        # not selected → primary
                        primary_key = ik
                        extra_keys.discard(ik)
            elif key == curses.KEY_RESIZE:
                pass

    return curses.wrapper(draw)

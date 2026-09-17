"""Phase 3 RED evidence: visual replacement boundaries and canonical migration.

These tests pin:

* every rendered replacement fragment begins with
  ``# --- <display path> ---`` where the display path strips the leading
  ``build.stages.`` / ``runtime.`` prefix exactly once while the TOML table
  headers retain the full canonical path;
* visual comments are display-only — missing, altered, or duplicated
  comments never change parsing, validation, or fragment construction;
* the repository canonical ``docker-constructor.toml`` carries the matching
  visual header immediately before every replaceable block.
"""
from __future__ import annotations

import pathlib
import tomllib
import unittest

from docker.versioning.inventory import validate_inventory
from docker.versioning.model import UpdateKind, UpdateResult, UpdateStatus
from docker.versioning.updates import (
    build_replacement_blocks,
    build_update_targets,
    display_path,
    group_targets_by_owner,
    render_replacement_fragments,
    serialize_replacement_block,
)

from tests.versioning.support.inventory_builder import minimal_toml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CANONICAL = REPO_ROOT / "docker-constructor.toml"

# Owner path → visual display path (leading ``build.stages.``/``runtime.``
# stripped exactly once).
CANONICAL_OWNERS = {
    "build.stages.base.node": "base.node",
    "build.stages.toolchain.rust": "toolchain.rust",
    "build.stages.toolchain.uv": "toolchain.uv",
    "build.stages.toolchain.python": "toolchain.python",
    "build.stages.toolchain.ty": "toolchain.ty",
    "build.stages.rtk-prebuilt.rtk": "rtk-prebuilt.rtk",
    "build.stages.fd-prebuilt.fd": "fd-prebuilt.fd",
    "build.stages.pi-tools.pi": "pi-tools.pi",
    "build.stages.openspec-tools.openspec": "openspec-tools.openspec",
    "build.stages.runtime.oh-my-zsh": "runtime.oh-my-zsh",
    "runtime.pi-extensions.pi-read": "pi-extensions.pi-read",
    "runtime.pi-extensions.pi-usage": "pi-extensions.pi-usage",
    "runtime.pi-extensions.pi-proxy": "pi-extensions.pi-proxy",
}

# Owner → (current, candidate) for a synthetic applicable VERSION update.
# oh-my-zsh is a git revision and is handled specially below.
_OWNER_VERSIONS = {
    "build.stages.base.node": ("24-trixie-slim", "25-trixie-slim"),
    "build.stages.toolchain.rust": ("1.0.0", "1.1.0"),
    "build.stages.toolchain.uv": ("0.1.0", "0.2.0"),
    "build.stages.toolchain.python": ("3.14.6", "3.15.0"),
    "build.stages.toolchain.ty": ("0.0.61", "0.0.62"),
    "build.stages.rtk-prebuilt.rtk": ("v0.43.0", "v0.44.0"),
    "build.stages.fd-prebuilt.fd": ("v10.4.2", "v10.5.0"),
    "build.stages.pi-tools.pi": ("0.80.10", "0.81.0"),
    "build.stages.openspec-tools.openspec": ("1.6.0", "1.7.0"),
    "build.stages.runtime.oh-my-zsh": ("a" * 40, "b" * 40),
    "runtime.pi-extensions.pi-read": ("0.2.0", "0.3.0"),
    "runtime.pi-extensions.pi-usage": ("0.52.3", "0.53.0"),
    "runtime.pi-extensions.pi-proxy": ("1.0.0", "1.1.0"),
}


def _outdated(path: str, current: str, candidate: str) -> UpdateResult:
    kind = (
        UpdateKind.REVISION
        if path.endswith("oh-my-zsh")
        else UpdateKind.VERSION
    )
    return UpdateResult(
        path=path,
        provider="test",
        current=current,
        candidate=candidate,
        status=UpdateStatus.OUTDATED,
        kind=kind,
        applicable=True,
        reason=None,
        artifacts={},
    )


def _raw_and_targets():
    raw = tomllib.loads(minimal_toml())
    inventory = validate_inventory(raw)
    targets = build_update_targets(inventory)
    return raw, inventory, targets


class TestFragmentVisualHeaders(unittest.TestCase):
    """3.1 — fragments begin with `# --- <display path> ---`."""

    def test_display_path_strips_single_prefix(self) -> None:
        self.assertEqual(display_path("build.stages.base.node"), "base.node")
        self.assertEqual(
            display_path("build.stages.runtime.oh-my-zsh"),
            "runtime.oh-my-zsh",
        )
        self.assertEqual(
            display_path("runtime.pi-extensions.pi-read"),
            "pi-extensions.pi-read",
        )
        # "once" — the embedded ``runtime.`` after ``build.stages.`` is kept.
        self.assertEqual(
            display_path("build.stages.runtime.oh-my-zsh"),
            "runtime.oh-my-zsh",
        )

    def test_display_path_matches_canonical_mapping(self) -> None:
        for owner, expected in CANONICAL_OWNERS.items():
            with self.subTest(owner=owner):
                self.assertEqual(display_path(owner), expected)

    def test_every_fragment_begins_with_matching_header(self) -> None:
        raw, _, targets = _raw_and_targets()
        results = [
            _outdated(owner, current, candidate)
            for owner, (current, candidate) in _OWNER_VERSIONS.items()
        ]
        blocks = build_replacement_blocks(raw, targets, results)
        owners = {owner for owner, _ in blocks}
        self.assertEqual(owners, set(CANONICAL_OWNERS))

        for owner, block in blocks:
            with self.subTest(owner=owner):
                fragment = serialize_replacement_block(owner, block)
                self.assertTrue(
                    fragment.startswith(
                        f"# --- {CANONICAL_OWNERS[owner]} ---\n"
                    ),
                    fragment,
                )
                # Full canonical TOML table path is retained.
                self.assertIn(f"[{owner}]\n", fragment)
                # The shortened display path never becomes a table header.
                self.assertNotIn(
                    f"[{CANONICAL_OWNERS[owner]}]\n", fragment,
                )

    def test_combined_render_headers_delimit_blocks(self) -> None:
        raw, _, targets = _raw_and_targets()
        results = [
            _outdated("build.stages.base.node", "24-trixie-slim", "25-trixie-slim"),
            _outdated("runtime.pi-extensions.pi-read", "0.2.0", "0.3.0"),
        ]
        text = render_replacement_fragments(raw, targets, results)
        # Build block precedes runtime block (inventory order); each header
        # sits immediately before its complete canonical block.
        self.assertIn(
            "# --- base.node ---\n[build.stages.base.node]\n", text,
        )
        self.assertIn(
            "# --- pi-extensions.pi-read ---\n"
            '[runtime.pi-extensions.pi-read]\n',
            text,
        )


class TestVisualCommentsAreDisplayOnly(unittest.TestCase):
    """3.2 — comments never affect parsing, validation, or fragments."""

    def test_missing_altered_duplicated_comments_are_ignored(self) -> None:
        baseline = minimal_toml()
        # Altered + duplicated comments before one block; nothing before
        # another (missing).  Comments must be display-only.
        altered = baseline.replace(
            "[build.stages.base.node]",
            "# --- WRONG altered header ---\n"
            "# --- base.node ---\n"
            "# --- base.node ---\n"
            "[build.stages.base.node]",
        )
        raw_base = tomllib.loads(baseline)
        raw_alt = tomllib.loads(altered)
        # tomllib drops comments, so the parsed mappings are identical.
        self.assertEqual(raw_base, raw_alt)
        validate_inventory(raw_alt)  # must not raise

        results = [_outdated("build.stages.base.node", "24-trixie-slim", "25-trixie-slim")]
        inv_base = validate_inventory(raw_base)
        inv_alt = validate_inventory(raw_alt)
        text_base = render_replacement_fragments(
            raw_base, build_update_targets(inv_base), results,
        )
        text_alt = render_replacement_fragments(
            raw_alt, build_update_targets(inv_alt), results,
        )
        # Fragment construction is comment-independent.
        self.assertEqual(text_base, text_alt)
        # The emitted header is the canonical display path, never the
        # altered comment text.
        self.assertTrue(text_alt.startswith("# --- base.node ---\n"))


class TestCanonicalInventoryVisualHeaders(unittest.TestCase):
    """3.3 — canonical docker-constructor.toml carries matching headers."""

    def test_code_owners_match_canonical_blocks(self) -> None:
        raw = tomllib.loads(CANONICAL.read_text())
        inventory = validate_inventory(raw)
        targets = build_update_targets(inventory)
        owners = {owner for owner, _ in group_targets_by_owner(targets)}
        self.assertEqual(owners, set(CANONICAL_OWNERS))

    def test_visual_header_immediately_before_each_owner(self) -> None:
        lines = CANONICAL.read_text().splitlines()
        for owner, display in CANONICAL_OWNERS.items():
            with self.subTest(owner=owner):
                header_line = f"[{owner}]"
                self.assertIn(header_line, lines)
                idx = lines.index(header_line)
                self.assertEqual(lines[idx - 1], f"# --- {display} ---")


if __name__ == "__main__":
    unittest.main()

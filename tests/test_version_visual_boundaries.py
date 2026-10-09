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
    render_replacement_fragments,
    serialize_replacement_block,
)

from tests.live_owner_audit import (
    independent_update_owners,
    owner_header_violations,
    production_update_owners,
)
from tests.versioning.support.inventory_builder import minimal_toml
from tests.inventory_fixtures import stable_inventory_path

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
# Behavioural lane: committed stable fixture (fixed owner→display examples).
STABLE = stable_inventory_path()
# Live-contract lane: the repository's reviewed inventory.
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


def _assert_owner_headers(document_text: str, owners) -> None:
    """Assert each *owner* table has its independently derived header."""
    violations = owner_header_violations(document_text, owners)
    assert not violations, f"owner header violations: {violations}"


def _assert_independent_membership(
    document_text: str,
    *,
    omit_paths=(),
) -> set[str]:
    """Compare independently enumerated owners with production membership.

    Raises ``AssertionError`` when the two disagree, so a production
    enumeration that silently drops an owner is detected.  *omit_paths*
    simulates such an omission for regression coverage.
    """
    raw = tomllib.loads(document_text)
    inventory = validate_inventory(raw)
    independent = set(independent_update_owners(raw))
    production = set(production_update_owners(inventory, omit_paths=omit_paths))
    assert independent == production, (
        "owner membership mismatch: "
        f"independent-only={sorted(independent - production)}, "
        f"production-only={sorted(production - independent)}"
    )
    return independent


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


class TestStableFixtureVisualHeaders(unittest.TestCase):
    """Fixed-fixture lane — exact owner→display examples.

    The stable fixture pins the full reviewed block set, so the canonical
    mapping can be asserted exactly without touching the live inventory.
    """

    def test_fixture_owners_match_examples(self) -> None:
        raw = tomllib.loads(STABLE.read_text())
        independent = set(independent_update_owners(raw))
        self.assertEqual(independent, set(CANONICAL_OWNERS))

    def test_fixture_membership_matches_production(self) -> None:
        owners = _assert_independent_membership(STABLE.read_text())
        self.assertEqual(owners, set(CANONICAL_OWNERS))

    def test_production_omission_is_detected(self) -> None:
        # Simulate production enumeration dropping ``toolchain.ty``: the
        # independent membership check must fail rather than inherit the
        # omission.
        with self.assertRaises(AssertionError):
            _assert_independent_membership(
                STABLE.read_text(),
                omit_paths=("build.stages.toolchain.ty",),
            )

    def test_every_fixture_owner_has_matching_header(self) -> None:
        _assert_owner_headers(STABLE.read_text(), CANONICAL_OWNERS)

    def test_missing_or_misplaced_header_is_detected(self) -> None:
        # Negative: remove one header.  The boundary check must fail.
        without_header = STABLE.read_text().replace("# --- base.node ---\n", "", 1)
        self.assertNotEqual(without_header, STABLE.read_text())
        with self.assertRaises(AssertionError):
            _assert_owner_headers(without_header, CANONICAL_OWNERS)

        # Negative: move a header so it no longer immediately precedes its
        # owner table.
        misplaced = STABLE.read_text().replace(
            "# --- toolchain.uv ---\n[build.stages.toolchain.uv]",
            "[build.stages.toolchain.uv]\n# --- toolchain.uv ---",
            1,
        )
        self.assertNotEqual(misplaced, STABLE.read_text())
        with self.assertRaises(AssertionError):
            _assert_owner_headers(misplaced, CANONICAL_OWNERS)


class TestLiveInventoryVisualHeaders(unittest.TestCase):
    """Live-contract lane — every declared owner has a matching header.

    Owner membership is enumerated independently from raw TOML, then
    cross-checked against the production replacement-owner enumeration, so
    an omitted owner cannot escape detection.  Nothing is hardcoded, so
    installing or removing an optional extension needs no change.
    """

    def test_every_declared_owner_has_matching_header(self) -> None:
        document = CANONICAL.read_text()
        independent = _assert_independent_membership(document)
        self.assertTrue(independent)
        self.assertEqual([], owner_header_violations(document, independent))

    def test_owner_membership_omission_is_detected(self) -> None:
        with self.assertRaises(AssertionError):
            _assert_independent_membership(
                CANONICAL.read_text(),
                omit_paths=("build.stages.toolchain.ty",),
            )


if __name__ == "__main__":
    unittest.main()

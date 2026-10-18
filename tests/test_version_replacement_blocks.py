"""Replacement ownership and raw block evidence tests.

Phase 1 RED evidence for the ``render-replaceable-update-suggestions``
change.  These tests pin the target-to-owner mapping, deterministic
grouping, raw-subtree selection, and candidate overlay behaviour without
touching provider discovery, classification, or JSON serialization.
"""

from __future__ import annotations

import tomllib
import unittest

from docker.versioning.model import (
    CandidateArtifact,
    UpdateKind,
    UpdateResult,
    UpdateStatus,
)
from docker.versioning.inventory import validate_inventory
from docker.versioning.updates import (
    build_replacement_blocks,
    build_update_targets,
    extract_raw_block,
    group_targets_by_owner,
    overlay_candidates,
    path_segments,
    replacement_owner,
)
from tests.versioning.support.inventory_builder import minimal_toml


def _raw_and_targets(toml_text: str | None = None):
    """Parse raw TOML, validate it, and build update targets."""
    raw = tomllib.loads(toml_text or minimal_toml())
    inventory = validate_inventory(raw)
    targets = build_update_targets(inventory)
    return raw, inventory, targets


def _multi_platform_toml() -> str:
    """minimal_toml plus a second uv platform artifact (linux-arm64)."""
    return (
        minimal_toml()
        + """

[build.stages.toolchain.uv.artifacts.linux-arm64]
url = "https://github.com/astral-sh/uv/releases/download/0.1.0/uv-aarch64-unknown-linux-gnu.tar.gz"
sha256 = "faaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
"""
    )


class TestReplacementOwnership(unittest.TestCase):
    """1.1 — every update target maps to exactly one replaceable raw block."""

    def test_every_target_maps_to_exactly_one_owner(self):
        raw, _, targets = _raw_and_targets()
        owners: dict[str, str] = {}
        for target in targets:
            owner = replacement_owner(target.path)
            self.assertIsInstance(owner, str)
            self.assertTrue(owner)
            owners[target.path] = owner

        # Every owner resolves to a real raw inventory table.
        for owner in set(owners.values()):
            block = extract_raw_block(raw, path_segments(owner))
            self.assertIsInstance(block, dict)
            self.assertTrue(block)

        # Rust and its nested rustup bootstrap share one owner.
        self.assertEqual(
            replacement_owner("build.stages.toolchain.rust"),
            "build.stages.toolchain.rust",
        )
        self.assertEqual(
            replacement_owner("build.stages.toolchain.rust.rustup"),
            "build.stages.toolchain.rust",
        )

    def test_every_target_family_has_an_owner(self):
        _, _, targets = _raw_and_targets()
        for target in targets:
            # Must not raise — every current target family is covered.
            owner = replacement_owner(target.path)
            self.assertTrue(owner.startswith("build.") or owner.startswith("runtime."))

    def test_pi_extensions_own_their_own_block(self):
        _, _, targets = _raw_and_targets()
        for target in targets:
            if target.path.startswith("runtime.pi-extensions."):
                self.assertEqual(replacement_owner(target.path), target.path)


class TestReplacementGrouping(unittest.TestCase):
    """1.2 — deterministic grouping with no overlapping owner blocks."""

    def test_grouping_is_deterministic(self):
        _, _, targets = _raw_and_targets()
        first = group_targets_by_owner(targets)
        second = group_targets_by_owner(targets)
        self.assertEqual(first, second)

    def test_no_overlapping_owner_blocks(self):
        _, _, targets = _raw_and_targets()
        grouped = group_targets_by_owner(targets)
        owners = [owner for owner, _ in grouped]

        # Distinct owners, each emitted once.
        self.assertEqual(len(owners), len(set(owners)))

        # Every target appears exactly once across all groups.
        seen: list[str] = []
        for _owner, group in grouped:
            for target in group:
                seen.append(target.path)
        self.assertEqual(sorted(seen), sorted(t.path for t in targets))

    def test_distinct_owners_in_inventory_order(self):
        _, _, targets = _raw_and_targets()
        grouped = group_targets_by_owner(targets)
        owners = [owner for owner, _ in grouped]

        expected: list[str] = []
        seen: set[str] = set()
        for target in targets:
            owner = replacement_owner(target.path)
            if owner not in seen:
                seen.add(owner)
                expected.append(owner)
        self.assertEqual(owners, expected)

    def test_rust_and_rustup_grouped_together(self):
        _, _, targets = _raw_and_targets()
        grouped = dict(group_targets_by_owner(targets))
        rust_group = grouped.get("build.stages.toolchain.rust")
        self.assertIsNotNone(rust_group)
        paths = {t.path for t in rust_group}
        self.assertIn("build.stages.toolchain.rust", paths)
        self.assertIn("build.stages.toolchain.rust.rustup", paths)
        self.assertEqual(len(rust_group), 2)


class TestRawBlockOverlay(unittest.TestCase):
    """1.3 — complete raw block retention with candidate-only overlay."""

    def test_complete_raw_block_retains_unchanged_and_overlays_applicable(self):
        raw, _, targets = _raw_and_targets(_multi_platform_toml())
        results = [
            UpdateResult(
                path="build.stages.toolchain.uv",
                provider="github-release",
                current="0.1.0",
                candidate="0.2.0",
                status=UpdateStatus.OUTDATED,
                kind=UpdateKind.VERSION,
                applicable=True,
                reason=None,
                artifacts={
                    "linux-amd64": CandidateArtifact(
                        platform="linux-amd64",
                        name="uv-amd64",
                        url="https://github.com/astral-sh/uv/releases/download/0.2.0/uv-x86_64-unknown-linux-gnu.tar.gz",
                        sha256="b" * 64,
                    ),
                },
            ),
            UpdateResult(
                path="build.stages.toolchain.rust",
                provider="rust-channel",
                current="1.0.0",
                candidate="1.1.0",
                status=UpdateStatus.OUTDATED,
                kind=UpdateKind.VERSION,
                applicable=False,
                reason="not applicable",
                artifacts={},
            ),
        ]

        blocks = dict(build_replacement_blocks(raw, targets, results))

        # Only the applicable uv owner appears; rust was non-applicable.
        self.assertEqual(set(blocks), {"build.stages.toolchain.uv"})

        uv = blocks["build.stages.toolchain.uv"]

        # Candidate leaves overlaid.
        self.assertEqual(uv["version"], "0.2.0")
        self.assertEqual(uv["source"]["tag"], "0.2.0")
        self.assertEqual(
            uv["artifacts"]["linux-amd64"]["url"],
            "https://github.com/astral-sh/uv/releases/download/0.2.0/uv-x86_64-unknown-linux-gnu.tar.gz",
        )
        self.assertEqual(uv["artifacts"]["linux-amd64"]["sha256"], "b" * 64)

        # Unchanged source / update fields retained.
        self.assertEqual(uv["source"]["type"], "github-release")
        self.assertEqual(uv["source"]["repository"], "astral-sh/uv")
        self.assertEqual(uv["update"]["provider"], "github-release")
        self.assertEqual(uv["update"]["stable_only"], True)
        self.assertEqual(uv["update"]["required_platforms"], ["linux-amd64"])

        # Untouched platform artifact retained unchanged.
        arm = uv["artifacts"]["linux-arm64"]
        self.assertEqual(
            arm["url"],
            "https://github.com/astral-sh/uv/releases/download/0.1.0/uv-aarch64-unknown-linux-gnu.tar.gz",
        )
        self.assertEqual(arm["sha256"], "f" + "a" * 63)

        # Original raw block is not mutated.
        self.assertEqual(
            raw["build"]["stages"]["toolchain"]["uv"]["version"],
            "0.1.0",
        )

    def test_rust_and_rustup_overlay_share_one_block(self):
        raw, _, targets = _raw_and_targets()
        results = [
            UpdateResult(
                path="build.stages.toolchain.rust",
                provider="rust-channel",
                current="1.0.0",
                candidate="1.1.0",
                status=UpdateStatus.OUTDATED,
                kind=UpdateKind.VERSION,
                applicable=True,
                reason=None,
                artifacts={},
            ),
            UpdateResult(
                path="build.stages.toolchain.rust.rustup",
                provider="static-url",
                current="0" * 64,
                candidate="c" * 64,
                status=UpdateStatus.OUTDATED,
                kind=UpdateKind.DIGEST_REFRESH,
                applicable=True,
                reason=None,
                artifacts={
                    "linux-amd64": CandidateArtifact(
                        platform="linux-amd64",
                        name="rustup-init",
                        url="https://static.rust-lang.org/rustup/dist/x86_64-unknown-linux-gnu/rustup-init",
                        sha256="c" * 64,
                    ),
                },
            ),
        ]

        blocks = dict(build_replacement_blocks(raw, targets, results))

        # One shared owner block for both targets.
        self.assertEqual(set(blocks), {"build.stages.toolchain.rust"})
        rust = blocks["build.stages.toolchain.rust"]

        # Both candidate leaves overlaid.
        self.assertEqual(rust["version"], "1.1.0")
        self.assertEqual(
            rust["rustup"]["artifacts"]["linux-amd64"]["sha256"],
            "c" * 64,
        )

        # Unchanged rust fields retained.
        self.assertEqual(rust["profile"], "minimal")
        self.assertEqual(rust["components"], ["rustfmt", "clippy"])
        self.assertEqual(
            rust["rustup"]["artifacts"]["linux-amd64"]["url"],
            "https://static.rust-lang.org/rustup/dist/x86_64-unknown-linux-gnu/rustup-init",
        )
        self.assertEqual(
            rust["source"]["manifest"],
            "https://static.rust-lang.org/dist/channel-rust-1.1.0.toml",
        )

    def test_python_override_retained_when_version_overlaid(self):
        raw, _, targets = _raw_and_targets()
        results = [
            UpdateResult(
                path="build.stages.toolchain.python",
                provider="uv-python",
                current="3.14.6",
                candidate="3.15.0",
                status=UpdateStatus.OUTDATED,
                kind=UpdateKind.VERSION,
                applicable=True,
                reason=None,
                artifacts={},
            ),
        ]

        blocks = dict(build_replacement_blocks(raw, targets, results))
        python = blocks["build.stages.toolchain.python"]

        self.assertEqual(python["version"], "3.15.0")
        # Override retained verbatim.
        self.assertEqual(python["override"]["constraint"], ">=3.14.6")
        self.assertEqual(python["override"]["allow_prerelease"], False)
        self.assertEqual(python["override"]["scheme"], "numeric")
        # Source / update retained.
        self.assertEqual(python["source"]["type"], "uv-python")
        self.assertEqual(python["source"]["implementation"], "cpython")
        self.assertEqual(python["update"]["provider"], "uv-python")

    def test_overlay_candidates_does_not_mutate_input(self):
        raw, _, targets = _raw_and_targets()
        owner = "build.stages.toolchain.uv"
        block = extract_raw_block(raw, path_segments(owner))
        before = repr(block)
        uv_target = [t for t in targets if t.path == owner]
        result = {
            owner: UpdateResult(
                path=owner,
                provider="github-release",
                current="0.1.0",
                candidate="0.2.0",
                status=UpdateStatus.OUTDATED,
                kind=UpdateKind.VERSION,
                applicable=True,
                reason=None,
                artifacts={},
            )
        }
        overlay_candidates(
            block,
            owner=owner,
            targets=uv_target,
            results=result,
        )
        self.assertEqual(repr(block), before)


_DOTTED_EXT_TOML = """
[runtime.pi-extensions."foo.bar"]
version = "1.0.0"

[runtime.pi-extensions."foo.bar".source]
type = "npm"
package = "foo.bar"

[runtime.pi-extensions."foo.bar".artifacts."1.0.0"]
url = "https://registry.npmjs.org/foo.bar/-/foo.bar-1.0.0.tgz"
integrity = "sha512-VO9pV15PFTBOfcNq9hgKJ3K6k4Bb0ndDlX6N5ReNTOa/r66/Ppfc9N/hexsK5veMHGl1YbjCo3wOnL5jJu17/Q=="

[runtime.pi-extensions."foo.bar".update]
provider = "npm"
stable_only = true

[runtime.pi-extensions."foo.bar".override]
constraint = ">=1.0.0"
allow_prerelease = false
scheme = "numeric"

[runtime.pi-extensions."foo.bar".validation]
metadata_file = "package.json"
"""


class TestDottedExtensionName(unittest.TestCase):
    """Regression: dotted Pi-extension names must not be split on '.'."""

    def _raw(self):
        return tomllib.loads(minimal_toml() + _DOTTED_EXT_TOML)

    def test_dotted_extension_name_round_trip(self):
        raw = self._raw()
        before = repr(raw)

        # 1. validate_inventory() accepts the inventory.
        inventory = validate_inventory(raw)

        # 2. build_update_targets() creates the target.
        targets = build_update_targets(inventory)
        ext_targets = [t for t in targets if t.path == "runtime.pi-extensions.foo.bar"]
        self.assertEqual(len(ext_targets), 1)
        self.assertEqual(
            path_segments(ext_targets[0].path),
            ("runtime", "pi-extensions", "foo.bar"),
        )

        # 3. build_replacement_blocks() extracts and returns the block
        #    without KeyError.
        results = [
            UpdateResult(
                path="runtime.pi-extensions.foo.bar",
                provider="npm",
                current="1.0.0",
                candidate="1.1.0",
                status=UpdateStatus.OUTDATED,
                kind=UpdateKind.VERSION,
                applicable=True,
                reason=None,
                artifacts={},
            ),
        ]
        blocks = dict(build_replacement_blocks(raw, targets, results))
        self.assertIn("runtime.pi-extensions.foo.bar", blocks)

        block = blocks["runtime.pi-extensions.foo.bar"]
        self.assertEqual(block["version"], "1.1.0")
        self.assertEqual(block["source"]["package"], "foo.bar")
        # Version-keyed artifact table retained (leaf overlay only).
        self.assertIn("1.0.0", block["artifacts"])
        self.assertEqual(
            block["validation"]["metadata_file"],
            "package.json",
        )
        self.assertEqual(block["override"]["constraint"], ">=1.0.0")

        # 4. Original raw inventory is unchanged.
        self.assertEqual(repr(raw), before)


if __name__ == "__main__":
    unittest.main()

"""Canonical replacement fragment rendering tests.

Phase 2 RED evidence for the ``render-replaceable-update-suggestions``
change.  These tests pin deterministic complete-block TOML serialization,
shared-block combination, load-inventory round trips, and multi-platform
artifact retention.  They construct results directly (no network) and never
touch provider discovery, classification, or JSON serialization.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import tomllib
import unittest

from docker.versioning.inventory import validate_inventory
from docker.versioning.model import (
    CandidateArtifact,
    UpdateKind,
    UpdateResult,
    UpdateStatus,
)
from docker.versioning.updates import (
    build_replacement_blocks,
    build_update_targets,
    path_segments,
    render_replacement_fragments,
    serialize_replacement_block,
)
from tests.versioning.support.inventory_builder import minimal_toml


def _sha256(seed: str) -> str:
    """Return a valid, non-placeholder 64-lowercase-hex digest."""
    return hashlib.sha256(seed.encode()).hexdigest()


def _sri(seed: bytes) -> str:
    """Return a valid ``sha512-<base64>`` SRI string for 64-byte *seed*."""
    return "sha512-" + base64.b64encode(seed).decode()


def _raw_and_targets():
    raw = tomllib.loads(minimal_toml())
    inventory = validate_inventory(raw)
    targets = build_update_targets(inventory)
    return raw, inventory, targets


def _outdated(
    path: str,
    provider: str,
    current: str,
    candidate: str,
    *,
    kind: UpdateKind = UpdateKind.VERSION,
    artifacts: dict | None = None,
    digest: str | None = None,
) -> UpdateResult:
    return UpdateResult(
        path=path,
        provider=provider,
        current=current,
        candidate=candidate,
        status=UpdateStatus.OUTDATED,
        kind=kind,
        applicable=True,
        reason=None,
        artifacts=artifacts or {},
        digest=digest,
    )


def _all_family_results() -> list[UpdateResult]:
    """Applicable results for every update target family in minimal_toml."""
    node_digest = "sha256:" + _sha256("node")
    return [
        _outdated(
            "build.stages.base.node",
            "docker-registry",
            "24-trixie-slim",
            node_digest,
            kind=UpdateKind.DIGEST_REFRESH,
            digest=node_digest,
        ),
        _outdated("build.stages.toolchain.rust", "rust-channel", "1.0.0", "1.1.0"),
        _outdated(
            "build.stages.toolchain.rust.rustup",
            "static-url",
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            _sha256("rustup"),
            kind=UpdateKind.DIGEST_REFRESH,
            artifacts={
                "linux-amd64": CandidateArtifact(
                    platform="linux-amd64",
                    name="rustup-init",
                    url="https://static.rust-lang.org/rustup/dist/x86_64-unknown-linux-gnu/rustup-init",
                    sha256=_sha256("rustup"),
                ),
            },
        ),
        _outdated(
            "build.stages.toolchain.uv",
            "github-release",
            "0.1.0",
            "0.2.0",
            artifacts={
                "linux-amd64": CandidateArtifact(
                    platform="linux-amd64",
                    name="uv-x86_64-unknown-linux-gnu.tar.gz",
                    url="https://github.com/astral-sh/uv/releases/download/0.2.0/uv-x86_64-unknown-linux-gnu.tar.gz",
                    sha256=_sha256("uv"),
                ),
            },
        ),
        _outdated("build.stages.toolchain.python", "uv-python", "3.14.6", "3.15.0"),
        _outdated("build.stages.toolchain.ty", "pypi", "0.0.61", "0.0.62"),
        _outdated(
            "build.stages.rtk-prebuilt.rtk",
            "github-release",
            "v0.43.0",
            "v0.44.0",
            artifacts={
                "linux-amd64": CandidateArtifact(
                    platform="linux-amd64",
                    name="rtk_amd64.deb",
                    url="https://github.com/rtk-ai/rtk/releases/download/v0.44.0/rtk_amd64.deb",
                    sha256=_sha256("rtk"),
                ),
            },
        ),
        _outdated(
            "build.stages.fd-prebuilt.fd",
            "github-release",
            "v10.4.2",
            "v10.5.0",
            artifacts={
                "linux-amd64": CandidateArtifact(
                    platform="linux-amd64",
                    name="fd_10.5.0_amd64.deb",
                    url="https://github.com/sharkdp/fd/releases/download/v10.5.0/fd_10.5.0_amd64.deb",
                    sha256=_sha256("fd"),
                ),
            },
        ),
        _outdated("build.stages.pi-tools.pi", "npm", "0.80.10", "0.81.0"),
        _outdated("build.stages.openspec-tools.openspec", "npm", "1.6.0", "1.7.0"),
        _outdated(
            "build.stages.runtime.oh-my-zsh",
            "git-ref",
            "70ad5e3df8f7bed68aa6672029496926e632aedd",
            "b" * 40,
            kind=UpdateKind.REVISION,
        ),
        _outdated(
            "runtime.pi-extensions.highlight-js",
            "npm",
            "10.7.2",
            "10.7.3",
            artifacts={
                "10.7.3": CandidateArtifact(
                    platform="10.7.3",
                    name="highlight.js",
                    url="https://registry.npmjs.org/highlight.js/-/highlight.js-10.7.3.tgz",
                    sha256=None,
                    integrity=_sri(b"h" * 64),
                ),
            },
        ),
        _outdated(
            "runtime.pi-extensions.pi-read",
            "npm",
            "0.2.0",
            "0.3.0",
            artifacts={
                "0.3.0": CandidateArtifact(
                    platform="0.3.0",
                    name="pi-read",
                    url="https://registry.npmjs.org/@arcanemachine/pi-read/-/pi-read-0.3.0.tgz",
                    sha256=None,
                    integrity=_sri(b"n" * 64),
                ),
            },
        ),
        _outdated(
            "runtime.pi-extensions.pi-tui-kit",
            "npm",
            "0.49.1",
            "0.49.3",
            artifacts={
                "0.49.3": CandidateArtifact(
                    platform="0.49.3",
                    name="pi-tui-kit",
                    url="https://registry.npmjs.org/@narumitw/pi-tui-kit/-/pi-tui-kit-0.49.3.tgz",
                    sha256=None,
                    integrity=_sri(b"t" * 64),
                ),
            },
        ),
        _outdated(
            "runtime.pi-extensions.pi-usage",
            "npm",
            "0.52.3",
            "0.53.0",
            artifacts={
                "0.53.0": CandidateArtifact(
                    platform="0.53.0",
                    name="pi-usage",
                    url="https://registry.npmjs.org/@narumitw/pi-usage/-/pi-usage-0.53.0.tgz",
                    sha256=None,
                    integrity=_sri(b"o" * 64),
                ),
            },
        ),
        _outdated(
            "runtime.pi-extensions.pi-proxy",
            "npm",
            "1.0.0",
            "1.1.0",
            artifacts={
                "1.1.0": CandidateArtifact(
                    platform="1.1.0",
                    name="pi-proxy",
                    url="https://registry.npmjs.org/pi-proxy/-/pi-proxy-1.1.0.tgz",
                    sha256=None,
                    integrity=_sri(b"p" * 64),
                ),
            },
        ),
    ]


def _deep_merge(dst: dict, src: dict) -> None:
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            _deep_merge(dst[key], value)
        else:
            dst[key] = copy.deepcopy(value)


def _delete_path(root: dict, segments: tuple[str, ...]) -> None:
    node = root
    for part in segments[:-1]:
        node = node[part]
    del node[segments[-1]]


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


class TestCanonicalFragmentRendering(unittest.TestCase):
    """2.1 — exact, complete, deterministic TOML fragment rendering."""

    def test_github_block_renders_exact_complete_toml(self) -> None:
        raw, _, targets = _raw_and_targets()
        new_url = (
            "https://github.com/astral-sh/uv/releases/download/0.2.0/"
            "uv-x86_64-unknown-linux-gnu.tar.gz"
        )
        new_sha = _sha256("uv")
        results = [
            _outdated(
                "build.stages.toolchain.uv",
                "github-release",
                "0.1.0",
                "0.2.0",
                artifacts={
                    "linux-amd64": CandidateArtifact(
                        platform="linux-amd64",
                        name="uv-x86_64-unknown-linux-gnu.tar.gz",
                        url=new_url,
                        sha256=new_sha,
                    ),
                },
            ),
        ]
        text = render_replacement_fragments(raw, targets, results)
        expected = (
            "# --- toolchain.uv ---\n"
            "[build.stages.toolchain.uv]\n"
            'version = "0.2.0"\n'
            "\n"
            "[build.stages.toolchain.uv.artifacts.linux-amd64]\n"
            f'sha256 = "{new_sha}"\n'
            f'url = "{new_url}"\n'
            "\n"
            "[build.stages.toolchain.uv.source]\n"
            'repository = "astral-sh/uv"\n'
            'tag = "0.2.0"\n'
            'type = "github-release"\n'
            "\n"
            "[build.stages.toolchain.uv.update]\n"
            'provider = "github-release"\n'
            'required_platforms = ["linux-amd64"]\n'
            "stable_only = true\n"
        )
        self.assertEqual(expected, text)
        # Raw inventory is never mutated.
        self.assertEqual(tomllib.loads(minimal_toml()), raw)

    def test_rendering_is_deterministic_and_retains_unchanged_fields(self) -> None:
        raw, _, targets = _raw_and_targets()
        results = [_outdated("build.stages.toolchain.ty", "pypi", "0.0.61", "0.0.62")]
        first = render_replacement_fragments(raw, targets, results)
        second = render_replacement_fragments(raw, targets, results)
        self.assertEqual(first, second)
        # Complete block: version overlaid, source/update retained.
        self.assertIn('[build.stages.toolchain.ty]\nversion = "0.0.62"', first)
        self.assertIn(
            '[build.stages.toolchain.ty.source]\npackage = "ty"\ntype = "pypi"', first
        )
        self.assertIn(
            '[build.stages.toolchain.ty.update]\nprovider = "pypi"\nstable_only = true',
            first,
        )

    def test_rust_block_renders_array_and_nested_rustup(self) -> None:
        raw, _, targets = _raw_and_targets()
        results = [
            _outdated("build.stages.toolchain.rust", "rust-channel", "1.0.0", "1.1.0")
        ]
        text = render_replacement_fragments(raw, targets, results)
        self.assertIn('components = ["rustfmt", "clippy"]', text)
        self.assertIn('profile = "minimal"', text)
        self.assertIn('version = "1.1.0"', text)
        # Nested rustup tables retained with full canonical paths.
        self.assertIn("[build.stages.toolchain.rust.rustup.source]", text)
        self.assertIn(
            "[build.stages.toolchain.rust.rustup.artifacts.linux-amd64]", text
        )

    def test_dotted_extension_name_is_quoted_and_round_trips(self) -> None:
        raw = tomllib.loads(minimal_toml() + _DOTTED_EXT_TOML)
        inventory = validate_inventory(raw)
        targets = build_update_targets(inventory)
        results = [
            _outdated(
                "runtime.pi-extensions.foo.bar",
                "npm",
                "1.0.0",
                "1.1.0",
                artifacts={
                    "1.1.0": CandidateArtifact(
                        platform="1.1.0",
                        name="foo.bar",
                        url="https://registry.npmjs.org/foo.bar/-/foo.bar-1.1.0.tgz",
                        sha256=None,
                        integrity=_sri(b"q" * 64),
                    ),
                },
            ),
        ]
        text = render_replacement_fragments(raw, targets, results)
        # Dotted extension name and version key are quoted TOML keys.
        self.assertIn('[runtime.pi-extensions."foo.bar"]', text)
        self.assertIn('[runtime.pi-extensions."foo.bar".artifacts."1.1.0"]', text)
        self.assertIn('version = "1.1.0"', text)

        ((owner, block),) = build_replacement_blocks(raw, targets, results)
        fragment = serialize_replacement_block(owner, block)
        merged = tomllib.loads(minimal_toml() + _DOTTED_EXT_TOML)
        _delete_path(merged, path_segments(owner))
        _deep_merge(merged, tomllib.loads(fragment))
        validate_inventory(merged)


class TestSharedRustBlock(unittest.TestCase):
    """2.2 — one complete fragment combining Rust + rustup, no duplicates."""

    def test_rust_and_rustup_combine_into_one_fragment(self) -> None:
        raw, _, targets = _raw_and_targets()
        rustup_sha = _sha256("rustup")
        results = [
            _outdated("build.stages.toolchain.rust", "rust-channel", "1.0.0", "1.1.0"),
            _outdated(
                "build.stages.toolchain.rust.rustup",
                "static-url",
                "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                rustup_sha,
                kind=UpdateKind.DIGEST_REFRESH,
                artifacts={
                    "linux-amd64": CandidateArtifact(
                        platform="linux-amd64",
                        name="rustup-init",
                        url="https://static.rust-lang.org/rustup/dist/x86_64-unknown-linux-gnu/rustup-init",
                        sha256=rustup_sha,
                    ),
                },
            ),
        ]
        blocks = build_replacement_blocks(raw, targets, results)
        self.assertEqual(len(blocks), 1)
        ((owner, block),) = blocks
        self.assertEqual(owner, "build.stages.toolchain.rust")
        self.assertEqual(block["version"], "1.1.0")
        self.assertEqual(
            block["rustup"]["artifacts"]["linux-amd64"]["sha256"],
            rustup_sha,
        )

        text = render_replacement_fragments(raw, targets, results)
        # Exactly one top-level Rust block header — no duplicate declarations.
        self.assertEqual(text.count("[build.stages.toolchain.rust]"), 1)
        self.assertIn('version = "1.1.0"', text)
        self.assertIn(f'sha256 = "{rustup_sha}"', text)


class TestReplacementRoundTrip(unittest.TestCase):
    """2.3 — each replacement block is accepted by the ordinary loader."""

    def test_every_replacement_block_round_trips(self) -> None:
        raw, _, targets = _raw_and_targets()
        results = _all_family_results()
        blocks = build_replacement_blocks(raw, targets, results)

        # Every target family produced a replacement block (14 targets map to
        # 13 owners — rust and rustup share one).
        owners = {owner for owner, _ in blocks}
        self.assertIn("build.stages.base.node", owners)
        self.assertIn("build.stages.toolchain.rust", owners)
        self.assertIn("build.stages.toolchain.uv", owners)
        self.assertIn("build.stages.toolchain.python", owners)
        self.assertIn("build.stages.toolchain.ty", owners)
        self.assertIn("build.stages.rtk-prebuilt.rtk", owners)
        self.assertIn("build.stages.fd-prebuilt.fd", owners)
        self.assertIn("build.stages.pi-tools.pi", owners)
        self.assertIn("build.stages.openspec-tools.openspec", owners)
        self.assertIn("build.stages.runtime.oh-my-zsh", owners)
        self.assertIn("runtime.pi-extensions.highlight-js", owners)
        self.assertIn("runtime.pi-extensions.pi-read", owners)
        self.assertIn("runtime.pi-extensions.pi-tui-kit", owners)
        self.assertIn("runtime.pi-extensions.pi-usage", owners)
        self.assertIn("runtime.pi-extensions.pi-proxy", owners)
        self.assertEqual(len(owners), 15)

        for owner, block in blocks:
            with self.subTest(owner=owner):
                fragment = serialize_replacement_block(owner, block)
                merged = tomllib.loads(minimal_toml())
                _delete_path(merged, path_segments(owner))
                _deep_merge(merged, tomllib.loads(fragment))
                validate_inventory(merged)  # must not raise

    def test_incomplete_npm_extension_emits_no_fragment(self) -> None:
        """An INCOMPLETE npm release never emits a version-only fragment."""
        raw, _, targets = _raw_and_targets()
        results = [
            UpdateResult(
                path="runtime.pi-extensions.pi-read",
                provider="npm",
                current="0.2.0",
                candidate="0.3.0",
                status=UpdateStatus.INCOMPLETE,
                kind=UpdateKind.VERSION,
                applicable=False,
                reason="missing npm integrity",
                artifacts={},
            ),
        ]
        blocks = build_replacement_blocks(raw, targets, results)
        self.assertEqual(blocks, ())
        text = render_replacement_fragments(raw, targets, results)
        self.assertEqual(text, "")
        self.assertNotIn("0.3.0", text)


class TestMultiPlatformRetention(unittest.TestCase):
    """2.4 — updating one artifact keeps untouched configured platforms."""

    def _multi_platform_raw(self):
        extra = """

[build.stages.toolchain.uv.artifacts.linux-arm64]
url = "https://github.com/astral-sh/uv/releases/download/0.1.0/uv-aarch64-unknown-linux-gnu.tar.gz"
sha256 = "%s"
""" % _sha256("uv-arm64")
        return tomllib.loads(minimal_toml() + extra)

    def test_update_one_platform_retains_other(self) -> None:
        raw = self._multi_platform_raw()
        inventory = validate_inventory(raw)
        targets = build_update_targets(inventory)
        results = [
            _outdated(
                "build.stages.toolchain.uv",
                "github-release",
                "0.1.0",
                "0.2.0",
                artifacts={
                    "linux-amd64": CandidateArtifact(
                        platform="linux-amd64",
                        name="uv-x86_64-unknown-linux-gnu.tar.gz",
                        url="https://github.com/astral-sh/uv/releases/download/0.2.0/uv-x86_64-unknown-linux-gnu.tar.gz",
                        sha256=_sha256("uv"),
                    ),
                },
            ),
        ]
        blocks = dict(build_replacement_blocks(raw, targets, results))
        uv = blocks["build.stages.toolchain.uv"]
        self.assertIn("linux-amd64", uv["artifacts"])
        self.assertIn("linux-arm64", uv["artifacts"])
        # Updated platform reflects the candidate; untouched platform retained.
        self.assertEqual(uv["artifacts"]["linux-amd64"]["sha256"], _sha256("uv"))
        self.assertEqual(
            uv["artifacts"]["linux-arm64"]["sha256"],
            _sha256("uv-arm64"),
        )

        text = render_replacement_fragments(raw, targets, results)
        self.assertIn("[build.stages.toolchain.uv.artifacts.linux-amd64]", text)
        self.assertIn("[build.stages.toolchain.uv.artifacts.linux-arm64]", text)


if __name__ == "__main__":
    unittest.main()

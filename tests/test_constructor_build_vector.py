"""Focused renderer tests for Stage 6.1 — Build command-vector rendering.

Tests ``render_build_vector`` against ``BuildRenderInputs``.
No Docker, no network, no subprocess, no filesystem.
"""

from __future__ import annotations

import unittest

from docker.versioning.errors import EffectiveConfigError
from docker.versioning.model import (
    EffectiveArtifact,
    EffectiveBuildProjection,
    EffectiveNode,
    EffectivePiRelease,
    EffectiveRust,
    EffectiveTool,
)
from docker.versioning.rendering import (
    BuildRenderInputs,
    CacheControls,
    Materialized,
    NoDerivedEnvironment,
    render_build_vector,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _artifact(
    url: str = "https://example.com/pkg-1.0.0.tar.gz",
    sha256: str = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
) -> EffectiveArtifact:
    return EffectiveArtifact(url=url, sha256=sha256)


def _projection(
    *,
    platform: str = "linux-amd64",
    node_image: str = "docker.io/library/node:22@sha256:ab"
    "cdef1234567890abcdef1234567890abcdef12"
    "34567890abcdef1234567890",
    rust_version: str = "1.85.0",
    rust_profile: str = "minimal",
    rust_components: tuple[str, ...] = ("rustfmt", "clippy"),
    rustup: EffectiveArtifact | None = None,
    uv_version: str = "0.6.14",
    uv_artifact: EffectiveArtifact | None = None,
    python_version: str = "3.14.6",
    ty_version: str = "1.0.0",
    rtk_version: str = "0.1.0",
    rtk_artifact: EffectiveArtifact | None = None,
    fd_version: str = "0.1.0",
    fd_artifact: EffectiveArtifact | None = None,
    pi_version: str = "1.0.0",
    openspec_version: str = "1.0.0",
    oh_my_zsh_revision: str = "abc123def456",
) -> EffectiveBuildProjection:
    if rustup is None:
        rustup = _artifact(
            url="https://static.rust-lang.org/rustup/dist/x86_64-unknown-linux-gnu/rustup-init",
            sha256="rustup000000000000000000000000000000000000000000000000000000000000",
        )
    if uv_artifact is None:
        uv_artifact = _artifact(
            url="https://github.com/astral-sh/uv/releases/download/0.6.14/uv-x86_64-unknown-linux-gnu.tar.gz",
            sha256="uv00000000000000000000000000000000000000000000000000000000000000",
        )
    if rtk_artifact is None:
        rtk_artifact = _artifact(
            url="https://github.com/example/rtk/releases/download/v0.1.0/rtk-linux-amd64.tar.gz",
            sha256="rtk000000000000000000000000000000000000000000000000000000000000",
        )
    if fd_artifact is None:
        fd_artifact = _artifact(
            url="https://github.com/example/fd/releases/download/v0.1.0/fd-linux-amd64.tar.gz",
            sha256="fd0000000000000000000000000000000000000000000000000000000000000",
        )
    return EffectiveBuildProjection(
        platform=platform,
        node=EffectiveNode(
            image=node_image,
            node_version="24.18.0",
            npm_version="11.16.0",
        ),
        rust=EffectiveRust(
            version=rust_version,
            profile=rust_profile,
            components=rust_components,
            rustup=rustup,
        ),
        uv=EffectiveTool(version=uv_version, artifact=uv_artifact),
        python_version=python_version,
        ty_version=ty_version,
        rtk=EffectiveTool(version=rtk_version, artifact=rtk_artifact),
        fd=EffectiveTool(version=fd_version, artifact=fd_artifact),
        pi_version=pi_version,
        pi_release=EffectivePiRelease(
            package="@earendil-works/pi-coding-agent",
            release_repository="earendil-works/pi",
            release_tag_prefix="v",
        ),
        openspec_version=openspec_version,
        oh_my_zsh_revision=oh_my_zsh_revision,
    )


def _render(
    *,
    build_context: str = ".",
    projection: EffectiveBuildProjection | None = None,
    target_stage: str = "runtime",
    image_tag: str = "pi-cli-pi:latest",
    platform: str = "linux/amd64",
    cache: CacheControls | None = None,
    pull: bool = False,
    progress: str = "auto",
    dockerfile: str | None = None,
    dev_uid: int = 1000,
    dev_gid: int = 1000,
) -> tuple[str, ...]:
    if projection is None:
        projection = _projection()
    if cache is None:
        cache = CacheControls()
    return render_build_vector(
        BuildRenderInputs(
            build_context=build_context,
            projection=projection,
            target_stage=target_stage,
            image_tag=image_tag,
            platform=platform,
            cache=cache,
            pull=pull,
            progress=progress,
            dockerfile=dockerfile,
            dev_uid=dev_uid,
            dev_gid=dev_gid,
            named_context=Materialized(
                "/tmp/constructor-artifacts", NoDerivedEnvironment()
            ),
        )
    )


def _arg_pairs(args: tuple[str, ...]) -> dict[str, str]:
    """Extract ``--build-arg KEY=VALUE`` pairs into a plain dict."""
    it = iter(args)
    pairs: dict[str, str] = {}
    for token in it:
        if token == "--build-arg":
            raw = next(it)
            key, _, value = raw.partition("=")
            pairs[key] = value
    return pairs


# ---------------------------------------------------------------------------
# 6.1.1  Default build vector shape
# ---------------------------------------------------------------------------


class TestDefaultBuildVector(unittest.TestCase):
    """Assert the exact tuple shape for a default direct build."""

    def test_command_prefix_is_docker_build(self):
        args = _render()
        self.assertEqual(
            args[:2],
            ("docker", "build"),
            "prefix must be 'docker build', never 'docker compose'",
        )

    def test_target_is_runtime(self):
        args = _render(target_stage="runtime")
        idx = args.index("--target")
        self.assertEqual(args[idx + 1], "runtime")

    def test_canonical_image_tag_is_pi_cli_pi_latest(self):
        """Default build vector must preserve the canonical
        pi-cli-pi:latest tag — not pi:latest or any other name."""
        args = _render()
        idx = args.index("--tag")
        self.assertEqual(args[idx + 1], "pi-cli-pi:latest")

    def test_explicit_tag_override_replaces_default(self):
        args = _render(image_tag="pi:2025.07.24")
        idx = args.index("--tag")
        self.assertEqual(args[idx + 1], "pi:2025.07.24")

    def test_platform_flag(self):
        proj = _projection(
            platform="linux-arm64",
            rustup=_artifact(
                url="https://rustup.example.com/aarch64",
                sha256="arm64rustup0000000000000000000000000000000000000000000000000000",
            ),
            uv_artifact=_artifact(
                url="https://uv.example.com/uv-aarch64.tar.gz",
                sha256="uvaarch640000000000000000000000000000000000000000000000000000",
            ),
            rtk_artifact=_artifact(
                url="https://rtk.example.com/rtk-arm64.tar.gz",
                sha256="rtkarm640000000000000000000000000000000000000000000000000000",
            ),
            fd_artifact=_artifact(
                url="https://fd.example.com/fd-arm64.tar.gz",
                sha256="fdarm6400000000000000000000000000000000000000000000000000000",
            ),
        )
        args = _render(projection=proj, platform="linux/arm64")
        idx = args.index("--platform")
        self.assertEqual(args[idx + 1], "linux/arm64")

    def test_progress_defaults_to_auto(self):
        args = _render()
        idx = args.index("--progress")
        self.assertEqual(args[idx + 1], "auto")

    def test_final_arg_is_build_context(self):
        args = _render(build_context="/home/user/repo")
        self.assertEqual(args[-1], "/home/user/repo")

    def test_no_compose_in_output(self):
        args = _render()
        self.assertNotIn("compose", args)
        self.assertNotIn("docker-compose", args)

    def test_every_arg_is_separate_string(self):
        """Each CLI token is a standalone string in the tuple — no
        shell concatenation like ``'--build-arg FOO=bar'`` smuggled
        into a single element."""
        args = _render()
        for token in args:
            self.assertIsInstance(token, str)
        # Flags and build-args must not be fused: '--build-arg KEY=VALUE'
        # ships as two consecutive elements, not one joined string.
        for i, token in enumerate(args):
            if token in (
                "--tag",
                "--target",
                "--platform",
                "--progress",
                "--file",
                "--build-arg",
            ):
                self.assertLess(i + 1, len(args), f"flag {token!r} at {i} has no value")
                self.assertNotEqual(
                    args[i + 1][:2],
                    "--",
                    f"flag {token!r} followed by another "
                    f"flag {args[i + 1]!r}, not a value",
                )


# ---------------------------------------------------------------------------
# 6.1.2  Deterministic flag placement
# ---------------------------------------------------------------------------


class TestFlagPlacement(unittest.TestCase):
    """Assert deterministic placement of flags in the argument vector."""

    def test_flags_before_build_args(self):
        """All --tag, --target, --platform, --progress, --file must
        appear before the first --build-arg."""
        args = _render(dockerfile="docker/Dockerfile")
        first_build_arg = args.index("--build-arg")
        flag_indices = [
            i
            for i, a in enumerate(args)
            if a in ("--tag", "--target", "--platform", "--progress", "--file")
        ]
        for fi in flag_indices:
            self.assertLess(
                fi,
                first_build_arg,
                f"flag {args[fi]!r} at {fi} must precede "
                f"first --build-arg at {first_build_arg}",
            )

    def test_build_args_before_context(self):
        args = _render()
        last_build_arg = max(i for i, a in enumerate(args) if a == "--build-arg")
        self.assertLess(
            last_build_arg + 1,
            len(args) - 1,
            "last --build-arg VALUE pair must precede context",
        )

    def test_file_flag_when_explicit(self):
        args = _render(dockerfile="docker/Dockerfile")
        self.assertIn("--file", args)
        file_idx = args.index("--file")
        self.assertEqual(args[file_idx + 1], "docker/Dockerfile")

    def test_no_file_flag_when_default(self):
        args = _render()
        self.assertNotIn("--file", args)


# ---------------------------------------------------------------------------
# 6.1.3  Build-arg mapping from EffectiveBuildProjection
# ---------------------------------------------------------------------------


class TestBuildArgMapping(unittest.TestCase):
    """Every projection field maps to its corresponding --build-arg."""

    def test_node_image_mapped(self):
        proj = _projection(
            node_image="docker.io/myreg/node:22@sha256:abc123",
        )
        pairs = _arg_pairs(_render(projection=proj))
        self.assertEqual(
            pairs["NODE_BASE_IMAGE"], "docker.io/myreg/node:22@sha256:abc123"
        )

    def test_rust_version_profile_components_mapped(self):
        proj = _projection(
            rust_version="1.86.0",
            rust_profile="default",
            rust_components=("rustfmt",),
        )
        pairs = _arg_pairs(_render(projection=proj))
        self.assertEqual(pairs["RUST_VERSION"], "1.86.0")
        self.assertEqual(pairs["RUST_PROFILE"], "default")
        self.assertEqual(pairs["RUST_COMPONENTS"], "rustfmt")

    def test_rustup_url_and_checksum_mapped(self):
        rustup = _artifact(
            url="https://rustup.example.com/rustup-arm64",
            sha256="arm64rustup0000000000000000000000000000000000000000000000000000",
        )
        proj = _projection(rustup=rustup)
        pairs = _arg_pairs(_render(projection=proj))
        self.assertNotIn("RUSTUP_URL", pairs)
        self.assertEqual(
            pairs["RUSTUP_SHA256"],
            "arm64rustup0000000000000000000000000000000000000000000000000000",
        )

    def test_uv_version_and_artifact_mapped(self):
        uv = _artifact(
            url="https://uv.example.com/uv-0.7.0.tar.gz",
            sha256="uv070000000000000000000000000000000000000000000000000000000000",
        )
        proj = _projection(uv_version="0.7.0", uv_artifact=uv)
        pairs = _arg_pairs(_render(projection=proj))
        self.assertEqual(pairs["UV_VERSION"], "0.7.0")
        self.assertNotIn("UV_URL", pairs)
        self.assertEqual(
            pairs["UV_SHA256"],
            "uv070000000000000000000000000000000000000000000000000000000000",
        )

    def test_python_version_mapped(self):
        proj = _projection(python_version="3.15.0")
        pairs = _arg_pairs(_render(projection=proj))
        self.assertEqual(pairs["PYTHON_VERSION"], "3.15.0")

    def test_ty_version_mapped(self):
        proj = _projection(ty_version="2.0.0")
        pairs = _arg_pairs(_render(projection=proj))
        self.assertEqual(pairs["TY_VERSION"], "2.0.0")

    def test_rtk_version_and_artifact_mapped(self):
        rtk = _artifact(
            url="https://rtk.example.com/rtk-0.2.0.tar.gz",
            sha256="rtk020000000000000000000000000000000000000000000000000000000000",
        )
        proj = _projection(rtk_version="0.2.0", rtk_artifact=rtk)
        pairs = _arg_pairs(_render(projection=proj))
        self.assertEqual(pairs["RTK_VERSION"], "0.2.0")
        self.assertNotIn("RTK_URL", pairs)
        self.assertEqual(
            pairs["RTK_SHA256"],
            "rtk020000000000000000000000000000000000000000000000000000000000",
        )

    def test_fd_version_and_artifact_mapped(self):
        fd = _artifact(
            url="https://fd.example.com/fd-0.2.0.tar.gz",
            sha256="fd0200000000000000000000000000000000000000000000000000000000000",
        )
        proj = _projection(fd_version="0.2.0", fd_artifact=fd)
        pairs = _arg_pairs(_render(projection=proj))
        self.assertEqual(pairs["FD_VERSION"], "0.2.0")
        self.assertNotIn("FD_URL", pairs)
        self.assertEqual(
            pairs["FD_SHA256"],
            "fd0200000000000000000000000000000000000000000000000000000000000",
        )

    def test_pi_version_mapped(self):
        proj = _projection(pi_version="2.0.0")
        pairs = _arg_pairs(_render(projection=proj))
        self.assertEqual(pairs["PI_VERSION"], "2.0.0")

    def test_openspec_version_mapped(self):
        proj = _projection(openspec_version="2.0.0")
        pairs = _arg_pairs(_render(projection=proj))
        self.assertEqual(pairs["OPENSPEC_VERSION"], "2.0.0")

    def test_oh_my_zsh_revision_mapped(self):
        proj = _projection(oh_my_zsh_revision="def789abc")
        pairs = _arg_pairs(_render(projection=proj))
        self.assertEqual(pairs["OH_MY_ZSH_VERSION"], "def789abc")

    def test_all_args_present_in_default(self):
        """Every expected ARG name appears in the default output."""
        args = _render()
        pairs = _arg_pairs(args)
        expected = {
            "NODE_BASE_IMAGE",
            "RUST_VERSION",
            "RUST_PROFILE",
            "RUST_COMPONENTS",
            "RUSTUP_SHA256",
            "UV_VERSION",
            "UV_SHA256",
            "PYTHON_VERSION",
            "TY_VERSION",
            "RTK_VERSION",
            "RTK_SHA256",
            "FD_VERSION",
            "FD_SHA256",
            "PI_VERSION",
            "OPENSPEC_VERSION",
            "OH_MY_ZSH_VERSION",
            "DEV_UID",
            "DEV_GID",
        }
        self.assertEqual(set(pairs.keys()), expected)


# ---------------------------------------------------------------------------
# 6.1.4  Python override isolation
# ---------------------------------------------------------------------------


class TestPythonOverrideIsolation(unittest.TestCase):
    """An overridden Python projection changes only PYTHON_VERSION."""

    def test_python_override_changes_only_python_version(self):
        base = _projection(python_version="3.14.6")
        overridden = _projection(python_version="3.15.0")
        base_pairs = _arg_pairs(_render(projection=base))
        ovr_pairs = _arg_pairs(_render(projection=overridden))

        self.assertNotEqual(base_pairs["PYTHON_VERSION"], ovr_pairs["PYTHON_VERSION"])
        # Every other arg must be identical
        for key in base_pairs:
            if key == "PYTHON_VERSION":
                continue
            self.assertEqual(
                base_pairs[key],
                ovr_pairs[key],
                f"{key} differs between Python overrides",
            )


# ---------------------------------------------------------------------------
# 6.1.5  Platform-specific rendering
# ---------------------------------------------------------------------------


class TestPlatformRendering(unittest.TestCase):
    """AMD64 and ARM64 projections produce matching Docker platform
    and artifact arguments."""

    def test_amd64_platform_string_is_linux_amd64(self):
        proj = _projection(platform="linux-amd64")
        args = _render(projection=proj, platform="linux/amd64")
        idx = args.index("--platform")
        self.assertEqual(args[idx + 1], "linux/amd64")

    def test_arm64_platform_string_is_linux_arm64(self):
        proj = _projection(
            platform="linux-arm64",
            rustup=_artifact(
                url="https://rustup.example.com/aarch64",
                sha256="arm64rustup0000000000000000000000000000000000000000000000000000",
            ),
            uv_artifact=_artifact(
                url="https://uv.example.com/uv-aarch64.tar.gz",
                sha256="uvaarch640000000000000000000000000000000000000000000000000000",
            ),
            rtk_artifact=_artifact(
                url="https://rtk.example.com/rtk-arm64.tar.gz",
                sha256="rtkarm640000000000000000000000000000000000000000000000000000",
            ),
            fd_artifact=_artifact(
                url="https://fd.example.com/fd-arm64.tar.gz",
                sha256="fdarm6400000000000000000000000000000000000000000000000000000",
            ),
        )
        args = _render(projection=proj, platform="linux/arm64")
        idx = args.index("--platform")
        self.assertEqual(args[idx + 1], "linux/arm64")

    def test_arm64_artifacts_differ_from_amd64(self):
        amd64 = _projection(platform="linux-amd64")
        arm64 = _projection(
            platform="linux-arm64",
            rustup=_artifact(
                url="https://rustup.example.com/aarch64",
                sha256="arm64rustup0000000000000000000000000000000000000000000000000000",
            ),
            uv_artifact=_artifact(
                url="https://uv.example.com/uv-aarch64.tar.gz",
                sha256="uvaarch640000000000000000000000000000000000000000000000000000",
            ),
            rtk_artifact=_artifact(
                url="https://rtk.example.com/rtk-arm64.tar.gz",
                sha256="rtkarm640000000000000000000000000000000000000000000000000000",
            ),
            fd_artifact=_artifact(
                url="https://fd.example.com/fd-arm64.tar.gz",
                sha256="fdarm6400000000000000000000000000000000000000000000000000000",
            ),
        )
        a_pairs = _arg_pairs(_render(projection=amd64, platform="linux/amd64"))
        b_pairs = _arg_pairs(_render(projection=arm64, platform="linux/arm64"))

        for key in ("RUSTUP_SHA256", "UV_SHA256", "RTK_SHA256", "FD_SHA256"):
            self.assertNotEqual(
                a_pairs[key], b_pairs[key], f"{key} must differ between platforms"
            )

    def test_platform_mismatch_rejected(self):
        """Command-platform 'linux/arm64' with projection-platform
        'linux-amd64' must raise EffectiveConfigError."""
        proj = _projection(platform="linux-amd64")
        with self.assertRaises(EffectiveConfigError):
            _render(projection=proj, platform="linux/arm64")


# ---------------------------------------------------------------------------
# 6.1.6  Missing / empty required values
# ---------------------------------------------------------------------------


class TestMissingOrEmptyValues(unittest.TestCase):
    """Reject missing or empty required projection values."""

    def test_empty_node_image_rejected(self):
        proj = _projection(node_image="")
        with self.assertRaises(EffectiveConfigError):
            _render(projection=proj)

    def test_empty_rust_version_rejected(self):
        proj = _projection(rust_version="")
        with self.assertRaises(EffectiveConfigError):
            _render(projection=proj)

    def test_empty_rust_profile_rejected(self):
        proj = _projection(rust_profile="")
        with self.assertRaises(EffectiveConfigError):
            _render(projection=proj)

    def test_empty_rust_components_rejected(self):
        proj = _projection(rust_components=())
        with self.assertRaises(EffectiveConfigError):
            _render(projection=proj)

    def test_empty_uv_version_rejected(self):
        proj = _projection(uv_version="")
        with self.assertRaises(EffectiveConfigError):
            _render(projection=proj)

    def test_empty_ty_version_rejected(self):
        proj = _projection(ty_version="")
        with self.assertRaises(EffectiveConfigError):
            _render(projection=proj)

    def test_empty_rtk_version_rejected(self):
        proj = _projection(rtk_version="")
        with self.assertRaises(EffectiveConfigError):
            _render(projection=proj)

    def test_empty_fd_version_rejected(self):
        proj = _projection(fd_version="")
        with self.assertRaises(EffectiveConfigError):
            _render(projection=proj)

    def test_empty_pi_version_rejected(self):
        proj = _projection(pi_version="")
        with self.assertRaises(EffectiveConfigError):
            _render(projection=proj)

    def test_empty_openspec_version_rejected(self):
        proj = _projection(openspec_version="")
        with self.assertRaises(EffectiveConfigError):
            _render(projection=proj)

    def test_empty_oh_my_zsh_revision_rejected(self):
        proj = _projection(oh_my_zsh_revision="")
        with self.assertRaises(EffectiveConfigError):
            _render(projection=proj)

    def test_empty_python_version_rejected(self):
        proj = _projection(python_version="")
        with self.assertRaises(EffectiveConfigError):
            _render(projection=proj)

    def test_empty_artifact_url_rejected(self):
        bad = _artifact(
            url="",
            sha256="abc12300000000000000000000000000000000000000000000000000000000",
        )
        proj = _projection(uv_artifact=bad)
        with self.assertRaises(EffectiveConfigError):
            _render(projection=proj)

    def test_empty_artifact_sha256_rejected(self):
        bad = _artifact(url="https://example.com/pkg.tar.gz", sha256="")
        proj = _projection(rtk_artifact=bad)
        with self.assertRaises(EffectiveConfigError):
            _render(projection=proj)


# ---------------------------------------------------------------------------
# 6.1.7  Cache, pull, and progress controls
# ---------------------------------------------------------------------------


class TestCachePullProgressOptions(unittest.TestCase):
    """Assert cache enabled/disabled, pull enabled, and progress modes
    produce correct flags."""

    def test_cache_enabled_default(self):
        """Default cache → no --no-cache in output."""
        args = _render(cache=CacheControls(enabled=True))
        self.assertNotIn("--no-cache", args)

    def test_cache_disabled(self):
        args = _render(cache=CacheControls(enabled=False))
        self.assertIn("--no-cache", args)

    def test_pull_enabled(self):
        args = _render(pull=True)
        self.assertIn("--pull", args)

    def test_pull_disabled_by_default(self):
        args = _render()
        self.assertNotIn("--pull", args)

    def test_progress_auto(self):
        args = _render(progress="auto")
        idx = args.index("--progress")
        self.assertEqual(args[idx + 1], "auto")

    def test_progress_plain(self):
        args = _render(progress="plain")
        idx = args.index("--progress")
        self.assertEqual(args[idx + 1], "plain")

    def test_progress_tty(self):
        args = _render(progress="tty")
        idx = args.index("--progress")
        self.assertEqual(args[idx + 1], "tty")


# ---------------------------------------------------------------------------
# 6.1.8  Paths with spaces are separate, unsplit arguments
# ---------------------------------------------------------------------------


class TestPathWithSpaces(unittest.TestCase):
    """Every argument is a separate string; paths containing spaces
    are not split or quoted."""

    def test_build_context_with_spaces_is_single_arg(self):
        ctx = "path with spaces/my project"
        args = _render(build_context=ctx)
        self.assertIn(ctx, args)
        self.assertEqual(args[-1], ctx)

    def test_dockerfile_path_with_spaces(self):
        df = "docker/sub dir/Dockerfile"
        args = _render(dockerfile=df)
        self.assertIn(df, args)
        file_idx = args.index("--file")
        self.assertEqual(args[file_idx + 1], df)


# ---------------------------------------------------------------------------
# 6.1.9  Deterministic byte-for-byte identical output
# ---------------------------------------------------------------------------


class TestDeterministicOutput(unittest.TestCase):
    """Equivalent inputs produce byte-for-byte identical vectors."""

    def test_same_inputs_produce_identical_vector(self):
        proj = _projection()
        bi = BuildRenderInputs(
            build_context=".",
            projection=proj,
            target_stage="runtime",
            image_tag="pi-cli-pi:latest",
            platform="linux/amd64",
            named_context=Materialized(
                "/tmp/constructor-artifacts", NoDerivedEnvironment()
            ),
        )
        a = render_build_vector(bi)
        b = render_build_vector(bi)
        self.assertEqual(a, b)
        # Verify byte-for-byte via hash
        self.assertEqual(hash(a), hash(b))

    def test_different_order_of_creation_same_result(self):
        """Building the input model with fields in different order
        (all kwargs) yields identical output."""
        proj = _projection()
        a = render_build_vector(
            BuildRenderInputs(
                build_context=".",
                projection=proj,
                target_stage="runtime",
                image_tag="pi-cli-pi:latest",
                platform="linux/amd64",
                named_context=Materialized(
                    "/tmp/constructor-artifacts", NoDerivedEnvironment()
                ),
            )
        )
        b = render_build_vector(
            BuildRenderInputs(
                platform="linux/amd64",
                image_tag="pi-cli-pi:latest",
                target_stage="runtime",
                projection=proj,
                build_context=".",
                named_context=Materialized(
                    "/tmp/constructor-artifacts", NoDerivedEnvironment()
                ),
            )
        )
        self.assertEqual(a, b)


# ---------------------------------------------------------------------------
# 6.3  Edge-case tests (build-specific)
# ---------------------------------------------------------------------------


class TestBuildPathEdgeCases(unittest.TestCase):
    """Paths with spaces, Unicode, and leading dashes in build inputs."""

    def test_build_context_with_spaces_preserved(self):
        ctx = "path with spaces/my project"
        args = _render(build_context=ctx)
        self.assertEqual(args[-1], ctx)

    def test_build_context_with_unicode(self):
        ctx = "/home/dev/work/projéct-α"
        args = _render(build_context=ctx)
        self.assertEqual(args[-1], ctx)

    def test_build_context_with_leading_dash(self):
        ctx = "/home/dev/--my-repo"
        args = _render(build_context=ctx)
        self.assertEqual(args[-1], ctx)

    def test_dockerfile_path_with_unicode(self):
        df = "docker/nível/Dockerfile"
        args = _render(dockerfile=df)
        idx = args.index("--file")
        self.assertEqual(args[idx + 1], df)


class TestBuildImageValidation(unittest.TestCase):
    """Reject invalid image tags."""

    def test_empty_image_tag_rejected(self):
        with self.assertRaises(ValueError):
            _render(image_tag="")

    def test_whitespace_image_tag_rejected(self):
        with self.assertRaises(ValueError):
            _render(image_tag="   ")


class TestBuildInputValidation(unittest.TestCase):
    """Reject invalid build renderer inputs that are not tied to
    the projection."""

    def test_empty_target_stage_rejected(self):
        with self.assertRaises(ValueError):
            _render(target_stage="")

    def test_whitespace_target_stage_rejected(self):
        with self.assertRaises(ValueError):
            _render(target_stage="   ")

    def test_empty_build_context_rejected(self):
        with self.assertRaises(ValueError):
            _render(build_context="")

    def test_whitespace_build_context_rejected(self):
        with self.assertRaises(ValueError):
            _render(build_context="   ")

    def test_unsupported_progress_mode_rejected(self):
        with self.assertRaises(ValueError):
            _render(progress="fancy")

    def test_progress_auto_accepted(self):
        _render(progress="auto")  # must not raise

    def test_progress_plain_accepted(self):
        _render(progress="plain")  # must not raise

    def test_progress_tty_accepted(self):
        _render(progress="tty")  # must not raise


class TestBuildArgumentAtomicity(unittest.TestCase):
    """No argument may contain shell-joined text."""

    def test_no_flag_equals_value_fused(self):
        args = _render()
        for token in args:
            if token.startswith("--"):
                self.assertNotIn("=", token, f"flag {token!r} must not embed its value")

    def test_every_build_arg_is_adjacent_pair(self):
        """Each ``--build-arg`` token must be immediately followed
        by a single ``KEY=VALUE`` string — two elements, never fused
        into one."""
        args = _render()
        for i, token in enumerate(args):
            if token == "--build-arg":
                self.assertLess(
                    i + 1, len(args), f"--build-arg at {i} has no successor"
                )
                val = args[i + 1]
                # The successor must be a plain KEY=VALUE string, not
                # another flag.
                self.assertFalse(
                    val.startswith("--"), f"--build-arg followed by flag {val!r}"
                )
                self.assertIn("=", val, f"--build-arg value {val!r} missing '='")

    def test_build_arg_with_spaces_remains_one_element(self):
        """Values containing spaces (e.g. RUST_COMPONENTS listings)
        must stay as a single KEY=VALUE element — spaces inside the
        value are not argument separators."""
        projection = _projection(
            rust_components=("rls", "rust-analysis", "rustfmt"),
        )
        args = _render(projection=projection)
        # Walk through all --build-arg pairs to find RUST_COMPONENTS.
        rust_components_val = None
        for i, token in enumerate(args):
            if token == "--build-arg" and args[i + 1].startswith("RUST_COMPONENTS="):
                rust_components_val = args[i + 1]
                break
        self.assertIsNotNone(rust_components_val, "RUST_COMPONENTS build arg not found")
        self.assertEqual(
            rust_components_val,
            "RUST_COMPONENTS=rls rust-analysis rustfmt",
        )
        # The value is one element — verify by indexing.
        rc_idx = args.index(rust_components_val)
        self.assertEqual(args[rc_idx - 1], "--build-arg")
        # The spaces inside the value did not split it into multiple args.
        self.assertEqual(
            tuple(args[rc_idx : rc_idx + 1]),
            ("RUST_COMPONENTS=rls rust-analysis rustfmt",),
        )


class TestDevUidGid(unittest.TestCase):
    """DEV_UID and DEV_GID are emitted as explicit build args with
    sensible defaults, not derived from the projection."""

    def test_default_dev_uid_gid(self):
        args = _render()
        pairs = _arg_pairs(args)
        self.assertEqual(pairs["DEV_UID"], "1000")
        self.assertEqual(pairs["DEV_GID"], "1000")

    def test_custom_dev_uid(self):
        args = _render(dev_uid=501)
        pairs = _arg_pairs(args)
        self.assertEqual(pairs["DEV_UID"], "501")
        self.assertEqual(pairs["DEV_GID"], "1000")

    def test_custom_dev_gid(self):
        args = _render(dev_gid=20)
        pairs = _arg_pairs(args)
        self.assertEqual(pairs["DEV_UID"], "1000")
        self.assertEqual(pairs["DEV_GID"], "20")

    def test_custom_dev_uid_and_gid(self):
        args = _render(dev_uid=501, dev_gid=20)
        pairs = _arg_pairs(args)
        self.assertEqual(pairs["DEV_UID"], "501")
        self.assertEqual(pairs["DEV_GID"], "20")

    def test_dev_uid_after_projection_args(self):
        """DEV_UID must appear after the last projection-derived
        --build-arg and before the build context."""
        args = _render()
        oh_my_idx = None
        dev_uid_idx = None
        for i, token in enumerate(args):
            if token == "--build-arg" and args[i + 1].startswith("OH_MY_ZSH_VERSION="):
                oh_my_idx = i
            if token == "--build-arg" and args[i + 1].startswith("DEV_UID="):
                dev_uid_idx = i
        self.assertIsNotNone(oh_my_idx)
        self.assertIsNotNone(dev_uid_idx)
        self.assertLess(
            oh_my_idx, dev_uid_idx, "DEV_UID must come after projection build args"
        )

    def test_negative_dev_uid_rejected(self):
        with self.assertRaises(ValueError):
            _render(dev_uid=-1)

    def test_negative_dev_gid_rejected(self):
        with self.assertRaises(ValueError):
            _render(dev_gid=-1)

    # ── 6.6 architectural guards ──────────────────────────────────

    def test_result_is_always_tuple(self):
        """The renderer returns ``tuple[str, ...]`` — never a list,
        never a string, never None."""
        result = _render()
        self.assertIsInstance(result, tuple)
        self.assertTrue(all(isinstance(t, str) for t in result))

    def test_environment_does_not_affect_output(self):
        """Setting arbitrary environment variables MUST NOT change the
        rendered vector.  The renderer reads only its typed inputs."""
        import os as _os

        baseline = _render()
        saved = {}
        for k in (
            "COMPOSE_FILE",
            "DOCKER_HOST",
            "PI_COMPOSE_PROJECT",
            "DOCKER_BUILDKIT",
            "BUILDKIT_PROGRESS",
        ):
            saved[k] = _os.environ.get(k)
            _os.environ[k] = f"injected-{k}-value"
        try:
            self.assertEqual(_render(), baseline)
        finally:
            for k, v in saved.items():
                if v is None:
                    _os.environ.pop(k, None)
                else:
                    _os.environ[k] = v

    def test_vector_never_contains_compose(self):
        """Guarantee that the new renderer does not produce
        ``docker compose`` — that is the legacy path."""
        result = _render()
        self.assertNotIn("compose", result)
        self.assertEqual(result[0], "docker")
        self.assertEqual(result[1], "build")

    # ── 6.7 projection-origin tests ───────────────────────────────

    def test_every_build_arg_traces_to_effective_build_projection(self):
        """Every ``_BUILD_ARG_ORDER`` entry must resolve to a real
        ``EffectiveBuildProjection`` field — no orphaned or
        hardcoded arg names."""
        from docker.versioning.rendering import (
            _BUILD_ARG_ORDER,
            _resolve_build_arg,
        )

        proj = _projection()
        for arg_name, dotted_path in _BUILD_ARG_ORDER:
            try:
                value = _resolve_build_arg(proj, dotted_path)
            except AttributeError:
                self.fail(
                    f"_BUILD_ARG_ORDER entry {arg_name!r} maps to "
                    f"{dotted_path!r} — not a field on "
                    f"EffectiveBuildProjection"
                )
            self.assertIsInstance(
                value, str, f"{arg_name!r} ({dotted_path}) did not resolve to str"
            )
            self.assertNotEqual(
                value, "", f"{arg_name!r} ({dotted_path}) resolved to empty string"
            )

    def test_no_concrete_dependency_version_in_renderer(self):
        """If a projection field is empty the renderer must raise,
        not silently substitute a hardcoded default version."""
        for label, make_proj in (
            ("node.image", lambda: _projection(node_image=" ")),
            ("rust.version", lambda: _projection(rust_version=" ")),
            ("python_version", lambda: _projection(python_version=" ")),
            ("pi_version", lambda: _projection(pi_version=" ")),
            ("openspec_version", lambda: _projection(openspec_version=" ")),
            ("oh_my_zsh_revision", lambda: _projection(oh_my_zsh_revision=" ")),
            ("uv_version", lambda: _projection(uv_version=" ")),
            ("ty_version", lambda: _projection(ty_version=" ")),
            ("rtk_version", lambda: _projection(rtk_version=" ")),
            ("fd_version", lambda: _projection(fd_version=" ")),
        ):
            with self.subTest(field=label):
                with self.assertRaises(
                    EffectiveConfigError,
                    msg=f"Empty {label} must raise, not fall back to a default",
                ):
                    _render(projection=make_proj())


# end of red-phase tests

"""RED tests for host-side build verification (Stage 12, task 12.1).

Verifies that the internal verification API compares container-side
observations against host-side effective build expectations without
injecting build metadata into the container.

All tests use fake process runners — no Docker required.
"""

from __future__ import annotations

import io
import re
import tempfile
import tomllib
import unittest
from pathlib import Path
from typing import Sequence

from docker.versioning.effective import (
    resolve_build_projection,
)
from docker.versioning.inventory import load_inventory
from docker.versioning.model import (
    EffectiveArtifact,
    EffectiveBuildProjection,
    EffectiveNode,
    EffectivePiRelease,
    EffectiveRust,
    EffectiveTool,
)
from docker.versioning.rendering import (
    _write_toml,
    serialize_effective_build,
    validate_effective_build,
)
from docker.versioning.verification import (
    _CONTRACTS,
    BuildObservation,
    BuildVerificationResult,
    ObservationContract,
    VerifyBuildRequest,
    _applicable_contracts,
    _extract_expected_value,
    verify_build,
)
from docker.versioning.verification import (
    ProcessResult as VProcessResult,
)

# ═══════════════════════════════════════════════════════════════════════
# Canonical effective build projection — built from model types and
# validated through the maintained serializer / validator pipeline.
# ═══════════════════════════════════════════════════════════════════════


def _canonical_projection() -> EffectiveBuildProjection:
    """Return the project-wide canonical effective build projection.

    Every field maps 1:1 to the observation contracts in
    :data:`docker.versioning.verification._CONTRACTS`.
    """
    return EffectiveBuildProjection(
        platform="linux-amd64",
        node=EffectiveNode(
            image="node:22.11.0-bookworm-slim",
            node_version="22.11.0",
            npm_version="10.9.0",
        ),
        rust=EffectiveRust(
            version="1.83.0",
            profile="minimal",
            components=("clippy", "rustfmt"),
            rustup=EffectiveArtifact(
                url="https://static.rust-lang.org/rustup/archive/1.27.1/"
                "x86_64-unknown-linux-gnu/rustup-init",
                sha256="6a20b2c8a3945a3a5a8d6c2e9e4a0ce4a20f3be3710957477c5353b6b3d28f5d",
            ),
        ),
        uv=EffectiveTool(
            version="0.6.17",
            artifact=EffectiveArtifact(
                url="https://github.com/astral-sh/uv/releases/download/0.6.17/"
                "uv-aarch64-unknown-linux-gnu.tar.gz",
                sha256="fab4a20b2c8e3945a3a5a8d6c2e9e4a0ce4a20f3be3710957477c5353b6b3d28f5",
            ),
        ),
        python_version="3.14.0",
        ty_version="v0.9.0",
        rtk=EffectiveTool(
            version="0.1.29",
            artifact=EffectiveArtifact(
                url="https://github.com/vaibhav-patel/rtk/releases/download/"
                "v0.1.29/rtk-v0.1.29-aarch64-unknown-linux-gnu.tar.gz",
                sha256="cbb4a20b2c8e3945a3a5a8d6c2e9e4a0ce4a20f3be3710957477c5353b6b3d28f5",
            ),
        ),
        fd=EffectiveTool(
            version="10.1.0",
            artifact=EffectiveArtifact(
                url="https://github.com/sharkdp/fd/releases/download/"
                "v10.1.0/fd-v10.1.0-aarch64-unknown-linux-gnu.tar.gz",
                sha256="dbb4a20b2c8e3945a3a5a8d6c2e9e4a0ce4a20f3be3710957477c5353b6b3d28f5",
            ),
        ),
        pi_version="v1.4.236",
        pi_release=EffectivePiRelease(
            package="@earendil-works/pi-coding-agent",
            release_repository="earendil-works/pi",
            release_tag_prefix="v",
        ),
        openspec_version="v0.15.0",
        oh_my_zsh_revision="eea3ac1a6802f0d8a778447413b9b52a14decb40",
    )


def _serialize_projection_to_toml(proj: EffectiveBuildProjection) -> str:
    """Serialize *proj* to a TOML string through the maintained pipeline.

    1. ``serialize_effective_build`` — typed → plain dict
    2. ``validate_effective_build`` — schema check
    3. ``_write_toml`` — dict → TOML string
    """
    data = serialize_effective_build(proj)
    validate_effective_build(data)
    buf = io.StringIO()
    _write_toml(buf, data)
    return buf.getvalue()


def _write_projection_fixture(proj: EffectiveBuildProjection | None = None) -> Path:
    """Write *proj* (or the canonical projection) as a validated TOML file.

    Returns the path to the temporary file.
    """
    if proj is None:
        proj = _canonical_projection()
    toml_str = _serialize_projection_to_toml(proj)
    fd, path = tempfile.mkstemp(suffix=".toml", prefix="test-eff-build-")
    import os

    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(toml_str)
    return Path(path)


def _canonical_projection_dict() -> dict:
    """Return the serialized canonical projection as a plain dict."""
    from docker.versioning.rendering import serialize_effective_build

    return serialize_effective_build(_canonical_projection())


def _fixture_is_valid_round_trip(path: Path) -> None:
    """Parse *path* with ``tomllib`` and validate — proving the written
    file is a valid effective build projection."""
    raw = path.read_text(encoding="utf-8")
    data = tomllib.loads(raw)
    validate_effective_build(data)


# ═══════════════════════════════════════════════════════════════════════
# Fake process runner — dispatches by tool name found in argv
# ═══════════════════════════════════════════════════════════════════════


class ToolVersionRunner:
    """Returns canned version output for *tool* names found in argv.
    Scans every arg; the first one present in *versions* wins.
    Unrecognised tools get exit-code 1 and ``"no version known"`` on
    stderr so tests can prove unexpected commands are never issued.
    """

    def __init__(
        self,
        versions: dict[str, str] | None = None,
        *,
        return_code: int = 0,
        stderr: str = "",
    ) -> None:
        self._versions = dict(versions or {})
        self._return_code = return_code
        self._stderr = stderr
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: Sequence[str]) -> VProcessResult:
        t = tuple(argv)
        self.calls.append(t)
        # Dispatch by the tool name: for ``--version`` commands use
        # the arg immediately before ``--version``; for non-version
        # commands scan every arg for a known tool.
        if "--version" in t:
            idx = t.index("--version")
            tool = t[idx - 1] if idx > 0 else ""
        else:
            tool = ""
            for arg in t:
                if arg in self._versions:
                    tool = arg
                    break
            if not tool and t and t[0] in self._versions:
                tool = t[0]
        if tool in self._versions:
            return VProcessResult(
                argv=t,
                return_code=self._return_code,
                stdout=self._versions[tool],
                stderr=self._stderr,
            )
        return VProcessResult(
            argv=t,
            return_code=1,
            stdout="",
            stderr="no version known for: " + ", ".join(argv),
        )


# ═══════════════════════════════════════════════════════════════════════
# Container-side version outputs — realistic tool --version strings
# ═══════════════════════════════════════════════════════════════════════

_MATCHING: dict[str, str] = {
    "node": "v22.11.0\n",
    "rustc": "rustc 1.83.0 (90b35a623 2024-11-26)\n",
    "cargo": "cargo 1.83.0 (5ffa321 2024-11-26)\n",
    "rustfmt": "rustfmt 1.83.0-stable (abc1234 2024-11-26)\n",
    "clippy": "clippy 0.1.83 (abc1234 2024-11-26)\n",
    "uv": "uv 0.6.17\n",
    "python3": "Python 3.14.0\n",
    "ty": "ty v0.9.0\n",
    "rtk": "rtk 0.1.29\n",
    "fd": "fd 10.1.0\n",
    "pi": "v1.4.236\n",
    "openspec": "openspec v0.15.0\n",
    "git": "eea3ac1a6802f0d8a778447413b9b52a14decb40\n",
    "rustup": (
        "/home/dev/.rustup/toolchains/1.83.0-x86_64-unknown"
        "-linux-gnu/bin/rustfmt\n"
        "rustfmt-x86_64-unknown-linux-gnu (installed)\n"
    ),
}

_MISMATCHED: dict[str, str] = {
    **_MATCHING,
    "node": "v18.0.0\n",
}

# ── Per-tool normalization helpers used by the tests to prove the
#    API applies the right rule, not to re-implement it.


def _normalize_node(raw: str) -> str:
    """Strip leading ``v``/``V`` then trailing whitespace."""
    s = raw.strip()
    if s.startswith(("v", "V")):
        s = s[1:]
    return s


def _extract_first_version_token(raw: str) -> str:
    """Return the first ``X.Y.Z`` token, stripping trailing newline."""
    m = re.search(r"[0-9]+\.[0-9]+\.[0-9]+", raw)
    return m.group(0).strip() if m else raw.strip()


def _normalize_v_stripped(raw: str) -> str:
    """Extract first ``X.Y.Z`` token, then strip leading ``v``/``V``."""
    tok = _extract_first_version_token(raw)
    if tok.startswith(("v", "V")) and len(tok) > 1:
        tok = tok[1:]
    return tok


def _normalize_component_status(raw: str) -> str:
    """Parse ``rustup component list`` output and return
    ``"installed"`` or ``"not-installed"`` for the rustfmt entry."""
    for line in raw.splitlines():
        line = line.strip()
        if "rustfmt" in line and "(installed)" in line:
            return "installed"
    return "not-installed"


_RUSTUP_TOOLCHAIN_PATH_RE = re.compile(
    r"\.rustup/toolchains/(\d+\.\d+\.\d+)-[^/]+/bin/rustfmt(?:\.exe)?"
)


def _normalize_rustup_toolchain_version(raw: str) -> str:
    """Extract the toolchain version from a rustup which path
    matching the ``…/.rustup/toolchains/<ver>-<target>/bin/rustfmt``
    layout.  Falls back to the raw stripped value for non-matching
    paths so they cannot coincidentally match a version."""
    s = raw.strip()
    m = _RUSTUP_TOOLCHAIN_PATH_RE.search(s)
    return m.group(1) if m else s


_NORMALIZERS: dict[str, object] = {
    "node.version": _normalize_node,
    "rust.version": _extract_first_version_token,
    "rust.cargo": _extract_first_version_token,
    "rust.rustfmt": _normalize_rustup_toolchain_version,
    "rust.rustfmt.component": _normalize_component_status,
    "rust.clippy": _extract_first_version_token,
    "uv.version": _extract_first_version_token,
    "python.version": _extract_first_version_token,
    "ty.version": _normalize_v_stripped,
    "rtk.version": _extract_first_version_token,
    "fd.version": _extract_first_version_token,
    "pi.version": _normalize_node,
    "openspec.version": _normalize_v_stripped,
    "oh-my-zsh.revision": lambda r: r.strip(),
}

# Map contract key → _MATCHING tool name (command_suffix[0] isn't
# always the tool name — e.g. rust.clippy uses "cargo clippy").
_CONTRACT_TO_TOOL: dict[str, str] = {
    "node.version": "node",
    "rust.version": "rustc",
    "rust.cargo": "cargo",
    "rust.rustfmt": "rustup",
    "rust.rustfmt.component": "rustup",
    "rust.clippy": "clippy",
    "uv.version": "uv",
    "python.version": "python3",
    "ty.version": "ty",
    "rtk.version": "rtk",
    "fd.version": "fd",
    "pi.version": "pi",
    "openspec.version": "openspec",
    "oh-my-zsh.revision": "git",
}


# ═══════════════════════════════════════════════════════════════════════
# Tests
# ═══════════════════════════════════════════════════════════════════════


class TestBuildVerification(unittest.TestCase):
    """verify_build compares container observations with host-side
    effective build expectations without injecting build metadata
    into the container."""

    # ── Stub-state gate ────────────────────────────────────────────

    def test_verify_build_is_not_implemented(self) -> None:
        """Gate test — verify_build is now live (no longer a stub)."""
        tf = _write_projection_fixture()
        req = VerifyBuildRequest(
            image="pi-cli-pi:latest",
            effective_projection_path=tf,
            runner=ToolVersionRunner(),
        )
        # verify_build returns a real result now.
        result = verify_build(req)
        self.assertIsInstance(result, BuildVerificationResult)

    # ── Behavioral RED tests ───────────────────────────────────────

    def test_observation_matches_effective_projection(self) -> None:
        """Every observation is ok — normalized container output
        matches the projection-derived expected_value."""
        IMG = "pi-cli-pi:latest"
        tf = _write_projection_fixture()
        _fixture_is_valid_round_trip(tf)  # prove the fixture is real
        proj_dict = _canonical_projection_dict()
        runner = ToolVersionRunner(_MATCHING)
        result = verify_build(
            VerifyBuildRequest(
                image=IMG,
                effective_projection_path=tf,
                runner=runner,
            )
        )

        self.assertTrue(result.all_ok)
        self.assertEqual((), result.errors)

        obs_by_key = {o.key: o for o in result.observations}
        applicable = _applicable_contracts(proj_dict)
        contract_by_key = {c.key: c for c in applicable}
        self.assertEqual(
            set(contract_by_key),
            set(obs_by_key),
            "every applicable contract must produce an observation",
        )

        for contract in applicable:
            obs = obs_by_key[contract.key]
            expected = _extract_expected_value(contract.key, proj_dict)
            with self.subTest(key=contract.key):
                expected_cmd = ("docker", "run", "--rm", IMG, *contract.command_suffix)
                self.assertEqual(expected_cmd, obs.command)
                self.assertEqual(expected, obs.expected_value)
                self.assertTrue(obs.ok)
                self.assertIsNotNone(obs.observed_value)

                # Prove normalization was applied correctly.
                tool = _CONTRACT_TO_TOOL[contract.key]
                raw = _MATCHING[tool]
                norm = _NORMALIZERS[contract.key](raw)
                self.assertEqual(expected, norm)

    def test_observation_mismatch_detected(self) -> None:
        """A single mismatch makes that observation not-ok and
        result.all_ok False, while other observations stay ok."""
        IMG = "pi-cli-pi:latest"
        tf = _write_projection_fixture()
        runner = ToolVersionRunner(_MISMATCHED)
        result = verify_build(
            VerifyBuildRequest(
                image=IMG,
                effective_projection_path=tf,
                runner=runner,
            )
        )

        self.assertFalse(result.all_ok)
        obs_by_key = {o.key: o for o in result.observations}

        proj_dict = _canonical_projection_dict()
        expected_node = _extract_expected_value("node.version", proj_dict)

        node_obs = obs_by_key["node.version"]
        self.assertFalse(node_obs.ok)
        self.assertEqual(expected_node, node_obs.expected_value)
        self.assertIn("18.0.0", node_obs.observed_value or "")

        for contract in _applicable_contracts(proj_dict):
            if contract.key == "node.version":
                continue
            self.assertTrue(obs_by_key[contract.key].ok)

    def test_effective_projection_not_mounted_in_container(self) -> None:
        """The effective build projection path never appears in any
        docker argument — no --mount, -v, or docker cp."""
        IMG = "pi-cli-pi:latest"
        tf = _write_projection_fixture()
        runner = ToolVersionRunner(_MATCHING)
        verify_build(
            VerifyBuildRequest(
                image=IMG,
                effective_projection_path=tf,
                runner=runner,
            )
        )

        self.assertTrue(len(runner.calls) > 0)
        tf_str = str(tf)
        for call in runner.calls:
            joined = " ".join(call)
            self.assertNotIn("--mount", joined)
            # ``-v`` must not be a *standalone argument* (it appears
            # inside ``--version`` flags in every command).
            self.assertNotIn("-v", call)
            self.assertNotIn(tf_str, joined)
            self.assertNotIn("cp", call)

    def test_missing_effective_projection_file(self) -> None:
        """Missing file → errors, zero observations."""
        missing = Path("/nonexistent/build.effective.toml")
        assert not missing.exists()
        result = verify_build(
            VerifyBuildRequest(
                image="img",
                effective_projection_path=missing,
                runner=ToolVersionRunner(),
            )
        )
        self.assertFalse(result.all_ok)
        self.assertEqual((), result.observations)
        self.assertTrue(len(result.errors) > 0)

    def test_corrupted_effective_projection_file(self) -> None:
        """Unparseable TOML → errors, zero observations."""
        tf = _write_projection_fixture()
        tf.write_text("}}} not valid toml {{{[[[")
        result = verify_build(
            VerifyBuildRequest(
                image="img",
                effective_projection_path=tf,
                runner=ToolVersionRunner(),
            )
        )
        self.assertFalse(result.all_ok)
        self.assertEqual((), result.observations)
        self.assertTrue(len(result.errors) > 0)

    def test_docker_execution_failure(self) -> None:
        """Non-zero docker exit → at least one observation not-ok."""
        tf = _write_projection_fixture()
        runner = ToolVersionRunner(
            _MATCHING,
            return_code=125,
            stderr="No such image: pi-cli-pi:latest\n",
        )
        result = verify_build(
            VerifyBuildRequest(
                image="pi-cli-pi:latest",
                effective_projection_path=tf,
                runner=runner,
            )
        )
        self.assertFalse(result.all_ok)
        self.assertTrue(
            any(not o.ok for o in result.observations),
            "at least one observation must be not-ok",
        )

    def test_all_observations_run_for_complete_projection(self) -> None:
        """Every applicable contract produces exactly one observation,
        in the deterministic _CONTRACTS subsequence order."""
        IMG = "pi-cli-pi:latest"
        tf = _write_projection_fixture()
        result = verify_build(
            VerifyBuildRequest(
                image=IMG,
                effective_projection_path=tf,
                runner=ToolVersionRunner(_MATCHING),
            )
        )
        obs_keys = tuple(o.key for o in result.observations)
        proj_dict = _canonical_projection_dict()
        contract_keys = tuple(c.key for c in _applicable_contracts(proj_dict))
        self.assertEqual(contract_keys, obs_keys)

    def test_result_structure_is_deterministic(self) -> None:
        """Two identical requests → same observation keys, same order."""
        IMG = "pi-cli-pi:latest"
        tf = _write_projection_fixture()

        def _run():
            return verify_build(
                VerifyBuildRequest(
                    image=IMG,
                    effective_projection_path=tf,
                    runner=ToolVersionRunner(_MATCHING),
                )
            )

        r1, r2 = _run(), _run()
        self.assertEqual(
            tuple(o.key for o in r1.observations),
            tuple(o.key for o in r2.observations),
        )

    def test_observation_keys_map_to_projection_sections(self) -> None:
        """Each observation expected_value is derived from the
        corresponding section of the effective build projection."""
        IMG = "pi-cli-pi:latest"
        tf = _write_projection_fixture()
        result = verify_build(
            VerifyBuildRequest(
                image=IMG,
                effective_projection_path=tf,
                runner=ToolVersionRunner(_MATCHING),
            )
        )
        obs_by_key = {o.key: o for o in result.observations}
        proj_dict = _canonical_projection_dict()

        self.assertEqual(
            _extract_expected_value("node.version", proj_dict),
            obs_by_key["node.version"].expected_value,
        )
        self.assertEqual(
            _extract_expected_value("rust.version", proj_dict),
            obs_by_key["rust.version"].expected_value,
        )
        self.assertEqual(
            _extract_expected_value("rust.cargo", proj_dict),
            obs_by_key["rust.cargo"].expected_value,
        )
        self.assertEqual(
            _extract_expected_value("rust.rustfmt", proj_dict),
            obs_by_key["rust.rustfmt"].expected_value,
        )
        self.assertEqual(
            _extract_expected_value("rust.clippy", proj_dict),
            obs_by_key["rust.clippy"].expected_value,
        )
        self.assertEqual(
            _extract_expected_value("uv.version", proj_dict),
            obs_by_key["uv.version"].expected_value,
        )
        self.assertEqual(
            _extract_expected_value("python.version", proj_dict),
            obs_by_key["python.version"].expected_value,
        )
        self.assertEqual(
            _extract_expected_value("ty.version", proj_dict),
            obs_by_key["ty.version"].expected_value,
        )
        self.assertEqual(
            _extract_expected_value("rtk.version", proj_dict),
            obs_by_key["rtk.version"].expected_value,
        )
        self.assertEqual(
            _extract_expected_value("fd.version", proj_dict),
            obs_by_key["fd.version"].expected_value,
        )
        self.assertEqual(
            _extract_expected_value("pi.version", proj_dict),
            obs_by_key["pi.version"].expected_value,
        )
        self.assertEqual(
            _extract_expected_value("openspec.version", proj_dict),
            obs_by_key["openspec.version"].expected_value,
        )
        self.assertEqual(
            _extract_expected_value("oh-my-zsh.revision", proj_dict),
            obs_by_key["oh-my-zsh.revision"].expected_value,
        )


class TestAlteredProjection(unittest.TestCase):
    """Prove that expected values are derived from the actual loaded
    projection, not from hard-coded contract values."""

    def _altered_projection(self) -> EffectiveBuildProjection:
        """Return a projection with deliberately different versions
        from the canonical one, including a digest-qualified Node image."""
        base = _canonical_projection()
        return EffectiveBuildProjection(
            platform=base.platform,
            node=EffectiveNode(
                image="node:18.19.0-bookworm-slim"
                "@sha256:abc123def4567890011223344556677889900aabbcc",
                node_version="18.19.0",
                npm_version="10.2.4",
            ),
            rust=EffectiveRust(
                version="1.75.0",
                profile=base.rust.profile,
                components=base.rust.components,
                rustup=base.rust.rustup,
            ),
            uv=EffectiveTool(
                version="0.5.0",
                artifact=base.uv.artifact,
            ),
            python_version=base.python_version,
            ty_version=base.ty_version,
            rtk=base.rtk,
            fd=base.fd,
            pi_version=base.pi_version,
            pi_release=base.pi_release,
            openspec_version=base.openspec_version,
            oh_my_zsh_revision="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        )

    def _altered_matching(self) -> dict[str, str]:
        """Container outputs that match the altered projection."""
        return {
            **_MATCHING,
            "node": "v18.19.0\n",
            "rustc": "rustc 1.75.0 (90b35a623 2024-11-26)\n",
            "cargo": "cargo 1.75.0 (5ffa321 2024-11-26)\n",
            "rustup": (
                "/home/dev/.rustup/toolchains/1.75.0-x86_64-unknown"
                "-linux-gnu/bin/rustfmt\n"
                "rustfmt-x86_64-unknown-linux-gnu (installed)\n"
            ),
            "clippy": "clippy 0.1.75 (abc1234 2024-11-26)\n",
            "uv": "uv 0.5.0\n",
            "git": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\n",
        }

    def test_altered_projection_changes_expected_values(self) -> None:
        """When the projection changes, observations use the new
        expected values — no hard-coded contract leakage."""
        IMG = "pi-cli-pi:latest"
        altered = self._altered_projection()
        tf = _write_projection_fixture(altered)
        runner = ToolVersionRunner(self._altered_matching())
        result = verify_build(
            VerifyBuildRequest(
                image=IMG,
                effective_projection_path=tf,
                runner=runner,
            )
        )

        self.assertTrue(result.all_ok)
        obs_by_key = {o.key: o for o in result.observations}

        # Node: digest-qualified image → extract "18.19.0"
        self.assertEqual("18.19.0", obs_by_key["node.version"].expected_value)
        self.assertTrue(obs_by_key["node.version"].ok)

        # Rust family: 1.75.0
        self.assertEqual("1.75.0", obs_by_key["rust.version"].expected_value)
        self.assertEqual("1.75.0", obs_by_key["rust.cargo"].expected_value)
        self.assertEqual("1.75.0", obs_by_key["rust.rustfmt"].expected_value)
        # clippy: 0.1.{minor} → 0.1.75
        self.assertEqual("0.1.75", obs_by_key["rust.clippy"].expected_value)

        # uv: 0.5.0
        self.assertEqual("0.5.0", obs_by_key["uv.version"].expected_value)

        # Unchanged fields still match canonical
        self.assertEqual("3.14.0", obs_by_key["python.version"].expected_value)

        # oh-my-zsh revision changed
        self.assertEqual(
            "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
            obs_by_key["oh-my-zsh.revision"].expected_value,
        )

    def test_altered_projection_digest_qualified_node(self) -> None:
        """Node version is the reviewed ``node_version`` field, never the tag."""
        self.assertEqual(
            "18.19.0",
            _extract_expected_value(
                "node.version",
                {
                    "node": {
                        "image": "node:18.19.0-bookworm-slim@sha256:abc123def456",
                        "node_version": "18.19.0",
                        "npm_version": "10.2.4",
                    }
                },
            ),
        )

    def test_altered_projection_is_valid(self) -> None:
        """The altered projection serializes and validates without
        error — proving it is a legitimate fixture."""
        altered = self._altered_projection()
        toml_str = _serialize_projection_to_toml(altered)
        data = tomllib.loads(toml_str)
        validate_effective_build(data)


class TestConditionalComponents(unittest.TestCase):
    """Rust component observations (rustfmt, clippy) are conditional on
    ``projection["rust"]["components"]`` — undeclared components produce
    no command and no observation."""

    def _proj_with_components(self, *components: str) -> EffectiveBuildProjection:
        """Return the canonical projection with only the given Rust
        *components* declared."""
        base = _canonical_projection()
        return EffectiveBuildProjection(
            platform=base.platform,
            node=base.node,
            rust=EffectiveRust(
                version=base.rust.version,
                profile=base.rust.profile,
                components=components,
                rustup=base.rust.rustup,
            ),
            uv=base.uv,
            python_version=base.python_version,
            ty_version=base.ty_version,
            rtk=base.rtk,
            fd=base.fd,
            pi_version=base.pi_version,
            pi_release=base.pi_release,
            openspec_version=base.openspec_version,
            oh_my_zsh_revision=base.oh_my_zsh_revision,
        )

    # ── Unit tests (work against projection dict, no verify_build) ──

    def test_both_declared_includes_both(self) -> None:
        """When both components are declared, both contracts apply."""
        proj = self._proj_with_components("rustfmt", "clippy")
        proj_dict = serialize_effective_build(proj)
        contracts = _applicable_contracts(proj_dict)
        keys = {c.key for c in contracts}
        self.assertIn("rust.rustfmt", keys)
        self.assertIn("rust.clippy", keys)

    def test_only_rustfmt_includes_rustfmt_only(self) -> None:
        """When only rustfmt is declared, clippy contract is omitted."""
        proj = self._proj_with_components("rustfmt")
        proj_dict = serialize_effective_build(proj)
        contracts = _applicable_contracts(proj_dict)
        keys = {c.key for c in contracts}
        self.assertIn("rust.rustfmt", keys)
        self.assertIn("rust.rustfmt.component", keys)
        self.assertNotIn("rust.clippy", keys)
        # All non-Rust-component contracts plus the two dynamic
        # rustfmt contracts are present.
        all_keys = {c.key for c in _CONTRACTS}
        expected_extra = {"rust.rustfmt", "rust.rustfmt.component"}
        expected_excluded = {"rust.clippy"}
        self.assertEqual(
            (all_keys - expected_excluded) | expected_extra,
            keys,
        )

    def test_only_clippy_includes_clippy_only(self) -> None:
        """When only clippy is declared, rustfmt contract is omitted."""
        proj = self._proj_with_components("clippy")
        proj_dict = serialize_effective_build(proj)
        contracts = _applicable_contracts(proj_dict)
        keys = {c.key for c in contracts}
        self.assertNotIn("rust.rustfmt", keys)
        self.assertIn("rust.clippy", keys)
        all_keys = {c.key for c in _CONTRACTS}
        expected_excluded = {"rust.rustfmt"}
        self.assertEqual(all_keys - expected_excluded, keys)

    def test_neither_declared_excludes_both(self) -> None:
        """When no Rust components are declared, neither rustfmt nor
        clippy contracts apply."""
        proj = self._proj_with_components()
        proj_dict = serialize_effective_build(proj)
        contracts = _applicable_contracts(proj_dict)
        keys = {c.key for c in contracts}
        self.assertNotIn("rust.rustfmt", keys)
        self.assertNotIn("rust.clippy", keys)
        all_keys = {c.key for c in _CONTRACTS}
        expected_excluded = {"rust.rustfmt", "rust.clippy"}
        self.assertEqual(all_keys - expected_excluded, keys)

    def test_applicable_contracts_order_matches_conracts(self) -> None:
        """Applicable contracts preserve the deterministic _CONTRACTS
        order, with the two dynamic ``rust.rustfmt*`` contracts
        appended at the end."""
        proj = self._proj_with_components("rustfmt", "clippy")
        proj_dict = serialize_effective_build(proj)
        contracts = _applicable_contracts(proj_dict)
        full_keys = [c.key for c in _CONTRACTS]
        applicable_keys = [c.key for c in contracts]
        # The two dynamic contracts are appended after the static ones.
        dynamic_keys = {"rust.rustfmt", "rust.rustfmt.component"}
        static_keys = [k for k in applicable_keys if k not in dynamic_keys]
        # Static keys must be a subsequence of _CONTRACTS order.
        filtered = [k for k in full_keys if k in set(static_keys)]
        self.assertEqual(filtered, static_keys)
        # The dynamic contracts must be the last two keys, in order.
        self.assertEqual(
            "rust.rustfmt",
            applicable_keys[-2],
            "rust.rustfmt must be the second-to-last applicable contract",
        )
        self.assertEqual(
            "rust.rustfmt.component",
            applicable_keys[-1],
            "rust.rustfmt.component must be the last applicable contract",
        )

    # ── Behavioral RED tests ───────────────────────────────────────

    def test_rustfmt_only_projection_skips_clippy_command(self) -> None:
        """When the projection has only rustfmt, verify_build must not
        issue a cargo-clippy command."""
        IMG = "pi-cli-pi:latest"
        proj = self._proj_with_components("rustfmt")
        tf = _write_projection_fixture(proj)
        runner = ToolVersionRunner(_MATCHING)
        result = verify_build(
            VerifyBuildRequest(
                image=IMG,
                effective_projection_path=tf,
                runner=runner,
            )
        )
        obs_by_key = {o.key: o for o in result.observations}
        self.assertIn("rust.rustfmt", obs_by_key)
        self.assertNotIn("rust.clippy", obs_by_key)
        # No cargo-clippy command was issued
        for call in runner.calls:
            self.assertNotIn("clippy", call)

    def test_clippy_only_projection_skips_rustfmt_command(self) -> None:
        """When the projection has only clippy, verify_build must not
        issue a rustfmt command."""
        IMG = "pi-cli-pi:latest"
        proj = self._proj_with_components("clippy")
        tf = _write_projection_fixture(proj)
        runner = ToolVersionRunner(_MATCHING)
        result = verify_build(
            VerifyBuildRequest(
                image=IMG,
                effective_projection_path=tf,
                runner=runner,
            )
        )
        obs_by_key = {o.key: o for o in result.observations}
        self.assertNotIn("rust.rustfmt", obs_by_key)
        self.assertIn("rust.clippy", obs_by_key)
        for call in runner.calls:
            self.assertNotIn("rustfmt", call)

    def test_no_components_projection_skips_both(self) -> None:
        """When the projection has no Rust components, neither rustfmt
        nor clippy observations are produced."""
        IMG = "pi-cli-pi:latest"
        proj = self._proj_with_components()
        tf = _write_projection_fixture(proj)
        runner = ToolVersionRunner(_MATCHING)
        result = verify_build(
            VerifyBuildRequest(
                image=IMG,
                effective_projection_path=tf,
                runner=runner,
            )
        )
        obs_by_key = {o.key: o for o in result.observations}
        self.assertNotIn("rust.rustfmt", obs_by_key)
        self.assertNotIn("rust.clippy", obs_by_key)
        # rustc and cargo still present (they're always applicable)
        self.assertIn("rust.version", obs_by_key)
        self.assertIn("rust.cargo", obs_by_key)


class TestFixtureIntegrity(unittest.TestCase):
    """Prove the fixture is a valid effective build projection that
    survives the maintained serialize → validate → write → parse →
    validate round-trip."""

    def test_canonical_projection_is_valid(self) -> None:
        """The canonical projection serializes and validates without
        error — no hand-rolled TOML can drift from the schema."""
        proj = _canonical_projection()
        toml_str = _serialize_projection_to_toml(proj)
        # Round-trip: parse the TOML and re-validate
        data = tomllib.loads(toml_str)
        validate_effective_build(data)

    def test_written_fixture_is_valid_effective_build(self) -> None:
        """A fixture written to disk parses and validates successfully."""
        tf = _write_projection_fixture()
        _fixture_is_valid_round_trip(tf)

    def test_fixture_matches_contract_table(self) -> None:
        """Every _extract_expected_value result matches the canonical
        projection's fields — no drift between extraction logic and model."""
        proj_dict = _canonical_projection_dict()
        proj = _canonical_projection()

        # Spot-checks that prove extraction uses the right projection fields
        self.assertEqual("22.11.0", _extract_expected_value("node.version", proj_dict))
        self.assertEqual(
            proj.rust.version, _extract_expected_value("rust.version", proj_dict)
        )
        self.assertEqual(
            proj.rust.version, _extract_expected_value("rust.cargo", proj_dict)
        )
        self.assertEqual(
            proj.rust.version, _extract_expected_value("rust.rustfmt", proj_dict)
        )
        self.assertEqual(
            f"0.1.{proj.rust.version.split('.')[1]}",
            _extract_expected_value("rust.clippy", proj_dict),
        )
        self.assertEqual(
            proj.uv.version, _extract_expected_value("uv.version", proj_dict)
        )
        self.assertEqual(
            proj.python_version, _extract_expected_value("python.version", proj_dict)
        )
        self.assertEqual(
            proj.ty_version.lstrip("v"),
            _extract_expected_value("ty.version", proj_dict),
        )
        self.assertEqual(
            proj.rtk.version, _extract_expected_value("rtk.version", proj_dict)
        )
        self.assertEqual(
            proj.fd.version, _extract_expected_value("fd.version", proj_dict)
        )
        self.assertEqual(
            proj.pi_version.lstrip("v"),
            _extract_expected_value("pi.version", proj_dict),
        )
        self.assertEqual(
            proj.openspec_version.lstrip("v"),
            _extract_expected_value("openspec.version", proj_dict),
        )
        self.assertEqual(
            proj.oh_my_zsh_revision,
            _extract_expected_value("oh-my-zsh.revision", proj_dict),
        )

        # Every contract key must be extractable
        for c in _CONTRACTS:
            with self.subTest(key=c.key):
                val = _extract_expected_value(c.key, proj_dict)
                self.assertIsInstance(val, str)
                self.assertTrue(
                    len(val) > 0, f"{c.key}: expected_value must be non-empty"
                )


class TestObservationContracts(unittest.TestCase):
    """Verify that the _CONTRACTS table is internally consistent and
    covers every version-checkable section of the projection."""

    def test_contracts_are_in_deterministic_order(self) -> None:
        keys = tuple(c.key for c in _CONTRACTS)
        self.assertEqual(
            (
                "node.version",
                "rust.version",
                "rust.cargo",
                "rust.clippy",
                "uv.version",
                "python.version",
                "ty.version",
                "rtk.version",
                "fd.version",
                "pi.version",
                "openspec.version",
                "oh-my-zsh.revision",
            ),
            keys,
        )

    def test_contract_keys_are_unique(self) -> None:
        keys = [c.key for c in _CONTRACTS]
        self.assertEqual(len(keys), len(set(keys)))

    def test_contract_commands_are_pairs(self) -> None:
        for c in _CONTRACTS:
            with self.subTest(key=c.key):
                # oh-my-zsh uses git rev-parse, not --version
                if c.key == "oh-my-zsh.revision":
                    self.assertTrue(len(c.command_suffix) >= 4)
                    self.assertEqual("rev-parse", c.command_suffix[-2])
                    self.assertEqual("HEAD", c.command_suffix[-1])
                # rust.clippy uses cargo clippy --version (3 args)
                elif c.key == "rust.clippy":
                    self.assertEqual(3, len(c.command_suffix))
                    self.assertEqual("cargo", c.command_suffix[0])
                    self.assertEqual("--version", c.command_suffix[-1])
                else:
                    self.assertEqual(2, len(c.command_suffix))
                    self.assertTrue(c.command_suffix[0])
                    self.assertEqual("--version", c.command_suffix[1])

    def test_normalizers_cover_all_contracts(self) -> None:
        for c in _CONTRACTS:
            self.assertIn(c.key, _NORMALIZERS)

    def test_normalized_matching_outputs_match_contract_expected(self) -> None:
        proj_dict = _canonical_projection_dict()
        for c in _CONTRACTS:
            with self.subTest(key=c.key):
                tool = _CONTRACT_TO_TOOL[c.key]
                raw = _MATCHING[tool]
                norm = _NORMALIZERS[c.key](raw)
                expected = _extract_expected_value(c.key, proj_dict)
                self.assertEqual(expected, norm)

    def test_normalized_mismatched_node_differs_from_expected(self) -> None:
        raw = _MISMATCHED["node"]
        norm = _normalize_node(raw)
        self.assertEqual("18.0.0", norm)
        proj_dict = _canonical_projection_dict()
        expected_node = _extract_expected_value("node.version", proj_dict)
        self.assertNotEqual(expected_node, norm)

    # ── 1.1: Exact Node tag in inventory/effective projection ──────

    def test_extract_node_version_uses_reviewed_field_not_tag(self) -> None:
        """``node.version`` is derived from the reviewed ``node_version``
        field, never inferred from the image tag."""
        self.assertEqual(
            "24.18.0",
            _extract_expected_value(
                "node.version",
                {
                    "node": {
                        "image": "docker.io/library/node:24.18.0-trixie-slim"
                        "@sha256:ae91dcc111a68c9d2d81ff2a17bda61be126426176fde"
                        "6fe7d08ab13b7f50573",
                        "node_version": "24.18.0",
                        "npm_version": "11.16.0",
                    }
                },
            ),
        )

    def test_node_version_field_ignores_floating_tag(self) -> None:
        """A floating major-only tag still yields the reviewed node_version."""
        self.assertEqual(
            "24.18.0",
            _extract_expected_value(
                "node.version",
                {
                    "node": {
                        "image": "node:24-trixie-slim",
                        "node_version": "24.18.0",
                        "npm_version": "11.16.0",
                    }
                },
            ),
        )

    def test_inventory_pipeline_resolves_exact_node_image(self) -> None:
        """The actual ``docker-constructor.toml`` loaded through the
        maintained inventory→effective-projection pipeline must
        resolve the Node image to the exact ``24.18.0-trixie-slim``
        tag with the pinned digest."""
        repo_root = Path(__file__).resolve().parents[1]
        inv_path = repo_root / "docker-constructor.toml"
        self.assertTrue(inv_path.is_file(), f"inventory not found at {inv_path}")
        inv = load_inventory(inv_path)

        # The inventory tag is the exact semver the verifier must
        # extract; a floating tag would break exact-version
        # verification.
        node = inv.stages.base.node
        self.assertEqual(
            node.tag,
            "24.18.0-trixie-slim",
            "inventory must pin the exact Node semver tag",
        )
        # The digest is the immutable build authority.
        self.assertEqual(
            node.digest,
            "sha256:ae91dcc111a68c9d2d81ff2a17bda61be126426176fde6fe7d08ab13b7f50573",
            "inventory must retain the reviewed digest",
        )

        # The effective build projection is what the verifier reads
        # to derive expectations — it must carry the exact image.
        eff = resolve_build_projection(inv.build, {})
        self.assertIn(
            "24.18.0-trixie-slim@sha256:ae91dcc111a68c9d2d81ff2a17bd"
            "a61be126426176fde6fe7d08ab13b7f50573",
            eff.node.image,
            "effective build projection node.image must contain the "
            "exact tag@digest the verifier depends on",
        )

    # ── 1.2: Exact vs floating Node tag verification ──────────────

    def test_exact_node_tag_with_matching_version_passes(self) -> None:
        """When the effective projection pins an exact Node semver
        tag and ``node --version`` reports the same version,
        verification reports success."""
        proj = _canonical_projection()
        # Pin an exact semver tag.
        proj = EffectiveBuildProjection(
            platform=proj.platform,
            node=EffectiveNode(
                image="docker.io/library/node:24.18.0-trixie-slim"
                "@sha256:ae91dcc111a68c9d2d81ff2a17bda61be12642"
                "6176fde6fe7d08ab13b7f50573",
                node_version="24.18.0",
                npm_version="11.16.0",
            ),
            rust=proj.rust,
            uv=proj.uv,
            python_version=proj.python_version,
            ty_version=proj.ty_version,
            rtk=proj.rtk,
            fd=proj.fd,
            pi_version=proj.pi_version,
            pi_release=proj.pi_release,
            openspec_version=proj.openspec_version,
            oh_my_zsh_revision=proj.oh_my_zsh_revision,
        )
        tf = _write_projection_fixture(proj)
        # Matching output: node reports the exact version.
        matching = dict(_MATCHING)
        matching["node"] = "v24.18.0\n"
        runner = ToolVersionRunner(matching)
        result = verify_build(
            VerifyBuildRequest(
                image="pi-cli-pi:latest",
                effective_projection_path=tf,
                runner=runner,
            )
        )
        node_obs = {o.key: o for o in result.observations}["node.version"]
        self.assertTrue(
            node_obs.ok,
            f"exact tag must pass: expected={node_obs.expected_value!r} "
            f"observed={node_obs.observed_value!r}",
        )

    def test_floating_node_tag_uses_reviewed_node_version(self) -> None:
        """A floating major-only tag still verifies against the reviewed
        ``node_version`` field — the version is never inferred from the tag."""
        proj = _canonical_projection()
        proj = EffectiveBuildProjection(
            platform=proj.platform,
            node=EffectiveNode(
                image="docker.io/library/node:24-trixie-slim"
                "@sha256:ae91dcc111a68c9d2d81ff2a17bda61be12642"
                "6176fde6fe7d08ab13b7f50573",
                node_version="24.18.0",
                npm_version="11.16.0",
            ),
            rust=proj.rust,
            uv=proj.uv,
            python_version=proj.python_version,
            ty_version=proj.ty_version,
            rtk=proj.rtk,
            fd=proj.fd,
            pi_version=proj.pi_version,
            pi_release=proj.pi_release,
            openspec_version=proj.openspec_version,
            oh_my_zsh_revision=proj.oh_my_zsh_revision,
        )
        tf = _write_projection_fixture(proj)
        matching = dict(_MATCHING)
        matching["node"] = "v24.18.0\n"
        runner = ToolVersionRunner(matching)
        result = verify_build(
            VerifyBuildRequest(
                image="pi-cli-pi:latest",
                effective_projection_path=tf,
                runner=runner,
            )
        )
        node_obs = {o.key: o for o in result.observations}["node.version"]
        self.assertTrue(
            node_obs.ok,
            f"reviewed node_version must pass regardless of tag: "
            f"expected={node_obs.expected_value!r} "
            f"observed={node_obs.observed_value!r}",
        )

    # ── 1.3: rustfmt independent version RED ─────────────────────

    def test_rustfmt_provenance_passes_with_independent_version_banner(
        self,
    ) -> None:
        """When Rust 1.97.1 is configured with the rustfmt component,
        ``rustfmt --version`` correctly reports its own independent
        version (e.g. ``1.9.0-stable``).  Verification must pass
        because rustfmt provenance is determined by rustup toolchain
        membership, not by comparing rustfmt's banner to the Rust
        version."""
        proj = _canonical_projection()
        proj = EffectiveBuildProjection(
            platform=proj.platform,
            node=proj.node,
            rust=EffectiveRust(
                version="1.97.1",
                profile="minimal",
                components=("rustfmt",),
                rustup=proj.rust.rustup,
            ),
            uv=proj.uv,
            python_version=proj.python_version,
            ty_version=proj.ty_version,
            rtk=proj.rtk,
            fd=proj.fd,
            pi_version=proj.pi_version,
            pi_release=proj.pi_release,
            openspec_version=proj.openspec_version,
            oh_my_zsh_revision=proj.oh_my_zsh_revision,
        )
        tf = _write_projection_fixture(proj)
        # rustfmt 1.9.0-stable resolves through the 1.97.1 toolchain.
        matching = dict(_MATCHING)
        matching["rustc"] = "rustc 1.97.1 (8bab26f4f6 2025-01-15)\n"
        matching["cargo"] = "cargo 1.97.1 (8bab26f4f6 2025-01-15)\n"
        # rustup which rustfmt → path inside the 1.97.1 toolchain.
        matching["rustup"] = (
            "/home/dev/.rustup/toolchains/1.97.1-x86_64-unknown-linux-gnu/bin/rustfmt\n"
        )
        runner = ToolVersionRunner(matching)
        result = verify_build(
            VerifyBuildRequest(
                image="pi-cli-pi:latest",
                effective_projection_path=tf,
                runner=runner,
            )
        )
        obs_by_key = {o.key: o for o in result.observations}
        rustfmt_obs = obs_by_key["rust.rustfmt"]
        # GREEN: rustfmt provenance passes because the rustup
        # which path contains the configured 1.97.1 toolchain,
        # regardless of rustfmt's independent version banner.
        self.assertTrue(
            rustfmt_obs.ok,
            f"rustfmt 1.9.0-stable on Rust 1.97.1 must pass "
            f"provenance check — got expected={rustfmt_obs.expected_value!r} "
            f"observed={rustfmt_obs.observed_value!r}",
        )

    # ── 1.4: rustfmt provenance fixture RED tests ────────────────

    def _proj_rust_1_97_1_with_rustfmt(self) -> EffectiveBuildProjection:
        """Helper: canonical projection with Rust 1.97.1 and
        rustfmt component only."""
        proj = _canonical_projection()
        return EffectiveBuildProjection(
            platform=proj.platform,
            node=proj.node,
            rust=EffectiveRust(
                version="1.97.1",
                profile="minimal",
                components=("rustfmt",),
                rustup=proj.rust.rustup,
            ),
            uv=proj.uv,
            python_version=proj.python_version,
            ty_version=proj.ty_version,
            rtk=proj.rtk,
            fd=proj.fd,
            pi_version=proj.pi_version,
            pi_release=proj.pi_release,
            openspec_version=proj.openspec_version,
            oh_my_zsh_revision=proj.oh_my_zsh_revision,
        )

    def test_rustfmt_uninstalled_component_reported(self) -> None:
        """When rustfmt is in the effective component list but not
        actually installed for the configured toolchain, verification
        must fail with provenance details."""
        proj = self._proj_rust_1_97_1_with_rustfmt()
        tf = _write_projection_fixture(proj)
        # rustfmt is NOT managed by rustup — ``rustup which``
        # will fail with a non-zero exit code.
        matching = dict(_MATCHING)
        matching["rustc"] = "rustc 1.97.1 (8bab26f4f6 2025-01-15)\n"
        matching["cargo"] = "cargo 1.97.1 (8bab26f4f6 2025-01-15)\n"
        # No "rustup" key → exit 1, "no version known" on stderr.
        runner = ToolVersionRunner(matching)
        result = verify_build(
            VerifyBuildRequest(
                image="pi-cli-pi:latest",
                effective_projection_path=tf,
                runner=runner,
            )
        )
        obs_by_key = {o.key: o for o in result.observations}
        rustfmt_obs = obs_by_key["rust.rustfmt"]
        # RED: current code only compares version numbers —
        # it won't detect that the component isn't installed.
        self.assertFalse(
            rustfmt_obs.ok,
            "rustfmt not installed as a rustup component for the "
            "configured toolchain must be reported as a failure",
        )

    def test_rustfmt_outside_configured_toolchain(self) -> None:
        """When rustfmt resolves to a binary outside the configured
        rustup toolchain directory, verification must fail."""
        proj = self._proj_rust_1_97_1_with_rustfmt()
        tf = _write_projection_fixture(proj)
        matching = dict(_MATCHING)
        matching["rustc"] = "rustc 1.97.1 (8bab26f4f6 2025-01-15)\n"
        matching["cargo"] = "cargo 1.97.1 (8bab26f4f6 2025-01-15)\n"
        # rustup which returns a path outside the configured
        # toolchain directory.
        matching["rustup"] = "/usr/bin/rustfmt\n"
        runner = ToolVersionRunner(matching)
        result = verify_build(
            VerifyBuildRequest(
                image="pi-cli-pi:latest",
                effective_projection_path=tf,
                runner=runner,
            )
        )
        obs_by_key = {o.key: o for o in result.observations}
        rustfmt_obs = obs_by_key["rust.rustfmt"]
        # GREEN: the rustup which path does not contain 1.97.1,
        # so provenance fails even if version banners match.
        self.assertFalse(
            rustfmt_obs.ok,
            "rustfmt outside the configured rustup toolchain must "
            "be reported as a provenance failure — version matching "
            "is insufficient",
        )

    def test_rustfmt_path_outside_rustup_toolchain_dir(self) -> None:
        """When ``rustup which rustfmt`` returns a path that contains
        the configured version number but lies outside of rustup's
        ``toolchains`` directory (e.g. ``/opt/tools/1.97.1/bin/rustfmt``),
        provenance must fail.  Coincidental version-number matches are
        not a substitute for toolchain membership."""
        proj = self._proj_rust_1_97_1_with_rustfmt()
        tf = _write_projection_fixture(proj)
        matching = dict(_MATCHING)
        matching["rustc"] = "rustc 1.97.1 (8bab26f4f6 2025-01-15)\n"
        matching["cargo"] = "cargo 1.97.1 (8bab26f4f6 2025-01-15)\n"
        # Path contains 1.97.1 but is NOT inside .rustup/toolchains/.
        matching["rustup"] = "/opt/tools/1.97.1/bin/rustfmt\n"
        runner = ToolVersionRunner(matching)
        result = verify_build(
            VerifyBuildRequest(
                image="pi-cli-pi:latest",
                effective_projection_path=tf,
                runner=runner,
            )
        )
        obs_by_key = {o.key: o for o in result.observations}
        rustfmt_obs = obs_by_key["rust.rustfmt"]
        self.assertFalse(
            rustfmt_obs.ok,
            "rustfmt at /opt/tools/1.97.1/bin/rustfmt must fail "
            "provenance — it is not inside the rustup toolchains "
            "directory, so the path does not prove toolchain membership",
        )

    def test_rustfmt_malformed_rustup_output(self) -> None:
        """When rustup output cannot be parsed, verification must
        report the failure clearly rather than silently passing."""
        proj = self._proj_rust_1_97_1_with_rustfmt()
        tf = _write_projection_fixture(proj)
        matching = dict(_MATCHING)
        matching["rustc"] = "rustc 1.97.1 (8bab26f4f6 2025-01-15)\n"
        matching["cargo"] = "cargo 1.97.1 (8bab26f4f6 2025-01-15)\n"
        # rustup which returns unparseable output — no X.Y.Z
        # token to extract.
        matching["rustup"] = ""  # empty output
        runner = ToolVersionRunner(matching)
        result = verify_build(
            VerifyBuildRequest(
                image="pi-cli-pi:latest",
                effective_projection_path=tf,
                runner=runner,
            )
        )
        obs_by_key = {o.key: o for o in result.observations}
        rustfmt_obs = obs_by_key["rust.rustfmt"]
        # RED: current code normalizes empty string to "" which
        # won't match the expected version — but the failure
        # should say *why* (unparseable rustfmt output), not
        # just report a version mismatch.
        self.assertFalse(
            rustfmt_obs.ok,
            "malformed/empty rustfmt output must be reported as a verification failure",
        )

    def test_rustfmt_mismatched_toolchain_reported(self) -> None:
        """When rustfmt belongs to a different toolchain than the
        one configured, verification must identify the mismatch."""
        proj = self._proj_rust_1_97_1_with_rustfmt()
        tf = _write_projection_fixture(proj)
        matching = dict(_MATCHING)
        matching["rustc"] = "rustc 1.97.1 (8bab26f4f6 2025-01-15)\n"
        matching["cargo"] = "cargo 1.97.1 (8bab26f4f6 2025-01-15)\n"
        # rustup which returns a path from a *different* toolchain
        # (1.83.0).
        matching["rustup"] = (
            "/home/dev/.rustup/toolchains/1.83.0-x86_64-unknown-linux-gnu/bin/rustfmt\n"
        )
        runner = ToolVersionRunner(matching)
        result = verify_build(
            VerifyBuildRequest(
                image="pi-cli-pi:latest",
                effective_projection_path=tf,
                runner=runner,
            )
        )
        obs_by_key = {o.key: o for o in result.observations}
        rustfmt_obs = obs_by_key["rust.rustfmt"]
        # GREEN: the observed path contains toolchain 1.83.0,
        # not the expected 1.97.1.
        self.assertFalse(
            rustfmt_obs.ok,
            "rustfmt from toolchain 1.83.0 must be rejected when "
            "the configured toolchain is 1.97.1",
        )
        # The failure detail must mention the toolchain mismatch.
        observed = rustfmt_obs.observed_value or ""
        self.assertIn(
            "1.83.0",
            observed,
            "failure detail must identify the actual toolchain rustfmt resolves to",
        )

    def test_rustfmt_provenance_invokes_rustup_which(self) -> None:
        """Proving rustfmt provenance requires running
        ``rustup which --toolchain <configured> rustfmt`` to confirm
        the binary resolves through the configured toolchain, not
        the active/default one."""
        proj = self._proj_rust_1_97_1_with_rustfmt()
        tf = _write_projection_fixture(proj)
        matching = dict(_MATCHING)
        matching["rustc"] = "rustc 1.97.1 (8bab26f4f6 2025-01-15)\n"
        matching["cargo"] = "cargo 1.97.1 (8bab26f4f6 2025-01-15)\n"
        matching["rustup"] = (
            "/home/dev/.rustup/toolchains/1.97.1-x86_64-unknown-linux-gnu/bin/rustfmt\n"
        )
        runner = ToolVersionRunner(matching)
        verify_build(
            VerifyBuildRequest(
                image="pi-cli-pi:latest",
                effective_projection_path=tf,
                runner=runner,
            )
        )
        # GREEN: the exact ``rustup which --toolchain 1.97.1
        # rustfmt`` command must appear in the recorded calls.
        expected_cmd = (
            "docker",
            "run",
            "--rm",
            "pi-cli-pi:latest",
            "rustup",
            "which",
            "--toolchain",
            "1.97.1",
            "rustfmt",
        )
        self.assertIn(
            expected_cmd,
            runner.calls,
            "rustfmt provenance must invoke the exact "
            "'rustup which --toolchain 1.97.1 rustfmt' command",
        )

    def test_rustfmt_provenance_invokes_rustup_component_list(self) -> None:
        """Proving rustfmt provenance requires running
        ``rustup component list --toolchain <toolchain>`` to
        confirm the component is installed."""
        proj = self._proj_rust_1_97_1_with_rustfmt()
        tf = _write_projection_fixture(proj)
        matching = dict(_MATCHING)
        matching["rustc"] = "rustc 1.97.1 (8bab26f4f6 2025-01-15)\n"
        matching["cargo"] = "cargo 1.97.1 (8bab26f4f6 2025-01-15)\n"
        # rustup component list output with rustfmt installed.
        matching["rustup"] = (
            "rustfmt-x86_64-unknown-linux-gnu (installed)\n"
            "clippy-x86_64-unknown-linux-gnu (default)\n"
        )
        runner = ToolVersionRunner(matching)
        verify_build(
            VerifyBuildRequest(
                image="pi-cli-pi:latest",
                effective_projection_path=tf,
                runner=runner,
            )
        )
        # GREEN: the exact ``rustup component list --toolchain
        # 1.97.1`` command must appear in the recorded calls.
        expected_cmd = (
            "docker",
            "run",
            "--rm",
            "pi-cli-pi:latest",
            "rustup",
            "component",
            "list",
            "--toolchain",
            "1.97.1",
        )
        self.assertIn(
            expected_cmd,
            runner.calls,
            "rustfmt provenance must invoke the exact "
            "'rustup component list --toolchain 1.97.1' command",
        )

    # ── 1.5: Conditional rustfmt/clippy checks remain absent ─────

    def test_rustfmt_check_absent_when_not_configured(self) -> None:
        """When rustfmt is not in the Rust components list, the
        rustfmt observation contract must not be included."""
        # Already covered by existing TestVerificationContracts
        # tests (test_only_rustfmt_includes_rustfmt_only, etc.).
        # This test confirms the invariant from the ground up.
        proj = _canonical_projection()
        proj = EffectiveBuildProjection(
            platform=proj.platform,
            node=proj.node,
            rust=EffectiveRust(
                version="1.97.1",
                profile="minimal",
                components=(),  # no components configured
                rustup=proj.rust.rustup,
            ),
            uv=proj.uv,
            python_version=proj.python_version,
            ty_version=proj.ty_version,
            rtk=proj.rtk,
            fd=proj.fd,
            pi_version=proj.pi_version,
            pi_release=proj.pi_release,
            openspec_version=proj.openspec_version,
            oh_my_zsh_revision=proj.oh_my_zsh_revision,
        )
        tf = _write_projection_fixture(proj)
        matching = dict(_MATCHING)
        matching["rustc"] = "rustc 1.97.1 (8bab26f4f6 2025-01-15)\n"
        matching["cargo"] = "cargo 1.97.1 (8bab26f4f6 2025-01-15)\n"
        runner = ToolVersionRunner(matching)
        result = verify_build(
            VerifyBuildRequest(
                image="pi-cli-pi:latest",
                effective_projection_path=tf,
                runner=runner,
            )
        )
        keys = {o.key for o in result.observations}
        self.assertNotIn(
            "rust.rustfmt",
            keys,
            "rustfmt observation must be absent when rustfmt is "
            "not in the configured components",
        )
        self.assertNotIn(
            "rust.clippy",
            keys,
            "clippy observation must be absent when clippy is "
            "not in the configured components",
        )


class TestBuildObservationModel(unittest.TestCase):
    """Verify the observation data class contracts."""

    def test_observation_defaults(self) -> None:
        obs = BuildObservation(
            key="node.version",
            command=("docker", "run", "--rm", "img", "node", "--version"),
            expected_value="22.11.0",
        )
        self.assertIsNone(obs.observed_value)
        self.assertFalse(obs.ok)

    def test_observation_with_match(self) -> None:
        obs = BuildObservation(
            key="rust.version",
            command=("docker", "run", "--rm", "img", "rustc", "--version"),
            expected_value="1.83.0",
            observed_value="rustc 1.83.0 (90b35a623 2024-11-26)\n",
            ok=True,
        )
        self.assertTrue(obs.ok)
        self.assertEqual("1.83.0", obs.expected_value)

    def test_observation_with_mismatch(self) -> None:
        obs = BuildObservation(
            key="uv.version",
            command=("docker", "run", "--rm", "img", "uv", "--version"),
            expected_value="0.6.17",
            observed_value="uv 0.6.10\n",
            ok=False,
        )
        self.assertFalse(obs.ok)

    def test_node_observation_normalization(self) -> None:
        obs = BuildObservation(
            key="node.version",
            command=("docker", "run", "--rm", "img", "node", "--version"),
            expected_value="22.11.0",
            observed_value="v22.11.0\n",
            ok=True,
        )
        self.assertTrue(obs.ok)
        self.assertEqual("v22.11.0\n", obs.observed_value)
        self.assertEqual("22.11.0", obs.expected_value)

    def test_python_version_extraction(self) -> None:
        obs = BuildObservation(
            key="python.version",
            command=("docker", "run", "--rm", "img", "python3", "--version"),
            expected_value="3.14.0",
            observed_value="Python 3.14.0\n",
            ok=True,
        )
        self.assertTrue(obs.ok)


class TestVerifyBuildRequestModel(unittest.TestCase):
    """Verify the request data class contracts."""

    def test_request_requires_image(self) -> None:
        req = VerifyBuildRequest(
            image="pi-cli-pi:latest",
            effective_projection_path=_write_projection_fixture(),
            runner=ToolVersionRunner(),
        )
        self.assertEqual("pi-cli-pi:latest", req.image)

    def test_request_requires_projection_path(self) -> None:
        p = _write_projection_fixture()
        req = VerifyBuildRequest(
            image="img",
            effective_projection_path=p,
            runner=ToolVersionRunner(),
        )
        self.assertIsInstance(req.effective_projection_path, Path)

    def test_request_requires_runner(self) -> None:
        r = ToolVersionRunner()
        req = VerifyBuildRequest(
            image="img",
            effective_projection_path=_write_projection_fixture(),
            runner=r,
        )
        self.assertIs(r, req.runner)


class TestBuildVerificationResultModel(unittest.TestCase):
    """Verify the result data class contracts."""

    def test_all_ok_when_every_observation_ok(self) -> None:
        result = BuildVerificationResult(
            image="img",
            observations=(
                BuildObservation(
                    key="node.version",
                    command=("cmd",),
                    expected_value="22.11.0",
                    observed_value="v22.11.0\n",
                    ok=True,
                ),
            ),
            all_ok=True,
            errors=(),
        )
        self.assertTrue(result.all_ok)

    def test_not_all_ok_when_any_mismatch(self) -> None:
        result = BuildVerificationResult(
            image="img",
            observations=(
                BuildObservation(
                    key="node.version",
                    command=("cmd",),
                    expected_value="22.11.0",
                    observed_value="v22.11.0\n",
                    ok=True,
                ),
                BuildObservation(
                    key="uv.version",
                    command=("cmd",),
                    expected_value="0.6.17",
                    observed_value="uv 0.6.10\n",
                    ok=False,
                ),
            ),
            all_ok=False,
            errors=(),
        )
        self.assertFalse(result.all_ok)

    def test_errors_for_non_observation_failures(self) -> None:
        result = BuildVerificationResult(
            image="img",
            observations=(),
            all_ok=False,
            errors=("missing projection file",),
        )
        self.assertEqual(("missing projection file",), result.errors)


if __name__ == "__main__":
    unittest.main()

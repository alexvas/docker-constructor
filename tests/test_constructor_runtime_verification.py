"""RED tests for runtime verification (Stage 12, task 12.2).

Verifies that the runtime verification API inspects a running container's
state against host-side expectations — projection identity, read-only mount,
extension results, projects, working directory, ownership, Pi home,
gateway mapping, and forbidden paths.

The runtime projection is built through the maintained pipeline:
``RuntimeInventory`` → ``resolve_runtime()`` → ``create_runtime_projection()``.

Extension checks verify installed npm package metadata under
``/home/dev/.pi/agent/npm/node_modules/<package>/``.

All tests use fake process runners — no Docker required.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Sequence

from docker.versioning.constraints import Constraint, ConstraintClause, NumericVersion
from docker.versioning.effective import (
    Filesystem,
    RuntimeProjectionHandle,
    create_runtime_projection,
    resolve_runtime,
)
from docker.versioning.model import (
    EffectiveRuntimeProjection,
    HostAccessPolicy,
    NpmArtifact,
    NpmSource,
    NpmUpdate,
    OverridePolicy,
    PiExtensionEntry,
    RuntimeInventory,
    RuntimeValidation,
)
from docker.versioning.runtime_verification import (
    ProcessResult,
    RuntimeCheck,
    RuntimeVerificationResult,
    VerifyRuntimeRequest,
    verify_runtime,
)

# Module-level enabled policy shared by all docker-gateway verification tests
_ENABLED_POLICY = HostAccessPolicy(
    enabled=True,
    mode="docker-gateway",
    proxy_port=None,
)
_DEFAULT_ADDR = "192.168.65.254"


# ═══════════════════════════════════════════════════════════════════════
# Canonical runtime projection — built from model types through the
# maintained resolver / serializer pipeline
# ═══════════════════════════════════════════════════════════════════════

_EXT_CONSTRAINT = Constraint(
    (
        ConstraintClause(
            operator=">=", operand=NumericVersion(major=0, minor=0, patch=0)
        ),
    )
)


def _pi_read_entry() -> PiExtensionEntry:
    return PiExtensionEntry(
        version="0.2.0",
        source=NpmSource(package="@arcanemachine/pi-read"),
        update=NpmUpdate(stable_only=True),
        artifacts=MappingProxyType(
            {
                "0.2.0": NpmArtifact(
                    url="https://registry.npmjs.org/@arcanemachine/pi-read/-/pi-read-0.2.0.tgz",
                    integrity="sha512-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==",
                ),
            }
        ),
        validation=RuntimeValidation(metadata_file="package.json"),
        override=OverridePolicy(
            constraint=_EXT_CONSTRAINT,
            allow_prerelease=False,
            scheme="numeric",
        ),
    )


def _pi_codex_usage_entry() -> PiExtensionEntry:
    return PiExtensionEntry(
        version="0.9.1",
        source=NpmSource(package="@llblab/pi-codex-usage"),
        update=NpmUpdate(stable_only=True),
        artifacts=MappingProxyType(
            {
                "0.9.1": NpmArtifact(
                    url="https://registry.npmjs.org/@llblab/pi-codex-usage/-/pi-codex-usage-0.9.1.tgz",
                    integrity="sha384-AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEB",
                ),
            }
        ),
        validation=RuntimeValidation(metadata_file="package.json"),
        override=OverridePolicy(
            constraint=_EXT_CONSTRAINT,
            allow_prerelease=False,
            scheme="numeric",
        ),
    )


def _canonical_runtime_inventory() -> RuntimeInventory:
    """Return the test canonical runtime inventory."""
    return RuntimeInventory(
        pi_extensions=MappingProxyType(
            {
                "pi-read": _pi_read_entry(),
                "pi-codex-usage": _pi_codex_usage_entry(),
            }
        )
    )


def _canonical_runtime_projection(
    overrides: Mapping[str, str] | None = None,
) -> EffectiveRuntimeProjection:
    """Return the test canonical effective runtime projection."""
    _, proj = resolve_runtime(
        _canonical_runtime_inventory(),
        overrides or {},
    )
    return proj


def _canonical_runtime_projection_dict() -> dict:
    """Return the serialized dict representation for deriving
    expected values at runtime."""
    return _projection_to_data(_canonical_runtime_projection())


def _projection_to_data(projection: EffectiveRuntimeProjection) -> dict:
    """Manual plain-data serialization matching create_runtime_projection."""
    return {
        "extensions": {
            name: {
                "package": ext.package,
                "version": ext.version,
                "artifact_id": ext.artifact_id,
                "integrity": ext.integrity,
                "metadata_file": ext.metadata_file,
            }
            for name, ext in sorted(projection.extensions.items())
        },
    }


# ═══════════════════════════════════════════════════════════════════════
# Fake process runner — asserts every call targets the container
# ═══════════════════════════════════════════════════════════════════════


class _FakeRunner:
    """Returns canned output.  Asserts every call targets *container*."""

    def __init__(
        self,
        *,
        container: str = "",
        exit_code: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        self._container = container
        self._exit_code = exit_code
        self._stdout = stdout
        self._stderr = stderr
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: Sequence[str]) -> ProcessResult:
        t = tuple(argv)
        self.calls.append(t)
        if self._container:
            assert self._container in t, (
                f"command must target container {self._container!r}; got argv={t}"
            )
        return ProcessResult(
            argv=t,
            return_code=self._exit_code,
            stdout=self._stdout,
            stderr=self._stderr,
        )


class _DispatchRunner:
    """Matches calls by exact argv suffix.  For ``docker exec``
    commands the suffix is the command portion after
    ``("docker", "exec", container)``.  Asserts *container* is
    present in every call."""

    def __init__(
        self,
        container: str,
        handlers: dict[tuple[str, ...], tuple[int, str, str]],
    ) -> None:
        self._container = container
        self._handlers = dict(handlers)
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: Sequence[str]) -> ProcessResult:
        t = tuple(argv)
        self.calls.append(t)
        assert self._container in t, (
            f"command must target container {self._container!r}; got argv={t}"
        )
        for key, (rc, out, err) in self._handlers.items():
            if len(key) <= len(t) and t[-len(key) :] == key:
                return ProcessResult(argv=t, return_code=rc, stdout=out, stderr=err)
        return ProcessResult(argv=t, return_code=1, stdout="", stderr=f"unhandled: {t}")


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════

_CONTAINER = "pi-cli-pi-1"

# Abbreviated package.json responses for extension checks.
_PI_READ_PACKAGE_JSON = '{"name":"@arcanemachine/pi-read","version":"0.2.0"}'
_PI_CODEX_PACKAGE_JSON = '{"name":"@llblab/pi-codex-usage","version":"0.9.1"}'
# Alternate extension for projection-driven checks.
_ALT_PACKAGE = "@examples/alternate-tool"
_ALT_VERSION = "3.1.4"
_ALT_PACKAGE_JSON = '{"name":"@examples/alternate-tool","version":"3.1.4"}'

# Projection content hash — derived from the canonical projection.
# The test that verifies the actual hash uses a fresh handle from
# create_runtime_projection() in an explicit external runtime root.
_TEST_RUNTIME_ROOT = Path(tempfile.mkdtemp(prefix="runtime-verification-"))
unittest.addModuleCleanup(shutil.rmtree, _TEST_RUNTIME_ROOT, True)
_TEST_RUNTIME_FS = Filesystem(runtime_root=str(_TEST_RUNTIME_ROOT))


def _fresh_runtime_handle(suffix: str = ".toml"):
    import uuid

    path = _TEST_RUNTIME_ROOT / f"test-rt-{uuid.uuid4().hex}{suffix}"
    return create_runtime_projection(
        _canonical_runtime_projection(),
        host_path=str(path),
        _fs=_TEST_RUNTIME_FS,
    )


# ═══════════════════════════════════════════════════════════════════════
# Dispatch helpers — build a handler dict for a full passing run
# ═══════════════════════════════════════════════════════════════════════


def _passing_handlers(
    *,
    expected_hash: str,
    expected_addr: str = "192.168.65.254",
    workspace_paths: tuple[str, ...] = ("/tmp/p1",),
    extensions: dict[str, tuple[str, str]] | None = None,
) -> dict[tuple[str, ...], tuple[int, str, str]]:
    """Return handlers for a fully passing verification run.
    Each key is the exact argv suffix tuple (command after
    ``("docker", "exec", container)``).

    *extensions* maps npm package name → (version, package_json).
    When *None*, defaults to the canonical pi-read + pi-codex-usage."""
    if extensions is None:
        extensions = {
            "@arcanemachine/pi-read": ("0.2.0", _PI_READ_PACKAGE_JSON),
            "@llblab/pi-codex-usage": ("0.9.1", _PI_CODEX_PACKAGE_JSON),
        }
    handlers: dict[tuple[str, ...], tuple[int, str, str]] = {
        # projection.identity
        ("sha256sum", "/run/pi-cli/docker-constructor.runtime.toml"): (
            0,
            f"{expected_hash}  /run/pi-cli/docker-constructor.runtime.toml\n",
            "",
        ),
        # projection.readonly — verified via /proc/mounts mount options
        ("grep", "docker-constructor.runtime.toml", "/proc/mounts"): (
            0,
            "none /run/pi-cli/docker-constructor.runtime.toml ext4 ro,nosuid,nodev,relatime 0 0\n",
            "",
        ),
        ("test", "-w", "/run/pi-cli/docker-constructor.runtime.toml"): (1, "", ""),
        # working.directory
        ("pwd",): (0, f"{workspace_paths[0]}\n", ""),
        # ownership.dev — pi-home
        ("stat", "-c", "%U:%G", "/home/dev/.pi"): (0, "dev:dev\n", ""),
        # pi-home.setup
        ("test", "-d", "/home/dev/.pi"): (0, "", ""),
        ("test", "-w", "/home/dev/.pi"): (0, "", ""),
        # gateway.mapping — must resolve to the exact expected address
        ("getent", "hosts", "host.docker.internal"): (
            0,
            f"{expected_addr}  host.docker.internal\n",
            "",
        ),
        # forbidden.paths
        ("test", "-f", "/run/pi-cli/docker-constructor.toml"): (1, "", ""),
        ("test", "-f", "/run/pi-cli/docker-constructor.build.effective.toml"): (
            1,
            "",
            "",
        ),
    }
    # extensions.results — one cat per declared extension
    for pkg, (_ver, pkg_json) in sorted(extensions.items()):
        path = f"/home/dev/.pi/agent/npm/node_modules/{pkg}/package.json"
        handlers[("cat", path)] = (0, pkg_json, "")
    for i, pp in enumerate(workspace_paths, start=1):
        # workspaces.present — directory accessible AND env-var exact
        handlers[("test", "-d", pp)] = (0, "", "")
        handlers[("printenv", f"WORKSPACE_PATH_{i}")] = (0, f"{pp}\n", "")
        # ownership.dev — per-project
        handlers[("stat", "-c", "%U:%G", pp)] = (0, "dev:dev\n", "")
    # No unexpected next entry
    handlers[("printenv", f"WORKSPACE_PATH_{len(workspace_paths) + 1}")] = (1, "", "")
    return handlers


# ── Alternate-extension helpers ──────────────────────────────────────


def _alternate_entry() -> PiExtensionEntry:
    """A single non-canonical extension (alternate-tool v3.1.4)."""
    return PiExtensionEntry(
        version=_ALT_VERSION,
        source=NpmSource(package=_ALT_PACKAGE),
        update=NpmUpdate(stable_only=True),
        artifacts=MappingProxyType(
            {
                _ALT_VERSION: NpmArtifact(
                    url="https://registry.npmjs.org/@examples/alternate-tool/-/alternate-tool-3.1.4.tgz",
                    integrity="sha512-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==",
                ),
            }
        ),
        validation=RuntimeValidation(metadata_file="package.json"),
        override=OverridePolicy(
            constraint=_EXT_CONSTRAINT,
            allow_prerelease=False,
            scheme="numeric",
        ),
    )


def _alternate_inventory() -> RuntimeInventory:
    return RuntimeInventory(
        pi_extensions=MappingProxyType(
            {
                "alternate-tool": _alternate_entry(),
            }
        )
    )


def _alternate_runtime_projection(
    overrides: Mapping[str, str] | None = None,
) -> EffectiveRuntimeProjection:
    _, proj = resolve_runtime(_alternate_inventory(), overrides or {})
    return proj


def _alternate_runtime_handle(suffix: str = ".toml") -> RuntimeProjectionHandle:
    import uuid

    path = _TEST_RUNTIME_ROOT / f"test-alt-{uuid.uuid4().hex}{suffix}"
    return create_runtime_projection(
        _alternate_runtime_projection(),
        host_path=str(path),
        _fs=_TEST_RUNTIME_FS,
    )


# ═══════════════════════════════════════════════════════════════════════
# Tests
# ═══════════════════════════════════════════════════════════════════════


class TestRuntimeProjectionIdentity(unittest.TestCase):
    """verify_runtime checks that the container-side runtime projection
    identity matches the host-generated one."""

    def test_projection_data_helper_uses_flat_format(self) -> None:
        """_projection_to_data must produce artifact_id + integrity
        as top-level strings, not a nested artifact table."""
        data = _projection_to_data(_canonical_runtime_projection())
        self.assertIn("extensions", data)
        for _, entry in data["extensions"].items():
            self.assertIsInstance(entry.get("artifact_id"), str)
            self.assertIsInstance(entry.get("integrity"), str)
            self.assertNotIn(
                "artifact", entry, "legacy 'artifact' table must not appear"
            )
            self.assertNotIn("url", entry)

    def test_identity_matches(self) -> None:
        handle = _fresh_runtime_handle()
        handlers = _passing_handlers(expected_hash=handle.content_hash)
        runner = _DispatchRunner(_CONTAINER, handlers)
        with handle:
            result = verify_runtime(
                VerifyRuntimeRequest(
                    container=_CONTAINER,
                    runtime_projection_path=handle.path,
                    workspace_paths=(Path("/tmp/p1"),),
                    container_pi_home=Path("/home/dev/.pi"),
                    host_access=_ENABLED_POLICY,
                    host_access_address=_DEFAULT_ADDR,
                    runner=runner,
                )
            )
        identity = next(c for c in result.checks if c.key == "projection.identity")
        self.assertTrue(identity.ok)
        self.assertIn(handle.content_hash, identity.detail)

    def test_identity_mismatch_detected(self) -> None:
        handle = _fresh_runtime_handle()
        bogus = "deadbeef" + handle.content_hash[8:]
        handlers = _passing_handlers(expected_hash=bogus)
        runner = _DispatchRunner(_CONTAINER, handlers)
        with handle:
            result = verify_runtime(
                VerifyRuntimeRequest(
                    container=_CONTAINER,
                    runtime_projection_path=handle.path,
                    workspace_paths=(Path("/tmp/p1"),),
                    container_pi_home=Path("/home/dev/.pi"),
                    host_access=_ENABLED_POLICY,
                    host_access_address=_DEFAULT_ADDR,
                    runner=runner,
                )
            )
        identity = next(c for c in result.checks if c.key == "projection.identity")
        self.assertFalse(identity.ok)

    def test_identity_file_missing(self) -> None:
        handle = _fresh_runtime_handle()
        handlers = _passing_handlers(expected_hash=handle.content_hash)
        # Override sha256sum to fail — file not found
        handlers[("sha256sum", "/run/pi-cli/docker-constructor.runtime.toml")] = (
            1,
            "",
            "No such file or directory\n",
        )
        runner = _DispatchRunner(_CONTAINER, handlers)
        with handle:
            result = verify_runtime(
                VerifyRuntimeRequest(
                    container=_CONTAINER,
                    runtime_projection_path=handle.path,
                    workspace_paths=(Path("/tmp/p1"),),
                    container_pi_home=Path("/home/dev/.pi"),
                    host_access=_ENABLED_POLICY,
                    host_access_address=_DEFAULT_ADDR,
                    runner=runner,
                )
            )
        identity = next(c for c in result.checks if c.key == "projection.identity")
        self.assertFalse(identity.ok)


class TestRuntimeReadonlyMount(unittest.TestCase):
    """The runtime projection mount must be read-only — verified via
    ``/proc/mounts`` mount-option metadata, not file permissions."""

    def test_readonly_mount(self) -> None:
        """Both /proc/mounts shows ``ro`` and test -w fails → pass."""
        handle = _fresh_runtime_handle()
        handlers = _passing_handlers(expected_hash=handle.content_hash)
        runner = _DispatchRunner(_CONTAINER, handlers)
        with handle:
            result = verify_runtime(
                VerifyRuntimeRequest(
                    container=_CONTAINER,
                    runtime_projection_path=handle.path,
                    workspace_paths=(Path("/tmp/p1"),),
                    container_pi_home=Path("/home/dev/.pi"),
                    host_access=_ENABLED_POLICY,
                    host_access_address=_DEFAULT_ADDR,
                    runner=runner,
                )
            )
        ro = next(c for c in result.checks if c.key == "projection.readonly")
        self.assertTrue(ro.ok)

    def test_writable_mount_detected_via_proc_mounts(self) -> None:
        """/proc/mounts shows ``rw`` → mount is writable → fail."""
        handle = _fresh_runtime_handle()
        handlers = _passing_handlers(expected_hash=handle.content_hash)
        handlers[("grep", "docker-constructor.runtime.toml", "/proc/mounts")] = (
            0,
            "none /run/pi-cli/docker-constructor.runtime.toml ext4 rw,nosuid,nodev,relatime 0 0\n",
            "",
        )
        runner = _DispatchRunner(_CONTAINER, handlers)
        with handle:
            result = verify_runtime(
                VerifyRuntimeRequest(
                    container=_CONTAINER,
                    runtime_projection_path=handle.path,
                    workspace_paths=(Path("/tmp/p1"),),
                    container_pi_home=Path("/home/dev/.pi"),
                    host_access=_ENABLED_POLICY,
                    host_access_address=_DEFAULT_ADDR,
                    runner=runner,
                )
            )
        ro = next(c for c in result.checks if c.key == "projection.readonly")
        self.assertFalse(ro.ok)

    def test_writable_mount_with_restrictive_permissions(self) -> None:
        """Mount is writable (rw in /proc/mounts) but file permissions
        make test -w fail — check must still FAIL because the mount
        metadata is authoritative."""
        handle = _fresh_runtime_handle()
        handlers = _passing_handlers(expected_hash=handle.content_hash)
        # /proc/mounts shows rw — writable mount
        handlers[("grep", "docker-constructor.runtime.toml", "/proc/mounts")] = (
            0,
            "none /run/pi-cli/docker-constructor.runtime.toml ext4 rw,nosuid,nodev,relatime 0 0\n",
            "",
        )
        # test -w still fails — file permissions, not mount
        runner = _DispatchRunner(_CONTAINER, handlers)
        with handle:
            result = verify_runtime(
                VerifyRuntimeRequest(
                    container=_CONTAINER,
                    runtime_projection_path=handle.path,
                    workspace_paths=(Path("/tmp/p1"),),
                    container_pi_home=Path("/home/dev/.pi"),
                    host_access=_ENABLED_POLICY,
                    host_access_address=_DEFAULT_ADDR,
                    runner=runner,
                )
            )
        ro = next(c for c in result.checks if c.key == "projection.readonly")
        self.assertFalse(ro.ok)


class TestRuntimeExtensionsResults(unittest.TestCase):
    """Extension checks verify installed npm package metadata under
    ``/home/dev/.pi/agent/npm/node_modules/<package>/``."""

    def test_all_extensions_installed(self) -> None:
        handle = _fresh_runtime_handle()
        handlers = _passing_handlers(expected_hash=handle.content_hash)
        runner = _DispatchRunner(_CONTAINER, handlers)
        with handle:
            result = verify_runtime(
                VerifyRuntimeRequest(
                    container=_CONTAINER,
                    runtime_projection_path=handle.path,
                    workspace_paths=(Path("/tmp/p1"),),
                    container_pi_home=Path("/home/dev/.pi"),
                    host_access=_ENABLED_POLICY,
                    host_access_address=_DEFAULT_ADDR,
                    runner=runner,
                )
            )
        per_extension = [c for c in result.checks if c.key == "extensions.results"]
        self.assertTrue(len(per_extension) >= 1)
        self.assertTrue(all(e.ok for e in per_extension))
        # The detail should mention both installed packages.
        detail = " ".join(e.detail for e in per_extension)
        self.assertIn("pi-read", detail.lower())
        self.assertIn("pi-codex", detail.lower())

    def test_missing_extension_detected(self) -> None:
        handle = _fresh_runtime_handle()
        handlers = _passing_handlers(expected_hash=handle.content_hash)
        # pi-read package.json missing
        handlers[
            (
                "cat",
                "/home/dev/.pi/agent/npm/node_modules/@arcanemachine/pi-read/package.json",
            )
        ] = (1, "", "cat: ... No such file or directory\n")
        runner = _DispatchRunner(_CONTAINER, handlers)
        with handle:
            result = verify_runtime(
                VerifyRuntimeRequest(
                    container=_CONTAINER,
                    runtime_projection_path=handle.path,
                    workspace_paths=(Path("/tmp/p1"),),
                    container_pi_home=Path("/home/dev/.pi"),
                    host_access=_ENABLED_POLICY,
                    host_access_address=_DEFAULT_ADDR,
                    runner=runner,
                )
            )
        per_extension = [c for c in result.checks if c.key == "extensions.results"]
        # Find the pi-read check (missing handler → fail).
        pi_read = next(c for c in per_extension if "pi-read" in c.detail.lower())
        self.assertFalse(pi_read.ok)

    def test_version_mismatch_detected(self) -> None:
        handle = _fresh_runtime_handle()
        handlers = _passing_handlers(expected_hash=handle.content_hash)
        # pi-codex-usage has wrong version
        handlers[
            (
                "cat",
                "/home/dev/.pi/agent/npm/node_modules/@llblab/pi-codex-usage/package.json",
            )
        ] = (0, '{"name":"@llblab/pi-codex-usage","version":"0.7.0"}', "")
        runner = _DispatchRunner(_CONTAINER, handlers)
        with handle:
            result = verify_runtime(
                VerifyRuntimeRequest(
                    container=_CONTAINER,
                    runtime_projection_path=handle.path,
                    workspace_paths=(Path("/tmp/p1"),),
                    container_pi_home=Path("/home/dev/.pi"),
                    host_access=_ENABLED_POLICY,
                    host_access_address=_DEFAULT_ADDR,
                    runner=runner,
                )
            )
        per_extension = [c for c in result.checks if c.key == "extensions.results"]
        # Find the pi-codex-usage check (wrong version → fail).
        codex = next(c for c in per_extension if "pi-codex" in c.detail.lower())
        self.assertFalse(codex.ok)

    def test_extensions_parsed_from_projection_not_hardcoded(self) -> None:
        """The extensions.results check reads its expectations from
        the effective runtime projection, not from hard-coded defaults.

        Using an alternate projection with only ``alternate-tool``
        v3.1.4, the check must ONLY cat that one package.json — no
        handlers for canonical pi-read or pi-codex-usage exist, so any
        stray cat would fail via ``_DispatchRunner`` unhandled."""
        alt_handle = _alternate_runtime_handle()
        handlers = _passing_handlers(
            expected_hash=alt_handle.content_hash,
            extensions={_ALT_PACKAGE: (_ALT_VERSION, _ALT_PACKAGE_JSON)},
        )
        runner = _DispatchRunner(_CONTAINER, handlers)
        with alt_handle:
            result = verify_runtime(
                VerifyRuntimeRequest(
                    container=_CONTAINER,
                    runtime_projection_path=alt_handle.path,
                    workspace_paths=(Path("/tmp/p1"),),
                    container_pi_home=Path("/home/dev/.pi"),
                    host_access=_ENABLED_POLICY,
                    host_access_address=_DEFAULT_ADDR,
                    runner=runner,
                )
            )
        ext = next(c for c in result.checks if c.key == "extensions.results")
        self.assertTrue(ext.ok)
        # Detail MUST mention the alternate package, not canonicals.
        detail_lower = ext.detail.lower()
        self.assertIn("alternate-tool", detail_lower)
        self.assertNotIn("pi-read", detail_lower)
        self.assertNotIn("pi-codex-usage", detail_lower)
        # No cat commands for canonical packages were issued.
        all_argv = [tuple(c) for c in runner.calls]
        canonicals = [
            t
            for t in all_argv
            if any("pi-read" in a or "pi-codex-usage" in a for a in t)
        ]
        self.assertEqual([], canonicals)


class TestRuntimeProjects(unittest.TestCase):
    """WORKSPACE_PATH_1..N environment contract — exact values,
    consecutive numbering, no unexpected next entry."""

    def test_project_env_contract(self) -> None:
        """Each WORKSPACE_PATH_N has the exact expected value,
        numbering is 1..N, and WORKSPACE_PATH_{N+1} is unset."""
        workspace_paths = ("/tmp/p1", "/tmp/p2")
        handle = _fresh_runtime_handle()
        handlers = _passing_handlers(
            expected_hash=handle.content_hash,
            workspace_paths=workspace_paths,
        )
        runner = _DispatchRunner(_CONTAINER, handlers)
        with handle:
            result = verify_runtime(
                VerifyRuntimeRequest(
                    container=_CONTAINER,
                    runtime_projection_path=handle.path,
                    workspace_paths=tuple(Path(p) for p in workspace_paths),
                    container_pi_home=Path("/home/dev/.pi"),
                    host_access=_ENABLED_POLICY,
                    host_access_address=_DEFAULT_ADDR,
                    runner=runner,
                )
            )
        project_checks = [c for c in result.checks if c.key == "workspaces.present"]
        self.assertTrue(all(c.ok for c in project_checks))
        detail = " ".join(c.detail for c in project_checks)
        self.assertIn("/tmp/p1", detail)
        self.assertIn("/tmp/p2", detail)

    def test_wrong_env_value_detected(self) -> None:
        """WORKSPACE_PATH_1 is set but to a different path → fail."""
        workspace_paths = ("/tmp/p1",)
        handle = _fresh_runtime_handle()
        handlers = _passing_handlers(
            expected_hash=handle.content_hash,
            workspace_paths=workspace_paths,
        )
        handlers[("printenv", "WORKSPACE_PATH_1")] = (0, "/some/other/dir\n", "")
        runner = _DispatchRunner(_CONTAINER, handlers)
        with handle:
            result = verify_runtime(
                VerifyRuntimeRequest(
                    container=_CONTAINER,
                    runtime_projection_path=handle.path,
                    workspace_paths=tuple(Path(p) for p in workspace_paths),
                    container_pi_home=Path("/home/dev/.pi"),
                    host_access=_ENABLED_POLICY,
                    host_access_address=_DEFAULT_ADDR,
                    runner=runner,
                )
            )
        project_checks = [c for c in result.checks if c.key == "workspaces.present"]
        self.assertFalse(all(c.ok for c in project_checks))

    def test_missing_env_variable_detected(self) -> None:
        """WORKSPACE_PATH_2 is unset when it should exist → fail."""
        workspace_paths = ("/tmp/p1", "/tmp/p2")
        handle = _fresh_runtime_handle()
        handlers = _passing_handlers(
            expected_hash=handle.content_hash,
            workspace_paths=workspace_paths,
        )
        handlers[("printenv", "WORKSPACE_PATH_2")] = (1, "", "")
        runner = _DispatchRunner(_CONTAINER, handlers)
        with handle:
            result = verify_runtime(
                VerifyRuntimeRequest(
                    container=_CONTAINER,
                    runtime_projection_path=handle.path,
                    workspace_paths=tuple(Path(p) for p in workspace_paths),
                    container_pi_home=Path("/home/dev/.pi"),
                    host_access=_ENABLED_POLICY,
                    host_access_address=_DEFAULT_ADDR,
                    runner=runner,
                )
            )
        project_checks = [c for c in result.checks if c.key == "workspaces.present"]
        self.assertFalse(all(c.ok for c in project_checks))

    def test_unexpected_next_entry_detected(self) -> None:
        """WORKSPACE_PATH_3 is set but only 2 projects declared → fail."""
        workspace_paths = ("/tmp/p1", "/tmp/p2")
        handle = _fresh_runtime_handle()
        handlers = _passing_handlers(
            expected_hash=handle.content_hash,
            workspace_paths=workspace_paths,
        )
        handlers[("printenv", "WORKSPACE_PATH_3")] = (0, "/unexpected/path\n", "")
        runner = _DispatchRunner(_CONTAINER, handlers)
        with handle:
            result = verify_runtime(
                VerifyRuntimeRequest(
                    container=_CONTAINER,
                    runtime_projection_path=handle.path,
                    workspace_paths=tuple(Path(p) for p in workspace_paths),
                    container_pi_home=Path("/home/dev/.pi"),
                    host_access=_ENABLED_POLICY,
                    host_access_address=_DEFAULT_ADDR,
                    runner=runner,
                )
            )
        project_checks = [c for c in result.checks if c.key == "workspaces.present"]
        self.assertFalse(all(c.ok for c in project_checks))

    def test_directory_missing_but_env_set(self) -> None:
        """WORKSPACE_PATH_1 is set correctly but the directory does not
        exist → fail (both checks are independent)."""
        workspace_paths = ("/tmp/p1",)
        handle = _fresh_runtime_handle()
        handlers = _passing_handlers(
            expected_hash=handle.content_hash,
            workspace_paths=workspace_paths,
        )
        # Env var is correct, but directory is missing.
        handlers[("test", "-d", "/tmp/p1")] = (1, "", "")
        runner = _DispatchRunner(_CONTAINER, handlers)
        with handle:
            result = verify_runtime(
                VerifyRuntimeRequest(
                    container=_CONTAINER,
                    runtime_projection_path=handle.path,
                    workspace_paths=tuple(Path(p) for p in workspace_paths),
                    container_pi_home=Path("/home/dev/.pi"),
                    host_access=_ENABLED_POLICY,
                    host_access_address=_DEFAULT_ADDR,
                    runner=runner,
                )
            )
        proj = next(c for c in result.checks if c.key == "workspaces.present")
        self.assertFalse(proj.ok)


class TestRuntimeWorkingDirectory(unittest.TestCase):
    """Working directory must equal WORKSPACE_PATH_1."""

    def test_correct_dir(self) -> None:
        handle = _fresh_runtime_handle()
        handlers = _passing_handlers(expected_hash=handle.content_hash)
        runner = _DispatchRunner(_CONTAINER, handlers)
        with handle:
            result = verify_runtime(
                VerifyRuntimeRequest(
                    container=_CONTAINER,
                    runtime_projection_path=handle.path,
                    workspace_paths=(Path("/tmp/p1"),),
                    container_pi_home=Path("/home/dev/.pi"),
                    host_access=_ENABLED_POLICY,
                    host_access_address=_DEFAULT_ADDR,
                    runner=runner,
                )
            )
        wd = next(c for c in result.checks if c.key == "working.directory")
        self.assertTrue(wd.ok)
        self.assertIn("/tmp/p1", wd.detail)

    def test_wrong_dir_detected(self) -> None:
        handle = _fresh_runtime_handle()
        handlers = _passing_handlers(expected_hash=handle.content_hash)
        handlers[("pwd",)] = (0, "/home/dev\n", "")
        runner = _DispatchRunner(_CONTAINER, handlers)
        with handle:
            result = verify_runtime(
                VerifyRuntimeRequest(
                    container=_CONTAINER,
                    runtime_projection_path=handle.path,
                    workspace_paths=(Path("/tmp/p1"),),
                    container_pi_home=Path("/home/dev/.pi"),
                    host_access=_ENABLED_POLICY,
                    host_access_address=_DEFAULT_ADDR,
                    runner=runner,
                )
            )
        wd = next(c for c in result.checks if c.key == "working.directory")
        self.assertFalse(wd.ok)


class TestRuntimeOwnership(unittest.TestCase):
    """Key paths must be owned by dev:dev."""

    def test_dev_ownership(self) -> None:
        handle = _fresh_runtime_handle()
        handlers = _passing_handlers(expected_hash=handle.content_hash)
        runner = _DispatchRunner(_CONTAINER, handlers)
        with handle:
            result = verify_runtime(
                VerifyRuntimeRequest(
                    container=_CONTAINER,
                    runtime_projection_path=handle.path,
                    workspace_paths=(Path("/tmp/p1"),),
                    container_pi_home=Path("/home/dev/.pi"),
                    host_access=_ENABLED_POLICY,
                    host_access_address=_DEFAULT_ADDR,
                    runner=runner,
                )
            )
        own = next(c for c in result.checks if c.key == "ownership.dev")
        self.assertTrue(own.ok)

    def test_wrong_ownership_detected(self) -> None:
        handle = _fresh_runtime_handle()
        handlers = _passing_handlers(expected_hash=handle.content_hash)
        # Corrupt a project path ownership — proves every project is checked.
        handlers[("stat", "-c", "%U:%G", "/tmp/p1")] = (0, "root:root\n", "")
        runner = _DispatchRunner(_CONTAINER, handlers)
        with handle:
            result = verify_runtime(
                VerifyRuntimeRequest(
                    container=_CONTAINER,
                    runtime_projection_path=handle.path,
                    workspace_paths=(Path("/tmp/p1"),),
                    container_pi_home=Path("/home/dev/.pi"),
                    host_access=_ENABLED_POLICY,
                    host_access_address=_DEFAULT_ADDR,
                    runner=runner,
                )
            )
        per_owner = [c for c in result.checks if c.key == "ownership.dev"]
        self.assertFalse(all(c.ok for c in per_owner))
        # Specifically, the project path check should have failed.
        proj_own = next(c for c in per_owner if "/tmp/p1" in c.detail)
        self.assertFalse(proj_own.ok)


class TestRuntimePiHome(unittest.TestCase):
    """~/.pi must exist and be writable."""

    def test_pi_home_configured(self) -> None:
        handle = _fresh_runtime_handle()
        handlers = _passing_handlers(expected_hash=handle.content_hash)
        runner = _DispatchRunner(_CONTAINER, handlers)
        with handle:
            result = verify_runtime(
                VerifyRuntimeRequest(
                    container=_CONTAINER,
                    runtime_projection_path=handle.path,
                    workspace_paths=(Path("/tmp/p1"),),
                    container_pi_home=Path("/home/dev/.pi"),
                    host_access=_ENABLED_POLICY,
                    host_access_address=_DEFAULT_ADDR,
                    runner=runner,
                )
            )
        ph = next(c for c in result.checks if c.key == "pi-home.setup")
        self.assertTrue(ph.ok)

    def test_pi_home_missing_detected(self) -> None:
        handle = _fresh_runtime_handle()
        handlers = _passing_handlers(expected_hash=handle.content_hash)
        handlers[("test", "-d", "/home/dev/.pi")] = (1, "", "")
        handlers[("test", "-w", "/home/dev/.pi")] = (1, "", "")
        runner = _DispatchRunner(_CONTAINER, handlers)
        with handle:
            result = verify_runtime(
                VerifyRuntimeRequest(
                    container=_CONTAINER,
                    runtime_projection_path=handle.path,
                    workspace_paths=(Path("/tmp/p1"),),
                    container_pi_home=Path("/home/dev/.pi"),
                    host_access=_ENABLED_POLICY,
                    host_access_address=_DEFAULT_ADDR,
                    runner=runner,
                )
            )
        ph = next(c for c in result.checks if c.key == "pi-home.setup")
        self.assertFalse(ph.ok)


class TestRuntimeGatewayMapping(unittest.TestCase):
    """host.docker.internal must resolve to the exact expected gateway."""

    _GATEWAY = "192.168.65.254"

    def test_gateway_matches_expected_address(self) -> None:
        handle = _fresh_runtime_handle()
        handlers = _passing_handlers(
            expected_hash=handle.content_hash,
            expected_addr=self._GATEWAY,
        )
        runner = _DispatchRunner(_CONTAINER, handlers)
        with handle:
            result = verify_runtime(
                VerifyRuntimeRequest(
                    container=_CONTAINER,
                    runtime_projection_path=handle.path,
                    workspace_paths=(Path("/tmp/p1"),),
                    container_pi_home=Path("/home/dev/.pi"),
                    host_access=_ENABLED_POLICY,
                    host_access_address=self._GATEWAY,
                    runner=runner,
                )
            )
        gw = next(c for c in result.checks if c.key == "gateway.mapping")
        self.assertTrue(gw.ok)
        self.assertIn(self._GATEWAY, gw.detail)

    def test_gateway_wrong_address_detected(self) -> None:
        """getent resolves but to a different address — check fails."""
        handle = _fresh_runtime_handle()
        handlers = _passing_handlers(
            expected_hash=handle.content_hash,
            expected_addr=self._GATEWAY,
        )
        # Container resolves host.docker.internal to a different IP.
        handlers[("getent", "hosts", "host.docker.internal")] = (
            0,
            "10.0.0.1  host.docker.internal\n",
            "",
        )
        runner = _DispatchRunner(_CONTAINER, handlers)
        with handle:
            result = verify_runtime(
                VerifyRuntimeRequest(
                    container=_CONTAINER,
                    runtime_projection_path=handle.path,
                    workspace_paths=(Path("/tmp/p1"),),
                    container_pi_home=Path("/home/dev/.pi"),
                    host_access=_ENABLED_POLICY,
                    host_access_address=self._GATEWAY,
                    runner=runner,
                )
            )
        gw = next(c for c in result.checks if c.key == "gateway.mapping")
        self.assertFalse(gw.ok)

    def test_gateway_resolution_failure_detected(self) -> None:
        """getent fails entirely — check fails."""
        handle = _fresh_runtime_handle()
        handlers = _passing_handlers(
            expected_hash=handle.content_hash,
            expected_addr=self._GATEWAY,
        )
        handlers[("getent", "hosts", "host.docker.internal")] = (2, "", "")
        runner = _DispatchRunner(_CONTAINER, handlers)
        with handle:
            result = verify_runtime(
                VerifyRuntimeRequest(
                    container=_CONTAINER,
                    runtime_projection_path=handle.path,
                    workspace_paths=(Path("/tmp/p1"),),
                    container_pi_home=Path("/home/dev/.pi"),
                    host_access=_ENABLED_POLICY,
                    host_access_address=self._GATEWAY,
                    runner=runner,
                )
            )
        gw = next(c for c in result.checks if c.key == "gateway.mapping")
        self.assertFalse(gw.ok)


class TestRuntimeForbiddenPaths(unittest.TestCase):
    """Host-only files must NOT be present in the container."""

    def test_forbidden_paths_absent(self) -> None:
        handle = _fresh_runtime_handle()
        handlers = _passing_handlers(expected_hash=handle.content_hash)
        runner = _DispatchRunner(_CONTAINER, handlers)
        with handle:
            result = verify_runtime(
                VerifyRuntimeRequest(
                    container=_CONTAINER,
                    runtime_projection_path=handle.path,
                    workspace_paths=(Path("/tmp/p1"),),
                    container_pi_home=Path("/home/dev/.pi"),
                    host_access=_ENABLED_POLICY,
                    host_access_address=_DEFAULT_ADDR,
                    runner=runner,
                )
            )
        fp = next(c for c in result.checks if c.key == "forbidden.paths")
        self.assertTrue(fp.ok)

    def test_forbidden_path_present_detected(self) -> None:
        handle = _fresh_runtime_handle()
        handlers = _passing_handlers(expected_hash=handle.content_hash)
        # Reviewed inventory IS present — must fail
        handlers[("test", "-f", "/run/pi-cli/docker-constructor.toml")] = (0, "", "")
        runner = _DispatchRunner(_CONTAINER, handlers)
        with handle:
            result = verify_runtime(
                VerifyRuntimeRequest(
                    container=_CONTAINER,
                    runtime_projection_path=handle.path,
                    workspace_paths=(Path("/tmp/p1"),),
                    container_pi_home=Path("/home/dev/.pi"),
                    host_access=_ENABLED_POLICY,
                    host_access_address=_DEFAULT_ADDR,
                    runner=runner,
                )
            )
        fp = next(c for c in result.checks if c.key == "forbidden.paths")
        self.assertFalse(fp.ok)


class TestRuntimeVerificationStub(unittest.TestCase):
    """Explicit gate test — verify_runtime is NOT implemented yet."""

    def test_verify_runtime_is_not_implemented(self) -> None:
        """Gate test — verify_runtime is now live (no longer a stub)."""
        handle = _fresh_runtime_handle()
        runner = _FakeRunner(container=_CONTAINER)
        with handle:
            req = VerifyRuntimeRequest(
                container=_CONTAINER,
                runtime_projection_path=handle.path,
                workspace_paths=(Path("/tmp/p1"),),
                container_pi_home=Path("/home/dev/.pi"),
                host_access=_ENABLED_POLICY,
                host_access_address=_DEFAULT_ADDR,
                runner=runner,
            )
            result = verify_runtime(req)
            self.assertIsInstance(result, RuntimeVerificationResult)


# ═══════════════════════════════════════════════════════════════════════
# Model tests
# ═══════════════════════════════════════════════════════════════════════


class TestRuntimeCheckModel(unittest.TestCase):
    def test_check_ok(self) -> None:
        c = RuntimeCheck(
            key="projection.identity", ok=True, detail="hash matches abc123"
        )
        self.assertTrue(c.ok)
        self.assertEqual("projection.identity", c.key)

    def test_check_not_ok(self) -> None:
        c = RuntimeCheck(
            key="gateway.mapping", ok=False, detail="host.docker.internal not found"
        )
        self.assertFalse(c.ok)


class TestRuntimeVerificationResultModel(unittest.TestCase):
    def test_all_ok_when_all_checks_pass(self) -> None:
        result = RuntimeVerificationResult(
            container="c",
            checks=(
                RuntimeCheck(key="a", ok=True, detail="ok"),
                RuntimeCheck(key="b", ok=True, detail="ok"),
            ),
            all_ok=True,
            errors=(),
        )
        self.assertTrue(result.all_ok)

    def test_not_all_ok_when_any_check_fails(self) -> None:
        result = RuntimeVerificationResult(
            container="c",
            checks=(
                RuntimeCheck(key="a", ok=True, detail="ok"),
                RuntimeCheck(key="b", ok=False, detail="fail"),
            ),
            all_ok=False,
            errors=(),
        )
        self.assertFalse(result.all_ok)

    def test_errors_independent_of_checks(self) -> None:
        result = RuntimeVerificationResult(
            container="c",
            checks=(),
            all_ok=False,
            errors=("container not running",),
        )
        self.assertEqual(("container not running",), result.errors)


class TestVerifyRuntimeRequestModel(unittest.TestCase):
    def test_request_fields(self) -> None:
        handle = _fresh_runtime_handle()
        runner = _FakeRunner(container=_CONTAINER)
        with handle:
            req = VerifyRuntimeRequest(
                container=_CONTAINER,
                runtime_projection_path=handle.path,
                workspace_paths=(Path("/tmp/p1"),),
                container_pi_home=Path("/home/dev/.pi"),
                host_access=_ENABLED_POLICY,
                host_access_address=_DEFAULT_ADDR,
                runner=runner,
            )
            self.assertEqual(_CONTAINER, req.container)
            self.assertEqual(Path("/home/dev/.pi"), req.container_pi_home)
            self.assertEqual("192.168.65.254", req.host_access_address)
            self.assertIsNotNone(req.host_access)
            self.assertEqual("docker-gateway", req.host_access.mode)
            self.assertIs(runner, req.runner)


if __name__ == "__main__":
    unittest.main()

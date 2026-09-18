"""Phase 6 integrated acceptance tests — end-to-end proof.

Each test exercises the full orchestration boundary (build, run, doctor,
verify) through the real production functions with a minimal filesystem
and fake Docker/network injectables.  No real Docker is touched.
"""
from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Mapping
from unittest import mock

from docker.launcher import RunRequest
from docker.versioning.build_orchestration import (
    BuildRequest,
    DoctorRequest,
    ExitKind,
    orchestrate_build,
    orchestrate_doctor,
)
from docker.versioning.rendering import RunHostAccess
# verify_runtime is covered by Phase 4 focused tests


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════

_REAL_INVENTORY = (
    Path(__file__).resolve().parents[1] / "docker-constructor.toml"
).read_text()


def _write_inventory(root: Path, *, policy: str = "") -> Path:
    """Write a reviewed inventory under *root* with optional
    ``[runtime.host-access]`` tail."""
    inv = root / "docker-constructor.toml"
    inv.write_text(_REAL_INVENTORY + "\n" + policy)
    return inv


def _write_custom_inventory(root: Path, stem: str, *, policy: str = "") -> Path:
    """Write a custom-named reviewed inventory and return its path."""
    inv = root / f"{stem}.toml"
    inv.write_text(_REAL_INVENTORY + "\n" + policy)
    return inv


def _make_fake_diagnose(ip: str = "10.0.2.2"):
    """Return a fake ``_diagnose_gateway`` callable."""
    from docker.networking import DockerMode, GatewayDiagnosis, ProbeResult

    def _fake(**__: Any):
        probe = ProbeResult(
            candidate=ip, ok=True, resolved_ip=ip, detail="fake",
        )
        return GatewayDiagnosis(
            mode=DockerMode.ROOTFUL,
            probe_port=19999,
            probe_token="fake-token",
            lan_ip=None,
            probes=(probe,),
            chosen_gateway=ip,
            override_installed=False,
            override_needed=False,
        )

    return _fake


_FALLBACK_RUNNER_RESULT: Any = None


def _fallback_runner():
    """A process runner that returns success for any unknown command."""
    from docker.launcher import ProcessResult

    def _run(argv, *, interactive: bool = False):
        return ProcessResult(
            argv=tuple(argv), return_code=0, stdout="", stderr="",
        )

    return _run


def _bomb_docker():
    """Runner that fails if called -- proves Docker was never invoked."""
    def _run(argv, *, interactive: bool = False):
        raise AssertionError(
            f"Docker must not run; got {argv}"
        )
    return _run


def _bomb_gateway(**__: Any):
    """Fail if gateway diagnosis is invoked."""
    raise AssertionError("gateway diagnosis must not be called")


def _bomb_doctor(**__: Any):
    """Fail if doctor orchestration is invoked."""
    raise AssertionError("doctor must not be called")


def _bomb_rootless_plan(**__: Any):
    """Fail if rootless-override planning is invoked."""
    raise AssertionError("rootless-override planning must not be called")


def _bomb_rootless_apply(**__: Any):
    """Fail if rootless-override application is invoked."""
    raise AssertionError("rootless-override application must not be called")


# ═══════════════════════════════════════════════════════════════════════
# 6.1  Disabled host access — builds and planned runs need no gateway
# ═══════════════════════════════════════════════════════════════════════

class TestDisabledHostAccess(unittest.TestCase):
    """6.1: From absent host-access policy, prove build and planned
    run require neither gateway nor local state."""

    def test_build_succeeds_without_host_access_policy(self):
        """With no ``[runtime.host-access]`` the build must complete
        successfully without ever probing a gateway."""
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            inv = _write_inventory(root_path)  # disabled by omission

            req = BuildRequest(
                inventory_path=str(inv),
                platform="linux-amd64",
                confirmed=True,
                dry_run=True,
                runner=_bomb_docker(),
                _diagnose_gateway=_bomb_gateway,
            project_root=Path(str(inv)).resolve().parent)
            result = orchestrate_build(req)
            self.assertEqual(
                ExitKind.SUCCESS, result.exit_kind,
                f"expected SUCCESS, got {result.exit_kind}: {result.message}",
            )
            self.assertIsNotNone(result.display_string)

    def test_run_vector_omits_host_access_when_disabled(self):
        """Disabled host access produces a run vector with no
        ``--add-host``, ``HOST_ACCESS_ADDRESS``, or ``HOST_PROXY_PORT``."""
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            inv = _write_inventory(root_path)
            # No local companion at all — disabled should work without it

            from docker.launcher import orchestrate_run
            from docker.launcher import WorkspaceSelection

            with mock.patch(
                "docker.versioning.build_orchestration.orchestrate_doctor",
                side_effect=_bomb_doctor,
            ) as mocked_doctor, mock.patch(
                "docker.versioning.build_orchestration.diagnose_gateway",
                side_effect=_bomb_gateway,
            ) as mocked_build_orch_gateway, mock.patch(
                "docker.networking.diagnose_gateway",
                side_effect=_bomb_gateway,
            ) as mocked_gateway:
                dry_result = orchestrate_run(
                    RunRequest(
                        inventory_path=str(inv),
                        image="pi-cli-pi:latest",
                        selection=WorkspaceSelection(workspace="/work/project"),
                        pi_home_host="/home/user/.pi",
                        dry_run=True,
                        executor=_bomb_docker(),
                    project_root=Path(str(inv)).resolve().parent),
                )
            self.assertEqual(ExitKind.SUCCESS, dry_result.exit_kind)
            self.assertFalse(mocked_doctor.called,
                             "doctor must not be called for disabled host access")
            self.assertFalse(mocked_build_orch_gateway.called,
                             "build-orch diagnose_gateway must not be called for disabled host access")
            self.assertFalse(mocked_gateway.called,
                             "gateway diagnosis must not be called for disabled host access")
            vector = getattr(dry_result, "display_string", "") or ""
            self.assertNotIn(
                "--add-host", vector,
                "disabled host access must not emit --add-host",
            )
            self.assertNotIn(
                "HOST_ACCESS_ADDRESS", vector,
                "disabled host access must not emit HOST_ACCESS_ADDRESS",
            )
            self.assertNotIn(
                "HOST_PROXY_PORT", vector,
                "disabled host access must not emit HOST_PROXY_PORT",
            )

    def test_disabled_run_needs_no_local_companion(self):
        """With disabled host access the launcher requires neither a
        local companion file nor any gateway probe."""
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            inv = _write_inventory(root_path)

            from docker.launcher import orchestrate_run
            from docker.launcher import WorkspaceSelection

            with mock.patch(
                "docker.versioning.build_orchestration.orchestrate_doctor",
                side_effect=_bomb_doctor,
            ) as mocked_doctor, mock.patch(
                "docker.versioning.build_orchestration.diagnose_gateway",
                side_effect=_bomb_gateway,
            ) as mocked_build_orch_gateway, mock.patch(
                "docker.networking.diagnose_gateway",
                side_effect=_bomb_gateway,
            ) as mocked_gateway:
                result = orchestrate_run(
                    RunRequest(
                        inventory_path=str(inv),
                        image="pi-cli-pi:latest",
                        selection=WorkspaceSelection(workspace="/work/project"),
                        pi_home_host="/home/user/.pi",
                        dry_run=True,
                        executor=_bomb_docker(),
                    project_root=Path(str(inv)).resolve().parent),
                )
            self.assertEqual(
                ExitKind.SUCCESS, result.exit_kind,
                "disabled launch must succeed without local companion",
            )
            self.assertFalse(mocked_doctor.called,
                             "doctor must not be called for disabled host access")
            self.assertFalse(mocked_build_orch_gateway.called,
                             "build-orch diagnose_gateway must not be called for disabled host access")
            self.assertFalse(mocked_gateway.called,
                             "gateway diagnosis must not be called for disabled host access")


# ═══════════════════════════════════════════════════════════════════════
# 6.2  Docker-gateway mode — doctor → run → verify chain
# ═══════════════════════════════════════════════════════════════════════

class _VerifyRuntimeRunner:
    """Fake process runner for ``verify_runtime`` integration.

    Returns scripted responses for every ``docker exec …`` command
    the verifier issues so the entire check pass succeeds when the
    host-access expectations match."""

    def __init__(
        self,
        *,
        container: str,
        proj_hash: str,
        proj_mount_line: str,
        pi_home: str,
        project_path: str,
        gateway_ip: str,
    ) -> None:
        self._container = container
        self._proj_hash = proj_hash
        self._proj_mount_line = proj_mount_line
        self._pi_home = pi_home
        self._project_path = project_path
        self._gateway_ip = gateway_ip
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: tuple[str, ...], *,
            interactive: bool = False) -> "Any":
        from docker.versioning.runtime_verification import ProcessResult

        self.calls.append(argv)
        cmd = " ".join(argv)

        # Per-command dispatch — every key the real verifier probes.
        P = ProcessResult

        # projection.identity — sha256sum of projection
        if "sha256sum" in cmd and "docker-constructor.runtime.toml" in cmd:
            return P(argv=argv, return_code=0,
                     stdout=f"{self._proj_hash}  /run/.../runtime.toml\n",
                     stderr="")

        # projection.readonly — grep /proc/mounts
        if "grep" in cmd and "docker-constructor.runtime.toml" in cmd:
            return P(argv=argv, return_code=0,
                     stdout=self._proj_mount_line + "\n", stderr="")

        # projection.readonly — test -w (must fail to prove read-only)
        if "test" in cmd and "-w" in cmd and "docker-constructor.runtime.toml" in cmd:
            return P(argv=argv, return_code=1, stdout="", stderr="")

        # projects.present — test -d WORKSPACE_PATH_1
        if "test" in cmd and "-d" in cmd and self._project_path in cmd:
            return P(argv=argv, return_code=0, stdout="", stderr="")

        # projects.present — printenv WORKSPACE_PATH_1
        if "printenv" in cmd and "WORKSPACE_PATH_1" in cmd and "WORKSPACE_PATH_2" not in cmd:
            return P(argv=argv, return_code=0,
                     stdout=self._project_path, stderr="")

        # projects.present — printenv WORKSPACE_PATH_2 (must be absent)
        if "printenv" in cmd and "WORKSPACE_PATH_2" in cmd:
            return P(argv=argv, return_code=1, stdout="", stderr="")

        # working.directory — pwd
        if "pwd" in cmd:
            return P(argv=argv, return_code=0,
                     stdout=self._project_path, stderr="")

        # ownership.dev — stat pi_home
        if "stat" in cmd and self._pi_home in cmd:
            return P(argv=argv, return_code=0,
                     stdout="dev:dev", stderr="")

        # ownership.dev — stat project_path
        if "stat" in cmd and self._project_path in cmd:
            return P(argv=argv, return_code=0,
                     stdout="dev:dev", stderr="")

        # pi-home.setup — test -d pi_home
        if "test" in cmd and "-d" in cmd and self._pi_home in cmd:
            return P(argv=argv, return_code=0, stdout="", stderr="")

        # pi-home.setup — test -w pi_home
        if "test" in cmd and "-w" in cmd and self._pi_home in cmd:
            return P(argv=argv, return_code=0, stdout="", stderr="")

        # gateway.mapping — getent hosts host.docker.internal
        if "getent" in cmd and "host.docker.internal" in cmd:
            return P(argv=argv, return_code=0,
                     stdout=f"{self._gateway_ip} host.docker.internal\n",
                     stderr="")

        # host-access.address — printenv HOST_ACCESS_ADDRESS
        if "printenv" in cmd and "HOST_ACCESS_ADDRESS" in cmd:
            return P(argv=argv, return_code=0,
                     stdout=self._gateway_ip, stderr="")

        # corporate-trust.mount (disabled) — exact-mountpoint awk probe
        # must report no options so the disabled contract passes.
        if "awk" in cmd and "ca-certificates.crt" in cmd:
            return P(argv=argv, return_code=0, stdout="", stderr="")

        # proxy.environment (disabled) — keyed printenv must be unset.
        if "printenv" in cmd and any(
            name in cmd for name in (
                "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
                "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy",
            )
        ):
            return P(argv=argv, return_code=1, stdout="", stderr="")

        # forbidden.paths — test -f (must be absent → rc != 0)
        if "test" in cmd and "-f" in cmd:
            return P(argv=argv, return_code=1, stdout="", stderr="")

        # Unknown command — fail loudly
        raise AssertionError(
            f"Unexpected docker exec command: {argv}"
        )


class TestDockerGatewayEndToEnd(unittest.TestCase):
    """6.2: Configure Docker-gateway mode, record a doctor-selected
    address, prove later run and verification consume that exact
    address."""

    def test_doctor_persists_address_and_run_verification_consume_it(self):
        gateway_ip = "10.0.2.2"

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            inv = _write_inventory(
                root_path,
                policy='[runtime.host-access]\nenabled = true\nmode = "docker-gateway"\n',
            )
            companion = root_path / "docker-constructor.local.toml"

            # ── Doctor ──
            doctor_result = orchestrate_doctor(
                DoctorRequest(
                    inventory_path=inv,
                    _diagnose_gateway=_make_fake_diagnose(ip=gateway_ip),
                ),
            )
            self.assertEqual(
                ExitKind.SUCCESS, doctor_result.exit_kind,
                f"doctor must succeed: {doctor_result.message}",
            )
            self.assertEqual(
                gateway_ip, doctor_result.selected_gateway,
            )
            self.assertTrue(companion.is_file(), "doctor must persist local companion")

            # Check that companion contains the address
            raw = companion.read_text()
            self.assertIn(f'address = "{gateway_ip}"', raw)

            # ── Run (dry) — consumes the persisted address ──
            from docker.launcher import orchestrate_run
            from docker.launcher import WorkspaceSelection

            run_result = orchestrate_run(
                RunRequest(
                    inventory_path=str(inv),
                    image="pi-cli-pi:latest",
                    selection=WorkspaceSelection(workspace="/work/project"),
                    pi_home_host="/home/user/.pi",
                    dry_run=True,
                    executor=_bomb_docker(),
                project_root=Path(str(inv)).resolve().parent),
            )
            self.assertEqual(ExitKind.SUCCESS, run_result.exit_kind)
            vector = getattr(run_result, "display_string", "") or ""
            self.assertIn(
                f"host.docker.internal:{gateway_ip}", vector,
                "run vector must map host.docker.internal to the persisted address",
            )
            self.assertIn(
                f"HOST_ACCESS_ADDRESS={gateway_ip}", vector,
                "run vector must export HOST_ACCESS_ADDRESS",
            )

            # ── Verify — production resolution + runtime verification ──
            # Use the same facade path that the CLI verification
            # command traverses: load inventory → resolve companion →
            # derive host_access + address for VerifyRuntimeRequest.
            from docker.constructor_cli import _resolve_verify_host_access
            from docker.versioning.runtime_verification import (
                VerifyRuntimeRequest,
                verify_runtime,
            )

            ha, resolved_addr, resolve_err = _resolve_verify_host_access(
                str(inv),
            )
            self.assertIsNone(
                resolve_err,
                f"_resolve_verify_host_access must succeed: {resolve_err}",
            )
            self.assertIsNotNone(
                ha, "_resolve_verify_host_access must return enabled policy",
            )
            self.assertEqual(
                gateway_ip, resolved_addr,
                "resolved address must equal the doctor-persisted value",
            )

            # Build a projection file the verifier will hash-compare
            import hashlib

            proj = root_path / "runtime-projection.toml"
            proj_content = (
                "[extensions]\n"
                "[workspace_paths]\n"
                'paths = ["/work/project"]\n'
            )
            proj.write_text(proj_content)
            proj_hash = hashlib.sha256(proj_content.encode()).hexdigest()

            container = "pi-test"
            pi_home = "/home/dev/.pi"
            proj_mount_line = (
                f"/dev/sda1 /run/pi-cli/docker-constructor.runtime.toml"
                f" ext4 ro,nosuid,nodev,relatime 0 0"
            )

            runner = _VerifyRuntimeRunner(
                container=container,
                proj_hash=proj_hash,
                proj_mount_line=proj_mount_line,
                pi_home=pi_home,
                project_path="/work/project",
                gateway_ip=gateway_ip,
            )

            verify_result = verify_runtime(
                VerifyRuntimeRequest(
                    container=container,
                    runtime_projection_path=proj,
                    workspace_paths=(Path("/work/project"),),
                    container_pi_home=Path(pi_home),
                    runner=runner,
                    host_access=ha,
                    host_access_address=resolved_addr,
                ),
            )
            self.assertTrue(
                verify_result.all_ok,
                f"verification must pass; failures={verify_result.checks}",
            )



# ═══════════════════════════════════════════════════════════════════════
# 6.3  External-address mode + proxy port
# ═══════════════════════════════════════════════════════════════════════

class TestExternalAddressEndToEnd(unittest.TestCase):
    """6.3: External-address mode plus proxy port.  Run emits the
    hostname mapping, ``HOST_ACCESS_ADDRESS``, and ``HOST_PROXY_PORT``
    without invoking doctor boundaries."""

    def test_external_address_with_proxy_port(self):
        address = "192.168.1.100"
        port = 1080

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            inv = _write_inventory(
                root_path,
                policy=(
                    '[runtime.host-access]\n'
                    'enabled = true\n'
                    'mode = "external-address"\n'
                    f"proxy-port = {port}\n"
                ),
            )
            # Write local companion with the external address
            companion = root_path / "docker-constructor.local.toml"
            companion.write_text(
                f'[host-access]\naddress = "{address}"\n'
            )

            from docker.launcher import orchestrate_run
            from docker.launcher import WorkspaceSelection

            with mock.patch(
                "docker.versioning.build_orchestration.orchestrate_doctor",
                side_effect=_bomb_doctor,
            ) as mocked_doctor, mock.patch(
                "docker.versioning.build_orchestration.diagnose_gateway",
                side_effect=_bomb_gateway,
            ) as mocked_build_orch_gateway, mock.patch(
                "docker.networking.diagnose_gateway",
                side_effect=_bomb_gateway,
            ) as mocked_gateway, mock.patch(
                "docker.versioning.build_orchestration.plan_rootless_override",
                side_effect=_bomb_rootless_plan,
            ) as mocked_rootless_plan, mock.patch(
                "docker.versioning.build_orchestration.apply_rootless_override",
                side_effect=_bomb_rootless_apply,
            ) as mocked_rootless_apply:
                run_result = orchestrate_run(
                    RunRequest(
                        inventory_path=str(inv),
                        image="pi-cli-pi:latest",
                        selection=WorkspaceSelection(workspace="/work/project"),
                        pi_home_host="/home/user/.pi",
                        dry_run=True,
                        executor=_bomb_docker(),
                    project_root=Path(str(inv)).resolve().parent),
                )
            self.assertEqual(ExitKind.SUCCESS, run_result.exit_kind)
            self.assertFalse(mocked_doctor.called,
                             "doctor must not be called for external-address run")
            self.assertFalse(mocked_build_orch_gateway.called,
                             "build-orch diagnose_gateway must not be called for external-address run")
            self.assertFalse(mocked_gateway.called,
                             "gateway diagnosis must not be called for external-address run")
            self.assertFalse(mocked_rootless_plan.called,
                             "rootless-override planning must not be called for external-address run")
            self.assertFalse(mocked_rootless_apply.called,
                             "rootless-override application must not be called for external-address run")
            vector = getattr(run_result, "display_string", "") or ""
            self.assertIn(
                f"host.docker.internal:{address}", vector,
                "external-address run must map host.docker.internal",
            )
            self.assertIn(
                f"HOST_ACCESS_ADDRESS={address}", vector,
                "external-address run must export HOST_ACCESS_ADDRESS",
            )
            self.assertIn(
                f"HOST_PROXY_PORT={port}", vector,
                "external-address run must export HOST_PROXY_PORT",
            )

    def test_external_address_doctor_no_op(self):
        """Doctor must not probe or persist for external-address mode."""
        address = "192.168.1.100"

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            inv = _write_inventory(
                root_path,
                policy=(
                    '[runtime.host-access]\n'
                    'enabled = true\n'
                    'mode = "external-address"\n'
                ),
            )
            companion = root_path / "docker-constructor.local.toml"
            companion.write_text(
                f'[host-access]\naddress = "{address}"\n'
            )
            original = companion.read_text()

            doctor_result = orchestrate_doctor(
                DoctorRequest(
                    inventory_path=inv,
                    _diagnose_gateway=_bomb_gateway,
                    _plan_rootless_override=_bomb_rootless_plan,
                    _apply_rootless_override=_bomb_rootless_apply,
                ),
            )
            self.assertEqual(ExitKind.SUCCESS, doctor_result.exit_kind)
            self.assertIn(
                "external-address", doctor_result.message or "",
                "doctor must report external-address skip",
            )
            # Local companion must remain unchanged
            self.assertEqual(
                original, companion.read_text(),
                "local companion must not be altered by doctor in external-address mode",
            )


# ═══════════════════════════════════════════════════════════════════════
# 6.4  Custom inventory path + fixed local companion + cache
# ═══════════════════════════════════════════════════════════════════════

class TestCustomInventoryWithCache(unittest.TestCase):
    """6.4: Custom inventory path, fixed local companion, reviewed cache
    TTL, and local cache directory — no repository-state fallback."""

    def test_custom_inventory_with_local_cache(self):
        custom_ttl = 7200
        custom_address = "10.0.2.2"

        # Conflicting repository-root values — must be ignored
        repo_address = "192.168.99.1"

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            custom_cache_dir = str(root_path / "custom-constructor-cache")
            repo_cache_dir = str(root_path / "ignored-repo-cache")
            repo_root = root_path / "repo"
            repo_root.mkdir()
            stem = "custom-inv"

            # Build an inventory with an explicit reviewed cache.ttl.
            # The real docker-constructor.toml has ``# ttl = 3600``
            # commented out, so we uncomment it with a custom value.
            import re

            raw = _REAL_INVENTORY
            raw = re.sub(r"^# ttl = \d+$", f"ttl = {custom_ttl}", raw, flags=re.M)
            inv = repo_root / f"{stem}.toml"
            inv.write_text(
                raw + "\n"
                '[runtime.host-access]\n'
                'enabled = true\n'
                'mode = "docker-gateway"\n',
            )

            # The fixed local companion beside the custom inventory.
            local = repo_root / "docker-constructor.local.toml"
            local.write_text(
                f'[host-access]\naddress = "{custom_address}"\n'
                f'[cache]\ndir = "{custom_cache_dir}"\n',
            )

            # Repository-root local companion (canonical name) —
            # must NEVER be consulted for a custom inventory.
            repo_local = root_path / "docker-constructor.local.toml"
            repo_local.write_text(
                f'[host-access]\naddress = "{repo_address}"\n'
                f'[cache]\ndir = "{repo_cache_dir}"\n',
            )

            # ── Assertions ──────────────────────────────────────────

            # 0. resolve_local_companion_path points at the fixed
            #    companion, not the parent-directory one
            from docker.versioning.inventory import (
                resolve_local_companion_path,
            )

            resolved = resolve_local_companion_path(inv)
            self.assertEqual(
                local.resolve(), resolved.resolve(),
                "companion path must use the fixed basename beside"
                " the custom inventory, not its parent directory.",
            )
            self.assertNotEqual(
                repo_local.resolve(), resolved.resolve(),
                "repository-root companion must never be resolved"
                " for a custom inventory",
            )

            # 1. Reviewed inventory has the explicit TTL
            from docker.versioning.inventory import load_inventory

            reviewed = load_inventory(inv)
            self.assertIsNotNone(reviewed.cache)
            assert reviewed.cache is not None
            self.assertEqual(
                custom_ttl, reviewed.cache.ttl,
                "reviewed cache.ttl must be the explicit custom value",
            )
            self.assertIsNone(
                getattr(reviewed.cache, "dir", None),
                "reviewed cache.dir must not exist",
            )

            # 2. load_local_config_for_inventory uses only the fixed
            #    same-directory companion, not the parent-directory one
            from docker.versioning.local_project_configuration import (
                load_optional_local_project_configuration as load_local_config_for_inventory,
            )

            local_cfg = load_local_config_for_inventory(inv)
            self.assertIsNotNone(local_cfg)
            assert local_cfg is not None
            self.assertEqual(
                custom_address, local_cfg.host_access.address,
                "custom local companion address must be used",
            )
            self.assertNotEqual(
                repo_address, local_cfg.host_access.address,
                "repository-root address must not leak into local config",
            )
            self.assertEqual(
                custom_cache_dir, local_cfg.cache.dir,
                f"custom cache dir {custom_cache_dir!r} must be used,"
                f" not repo {repo_cache_dir!r}",
            )
            self.assertNotEqual(
                repo_cache_dir, local_cfg.cache.dir,
                "repository-root cache dir must not leak into local config",
            )

            # 3. Run resolves the custom companion, not the repo one
            from docker.launcher import orchestrate_run
            from docker.launcher import WorkspaceSelection

            run_result = orchestrate_run(
                RunRequest(
                    inventory_path=str(inv),
                    image="pi-cli-pi:latest",
                    selection=WorkspaceSelection(workspace="/work/project"),
                    pi_home_host="/home/user/.pi",
                    dry_run=True,
                    executor=_bomb_docker(),
                project_root=Path(str(inv)).resolve().parent),
            )
            self.assertEqual(ExitKind.SUCCESS, run_result.exit_kind)
            vector = getattr(run_result, "display_string", "") or ""
            self.assertIn(
                f"host.docker.internal:{custom_address}", vector,
                "custom inventory must resolve its own local companion,"
                f" not repo ({repo_address})",
            )
            self.assertNotIn(
                repo_address, vector,
                "repository-root companion must not leak into run",
            )


# ═══════════════════════════════════════════════════════════════════════
# 6.5  Invalid local state — no side effects
# ═══════════════════════════════════════════════════════════════════════

class TestInvalidLocalStateSafety(unittest.TestCase):
    """6.5: Invalid local state must cause failure **before** any
    download, cache mutation, projection publication, Docker execution,
    systemd mutation, or reviewed-source mutation."""

    # ── shared guard helpers ──────────────────────────────────────

    class _RecordingExecutor:
        """Callable with a ``.run()`` that records and returns a
        dummy result — proves Docker was never reached."""

        def __init__(self, effects: list[str]) -> None:
            self._effects = effects

        def run(self, argv: tuple[str, ...], *,
                interactive: bool = False) -> Any:
            from docker.launcher import ProcessResult
            self._effects.append("docker-execution")
            return ProcessResult(argv=argv, return_code=0,
                                 stdout="", stderr="")

    class _RecordingInspector:
        """Records container-name-list calls."""

        def __init__(self, effects: list[str]) -> None:
            self._effects = effects

        def list_names(self) -> list[str]:
            self._effects.append("container-inspection")
            return []

    @staticmethod
    def _record_projection(effects: list[str]):
        def _fn(*_args, **_kwargs):
            effects.append("runtime-projection-creation")
            return None
        return _fn

    @staticmethod
    def _record_artifact_fetch(effects: list[str]):
        def _fn(*_args, **_kwargs):
            effects.append("artifact-download-materialization")
            return []
        return _fn

    @staticmethod
    def _record_materialize(effects: list[str]):
        def _fn(*_args, **_kwargs):
            effects.append("cache-publication")
            return []
        return _fn

    @staticmethod
    def _record_rootless_apply(effects: list[str]):
        def _fn(*_args, **_kwargs):
            effects.append("rootless-override-apply")
            return None
        return _fn

    @staticmethod
    def _record_rootless_plan(effects: list[str]):
        def _fn(*_args, **_kwargs):
            effects.append("rootless-override-plan")
            return None
        return _fn

    @staticmethod
    def _record_diagnose(effects: list[str]):
        def _fn(*_args, **_kwargs):
            effects.append("gateway-diagnosis")
            return None
        return _fn

    def _assert_no_side_effects(self, effects: list[str],
                                reviewed_bytes: bytes,
                                inv: Path) -> None:
        """Assert no side-effect guard was triggered and reviewed
        source is byte-for-byte unchanged."""
        self.assertEqual(
            [], effects,
            f"side effects triggered before failure: {effects}",
        )
        self.assertEqual(
            reviewed_bytes, inv.read_bytes(),
            "reviewed inventory must be byte-for-byte unchanged",
        )

    # ── 6.5.1  Malformed local companion ───────────────────────────

    def test_invalid_local_state_fails_before_all_side_effects(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            inv = _write_inventory(
                root_path,
                policy='[runtime.host-access]\nenabled = true\nmode = "docker-gateway"\n',
            )
            reviewed_bytes = inv.read_bytes()

            # Malformed local companion
            companion = root_path / "docker-constructor.local.toml"
            companion.write_text("{{{ not toml }}}\n")

            from docker.launcher import orchestrate_run
            from docker.launcher import WorkspaceSelection

            effects: list[str] = []

            with (
                mock.patch(
                    "docker.launcher.artifact_cache.materialize_selected_artifacts",
                    side_effect=self._record_materialize(effects),
                ),
                mock.patch(
                    "docker.networking.apply_rootless_override",
                    side_effect=self._record_rootless_apply(effects),
                ),
                mock.patch(
                    "docker.networking.plan_rootless_override",
                    side_effect=self._record_rootless_plan(effects),
                ),
                mock.patch(
                    "docker.networking.diagnose_gateway",
                    side_effect=self._record_diagnose(effects),
                ),
            ):
                result = orchestrate_run(
                    RunRequest(
                        inventory_path=str(inv),
                        image="pi-cli-pi:latest",
                        selection=WorkspaceSelection(workspace="/work/project"),
                        pi_home_host="/home/user/.pi",
                        dry_run=False,
                        executor=self._RecordingExecutor(effects),
                        inspector=self._RecordingInspector(effects),
                        _create_projection=self._record_projection(effects),
                        _artifact_fetcher=self._record_artifact_fetch(effects),
                    project_root=Path(str(inv)).resolve().parent),
                )

            self.assertNotEqual(
                ExitKind.SUCCESS, result.exit_kind,
                "malformed local companion must produce a failure exit",
            )
            self._assert_no_side_effects(effects, reviewed_bytes, inv)

    # ── 6.5.2  Missing host-access address ─────────────────────────

    def test_missing_address_for_enabled_mode_fails_safely(self):
        """Enabled docker-gateway with a valid but empty local companion
        (no address) must fail before effects."""
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            inv = _write_inventory(
                root_path,
                policy='[runtime.host-access]\nenabled = true\nmode = "docker-gateway"\n',
            )
            reviewed_bytes = inv.read_bytes()

            # Local companion present but no host-access section
            companion = root_path / "docker-constructor.local.toml"
            companion.write_text("[cache]\ndir = \"/tmp/cache\"\n")

            from docker.launcher import orchestrate_run
            from docker.launcher import WorkspaceSelection

            effects: list[str] = []

            with (
                mock.patch(
                    "docker.launcher.artifact_cache.materialize_selected_artifacts",
                    side_effect=self._record_materialize(effects),
                ),
                mock.patch(
                    "docker.networking.apply_rootless_override",
                    side_effect=self._record_rootless_apply(effects),
                ),
                mock.patch(
                    "docker.networking.plan_rootless_override",
                    side_effect=self._record_rootless_plan(effects),
                ),
                mock.patch(
                    "docker.networking.diagnose_gateway",
                    side_effect=self._record_diagnose(effects),
                ),
            ):
                result = orchestrate_run(
                    RunRequest(
                        inventory_path=str(inv),
                        image="pi-cli-pi:latest",
                        selection=WorkspaceSelection(workspace="/work/project"),
                        pi_home_host="/home/user/.pi",
                        dry_run=False,
                        executor=self._RecordingExecutor(effects),
                        inspector=self._RecordingInspector(effects),
                        _create_projection=self._record_projection(effects),
                        _artifact_fetcher=self._record_artifact_fetch(effects),
                    project_root=Path(str(inv)).resolve().parent),
                )

            self.assertNotEqual(
                ExitKind.SUCCESS, result.exit_kind,
                "missing address for enabled mode must fail",
            )
            self._assert_no_side_effects(effects, reviewed_bytes, inv)

    # ── 6.5.3  Companion without [host-access].address ──────────────

    def test_companion_without_host_access_address_returns_error(self):
        """A local companion that exists but omits [host-access].address
        must return an actionable error from _resolve_verify_host_access
        — no UnboundLocalError, no crash."""
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            inv = _write_inventory(
                root_path,
                policy='[runtime.host-access]\nenabled = true\nmode = "docker-gateway"\n',
            )
            # Companion exists but with only [cache].dir — no host-access
            companion = root_path / "docker-constructor.local.toml"
            companion.write_text('[cache]\ndir = "/tmp/cache"\n')

            from docker.constructor_cli import _resolve_verify_host_access

            ha, address, error = _resolve_verify_host_access(str(inv))

            # Must not crash with UnboundLocalError
            self.assertIsNotNone(error, "must return an actionable error")
            assert error is not None
            self.assertIn(
                "[host-access].address", error,
                "error must mention the missing key",
            )
            self.assertIn(
                "doctor", error.lower(),
                "error must suggest running doctor",
            )
            # Policy and address must be absent when there's an error
            self.assertIsNone(ha)
            self.assertIsNone(address)

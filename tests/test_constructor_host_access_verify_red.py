"""RED tests for Phase 4: policy-aware runtime verification.

Proves that ``verify_runtime`` respects the reviewed host-access
policy instead of unconditionally checking ``host.docker.internal``
against a legacy ``.env`` value.

All tests use fake process runners — no Docker required.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from docker.versioning.model import HostAccessPolicy
from docker.versioning.runtime_verification import (
    VerifyRuntimeRequest,
    verify_runtime,
)


_CANONICAL_INVENTORY = (
    Path(__file__).resolve().parents[1] / "docker-constructor.toml"
).read_text(encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════
# Minimal fake runner
# ═══════════════════════════════════════════════════════════════════════

def _rf(argv: tuple[str, ...], rc: int = 0, stdout: str = "",
         stderr: str = "") -> Any:
    """Return a fake process result."""
    from docker.versioning.runtime_verification import ProcessResult
    return ProcessResult(argv=argv, return_code=rc,
                         stdout=stdout, stderr=stderr)


class _FakeRunner:
    """Callable that returns a canned response for each argv."""

    def __init__(self, responses: dict[tuple[str, ...], Any]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: tuple[str, ...]) -> Any:
        self.calls.append(argv)
        for pattern, result in self._responses.items():
            if all(p in argv for p in pattern):
                return result
        return _rf(argv, rc=0, stdout="")


# ═══════════════════════════════════════════════════════════════════════
# Canonical test helpers
# ═══════════════════════════════════════════════════════════════════════

def _make_projection(path: Path) -> Path:
    """Write a minimal effective runtime projection file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '[extensions]\n'
        '[workspace_paths]\n'
        'paths = []\n'
        '[pi_home]\n'
        'path = "/home/dev/.pi"\n'
        '[gateway]\n'
        'address = "192.168.65.1"\n'
        '[integrity]\n'
        'sha256 = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="\n'
    )
    return path


def _projection_identity_response(proj_path: Path) -> Any:
    """Return responses to satisfy projection.identity and read-only checks."""
    import hashlib
    h = hashlib.sha256(proj_path.read_bytes()).hexdigest()
    return _rf(("sha256sum",), rc=0,
               stdout=f"{h}  /run/pi-cli/docker-constructor.runtime.toml\n")


def _projection_readonly_responses() -> tuple[Any, Any]:
    """Return responses for /proc/mounts grep and test -w."""
    return (
        _rf(("grep",), rc=0,
            stdout="/dev/sda1 /run/pi-cli/docker-constructor.runtime.toml ext4 ro,nosuid,nodev,relatime 0 0\n"),
        _rf(("test", "-w", "/run/pi-cli/docker-constructor.runtime.toml"), rc=1),
    )


def _base_responses(proj: Path) -> dict[tuple[str, ...], Any]:
    """Responses for the non-host-access checks that every test needs."""
    return {
        ("sha256sum", "/run/pi-cli/docker-constructor.runtime.toml"):
            _projection_identity_response(proj),
        ("grep", "docker-constructor.runtime.toml", "/proc/mounts"):
            _projection_readonly_responses()[0],
        ("test", "-w", "/run/pi-cli/docker-constructor.runtime.toml"):
            _projection_readonly_responses()[1],
        ("stat", "-c", "%U:%G"): _rf(("stat",), rc=0, stdout="dev:dev\n"),
        ("test", "-d", "/home/dev/.pi"): _rf(("test",), rc=0),
        ("test", "-w", "/home/dev/.pi"): _rf(("test",), rc=0),
        ("test", "-f",): _rf(("test",), rc=1),
    }


# ═══════════════════════════════════════════════════════════════════════
# 4.1  Enabled host-access verification (address + hostname)
# ═══════════════════════════════════════════════════════════════════════

class TestEnabledHostAccessVerificationRed(unittest.TestCase):
    """Enabled host access must verify exact ``host.docker.internal``
    resolution and exact ``HOST_ACCESS_ADDRESS`` equality."""

    def test_docker_gateway_verifies_hostname_and_env(self) -> None:
        """When host access is enabled, verifier must check hostname
        resolution and HOST_ACCESS_ADDRESS inside the container."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            proj = _make_projection(Path(tmp) / "proj.toml")
            responses = dict(_base_responses(proj))
            responses.update({
                ("getent", "hosts", "host.docker.internal"):
                    _rf(("getent",), rc=0,
                        stdout="10.0.2.2  host.docker.internal\n"),
                ("printenv", "HOST_ACCESS_ADDRESS"):
                    _rf(("printenv",), rc=0, stdout="10.0.2.2\n"),
            })
            runner = _FakeRunner(responses)

            request = VerifyRuntimeRequest(
                container="pi-test",
                runtime_projection_path=proj,
                workspace_paths=(),
                container_pi_home=Path("/home/dev/.pi"),
                runner=runner,
                host_access=HostAccessPolicy(
                    enabled=True, mode="docker-gateway", proxy_port=None,
                ),
                host_access_address="10.0.2.2",
            )

            result = verify_runtime(request)
            gm = [c for c in result.checks if c.key == "gateway.mapping"]
            self.assertTrue(gm, "gateway.mapping check must be present")
            self.assertTrue(gm[0].ok,
                            f"gateway.mapping must pass, got: {gm[0].detail}")
            ha = [c for c in result.checks if c.key == "host-access.address"]
            self.assertTrue(ha, "host-access.address check must be present")
            self.assertTrue(ha[0].ok,
                            f"host-access.address must pass, got: {ha[0].detail}")

    def test_docker_gateway_hostname_mismatch_fails(self) -> None:
        """When resolution returns a different address, check fails."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            proj = _make_projection(Path(tmp) / "proj.toml")
            responses = dict(_base_responses(proj))
            responses.update({
                ("getent", "hosts", "host.docker.internal"):
                    _rf(("getent",), rc=0,
                        stdout="192.168.99.1  host.docker.internal\n"),
                ("printenv", "HOST_ACCESS_ADDRESS"):
                    _rf(("printenv",), rc=0, stdout="10.0.2.2\n"),
            })
            runner = _FakeRunner(responses)

            request = VerifyRuntimeRequest(
                container="pi-test",
                runtime_projection_path=proj,
                workspace_paths=(),
                container_pi_home=Path("/home/dev/.pi"),
                runner=runner,
                host_access=HostAccessPolicy(
                    enabled=True, mode="docker-gateway", proxy_port=None,
                ),
                host_access_address="10.0.2.2",
            )

            result = verify_runtime(request)
            gm = [c for c in result.checks if c.key == "gateway.mapping"]
            self.assertTrue(gm, "gateway.mapping check must be present")
            self.assertFalse(gm[0].ok,
                             "gateway.mapping must fail for mismatched address")

    def test_external_address_verifies_hostname_and_env(self) -> None:
        """External-address mode also verifies resolution and env."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            proj = _make_projection(Path(tmp) / "proj.toml")
            responses = dict(_base_responses(proj))
            responses.update({
                ("getent", "hosts", "host.docker.internal"):
                    _rf(("getent",), rc=0,
                        stdout="203.0.113.5  host.docker.internal\n"),
                ("printenv", "HOST_ACCESS_ADDRESS"):
                    _rf(("printenv",), rc=0, stdout="203.0.113.5\n"),
            })
            runner = _FakeRunner(responses)

            request = VerifyRuntimeRequest(
                container="pi-test",
                runtime_projection_path=proj,
                workspace_paths=(),
                container_pi_home=Path("/home/dev/.pi"),
                runner=runner,
                host_access=HostAccessPolicy(
                    enabled=True, mode="external-address", proxy_port=None,
                ),
                host_access_address="203.0.113.5",
            )

            result = verify_runtime(request)
            gm = [c for c in result.checks if c.key == "gateway.mapping"]
            self.assertTrue(gm, "gateway.mapping check must be present")
            self.assertTrue(gm[0].ok,
                            f"gateway.mapping must pass, got: {gm[0].detail}")
            ha = [c for c in result.checks if c.key == "host-access.address"]
            self.assertTrue(ha, "host-access.address check must be present")
            self.assertTrue(ha[0].ok,
                            f"host-access.address must pass, got: {ha[0].detail}")


# ═══════════════════════════════════════════════════════════════════════
# 4.2  Proxy-port verification
# ═══════════════════════════════════════════════════════════════════════

class TestProxyPortVerificationRed(unittest.TestCase):
    """Configured proxy port must be verified; omitted proxy port
    must not create any positive port requirement."""

    def test_proxy_port_set_verifies_env(self) -> None:
        """When ``proxy-port`` is configured, the runtime check must
        verify that ``HOST_PROXY_PORT`` equals the configured value."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            proj = _make_projection(Path(tmp) / "proj.toml")
            responses = dict(_base_responses(proj))
            responses.update({
                ("getent", "hosts", "host.docker.internal"):
                    _rf(("getent",), rc=0,
                        stdout="10.0.2.2  host.docker.internal\n"),
                ("printenv", "HOST_ACCESS_ADDRESS"):
                    _rf(("printenv",), rc=0, stdout="10.0.2.2\n"),
                ("printenv", "HOST_PROXY_PORT"):
                    _rf(("printenv",), rc=0, stdout="1080\n"),
            })
            runner = _FakeRunner(responses)

            request = VerifyRuntimeRequest(
                container="pi-test",
                runtime_projection_path=proj,
                workspace_paths=(),
                container_pi_home=Path("/home/dev/.pi"),
                runner=runner,
                host_access=HostAccessPolicy(
                    enabled=True, mode="docker-gateway", proxy_port=1080,
                ),
                host_access_address="10.0.2.2",
            )

            result = verify_runtime(request)
            pp = [c for c in result.checks if c.key == "host-access.proxy-port"]
            self.assertTrue(pp, "host-access.proxy-port check must be present")
            self.assertTrue(pp[0].ok,
                            f"proxy-port check must pass, got: {pp[0].detail}")

    def test_proxy_port_mismatch_fails(self) -> None:
        """When ``HOST_PROXY_PORT`` does not match, check fails."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            proj = _make_projection(Path(tmp) / "proj.toml")
            responses = dict(_base_responses(proj))
            responses.update({
                ("getent", "hosts", "host.docker.internal"):
                    _rf(("getent",), rc=0,
                        stdout="10.0.2.2  host.docker.internal\n"),
                ("printenv", "HOST_ACCESS_ADDRESS"):
                    _rf(("printenv",), rc=0, stdout="10.0.2.2\n"),
                ("printenv", "HOST_PROXY_PORT"):
                    _rf(("printenv",), rc=0, stdout="9999\n"),
            })
            runner = _FakeRunner(responses)

            request = VerifyRuntimeRequest(
                container="pi-test",
                runtime_projection_path=proj,
                workspace_paths=(),
                container_pi_home=Path("/home/dev/.pi"),
                runner=runner,
                host_access=HostAccessPolicy(
                    enabled=True, mode="docker-gateway", proxy_port=1080,
                ),
                host_access_address="10.0.2.2",
            )

            result = verify_runtime(request)
            pp = [c for c in result.checks if c.key == "host-access.proxy-port"]
            self.assertTrue(pp, "host-access.proxy-port check must be present")
            self.assertFalse(pp[0].ok,
                             "proxy-port check must fail for mismatch")

    def test_no_proxy_port_no_positive_port_requirement(self) -> None:
        """When proxy-port is omitted, the verifier must NOT check for
        HOST_PROXY_PORT at all."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            proj = _make_projection(Path(tmp) / "proj.toml")
            responses = dict(_base_responses(proj))
            responses.update({
                ("getent", "hosts", "host.docker.internal"):
                    _rf(("getent",), rc=0,
                        stdout="10.0.2.2  host.docker.internal\n"),
                ("printenv", "HOST_ACCESS_ADDRESS"):
                    _rf(("printenv",), rc=0, stdout="10.0.2.2\n"),
            })
            runner = _FakeRunner(responses)

            request = VerifyRuntimeRequest(
                container="pi-test",
                runtime_projection_path=proj,
                workspace_paths=(),
                container_pi_home=Path("/home/dev/.pi"),
                runner=runner,
                host_access=HostAccessPolicy(
                    enabled=True, mode="docker-gateway", proxy_port=None,
                ),
                host_access_address="10.0.2.2",
            )

            result = verify_runtime(request)
            pp_checks = [c for c in result.checks
                         if c.key == "host-access.proxy-port"]
            self.assertEqual(0, len(pp_checks),
                             "proxy-port check must be absent when None")
            ha = [c for c in result.checks if c.key == "host-access.address"]
            self.assertTrue(ha, "host-access.address must still be present")


# ═══════════════════════════════════════════════════════════════════════
# 4.3  Disabled host-access verification
# ═══════════════════════════════════════════════════════════════════════

class TestDisabledHostAccessVerificationRed(unittest.TestCase):
    """Disabled host access must skip positive gateway resolution
    and fail if constructor-set HOST_ACCESS_ADDRESS or HOST_PROXY_PORT
    is present."""

    def test_disabled_skips_hostname_resolution(self) -> None:
        """When host access is disabled, no getent for host.docker.internal."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            proj = _make_projection(Path(tmp) / "proj.toml")
            responses = dict(_base_responses(proj))
            responses.update({
                ("printenv", "HOST_ACCESS_ADDRESS"):
                    _rf(("printenv",), rc=1, stdout=""),
                ("printenv", "HOST_PROXY_PORT"):
                    _rf(("printenv",), rc=1, stdout=""),
            })
            runner = _FakeRunner(responses)

            request = VerifyRuntimeRequest(
                container="pi-test",
                runtime_projection_path=proj,
                workspace_paths=(),
                container_pi_home=Path("/home/dev/.pi"),
                runner=runner,
                host_access=None,
                host_access_address=None,
            )

            result = verify_runtime(request)
            gm = [c for c in result.checks if c.key == "gateway.mapping"]
            self.assertEqual(0, len(gm),
                             "gateway.mapping must not be checked when disabled")
            getent_calls = [a for a in runner.calls if "getent" in a]
            self.assertEqual(0, len(getent_calls),
                             "no getent call when host access is disabled")

    def test_disabled_fails_if_host_access_address_present(self) -> None:
        """When disabled but HOST_ACCESS_ADDRESS is set, verification fails."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            proj = _make_projection(Path(tmp) / "proj.toml")
            responses = dict(_base_responses(proj))
            responses.update({
                ("printenv", "HOST_ACCESS_ADDRESS"):
                    _rf(("printenv",), rc=0, stdout="10.0.2.2\n"),
                ("printenv", "HOST_PROXY_PORT"):
                    _rf(("printenv",), rc=1, stdout=""),
            })
            runner = _FakeRunner(responses)

            request = VerifyRuntimeRequest(
                container="pi-test",
                runtime_projection_path=proj,
                workspace_paths=(),
                container_pi_home=Path("/home/dev/.pi"),
                runner=runner,
                host_access=None,
                host_access_address=None,
            )

            result = verify_runtime(request)
            leak_checks = [c for c in result.checks
                           if c.key == "host-access.address"]
            self.assertTrue(leak_checks, "must report HOST_ACCESS_ADDRESS leak")
            self.assertFalse(leak_checks[0].ok,
                             "HOST_ACCESS_ADDRESS must fail verification")

    def test_disabled_fails_if_host_proxy_port_present(self) -> None:
        """When disabled but HOST_PROXY_PORT is set, verification fails."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            proj = _make_projection(Path(tmp) / "proj.toml")
            responses = dict(_base_responses(proj))
            responses.update({
                ("printenv", "HOST_ACCESS_ADDRESS"):
                    _rf(("printenv",), rc=1, stdout=""),
                ("printenv", "HOST_PROXY_PORT"):
                    _rf(("printenv",), rc=0, stdout="1080\n"),
            })
            runner = _FakeRunner(responses)

            request = VerifyRuntimeRequest(
                container="pi-test",
                runtime_projection_path=proj,
                workspace_paths=(),
                container_pi_home=Path("/home/dev/.pi"),
                runner=runner,
                host_access=None,
                host_access_address=None,
            )

            result = verify_runtime(request)
            pp_checks = [c for c in result.checks
                         if c.key == "host-access.proxy-port"]
            self.assertTrue(pp_checks, "must report HOST_PROXY_PORT leak")
            self.assertFalse(pp_checks[0].ok,
                             "HOST_PROXY_PORT leak must fail verification")

    def test_disabled_passes_when_no_host_access_env_set(self) -> None:
        """When disabled and no constructor vars present, passes."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            proj = _make_projection(Path(tmp) / "proj.toml")
            responses = dict(_base_responses(proj))
            responses.update({
                ("printenv", "HOST_ACCESS_ADDRESS"):
                    _rf(("printenv",), rc=1, stdout=""),
                ("printenv", "HOST_PROXY_PORT"):
                    _rf(("printenv",), rc=1, stdout=""),
            })
            runner = _FakeRunner(responses)

            request = VerifyRuntimeRequest(
                container="pi-test",
                runtime_projection_path=proj,
                workspace_paths=(),
                container_pi_home=Path("/home/dev/.pi"),
                runner=runner,
                host_access=None,
                host_access_address=None,
            )

            result = verify_runtime(request)
            leak_checks = [c for c in result.checks
                           if c.key.startswith("host-access.")]
            self.assertTrue(leak_checks,
                            "host-access checks must be present when disabled")
            self.assertTrue(all(c.ok for c in leak_checks),
                            f"all checks must pass when vars absent: "
                            f"{[(c.key, c.ok) for c in leak_checks]}")


# ═══════════════════════════════════════════════════════════════════════
# 4.4  Facade verification expectations from inventory + companion
# ═══════════════════════════════════════════════════════════════════════

class TestFacadeVerifyHostAccessRed(unittest.TestCase):
    """The facade's ``verify --scope runtime`` command must derive
    host-access expectations from the selected reviewed inventory and
    its matching local companion."""

    @staticmethod
    def _load_mod() -> Any:
        from docker import constructor_cli
        return constructor_cli

    @staticmethod
    def _run_cli(
        mod: Any,
        argv: list[str],
        *,
        _process_runner: Any = None,
        _prompt_user: Any = None,
    ) -> tuple[int, str, str]:
        import io
        from contextlib import redirect_stderr, redirect_stdout
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = mod.main(
                argv,
                _process_runner=_process_runner,
                _prompt_user=_prompt_user,
            )
        return rc, out.getvalue(), err.getvalue()

    def test_facade_passes_host_access_from_inventory_and_companion(self) -> None:
        """When inventory has docker-gateway and companion has address,
        the facade must derive and pass correct HostAccessPolicy and address."""
        import tempfile
        from unittest import mock

        mod = self._load_mod()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inv = root / "docker-constructor.toml"
            comp = root / "docker-constructor.local.toml"
            # Write minimal files — validation is mocked
            inv.write_text(_CANONICAL_INVENTORY)
            from docker.versioning.project_state import resolve_project_state
            cache = root / "constructor-cache"
            cache.mkdir(mode=0o700)
            comp.write_text(f"[cache]\ndir = {str(cache)!r}\n")
            state = resolve_project_state(root, cache_root=cache)
            rp = state.runtime_root / "a1b2c3d4.toml"
            rp.write_text(
                '[extensions]\n'
                '[workspace_paths]\n'
                'paths = []\n'
                '[pi_home]\n'
                'path = "/home/dev/.pi"\n'
                '[gateway]\n'
                'address = "10.0.2.100"\n'
                '[integrity]\n'
                'sha256 = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="\n'
            )

            from docker.launcher import ProcessResult
            from docker.versioning.model import HostAccessPolicy

            _enabled = HostAccessPolicy(
                enabled=True, mode="docker-gateway", proxy_port=None,
            )

            captured_request = []
            captured_inv_path = []

            def _fake_resolve(inv_path: Any, **_kwargs: Any) -> tuple[Any, str | None, str | None]:
                captured_inv_path.append(inv_path)
                return _enabled, "10.0.2.100", None

            def _fake_verify_runtime(req: Any) -> Any:
                captured_request.append(req)
                from docker.versioning.runtime_verification import (
                    RuntimeVerificationResult,
                )
                return RuntimeVerificationResult(
                    container=req.container, checks=(),
                    all_ok=True, errors=(),
                )

            class _Rec:
                def run(self, argv: Any, **kwargs: Any) -> ProcessResult:
                    if argv[0] == "docker" and argv[1] == "ps":
                        return ProcessResult(argv=tuple(argv),
                                             return_code=0,
                                             stdout="abc123\n", stderr="")
                    return ProcessResult(argv=tuple(argv),
                                         return_code=0,
                                         stdout="", stderr="")

            with mock.patch(
                "docker.constructor_cli._resolve_verify_host_access",
                _fake_resolve,
            ), mock.patch(
                "docker.versioning.runtime_verification.verify_runtime",
                _fake_verify_runtime,
            ):
                rc, out, err = self._run_cli(
                    mod,
                    ["--project-directory", str(Path(inv).parent),
                     "verify", "--scope", "runtime",
                     "--container", "pi-test",
                     "--workspace", "/home/dev/p1"],
                    _process_runner=_Rec(),
                    _prompt_user=lambda _: True,
                )

            self.assertTrue(captured_request,
                            "verify_runtime must be called")
            req = captured_request[0]
            self.assertEqual(_enabled, req.host_access)
            self.assertEqual("10.0.2.100", req.host_access_address)
            # Must resolve using the correct inv_path
            self.assertEqual(str(inv),
                             str(captured_inv_path[0]),
                             "inventory path must match --inventory arg")

    def test_facade_disabled_passes_none_host_access(self) -> None:
        """When host access disabled, pass host_access=None, address=None."""
        import tempfile
        from unittest import mock

        mod = self._load_mod()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inv = root / "docker-constructor.toml"
            inv.write_text(_CANONICAL_INVENTORY)
            from docker.versioning.project_state import resolve_project_state
            cache = root / "constructor-cache"
            cache.mkdir(mode=0o700)
            (root / "docker-constructor.local.toml").write_text(
                f"[cache]\ndir = {str(cache)!r}\n"
            )
            state = resolve_project_state(root, cache_root=cache)
            rp = state.runtime_root / "a1b2c3d4.toml"
            rp.write_text(
                '[extensions]\n'
                '[workspace_paths]\n'
                'paths = []\n'
                '[pi_home]\n'
                'path = "/home/dev/.pi"\n'
                '[gateway]\n'
                'address = ""\n'
                '[integrity]\n'
                'sha256 = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="\n'
            )

            from docker.launcher import ProcessResult

            captured_request = []

            def _fake_resolve(inv_path: Any, **_kwargs: Any) -> tuple[Any, str | None, str | None]:
                return None, None, None

            def _fake_verify_runtime(req: Any) -> Any:
                captured_request.append(req)
                from docker.versioning.runtime_verification import (
                    RuntimeVerificationResult,
                )
                return RuntimeVerificationResult(
                    container=req.container, checks=(),
                    all_ok=True, errors=(),
                )

            class _Rec:
                def run(self, argv: Any, **kwargs: Any) -> ProcessResult:
                    if argv[0] == "docker" and argv[1] == "ps":
                        return ProcessResult(argv=tuple(argv),
                                             return_code=0,
                                             stdout="abc123\n", stderr="")
                    return ProcessResult(argv=tuple(argv),
                                         return_code=0,
                                         stdout="", stderr="")

            with mock.patch(
                "docker.constructor_cli._resolve_verify_host_access",
                _fake_resolve,
            ), mock.patch(
                "docker.versioning.runtime_verification.verify_runtime",
                _fake_verify_runtime,
            ):
                rc, out, err = self._run_cli(
                    mod,
                    ["--project-directory", str(Path(inv).parent),
                     "verify", "--scope", "runtime",
                     "--container", "pi-test",
                     "--workspace", "/home/dev/p1"],
                    _process_runner=_Rec(),
                    _prompt_user=lambda _: True,
                )

            self.assertTrue(captured_request,
                            "verify_runtime must be called")
            req = captured_request[0]
            self.assertIsNone(req.host_access,
                              "host_access must be None when disabled")
            self.assertIsNone(req.host_access_address,
                              "address must be None when disabled")

    def test_facade_project_inventory_resolves_fixed_companion(self) -> None:
        """The selected project's fixed inventory resolves its companion."""
        import tempfile
        from unittest import mock

        mod = self._load_mod()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inv = root / "docker-constructor.toml"
            inv.write_text(_CANONICAL_INVENTORY)
            from docker.versioning.project_state import resolve_project_state
            cache = root / "constructor-cache"
            cache.mkdir(mode=0o700)
            (root / "docker-constructor.local.toml").write_text(
                f"[cache]\ndir = {str(cache)!r}\n"
            )
            state = resolve_project_state(root, cache_root=cache)
            rp = state.runtime_root / "a1b2c3d4.toml"
            rp.write_text(
                '[extensions]\n'
                '[workspace_paths]\n'
                'paths = []\n'
                '[pi_home]\n'
                'path = "/home/dev/.pi"\n'
                '[gateway]\n'
                'address = "203.0.113.99"\n'
                '[integrity]\n'
                'sha256 = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="\n'
            )

            from docker.launcher import ProcessResult
            from docker.versioning.model import HostAccessPolicy

            _ext_addr = HostAccessPolicy(
                enabled=True, mode="external-address", proxy_port=None,
            )

            captured_request = []
            captured_inv_path = []

            def _fake_resolve(inv_path: Any, **_kwargs: Any) -> tuple[Any, str | None, str | None]:
                captured_inv_path.append(inv_path)
                return _ext_addr, "203.0.113.99", None

            def _fake_verify_runtime(req: Any) -> Any:
                captured_request.append(req)
                from docker.versioning.runtime_verification import (
                    RuntimeVerificationResult,
                )
                return RuntimeVerificationResult(
                    container=req.container, checks=(),
                    all_ok=True, errors=(),
                )

            class _Rec:
                def run(self, argv: Any, **kwargs: Any) -> ProcessResult:
                    if argv[0] == "docker" and argv[1] == "ps":
                        return ProcessResult(argv=tuple(argv),
                                             return_code=0,
                                             stdout="abc123\n", stderr="")
                    return ProcessResult(argv=tuple(argv),
                                         return_code=0,
                                         stdout="", stderr="")

            with mock.patch(
                "docker.constructor_cli._resolve_verify_host_access",
                _fake_resolve,
            ), mock.patch(
                "docker.versioning.runtime_verification.verify_runtime",
                _fake_verify_runtime,
            ):
                rc, out, err = self._run_cli(
                    mod,
                    ["--project-directory", str(Path(inv).parent),
                     "verify", "--scope", "runtime",
                     "--container", "pi-test",
                     "--workspace", "/home/dev/p1"],
                    _process_runner=_Rec(),
                    _prompt_user=lambda _: True,
                )

            self.assertTrue(captured_request,
                            "verify_runtime must be called")
            req = captured_request[0]
            self.assertIsNotNone(req.host_access)
            self.assertEqual("external-address", req.host_access.mode)
            self.assertEqual("203.0.113.99", req.host_access_address)
            self.assertEqual(str(inv), str(captured_inv_path[0]))


class TestVerifyHostAccessConfigErrorsRed(unittest.TestCase):
    """Invalid inventory or missing/malformed enabled companion must
    produce a CONFIG error before container discovery or Docker exec."""

    @staticmethod
    def _load_mod() -> Any:
        from docker import constructor_cli
        return constructor_cli

    @staticmethod
    def _run_cli(
        mod: Any,
        argv: list[str],
        *,
        _process_runner: Any = None,
        _prompt_user: Any = None,
    ) -> tuple[int, str, str]:
        import io
        from contextlib import redirect_stderr, redirect_stdout
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = mod.main(
                argv,
                _process_runner=_process_runner,
                _prompt_user=_prompt_user,
            )
        return rc, out.getvalue(), err.getvalue()

    def test_invalid_inventory_returns_config_error(self) -> None:
        """When the explicit --inventory is unreadable or malformed,
        the verify command must return CONFIG before container discovery."""
        import tempfile

        mod = self._load_mod()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Malformed TOML
            bad = root / "docker-constructor.toml"
            bad.write_text("not valid toml [[[")

            from docker.launcher import ProcessResult

            # Runner must NOT be called for container discovery
            class _Rec:
                def run(self, argv: Any, **kwargs: Any) -> ProcessResult:
                    self.called = True
                    return ProcessResult(argv=tuple(argv),
                                         return_code=0,
                                         stdout="abc123\n", stderr="")
            _rec = _Rec()
            _rec.called = False

            rc, out, err = self._run_cli(
                mod,
                ["--project-directory", str(Path(bad).parent),
                 "verify", "--scope", "runtime",
                 "--container", "pi-test",
                 "--workspace", "/home/dev/p1"],
                _process_runner=_rec,
                _prompt_user=lambda _: True,
            )
            self.assertEqual(3, rc,
                             "invalid inventory must exit with OPERATIONAL code")
            self.assertIn("cannot load", err.lower() or "",
                          "error must mention inventory load failure")
            self.assertFalse(_rec.called,
                             "container discovery must NOT run for bad inventory")

    def test_enabled_missing_companion_returns_config_error(self) -> None:
        """When host access is enabled but the local companion does
        not exist, verify must return CONFIG before container exec."""
        import tempfile

        mod = self._load_mod()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inv = root / "docker-constructor.toml"
            # Use the real inventory as a base and enable docker-gateway
            import shutil
            shutil.copyfile(
                Path(__file__).resolve().parent.parent / "docker-constructor.toml",
                inv,
            )
            with inv.open("a") as f:
                f.write(
                    '[runtime.host-access]\n'
                    'enabled = true\n'
                    'mode = "docker-gateway"\n'
                )
            # Local companion does NOT exist
            companion = root / "docker-constructor.local.toml"
            self.assertFalse(companion.exists())

            from docker.launcher import ProcessResult

            class _Rec:
                def run(self, argv: Any, **kwargs: Any) -> ProcessResult:
                    self.called = True
                    if argv[0] == "docker" and argv[1] == "ps":
                        return ProcessResult(argv=tuple(argv),
                                             return_code=0,
                                             stdout="abc123\n", stderr="")
                    return ProcessResult(argv=tuple(argv),
                                         return_code=0,
                                         stdout="", stderr="")
            _rec = _Rec()
            _rec.called = False

            rc, out, err = self._run_cli(
                mod,
                ["--project-directory", str(Path(inv).parent),
                 "verify", "--scope", "runtime",
                 "--container", "pi-test",
                 "--workspace", "/home/dev/p1"],
                _process_runner=_rec,
                _prompt_user=lambda _: True,
            )
            self.assertEqual(3, rc,
                             "missing companion must exit with OPERATIONAL")
            self.assertIn("local companion", (err or "").lower(),
                          f"error must mention companion, got: {err!r}")

    def test_enabled_malformed_companion_returns_config_error(self) -> None:
        """When host access is enabled but the local companion has
        malformed TOML, verify must return CONFIG."""
        import tempfile

        mod = self._load_mod()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inv = root / "docker-constructor.toml"
            # Use the real inventory as a base and enable docker-gateway
            import shutil
            shutil.copyfile(
                Path(__file__).resolve().parent.parent / "docker-constructor.toml",
                inv,
            )
            with inv.open("a") as f:
                f.write(
                    '[runtime.host-access]\n'
                    'enabled = true\n'
                    'mode = "docker-gateway"\n'
                )
            comp = root / "docker-constructor.local.toml"
            comp.write_text("not valid toml [[[")

            from docker.launcher import ProcessResult

            class _Rec:
                def run(self, argv: Any, **kwargs: Any) -> ProcessResult:
                    self.called = True
                    if argv[0] == "docker" and argv[1] == "ps":
                        return ProcessResult(argv=tuple(argv),
                                             return_code=0,
                                             stdout="abc123\n", stderr="")
                    return ProcessResult(argv=tuple(argv),
                                         return_code=0,
                                         stdout="", stderr="")
            _rec = _Rec()
            _rec.called = False

            rc, out, err = self._run_cli(
                mod,
                ["--project-directory", str(Path(inv).parent),
                 "verify", "--scope", "runtime",
                 "--container", "pi-test",
                 "--workspace", "/home/dev/p1"],
                _process_runner=_rec,
                _prompt_user=lambda _: True,
            )
            self.assertEqual(3, rc,
                             "malformed companion must exit with OPERATIONAL")
            self.assertIn("companion", (err or "").lower(),
                          f"error must mention the companion, got: {err!r}")
            self.assertIn("malformed", (err or "").lower(),
                          f"error must mention malformed TOML, got: {err!r}")

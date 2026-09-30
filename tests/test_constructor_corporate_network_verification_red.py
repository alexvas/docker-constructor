"""RED contracts for Phase 3 — runtime verification and orchestration.

These tests pin the Phase 3 contract before the GREEN implementation
exists:

* enabled local corporate trust produces both the read-only system-bundle
  mount and the five exact client CA environment assignments in the final
  launch request, while disabled/absent trust produces neither (task 3.1);
* runtime verification inspects each of the five client CA variables when
  trust is enabled and reports every missing or mismatched variable
  independently while retaining the existing mount result (tasks 3.2, 3.6);
* disabled runtime verification neither requires constructor-generated
  client CA assignments nor reports them as enabled policy, and ignores
  values inherited from the base image (tasks 3.3, 3.7);
* malformed enabled corporate configuration fails closed before launch
  planning, container inspection, or Docker execution (tasks 3.4, 3.8);
* verification details disclose only fixed in-container variable names and
  the fixed system path, never the host bundle source or certificate
  contents, and issue no TLS request or connectivity/validity claim
  (tasks 3.9, 3.10).

The GREEN implementation extends ``VerifyRuntimeRequest`` /
``verify_runtime`` with the client CA environment checks; the orchestration
and malformed-configuration contracts are regression guards for the
Phase 2 wiring.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docker.launcher import (
    WorkspaceSelection,
    RunRequest,
    RunResult,
    orchestrate_run,
)
from docker.versioning.corporate_network import (
    CLIENT_CA_ENVIRONMENT,
    SYSTEM_CA_BUNDLE,
)
from docker.versioning.dispatch_types import ExitKind
from docker.versioning.runtime_verification import (
    VerifyRuntimeRequest,
    _MOUNTS_PROBE,
    verify_runtime,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CANONICAL = (_REPO_ROOT / "docker-constructor.toml").read_text()

_CLIENT_CA_NAMES = tuple(name for name, _value in CLIENT_CA_ENVIRONMENT)

_VALID_BUNDLE = (
    "-----BEGIN CERTIFICATE-----\n"
    "AQIDBAU=\n"
    "-----END CERTIFICATE-----\n"
)

_MALFORMED_BUNDLE = "not a pem bundle\n"

_NETWORK_TOOLS = {
    "openssl",
    "gnutls-cli",
    "curl",
    "wget",
    "nc",
    "ncat",
    "telnet",
}


# ---------------------------------------------------------------------------
# Process boundary
# ---------------------------------------------------------------------------


class _ProcessResult:
    def __init__(self, *, argv, return_code, stdout, stderr) -> None:
        self.argv = argv
        self.return_code = return_code
        self.stdout = stdout
        self.stderr = stderr


class _ClientCaRunner:
    """Answers the client CA / mount / proxy probes for a launch.

    When *enabled*, the runner reports a read-only system-bundle mount and
    one fixed value per client CA variable.  When disabled it reports no
    mount.  Any command without a handler falls through to a generic
    success with empty output, exactly like the other verification fakes.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        overrides: dict[tuple[str, ...], tuple[int, str, str]] | None = None,
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._handlers: dict[tuple[str, ...], tuple[int, str, str]] = {}
        if enabled:
            self._handlers[_MOUNTS_PROBE] = (0, "ro,relatime\n", "")
            self._handlers[("test", "-w", SYSTEM_CA_BUNDLE)] = (1, "", "")
            for name, value in CLIENT_CA_ENVIRONMENT:
                self._handlers[("printenv", name)] = (0, value + "\n", "")
        else:
            self._handlers[_MOUNTS_PROBE] = (0, "", "")
        if overrides:
            self._handlers.update(overrides)

    def run(self, argv) -> object:
        t = tuple(argv)
        self.calls.append(t)
        rc, out, err = self._handlers.get(t[3:], (0, "", ""))
        return _ProcessResult(
            argv=t, return_code=rc, stdout=out, stderr=err,
        )


def _verify(runner: _ClientCaRunner, *, enabled: bool):
    with tempfile.TemporaryDirectory() as root:
        proj = Path(root) / "projection.toml"
        proj.write_text("[extensions]\n")
        return verify_runtime(VerifyRuntimeRequest(
            container="pi-cli-pi-1",
            runtime_projection_path=proj,
            workspace_paths=(Path("/work/project"),),
            container_pi_home=Path("/home/dev/.pi"),
            runner=runner,
            host_access=None,
            corporate_trust_enabled=enabled,
            proxy_url=None,
            proxy_no_proxy=None,
        ))


def _client_ca_checks(result) -> dict[str, list]:
    """Map each client CA variable name to its verification checks."""
    out: dict[str, list] = {}
    for check in result.checks:
        if check.key != "corporate-trust.environment":
            continue
        for name in _CLIENT_CA_NAMES:
            if name in check.detail:
                out.setdefault(name, []).append(check)
    return out


# ---------------------------------------------------------------------------
# 3.1  Enabled orchestration: mount + all five assignments
# ---------------------------------------------------------------------------


def _bomb_executor(effects: list[str]):
    class _Exec:
        def run(self, argv: tuple[str, ...], *, interactive: bool = False):
            del argv, interactive
            effects.append("execution")
            raise AssertionError("Docker run must not execute during dry-run")

    return _Exec()


def _bomb_inspector(effects: list[str]):
    class _Insp:
        def list_names(self) -> set[str]:
            effects.append("inspection")
            raise AssertionError("container inspection must not run")

    return _Insp()


def _bomb_projection(effects: list[str]):
    def _factory(*_args, **_kwargs):
        effects.append("projection")
        raise AssertionError("projection publication must not run")

    return _factory


def _bomb_artifact(effects: list[str]):
    def _fetcher(*_args, **_kwargs):
        effects.append("materialization")
        raise AssertionError("artifact materialization must not run")

    return _fetcher


def _collect_env_assignments(args: tuple[str, ...]) -> list[tuple[str, str]]:
    assignments: list[tuple[str, str]] = []
    it = iter(args)
    for token in it:
        if token == "--env":
            raw = next(it)
            k, _, v = raw.partition("=")
            assignments.append((k, v))
    return assignments


def _collect_mounts(args: tuple[str, ...]) -> list[dict[str, str]]:
    mounts: list[dict[str, str]] = []
    it = iter(args)
    for token in it:
        if token == "--mount":
            raw = next(it)
            kv: dict[str, str] = {}
            for pair in raw.split(","):
                k, _, v = pair.partition("=")
                kv[k] = v
            mounts.append(kv)
    return mounts


def _find_mount(
    mounts: list[dict[str, str]], dst: str,
) -> dict[str, str] | None:
    for mount in mounts:
        if mount.get("dst") == dst:
            return mount
    return None


class _RunOrchestrationRed(unittest.TestCase):
    """Runs ``orchestrate_run`` (dry-run) against a real inventory +
    local companion, mirroring the Phase 2 orchestration harness."""

    def run_with_local(
        self,
        companion: str | None,
        *,
        bundle: str | None = None,
    ) -> RunResult:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            inventory = root_path / "inventory.toml"
            inventory.write_text(_CANONICAL)
            if companion is not None:
                (root_path / "docker-constructor.local.toml").write_text(
                    companion,
                )
            if bundle is not None:
                bundle_dir = root_path / ".docker-local"
                bundle_dir.mkdir()
                (bundle_dir / "corporate-ca-bundle.crt").write_text(bundle)
            effects: list[str] = []
            result = orchestrate_run(RunRequest(
                inventory_path=str(inventory),
                image="pi-cli-pi:latest",
                selection=WorkspaceSelection(workspace="/work/project"),
                pi_home_host="/home/user/.pi",
                repo_root=str(root_path),
                project_root=str(root_path),
                dry_run=True,
                executor=_bomb_executor(effects),
                inspector=_bomb_inspector(effects),
                _create_projection=_bomb_projection(effects),
                _artifact_fetcher=_bomb_artifact(effects),
            ))
            self.assertEqual([], effects)
            return result


class TestEnabledTrustOrchestrationRed(_RunOrchestrationRed):
    """Task 3.1: enabled trust plans mount + five assignments."""

    def test_enabled_trust_final_launch_has_mount_and_all_five(self) -> None:
        result = self.run_with_local(
            "[corporate-trust]\nenabled = true\n",
            bundle=_VALID_BUNDLE,
        )
        self.assertEqual(ExitKind.SUCCESS, result.exit_kind, result.message)
        mount = _find_mount(
            _collect_mounts(result.run_args), SYSTEM_CA_BUNDLE,
        )
        self.assertIsNotNone(mount, "missing corporate trust mount")
        self.assertIn("readonly", mount)
        by_name: dict[str, list[str]] = {}
        for name, value in _collect_env_assignments(result.run_args):
            by_name.setdefault(name, []).append(value)
        for name in _CLIENT_CA_NAMES:
            self.assertEqual(
                [SYSTEM_CA_BUNDLE], by_name.get(name, []), name,
            )

    def test_disabled_trust_final_launch_has_neither_mount_nor_env(self) -> None:
        for companion in (None, "[corporate-trust]\nenabled = false\n"):
            with self.subTest(companion=companion):
                result = self.run_with_local(companion)
                self.assertEqual(
                    ExitKind.SUCCESS, result.exit_kind, result.message,
                )
                self.assertIsNone(
                    _find_mount(
                        _collect_mounts(result.run_args), SYSTEM_CA_BUNDLE,
                    ),
                    "disabled trust must not emit a system-bundle mount",
                )
                assignments = _collect_env_assignments(result.run_args)
                for name in _CLIENT_CA_NAMES:
                    self.assertEqual(
                        [], [v for k, v in assignments if k == name], name,
                    )


# ---------------------------------------------------------------------------
# 3.2 / 3.6  Enabled runtime verification of the five variables
# ---------------------------------------------------------------------------


class TestEnabledClientCaRuntimeVerificationRed(unittest.TestCase):
    """Tasks 3.2 and 3.6: report each variable, retain the mount."""

    def test_reports_each_variable_with_exact_fixed_value(self) -> None:
        runner = _ClientCaRunner(enabled=True)
        result = _verify(runner, enabled=True)
        by_name = _client_ca_checks(result)
        self.assertEqual(set(_CLIENT_CA_NAMES), set(by_name))
        for name in _CLIENT_CA_NAMES:
            self.assertEqual(1, len(by_name[name]), name)
            check = by_name[name][0]
            self.assertTrue(check.ok, check.detail)
            self.assertIn(SYSTEM_CA_BUNDLE, check.detail)
        mount = next(
            c for c in result.checks if c.key == "corporate-trust.mount"
        )
        self.assertTrue(mount.ok, mount.detail)

    def test_missing_and_mismatched_variables_reported_individually(
        self,
    ) -> None:
        runner = _ClientCaRunner(enabled=True, overrides={
            ("printenv", "PIP_CERT"): (0, "/etc/other/cert.pem\n", ""),
            ("printenv", "CURL_CA_BUNDLE"): (1, "", ""),
        })
        result = _verify(runner, enabled=True)
        by_name = _client_ca_checks(result)
        self.assertEqual(set(_CLIENT_CA_NAMES), set(by_name))
        self.assertTrue(by_name["NODE_EXTRA_CA_CERTS"][0].ok)
        self.assertTrue(by_name["SSL_CERT_FILE"][0].ok)
        self.assertTrue(by_name["REQUESTS_CA_BUNDLE"][0].ok)
        self.assertFalse(by_name["PIP_CERT"][0].ok)
        self.assertFalse(by_name["CURL_CA_BUNDLE"][0].ok)
        # Each variable is diagnosed on its own check.
        self.assertIn("PIP_CERT", by_name["PIP_CERT"][0].detail)
        self.assertIn("CURL_CA_BUNDLE", by_name["CURL_CA_BUNDLE"][0].detail)
        # The existing mount result is retained alongside the new checks.
        mount = next(
            c for c in result.checks if c.key == "corporate-trust.mount"
        )
        self.assertTrue(mount.ok, mount.detail)

    def test_whitespace_and_extra_content_values_are_exact_mismatches(
        self,
    ) -> None:
        variants = (
            (" /etc/ssl/certs/ca-certificates.crt\n", "leading space"),
            ("/etc/ssl/certs/ca-certificates.crt \n", "trailing space"),
            (
                "/etc/ssl/certs/ca-certificates.crt\nextra\n",
                "extra embedded line",
            ),
            ("\t/etc/ssl/certs/ca-certificates.crt\n", "leading tab"),
            (
                "/etc/ssl/certs/ca-certificates.crt\r\n",
                "trailing carriage return",
            ),
        )
        for (stdout, label), name in zip(variants, _CLIENT_CA_NAMES):
            with self.subTest(variant=label, variable=name):
                runner = _ClientCaRunner(enabled=True, overrides={
                    ("printenv", name): (0, stdout, ""),
                })
                result = _verify(runner, enabled=True)
                by_name = _client_ca_checks(result)
                self.assertEqual(set(_CLIENT_CA_NAMES), set(by_name))
                self.assertFalse(by_name[name][0].ok, label)
                self.assertIn(name, by_name[name][0].detail)
                self.assertIn(SYSTEM_CA_BUNDLE, by_name[name][0].detail)
                for other in _CLIENT_CA_NAMES:
                    if other == name:
                        continue
                    self.assertTrue(by_name[other][0].ok, other)
                mount = next(
                    c for c in result.checks
                    if c.key == "corporate-trust.mount"
                )
                self.assertTrue(mount.ok, mount.detail)

    def test_mismatched_value_is_redacted_from_detail_and_structured_output(
        self,
    ) -> None:
        hostile = (
            "/host/.docker-local/corporate-ca-bundle.crt\n"
            "-----BEGIN CERTIFICATE-----\n"
            "AQIDBAU=\n"
            "-----END CERTIFICATE-----"
        )
        runner = _ClientCaRunner(enabled=True, overrides={
            ("printenv", "PIP_CERT"): (
                0,
                hostile + "\n",
                "printenv failed: " + hostile + "\n",
            ),
        })
        result = _verify(runner, enabled=True)
        check = _client_ca_checks(result)["PIP_CERT"][0]
        self.assertFalse(check.ok)
        # The fixed variable name and fixed expected path remain identifiable.
        self.assertIn("PIP_CERT", check.detail)
        self.assertIn(SYSTEM_CA_BUNDLE, check.detail)
        # No observed/host/certificate content leaks through any field.
        for field in (
            check.detail,
            check.raw_stdout or "",
            check.raw_stderr or "",
        ):
            for token in (
                "corporate-ca-bundle",
                ".docker-local",
                "BEGIN CERTIFICATE",
                "AQIDBAU=",
                hostile,
            ):
                self.assertNotIn(token, field)
        # Command and exit-code diagnostics are preserved.
        self.assertEqual(
            ("docker", "exec", "pi-cli-pi-1", "printenv", "PIP_CERT"),
            tuple(check.command or ()),
        )
        self.assertEqual(0, check.exit_code)
        # Other variables and the mount result are independently reported.
        by_name = _client_ca_checks(result)
        for other in _CLIENT_CA_NAMES:
            if other == "PIP_CERT":
                continue
            self.assertTrue(by_name[other][0].ok, other)
        mount = next(
            c for c in result.checks if c.key == "corporate-trust.mount"
        )
        self.assertTrue(mount.ok, mount.detail)


# ---------------------------------------------------------------------------
# 3.3 / 3.7  Disabled runtime verification
# ---------------------------------------------------------------------------


class TestDisabledClientCaRuntimeVerificationRed(unittest.TestCase):
    """Tasks 3.3 and 3.7: disabled policy requires and reports nothing."""

    def test_disabled_verification_expects_no_client_ca_assignments(
        self,
    ) -> None:
        runner = _ClientCaRunner(enabled=False)
        result = _verify(runner, enabled=False)
        self.assertEqual(
            [],
            [c for c in result.checks
             if c.key == "corporate-trust.environment"],
        )
        mount = next(
            c for c in result.checks if c.key == "corporate-trust.mount"
        )
        self.assertTrue(mount.ok, mount.detail)
        # No client CA variable is even inspected.
        for argv in runner.calls:
            if argv[3:4] == ("printenv",):
                self.assertNotIn(argv[4], _CLIENT_CA_NAMES)

    def test_disabled_verification_ignores_inherited_client_ca_values(
        self,
    ) -> None:
        runner = _ClientCaRunner(enabled=False, overrides={
            ("printenv", "SSL_CERT_FILE"): (0, "/usr/lib/ssl/cert.pem\n", ""),
            ("printenv", "NODE_EXTRA_CA_CERTS"):
                (0, "/etc/ssl/certs/other.pem\n", ""),
        })
        result = _verify(runner, enabled=False)
        self.assertEqual(
            [],
            [c for c in result.checks
             if c.key == "corporate-trust.environment"],
        )
        mount = next(
            c for c in result.checks if c.key == "corporate-trust.mount"
        )
        self.assertTrue(mount.ok, mount.detail)


# ---------------------------------------------------------------------------
# 3.4 / 3.8  Malformed enabled configuration fails before effects
# ---------------------------------------------------------------------------


class TestMalformedEnabledTrustPreEffectRed(_RunOrchestrationRed):
    """Task 3.4: malformed enabled trust fails before any effect."""

    def test_malformed_bundle_fails_before_planning_or_execution(self) -> None:
        result = self.run_with_local(
            "[corporate-trust]\nenabled = true\n",
            bundle=_MALFORMED_BUNDLE,
        )
        self.assertEqual(ExitKind.CONFIG, result.exit_kind, result.message)
        self.assertEqual((), result.run_args)
        self.assertIn("corporate-ca-bundle.crt", result.message)


class TestMalformedEnabledTrustVerificationRed(unittest.TestCase):
    """Task 3.4: malformed enabled trust fails before container inspection."""

    def test_malformed_bundle_fails_verify_before_any_docker_call(self) -> None:
        from docker.constructor_cli import _resolve_verify_corporate_network

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            inventory = root_path / "inventory.toml"
            inventory.write_text(_CANONICAL)
            (root_path / "docker-constructor.local.toml").write_text(
                "[corporate-trust]\nenabled = true\n",
            )
            bundle_dir = root_path / ".docker-local"
            bundle_dir.mkdir()
            (bundle_dir / "corporate-ca-bundle.crt").write_text(
                _MALFORMED_BUNDLE,
            )
            _trust, _url, _no_proxy, error = (
                _resolve_verify_corporate_network(
                    str(inventory), root_path,
                )
            )
        self.assertIsNotNone(error)
        self.assertIn("corporate-ca-bundle.crt", error or "")


# ---------------------------------------------------------------------------
# 3.9 / 3.10  Disclosure and no-TLS boundaries
# ---------------------------------------------------------------------------


class TestClientCaVerificationBoundariesRed(unittest.TestCase):
    """Tasks 3.9 and 3.10: fixed diagnostics, no TLS or validity claims."""

    def test_enabled_details_reveal_only_fixed_names_and_path(self) -> None:
        runner = _ClientCaRunner(enabled=True)
        result = _verify(runner, enabled=True)
        for check in result.checks:
            if check.key != "corporate-trust.environment":
                continue
            self.assertIn(
                check.detail.split("=")[0], _CLIENT_CA_NAMES,
                check.detail,
            )
            # The command vector names only the fixed variable.
            self.assertEqual(
                ("docker", "exec", "pi-cli-pi-1", "printenv"),
                tuple(check.command or ())[:4],
            )
            self.assertEqual(5, len(check.command or ()))
            self.assertIn((check.command or ("", "", "", "", ""))[4],
                          _CLIENT_CA_NAMES)
            # Raw structured fields carry only the fixed value, never the
            # host bundle source or certificate contents.
            self.assertEqual(SYSTEM_CA_BUNDLE, (check.raw_stdout or "").strip())
            for field in (check.detail, check.raw_stdout or "",
                          check.raw_stderr or ""):
                for forbidden in (
                    "corporate-ca-bundle",
                    ".docker-local",
                    "BEGIN CERTIFICATE",
                    "/home/",
                ):
                    self.assertNotIn(forbidden, field)

    def test_enabled_verification_issues_no_tls_or_network_request(
        self,
    ) -> None:
        runner = _ClientCaRunner(enabled=True)
        result = _verify(runner, enabled=True)
        for argv in runner.calls:
            self.assertEqual(("docker", "exec"), argv[:2])
            joined = " ".join(argv)
            self.assertNotIn("s_client", joined)
            self.assertNotIn("/dev/tcp", joined)
            if len(argv) > 3:
                self.assertNotIn(argv[3], _NETWORK_TOOLS)
        for check in result.checks:
            if check.key != "corporate-trust.environment":
                continue
            lowered = check.detail.lower()
            for claim in ("valid", "connect", "replace", "augment", "root"):
                self.assertNotIn(claim, lowered)


if __name__ == "__main__":
    unittest.main()

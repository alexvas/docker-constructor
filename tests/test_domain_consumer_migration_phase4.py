"""Phase 4 domain-consumer migration coverage.

Every command transaction must route both fixed project TOML documents
(``docker-constructor.toml`` and ``docker-constructor.local.toml``) through the
Phase 1 configuration-document boundary exactly once and share one Phase 2
aggregate local result.  Each domain consumer must receive only its owning
slice of that result (host-access, cache, corporate trust/proxy), and the
local companion path, original representation, and aggregate contents must stay
out of serialization, projections, and container vectors.

These tests were written RED before the Phase 4 migration and are expected to
fail until the command orchestration and domain consumers are migrated.
"""
from __future__ import annotations

import io
import json
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

from docker.versioning.configuration_document_validation import DocumentRole
from docker.versioning.dispatch_types import ExitKind

REPO_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_TOML = (REPO_ROOT / "docker-constructor.toml").read_text(
    encoding="utf-8"
)

MALFORMED_LOCAL = "not valid toml [[["
VALID_LOCAL = '[cache]\ndir = "/tmp/phase4-migration-cache"\n'

HOST_ACCESS_POLICY = (
    '[runtime.host-access]\n'
    'enabled = true\n'
    'mode = "external-address"\n'
)
HOST_ACCESS_LOCAL = (
    '[host-access]\n'
    'address = "192.0.2.10"\n'
)


def _fake_transports(**_kwargs: Any) -> Any:
    """Stand-in transport bundle so update-discovery never touches the network."""
    return types.SimpleNamespace(http=object(), git=object(), tokens={})


class _Phase4Harness(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="phase4-migration-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def _write_project(
        self,
        *,
        local: str | None = None,
        policy: str = "",
        dockerfile: bool = False,
    ) -> Path:
        inventory = self.root / "docker-constructor.toml"
        inventory.write_text(
            CANONICAL_TOML + (("\n" + policy) if policy else ""),
            encoding="utf-8",
        )
        if local is not None:
            (self.root / "docker-constructor.local.toml").write_text(
                local, encoding="utf-8"
            )
        if dockerfile:
            (self.root / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        return inventory

    @property
    def inventory(self) -> Path:
        return self.root / "docker-constructor.toml"

    # ── per-command transaction runners ──────────────────────────────

    def _run_validate(self) -> Any:
        from docker.versioning.readonly_service import dispatch

        return dispatch(self.inventory, "validate", command_args={})

    def _run_show(self) -> Any:
        from docker.versioning.readonly_service import dispatch

        return dispatch(self.inventory, "show", command_args={})

    def _run_check_updates(self, *, suggest: bool = False) -> Any:
        from docker.versioning.readonly_service import dispatch

        with patch(
            "docker.versioning.updates.check_updates",
            lambda *a, **k: [],
        ), patch(
            "docker.versioning.transports.build_transports",
            _fake_transports,
        ):
            return dispatch(
                self.inventory,
                "check-updates",
                command_args={"suggest": suggest},
            )

    def _run_build(self, *, confirmed: bool = True) -> Any:
        from docker.versioning.build_orchestration import (
            BuildRequest,
            plan_build,
        )

        request = BuildRequest(
            inventory_path=str(self.inventory),
            repo_root=str(self.root),
            project_root=str(self.root),
            context=str(self.root),
            dockerfile=str(self.root / "Dockerfile"),
            platform="linux-amd64",
            tag=None,
            overrides={},
            cache=True,
            pull=False,
            progress="auto",
            uid=None,
            gid=None,
            confirmed=confirmed,
            dry_run=True,
        )
        return plan_build(request)

    def _run_run(self) -> Any:
        from docker.launcher import (
            RunRequest,
            WorkspaceSelection,
            orchestrate_run,
        )

        request = RunRequest(
            inventory_path=str(self.inventory),
            repo_root=str(self.root),
            project_root=str(self.root),
            image="pi-cli-pi:latest",
            selection=WorkspaceSelection(workspace="/work/project"),
            pi_home_host=str(self.root / "pi-home"),
            dry_run=True,
        )
        return orchestrate_run(request)

    def _run_doctor(self) -> Any:
        from docker.versioning.build_orchestration import (
            DoctorRequest,
            orchestrate_doctor,
        )

        return orchestrate_doctor(DoctorRequest(inventory_path=self.inventory))

    def _run_verify(self) -> tuple[int, str, str]:
        from docker.constructor_cli import main
        from docker.launcher import ProcessResult

        class _Runner:
            def run(self, argv: Any, **kwargs: Any) -> ProcessResult:
                return ProcessResult(
                    argv=tuple(argv), return_code=1, stdout="", stderr=""
                )

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = main(
                [
                    "--project-directory", str(self.root),
                    "verify", "--scope", "runtime",
                    "--container", "pi-test",
                    "--runtime-projection", str(self.root / "missing.toml"),
                ],
                _process_runner=_Runner(),
            )
        return rc, out.getvalue(), err.getvalue()

    # ── boundary-parse recorder ──────────────────────────────────────

    def _parse_roles(self, action: Any) -> list[DocumentRole]:
        import docker.versioning.configuration_document_validation as cdv

        roles: list[DocumentRole] = []
        real = cdv._parse_toml

        def spy(identity: Any) -> Any:
            roles.append(identity.role)
            return real(identity)

        with patch.object(cdv, "_parse_toml", spy):
            action()
        return roles


class TestOneSharedDocumentTransaction(_Phase4Harness):
    """Every command routes both documents through the boundary once."""

    def test_command_transactions_parse_each_document_once(self) -> None:
        self._write_project(local=VALID_LOCAL, dockerfile=True)
        commands = {
            "validate": self._run_validate,
            "show": self._run_show,
            "check-updates": self._run_check_updates,
            "build": self._run_build,
            "run": self._run_run,
            "doctor": self._run_doctor,
        }
        for name, action in commands.items():
            with self.subTest(command=name):
                roles = self._parse_roles(action)
                self.assertEqual(
                    roles.count(DocumentRole.REVIEWED),
                    1,
                    f"{name} must parse the reviewed document exactly once",
                )
                self.assertEqual(
                    roles.count(DocumentRole.LOCAL),
                    1,
                    f"{name} must parse the local document exactly once",
                )

    def test_verify_transaction_parses_local_document_once(self) -> None:
        self._write_project(local=VALID_LOCAL)
        roles = self._parse_roles(self._run_verify)
        self.assertEqual(
            roles.count(DocumentRole.LOCAL),
            1,
            "verify must share one parsed local document across its consumers",
        )


class TestMalformedLocalFailsBeforeEffects(_Phase4Harness):
    """A malformed local companion blocks every command configuration."""

    def test_readonly_and_doctor_commands_reject_malformed_local(self) -> None:
        self._write_project(local=MALFORMED_LOCAL, dockerfile=True)
        self.assertEqual(self._run_validate().exit_kind, ExitKind.CONFIG)
        self.assertEqual(self._run_show().exit_kind, ExitKind.CONFIG)
        self.assertEqual(
            self._run_check_updates().exit_kind, ExitKind.CONFIG
        )
        self.assertEqual(self._run_build().exit_kind, ExitKind.CONFIG)
        self.assertEqual(self._run_run().exit_kind, ExitKind.CONFIG)
        self.assertEqual(self._run_doctor().exit_kind, ExitKind.CONFIG)

    def test_verify_rejects_malformed_local(self) -> None:
        self._write_project(local=MALFORMED_LOCAL)
        rc, _out, err = self._run_verify()
        self.assertEqual(rc, 3, err)
        self.assertIn("malformed", err.lower())


class TestDomainSliceOwnership(_Phase4Harness):
    """Consumers receive only their owning slice, never the aggregate."""

    def test_run_does_not_reload_local_companion_for_host_access(self) -> None:
        self._write_project(
            policy=HOST_ACCESS_POLICY, local=HOST_ACCESS_LOCAL
        )
        with patch(
            "docker.versioning.local_project_configuration.load_optional_local_project_configuration",
            side_effect=AssertionError(
                "runtime host access must consume the shared local result"
            ),
        ):
            result = self._run_run()
        self.assertNotEqual(result.exit_kind, ExitKind.CONFIG, result.message)

    def test_check_updates_passes_only_the_cache_slice_to_transports(self) -> None:
        from docker.versioning.model import LocalCacheConfig

        self._write_project(local=VALID_LOCAL)
        captured: dict[str, Any] = {}

        def recording_build_transports(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return _fake_transports()

        with patch(
            "docker.versioning.updates.check_updates",
            lambda *a, **k: [],
        ), patch(
            "docker.versioning.transports.build_transports",
            recording_build_transports,
        ):
            from docker.versioning.readonly_service import dispatch

            dispatch(self.inventory, "check-updates", command_args={})

        self.assertIn(
            "local_cache",
            captured,
            "cache consumers must receive the cache slice, not the aggregate",
        )
        self.assertIsInstance(captured["local_cache"], LocalCacheConfig)


class TestCorporateNetworkIndependence(_Phase4Harness):
    """Corporate proxy/trust remain usable with host access disabled."""

    def test_proxy_is_usable_without_host_access(self) -> None:
        self._write_project(
            local=(
                '[network.proxy]\n'
                'url = "http://proxy.example.test:8080"\n'
                'no_proxy = "localhost"\n'
            )
        )
        plan = self._run_build()
        self.assertEqual(plan.exit_kind, ExitKind.SUCCESS, plan.message)
        rendered = plan.display_string or ""
        self.assertIn("http://proxy.example.test:8080", rendered)
        self.assertNotIn("HOST_ACCESS_ADDRESS", rendered)
        self.assertNotIn("HOST_PROXY_PORT", rendered)


class TestLocalSourceConfinement(_Phase4Harness):
    """The companion path and aggregate never leak into projections/vectors."""

    def test_local_state_absent_from_serialization_and_vectors(self) -> None:
        self._write_project(
            local=(
                '[cache]\ndir = "/tmp/phase4-confine-cache"\n'
                '[network.proxy]\n'
                'url = "http://proxy.example.test:8080"\n'
            ),
            dockerfile=True,
        )

        shown = self._run_show()
        serialized = json.dumps(shown.data, default=str)
        self.assertNotIn("docker-constructor.local.toml", serialized)
        self.assertNotIn("phase4-confine-cache", serialized)

        plan = self._run_build()
        self.assertEqual(plan.exit_kind, ExitKind.SUCCESS, plan.message)
        build_vector = plan.display_string or ""
        self.assertNotIn("docker-constructor.local.toml", build_vector)

        run = self._run_run()
        self.assertNotEqual(run.exit_kind, ExitKind.CONFIG, run.message)
        run_vector = " ".join(run.run_args)
        # The companion file/path/aggregate must never be exposed, while
        # cache-root mount paths are authorized derived values.
        self.assertNotIn("docker-constructor.local.toml", run_vector)
        self.assertNotIn(str(self.root / "docker-constructor.local.toml"), run_vector)

    def test_effective_projections_exclude_local_aggregate(self) -> None:
        from docker.versioning.readonly_service import dispatch

        self._write_project(
            local=(
                '[cache]\ndir = "/tmp/phase4-effective-confine"\n'
                '[network.proxy]\n'
                'url = "http://proxy.example.test:8080"\n'
            )
        )
        result = dispatch(
            self.inventory, "show", command_args={"effective": True}
        )
        serialized = json.dumps(result.data, default=str)
        self.assertNotIn("phase4-effective-confine", serialized)
        self.assertNotIn("docker-constructor.local.toml", serialized)


if __name__ == "__main__":
    unittest.main()

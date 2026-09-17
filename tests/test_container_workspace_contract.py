"""Phase 5 contract tests for the atomic host/image workspace interface."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from docker.versioning.rendering import RunHostAccess, RunRenderInputs, render_run_vector

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = (ROOT / "docker/entrypoint.sh").read_text(encoding="utf-8")
VERIFIER = (ROOT / "docker/versioning/runtime_verification.py").read_text(encoding="utf-8")
LAUNCHER = (ROOT / "docker/versioning/rendering.py").read_text(encoding="utf-8")


def _env(vector: tuple[str, ...]) -> dict[str, str]:
    result: dict[str, str] = {}
    for index, token in enumerate(vector[:-1]):
        if token == "--env":
            key, value = vector[index + 1].split("=", 1)
            result[key] = value
    return result


def _render(host_access: RunHostAccess = RunHostAccess.disabled(), **kwargs: object) -> tuple[str, ...]:
    values = dict(
        image="pi-cli-pi:latest",
        container_name="pi-1",
        projection_host_path="/cache/projects/p/runtime/runtime.toml",
        projection_container_path="/run/pi-cli/docker-constructor.runtime.toml",
        project_state_runtime_root="/cache/projects/p/runtime",
        pi_home_host="/host/.pi",
        workspace="/work/primary",
        extra_workspaces=("/work/extra-a", "/work/extra-b"),
        host_access=host_access,
        validate_artifact_sources=False,
    )
    values.update(kwargs)
    return render_run_vector(RunRenderInputs(**values))  # type: ignore[arg-type]


class TestRunWorkspaceContract(unittest.TestCase):
    def test_consecutive_stable_workspace_environment(self) -> None:
        vector = _render()
        env = _env(vector)
        self.assertEqual(
            [(key, value) for key, value in env.items() if key.startswith("WORKSPACE_PATH_")],
            [("WORKSPACE_PATH_1", "/work/primary"),
             ("WORKSPACE_PATH_2", "/work/extra-a"),
             ("WORKSPACE_PATH_3", "/work/extra-b")],
        )
        self.assertFalse(any(key.startswith("PROJECT_PATH_") for key in env))

    def test_host_access_uses_only_workspace_contract(self) -> None:
        env = _env(_render(RunHostAccess(address="172.17.0.1", mode="docker-gateway")))
        self.assertEqual(env["WORKSPACE_PATH_1"], "/work/primary")
        self.assertEqual(env["HOST_ACCESS_ADDRESS"], "172.17.0.1")
        self.assertFalse(any(key.startswith("PROJECT_PATH_") for key in env))

    def test_corporate_network_uses_only_workspace_contract(self) -> None:
        env = _env(_render(proxy_url="http://proxy.example:8080", proxy_no_proxy="localhost"))
        self.assertEqual(env["WORKSPACE_PATH_3"], "/work/extra-b")
        self.assertEqual(env["HTTPS_PROXY"], "http://proxy.example:8080")
        self.assertFalse(any(key.startswith("PROJECT_PATH_") for key in env))


class TestEntrypointWorkspaceContract(unittest.TestCase):
    def _run(self, *, mounted: tuple[str, ...] = (), enabled: str = "1", paths: dict[str, str] | None = None) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            workspace_paths = paths or {"WORKSPACE_PATH_1": tmp}
            env = {
                **{
                    key: value
                    for key, value in os.environ.items()
                    if not key.startswith("WORKSPACE_PATH_")
                },
                "_SCENARIO": "workspace-contract",
                "_ID_MODE": "root",
                "_MOUNT_RETURN_0": " ".join(mounted),
                "CHOWN_WORK_ON_START": enabled,
                **workspace_paths,
            }
            result = subprocess.run(
                ["bash", str(ROOT / "tests/entrypoint_harness.sh")],
                text=True, capture_output=True, env=env, check=True,
            )
            return result.stdout

    def test_mounted_workspace_is_repaired_registered_and_privileges_drop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trace = self._run(mounted=(tmp,), paths={"WORKSPACE_PATH_1": tmp})
        self.assertIn(f"mountpoint -q -- {tmp}", trace)
        self.assertIn("find ", trace)
        self.assertIn("chown dev:dev", trace)
        self.assertIn("chmod ug+rwX", trace)
        self.assertIn(f"git config --global --add safe.directory {tmp}", trace)
        self.assertIn("exec gosu dev:dev /bin/true", trace)

    def test_mounted_gap_is_rejected_before_any_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trace = self._run(
                mounted=(tmp,), paths={"WORKSPACE_PATH_2": tmp},
            )
        self.assertIn("exit_code 1", trace)
        self.assertNotIn("mountpoint -q", trace)
        self.assertNotIn("chown dev:dev", trace)
        self.assertNotIn("safe.directory", trace)
        self.assertNotIn("exec gosu dev:dev", trace)

    def test_malformed_suffix_is_rejected_before_any_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trace = self._run(
                mounted=(tmp,), paths={"WORKSPACE_PATH_X": tmp},
            )
        self.assertIn("exit_code 1", trace)
        self.assertNotIn("mountpoint -q", trace)
        self.assertNotIn("chown dev:dev", trace)
        self.assertNotIn("exec gosu dev:dev", trace)

    def test_non_mount_is_skipped_after_valid_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trace = self._run(paths={"WORKSPACE_PATH_1": tmp})
        self.assertIn(f"mountpoint -q -- {tmp}", trace)
        self.assertNotIn("chown dev:dev", trace)
        self.assertNotIn("safe.directory", trace)
        self.assertIn("exec gosu dev:dev /bin/true", trace)

    def test_disabled_repair_still_drops_privileges(self) -> None:
        trace = self._run(enabled="0")
        self.assertNotIn("mountpoint -q", trace)
        self.assertNotIn("chown dev:dev", trace)
        self.assertIn("exec gosu dev:dev /bin/true", trace)

    def test_scans_are_symlink_safe_and_preserve_executable_intent(self) -> None:
        self.assertNotIn("find -L", ENTRYPOINT)
        self.assertIn("find \"${1}\"", ENTRYPOINT)
        self.assertIn("chmod ug+rwX", ENTRYPOINT)


class TestImageFixtureCompatibility(unittest.TestCase):
    def test_launcher_entrypoint_and_verifier_share_exact_contract(self) -> None:
        for source in (LAUNCHER, ENTRYPOINT, VERIFIER):
            self.assertIn("WORKSPACE_PATH_", source)
            self.assertNotIn("PROJECT_PATH_", source)

    def test_image_assembly_has_no_whole_home_chown(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        setup = (ROOT / "docker/setup-dev-user.sh").read_text(encoding="utf-8")
        self.assertNotRegex(dockerfile, r"chown\s+-R\s+[^\n]*?/home/dev(?:\s|$)")
        self.assertNotRegex(setup, r"chown\s+-R\s+[^\n]*?/home/dev(?:\s|$)")
        self.assertIn("WORKSPACE_PATH_", ENTRYPOINT)

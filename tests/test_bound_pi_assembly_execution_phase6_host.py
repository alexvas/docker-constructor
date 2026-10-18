"""Task 6.1 host-harness capability checks (no registry required)."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import cast

from docker.npm_environment import (
    AssemblyTimeoutError,
    DockerRunExecutor,
    RootSpec,
    assemble_environment,
    assembler_script_digest,
    compute_assembler_identity,
    compute_assembler_input_identity,
    npm_policy_digest,
    preflight,
)
from docker.versioning.build_orchestration import (
    BuildExecutor,
    BuildRequest,
    ProcessResult,
    orchestrate_build,
)
from docker.versioning.dispatch_types import ExitKind
from docker.versioning.effective import resolve_build_projection
from docker.versioning.inventory import load_inventory
from tests.phase6_acceptance_harness import ManagedChild, make_lock, make_package


class TestPhase6HostHarness(unittest.TestCase):
    def test_can_keep_process_alive_past_a_short_deadline_and_reap_it(self):
        with ManagedChild.python("import time; time.sleep(60)") as child:
            deadline = time.monotonic() + 0.05
            while time.monotonic() < deadline:
                self.assertIsNone(child.process.poll())
                time.sleep(0.005)
            child.stop()
            self.assertIsNotNone(child.process.returncode)

    def test_can_emit_large_diagnostics_without_deadlocking(self):
        size = 2 * 1024 * 1024
        with ManagedChild.python(
            f"import os; os.write(1, b'o'*{size}); os.write(2, b'e'*{size})"
        ) as child:
            stdout, stderr = child.process.communicate(timeout=10)
        self.assertEqual(len(stdout), size)
        self.assertEqual(len(stderr), size)

    def test_can_interrupt_and_reap_process_group(self):
        with ManagedChild.python("import time; time.sleep(60)") as child:
            child.interrupt()
            child.process.wait(timeout=5)
            self.assertIn(child.process.returncode, (-signal.SIGINT, 130))
            with self.assertRaises(ProcessLookupError):
                os.kill(child.process.pid, 0)


class _StallingDockerExecutor(DockerRunExecutor):
    def run_streaming(self, argv, **kwargs):
        changed = (
            *argv[:-1],
            "printf 'phase6-large-diagnostic %.0s' $(seq 1 100000) >&2; sleep 60",
        )
        kwargs["deadline_seconds"] = 0.2
        kwargs["grace_seconds"] = 1.0
        return super().run_streaming(changed, **kwargs)


@unittest.skipUnless(
    os.environ.get("BOUND_PI_PHASE6_FORBID_REGISTRY") == "1",
    "run through validate-bound-pi-assembly-execution-phase6-host",
)
class TestDockerBackedHostAcceptance(unittest.TestCase):
    def test_outer_deadline_reaps_container_and_removes_staging(self):
        with tempfile.TemporaryDirectory(prefix="phase6-host-docker-") as temporary:
            cache = Path(temporary) / "cache"
            cache.mkdir(mode=0o700)
            package = make_package()
            projection = resolve_build_projection(
                load_inventory(
                    Path(__file__).resolve().parents[1] / "docker-constructor.toml"
                ).build,
                {},
                platform="linux-amd64",
            )
            validated = preflight(
                make_lock(package),
                roots=(RootSpec(package.name, package.version),),
                platform="linux-x64",
                node_version=projection.node.node_version,
                npm_version=projection.node.npm_version,
            )
            assembler = compute_assembler_identity(
                image_digest=projection.node.image,
                node_version=projection.node.node_version,
                npm_version=projection.node.npm_version,
                script_digest=assembler_script_digest(),
                policy_digest=npm_policy_digest(),
                platform="linux-x64",
            )
            input_identity = compute_assembler_input_identity(validated, assembler)
            container = "npm-assembler-" + input_identity.digest[:16]
            resource_file = os.environ.get("BOUND_PI_PHASE6_RESOURCE_FILE")
            if resource_file:
                with open(resource_file, "a", encoding="utf-8") as resources:
                    resources.write(container + "\n")
            with self.assertRaises(AssemblyTimeoutError) as raised:
                assemble_environment(
                    validated=validated,
                    assembler=assembler,
                    cache_root=cache,
                    executor=_StallingDockerExecutor(),
                )
            self.assertEqual(raised.exception.reason, "assembly_timeout")
            self.assertLessEqual(
                len(raised.exception.detail.encode()), 2 * 64 * 1024 + 4096
            )
            self.assertEqual([], list(cache.rglob("staging/*")))
            self.assertNotEqual(
                subprocess.run(
                    ("docker", "inspect", container),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ).returncode,
                0,
            )

            repo = Path(temporary) / "repo"
            repo.mkdir()
            shutil.copyfile(
                Path(__file__).resolve().parents[1] / "docker-constructor.toml",
                repo / "docker-constructor.toml",
            )
            (repo / "docker-constructor.local.toml").write_text(
                f'[cache]\ndir = "{cache}"\n'
            )
            artifact = repo / "artifact"
            artifact.write_bytes(b"phase6")

            def stall_pi(_projection, **_kwargs):
                return assemble_environment(
                    validated=validated,
                    assembler=assembler,
                    cache_root=cache,
                    executor=_StallingDockerExecutor(),
                )

            class NoMainBuild:
                calls = 0

                def run(inner_self, _argv: tuple[str, ...]) -> ProcessResult:
                    inner_self.calls += 1
                    raise AssertionError("main Docker build must not run after timeout")

            main = NoMainBuild()
            result = orchestrate_build(
                BuildRequest(
                    inventory_path=str(repo / "docker-constructor.toml"),
                    context=str(repo),
                    confirmed=True,
                    runner=cast(BuildExecutor, main),
                    _materialize_artifacts=lambda *_args, **_kwargs: (artifact,) * 4,
                    _materialize_pi=stall_pi,
                    _named_context_supported=lambda: True,
                    project_root=Path(str(repo / "docker-constructor.toml"))
                    .resolve()
                    .parent,
                )
            )
            self.assertEqual(result.exit_kind, ExitKind.OPERATIONAL)
            self.assertEqual(main.calls, 0)
            self.assertNotEqual(
                subprocess.run(
                    ("docker", "inspect", container),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ).returncode,
                0,
            )


if __name__ == "__main__":
    unittest.main()

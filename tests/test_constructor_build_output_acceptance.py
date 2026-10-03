"""End-to-end Docker build-output acceptance tests using a fake docker binary."""
from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


_FAKE_DOCKER = """#!{python}
import os
import sys
import time
scenario = os.environ['FAKE_DOCKER_SCENARIO']
if scenario == 'success-silent':
    pass
elif scenario == 'stream-success':
    print('first', flush=True)
    release_file = os.environ['FAKE_DOCKER_RELEASE_FILE']
    while not os.path.exists(release_file):
        time.sleep(.01)
    print('second', flush=True)
elif scenario == 'stream-failure':
    print('UNIQUE-DOCKER-FAILURE', file=sys.stderr, flush=True)
    sys.exit(23)
elif scenario == 'json-success':
    print('docker-stdout', flush=True)
    print('docker-stderr', file=sys.stderr, flush=True)
elif scenario == 'json-failure':
    print('docker-failure-stdout', flush=True)
    print('docker-failure-stderr', file=sys.stderr, flush=True)
    sys.exit(29)
else:
    raise AssertionError(scenario)
"""


class TestBuildOutputEndToEnd(unittest.TestCase):
    """Exercise CLI → orchestration → subprocess executor → renderer."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name) / "repo"
        source = Path(__file__).resolve().parents[1]
        shutil.copytree(source, cls.root, ignore=shutil.ignore_patterns(
            ".git", "__pycache__", ".docker-generated", ".docker-cache", ".docker-local",
            "docker-constructor.local.toml",
        ))
        if (cls.root / "docker-constructor.local.toml").exists():
            raise AssertionError("acceptance repository must not inherit a local companion")
        fake_dir = cls.root / "fake-bin"
        fake_dir.mkdir()
        fake = fake_dir / "docker"
        fake.write_text(_FAKE_DOCKER.format(python=sys.executable), encoding="utf-8")
        fake.chmod(0o755)
        cls.fake_dir = fake_dir
        # This subprocess fixture explicitly replaces the host materialization
        # dependency. Production code remains unchanged and fake Docker alone
        # does not bypass integrity checks.
        cls.acceptance_cli = cls.root / "acceptance_cli.py"
        cls.acceptance_cli.write_text(
            "import itertools\n"
            "import os\n"
            "import tempfile\n"
            "from pathlib import Path\n"
            "from types import SimpleNamespace\n"
            "from docker.versioning import build_orchestration\n"
            "from docker.versioning.build_snapshot import MaterializedSnapshot\n"
            "from tests.build_test_support import digest_valid_selected_artifacts, publish_digest_valid_artifacts\n"
            "from docker.constructor_cli import main\n"
            "with tempfile.TemporaryDirectory() as fixture_root:\n"
            "    fixture_root = Path(fixture_root)\n"
            "    blobs = fixture_root / 'blobs'; blobs.mkdir()\n"
            "    snapshots = fixture_root / 'snapshots'; snapshots.mkdir()\n"
            "    sequence = itertools.count()\n"
            "    def materialize(*args, **kwargs):\n"
            "        return publish_digest_valid_artifacts(*args, **kwargs)\n"
            "    def snapshot(*args, **kwargs):\n"
            "        path = snapshots / str(next(sequence)); path.mkdir()\n"
            "        return MaterializedSnapshot(path, path / 'manifest.json')\n"
            "    def materialize_pi(*args, **kwargs):\n"
            "        env_root = fixture_root / 'pi-env'; env_root.mkdir(exist_ok=True)\n"
            "        evidence = fixture_root / 'pi-evidence.json'; evidence.write_bytes(b'{}\\n')\n"
            "        return SimpleNamespace(\n"
            "            result=SimpleNamespace(environment_root=env_root, evidence_path=evidence),\n"
            "            launcher_plan=SimpleNamespace(contents=b'#!/bin/sh\\n', mode=0o555),\n"
            "            launcher_evidence=SimpleNamespace(data=b'{}\\n', digest='0' * 64),\n"
            "            output_identity='1' * 64,\n"
            "            tree_digest='2' * 64,\n"
            "            assembler_evidence_digest='3' * 64,\n"
            "            assembler_evidence_bytes_digest='5' * 64,\n"
            "            launcher_evidence_digest='4' * 64,\n"
            "        )\n"
            "    build_orchestration.materialize_build_artifacts = materialize\n"
            "    build_orchestration.select_build_artifacts = digest_valid_selected_artifacts\n"
            "    build_orchestration.materialize_pi = materialize_pi\n"
            "    build_orchestration.create_artifact_snapshot = snapshot\n"
            "    build_orchestration._default_named_context_supported = lambda: True\n"
            "    if os.environ.get('INJECT_CLEANUP_AGGREGATE') == '1':\n"
            "        from docker.versioning.build_cleanup import BuildCleanupError, CleanupFailure\n"
            "        failures = (\n"
            "            CleanupFailure('sha256:first.blob', OSError(5, 'first unlink failed')),\n"
            "            CleanupFailure('sha256:second.json', OSError(13, 'second unlink failed')),\n"
            "            CleanupFailure('markers', OSError(28, 'marker directory sync failed')),\n"
            "        )\n"
            "        def fail_recovery(*args, **kwargs):\n"
            "            raise BuildCleanupError('superseded cleanup failed for 3 step(s)', failures=failures)\n"
            "        build_orchestration.recover_build_generations = fail_recovery\n"
            "    commit_failure = os.environ.get('INJECT_COMMIT_FAILURE')\n"
            "    if commit_failure:\n"
            "        from docker.versioning.build_cache import PostCommitBuildError\n"
            "        from docker.versioning.build_cleanup import BuildCleanupError, CleanupFailure\n"
            "        def fail_commit(*args, **kwargs):\n"
            "            if commit_failure == 'pre':\n"
            "                raise OSError(5, 'generation publication fsync failed')\n"
            "            cleanup = BuildCleanupError(\n"
            "                'superseded cleanup failed for 2 step(s)',\n"
            "                failures=(\n"
            "                    CleanupFailure('sha256:candidate.blob', OSError(5, 'unlink failed')),\n"
            "                    CleanupFailure('blobs/sha256', OSError(28, 'directory sync failed')),\n"
            "                ),\n"
            "            )\n"
            "            try:\n"
            "                raise cleanup\n"
            "            except BuildCleanupError as cause:\n"
            "                raise PostCommitBuildError('post-publication cleanup failed') from cause\n"
            "        build_orchestration.commit_build_set = fail_commit\n"
            "    class NoNetworkTransport:\n"
            "        def __init__(self, policy=None): pass\n"
            "        def stream(self, url):\n"
            "            raise AssertionError('unexpected outbound network request: ' + url)\n"
            "    build_orchestration.UrllibStreamingTransport = NoNetworkTransport\n"
            "    raise SystemExit(main())\n",
            encoding="utf-8",
        )
        cls.release_dir = cls.root / "release-signals"
        cls.release_dir.mkdir()
        cls._release_number = 0

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _env(self, scenario: str) -> tuple[dict[str, str], Path]:
        type(self)._release_number += 1
        release_file = self.release_dir / f"release-{type(self)._release_number}"
        env = os.environ.copy()
        env.update({
            "PATH": f"{self.fake_dir}{os.pathsep}{env['PATH']}",
            "FAKE_DOCKER_SCENARIO": scenario,
            "FAKE_DOCKER_RELEASE_FILE": str(release_file),
            "XDG_CACHE_HOME": str(self.root / "xdg"),
        })
        return env, release_file

    def _command(self, *args: str) -> list[str]:
        return [sys.executable, str(self.acceptance_cli), *args]

    def test_text_progress_is_visible_before_cli_exits(self):
        env, release_file = self._env("stream-success")
        proc = subprocess.Popen(
            self._command("build", "-y"), cwd=self.root, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            assert proc.stdout is not None
            ready, _, _ = select.select([proc.stdout], [], [], 5)
            self.assertTrue(ready, "first Docker progress line was buffered until CLI exit")
            self.assertEqual("first\n", proc.stdout.readline())
            self.assertIsNone(proc.poll(), "CLI exited before first progress became observable")
            release_file.touch()
            stdout_tail, stderr = proc.communicate(timeout=5)
            self.assertEqual(0, proc.returncode, stderr)
            self.assertIn("second", stdout_tail)
        finally:
            release_file.touch(exist_ok=True)
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()

    def test_cleanup_aggregate_details_reach_cli_with_operational_exit(self):
        env, _ = self._env("success-silent")
        env["INJECT_CLEANUP_AGGREGATE"] = "1"
        completed = subprocess.run(
            self._command("build", "-y"), cwd=self.root, env=env,
            capture_output=True, text=True, timeout=3,
        )
        self.assertEqual(4, completed.returncode)
        self.assertIn("superseded cleanup failed for 3 step(s)", completed.stderr)
        for detail in (
            "sha256:first.blob", "first unlink failed",
            "sha256:second.json", "second unlink failed",
            "markers", "marker directory sync failed",
        ):
            self.assertIn(detail, completed.stderr)

    def test_prepublication_commit_failure_has_no_committed_state_claim(self):
        env, _ = self._env("success-silent")
        env["INJECT_COMMIT_FAILURE"] = "pre"
        completed = subprocess.run(
            self._command("build", "-y"), cwd=self.root, env=env,
            capture_output=True, text=True, timeout=3,
        )
        self.assertEqual(4, completed.returncode)
        self.assertIn("generation publication fsync failed", completed.stderr)
        self.assertNotIn("newest build generation remains committed", completed.stderr)
        self.assertNotIn("Recovery is required", completed.stderr)

    def test_postpublication_commit_failure_renders_state_and_details(self):
        env, _ = self._env("success-silent")
        env["INJECT_COMMIT_FAILURE"] = "post"
        completed = subprocess.run(
            self._command("build", "-y"), cwd=self.root, env=env,
            capture_output=True, text=True, timeout=3,
        )
        self.assertEqual(4, completed.returncode)
        self.assertIn("image built successfully and remains available", completed.stderr)
        self.assertIn("newest build generation remains committed", completed.stderr)
        self.assertIn("Recovery is required", completed.stderr)
        for detail in (
            "superseded cleanup failed for 2 step(s)",
            "sha256:candidate.blob", "unlink failed",
            "blobs/sha256", "directory sync failed",
        ):
            self.assertIn(detail, completed.stderr)

    def test_streamed_failure_is_not_replayed_and_maps_operational_exit(self):
        env, _ = self._env("stream-failure")
        completed = subprocess.run(
            self._command("build", "-y"), cwd=self.root, env=env,
            capture_output=True, text=True, timeout=3,
        )
        self.assertEqual(4, completed.returncode)
        self.assertEqual(1, completed.stderr.count("UNIQUE-DOCKER-FAILURE"))
        self.assertIn("23", completed.stderr)

    def test_json_success_is_one_isolated_document(self):
        env, _ = self._env("json-success")
        completed = subprocess.run(
            self._command("--output", "json", "build", "-y"), cwd=self.root,
            env=env, capture_output=True, text=True, timeout=3,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(completed.stdout.strip(), json.dumps(payload, indent=2, sort_keys=True))
        self.assertEqual("success", payload["status"])
        self.assertEqual("docker-stdout\n", payload["data"]["stdout"])
        self.assertEqual("docker-stderr\n", payload["data"]["stderr"]) 
        self.assertEqual("", completed.stderr)

    def test_normal_text_hides_published_path_but_verbose_and_json_expose_it(self):
        from docker.versioning.project_state import resolve_project_state
        expected_path = resolve_project_state(
            self.root, cache_root=self.root / "xdg" / "docker-constructor",
        ).generated_root / "docker-constructor.build.effective.toml"

        env, _ = self._env("success-silent")
        text = subprocess.run(
            self._command("build", "-y"), cwd=self.root, env=env,
            capture_output=True, text=True, timeout=3,
        )
        self.assertEqual(0, text.returncode, text.stderr)
        self.assertTrue(expected_path.is_file())
        self.assertNotIn("published_path", text.stdout)
        self.assertNotIn(str(expected_path), text.stdout)

        env, _ = self._env("success-silent")
        verbose = subprocess.run(
            self._command("--verbose", "build", "-y"), cwd=self.root, env=env,
            capture_output=True, text=True, timeout=3,
        )
        self.assertEqual(0, verbose.returncode, verbose.stderr)
        self.assertIn("published_path", verbose.stdout)
        self.assertIn(str(expected_path), verbose.stdout)

        env, _ = self._env("success-silent")
        structured = subprocess.run(
            self._command("--output", "json", "build", "-y"), cwd=self.root, env=env,
            capture_output=True, text=True, timeout=3,
        )
        self.assertEqual(0, structured.returncode, structured.stderr)
        self.assertEqual(str(expected_path), json.loads(structured.stdout)["data"]["published_path"])

    def test_json_failure_contains_captured_diagnostics_and_operational_exit(self):
        env, _ = self._env("json-failure")
        completed = subprocess.run(
            self._command("--output", "json", "build", "-y"), cwd=self.root,
            env=env, capture_output=True, text=True, timeout=3,
        )
        self.assertEqual(4, completed.returncode)
        self.assertEqual("", completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual("operational", payload["status"])
        self.assertEqual({
            "return_code": 29,
            "stdout": "docker-failure-stdout\n",
            "stderr": "docker-failure-stderr\n",
            "output_policy": "captured",
        }, {key: payload["data"][key] for key in
            ("return_code", "stdout", "stderr", "output_policy")})

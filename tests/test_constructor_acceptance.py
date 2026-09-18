"""Acceptance fixtures for daemon-independent failure diagnosis.

Each test simulates a distinct end-to-end failure through the facade
(CLI → dispatch → handler → output) and asserts that the resulting
exit code, structured data, and human-readable message together
provide enough evidence for diagnosis **without** Docker.

No test touches a real Docker daemon, socket, or network.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Callable, Sequence

from tests.build_test_support import INVENTORY_PATH, fake_pi_materialization, fixture_directory, no_network_transport_factory

# ── helpers ────────────────────────────────────────────────────────────

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

def _load_mod() -> Any:
    from docker import constructor_cli
    return constructor_cli

def _run(
    mod: Any,
    argv: Sequence[str],
    *,
    dispatcher: Any = None,
    stdout_isatty: bool = False,
    stderr_isatty: bool = False,
    _prompt_user: Callable[[str], bool] | None = None,
    _process_runner: Any = None,
    _container_inspector: Any = None,
    _run_executor: Any = None,
    _create_projection: Any = None,
    _workspace_selector: Any = None,
) -> tuple[int, str, str]:
    # Run commands now resolve external project state from the selected
    # constructor project; integration fixtures must provide that readable
    # directory rather than relying on the former lexical checkout paths.
    for index, value in enumerate(argv[:-1]):
        if value == "--workspace":
            Path(argv[index + 1]).mkdir(parents=True, exist_ok=True)
    out = io.StringIO()
    err = io.StringIO()
    # Older injected projection handles carry a checkout-local fixture path.
    # Bind them to the external runtime child supplied by orchestration.
    if _create_projection is not None:
        original_create_projection = _create_projection
        def _create_projection(_projection: Any, *, parent_dir: str) -> Any:
            handle = original_create_projection(_projection, parent_dir=parent_dir)
            handle.path = str(Path(parent_dir) / "fake-projection.toml")
            return handle
    # This daemon-independent acceptance module never exercises transport or
    # cache materialization. Keep every real-launch fixture network-free even
    # when the isolated validation cache starts empty.
    with redirect_stdout(out), redirect_stderr(err), patch(
        "docker.launcher.artifact_cache.materialize_selected_artifacts",
        return_value={},
    ):
        rc = mod.main(
            list(argv),
            dispatcher=dispatcher,
            stdout_isatty=lambda: stdout_isatty,
            stderr_isatty=lambda: stderr_isatty,
            _prompt_user=_prompt_user,
            _process_runner=_process_runner,
            _container_inspector=_container_inspector,
            _run_executor=_run_executor,
            _create_projection=_create_projection,
            _workspace_selector=_workspace_selector,
        )
    return rc, out.getvalue(), err.getvalue()

def _strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)

def _make_fake_dispatcher(mod: Any, **kw: object) -> Any:
    """Return a callable that returns a single fixed CommandResult."""
    from docker.versioning.dispatch_types import ExitKind

    exit_kind = kw.pop("exit_kind", "success")
    data = kw.pop("data", None)
    message = kw.pop("message", None)
    debug = kw.pop("debug", None)

    def _dispatch(_cmd: str, _req: object) -> object:
        return mod.CommandResult(
            exit_kind=getattr(ExitKind, str(exit_kind).upper()),
            data=data,
            message=message,
            debug=debug,
        )

    return _dispatch

def _make_fake_process_runner(
    return_code: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> Any:
    from docker.launcher import ProcessResult

    class _Runner:
        def run(self, argv, *,
                mode=None):
            return ProcessResult(
                argv=tuple(argv), return_code=return_code,
                stdout=stdout, stderr=stderr,
            )

    return _Runner()

def _make_recording_runner(
    return_code: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> tuple[Any, list[tuple[str, ...]]]:
    from docker.launcher import ProcessResult

    calls: list[tuple[str, ...]] = []

    class _Rec:
        def run(self, argv, *,
                mode=None):
            calls.append(tuple(argv))
            return ProcessResult(
                argv=tuple(argv), return_code=return_code,
                stdout=stdout, stderr=stderr,
            )

    return _Rec(), calls

# ── Evidence bundle assertions ─────────────────────────────────────────

def _assert_evidence_bundle_complete(
    test_case: unittest.TestCase,
    evidence_dir: str,
    *,
    min_commands: int = 1,
    expect_command_substrs: Sequence[str] = (),
    expect_argv_substr: str | None = None,
) -> dict[str, Any]:
    """Verify that an evidence bundle written to *evidence_dir*:

    * contains an ``index.txt`` with per-command exit code, duration,
      argv, checksums, and truncation metadata
    * has bounded ``*-stdout.txt`` / ``*-stderr.txt`` files when output
      is non-empty
    * includes commands whose argv matches *expect_command_substrs*
      (each substring must appear in at least one command's argv)
    * includes commands whose argv contains *expect_argv_substr*

    Returns the parsed index sections for further assertions.
    """
    import re as _re
    dir_path = Path(evidence_dir)
    test_case.assertTrue(dir_path.is_dir(),
                          f"evidence dir must exist: {evidence_dir}")

    index_path = dir_path / "index.txt"
    test_case.assertTrue(index_path.is_file(),
                          "index.txt must exist")
    index_text = index_path.read_text(encoding="utf-8")

    # ── structural metadata ────────────────────────────────────
    test_case.assertIn("Evidence collection for image:", index_text,
                        "index must identify the image")
    test_case.assertIn("Commands:", index_text,
                        "index must have a Commands section")
    test_case.assertIn("Notes:", index_text,
                        "index must have a Notes section")
    test_case.assertIn("Bundle checksum (sha256):", index_text,
                        "index must contain a bundle self-checksum")

    # ── parse command entries ───────────────────────────────────
    cmd_lines = [l for l in index_text.split("\n")
                 if _re.match(r"\s+\[\d+\]", l)]
    test_case.assertGreaterEqual(
        len(cmd_lines), min_commands,
        f"at least {min_commands} commands must be recorded, "
        f"got {len(cmd_lines)}")

    # Each command line has exit=, dur=, argv=.
    for i, line in enumerate(cmd_lines):
        test_case.assertIn("exit=", line,
                            f"command [{i:03d}] missing exit code")
        test_case.assertIn("dur=", line,
                            f"command [{i:03d}] missing duration")
        test_case.assertIn("argv=", line,
                            f"command [{i:03d}] missing argv")

    # ── argv assertions ─────────────────────────────────────────
    all_argv_text = "\n".join(cmd_lines)
    for substr in expect_command_substrs:
        test_case.assertIn(
            substr, all_argv_text,
            f"evidence must include command containing {substr!r}"
        )
    if expect_argv_substr:
        test_case.assertIn(
            expect_argv_substr, all_argv_text,
            f"evidence must include command with argv "
            f"containing {expect_argv_substr!r}"
        )

    # ── checksums ──────────────────────────────────────────────
    sha_lines = [l for l in index_text.split("\n")
                 if "sha256=" in l]
    # Checksums appear only when output is non-empty.
    for line in sha_lines:
        m = _re.search(r"sha256=([0-9a-f]{64})", line)
        test_case.assertTrue(m,
                              f"invalid checksum format: {line.strip()}")

    # ── bounded output files ───────────────────────────────────
    for pat, label in [("*-stdout.txt", "stdout"),
                        ("*-stderr.txt", "stderr")]:
        for sf in sorted(dir_path.glob(pat)):
            fsize = sf.stat().st_size
            test_case.assertLess(
                fsize, 2 * 1024 * 1024,
                f"{label} file {sf.name} must be bounded ({fsize} bytes)"
            )
            # Content matches the recorded checksum when a checksum is
            # present in the index.
            if sha_lines:
                actual_hash = hashlib.sha256(
                    sf.read_bytes()).hexdigest()
                found = False
                for line in sha_lines:
                    if sf.name in line and actual_hash in line:
                        found = True
                        break
                test_case.assertTrue(
                    found,
                    f"{sf.name} checksum mismatch: "
                    f"file sha256={actual_hash}, "
                    f"not found in index"
                )

    return {
        "index_text": index_text,
        "cmd_lines": cmd_lines,
        "stdout_files": sorted(dir_path.glob("*-stdout.txt")),
        "stderr_files": sorted(dir_path.glob("*-stderr.txt")),
        "command_count": len(cmd_lines),
    }

# ── Scripted Docker runner ─────────────────────────────────────────────

class _ScriptedProcessRunner:
    """A ``ProcessRunner`` that returns scripted responses based on
    command substring matching.

    Matches are checked in **reverse insertion order** — later
    registrations override earlier ones.  When no handler matches, the
    *fallback* (if set) is used; otherwise ``RuntimeError`` is raised.
    """

    def __init__(self, fallback_rc: int = 0,
                 fallback_stdout: str = "",
                 fallback_stderr: str = "") -> None:
        from docker.launcher import ProcessResult
        self._ProcessResult = ProcessResult
        self._handlers: list[tuple[str, tuple[int, str, str]]] = []
        self._called: list[tuple[str, ...]] = []
        self._fallback = (fallback_rc, fallback_stdout, fallback_stderr)

    def when(self, substr: str, *, rc: int = 0,
             stdout: str = "", stderr: str = "") -> _ScriptedProcessRunner:
        """Register a handler for commands whose joined string contains
        *substr*.  Later registrations take priority over earlier ones."""
        self._handlers.append((substr, (rc, stdout, stderr)))
        return self

    def run(self, argv: Sequence[str], *,
            interactive: bool = False) -> Any:
        cmd = " ".join(argv)
        self._called.append(tuple(argv))
        # Scan in reverse — last registered wins.
        for substr, (rc, stdout, stderr) in reversed(self._handlers):
            if substr in cmd:
                return self._ProcessResult(
                    argv=tuple(argv), return_code=rc,
                    stdout=stdout, stderr=stderr,
                )
        # Fallback
        frc, fout, ferr = self._fallback
        return self._ProcessResult(
            argv=tuple(argv), return_code=frc,
            stdout=fout, stderr=ferr,
        )

    @property
    def called(self) -> tuple[tuple[str, ...], ...]:
        """Commands seen by this runner, in call order."""
        return tuple(self._called)

def _build_happy_runtime_runner(
    container: str,
    proj_hash: str,
    workspace_paths: tuple[str, ...],
    extensions: dict[str, tuple[str, str]],
    gateway: str | None = None,
) -> _ScriptedProcessRunner:
    """Return a scripted runner where every runtime check **passes**.

    *workspace_paths* are container-side directory paths (e.g.
    ``("/tmp/p1",)``).  *extensions* maps extension key → (package,
    version).  When *gateway* is ``None``, the real operational
    gateway is read from the repository ``.env``.
    """
    if gateway is None:
        try:
            from docker.constructor_cli import _read_operational_gateway
            gateway = _read_operational_gateway()
        except Exception:
            gateway = "host-gateway"
    r = _ScriptedProcessRunner()
    # ── projection identity ──
    r.when("sha256sum",
           rc=0, stdout=f"{proj_hash}  "
                        "/run/pi-cli/docker-constructor.runtime.toml")
    # ── projection readonly ──
    r.when("docker-constructor.runtime.toml /proc/mounts",
           rc=0,
           stdout="/dev/sda1 /run/pi-cli/docker-constructor.runtime.toml"
                  " ext4 ro,nosuid,nodev,relatime 0 0")
    r.when("cat /proc/mounts",
           rc=0,
           stdout="proc /proc proc rw,nosuid,nodev,noexec,relatime 0 0\n"
                  "/dev/sda1 / ext4 rw,relatime 0 0\n"
                  "/dev/sda1 /run/pi-cli/docker-constructor.runtime.toml"
                  " ext4 ro,nosuid,nodev,relatime 0 0\n")
    r.when("test -w /run/pi-cli/docker-constructor.runtime.toml",
           rc=1)  # not writable
    # ── extensions ──
    for _key, (pkg, ver) in sorted(extensions.items()):
        r.when(f"cat /home/dev/.pi/agent/npm/node_modules/{pkg}/package.json",
               rc=0, stdout=json.dumps({"version": ver}))
    # ── workspace paths ──
    for i, pp in enumerate(workspace_paths, start=1):
        r.when(f"test -d {pp}", rc=0)
        r.when(f"printenv WORKSPACE_PATH_{i}", rc=0, stdout=pp)
    # guard: no next entry
    if workspace_paths:
        r.when(f"printenv WORKSPACE_PATH_{len(workspace_paths) + 1}", rc=1)
    # ── working directory ──
    if workspace_paths:
        r.when("pwd", rc=0, stdout=workspace_paths[0])
    else:
        r.when("pwd", rc=0, stdout="/home/dev")
    # ── ownership ──
    # Match stat commands by the last argument (the path).
    r.when("stat -c %U:%G /home/dev/.pi", rc=0, stdout="dev:dev")
    for i, pp in enumerate(workspace_paths, start=1):
        r.when(f"stat -c %U:%G {pp}", rc=0, stdout="dev:dev")
    # ── pi-home ──
    r.when("test -d /home/dev/.pi", rc=0)
    r.when("test -w /home/dev/.pi", rc=0)
    # ── gateway ──
    r.when("getent hosts host.docker.internal",
           rc=0, stdout=f"{gateway} host.docker.internal")
    # ── forbidden paths ──
    r.when("test -f /run/pi-cli/docker-constructor.toml", rc=1)
    r.when("test -f /run/pi-cli/docker-constructor.build.effective.toml",
           rc=1)
    return r

def _make_runtime_fixture() -> tuple[str, str, str, str]:
    """Create a temporary directory with the files needed for runtime
    verification and return (dir_path, inventory_path, projection_path,
    projection_hash).

    The directory contains:

    * ``docker-constructor.toml`` — reviewed inventory (copied from repo, host access disabled)
    * ``runtime.toml`` — effective runtime projection with one extension
    """
    td = tempfile.mkdtemp(prefix="acc-runtime-")
    td_p = Path(td)

    # Copy the repository's reviewed inventory (no [runtime.host-access]
    # → host access disabled).
    import shutil
    repo_root = Path(__file__).resolve().parents[1]
    shutil.copy(repo_root / "docker-constructor.toml", td_p / "docker-constructor.toml")

    # Effective runtime projection with one extension.
    proj = td_p / "runtime.toml"
    proj.write_text("""\
[extensions."@llblab/pi-codex-usage"]
package = "@llblab/pi-codex-usage"
version = "0.9.1"
""")
    proj_hash = hashlib.sha256(proj.read_bytes()).hexdigest()

    return td, str(td_p / "docker-constructor.toml"), str(proj), proj_hash

# ════════════════════════════════════════════════════════════════════════
# 14.1  Failed build (real orchestration, injected runner)
# ════════════════════════════════════════════════════════════════════════

class TestBuildFailureDiagnostics(unittest.TestCase):
    """Build failures exercised through :func:`orchestrate_build`
    with an injected process runner that returns a failed Docker command.

    No real Docker daemon, socket, or network is touched.
    """

    _INVENTORY: str | None = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = _load_mod()
        # A copied inventory prevents machine-local companions from affecting
        # orchestration tests that do not exercise companion resolution.
        cls._INVENTORY = str(INVENTORY_PATH)
        from docker.versioning.build_snapshot import MaterializedSnapshot
        def snapshot(*_args, **_kwargs):
            path = fixture_directory("fixture-snapshot-")
            return MaterializedSnapshot(path, path / "manifest.json")
        cls._snapshot_patcher = patch("docker.versioning.build_orchestration.create_artifact_snapshot", side_effect=snapshot)
        cls._snapshot_patcher.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._snapshot_patcher.stop()

    # ── helpers ────────────────────────────────────────────────────

    @staticmethod
    def _materialize_fixture(*_args, **_kwargs):
        root = fixture_directory("fixture-blobs-")
        return tuple(root / name for name in ("rustup.blob", "uv.blob", "rtk.blob", "fd.blob"))

    @staticmethod
    def _make_fake_diagnose(ip: str = "192.168.1.1") -> Callable[..., Any]:
        """Return a callable that emulates a successful gateway diagnosis."""
        from docker.networking import GatewayDiagnosis, DockerMode, ProbeResult
        def _fake_diagnose(**__: Any) -> GatewayDiagnosis:
            probe = ProbeResult(
                candidate=ip, ok=True,
                resolved_ip=ip, detail="fake-probe",
            )
            return GatewayDiagnosis(
                mode=DockerMode.ROOTLESS,
                probe_port=19999,
                probe_token="fake-token",
                lan_ip=None,
                probes=(probe,),
                chosen_gateway=ip,
                override_installed=False,
                override_needed=False,
            )
        return _fake_diagnose

    @staticmethod
    def _make_fake_persist() -> Callable[..., Any]:
        """Return a callable that emulates a successful gateway persistence."""
        from docker.networking import PersistenceResult
        def _fake_persist(_path: Any, _ip: str, **__: Any) -> PersistenceResult:
            return PersistenceResult(
                path=_path, address=_ip, written=True,
            )
        return _fake_persist

    @staticmethod
    def _make_fake_publish() -> Callable[..., Any]:
        """Return a callable that emulates a successful projection publish."""
        from docker.versioning.build_orchestration import PublishResult
        def _fake_publish(*_: Any, **__: Any) -> PublishResult:
            return PublishResult(
                published_path="/tmp/.docker-generated/fake-projection.toml",
            )
        return _fake_publish

    # ── tests ──────────────────────────────────────────────────────

    def test_failed_docker_build_arg_vector_preserved(self) -> None:
        """The exact ``docker build`` argument vector is retained
        in ``BuildResult.build_args`` and ``BuildResult.process_result``
        after a failed execution."""
        from docker.versioning.build_orchestration import (
            BuildRequest, ExitKind, orchestrate_build,
        )

        # Register a handler for "docker build" that returns a failure.
        runner = _ScriptedProcessRunner()
        runner.when("docker build", rc=1,
                    stdout="Step 1/5 : FROM alpine:3.20\n",
                    stderr="COPY failed: file not found: Dockerfile")

        req = BuildRequest(
            inventory_path=self._INVENTORY,
            platform="linux-amd64",
            confirmed=True,
            dry_run=False,
            runner=runner,
            _diagnose_gateway=self._make_fake_diagnose(),
            _publish_projection=self._make_fake_publish(),
            _named_context_supported=lambda: True,
            _transport_factory=no_network_transport_factory,
            _materialize_artifacts=self._materialize_fixture,
            _materialize_pi=fake_pi_materialization,
        project_root=Path(self._INVENTORY).resolve().parent)
        result = orchestrate_build(req)

        # orchestrate_build goes through plan → execute → calls runner.run
        self.assertEqual(ExitKind.OPERATIONAL, result.exit_kind)
        self.assertIsNotNone(result.build_args)
        self.assertIn("docker", result.build_args[0].lower())
        self.assertIn("build", result.build_args)
        # The executed command is preserved as-is.
        self.assertIsNotNone(result.process_result)
        self.assertEqual(tuple(result.build_args),
                         result.process_result.argv)

    def test_failed_docker_build_return_code_retained(self) -> None:
        """A non-zero Docker return code surfaces through
        ``process_result.return_code`` and the exit kind is
        ``OPERATIONAL``."""
        from docker.versioning.build_orchestration import (
            BuildRequest, ExitKind, orchestrate_build,
        )

        runner = _ScriptedProcessRunner()
        runner.when("docker build", rc=42,
                    stderr="unable to prepare context: path not found")

        req = BuildRequest(
            inventory_path=self._INVENTORY,
            platform="linux-amd64",
            confirmed=True,
            dry_run=False,
            runner=runner,
            _diagnose_gateway=self._make_fake_diagnose(),
            _publish_projection=self._make_fake_publish(),
            _named_context_supported=lambda: True,
            _transport_factory=no_network_transport_factory,
            _materialize_artifacts=self._materialize_fixture,
            _materialize_pi=fake_pi_materialization,
        project_root=Path(self._INVENTORY).resolve().parent)
        result = orchestrate_build(req)

        self.assertEqual(ExitKind.OPERATIONAL, result.exit_kind)
        self.assertIsNotNone(result.process_result)
        self.assertEqual(42, result.process_result.return_code)

    def test_failed_docker_build_stderr_preserved(self) -> None:
        """Raw stderr from the failed build command is retained
        so callers can diagnose without re-execution."""
        from docker.versioning.build_orchestration import (
            BuildRequest, ExitKind, orchestrate_build,
        )

        stderr_text = (
            "error building image: error building stage: "
            "failed to copy files: lstat /var/lib/docker/tmp/"
            "buildkit-mount12345/src/Dockerfile: no such file or directory"
        )
        runner = _ScriptedProcessRunner()
        runner.when("docker build", rc=1, stderr=stderr_text)

        req = BuildRequest(
            inventory_path=self._INVENTORY,
            platform="linux-amd64",
            confirmed=True,
            dry_run=False,
            runner=runner,
            _diagnose_gateway=self._make_fake_diagnose(),
            _publish_projection=self._make_fake_publish(),
            _named_context_supported=lambda: True,
            _transport_factory=no_network_transport_factory,
            _materialize_artifacts=self._materialize_fixture,
            _materialize_pi=fake_pi_materialization,
        project_root=Path(self._INVENTORY).resolve().parent)
        result = orchestrate_build(req)

        self.assertIsNotNone(result.process_result)
        self.assertIn("failed to copy files",
                       result.process_result.stderr)
        self.assertEqual(stderr_text, result.process_result.stderr)

    def test_dry_run_skips_execution_but_renders_build_args(self) -> None:
        """In ``--dry-run`` mode, orchestration skips Docker execution
        entirely (no runner invocation) but still renders build args."""
        from docker.versioning.build_orchestration import (
            BuildRequest, ExitKind, orchestrate_build,
        )

        runner = _ScriptedProcessRunner()
        runner.when("docker build", rc=1,
                    stderr="this should never be called")

        req = BuildRequest(
            inventory_path=self._INVENTORY,
            platform="linux-amd64",
            confirmed=True,
            dry_run=True,
            runner=runner,
            _diagnose_gateway=self._make_fake_diagnose(),
            _publish_projection=self._make_fake_publish(),
            _named_context_supported=lambda: True,
            _transport_factory=no_network_transport_factory,
            _materialize_artifacts=self._materialize_fixture,
            _materialize_pi=fake_pi_materialization,
        project_root=Path(self._INVENTORY).resolve().parent)
        result = orchestrate_build(req)

        self.assertEqual(ExitKind.SUCCESS, result.exit_kind)
        self.assertEqual((), result.build_args)
        self.assertIn("Planned build (not executable)", result.display_string or "")
        self.assertIn("constructor-artifacts=<prospective:not-materialized>", result.display_string or "")
        # Dry run: never touched the runner.
        self.assertIsNone(result.process_result)
        self.assertEqual((), runner.called)

    def test_no_network_transport_rejects_any_outbound_request(self) -> None:
        """The acceptance transport factory must fail fast, never fetch.

        Every build path above injects a fake Pi materializer, but the
        transport factory is additionally pinned to a no-network transport so
        a future regression that drops the injection fails immediately instead
        of reaching the live GitHub release endpoint.
        """
        transport = no_network_transport_factory()
        with self.assertRaises(AssertionError) as ctx:
            list(transport.stream(
                "https://github.com/earendil-works/pi/releases/download/v0.84.4/SHA256SUMS"
            ))
        self.assertIn("unexpected outbound network request", str(ctx.exception))

# ════════════════════════════════════════════════════════════════════════
# 14.1  Failed run
# ════════════════════════════════════════════════════════════════════════

class TestRunFailureDiagnostics(unittest.TestCase):
    """Run failures exercised through :func:`orchestrate_run`
    with injected executors and inspectors.

    No real Docker daemon, socket, or network is touched.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = _load_mod()

    # ── helpers ────────────────────────────────────────────────────

    @staticmethod
    def _fake_inspector(*names: str) -> Any:
        """Return an inspector *instance* whose ``list_names()``
        returns the given set of names (so ``allocate_pi_name`` picks
        the next available)."""
        _names = set(names)
        class _Insp:
            def __init__(self, runner: Any = None) -> None: pass
            def list_names(self) -> set[str]:
                return _names.copy()
        return _Insp()

    class _ProjectionHandle:
        """Minimal projection context manager that exists just long
        enough for the orchestrator to read its attributes."""
        entered: bool = False
        exited: bool = False
        projection: object = None
        path: str = "/tmp/.docker-generated/runtime/fake-projection.toml"
        content_hash: str = "abc123def456"
        def __enter__(self) -> "TestRunFailureDiagnostics._ProjectionHandle":
            self.entered = True
            return self
        def __exit__(self, *a: object) -> None:
            self.exited = True

    @staticmethod
    def _scripted_executor(
        return_code: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> Any:
        """Return an executor whose ``run(argv)`` returns a
        ``ProcessResult`` with the given outcome."""
        from docker.launcher import ProcessResult
        class Executor:
            def run(self, argv: tuple[str, ...], *,
                    interactive: bool = False) -> ProcessResult:
                return ProcessResult(
                    argv=tuple(argv),
                    return_code=return_code,
                    stdout=stdout,
                    stderr=stderr,
                )
        return Executor()

    # ── tests ──────────────────────────────────────────────────────

    def test_oserror_retains_run_args_and_raw_error(self) -> None:
        """When the executor raises ``OSError``, the rendered
        ``docker run`` argument vector and the raw exception text
        are preserved in the result."""
        import errno

        class _FailingExecutor:
            def run(self, argv: tuple[str, ...], *,
                    interactive: bool = False) -> None:
                raise OSError(
                    errno.ENOENT,
                    "docker: command not found",
                )

        runner = _make_fake_process_runner(
            return_code=0, stdout="pi-0001\n",
        )

        rc, out, err = _run(
            self.m,
            ["run", "--workspace", "/tmp/fake-project"],
            _process_runner=runner,
            _container_inspector=self._fake_inspector("pi-0001"),
            _run_executor=_FailingExecutor(),
            _create_projection=lambda p, **kw: self._ProjectionHandle(),
            _prompt_user=lambda _: True,
        )
        self.assertEqual(4, rc)
        err_clean = _strip_ansi(err)
        self.assertIn("docker", err_clean.lower(),
                       "diagnostic must name the missing tool")
        # The error text itself must also be reachable — the
        # orchestrator propagates ``str(exc)`` into the message.
        self.assertIn("command not found", err_clean)

        # Verify structural data through JSON output.
        rc_j, out_j, _ = _run(
            self.m,
            ["--output", "json", "run", "--workspace",
             "/tmp/fake-project"],
            _process_runner=runner,
            _container_inspector=self._fake_inspector("pi-0001"),
            _run_executor=_FailingExecutor(),
            _create_projection=lambda p, **kw: self._ProjectionHandle(),
            _prompt_user=lambda _: True,
        )
        self.assertEqual(4, rc_j)
        data = json.loads(out_j)
        self.assertEqual("operational", data.get("status"))
        # The message carries the raw OS error text.
        self.assertIn("command not found",
                       data.get("message", ""))
        # run_args are preserved even though execution never happened.
        self.assertIsNotNone(data.get("data"))
        self.assertIn("run_args", data["data"])
        self.assertIn("docker", data["data"]["run_args"][0])
        self.assertIn("run", data["data"]["run_args"])

    def test_nonzero_exit_retains_argv_rc_stderr(self) -> None:
        """A non-zero Docker exit preserves the exact ``docker run``
        argv, return code, and raw stderr in the structured result."""

        stderr_raw = (
            "docker: Error response from daemon: "
            "Cannot start container abc123: "
            "mount denied: the source path /host/path "
            "is not a valid Windows path.\n"
        )
        executor = self._scripted_executor(
            return_code=127, stderr=stderr_raw,
        )

        runner = _make_fake_process_runner(
            return_code=0, stdout="pi-0001\n",
        )

        rc, out, err = _run(
            self.m,
            ["--output", "json", "run", "--workspace",
             "/tmp/fake-project", "--no-tty", "--no-interactive"],
            _process_runner=runner,
            _container_inspector=self._fake_inspector("pi-0001"),
            _run_executor=executor,
            _create_projection=lambda p, **kw: self._ProjectionHandle(),
            _prompt_user=lambda _: True,
        )
        self.assertEqual(4, rc)
        self.assertEqual("", err)
        data = json.loads(out)
        self.assertEqual("operational", data.get("status"))
        # Mode marker for captured execution.
        self.assertEqual("captured", data["data"]["mode"])
        # run_args preserved.
        self.assertIn("run_args", data["data"])
        self.assertIn("docker", data["data"]["run_args"][0])
        self.assertIn("run", data["data"]["run_args"])
        # Exit code preserved.
        self.assertEqual(127, data["data"]["exit_code"])
        # Raw stderr preserved — the original Docker error text.
        self.assertIn("Cannot start container", data["data"]["stderr"])
        self.assertIn("mount denied", data["data"]["stderr"])
        # The message summarises the exit.
        self.assertIn("127", data.get("message", ""))

    def test_missing_workspace_returns_config(self) -> None:
        """When ``--workspace`` is absent and TUI is not
        requested, ``resolve_workspace_selection`` raises
        ``NoWorkspaceError`` and the facade returns CONFIG."""
        rc, out, err = _run(
            self.m,
            ["run", "--dry-run"],
            _prompt_user=lambda _: True,
        )
        self.assertEqual(3, rc)
        self.assertIn("no primary workspace", _strip_ansi(err).lower())

    def test_captured_failure_exposes_both_streams_in_json(self) -> None:
        """In captured (non-interactive) mode, both stdout and
        stderr are captured independently and surfaced in the JSON
        result so callers can diagnose without guessing which
        stream holds the error."""
        stderr_text = "error: container failed to start"
        stdout_text = "captured standard output"
        executor = self._scripted_executor(
            return_code=1,
            stdout=stdout_text,
            stderr=stderr_text,
        )

        runner = _make_fake_process_runner(
            return_code=0, stdout="pi-0001\n",
        )

        with tempfile.TemporaryDirectory() as td:
            rc, out, err = _run(
                self.m,
                ["--output", "json", "run", "--workspace",
                 str(Path(td) / "main-project"),
                 "--no-tty", "--no-interactive"],
                _process_runner=runner,
                _container_inspector=self._fake_inspector("pi-0001"),
                _run_executor=executor,
                _create_projection=lambda p, **kw: self._ProjectionHandle(),
                _prompt_user=lambda _: True,
            )
        self.assertEqual(4, rc)
        self.assertEqual("", err)
        data = json.loads(out)
        self.assertEqual("operational", data.get("status"))
        # Mode marker confirms captured (non-interactive) execution.
        self.assertEqual("captured", data["data"]["mode"])
        # Both streams must be present.
        self.assertIn("stderr", data["data"])
        self.assertIn(stderr_text, data["data"]["stderr"])
        # Captured mode preserves both streams independently.
        self.assertIn("stdout", data["data"],
                       "stdout must be captured alongside stderr "
                       "in captured non-interactive mode")
        self.assertIn(stdout_text, data["data"]["stdout"])

    def test_captured_failure_text_mode_shows_stdout_diagnostics(self) -> None:
        """In captured (non-interactive) mode, text-mode diagnostics
        surface stdout with a clear label even when stderr is empty,
        so the user always sees the real failure text."""
        stdout_text = (
            "captured output with container failure\n"
        )
        executor = self._scripted_executor(
            return_code=1,
            stdout=stdout_text,
            stderr="",   # only stdout carries the diagnostic
        )

        runner = _make_fake_process_runner(
            return_code=0, stdout="pi-0001\n",
        )

        with tempfile.TemporaryDirectory() as td:
            rc, out, err = _run(
                self.m,
                ["run", "--workspace", str(Path(td) / "main-project"),
                 "--no-tty", "--no-interactive"],
                _process_runner=runner,
                _container_inspector=self._fake_inspector("pi-0001"),
                _run_executor=executor,
                _create_projection=lambda p, **kw: self._ProjectionHandle(),
                _prompt_user=lambda _: True,
            )

        self.assertEqual(4, rc)
        self.assertEqual("", out)
        # The diagnostic stream must contain the captured
        # stdout content.
        self.assertIn(stdout_text.strip(), err,
                       "text-mode failure must surface captured "
                       "stdout diagnostics")
        # The output must label captured streams clearly.
        self.assertIn("stdout", err.lower(),
                       "the output must label the captured stdout "
                       "clearly (e.g. 'stdout: ...')")
        # No duplication of diagnostic content.
        count = err.count(stdout_text.strip())
        self.assertEqual(1, count,
                          f"diagnostic content appears {count} times, "
                          f"expected exactly once")

    def test_interactive_json_preserves_identity_omits_streams(self) -> None:
        """In interactive (streaming) mode the JSON result carries
        ``"mode": "interactive"``, preserves exit_code and identity
        fields, and deliberately omits stdout/stderr because output
        was already delivered to the host terminal."""
        runner = _make_fake_process_runner(
            return_code=0, stdout="pi-0001\n",
        )
        executor = self._scripted_executor(
            return_code=1,
            stdout="",   # not captured — inherited by terminal
            stderr="",   # not captured — inherited by terminal
        )

        rc, out, err = _run(
            self.m,
            ["--output", "json", "run", "--workspace",
             "/tmp/fake-project", "--tty"],
            _process_runner=runner,
            _container_inspector=self._fake_inspector("pi-0001"),
            _run_executor=executor,
            _create_projection=lambda p, **kw: self._ProjectionHandle(),
            _prompt_user=lambda _: True,
        )
        self.assertEqual(4, rc)
        self.assertEqual("", err)
        data = json.loads(out)
        self.assertEqual("operational", data.get("status"))

        # Schema contract: mode marker.
        self.assertEqual("interactive", data["data"]["mode"],
                         "interactive execution must be recorded "
                         "as mode=interactive")

        # Identity fields preserved.
        for key in ("exit_code", "container_name", "run_args",
                     "projection_hash"):
            self.assertIsNotNone(
                data["data"].get(key),
                f"{key} must be present in interactive JSON result",
            )

        # Streams deliberately omitted — output already on terminal.
        for stream in ("stderr", "stdout"):
            self.assertNotIn(
                stream, data["data"],
                f"{stream} must be absent from interactive JSON "
                f"result — output was streamed to the terminal",
            )

    # ── execution-mode wiring from CLI flags ─────────────────────

    def test_default_flags_select_streaming_execution(self) -> None:
        """With no ``--no-tty``/``--no-interactive`` overrides, the
        full facade path (CLI → RunRequest → orchestrator → executor)
        must select streaming/interactive execution so Docker
        inherits the host terminal."""
        modes_seen: list[dict[str, object]] = []

        class _SpyExecutor:
            def run(self, argv: tuple[str, ...], *,
                    interactive: bool = False) -> Any:
                modes_seen.append({"interactive": interactive})
                from docker.launcher import ProcessResult
                return ProcessResult(argv=argv, return_code=0)

        runner = _make_fake_process_runner(
            return_code=0, stdout="pi-0001\n",
        )

        rc, out, err = _run(
            self.m,
            ["run", "--workspace", "/tmp/fake-project"],
            _process_runner=runner,
            _container_inspector=self._fake_inspector("pi-0001"),
            _run_executor=_SpyExecutor(),
            _create_projection=lambda p, **kw: self._ProjectionHandle(),
            _prompt_user=lambda _: True,
        )
        self.assertEqual(0, rc)
        self.assertTrue(
            len(modes_seen) >= 1,
            "executor was never invoked through the facade",
        )
        # RED: the facade→orchestrator→executor chain never
        # forwards tty/stdin_open as interactive=True.
        self.assertTrue(
            modes_seen[0]["interactive"],
            "default CLI flags (tty=True, stdin_open=True) must "
            "select interactive/streaming execution mode",
        )

    def test_no_tty_no_interactive_selects_captured_execution(self) -> None:
        """When ``--no-tty --no-interactive`` is passed, the full
        facade path must select captured execution so stdout/stderr
        are available for diagnostics."""
        modes_seen: list[dict[str, object]] = []

        class _SpyExecutor:
            def run(self, argv: tuple[str, ...], *,
                    interactive: bool = True) -> Any:
                modes_seen.append({"interactive": interactive})
                from docker.launcher import ProcessResult
                return ProcessResult(argv=argv, return_code=0)

        runner = _make_fake_process_runner(
            return_code=0, stdout="pi-0001\n",
        )

        rc, out, err = _run(
            self.m,
            ["run", "--workspace", "/tmp/fake-project",
             "--no-tty", "--no-interactive"],
            _process_runner=runner,
            _container_inspector=self._fake_inspector("pi-0001"),
            _run_executor=_SpyExecutor(),
            _create_projection=lambda p, **kw: self._ProjectionHandle(),
            _prompt_user=lambda _: True,
        )
        self.assertEqual(0, rc)
        self.assertTrue(
            len(modes_seen) >= 1,
            "executor was never invoked through the facade",
        )
        # RED: the facade→orchestrator→executor chain never
        # forwards tty=False/stdin_open=False as interactive=False.
        self.assertFalse(
            modes_seen[0]["interactive"],
            "--no-tty --no-interactive must select captured "
            "execution mode",
        )

    def test_interactive_nonzero_exit_maps_rc_and_no_duplicate_output(
        self,
    ) -> None:
        """When a container exits nonzero in interactive mode,
        the diagnostics have already appeared on the terminal
        (streamed via Docker).  The facade must:

        * map the return code to ``ExitKind.OPERATIONAL`` (rc=4)
        * NOT render the captured streams a second time (they
          are empty because ``capture_output`` was disabled)
        * still include the exit code in structured output
        """
        runner = _make_fake_process_runner(
            return_code=0, stdout="pi-0001\n",
        )
        executor = self._scripted_executor(
            return_code=1,
            stdout="",   # not captured in interactive mode
            stderr="",   # not captured in interactive mode
        )

        rc, out, err = _run(
            self.m,
            ["run", "--workspace", "/tmp/fake-project", "--tty"],
            _process_runner=runner,
            _container_inspector=self._fake_inspector("pi-0001"),
            _run_executor=executor,
            _create_projection=lambda p, **kw: self._ProjectionHandle(),
            _prompt_user=lambda _: True,
        )
        self.assertEqual(4, rc,
                         "nonzero interactive exit must map to "
                         "OPERATIONAL (exit code 4)")
        self.assertEqual("", out,
                         "stdout must be empty for error output")
        # The diagnostic line must appear exactly once.
        self.assertIn("[OPERATIONAL]", err)
        self.assertIn("exited with code 1", err)
        # No duplicate rendering — the exit message must appear
        # exactly once on stderr.
        self.assertEqual(
            1, err.count("exited with code 1"),
            "exit diagnostic must appear exactly once — "
            "streamed output must not be rendered a second time",
        )
        # In interactive mode, captured streams are empty; the
        # renderer must not emit a noisy "data: ..." line with
        # empty/None content.
        self.assertNotIn("data:", err,
                         "empty streams must not produce a 'data:' "
                         "line in interactive mode")

    def test_interactive_output_reaches_host_streams_not_captured(
        self,
    ) -> None:
        """Regression: in streaming/interactive mode Docker
        output must appear on the host terminal (inherited
        stdin/stdout/stderr) and must NOT be hidden inside
        ``ProcessResult.stdout`` for the facade to render a
        second time.

        The observed failure mode is:

        1. Docker streams startup and shell output to the
           host terminal — user sees it live.
        2. ``ProcessResult.stdout`` is **empty** because
           ``capture_output=False`` let the subprocess inherit
           the host streams.
        3. The facade receives an empty ``stdout`` field and
           renders ONLY the exit status — no duplicate.

        Timing matters: output must be observable on the host
        terminal **before** the process exits.  Captured-and-
        replayed output would appear only after ``_run()``
        returns, which is too late.
        """
        import threading

        host_terminal = io.StringIO()

        process_output = (
            "[container] Starting services...\n"
            "[container] pi@host:~$ echo hello\n"
            "[container] hello\n"
            "[container] pi@host:~$ exit 1\n"
        )

        output_written = threading.Event()
        executor_released = threading.Event()

        class _StreamingExecutor:
            """Fake executor simulating interactive Docker.

            Writes streamed output to the host terminal, then
            blocks until the test thread has verified that the
            output arrived before completion."""
            def run(self, argv, *, interactive=False):
                if interactive:
                    host_terminal.write(process_output)
                    host_terminal.flush()
                    output_written.set()
                    # Block here — the test thread now asserts
                    # that output is visible while the "process"
                    # is still running.
                    executor_released.wait()
                from docker.launcher import ProcessResult
                return ProcessResult(
                    argv=argv,
                    return_code=1,
                    stdout="",   # not captured — went to terminal
                    stderr="",   # not captured — went to terminal
                )

        runner = _make_fake_process_runner(
            return_code=0, stdout="pi-0001\n",
        )

        result: list[tuple[int, str, str]] = []

        def _run_in_worker() -> None:
            result.append(_run(
                self.m,
                ["run", "--workspace", "/tmp/fake-project",
                 "--tty"],
                _process_runner=runner,
                _container_inspector=self._fake_inspector("pi-0001"),
                _run_executor=_StreamingExecutor(),
                _create_projection=lambda p, **kw: (
                    self._ProjectionHandle()
                ),
                _prompt_user=lambda _: True,
            ))

        worker = threading.Thread(target=_run_in_worker, daemon=True)
        worker.start()

        try:
            # RED — the orchestrator never passes
            # interactive=True, so output_written is never set
            # and this wait times out.
            timed_out = not output_written.wait(timeout=2.0)
            self.assertFalse(
                timed_out,
                "interactive executor must write output to "
                "the host terminal — timed out waiting for "
                "output_written event",
            )

            # ── assertions while the executor is still blocked ──
            self.assertIn(
                "Starting services",
                host_terminal.getvalue(),
                "interactive output must be visible on the "
                "host terminal BEFORE the process exits",
            )
            self.assertIn(
                "exit 1",
                host_terminal.getvalue(),
                "full streaming output must reach the host "
                "terminal",
            )
        finally:
            # Always release the executor — an assertion
            # failure above must not leak the worker thread or
            # skip projection cleanup.
            executor_released.set()

        worker.join(timeout=2.0)
        self.assertFalse(
            worker.is_alive(),
            "worker thread must finish after executor "
            "is released",
        )

        rc, out, err = result[0]

        # Post-completion assertions.
        #   1. Exit code maps correctly.
        self.assertEqual(4, rc,
                         "nonzero interactive exit must map to "
                         "OPERATIONAL (exit code 4)")
        #   2. The facade's rendered stderr must NOT contain
        #      the streamed output again.
        self.assertNotIn(
            "Starting services", err,
            "facade must not re-render output that already "
            "appeared on the host terminal",
        )

    def test_captured_nonzero_output_is_bounded(self) -> None:
        """A non-interactive container that fails may emit
        megabytes of diagnostic output.  Both text and JSON
        failure presentations must bound captured stdout/stderr
        so evidence artifacts and log output do not grow
        without limit.

        ``MAX_RUN_DIAGNOSTIC_BYTES`` is a **byte** budget, not
        a character budget.  Multibyte text (CJK, emoji, …)
        must not slip past the bound.

        The bound applies **independently** to stdout and
        stderr — truncating only one stream is insufficient.
        """
        from docker.constructor_cli import MAX_RUN_DIAGNOSTIC_BYTES as _MAX

        # ── oversized fixtures (multibyte, > 2× _MAX) ──────
        def _oversized_stream(label: str) -> str:
            ascii_line = f"[{label}] OK entry 0x{{idx:08x}}\n"
            mb_line = f"[{label}] 🌐 entrée {{idx:08x}} ✗\n"
            return "".join(
                (ascii_line if i % 2 == 0 else mb_line).format(idx=i)
                for i in range(8192)
            )

        huge_stdout = _oversized_stream("OUT")
        huge_stderr = _oversized_stream("ERR")

        for name, stream in ("stdout", huge_stdout), ("stderr", huge_stderr):
            b = len(stream.encode("utf-8"))
            self.assertGreater(
                b, _MAX * 2,
                f"{name} fixture ({b} UTF-8 bytes) must exceed "
                f"MAX_RUN_DIAGNOSTIC_BYTES ({_MAX})",
            )
            self.assertLess(
                len(stream), b,
                f"{name} multibyte fixture must have "
                "len(str) < len(bytes)",
            )

        executor = self._scripted_executor(
            return_code=1,
            stdout=huge_stdout,
            stderr=huge_stderr,
        )
        runner = _make_fake_process_runner(
            return_code=0, stdout="pi-0001\n",
        )

        # ── text mode ───────────────────────────────────────
        rc_text, out_text, err_text = _run(
            self.m,
            ["run", "--workspace", "/tmp/fake-project",
             "--no-tty", "--no-interactive"],
            _process_runner=runner,
            _container_inspector=self._fake_inspector("pi-0001"),
            _run_executor=executor,
            _create_projection=lambda p, **kw: self._ProjectionHandle(),
            _prompt_user=lambda _: True,
        )
        self.assertEqual(4, rc_text)
        # The diagnostic must contain captured content from
        # both streams…
        self.assertIn("[ERR]", err_text)
        self.assertIn("[OUT]", err_text)
        # …but the combined human-readable message must be
        # bounded.  Since each stream is independently capped
        # at _MAX UTF-8 bytes, the rendered message may carry
        # up to 2× that plus label overhead.
        err_bytes = len(err_text.encode("utf-8"))
        self.assertLess(
            err_bytes, 2 * _MAX + 2048,
            "text-mode diagnostic (combined stdout + stderr) "
            "must be bounded; per-stream budget is "
            f"MAX_RUN_DIAGNOSTIC_BYTES ({_MAX}) each",
        )

        # ── JSON mode ───────────────────────────────────────
        rc_json, out_json, err_json = _run(
            self.m,
            ["--output", "json", "run", "--workspace",
             "/tmp/fake-project", "--no-tty", "--no-interactive"],
            _process_runner=runner,
            _container_inspector=self._fake_inspector("pi-0001"),
            _run_executor=executor,
            _create_projection=lambda p, **kw: self._ProjectionHandle(),
            _prompt_user=lambda _: True,
        )
        self.assertEqual(4, rc_json)
        data = json.loads(out_json)

        # Each captured stream is independently bounded.
        for key in ("stdout", "stderr"):
            val = data["data"].get(key, "")
            val_bytes = len(str(val).encode("utf-8"))
            self.assertLessEqual(
                val_bytes, _MAX,
                f"JSON captured {key} field must not exceed "
                f"MAX_RUN_DIAGNOSTIC_BYTES ({_MAX}); "
                f"got {val_bytes} UTF-8 bytes",
            )
            # Truncation metadata.
            truncated_key = f"{key}_truncated"
            self.assertTrue(
                data["data"].get(truncated_key),
                f"JSON response must carry '{truncated_key}': true "
                f"when captured {key} exceeds "
                "MAX_RUN_DIAGNOSTIC_BYTES",
            )
            # In-band sentinel inside the byte budget.
            self.assertTrue(
                str(val).rstrip().endswith("[truncated]"),
                f"truncated {key} value must end with "
                "'[truncated]' sentinel",
            )

        # Marker fits in the budget.
        marker_bytes = len("[truncated]".encode("utf-8"))
        self.assertLessEqual(
            marker_bytes, _MAX,
            f"'[truncated]' marker ({marker_bytes} UTF-8 bytes) "
            f"must fit within MAX_RUN_DIAGNOSTIC_BYTES ({_MAX})",
        )

# ════════════════════════════════════════════════════════════════════════
# 14.1  Mismatched image expectations (verify build)
# ════════════════════════════════════════════════════════════════════════

class TestVerifyBuildMismatchDiagnostics(unittest.TestCase):
    """``verify --scope build`` with mismatched versions produces
    diagnostics sufficient for investigation without Docker."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = _load_mod()

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._td = Path(self._tmpdir.name)
        self._cache_root = self._td / "constructor-cache"
        self._cache_root.mkdir(mode=0o700)
        from docker.versioning.project_state import resolve_project_state
        self._proj_dir = resolve_project_state(self._td, cache_root=self._cache_root).generated_root
        proj = self._proj_dir / "docker-constructor.build.effective.toml"
        proj.write_text(
            '[python]\nversion = "3.12.0"\n'
            '[node]\nimage = "node:20.11.0-bookworm-slim"\n'
            '[rust]\nversion = "1.77.0"\n'
            'components = ["cargo", "rustfmt", "clippy"]\n'
            '[uv]\nversion = "0.5.0"\n'
            '[ty]\nversion = "v0.9.0"\n'
            '[rtk]\nversion = "0.31.0"\n'
            '[fd]\nversion = "9.0.0"\n'
            '[pi]\nversion = "v1.4.236"\n'
            '[openspec]\nversion = "v0.15.0"\n'
            '[oh-my-zsh]\nrevision = "abc1234"\n'
        )
        self._inv_path = self._td / "docker-constructor.toml"
        # Reviewed inventory is validated by the shared transaction, so use
        # the repository's current schema instead of a legacy stub.
        import shutil
        shutil.copy(
            Path(__file__).resolve().parents[1] / "docker-constructor.toml",
            self._inv_path,
        )
        self._inv_path.with_name("docker-constructor.local.toml").write_text(
            f'[cache]\ndir = "{self._cache_root}"\n'
        )

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_every_observation_key_reported_even_on_total_mismatch(self) -> None:
        """When ALL observations mismatch, every contract key appears in
        the output — no silent omissions."""
        runner, calls = _make_recording_runner(
            return_code=0,
            stdout="99.99.99",
        )
        rc, out, err = _run(
            self.m,
            ["--project-directory", str(Path(self._inv_path).parent),
             "verify", "--scope", "build", "--image", "mis-img:v9"],
            _process_runner=runner,
            _prompt_user=lambda _: True,
        )
        self.assertEqual(4, rc)
        self.assertGreater(len(calls), 5, "must check multiple tools")
        plain = _strip_ansi(err)
        # The observations use keys like "node.version", "rust.version", etc.
        key_names = ["python", "rust", "node", "uv", "ty", "rtk", "fd",
                     "pi", "openspec"]
        found = sum(1 for k in key_names if k in plain.lower())
        self.assertGreaterEqual(found, 5,
                                "must name most contract keys in output")

    def test_json_output_includes_expected_and_observed_per_observation(self) -> None:
        """JSON output must include both expected and observed values for
        every observation, enabling offline comparison.

        With ``--collect-evidence``, the evidence bundle captures the
        ``docker inspect`` metadata alongside observations."""
        runner, _ = _make_recording_runner(return_code=0, stdout="")
        evidence_dir = str(self._td / "evidence")
        rc, out, err = _run(
            self.m,
            ["--project-directory", str(Path(self._inv_path).parent),
             "--output", "json",
             "verify", "--scope", "build", "--image", "empty-img:v1",
             "--collect-evidence", "--output-dir", evidence_dir],
            _process_runner=runner,
            _prompt_user=lambda _: True,
        )
        data = json.loads(out)
        obs = data["data"]["verification"]["build"]["observations"]
        self.assertGreater(len(obs), 5)
        for o in obs:
            self.assertIn("key", o)
            self.assertIn("ok", o)
            self.assertIn("expected", o)
            self.assertIn("observed", o)
        # At least one observation must be present and detailed.
        # Key names use dotted paths like "python.version".
        python_obs = [o for o in obs if "python" in o["key"]]
        self.assertTrue(python_obs,
                        "python must be in observations")
        self.assertFalse(python_obs[0]["ok"], "empty observed must fail")
        self.assertEqual("", python_obs[0]["observed"])

        # Evidence: docker inspect + failed version-check commands.
        ev_meta = data["data"]["verification"]["collect_evidence"]
        self.assertTrue(ev_meta["all_ok"])
        self.assertGreaterEqual(ev_meta["command_count"], 5)
        evidence = _assert_evidence_bundle_complete(
            self, evidence_dir,
            min_commands=5,
            expect_command_substrs=[
                "docker inspect empty-img:v1",
                "docker run --rm empty-img:v1 python3 --version",
                "docker run --rm empty-img:v1 node --version",
                "docker run --rm empty-img:v1 rustc --version",
            ],
        )
        # Each command has exit code 0 (runner succeeds).
        self.assertIn("exit=0", evidence["index_text"],
                       "evidence must record exit code")

    def test_mismatch_diagnostic_shows_what_was_run(self) -> None:
        """The diagnostic must include enough context to identify the
        failing verification run (image tag in the invocation args)."""
        runner, calls = _make_recording_runner(return_code=0, stdout="")
        rc, out, err = _run(
            self.m,
            ["--project-directory", str(Path(self._inv_path).parent),
             "verify", "--scope", "build", "--image", "specific-img:v2.5"],
            _process_runner=runner,
            _prompt_user=lambda _: True,
        )
        # The diagnostic includes the tool name and observation key.
        # Prove the output contains structured observation lines.
        plain = _strip_ansi(err)
        self.assertIn("✗", plain, "mismatches must use ✗ markers")
        # Every observation is listed
        self.assertIn("python", plain.lower(),
                       "python observation must be present")
        self.assertIn("rust", plain.lower(),
                       "rust observation must be present")

# ════════════════════════════════════════════════════════════════════════
# 14.1  Bad runtime mounts / exposed paths / extension failures
# ════════════════════════════════════════════════════════════════════════

class TestVerifyRuntimeMountDiagnostics(unittest.TestCase):
    """``verify --scope runtime`` exercises the real verification
    pipeline with an injected scripted Docker runner.  The tests pass
    all checks and then break one to confirm the evidence is precise."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = _load_mod()

    def setUp(self) -> None:
        td, inv_path, proj_path, proj_hash = _make_runtime_fixture()
        self._inv_path = inv_path
        self._proj_path = proj_path
        self._proj_hash = proj_hash
        self._cleanup = td  # str path for tempfile.mkdtemp

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._cleanup, ignore_errors=True)

    @property
    def _happy_base(self) -> _ScriptedProcessRunner:
        """Return a runner where every check passes."""
        return _build_happy_runtime_runner(
            container="test-ctr",
            proj_hash=self._proj_hash,
            workspace_paths=("/tmp/proj1",),
            extensions={"@llblab/pi-codex-usage":
                        ("@llblab/pi-codex-usage", "0.9.1")},
        )

    @property
    def _base_argv(self) -> list[str]:
        """CLI arguments shared by all runtime verification tests."""
        return [
            "--project-directory", str(Path(self._inv_path).parent),
            "verify", "--scope", "runtime",
            "--container", "test-ctr",
            "--runtime-projection", self._proj_path,
            "--workspace", "/tmp/proj1",
        ]

    @property
    def _evidence_dir(self) -> str:
        """Output directory for evidence bundles in this test."""
        return str(Path(self._cleanup) / "evidence")

    @property
    def _evidence_argv(self) -> list[str]:
        """Base args with ``--collect-evidence`` appended."""
        return self._base_argv + [
            "--collect-evidence",
            "--output-dir", self._evidence_dir,
        ]

    # ── Helper: shared evidence assertion ────────────────────────

    def _assert_evidence_written(self, **kw: Any) -> dict:
        """Run ``_assert_evidence_bundle_complete`` on the current
        test's evidence directory, forwarding *kw*."""
        return _assert_evidence_bundle_complete(
            self, self._evidence_dir, **kw,
        )

    # ── read-only mount absent ───────────────────────────────────

    def test_readonly_mount_absent_reported_in_proc_mounts_detail(self) -> None:
        """When ``grep`` of /proc/mounts fails, the ``projection.readonly``
        check records "not found in /proc/mounts" in its detail string."""
        runner = self._happy_base.when(
            "docker-constructor.runtime.toml /proc/mounts",
            rc=1, stdout="", stderr="grep: no match",
        )
        rc, out, err = _run(
            self.m, self._base_argv,
            _process_runner=runner, _prompt_user=lambda _: True,
        )
        self.assertEqual(4, rc, "must exit OPERATIONAL on check failure")
        plain = _strip_ansi(err)
        self.assertIn("projection.readonly", plain.lower(),
                       "must name failing check")
        self.assertIn("not found in /proc/mounts", plain,
                       "must include the check detail")

    def test_readonly_mount_absent_json_includes_check_key_and_detail(self) -> None:
        """JSON mode surfaces the raw ``projection.readonly`` check with
        ``ok: false`` and the detail string.  With ``--collect-evidence``,
        the evidence bundle captures the original ``grep`` command that
        detected the missing mount."""
        runner = self._happy_base.when(
            "docker-constructor.runtime.toml /proc/mounts",
            rc=1, stdout="", stderr="grep: no match",
        )
        rc, out, err = _run(
            self.m, ["--output", "json"] + self._evidence_argv,
            _process_runner=runner, _prompt_user=lambda _: True,
        )
        data = json.loads(out)
        rt = data["data"]["verification"]["runtime"]
        self.assertFalse(rt["all_ok"])
        readonly = [c for c in rt["checks"] if c["key"] == "projection.readonly"]
        self.assertEqual(1, len(readonly))
        self.assertFalse(readonly[0]["ok"])
        self.assertIn("not found", readonly[0]["detail"])

        # The check captures the primary diagnostic command result.
        self.assertEqual(
            readonly[0]["command"],
            ["docker", "exec", "test-ctr", "grep",
             "docker-constructor.runtime.toml", "/proc/mounts"],
        )
        self.assertEqual(readonly[0]["exit_code"], 1)
        self.assertIn("grep: no match", readonly[0]["raw_stderr"] or "")

        # Evidence bundle: one record for every check + docker inspect.
        ev_meta = data["data"]["verification"]["collect_evidence"]
        self.assertTrue(ev_meta["all_ok"])
        self.assertGreaterEqual(ev_meta["command_count"], 10)
        evidence = self._assert_evidence_written(
            min_commands=10,
            expect_command_substrs=[
                "docker inspect",
                "grep docker-constructor.runtime.toml /proc/mounts",
            ],
        )
        # The grep stderr is captured in the bundle.
        stderr_files = evidence["stderr_files"]
        self.assertGreater(len(stderr_files), 0,
                           "grep stderr must be captured")
        stderr_content = stderr_files[0].read_text()
        self.assertIn("grep: no match", stderr_content)

    # ── forbidden path present ───────────────────────────────────

    def test_forbidden_build_projection_present_reported_in_detail(self) -> None:
        """When ``test -f`` succeeds for a forbidden path, the
        ``forbidden.paths`` check reports ``forbidden path present``
        with the exact path."""
        runner = self._happy_base.when(
            "test -f /run/pi-cli/docker-constructor.build.effective.toml",
            rc=0,  # file EXISTS → violation
        )
        rc, out, err = _run(
            self.m, self._base_argv,
            _process_runner=runner, _prompt_user=lambda _: True,
        )
        self.assertEqual(4, rc)
        plain = _strip_ansi(err)
        self.assertIn("forbidden.paths", plain.lower())
        self.assertIn("forbidden path present: "
                      "/run/pi-cli/docker-constructor.build.effective.toml",
                      plain)

    def test_forbidden_inventory_present_reported_in_detail(self) -> None:
        """When the reviewed inventory (``docker-constructor.toml``) is
        present inside the container, the check names it."""
        runner = self._happy_base.when(
            "test -f /run/pi-cli/docker-constructor.toml",
            rc=0,  # file EXISTS → violation
        )
        rc, out, err = _run(
            self.m, self._base_argv,
            _process_runner=runner, _prompt_user=lambda _: True,
        )
        self.assertEqual(4, rc)
        plain = _strip_ansi(err)
        self.assertIn("forbidden path present: "
                      "/run/pi-cli/docker-constructor.toml",
                      plain)

    def test_forbidden_path_json_check_is_false(self) -> None:
        """JSON output includes both forbidden check entries with their
        ok status, AND the evidence bundle documents the violation with
        the original ``test -f`` command results (no re-execution)."""
        runner = self._happy_base.when(
            "test -f /run/pi-cli/docker-constructor.toml",
            rc=0,
        )
        rc, out, err = _run(
            self.m, ["--output", "json"] + self._evidence_argv,
            _process_runner=runner, _prompt_user=lambda _: True,
        )
        data = json.loads(out)
        rt = data["data"]["verification"]["runtime"]
        forbidden = [c for c in rt["checks"]
                     if c["key"] == "forbidden.paths"]
        self.assertGreaterEqual(len(forbidden), 2)
        ok_statuses = {c["detail"]: c["ok"] for c in forbidden}
        present_key = ("forbidden path present: "
                       "/run/pi-cli/docker-constructor.toml")
        self.assertIn(present_key, ok_statuses)
        self.assertFalse(ok_statuses[present_key])

        # The failing check captures the original command result.
        failed = [c for c in forbidden if not c["ok"]]
        self.assertGreaterEqual(len(failed), 1)
        self.assertIn("test -f /run/pi-cli/docker-constructor.toml",
                       " ".join(failed[0]["command"] or []))
        self.assertEqual(failed[0]["exit_code"], 0)

        # Evidence: one record per check + docker inspect.
        ev_meta = data["data"]["verification"]["collect_evidence"]
        self.assertIn("output_dir", ev_meta)
        self.assertGreaterEqual(ev_meta["command_count"], 10)
        evidence = self._assert_evidence_written(
            min_commands=10,
            expect_command_substrs=[
                "docker inspect",
                "test -f /run/pi-cli/docker-constructor.toml",
                "test -f /run/pi-cli/docker-constructor.build.effective.toml",
            ],
        )

    # ── extension check failure ──────────────────────────────────

    def test_extension_version_mismatch_reports_package_and_versions(self) -> None:
        """When ``cat package.json`` returns a version different from the
        projection, the check detail lists the package name, expected,
        and actual version."""
        runner = self._happy_base.when(
            "cat /home/dev/.pi/agent/npm/node_modules/@llblab/pi-codex-usage/"
            "package.json",
            rc=0, stdout=json.dumps({"version": "0.8.0"}),  # wrong
        )
        rc, out, err = _run(
            self.m, self._base_argv,
            _process_runner=runner, _prompt_user=lambda _: True,
        )
        self.assertEqual(4, rc)
        plain = _strip_ansi(err)
        self.assertIn("extensions.results", plain.lower())
        self.assertIn("@llblab/pi-codex-usage", plain,
                       "must name the mismatched package")
        self.assertIn("v0.9.1", plain,
                       "must name expected version")
        self.assertIn("v0.8.0", plain,
                       "must name actual version")

    def test_extension_package_json_missing_reported(self) -> None:
        """When ``cat package.json`` fails with non-zero exit, the check
        reports ``package.json not found``."""
        runner = self._happy_base.when(
            "cat /home/dev/.pi/agent/npm/node_modules/@llblab/pi-codex-usage/"
            "package.json",
            rc=1, stdout="", stderr="cat: No such file",
        )
        rc, out, err = _run(
            self.m, self._base_argv,
            _process_runner=runner, _prompt_user=lambda _: True,
        )
        self.assertEqual(4, rc)
        plain = _strip_ansi(err)
        self.assertIn("package.json not found", plain)

    def test_extension_failure_still_runs_other_checks(self) -> None:
        """Even when extension check fails, all other checks (readonly,
        ownership, forbidden) still run and appear in the output."""
        runner = self._happy_base.when(
            "cat /home/dev/.pi/agent/npm/node_modules/@llblab/pi-codex-usage/"
            "package.json",
            rc=0, stdout=json.dumps({"version": "0.8.0"}),
        )
        rc, out, err = _run(
            self.m, ["--output", "json"] + self._base_argv,
            _process_runner=runner, _prompt_user=lambda _: True,
        )
        data = json.loads(out)
        rt = data["data"]["verification"]["runtime"]
        check_keys = {c["key"] for c in rt["checks"]}
        self.assertIn("extensions.results", check_keys)
        self.assertIn("projection.readonly", check_keys,
                       "readonly check must still run")
        self.assertIn("ownership.dev", check_keys,
                       "ownership check must still run")
        self.assertIn("forbidden.paths", check_keys,
                       "forbidden check must still run")

    # ── multiple failures ────────────────────────────────────────

    def test_multiple_failures_all_visible_in_text_output(self) -> None:
        """When several checks fail, every failure is listed in the
        human-readable stderr output."""
        runner = (
            self._happy_base
            # Forbidden path
            .when("test -f /run/pi-cli/docker-constructor.toml", rc=0)
            # Extension mismatch
            .when("cat /home/dev/.pi/agent/npm/node_modules/@llblab/pi-codex-usage/"
                  "package.json",
                  rc=0, stdout=json.dumps({"version": "0.7.0"}))
            # Bad ownership
            .when("stat -c %U:%G /home/dev/.pi", rc=0, stdout="root:root")
        )
        rc, out, err = _run(
            self.m, self._base_argv,
            _process_runner=runner, _prompt_user=lambda _: True,
        )
        self.assertEqual(4, rc)
        plain = _strip_ansi(err)
        failures = [line for line in plain.split("\n") if "✗" in line]
        self.assertGreaterEqual(len(failures), 3,
                                "must list at least 3 failures")
        # Each failure names the relevant detail
        self.assertTrue(
            any("forbidden" in f.lower() for f in failures),
            "must include forbidden path failure")
        self.assertTrue(
            any("pi-codex-usage" in f for f in failures),
            "must include extension failure")
        self.assertTrue(
            any("dev:dev" in f for f in failures),
            "must include ownership failure")

    def test_multiple_failures_json_all_have_ok_false(self) -> None:
        """JSON output for multiple failures includes every check with
        the correct ``ok`` flag.  Each check carries its original
        ``command``, ``exit_code``, ``raw_stdout``, and ``raw_stderr``
        — the evidence bundle is built from those captured results,
        not re-executed."""
        runner = (
            self._happy_base
            .when("test -f /run/pi-cli/docker-constructor.toml", rc=0)
            .when("cat /home/dev/.pi/agent/npm/node_modules/@llblab/pi-codex-usage/"
                  "package.json",
                  rc=0, stdout=json.dumps({"version": "0.7.0"}))
            .when("stat -c %U:%G /home/dev/.pi", rc=0, stdout="root:root")
        )
        rc, out, err = _run(
            self.m, ["--output", "json"] + self._evidence_argv,
            _process_runner=runner, _prompt_user=lambda _: True,
        )
        data = json.loads(out)
        rt = data["data"]["verification"]["runtime"]
        self.assertFalse(rt["all_ok"])
        # Collect per-key statuses; keys may appear multiple times.
        checks_by_key: dict[str, list[bool]] = {}
        for c in rt["checks"]:
            checks_by_key.setdefault(c["key"], []).append(c["ok"])
        # At least one forbidden.paths entry must fail.
        self.assertIn("forbidden.paths", checks_by_key)
        self.assertIn(
            False, checks_by_key["forbidden.paths"],
            "at least one forbidden.paths entry must fail")
        self.assertIn(
            False, checks_by_key.get("extensions.results", [True]),
            "extension check must fail")
        self.assertIn(
            False, checks_by_key.get("ownership.dev", [True]),
            "ownership check must fail")

        # Every check (passing or failing) carries its original result.
        for c in rt["checks"]:
            if c.get("command"):
                self.assertIsInstance(c["exit_code"], int,
                                       f"{c['key']} must have exit_code")

        # Evidence: one record per check + docker inspect.
        evidence = self._assert_evidence_written(
            min_commands=10,
            expect_command_substrs=[
                "docker inspect",
                "test -f /run/pi-cli/docker-constructor.toml",
                "test -f /run/pi-cli/docker-constructor.build.effective.toml",
                "stat -c %U:%G /home/dev/.pi",
                "stat -c %U:%G /tmp/proj1",
            ],
        )
        # The stat commands produce output → checksums must be present.
        index = evidence["index_text"]
        self.assertIn("sha256=", index,
                       "evidence with output must include checksums")

    # ── evidence: identity check uses real hash ──────────────────

    def test_identity_check_compares_real_projection_hash(self) -> None:
        """The ``projection.identity`` check computes the SHA-256 from
        the host file.  When the container returns a different hash,
        the mismatch is reported with both hashes."""
        bogus = "deadbeef" * 8  # 64 hex chars
        runner = self._happy_base.when(
            "sha256sum",
            rc=0, stdout=f"{bogus}  "
                         "/run/pi-cli/docker-constructor.runtime.toml",
        )
        rc, out, err = _run(
            self.m, self._base_argv,
            _process_runner=runner, _prompt_user=lambda _: True,
        )
        self.assertEqual(4, rc)
        plain = _strip_ansi(err)
        self.assertIn("hash mismatch", plain,
                       "must report hash mismatch")
        self.assertIn(bogus[:12], plain,
                       "must include container hash prefix")
        self.assertIn(self._proj_hash[:12], plain,
                       "must include host hash prefix")

# ════════════════════════════════════════════════════════════════════════
# 14.1  Project errors
# ════════════════════════════════════════════════════════════════════════

class TestProjectErrorDiagnostics(unittest.TestCase):
    """Project-level misconfigurations must leave clear diagnostics."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = _load_mod()

    def test_no_workspace_through_facade_config_exit_and_suggestion(self) -> None:
        """A facade-level ``NoWorkspaceError`` produces CONFIG exit
        code and suggests ``--workspace`` or ``--tui``."""
        fake = _make_fake_dispatcher(
            self.m, exit_kind="config",
            message=(
                "no primary workspace selected; "
                "pass --workspace or use --tui"
            ),
        )
        rc, out, err = _run(self.m, ["run", "--dry-run"], dispatcher=fake)
        self.assertEqual(3, rc)
        text = _strip_ansi(err)
        self.assertIn("no primary workspace", text.lower())
        self.assertIn("--workspace", text)
        self.assertIn("--tui", text)

    def test_duplicate_workspace_paths_reported_in_error(self) -> None:
        """Duplicate workspace paths are rejected before Docker is invoked,
        with the duplicated path in the error."""
        rc, out, err = _run(
            self.m,
            ["run", "--workspace", "/tmp/proj1",
             "--extra-workspace", "/tmp/proj1", "--dry-run"],
        )
        self.assertEqual(3, rc)
        self.assertIn(
            "Duplicate workspace path after normalisation: '/tmp/proj1'",
            _strip_ansi(err),
        )

# ════════════════════════════════════════════════════════════════════════
# 14.2  Cross-cutting evidence assertions
# ════════════════════════════════════════════════════════════════════════

class TestEvidenceCompleteness(unittest.TestCase):
    """Every simulated failure provides enough evidence for diagnosis
    without re-running Docker."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = _load_mod()

    def test_failure_exit_code_distinguishes_category(self) -> None:
        """Exit codes must distinguish CONFIG (3), CLI (2), OPERATIONAL
        (4), and POLICY (1) so the caller knows who is at fault."""
        categories = [
            ("config", 3),
            ("cli", 2),
            ("operational", 4),
            ("policy", 1),
        ]
        for kind, expected_rc in categories:
            fake = _make_fake_dispatcher(
                self.m, exit_kind=kind,
                message=f"simulated {kind} failure",
            )
            rc, _, _ = _run(
                self.m, ["build", "--yes"], dispatcher=fake,
            )
            self.assertEqual(
                expected_rc, rc,
                f"{kind} must map to exit code {expected_rc}, got {rc}",
            )

    def test_json_mode_always_emits_command_and_status(self) -> None:
        """JSON output on failure must always include ``command`` and
        ``status`` so consumers can route without parsing text.

        Global options (``--output``) must precede the subcommand."""
        for command in ("build",):
            fake = _make_fake_dispatcher(
                self.m, exit_kind="operational",
                message=f"simulated {command} failure",
            )
            rc, out, err = _run(
                self.m,
                ["--output", "json", command, "--yes"],
                dispatcher=fake,
                _prompt_user=lambda _: True,
            )
            self.assertNotEqual(0, rc)
            data = json.loads(out)
            self.assertIn("command", data, f"{command}: missing 'command'")
            self.assertEqual(command, data["command"])
            self.assertIn("status", data, f"{command}: missing 'status'")

        # Verify does not accept --yes; work with confirmation bypass
        fake2 = _make_fake_dispatcher(
            self.m, exit_kind="operational",
            message="simulated verify failure",
        )
        rc2, out2, _ = _run(
            self.m,
            ["--output", "json", "verify", "--scope", "build"],
            dispatcher=fake2,
            _prompt_user=lambda _: True,
        )
        self.assertNotEqual(0, rc2)
        data2 = json.loads(out2)
        self.assertEqual("verify", data2["command"])

    def test_stderr_message_identifies_the_failing_condition(self) -> None:
        """The human-readable stderr message must contain the key detail
        that identifies the failure."""
        fake = _make_fake_dispatcher(
            self.m, exit_kind="operational",
            message="container pi-cli-pi-0007 failed to start",
        )
        rc, out, err = _run(self.m, ["run", "--dry-run"], dispatcher=fake)
        plain = _strip_ansi(err)
        self.assertIn("pi-cli-pi-0007", plain)
        self.assertIn("failed to start", plain)

    def test_non_json_failure_includes_message_on_stderr_not_stdout(self) -> None:
        """In non-JSON mode, failure messages go to stderr; stdout is empty."""
        fake = _make_fake_dispatcher(
            self.m, exit_kind="operational",
            message="image tag mismatch: "
                    "expected pi-cli-pi:latest, got other:v1",
        )
        rc, out, err = _run(
            self.m, ["verify", "--scope", "all"], dispatcher=fake,
        )
        self.assertEqual("", out)
        self.assertIn("image tag mismatch", _strip_ansi(err))

    # ── evidence bundle completeness ─────────────────────────────

    def test_evidence_bundle_captures_raw_command_output_with_metadata(self) -> None:
        """When ``--collect-evidence`` is used during runtime verification,
        the bundle on disk includes:

        * ``index.txt`` with per-command exit code, duration, and argv
        * bounded ``*-stdout.txt`` files with raw output
        * ``*-stderr.txt`` when stderr is non-empty
        """
        td, inv_path, proj_path, proj_hash = _make_runtime_fixture()
        try:
            runner = _build_happy_runtime_runner(
                container="ev-ctr",
                proj_hash=proj_hash,
                workspace_paths=("/tmp/p1",),
                extensions={"@llblab/p": ("@llblab/p", "0.9.1")},
            )
            # Cause an extension failure: return wrong version with
            # non-empty stderr.
            runner = runner.when(
                "cat /home/dev/.pi/agent/npm/node_modules/@llblab/p/"
                "package.json",
                rc=0,
                stdout=json.dumps({"version": "0.8.0"}),
                stderr="warning: deprecated package\n",
            )

            evidence_dir = str(Path(td) / "evidence")
            rc, out, err = _run(
                self.m,
                ["--output", "json",
                 "--project-directory", str(Path(inv_path).parent),
                 "verify", "--scope", "runtime",
                 "--container", "ev-ctr",
                 "--runtime-projection", proj_path,
                 "--workspace", "/tmp/p1",
                 "--collect-evidence",
                 "--output-dir", evidence_dir],
                _process_runner=runner,
                _prompt_user=lambda _: True,
            )
            self.assertEqual(4, rc)
            data = json.loads(out)
            ev = data["data"]["verification"]["collect_evidence"]
            self.assertGreaterEqual(ev["command_count"], 10)

            # On-disk evidence: every check record preserved.
            evidence = _assert_evidence_bundle_complete(
                self, evidence_dir,
                min_commands=10,
                expect_command_substrs=[
                    "docker inspect",
                    "cat /home/dev/.pi/agent/npm/node_modules/@llblab/pi-codex-usage/package.json",
                ],
            )
            index = evidence["index_text"]
            # Structured metadata per command.
            self.assertIn("exit=", index)
            self.assertIn("dur=", index)
            self.assertIn("argv=", index)
            # Index identifies the image.
            self.assertIn("Evidence collection for image:", index)
            # Every output file is bounded.
            for sf in evidence["stdout_files"]:
                self.assertLess(sf.stat().st_size, 2 * 1024 * 1024)
            for sf in evidence["stderr_files"]:
                self.assertLess(sf.stat().st_size, 2 * 1024 * 1024)
        finally:
            import shutil
            shutil.rmtree(td, ignore_errors=True)

# ════════════════════════════════════════════════════════════════════════
# 14.2-e  Truncation and redaction of captured evidence output
# ════════════════════════════════════════════════════════════════════════

class TestStaticEvidenceNormalization(unittest.TestCase):
    """Pre-captured verification output is normalised through the same
    redaction + truncation + checksum pipeline as live execution."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = _load_mod()

    def test_oversized_stdout_is_truncated_with_metadata(self) -> None:
        """Captured stdout exceeding *max_output_bytes* is truncated
        and the EvidenceCommand records ``stdout_truncated=True`` plus
        ``stdout_original_bytes``."""
        from docker.versioning.evidence import (
            normalize_static_record as _norm,
        )
        td = tempfile.mkdtemp(prefix="acc-norm-")
        out_dir = Path(td)
        big = "x" * 200  # 200 bytes; use a tiny cap
        rec = _norm(
            argv=("docker", "exec", "ctr", "cat", "/big"),
            return_code=0,
            stdout_raw=big,
            timestamp_epoch=0.0,
            output_dir=out_dir,
            index=0,
            max_output_bytes=50,
        )
        self.assertTrue(rec.stdout_truncated)
        self.assertEqual(rec.stdout_original_bytes, 200)
        self.assertIsNotNone(rec.stdout_file)
        truncated_content = (out_dir / rec.stdout_file).read_text()
        self.assertLessEqual(len(truncated_content.encode()), 50)
        self.assertIsNotNone(rec.stdout_sha256)
        # Checksum matches truncated content.
        actual_hash = hashlib.sha256(
            truncated_content.encode()).hexdigest()
        self.assertEqual(rec.stdout_sha256, actual_hash)
        shutil.rmtree(td, ignore_errors=True)

    def test_normal_sized_stdout_not_truncated(self) -> None:
        """Captured stdout within the cap is not flagged as
        truncated."""
        from docker.versioning.evidence import (
            normalize_static_record as _norm,
        )
        td = tempfile.mkdtemp(prefix="acc-norm-")
        out_dir = Path(td)
        rec = _norm(
            argv=("docker", "exec", "ctr", "cat", "/small"),
            return_code=0,
            stdout_raw="hello",
            timestamp_epoch=0.0,
            output_dir=out_dir,
            index=0,
            max_output_bytes=50,
        )
        self.assertFalse(rec.stdout_truncated)
        self.assertIsNone(rec.stdout_original_bytes)
        shutil.rmtree(td, ignore_errors=True)

    def test_stderr_secrets_are_redacted(self) -> None:
        """Captured stderr containing Bearer tokens is redacted
        before writing to disk."""
        from docker.versioning.evidence import (
            normalize_static_record as _norm,
        )
        td = tempfile.mkdtemp(prefix="acc-norm-")
        out_dir = Path(td)
        rec = _norm(
            argv=("docker", "exec", "ctr", "curl"),
            return_code=1,
            stderr_raw="curl: Authorization: Bearer sk-abc123secret\n",
            timestamp_epoch=0.0,
            output_dir=out_dir,
            index=0,
        )
        self.assertIsNotNone(rec.stderr_file)
        stderr_text = (out_dir / rec.stderr_file).read_text()
        self.assertIn("Bearer REDACTED", stderr_text)
        self.assertNotIn("sk-abc123secret", stderr_text)
        # Checksum must match the redacted (written) content.
        actual_hash = hashlib.sha256(stderr_text.encode()).hexdigest()
        self.assertEqual(rec.stderr_sha256, actual_hash)
        shutil.rmtree(td, ignore_errors=True)

    def test_stdout_secrets_are_redacted(self) -> None:
        """Captured stdout containing Bearer tokens is redacted
        through the same path as stderr."""
        from docker.versioning.evidence import (
            normalize_static_record as _norm,
        )
        td = tempfile.mkdtemp(prefix="acc-norm-")
        out_dir = Path(td)
        rec = _norm(
            argv=("docker", "exec", "ctr", "curl", "-s"),
            return_code=0,
            stdout_raw='{"auth": "Bearer sk-abc123secret", "ok": true}',
            timestamp_epoch=0.0,
            output_dir=out_dir,
            index=0,
        )
        self.assertIsNotNone(rec.stdout_file)
        stdout_text = (out_dir / rec.stdout_file).read_text()
        self.assertIn("Bearer REDACTED", stdout_text)
        self.assertNotIn("sk-abc123secret", stdout_text)
        actual_hash = hashlib.sha256(stdout_text.encode()).hexdigest()
        self.assertEqual(rec.stdout_sha256, actual_hash)
        shutil.rmtree(td, ignore_errors=True)

    def test_empty_output_produces_no_files(self) -> None:
        """When both stdout and stderr are empty, no files are
        written but checksums are recorded (sha256 of empty string)."""
        from docker.versioning.evidence import (
            normalize_static_record as _norm,
        )
        td = tempfile.mkdtemp(prefix="acc-norm-")
        out_dir = Path(td)
        rec = _norm(
            argv=("docker", "exec", "ctr", "true"),
            return_code=0,
            timestamp_epoch=0.0,
            output_dir=out_dir,
            index=0,
        )
        self.assertIsNone(rec.stdout_file)
        self.assertIsNone(rec.stderr_file)
        self.assertIsNotNone(rec.stdout_sha256)
        self.assertIsNotNone(rec.stderr_sha256)
        self.assertFalse(rec.stdout_truncated)
        shutil.rmtree(td, ignore_errors=True)

    def test_argv_env_secrets_are_redacted(self) -> None:
        """Command argv containing ``-e TOKEN=secret`` is redacted
        in the recorded EvidenceCommand."""
        from docker.versioning.evidence import (
            normalize_static_record as _norm,
        )
        td = tempfile.mkdtemp(prefix="acc-norm-")
        out_dir = Path(td)
        rec = _norm(
            argv=("docker", "run", "-e", "GITHUB_TOKEN=ghp_abc123secret",
                   "img"),
            return_code=0,
            timestamp_epoch=0.0,
            output_dir=out_dir,
            index=0,
        )
        joined = " ".join(rec.argv)
        self.assertIn("REDACTED", joined)
        self.assertNotIn("ghp_abc123secret", joined)
        shutil.rmtree(td, ignore_errors=True)

    def test_oversized_stderr_is_truncated_with_metadata(self) -> None:
        """Both stdout and stderr truncation metadata is set when
        exceeding the cap."""
        from docker.versioning.evidence import (
            normalize_static_record as _norm,
        )
        td = tempfile.mkdtemp(prefix="acc-norm-")
        out_dir = Path(td)
        rec = _norm(
            argv=("docker", "exec", "ctr", "find", "/"),
            return_code=1,
            stdout_raw="x" * 300,
            stderr_raw="err" * 200,
            timestamp_epoch=0.0,
            output_dir=out_dir,
            index=0,
            max_output_bytes=100,
        )
        self.assertTrue(rec.stdout_truncated)
        self.assertEqual(rec.stdout_original_bytes, 300)
        self.assertTrue(rec.stderr_truncated)
        self.assertEqual(rec.stderr_original_bytes, 600)
        # Both files exist and are bounded.
        self.assertIsNotNone(rec.stdout_file)
        self.assertIsNotNone(rec.stderr_file)
        self.assertLessEqual(
            len((out_dir / rec.stdout_file).read_text().encode()), 100)
        self.assertLessEqual(
            len((out_dir / rec.stderr_file).read_text().encode()), 100)
        shutil.rmtree(td, ignore_errors=True)

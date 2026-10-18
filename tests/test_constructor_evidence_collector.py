"""RED tests for the evidence collector (Stage 12, task 12.3).

Verifies that the collector records command arguments, exit codes,
timestamps, bounded stdout/stderr, image inspection, checksums,
redaction, failure handling, and a human-readable index — all through
injected fake process runners and clocks without Docker.
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from typing import Sequence

from docker.versioning.evidence import (
    EvidenceBundle,
    EvidenceCommand,
    EvidenceNote,
    ProcessResult,
    collect_evidence,
)

# ═══════════════════════════════════════════════════════════════════════
# Fake boundaries
# ═══════════════════════════════════════════════════════════════════════


class _FakeClock:
    """Deterministic clock that returns pre-programmed epoch values."""

    def __init__(self, times: list[float]):
        self._times = times
        self._idx = 0
        self._calls = 0

    def now(self) -> float:
        self._calls += 1
        if self._idx >= len(self._times):
            return self._times[-1] if self._times else 0.0
        t = self._times[self._idx]
        self._idx += 1
        return t


class _RecordingRunner:
    """Records every argv; returns each then-expected (rc, stdout, stderr)."""

    def __init__(self, *, responses: list[tuple[int, str, str]] | None = None):
        self.responses = responses or []
        self.calls: list[tuple[str, ...]] = []
        self._idx = 0

    def run(self, argv: Sequence[str]) -> ProcessResult:
        self.calls.append(tuple(argv))
        if self._idx >= len(self.responses):
            return ProcessResult(
                argv=tuple(argv),
                return_code=0,
                stdout="",
                stderr="",
            )
        rc, out, err = self.responses[self._idx]
        self._idx += 1
        return ProcessResult(argv=tuple(argv), return_code=rc, stdout=out, stderr=err)


class _FailingRunner:
    """Every call returns a non-zero exit."""

    def __init__(self, *, exit_code: int = 1, stderr: str = ""):
        self._exit_code = exit_code
        self._stderr = stderr

    def run(self, argv: Sequence[str]) -> ProcessResult:
        return ProcessResult(
            argv=tuple(argv),
            return_code=self._exit_code,
            stdout="",
            stderr=self._stderr,
        )


class _TimeoutRunner:
    """Every call raises ``TimeoutError`` with captured partial output."""

    def __init__(self, *, stdout: str = "", stderr: str = ""):
        self._stdout = stdout
        self._stderr = stderr
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: Sequence[str]) -> ProcessResult:
        self.calls.append(tuple(argv))
        raise TimeoutError(self._stdout, self._stderr, tuple(argv))


class _ExecErrorRunner:
    """Every call raises ``OSError`` (command not found, etc.)."""

    def __init__(self, *, message: str = "No such file or directory"):
        self._message = message
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: Sequence[str]) -> ProcessResult:
        self.calls.append(tuple(argv))
        raise OSError(self._message)


# ═══════════════════════════════════════════════════════════════════════
# Shared helpers
# ═══════════════════════════════════════════════════════════════════════

_IMAGE = "pi-cli-pi:latest"
_T0 = 1735689600.0  # 2025-01-01T00:00:00Z


def _tmp_dir() -> str:
    return tempfile.mkdtemp(prefix="test-evidence-")


def _clock(*offsets: float) -> _FakeClock:
    """Return a clock that returns T0+o0, T0+o0+o1, ..."""
    times: list[float] = [_T0]
    for o in offsets:
        times.append(times[-1] + o)
    return _FakeClock(times)


# ═══════════════════════════════════════════════════════════════════════
# Tests — gate (only this test catches NotImplementedError)
# ═══════════════════════════════════════════════════════════════════════


class TestCollectEvidenceStub(unittest.TestCase):
    def test_stub_raises_not_implemented(self) -> None:
        """Gate test — collect_evidence is now live (no longer a stub)."""
        runner = _RecordingRunner()
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=_clock(),
            image=_IMAGE,
        )
        self.assertIsInstance(bundle, EvidenceBundle)


# ═══════════════════════════════════════════════════════════════════════
# Tests — model integrity
# ═══════════════════════════════════════════════════════════════════════


class TestEvidenceCommandModel(unittest.TestCase):
    def test_defaults_and_fields(self) -> None:
        cmd = EvidenceCommand(
            argv=("docker", "inspect", _IMAGE),
            return_code=0,
            timestamp_epoch=_T0,
            duration_seconds=0.12,
            stdout_file="inspect.stdout.log",
            stderr_file="inspect.stderr.log",
            stdout_sha256="abc123",
            stderr_sha256="def456",
        )
        self.assertFalse(cmd.skipped)
        self.assertEqual(("docker", "inspect", _IMAGE), cmd.argv)
        self.assertEqual(0, cmd.return_code)
        self.assertEqual(_T0, cmd.timestamp_epoch)
        self.assertAlmostEqual(0.12, cmd.duration_seconds, delta=0.01)

    def test_skipped_command(self) -> None:
        cmd = EvidenceCommand(
            argv=("docker", "run", _IMAGE),
            return_code=None,
            timestamp_epoch=_T0,
            duration_seconds=0.0,
            stdout_file=None,
            stderr_file=None,
            stdout_sha256=None,
            stderr_sha256=None,
            skipped=True,
        )
        self.assertTrue(cmd.skipped)
        self.assertIsNone(cmd.return_code)
        self.assertIsNone(cmd.stdout_file)

    def test_immutable(self) -> None:
        cmd = EvidenceCommand(
            argv=("cmd",),
            return_code=0,
            timestamp_epoch=_T0,
            duration_seconds=1.0,
            stdout_file="o",
            stderr_file="e",
            stdout_sha256="a",
            stderr_sha256="b",
        )
        with self.assertRaises(Exception):
            cmd.argv = ("other",)  # type: ignore[misc]


class TestEvidenceNoteModel(unittest.TestCase):
    def test_fields(self) -> None:
        note = EvidenceNote(key="inspect", detail="Size: 1.2 GB")
        self.assertEqual("inspect", note.key)
        self.assertEqual("Size: 1.2 GB", note.detail)


class TestEvidenceBundleModel(unittest.TestCase):
    def test_fields(self) -> None:
        out = Path("/tmp/bundle")
        cmd = EvidenceCommand(
            argv=("cmd",),
            return_code=0,
            timestamp_epoch=_T0,
            duration_seconds=0.5,
            stdout_file="o.log",
            stderr_file="e.log",
            stdout_sha256="sha",
            stderr_sha256="sha",
        )
        note = EvidenceNote(key="image", detail="ok")
        bundle = EvidenceBundle(
            commands=(cmd,),
            notes=(note,),
            output_dir=out,
            index_path=out / "index.txt",
            dry_run=False,
        )
        self.assertEqual((cmd,), bundle.commands)
        self.assertEqual((note,), bundle.notes)
        self.assertEqual(out, bundle.output_dir)
        self.assertEqual(out / "index.txt", bundle.index_path)
        self.assertFalse(bundle.dry_run)


# ═══════════════════════════════════════════════════════════════════════
# Tests — command recording (RED — hits NotImplementedError)
# ═══════════════════════════════════════════════════════════════════════


class TestCommandRecording(unittest.TestCase):
    """Every executed command is captured with argv, exit code, and timing."""

    def test_argv_recorded_exactly(self) -> None:
        """The argument vector appears verbatim in the evidence record."""
        runner = _RecordingRunner(responses=[(0, "ok\n", "")])
        clk = _clock(0.5, 0.1)
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=clk,
            image=_IMAGE,
        )
        self.assertEqual(1, len(bundle.commands))
        self.assertEqual(("docker", "inspect", _IMAGE), bundle.commands[0].argv)

    def test_exit_code_recorded(self) -> None:
        """Non-zero exit codes are recorded, not treated as collection errors."""
        runner = _RecordingRunner(responses=[(2, "", "fatal\n")])
        clk = _clock(0.1, 0.2)
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=clk,
            image=_IMAGE,
        )
        self.assertEqual(2, bundle.commands[0].return_code)

    def test_timing_recorded(self) -> None:
        """Each command has a non-negative duration from the injected clock."""
        runner = _RecordingRunner(responses=[(0, "", "")])
        clk = _clock(0.5, 0.1)
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=clk,
            image=_IMAGE,
        )
        self.assertGreaterEqual(bundle.commands[0].duration_seconds, 0.0)

    def test_timestamp_is_clock_epoch_before_command(self) -> None:
        """The timestamp is the epoch from ``clock.now()`` just before running."""
        runner = _RecordingRunner(responses=[(0, "", "")])
        clk = _clock(3.0, 0.1)
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=clk,
            image=_IMAGE,
        )
        self.assertEqual(_T0, bundle.commands[0].timestamp_epoch)

    def test_duration_is_clock_difference(self) -> None:
        """Duration equals ``end_epoch - start_epoch`` from the injected clock."""
        runner = _RecordingRunner(responses=[(0, "", "")])
        clk = _clock(0.75)
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=clk,
            image=_IMAGE,
        )
        self.assertAlmostEqual(0.75, bundle.commands[0].duration_seconds, delta=0.001)

    def test_multi_command_timestamps_increase(self) -> None:
        """Successive commands get later timestamps."""
        runner = _RecordingRunner(responses=[(0, "", ""), (0, "", "")])
        clk = _clock(1.0, 2.0, 3.0, 4.0)
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=clk,
            image=_IMAGE,
            commands=[("cmd1",), ("cmd2",)],
        )
        self.assertEqual(2, len(bundle.commands))
        self.assertLess(
            bundle.commands[0].timestamp_epoch,
            bundle.commands[1].timestamp_epoch,
        )


# ═══════════════════════════════════════════════════════════════════════
# Tests — bounded output capture (RED — hits NotImplementedError)
# ═══════════════════════════════════════════════════════════════════════


class TestBoundedOutput(unittest.TestCase):
    """Stdout and stderr are saved to the output directory and checksummed."""

    def test_stdout_saved_to_file(self) -> None:
        runner = _RecordingRunner(responses=[(0, "hello world\n", "")])
        clk = _clock(0.1, 0.2)
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=clk,
            image=_IMAGE,
        )
        cmd = bundle.commands[0]
        self.assertIsNotNone(cmd.stdout_file)
        out_path = bundle.output_dir / cmd.stdout_file  # type: ignore[arg-type]
        self.assertTrue(out_path.is_file())
        self.assertEqual("hello world\n", out_path.read_text())

    def test_stderr_saved_to_file(self) -> None:
        runner = _RecordingRunner(responses=[(0, "", "warning\n")])
        clk = _clock(0.1, 0.2)
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=clk,
            image=_IMAGE,
        )
        cmd = bundle.commands[0]
        self.assertIsNotNone(cmd.stderr_file)
        err_path = bundle.output_dir / cmd.stderr_file  # type: ignore[arg-type]
        self.assertTrue(err_path.is_file())
        self.assertEqual("warning\n", err_path.read_text())

    def test_sha256_of_output_files(self) -> None:
        """Every recorded command includes the SHA-256 of its captured output."""
        runner = _RecordingRunner(responses=[(0, "data\n", "err\n")])
        clk = _clock(0.1, 0.2)
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=clk,
            image=_IMAGE,
        )
        cmd = bundle.commands[0]
        self.assertEqual(hashlib.sha256(b"data\n").hexdigest(), cmd.stdout_sha256)
        self.assertEqual(hashlib.sha256(b"err\n").hexdigest(), cmd.stderr_sha256)

    def test_empty_output_still_checksummed(self) -> None:
        """Even empty stdout/stderr produce the hash of empty content."""
        runner = _RecordingRunner(responses=[(0, "", "")])
        clk = _clock(0.1, 0.2)
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=clk,
            image=_IMAGE,
        )
        cmd = bundle.commands[0]
        empty_hash = hashlib.sha256(b"").hexdigest()
        self.assertEqual(empty_hash, cmd.stdout_sha256)
        self.assertEqual(empty_hash, cmd.stderr_sha256)


# ═══════════════════════════════════════════════════════════════════════
# Tests — output truncation (RED — hits NotImplementedError)
# ═══════════════════════════════════════════════════════════════════════


class TestOutputTruncation(unittest.TestCase):
    """Output that exceeds *max_output_bytes* is truncated and the
    command metadata records the truncation."""

    _BOUND = 256

    def test_oversized_stdout_truncated_to_bound(self) -> None:
        """Stdout larger than the bound is cut at exactly *max_output_bytes*."""
        oversized = "X" * (self._BOUND + 100)
        runner = _RecordingRunner(responses=[(0, oversized, "")])
        clk = _clock(0.1, 0.2)
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=clk,
            image=_IMAGE,
            max_output_bytes=self._BOUND,
        )
        cmd = bundle.commands[0]
        self.assertIsNotNone(cmd.stdout_file)
        out_path = bundle.output_dir / cmd.stdout_file  # type: ignore[arg-type]
        self.assertEqual(self._BOUND, out_path.stat().st_size)

    def test_oversized_stderr_truncated_to_bound(self) -> None:
        """Stderr larger than the bound is cut at exactly *max_output_bytes*."""
        oversized = "Y" * (self._BOUND + 50)
        runner = _RecordingRunner(responses=[(0, "", oversized)])
        clk = _clock(0.1, 0.2)
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=clk,
            image=_IMAGE,
            max_output_bytes=self._BOUND,
        )
        cmd = bundle.commands[0]
        self.assertIsNotNone(cmd.stderr_file)
        err_path = bundle.output_dir / cmd.stderr_file  # type: ignore[arg-type]
        self.assertEqual(self._BOUND, err_path.stat().st_size)

    def test_truncation_metadata_on_command(self) -> None:
        """When stdout is truncated the command record carries
        ``stdout_truncated=True`` and the original byte count."""
        oversized = "Z" * 1024
        bound = 200
        runner = _RecordingRunner(responses=[(0, oversized, "")])
        clk = _clock(0.1, 0.2)
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=clk,
            image=_IMAGE,
            max_output_bytes=bound,
        )
        cmd = bundle.commands[0]
        self.assertTrue(cmd.stdout_truncated)
        self.assertEqual(len(oversized), cmd.stdout_original_bytes)

    def test_stderr_truncation_metadata(self) -> None:
        """When stderr is truncated the command record carries
        ``stderr_truncated=True`` and the original byte count."""
        oversized = "E" * 5000
        bound = 100
        runner = _RecordingRunner(responses=[(0, "", oversized)])
        clk = _clock(0.1, 0.2)
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=clk,
            image=_IMAGE,
            max_output_bytes=bound,
        )
        cmd = bundle.commands[0]
        self.assertTrue(cmd.stderr_truncated)
        self.assertEqual(len(oversized), cmd.stderr_original_bytes)

    def test_under_bound_output_not_truncated(self) -> None:
        """Output under the bound is stored in full with no truncation
        metadata."""
        small = "hello"
        runner = _RecordingRunner(responses=[(0, small, small)])
        clk = _clock(0.1, 0.2)
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=clk,
            image=_IMAGE,
            max_output_bytes=4096,
        )
        cmd = bundle.commands[0]
        self.assertFalse(cmd.stdout_truncated)
        self.assertFalse(cmd.stderr_truncated)
        self.assertIsNone(cmd.stdout_original_bytes)
        self.assertIsNone(cmd.stderr_original_bytes)
        # Full content is present.
        out_path = bundle.output_dir / cmd.stdout_file  # type: ignore[arg-type]
        self.assertEqual(small, out_path.read_text())

    def test_both_streams_truncated_independently(self) -> None:
        """When both stdout and stderr are oversized each is truncated
        independently and both truncation flags are set."""
        bound = 64
        big_out = "A" * 200
        big_err = "B" * 300
        runner = _RecordingRunner(responses=[(0, big_out, big_err)])
        clk = _clock(0.1, 0.2)
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=clk,
            image=_IMAGE,
            max_output_bytes=bound,
        )
        cmd = bundle.commands[0]
        self.assertTrue(cmd.stdout_truncated)
        self.assertTrue(cmd.stderr_truncated)
        self.assertEqual(len(big_out), cmd.stdout_original_bytes)
        self.assertEqual(len(big_err), cmd.stderr_original_bytes)
        # Both files respect the bound.
        out_file = bundle.output_dir / cmd.stdout_file  # type: ignore[arg-type]
        err_file = bundle.output_dir / cmd.stderr_file  # type: ignore[arg-type]
        self.assertLessEqual(out_file.stat().st_size, bound)
        self.assertLessEqual(err_file.stat().st_size, bound)

    def test_hash_is_of_truncated_content(self) -> None:
        """The sha256 recorded is the hash of the written (truncated)
        content, not the pre-truncation original."""
        bound = 10
        oversized = "X" * 100
        runner = _RecordingRunner(responses=[(0, oversized, "")])
        clk = _clock(0.1, 0.2)
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=clk,
            image=_IMAGE,
            max_output_bytes=bound,
        )
        cmd = bundle.commands[0]
        out_path = bundle.output_dir / cmd.stdout_file  # type: ignore[arg-type]
        truncated_content = out_path.read_text()
        self.assertEqual(
            hashlib.sha256(truncated_content.encode()).hexdigest(), cmd.stdout_sha256
        )


# ═══════════════════════════════════════════════════════════════════════
# Tests — image inspection (RED — hits NotImplementedError)
# ═══════════════════════════════════════════════════════════════════════


class TestImageInspection(unittest.TestCase):
    """`docker inspect` output is captured as a static evidence note."""

    def test_docker_inspect_invoked(self) -> None:
        runner = _RecordingRunner(responses=[(0, '{"Id":"sha256:abc"}', "")])
        clk = _clock(0.1, 0.2)
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=clk,
            image=_IMAGE,
            commands=[("docker", "inspect", _IMAGE)],
        )
        inspect_argv = runner.calls[0]
        self.assertIn("inspect", inspect_argv)
        self.assertIn(_IMAGE, inspect_argv)

    def test_image_id_recorded_as_note(self) -> None:
        runner = _RecordingRunner(
            responses=[
                (0, '[{"Id":"sha256:deadbeef"}]', ""),
            ]
        )
        clk = _clock(0.1, 0.2)
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=clk,
            image=_IMAGE,
            commands=[("docker", "inspect", _IMAGE), ("echo", "hi")],
        )
        self.assertTrue(any(n.key == "image-inspect" for n in bundle.notes))
        img_note = next(n for n in bundle.notes if n.key == "image-inspect")
        self.assertIn("deadbeef", img_note.detail)

    def test_inspect_failure_recorded_as_error_note(self) -> None:
        """A failed inspect is recorded without aborting the collection."""
        runner = _RecordingRunner(responses=[(1, "", "no such image")])
        clk = _clock(0.1, 0.2)
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=clk,
            image=_IMAGE,
            commands=[("docker", "inspect", _IMAGE)],
        )
        self.assertTrue(any(n.key == "image-inspect" for n in bundle.notes))


# ═══════════════════════════════════════════════════════════════════════
# Tests — checksums (bundle-level) (RED — hits NotImplementedError)
# ═══════════════════════════════════════════════════════════════════════


class TestBundleChecksums(unittest.TestCase):
    """The overall bundle is checksummed for integrity verification."""

    def test_every_command_has_stdout_and_stderr_checksum(self) -> None:
        runner = _RecordingRunner(
            responses=[(0, "alpha", "beta"), (0, "gamma", "delta")]
        )
        clk = _clock(0.1, 0.2, 0.0, 0.3)
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=clk,
            image=_IMAGE,
        )
        for cmd in bundle.commands:
            self.assertIsNotNone(cmd.stdout_sha256)
            self.assertIsNotNone(cmd.stderr_sha256)

    def test_skipped_commands_have_none_checksums(self) -> None:
        """In dry-run mode, checksums are None since no output was captured."""
        runner = _RecordingRunner(responses=[])
        clk = _clock(0.0)
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=clk,
            image=_IMAGE,
            dry_run=True,
        )
        for cmd in bundle.commands:
            self.assertIsNone(cmd.stdout_sha256)
            self.assertIsNone(cmd.stderr_sha256)
            self.assertTrue(cmd.skipped)


# ═══════════════════════════════════════════════════════════════════════
# Tests — redaction (RED — hits NotImplementedError)
# ═══════════════════════════════════════════════════════════════════════


class TestRedaction(unittest.TestCase):
    """Sensitive values are scrubbed from recorded command arguments
    and captured output files.  The collector receives an explicit
    *commands* list so the test controls what secret-bearing argv is
    executed."""

    _SECRET_CMD: tuple[str, ...] = (
        "docker",
        "run",
        "-e",
        "SECRET_TOKEN=s3cret!",
        "-v",
        "/home/dev/project:/home/dev/work",
        _IMAGE,
        "sleep",
        "infinity",
    )

    def test_env_value_scrubbed_from_recorded_argv(self) -> None:
        """The raw secret value is absent from every
        ``EvidenceCommand.argv`` while a ``REDACTED`` marker replaces
        it.  ``runner.calls`` still contains the original, proving the
        command was issued with the secret."""
        runner = _RecordingRunner(responses=[(0, "", "")])
        clk = _clock(0.1, 0.2, 0.0, 0.1)
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=clk,
            image=_IMAGE,
            commands=[self._SECRET_CMD],
        )
        # 1. The runner was called with the raw secret.
        original = runner.calls[0]
        self.assertIn("s3cret!", " ".join(original))
        # 2. The bundle command NEVER contains the raw secret.
        recorded = bundle.commands[0]
        argv_joined = " ".join(recorded.argv)
        self.assertNotIn("s3cret!", argv_joined)
        # 3. A REDACTED marker is present in the scrubbed position.
        self.assertIn("REDACTED", argv_joined)
        # 4. Non-secret parts are preserved.
        self.assertIn("/home/dev/project:/home/dev/work", argv_joined)
        self.assertIn(_IMAGE, argv_joined)

    def test_host_paths_preserved_alongside_redacted_secrets(self) -> None:
        """Host filesystem paths (``-v /host:...``) are preserved
        verbatim while neighbouring ``-e`` values are redacted."""
        cmd_with_path = (
            "docker",
            "run",
            "-e",
            "ANOTHER_TOKEN=passw0rd",
            "-v",
            "/tmp/my-project:/home/dev/work",
            "-e",
            "DEBUG=1",
            _IMAGE,
        )
        runner = _RecordingRunner(responses=[(0, "", "")])
        clk = _clock(0.1, 0.2, 0.0, 0.1)
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=clk,
            image=_IMAGE,
            commands=[cmd_with_path],
        )
        recorded = bundle.commands[0]
        argv_joined = " ".join(recorded.argv)
        # Raw secret absent.
        self.assertNotIn("passw0rd", argv_joined)
        # Redaction marker present for the scrubbed -e value.
        self.assertIn("REDACTED", argv_joined)
        # Host path preserved intact.
        self.assertIn("/tmp/my-project:/home/dev/work", argv_joined)
        # Non-secret -e flag preserved.
        self.assertIn("DEBUG=1", argv_joined)

    def test_stderr_token_scrubbed_from_captured_file(self) -> None:
        """When a command's stderr contains a bearer token, the captured
        stderr file has the token replaced with a marker; the raw token
        does not appear on disk."""
        # Command that will emit secret-bearing stderr.
        cmd = ("some-tool", "--verbose")
        runner = _RecordingRunner(
            responses=[(0, "", "error log\nAuthorization: Bearer tok3n-abc123\n")],
        )
        clk = _clock(0.1, 0.2, 0.0, 0.1)
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=clk,
            image=_IMAGE,
            commands=[cmd],
        )
        self.assertGreaterEqual(len(bundle.commands), 1)
        recorded = bundle.commands[0]
        self.assertIsNotNone(recorded.stderr_file)
        captured = (bundle.output_dir / recorded.stderr_file).read_text()  # type: ignore[arg-type]
        self.assertNotIn("tok3n-abc123", captured)
        self.assertIn("REDACTED", captured)


# ═══════════════════════════════════════════════════════════════════════
# Tests — failure handling (RED — hits NotImplementedError)
# ═══════════════════════════════════════════════════════════════════════


class TestFailureHandling(unittest.TestCase):
    """The collector captures process failures without aborting the
    entire collection.  Timeout and exec errors are caught, recorded
    with distinguished return codes, and subsequent commands run."""

    def test_failed_command_does_not_abort_collection(self) -> None:
        """A non-zero exit is recorded; subsequent commands still run."""
        runner = _RecordingRunner(responses=[(1, "", "build failed"), (0, "ok", "")])
        clk = _clock(0.1, 0.2, 0.0, 0.3)
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=clk,
            image=_IMAGE,
            commands=[("docker", "inspect", _IMAGE), ("c1",)],
        )
        self.assertEqual(2, len(bundle.commands))
        self.assertTrue(any(c.return_code == 1 for c in bundle.commands))
        self.assertTrue(any(c.return_code == 0 for c in bundle.commands))

    def test_all_commands_run_even_after_failures(self) -> None:
        runner = _RecordingRunner(
            responses=[
                (1, "", "fail 1"),
                (2, "", "fail 2"),
                (0, "final ok", ""),
            ]
        )
        clk = _clock(0.1, 0.2, 0.0, 0.3, 0.0, 0.1)
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=clk,
            image=_IMAGE,
            commands=[("f1",), ("f2",), ("ok",)],
        )
        self.assertEqual(3, len(bundle.commands))
        self.assertTrue(any(c.return_code == 0 for c in bundle.commands))

    def test_timeout_recorded_with_code_124(self) -> None:
        """When the runner raises ``TimeoutError`` the collector records
        return code 124 and saves any partial stdout/stderr."""
        partial_out = "starting..."
        runner = _TimeoutRunner(stdout=partial_out, stderr="timed out\n")
        clk = _clock(0.1, 0.2)
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=clk,
            image=_IMAGE,
            commands=[("slow-cmd",)],
        )
        self.assertEqual(1, len(bundle.commands))
        cmd = bundle.commands[0]
        self.assertEqual(124, cmd.return_code)
        self.assertIsNotNone(cmd.stdout_file)
        captured = (bundle.output_dir / cmd.stdout_file).read_text()  # type: ignore[arg-type]
        self.assertEqual(partial_out, captured)

    def test_exec_error_recorded_with_code_126(self) -> None:
        """When the runner raises ``OSError`` the collector records
        return code 126 and captures the error message."""
        runner = _ExecErrorRunner(message="No such file or directory")
        clk = _clock(0.1, 0.2)
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=clk,
            image=_IMAGE,
            commands=[("nonexistent",)],
        )
        self.assertEqual(1, len(bundle.commands))
        cmd = bundle.commands[0]
        self.assertEqual(126, cmd.return_code)
        self.assertIsNotNone(cmd.stderr_file)
        captured = (bundle.output_dir / cmd.stderr_file).read_text()  # type: ignore[arg-type]
        self.assertIn("No such file", captured)

    def test_collection_continues_after_timeout(self) -> None:
        """A timeout on one command does not prevent subsequent
        commands from running."""

        # First command times out, second succeeds.
        class _Mixed:
            def __init__(self):
                self.calls: list[tuple[str, ...]] = []
                self._count = 0

            def run(self, argv: Sequence[str]) -> ProcessResult:
                self.calls.append(tuple(argv))
                self._count += 1
                if self._count == 1:
                    raise TimeoutError("", "timeout", tuple(argv))
                return ProcessResult(
                    argv=tuple(argv),
                    return_code=0,
                    stdout="recovered",
                    stderr="",
                )

        runner = _Mixed()
        clk = _clock(0.1, 0.2, 0.0, 0.3)
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=clk,
            image=_IMAGE,
            commands=[("slow-cmd",), ("fast-cmd",)],
        )
        self.assertEqual(2, len(bundle.commands))
        self.assertEqual(124, bundle.commands[0].return_code)
        self.assertEqual(0, bundle.commands[1].return_code)

    def test_collection_continues_after_exec_error(self) -> None:
        """An exec error on one command does not prevent subsequent
        commands from running."""

        class _Mixed:
            def __init__(self):
                self.calls: list[tuple[str, ...]] = []
                self._count = 0

            def run(self, argv: Sequence[str]) -> ProcessResult:
                self.calls.append(tuple(argv))
                self._count += 1
                if self._count == 1:
                    raise OSError("command not found")
                return ProcessResult(
                    argv=tuple(argv),
                    return_code=0,
                    stdout="recovered",
                    stderr="",
                )

        runner = _Mixed()
        clk = _clock(0.1, 0.2, 0.0, 0.3)
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=clk,
            image=_IMAGE,
            commands=[("bad-cmd",), ("good-cmd",)],
        )
        self.assertEqual(2, len(bundle.commands))
        self.assertEqual(126, bundle.commands[0].return_code)
        self.assertEqual(0, bundle.commands[1].return_code)


# ═══════════════════════════════════════════════════════════════════════
# Tests — dry-run (RED — hits NotImplementedError)
# ═══════════════════════════════════════════════════════════════════════


class TestDryRun(unittest.TestCase):
    """Dry-run records commands without executing them."""

    def test_dry_run_records_commands_without_invoking_runner(self) -> None:
        """In dry-run mode, commands are recorded but the runner is never called."""
        runner = _RecordingRunner(responses=[])
        clk = _clock(0.0)
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=clk,
            image=_IMAGE,
            dry_run=True,
        )
        self.assertEqual(0, len(runner.calls))
        self.assertTrue(bundle.dry_run)

    def test_dry_run_commands_all_skipped(self) -> None:
        runner = _RecordingRunner(responses=[])
        clk = _clock(0.0)
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=clk,
            image=_IMAGE,
            dry_run=True,
        )
        for cmd in bundle.commands:
            self.assertTrue(cmd.skipped)
            self.assertIsNone(cmd.return_code)

    def test_dry_run_still_produces_timestamps(self) -> None:
        """Even dry-run commands get a timestamp from the injected clock."""
        runner = _RecordingRunner(responses=[])
        clk = _clock(0.0)
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=clk,
            image=_IMAGE,
            dry_run=True,
        )
        for cmd in bundle.commands:
            self.assertEqual(_T0, cmd.timestamp_epoch)


# ═══════════════════════════════════════════════════════════════════════
# Tests — human-readable index (RED — hits NotImplementedError)
# ═══════════════════════════════════════════════════════════════════════


class TestHumanReadableIndex(unittest.TestCase):
    """The collector writes an index file with a structured summary for
    human inspection — not just a note in the bundle metadata."""

    def test_index_file_exists_in_output_dir(self) -> None:
        """An index file is written at ``bundle.index_path`` inside the
        output directory."""
        runner = _RecordingRunner(responses=[(0, "", "")])
        clk = _clock(0.1, 0.2)
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=clk,
            image=_IMAGE,
        )
        self.assertTrue(bundle.index_path.is_file())
        # index_path must live under output_dir
        self.assertTrue(str(bundle.index_path).startswith(str(bundle.output_dir)))

    def test_index_includes_image_name(self) -> None:
        """The index mentions the inspected image."""
        runner = _RecordingRunner(responses=[(0, "", "")])
        clk = _clock(0.1, 0.2)
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=clk,
            image=_IMAGE,
        )
        index_text = bundle.index_path.read_text()
        self.assertIn(_IMAGE, index_text)

    def test_index_includes_collection_timestamp(self) -> None:
        """The index shows the wall-clock time of collection (from the
        injected clock), in a human-readable form."""
        runner = _RecordingRunner(responses=[(0, "", "")])
        clk = _clock(0.1, 0.2)
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=clk,
            image=_IMAGE,
        )
        index_text = bundle.index_path.read_text()
        # The epoch value or a human-readable rendering must appear.
        self.assertTrue(
            str(int(_T0)) in index_text or "2025" in index_text,
            f"timestamp {_T0} or year not found in index:\n{index_text}",
        )

    def test_index_lists_every_command_with_exit_code_and_duration(self) -> None:
        """Every recorded command appears in the index with at least its
        argv, exit code, and duration."""
        runner = _RecordingRunner(responses=[(0, "ok1", ""), (1, "", "fail")])
        clk = _clock(0.1, 0.2, 0.0, 0.3)
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=clk,
            image=_IMAGE,
            commands=[("c1",), ("c2",)],
        )
        index_text = bundle.index_path.read_text()
        self.assertEqual(2, len(bundle.commands))
        for cmd in bundle.commands:
            if not cmd.skipped:
                # The command's first arg must appear in the index.
                self.assertIn(cmd.argv[0], index_text)
                # Its exit code must be referenced.
                self.assertIn(str(cmd.return_code), index_text)

    def test_index_states_overall_pass_or_fail(self) -> None:
        """The index declares whether the entire collection succeeded
        or had failures."""
        runner = _RecordingRunner(responses=[(0, "", ""), (1, "", "err")])
        clk = _clock(0.1, 0.2, 0.0, 0.3)
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=clk,
            image=_IMAGE,
        )
        index_text = bundle.index_path.read_text().lower()
        # At least one of these status words must appear.
        self.assertTrue(
            "fail" in index_text
            or "pass" in index_text
            or "success" in index_text
            or "error" in index_text,
            f"no pass/fail indicator in index:\n{index_text}",
        )

    def test_index_includes_bundle_self_checksum(self) -> None:
        """The index includes a checksum that covers the entire bundle
        for offline integrity verification."""
        runner = _RecordingRunner(responses=[(0, "data", "")])
        clk = _clock(0.1, 0.2)
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=clk,
            image=_IMAGE,
        )
        index_text = bundle.index_path.read_text()
        # A hash-like hex string (at least 32 chars) must appear.
        self.assertTrue(
            any(
                len(token) >= 32 and all(c in "0123456789abcdef" for c in token.lower())
                for token in index_text.split()
            ),
            f"no hash-like token in index:\n{index_text}",
        )

    def test_index_references_stdout_stderr_paths(self) -> None:
        """The index references the captured stdout/stderr files so a
        human can navigate to them."""
        runner = _RecordingRunner(
            responses=[(0, "alpha", "beta"), (0, "gamma", "delta")]
        )
        clk = _clock(0.1, 0.2, 0.0, 0.3)
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=clk,
            image=_IMAGE,
        )
        index_text = bundle.index_path.read_text()
        for cmd in bundle.commands:
            if cmd.stdout_file:
                self.assertIn(cmd.stdout_file, index_text)
            if cmd.stderr_file:
                self.assertIn(cmd.stderr_file, index_text)


# ═══════════════════════════════════════════════════════════════════════
# Tests — static notes (RED — hits NotImplementedError)
# ═══════════════════════════════════════════════════════════════════════


class TestStaticNotes(unittest.TestCase):
    """Static notes capture host/environment metadata alongside commands."""

    def test_host_metadata_recorded_as_note(self) -> None:
        runner = _RecordingRunner(responses=[(0, "", "")])
        clk = _clock(0.1, 0.2)
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=clk,
            image=_IMAGE,
        )
        self.assertTrue(any(n.key == "host-metadata" for n in bundle.notes))

    def test_git_sha_recorded_as_note(self) -> None:
        runner = _RecordingRunner(responses=[(0, "", "")])
        clk = _clock(0.1, 0.2)
        bundle = collect_evidence(
            output_dir=Path(_tmp_dir()),
            runner=runner,
            clock=clk,
            image=_IMAGE,
        )
        self.assertTrue(any(n.key == "git-sha" for n in bundle.notes))


if __name__ == "__main__":
    unittest.main()

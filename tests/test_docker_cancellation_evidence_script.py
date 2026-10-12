"""Offline tests of evidence collection mechanics, not Docker daemon behavior."""

import importlib.util
import json
import os

from docker.filesystem.operations import PosixDescriptorOps
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/collect-docker-cancellation-evidence.py"
spec = importlib.util.spec_from_file_location("cancellation_evidence_script", SCRIPT)
assert spec is not None and spec.loader is not None
script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(script)


class DockerCancellationEvidenceScriptTests(unittest.TestCase):
    def test_identity_requires_full_lowercase_hex(self):
        self.assertTrue(script.valid_id("a" * 64))
        for value in ("", "a" * 12, "a" * 65, "A" * 64, "g" * 64, "a" * 64 + "\n"):
            self.assertFalse(script.valid_id(value))

    def test_jitter_and_remaining_budget(self):
        self.assertAlmostEqual(script.next_delay(0.01, 10, 0.9), 0.009)
        self.assertAlmostEqual(script.next_delay(0.01, 10, 1.1), 0.011)
        self.assertEqual(script.next_delay(20, 0.004, 1.0), 0.004)
        self.assertEqual(script.next_delay(20, -1, 1.0), 0)
        with self.assertRaises(ValueError):
            script.next_delay(0.01, 10, 1.2)

    def test_discovery_requires_all_three_exact_filters(self):
        self.assertEqual(script.filters(123, "unique-operation"), [
            "--filter", "label=io.docker-constructor.managed=true",
            "--filter", "label=io.docker-constructor.controller-pid=123",
            "--filter", "label=io.docker-constructor.operation-id=unique-operation",
        ])

    def test_missing_docker_records_failure_not_a_simulated_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "evidence"
            missing = str(Path(temporary) / "missing-docker")
            with patch("sys.argv", [str(SCRIPT), "--output", str(output), "--docker", missing]):
                self.assertEqual(script.main(), 1)
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(summary["exit_status"], 1)
            self.assertIsNotNone(summary["commands"][0]["error"])
            self.assertIsNone(summary["commands"][0]["returncode"])
            self.assertFalse(summary["checks"][0]["passed"])
            self.assertTrue((output / "000.stdout").exists())
            self.assertTrue((output / "000.stderr").exists())

    def test_existing_output_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            marker = output / "marker"
            marker.write_text("preserve")
            with patch("sys.argv", [str(SCRIPT), "--output", str(output)]):
                with self.assertRaises(FileExistsError):
                    script.main()
            self.assertEqual(marker.read_text(), "preserve")


class FaultOps(PosixDescriptorOps):
    def __init__(self, faults):
        self.faults = list(faults)
        self.closed = []
        self.acquired = []

    def openat(self, directory_fd, name, flags, mode=0):
        fd = super().openat(directory_fd, name, flags, mode)
        self.acquired.append(fd)
        return fd

    def close(self, fd):
        self.closed.append(fd)
        super().close(fd)
        fault = self.faults.pop(0) if self.faults else None
        if fault is not None:
            raise fault


class OwnedEvidenceIOTests(unittest.TestCase):
    def exercise(self, faults, operation=None, repeat=False):
        ops = FaultOps(faults)
        with tempfile.TemporaryDirectory() as temporary:
            paths = [(Path(temporary) / str(i), os.O_WRONLY | os.O_CREAT) for i in range(2)]

            def action(owners):
                if repeat:
                    owners[0].close()
                    owners[0].close()
                if operation is not None:
                    raise operation
                return "result"

            try:
                result = script._with_owned(paths, action, ops=ops)
            except BaseException as error:
                return ops, error
            return ops, result

    def test_operation_plus_close_preserves_identity_and_secondary(self):
        operation = ValueError("operation")
        close = OSError("close")
        ops, result = self.exercise([close, None], operation)
        self.assertIs(result, operation)
        self.assertIn(close, result._transaction_secondary)
        self.assertEqual(ops.closed, ops.acquired)

    def test_sole_close_is_failure_and_sibling_cleanup_runs(self):
        close = OSError("close")
        ops, result = self.exercise([close, None])
        self.assertIs(result, close)
        self.assertEqual(ops.closed, ops.acquired)

    def test_close_interruption_wins_and_independent_cleanup_runs(self):
        operation = ValueError("operation")
        interrupt = KeyboardInterrupt("close")
        ops, result = self.exercise([interrupt, OSError("sibling")], operation)
        self.assertIs(result, interrupt)
        self.assertIn(operation, result._transaction_secondary)
        self.assertEqual(ops.closed, ops.acquired)

    def test_operation_interruption_retains_first_identity(self):
        interrupt = KeyboardInterrupt("operation")
        ops, result = self.exercise([KeyboardInterrupt("later"), None], interrupt)
        self.assertIs(result, interrupt)
        self.assertEqual(ops.closed, ops.acquired)

    def test_repeated_release_never_retries(self):
        ops, result = self.exercise([None, None], repeat=True)
        self.assertEqual(result, "result")
        self.assertEqual(ops.closed, ops.acquired)

    def test_repeated_failed_release_is_terminal_and_sibling_runs(self):
        close = OSError("close")
        ops, result = self.exercise([close, None], repeat=True)
        self.assertIs(result, close)
        self.assertEqual(ops.closed, ops.acquired)

    def test_unexpected_close_defect_outranks_operation(self):
        operation = ValueError("operation")
        defect = RuntimeError("close defect")
        ops, result = self.exercise([defect, None], operation)
        self.assertIs(result, defect)
        self.assertIn(operation, result._transaction_secondary)
        self.assertEqual(ops.closed, ops.acquired)

    def test_second_acquisition_failure_releases_first(self):
        with tempfile.TemporaryDirectory() as temporary:
            ops = FaultOps([OSError("release")])
            primary = OSError("second open")
            original = ops.openat

            def acquire(*args):
                if ops.acquired:
                    raise primary
                return original(*args)

            with patch.object(ops, "openat", side_effect=acquire):
                with self.assertRaises(OSError) as caught:
                    script._with_owned([
                        (Path(temporary) / "first", os.O_WRONLY | os.O_CREAT),
                        (Path(temporary) / "second", os.O_WRONLY | os.O_CREAT),
                    ], lambda owners: None, ops=ops)
            self.assertIs(caught.exception, primary)
            self.assertEqual(ops.closed, ops.acquired)

    def test_read_decode_error_and_close_fault_preserve_operation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "text"
            script._write_text(path, "é")
            close = OSError("close")
            ops = FaultOps([close])
            with self.assertRaises(UnicodeDecodeError) as caught:
                script._read_text(path, encoding="ascii", ops=ops)
            self.assertIn(close, caught.exception._transaction_secondary)
            self.assertEqual(ops.closed, ops.acquired)

    def test_write_operation_plus_close_preserves_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            primary = OSError("write")
            close = OSError("close")
            ops = FaultOps([close])
            with patch.object(script.os, "write", side_effect=primary):
                with self.assertRaises(OSError) as caught:
                    script._write_text(Path(temporary) / "text", "value", ops=ops)
            self.assertIs(caught.exception, primary)
            self.assertIn(close, primary._transaction_secondary)
            self.assertEqual(ops.closed, ops.acquired)

    def test_write_sole_close_fault_is_observable(self):
        with tempfile.TemporaryDirectory() as temporary:
            close = OSError("close")
            ops = FaultOps([close])
            with self.assertRaises(OSError) as caught:
                script._write_text(Path(temporary) / "text", "value", ops=ops)
            self.assertIs(caught.exception, close)
            self.assertEqual(ops.closed, ops.acquired)


class EvidenceCleanupIntegrationTests(unittest.TestCase):
    def test_command_interruption_survives_json_close_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            evidence = script.Evidence(Path(temporary), "image", 1, "docker")
            primary = KeyboardInterrupt("command")
            publication = OSError("record close")
            try:
                with patch.object(script.subprocess, "run", side_effect=primary), \
                     patch.object(script, "_write_text", side_effect=publication):
                    with self.assertRaises(KeyboardInterrupt) as caught:
                        evidence.run("version")
                self.assertIs(caught.exception, primary)
                self.assertIn(publication, primary._transaction_secondary)
            finally:
                evidence.cleanup()

    def test_cleanup_interruption_does_not_skip_other_operations_or_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            evidence = script.Evidence(Path(temporary), "image", 1, "docker")
            first = evidence.operation("first")
            second = evidence.operation("second")
            primary = KeyboardInterrupt("discovery")
            calls = []

            def discover(operation):
                calls.append(operation)
                if operation == first:
                    raise primary
                return []

            with patch.object(evidence, "discover", side_effect=discover):
                with self.assertRaises(KeyboardInterrupt) as caught:
                    evidence.cleanup()
                self.assertIs(caught.exception, primary)
                self.assertIn(second, calls)
                previous = list(calls)
                self.assertTrue(evidence.cleanup())
                self.assertEqual(calls, previous)

    def test_main_preserves_first_interruption_through_summary_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "evidence"
            primary = KeyboardInterrupt("probe")
            later = KeyboardInterrupt("cleanup")
            publication = OSError("summary close")
            selected = []
            finish = script._finish

            def observe(ledger, original):
                try:
                    return finish(ledger, original)
                except BaseException as error:
                    selected.append(error)
                    raise

            with patch("sys.argv", [str(SCRIPT), "--output", str(output)]), \
                 patch.object(script.Evidence, "probe", side_effect=primary), \
                 patch.object(script.Evidence, "cleanup", side_effect=later), \
                 patch.object(script, "_write_text", side_effect=publication), \
                 patch.object(script, "_finish", side_effect=observe):
                self.assertEqual(script.main(), 130)
            self.assertEqual(selected, [primary, primary])
            self.assertIn(later, primary._transaction_secondary)
            self.assertIn(publication, primary._transaction_secondary)

    def test_creation_race_red_contract_precedes_group_three_implementation(self):
        tasks = (SCRIPT.parents[1] / "openspec/changes/add-cooperative-docker-process-wrapper/tasks.md").read_text()
        group = tasks.split("## 3.", 1)[1].split("## 4.", 1)[0]
        self.assertLess(group.index("3.10 Add policy-level"), group.index("### GREEN"))
        implementation = next(line for line in group.splitlines() if line.startswith("- [ ] 3.9"))
        self.assertIn("3.10", implementation)
        self.assertNotIn("4.8", implementation)
        self.assertIn("creation races in 3.10 before group 4", group)

    def test_script_has_no_unowned_file_methods(self):
        import ast
        tree = ast.parse(SCRIPT.read_text())
        self.assertFalse([
            node for node in ast.walk(tree) if isinstance(node, ast.Call)
            and ((isinstance(node.func, ast.Attribute)
                  and node.func.attr in {"open", "read_text", "write_text"})
                 or (isinstance(node.func, ast.Name) and node.func.id == "open"))
        ])


if __name__ == "__main__":
    unittest.main()

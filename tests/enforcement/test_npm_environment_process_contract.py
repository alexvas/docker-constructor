"""6BC adapter API, delegation, explicit handoff and executable guide gates."""
import ast
import inspect
from pathlib import Path
import subprocess
import sys
from typing import get_type_hints

import pytest

from docker.process import OwnedProcess
from docker.npm_environment import execution, lifecycle, streaming
from docker.npm_environment.execution import DockerRunExecutor

ROOT = Path(__file__).resolve().parents[2]
EXECUTION = ROOT / "docker/npm_environment/execution.py"
LIFECYCLE = ROOT / "docker/npm_environment/lifecycle.py"


def functions(tree):
    return {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}


def adapter_proof(execution_source, lifecycle_source):
    failures = []
    execution_tree, lifecycle_tree = ast.parse(execution_source), ast.parse(lifecycle_source)
    execute, capture = functions(execution_tree), functions(lifecycle_tree)
    stream = ast.unparse(execute["run_streaming"])
    core = ast.unparse(capture["_run_captured"])
    for text, operation in ((stream, "stream"), (core, "capture")):
        tree = ast.parse(text)
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        if sum(ast.unparse(node.func) == "OwnedProcess.launch" for node in calls) != 1:
            failures.append("owner acquisition is missing/repeated/replaced")
        if sum(ast.unparse(node.func) == f"owner.{operation}" for node in calls) != 1:
            failures.append("collection is missing/repeated/replaced")
        if "result.lifecycle.failures" not in text:
            failures.append("returned typed failures are not consumed")
    if "result.stdout.decode(errors='replace')" not in core or "result.stderr.decode(errors='replace')" not in core:
        failures.append("captured values lost replacement decoding")
    if "selected.kind is FailureKind.ORDINARY" not in stream or "selected.phase is FailurePhase.READ" not in stream:
        failures.append("authority lost to timeout or outcome-only inference")
    work = execute["_work"]
    transfers = [node for node in ast.walk(work) if isinstance(node, ast.Call)
                 and ast.unparse(node.func) == "FailureInfo" and node.args
                 and ast.unparse(node.args[0]) == "f.error"]
    if not transfers or any([ast.unparse(arg) for arg in node.args] !=
                            ["f.error", "f.kind", "FailurePhase.ACTION"] for node in transfers):
        failures.append("helper kind/identity/context is reconstructed or lost")
    for tree in (execution_tree, lifecycle_tree):
        aliases = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    aliases[alias.asname or alias.name] = alias.name
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        def origin(node):
            if isinstance(node, ast.Name):
                return aliases.get(node.id, node.id)
            if isinstance(node, ast.Attribute):
                return origin(node.value) + "." + node.attr
            return ast.unparse(node)
        # Track renamed callables too, not only a literal Popen spelling.
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, (ast.Name, ast.Attribute)):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        aliases[target.id] = origin(node.value)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in (
                "_proc", "_owners", "_readers", "_fds", "_tails", "_ledger",
                "process_result", "reader_failure_stream", "failure_info", "error_kind", "callback_errors",
            ):
                failures.append("raw/private owner access or obsolete result channel")
            if isinstance(node, ast.Call):
                target = origin(node.func)
                if target in ("subprocess.Popen", "docker.filesystem.streams.OwnedStream"):
                    failures.append("adapter independently acquires/adopts pipes")
                if isinstance(node.func, ast.Attribute):
                    receiver = ast.unparse(node.func.value)
                    if node.func.attr in ("terminate", "kill"):
                        failures.append("adapter independently signals child")
                    if node.func.attr == "wait" and receiver not in ("self.request", "self.ready"):
                        failures.append("adapter independently waits/reaps child")
                    if node.func.attr == "join" and (receiver != "self.thread" or ast.unparse(node) != "self.thread.join(0)"):
                        failures.append("unbounded/non-owned worker join")
            if isinstance(node, ast.ClassDef) and any("OwnedProcess" in ast.unparse(base) for base in node.bases):
                failures.append("domain overrides generic ownership")
    for name in ("_adopt_stream_owners", "_make_streaming_reader", "_finalize_setup_failure", "_join_supervisor",
                 "_terminate_and_reap", "_terminate_and_remove", "_close_stream_owners"):
        if name in execute:
            failures.append("obsolete process coordinator retained")
    for name in ("DeadlineSupervisor", "reap_process", "terminate_and_reap", "close_descriptors"):
        if any(isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name == name for node in ast.walk(lifecycle_tree)):
            failures.append("obsolete captured coordinator retained")
    failures.extend(diagnostic_proof(execution_tree))
    return failures


def diagnostic_proof(tree):
    """Bind public note construction to typed prefixes and bounded sanitizers."""
    failures = []
    execute = functions(tree)
    prefix = execute["_process_cleanup_prefix"]
    expected = ast.parse(
        "f'{_FAILURE_SUBJECTS[failure.stream]} {_FAILURE_OPERATIONS[failure.phase]} failed'",
        mode="eval",
    ).body
    if (len(prefix.body) != 1 or not isinstance(prefix.body[0], ast.Return)
            or ast.dump(prefix.body[0].value) != ast.dump(expected)):
        failures.append("cleanup prefix guesses provenance instead of using declared context")
    permitted_notes = {
        ast.dump(ast.parse(expression, mode="eval").body)
        for expression in (
            "_mask_cleanup_urls(project(str(note), secrets, tail_bytes=READER_FAILURE_DETAIL_BYTES))",
            "_process_cleanup_prefix(failure) + ': ' + _mask_cleanup_urls("
            "_render_close_failure(failure.error, secrets, tail_projector=tail_projector))",
            "'local process cleanup incomplete'",
        )
    }
    for node in ast.walk(execute["run_streaming"]):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_note":
            if len(node.args) != 1 or ast.dump(node.args[0]) not in permitted_notes:
                failures.append("unbounded/unsanitized or guessed process cleanup note")
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Attribute) and target.attr == "__notes__" for target in node.targets
        ):
            if ast.dump(node.value) != ast.dump(ast.List(elts=[], ctx=ast.Load())):
                failures.append("raw generic notes retained across domain projection")
    return failures


def test_adapter_delegation_and_strict_handoff_proof():
    assert adapter_proof(EXECUTION.read_text(), LIFECYCLE.read_text()) == []


@pytest.mark.parametrize("which,old,new", [
    ("execution", "owner.stream(chunk, on_stream_end=end)", "owner._proc.wait()"),
    ("execution", "f.error, f.kind, FailurePhase.ACTION", "f.error, FailureKind.ORDINARY, FailurePhase.ACTION"),
    ("execution", "f.error, f.kind, FailurePhase.ACTION", "f.error, f.kind, FailurePhase.READ"),
    ("execution", "selected.kind is FailureKind.ORDINARY", "True"),
    ("execution", "selected.phase is FailurePhase.READ", "True"),
    ("lifecycle", "result = owner.capture()", "result = owner.capture(); owner.capture()"),
    ("lifecycle", "result.stdout.decode(errors=\"replace\")", "owner._tails[0]"),
])
def test_adapter_protocol_and_ownership_evasions_are_rejected(which, old, new):
    execution_source, lifecycle_source = EXECUTION.read_text(), LIFECYCLE.read_text()
    text = execution_source if which == "execution" else lifecycle_source
    assert old in text
    changed = text.replace(old, new)
    assert adapter_proof(changed if which == "execution" else execution_source,
                         changed if which == "lifecycle" else lifecycle_source)


@pytest.mark.parametrize("addition", [
    "\ndef unsafe(argv):\n    renamed = subprocess.Popen\n    return renamed(argv, stdout=subprocess.PIPE)\n",
    "\ndef unsafe(child):\n    child.terminate()\n    child.wait()\n",
    "\nclass Override(OwnedProcess):\n    def shutdown(self): pass\n",
    "\ndef unsafe(error, result):\n    error.process_result = result\n",
])
def test_new_alias_and_coordinator_additions_are_rejected(addition):
    assert adapter_proof(EXECUTION.read_text() + addition, LIFECYCLE.read_text())


@pytest.mark.parametrize("old,new", [
    ("raised.__notes__ = []", "raised.__notes__ = list(error.__notes__)"),
    ("_render_close_failure(failure.error, secrets, tail_projector=tail_projector)", "str(failure.error)"),
    ('_process_cleanup_prefix(failure) + ": "', '"stream cleanup failed: "'),
    ('return f"{_FAILURE_SUBJECTS[failure.stream]} {_FAILURE_OPERATIONS[failure.phase]} failed"',
     "return 'deadline supervisor failed'"),
])
def test_raw_notes_and_guessed_legacy_labels_are_rejected(old, new):
    source = EXECUTION.read_text()
    assert old in source
    assert diagnostic_proof(ast.parse(source)) == []
    assert diagnostic_proof(ast.parse(source.replace(old, new)))


def test_exact_captured_return_contract_and_standalone_collector_boundary():
    assert tuple(inspect.signature(lifecycle.run_captured).parameters) == ("argv", "policy")
    assert get_type_hints(lifecycle.run_captured)["return"] == tuple[OwnedProcess | None, lifecycle.LifecycleOutcome]
    assert tuple(lifecycle.LifecycleOutcome.__dataclass_fields__) == ("return_code", "errors", "stdout", "stderr")
    assert "collect_streams" not in inspect.getsource(DockerRunExecutor.run_streaming)
    import docker.npm_environment as npm
    assert npm.collect_streams is streaming.collect_streams
    assert tuple(inspect.signature(streaming.collect_streams).parameters) == (
        "stdout_read", "stderr_read", "secrets", "sink", "on_reader_failure", "on_interruption",
        "stream_factory", "tail_projector", "abort_event", "network_url_display",
    )
    assert "docker.process" not in (ROOT / "docker/runtime_installer.py").read_text()


@pytest.mark.parametrize("code,detail,extra_error,expected_count", [
    (0, "", False, 0),
    (1, "No such container: named", False, 0),
    (1, "removal in progress", False, 1),
    (0, "", True, 1),
    (1, "No such container: named", True, 1),
])
def test_bounded_removal_ignores_owner_tuple_but_preserves_domain_policy(monkeypatch, code, detail, extra_error, expected_count):
    from unittest.mock import Mock

    owner = Mock(spec=OwnedProcess)
    existing = OSError("captured cleanup")
    outcome = lifecycle.LifecycleOutcome(code, [existing] if extra_error else [], "", detail)
    capture = Mock(return_value=(owner, outcome))
    monkeypatch.setattr(execution, "run_captured", capture)
    errors = execution._bounded_docker_rm("named", grace_seconds=.1)
    assert len(errors) == expected_count
    if extra_error:
        assert errors[0] is existing
    elif expected_count:
        assert "docker rm -f exited 1: removal in progress" in str(errors[0])
    assert owner.method_calls == [] and capture.call_count == 1
    assert capture.call_args.args == (("docker", "rm", "-f", "named"),)
    assert capture.call_args.kwargs["policy"].process_label == "docker rm -f client"


def test_assembler_guide_examples_execute_and_links_resolve():
    text = (ROOT / "docs/npm-environment-assembler.md").read_text()
    blocks = []
    current = None
    for line in text.splitlines():
        if line == "```python":
            current = []
        elif line == "```" and current is not None:
            blocks.append("\n".join(current))
            current = None
        elif current is not None:
            current.append(line)
    assert len(blocks) == 2
    assert "unittest.mock" not in text and "_DiagnosticCollector" not in text
    for block in blocks:
        subprocess.run([sys.executable, "-c", block], cwd=ROOT, check=True, timeout=10)
    for name in ("filesystem-lifecycle.md", "process-lifecycle.md"):
        assert f"]({name})" in text and (ROOT / "docs" / name).is_file()

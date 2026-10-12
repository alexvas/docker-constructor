"""Executable generic API, dependency, transfer and reader-barrier proofs."""
from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from docker.process import CleanupIncomplete, CleanupResult, OwnedProcess, ProcessResult
from tests.test_descriptor_close_convergence import (
    _close_violations_with_owners, _inline_ledger, _module_aliases,
)

ROOT = Path(__file__).resolve().parents[1]


def methods(source):
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "OwnedProcess")
    return {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}


def process_proof(source, foundation):
    """Prove actual transfer/release structure, not a class-name exemption.

    The bounded generic implementation uses one fixed escrow pair, one actual
    foundation state machine, and one explicit reader-stop predicate. Renamed
    fake owners/imports cannot satisfy the required dataflow/statement shapes.
    Integrated adapter/origin proofs remain the later 6B-D obligation.
    """
    errors = []
    fs = ast.parse(foundation)
    functions = {n.name: n for n in fs.body if isinstance(n, ast.FunctionDef)}
    prepare = functions.get("_prepare_stream")
    bind = functions.get("_bind_prepared")
    if prepare is None or bind is None:
        return ["no private prepared-transfer implementation"]
    bind_statements = bind.body[1:]  # docstring is not an operation
    if [ast.unparse(n) for n in bind_statements] != ["owner._stream = stream", "owner._state = _LIVE"]:
        errors.append("binding constructs/replaces/omits release authority")
    preparation = ast.unparse(prepare)
    if "object.__new__(OwnedStream)" not in preparation or "threading.Lock()" not in preparation:
        errors.append("not the shared preallocated stream state machine")
    m = methods(source)
    launch = m["launch"]
    text = ast.unparse(launch)
    required = ["_prepare_stream(label='stdout')", "_prepare_stream(label='stderr')",
                "self._continuation = _make_continuation(self._continue_cleanup)",
                "self._continuation.start()", "self._ready.wait(self._grace)",
                "self._proc = factory(", "_bind_prepared(self._owners[index], pipe)",
                "self._transfer(index)", "self._configure_reader(index)"]
    positions = [text.find(token) for token in required]
    if -1 in positions or positions != sorted(positions):
        errors.append("preparation/acquisition/escrow/use ordering")
    if "self._acquired[index] = True" not in text:
        errors.append("missing acquired-pipe ledger")
    if "self._ledger = setup" not in text or "setup.run(" not in text:
        errors.append("transfer attempts do not share failure precedence")
    calls = [n for n in ast.walk(launch) if isinstance(n, ast.Call)]
    if sum(isinstance(n.func, ast.Name) and n.func.id == "factory" for n in calls) != 1:
        errors.append("acquisition bypass/retry")
    safe = ast.unparse(m["_safe"])
    if "self._stopped[index].is_set()" not in safe or "not self._started[index]" not in safe:
        errors.append("no tracked reader-stop authority")
    release = ast.unparse(m["_release_safe"])
    owner_assignments = [ast.unparse(n.value) for n in ast.walk(m["_release_safe"]) if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "owner" for t in n.targets)]
    if owner_assignments != ["self._owners[index]"]:
        errors.append("slot owner overwritten or reconstructed")
    if "owner = self._owners[index]" not in release or "self._safe(index) or not callable(getattr(owner.stream, 'fileno', None))" not in release:
        errors.append("release lacks prepared-owner/reader barrier")
    complete = ast.unparse(m["_complete"])
    for token in ("self._reaped", "self._safe(i)", "self._owners[i].released"):
        if token not in complete:
            errors.append("false completed cleanup")
    continuation = ast.unparse(m["_continue_cleanup"])
    for token in ("self._reap(end)", "self._release_safe()", "self._publish(self._complete())", "while not self._finished.is_set()"):
        if token not in continuation:
            errors.append("lost continuation responsibility/outcome")
    # Only this mechanically verified slot-bound owner may invoke close. No
    # other close call in the new module receives a spelling-based exemption.
    tree = ast.parse(source)
    aliases = _module_aliases(tree)
    for function in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
        # Only the previously proved captured slot owner is a close authority;
        # annotation/class spelling does not grant authority to other functions.
        owners = {"owner"} if function.name in ("_release_safe", "release") else set()
        errors.extend(_close_violations_with_owners(function, owners, aliases))
    for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
        target = ast.unparse(call.func)
        if target.endswith(".close") and (target != "owner.close" or call.lineno not in {n.lineno for n in ast.walk(m["_release_safe"]) if isinstance(n, ast.Call)}):
            errors.append("raw/duplicate release")
        if target in ("os.close", "OwnedDescriptor") or "is_alive" in target:
            errors.append("raw-fileno owner/unsafe liveness inference")
    return errors


def test_prepared_transfer_and_safe_retention_proof():
    assert process_proof((ROOT / "docker/process.py").read_text(),
                         (ROOT / "docker/filesystem/streams.py").read_text()) == []


@pytest.mark.parametrize("old,new", [
    ("_bind_prepared(self._owners[index], pipe)", "pass"),
    ("self._acquired[index] = True", "pass"),
    ("self._stopped[index].is_set()", "True"),
    ("self._safe(index) or not callable", "True or not callable"),
    ("self._publish(self._complete())", "self._publish(True)"),
    ("while not self._finished.is_set():", "while False:"),
    ("self._ledger = setup", "pass"),
])
def test_new_module_evasions_fail_proof(old, new):
    source = (ROOT / "docker/process.py").read_text()
    assert old in source
    assert process_proof(source.replace(old, new), (ROOT / "docker/filesystem/streams.py").read_text())


def test_binding_replacement_constructor_is_rejected():
    source = (ROOT / "docker/process.py").read_text()
    foundation = (ROOT / "docker/filesystem/streams.py").read_text()
    foundation = foundation.replace("owner._stream = stream", "owner._stream = OwnedStream(stream, label='retry')")
    assert process_proof(source, foundation)


@pytest.mark.parametrize("source", [
    """from docker.filesystem.streams import OwnedStream
class OwnedProcess: pass
def f(pipe):
    pipe.close()
""",
    """import os as renamed
from docker.process import OwnedProcess
def f(pipe):
    close_alias = renamed.close
    close_alias(pipe.fileno())
""",
    """from docker.process import OwnedProcess
def f(pipe):
    owned = pipe
    owned.close()
""",
])
def test_owner_name_or_import_never_grants_close_authority(source):
    function, owners, aliases = _inline_ledger(source)
    assert _close_violations_with_owners(function, owners, aliases)


@pytest.mark.parametrize("addition", [
    "import os as other\ndef added(pipe):\n    alias = other.close\n    alias(pipe.fileno())\n",
    "def added(pipe):\n    pipe.close()\n",
    "def added(pipe):\n    owner = pipe\n    owner.close()\n",
])
def test_new_source_additions_cannot_hide_release(addition):
    source = (ROOT / "docker/process.py").read_text() + "\n" + addition
    assert process_proof(source, (ROOT / "docker/filesystem/streams.py").read_text())


def test_exact_public_api_and_no_existing_process_constructor():
    assert {n for n in vars(OwnedProcess) if not n.startswith("_")} == {
        "launch", "capture", "stream", "shutdown", "cleanup_status", "wait_cleanup", "returncode"}
    assert list(inspect.signature(OwnedProcess.launch).parameters) == [
        "argv", "grace_seconds", "deadline_seconds", "capture_limit", "factory", "popen_kwargs", "on_launched"]
    assert list(inspect.signature(OwnedProcess.shutdown).parameters) == ["self", "primary"]
    assert list(inspect.signature(OwnedProcess.wait_cleanup).parameters) == ["self", "timeout"]
    with pytest.raises(TypeError, match="launch"):
        OwnedProcess()
    assert set(CleanupResult.__dataclass_fields__) == {"complete", "error", "failures"}
    assert set(ProcessResult.__dataclass_fields__) == {"stdout", "stderr", "returncode", "timed_out", "cleanup", "callback_errors"}
    assert issubclass(CleanupIncomplete, OSError)


def test_isolated_process_import_has_no_domain_or_reverse_dependency():
    script = "import docker.process,sys,json; print(json.dumps(sorted(n for n in sys.modules if n.startswith('docker'))))"
    loaded = json.loads(subprocess.check_output([sys.executable, "-c", script], cwd=ROOT))
    assert set(loaded) == {"docker", "docker.process", "docker.filesystem", "docker.filesystem.cleanup", "docker.filesystem.streams"}
    for path in (ROOT / "docker/filesystem").glob("*.py"):
        tree = ast.parse(path.read_text())
        imported = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        imported += [alias.name for n in ast.walk(tree) if isinstance(n, ast.Import) for alias in n.names]
        assert not any(n and n.startswith(("docker.process", "docker.npm_environment", "docker.versioning", "docker.transactions", "subprocess")) for n in imported)
    tree = ast.parse((ROOT / "docker/process.py").read_text())
    imported = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert [n for n in imported if n and n.startswith("docker")] == ["docker.filesystem.cleanup", "docker.filesystem.streams"]


def test_executable_process_guide_and_links():
    guide = ROOT / "docs/process-lifecycle.md"
    text = guide.read_text()
    blocks = []
    collecting = False
    current = []
    for line in text.splitlines():
        if line == "```python":
            collecting, current = True, []
        elif line == "```" and collecting:
            blocks.append("\n".join(current))
            collecting = False
        elif collecting:
            current.append(line)
    assert len(blocks) == 3
    for block in blocks:
        subprocess.run([sys.executable, "-c", textwrap.dedent(block)], cwd=ROOT, check=True, timeout=10)
    for name in ("filesystem-lifecycle.md", "npm-environment-assembler.md"):
        assert "process-lifecycle.md" in (ROOT / "docs" / name).read_text()
    for token in ("shutdown incomplete", "never stops", "not cleaned", "Late failures", "Exclusions", "no pipes"):
        assert token in text

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

from docker.process import CleanupIncomplete, LifecycleResult, OwnedProcess, ProcessResult
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
    if "self._ledger.record(exc, phase=FailurePhase.SETUP, stream=tuple(ProcessStream)[index])" not in text:
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
    if "owner = self._owners[index]" not in release or "self._safe(index) or self._descriptorless[index] or (not callable(getattr(owner.stream, 'fileno', None)))" not in release:
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
    errors.extend(policy_proof(source))
    return errors


def policy_proof(source):
    """Bind the extension's actual coordination/dataflow and reject mutations."""
    errors = []
    m = methods(source)
    launch = ast.unparse(m["launch"])
    ordering = [launch.find(t) for t in (
        "_validate_policy(stop_policy, float(grace_seconds))",
        "self._continuation.start()", "stage.action.prepare()", "self._proc = factory(")]
    if -1 in ordering or ordering != sorted(ordering):
        errors.append("policy validation/readiness follows acquisition")
    shutdown = ast.unparse(m["_shutdown"])
    for token in ("if not self._requested:", "self._cleanup_end = time.monotonic() + self._grace",
                  "self._stop.set()", "not self._policy.drain",
                  "_outcome(self._ledger, self._status.complete)"):
        if token not in shutdown:
            errors.append("shared request/deadline/delivery contract")
    if not any(
        isinstance(block, ast.With)
        and any(ast.unparse(node) ==
                "self._status = _outcome(self._ledger, self._status.complete)"
                for node in block.body)
        and any(ast.unparse(node) == "result = self._status" for node in block.body)
        for block in m["_shutdown"].body
    ):
        errors.append("shutdown observation skips unconditional ledger refresh")
    cleanup = ast.unparse(m["_continue_cleanup"])
    if "ordinary=(), phase=FailurePhase.JOIN, stream=tuple(ProcessStream)[index]" not in cleanup:
        errors.append("reader join no longer uses unexpected-defect classification")
    run = ast.unparse(m["_run_policy"])
    ordering = [run.find(t) for t in ("stage_end = min(", "context = StopContext(", "action.start(context)")]
    if -1 in ordering or ordering != sorted(ordering):
        errors.append("action initiated before phase clock")
    for token in ("end - policy.local_cleanup_seconds", "self._delivered",
                  "self._reason == 'natural'", "stage.run_on_normal", "stage.local_stop_before",
                  "while time.monotonic() < stage_end:", "stage.wait_child",
                  "observation.failures", "self._cancel_action(index, end)",
                  "def start_action(action: ManagedAction=stage.action) -> None:",
                  "failures.run(start_action, ordinary=(OSError,))",
                  "returncode: int | None = None",
                  "returncode = self._poll_child()",
                  "self._action(observe_returncode, ordinary=(OSError,), phase=FailurePhase.WAIT)",
                  "self._action(observe_child, ordinary=(OSError,), phase=FailurePhase.WAIT)",
                  "if child_done is None or child_done:"):
        if token not in run:
            errors.append("stage reserve/transition/eligibility contract")
    for call in (n for n in ast.walk(m["_run_policy"]) if isinstance(n, ast.Call)):
        if isinstance(call.func, ast.Attribute) and call.func.attr in ("start", "prepare", "observe", "cancel"):
            if ast.unparse(call.func.value) in ("stage.action", "action") and [ast.unparse(a) for a in call.args] != ["context"]:
                errors.append("policy obtains raw main-child authority")
    cancel = ast.unparse(m["_cancel_action"])
    if cancel.find("self._action_cancelled[index] = True") > cancel.find("action.cancel(end)") or "not self._action_cancelled[index]" not in cancel:
        errors.append("action cleanup is retryable")
    complete = ast.unparse(m["_complete"])
    if "result.done and result.cleanup_complete" not in complete:
        errors.append("pending action resources falsely complete")
    continuation = ast.unparse(m["_continue_cleanup"])
    if "self._observe_actions()" not in continuation or continuation.find("self._run_policy(end)") > continuation.find("self._cancel_readers_locked()"):
        errors.append("lost action retention or drain barrier")
    reader = ast.unparse(m["_read"])
    if "while not self._cancel.is_set() or (first and (not self._delivered)):" not in reader or "if self._stop.is_set():" not in reader:
        errors.append("drain depends on consumer progress")
    triggers = {"_reader": "reason='reader'", "_continue_cleanup": "reason='deadline'",
                "launch": "reason='setup'", "shutdown": "reason='cancel'", "__exit__": "self._shutdown(",
                "_collect": "reason='natural'"}
    for method, token in triggers.items():
        if token not in ast.unparse(m[method]):
            errors.append("shutdown trigger bypasses launch policy")
    tree = ast.parse(source)
    claim = ast.unparse(m["_claim_collection"])
    if "self._collection_claim.acquire(blocking=False)" not in claim or "raise RuntimeError" not in claim:
        errors.append("collection claim queues or permits a second subscriber")
    stream = ast.unparse(m["stream"])
    if stream.find("if not callable(callback)") > stream.find("self._claim_collection()"):
        errors.append("invalid callbacks consume the collection claim")
    if "if self._collection_delivered:" not in ast.unparse(m["__exit__"]):
        errors.append("context suppresses undelivered active operation authority")
    contexts = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and ast.unparse(n.func) == "StopContext"]
    if len(contexts) != 1 or [ast.unparse(a) for a in contexts[0].args] != ["self._reason", "returncode", "stage_end"]:
        errors.append("context grants ownership or fresh budgets")
    if "self._proc.wait(timeout=0)" not in ast.unparse(m["_poll_child"]):
        errors.append("wait-only observation can block or bypass the coordinator")
    control_locations = {n.lineno for name in ("_wait_child", "_reap", "_terminate_if_live", "_poll_child")
                         for n in ast.walk(m[name]) if isinstance(n, ast.Attribute)
                         and ast.unparse(n.value) == "self._proc" and n.attr in ("wait", "kill", "terminate")}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and any(ast.unparse(base).endswith("OwnedProcess") for base in node.bases):
            errors.append("policy replaces ownership through inheritance")
        if isinstance(node, ast.FunctionDef) and node.name == "shutdown" and node.lineno != m["shutdown"].lineno:
            errors.append("second shutdown coordinator")
        if isinstance(node, ast.Attribute) and node.attr in ("terminate", "kill", "wait"):
            # Resolve the release/control attribute even when assigned to an
            # arbitrarily named callable rather than directly invoked.
            receiver = ast.unparse(node.value)
            events = {"self._ready", "self._request", "self._finished", "self._phase_done", "self._stopped[index]", "self._cancel", "self._setup_done"}
            if not ((receiver == "self._proc" and node.lineno in control_locations) or (node.attr == "wait" and receiver in events)):
                errors.append("independent raw-child control")
    return errors


@pytest.mark.parametrize("old,new", [
    ("_validate_policy(stop_policy, float(grace_seconds))", "pass"),
    ("stage.action.prepare()", "None"),
    ("end - policy.local_cleanup_seconds", "end + self._grace"),
    ("StopContext(self._reason, returncode, stage_end)", "StopContext(self._reason, self._proc, time.monotonic() + self._grace)"),
    ("self._action(observe_returncode, ordinary=(OSError,), phase=FailurePhase.WAIT)", "observe_returncode()"),
    ("returncode: int | None = None", "returncode = self._proc"),
    ("returncode = self._poll_child()", "returncode = self._proc"),
    ("action.start(context)", "action.start(self._proc)"),
    ("action: ManagedAction = stage.action", "action: ManagedAction = self._proc"),
    ("stage.wait_child", "False"),
    ("if self._delivered:", "if False:"),
    ("result.done and result.cleanup_complete", "True"),
    ("not self._action_cancelled[index]", "True"),
    ("if self._stop.is_set():", "if False:"),
    ("reason=\"reader\"", "reason=\"unused\""),
    ("reason=\"deadline\"", "reason=\"unused\""),
    ("self._proc.wait(timeout=0)", "self._proc.wait()"),
    ("self._collection_claim.acquire(blocking=False)", "self._collection_claim.acquire()"),
    ("if self._collection_delivered:", "if self._collected:"),
    ("ordinary=(), phase=FailurePhase.JOIN, stream=tuple(ProcessStream)[index]",
     "ordinary=(OSError,), phase=FailurePhase.JOIN, stream=tuple(ProcessStream)[index]"),
])
def test_stop_policy_ownership_and_budget_evasions_are_rejected(old, new):
    source = (ROOT / "docker/process.py").read_text()
    assert old in source
    assert process_proof(source.replace(old, new), (ROOT / "docker/filesystem/streams.py").read_text())


def test_inheritance_and_second_coordinator_are_rejected():
    source = (ROOT / "docker/process.py").read_text()
    for addition in ("class Policy(OwnedProcess):\n    def shutdown(self): pass\n",
                     "def shutdown(child):\n    child.kill()\n    child.wait()\n",
                     "def added(child):\n    renamed = child.terminate\n    renamed()\n",
                     "def added(self):\n    renamed = self._proc.wait\n    renamed()\n"):
        assert process_proof(source + "\n" + addition, (ROOT / "docker/filesystem/streams.py").read_text())


def test_prepared_transfer_and_safe_retention_proof():
    assert process_proof((ROOT / "docker/process.py").read_text(),
                         (ROOT / "docker/filesystem/streams.py").read_text()) == []


@pytest.mark.parametrize("old,new", [
    ("_bind_prepared(self._owners[index], pipe)", "pass"),
    ("self._acquired[index] = True", "pass"),
    ("self._stopped[index].is_set()", "True"),
    ("self._safe(index) or self._descriptorless[index] or not callable", "True or not callable"),
    ("self._publish(self._complete())", "self._publish(True)"),
    ("while not self._finished.is_set():", "while False:"),
    ("            self._status = _outcome(self._ledger, self._status.complete)\n            if deliver:",
     "            if not self._delivered:\n                self._status = _outcome(self._ledger, self._status.complete)\n            if deliver:"),
    ("self._ledger.record(exc, phase=FailurePhase.SETUP, stream=tuple(ProcessStream)[index])", "pass"),
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
        "argv", "grace_seconds", "deadline_seconds", "capture_limit", "factory", "popen_kwargs", "on_launched", "stop_policy", "reporter"]
    assert list(inspect.signature(OwnedProcess.shutdown).parameters) == ["self", "primary"]
    assert list(inspect.signature(OwnedProcess.wait_cleanup).parameters) == ["self", "timeout"]
    with pytest.raises(TypeError, match="launch"):
        OwnedProcess()
    assert set(LifecycleResult.__dataclass_fields__) == {"complete", "failures"}
    assert set(ProcessResult.__dataclass_fields__) == {"stdout", "stderr", "returncode", "timed_out", "lifecycle", "stream_outcomes"}
    assert LifecycleResult.__dataclass_fields__["failures"].default == ()
    assert issubclass(CleanupIncomplete, OSError)
    import docker.process as api
    assert set(api.__all__) == {"OwnedProcess", "ProcessResult", "LifecycleResult", "CleanupIncomplete",
                               "ProcessStream", "StreamOutcome", "FailureInfo", "FailureKind", "FailurePhase",
                               "StopPolicy", "StopStage", "StopContext", "ManagedAction", "ActionResult"}
    assert not hasattr(api, "CleanupResult")
    assert set(api.StopContext.__dataclass_fields__) == {"reason", "returncode", "deadline"}
    assert set(api.ActionResult.__dataclass_fields__) == {"done", "cleanup_complete", "failures"}
    assert set(api.StopPolicy.__dataclass_fields__) == {"stages", "local_cleanup_seconds", "drain"}
    assert set(api.StopStage.__dataclass_fields__) == {"seconds", "action", "wait_child", "run_on_normal", "local_stop_before"}
    assert {n for n in vars(api.ManagedAction) if not n.startswith("_")} == {"prepare", "start", "observe", "cancel"}


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
    assert len(blocks) == 5
    for block in blocks:
        subprocess.run([sys.executable, "-c", textwrap.dedent(block)], cwd=ROOT, check=True, timeout=10)
    for name in ("filesystem-lifecycle.md", "npm-environment-assembler.md"):
        assert "process-lifecycle.md" in (ROOT / "docs" / name).read_text()
    for token in ("An incomplete result", "never stops", "live reader", "Late failures", "Exclusions", "no pipes"):
        assert token in text

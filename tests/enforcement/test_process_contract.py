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
from tests.enforcement.source_layout import replace_snippet
from tests.enforcement.test_descriptor_close_convergence import (
    _close_violations_with_owners, _inline_ledger, _module_aliases,
)

ROOT = Path(__file__).resolve().parents[2]

_DEADLINE_GUARD = (
    "                self._request.set()\n"
    "        if threading.current_thread() is self._continuation or "
    "threading.current_thread() in self._readers:\n"
    "            return self.cleanup_status"
)
_DEADLINE_UNGUARDED = (
    "                self._request.set()\n"
    "        {assignment}\n"
    "        if threading.current_thread() is self._continuation or "
    "threading.current_thread() in self._readers:\n"
    "            return self.cleanup_status"
)
_COMPLETE_RETURN = (
    "return self._reaped and all(self._safe(i) for i in range(2)) and all(\n"
    "            not self._acquired[i] or self._owners[i].released for i in range(2)) and all(\n"
    "            not prepared or (result.done and result.cleanup_complete)\n"
    "            for prepared, result in zip(self._action_prepared, self._action_results))"
)


def _flip_nth(text: str, needle: str, replacement: str, occurrence: int) -> str:
    index = -1
    for _ in range(occurrence):
        index = text.index(needle, index + 1)
    return text[:index] + replacement + text[index + len(needle):]


_COMPLETE_OR_FIRST = _flip_nth(_COMPLETE_RETURN, " and ", " or ", 1)
_COMPLETE_OR_SECOND = _flip_nth(_COMPLETE_RETURN, " and ", " or ", 2)
_COMPLETE_OR_THIRD = _flip_nth(_COMPLETE_RETURN, " and ", " or ", 3)
_COMPLETE_DECOY = "return True\n        " + _COMPLETE_RETURN


def methods(source):
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "OwnedProcess")
    return {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}


def _body_matches(method, expected):
    """Compare executed statement structure, ignoring formatting and locations."""
    actual = ast.Module(body=method.body, type_ignores=[])
    return ast.dump(actual, include_attributes=False) == ast.dump(
        ast.parse(textwrap.dedent(expected)), include_attributes=False)


def _complete_predicate_matches(method):
    return _body_matches(method, """\
        return self._reaped and all(self._safe(i) for i in range(2)) and all(
            not self._acquired[i] or self._owners[i].released for i in range(2)) and all(
            not prepared or (result.done and result.cleanup_complete)
            for prepared, result in zip(self._action_prepared, self._action_results))
    """)


def process_proof(source, foundation):
    """Prove actual transfer/release structure, not a class-name exemption.

    The bounded generic implementation uses one fixed escrow pair, one actual
    foundation state machine, and one explicit reader-stop predicate. Renamed
    fake owners/imports cannot satisfy the required dataflow/statement shapes.
    Integrated adapter/origin proofs compose this gate in
    tests/enforcement/process_convergence.py; no spelling grants release authority.
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
    # Prove the actual sole return, including polarity, operator and index.
    # Tokens in an inverted predicate or an unreachable decoy grant no authority.
    if not _body_matches(m["_safe"],
                         "return not self._started[index] or self._stopped[index].is_set()"):
        errors.append("no tracked reader-stop authority")
    owner_assignments = [ast.unparse(n.value) for n in ast.walk(m["_release_safe"]) if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "owner" for t in n.targets)]
    if owner_assignments != ["self._owners[index]"]:
        errors.append("slot owner overwritten or reconstructed")
    # Bind the actual close to its positive barrier, acquired/live owner guard,
    # captured slot/index and protected action. A decoy condition is not proof.
    if not _body_matches(m["_release_safe"], """\
        for index in range(2):
            owner = self._owners[index]
            if self._acquired[index] and not owner.released:
                def release(index: int = index, owner: OwnedStream = owner) -> None:
                    if self._safe(index) or self._descriptorless[index] or not callable(getattr(owner.stream, "fileno", None)):
                        owner.close()
                self._action(release, ordinary=(OSError,), phase=FailurePhase.RELEASE, stream=tuple(ProcessStream)[index])
    """):
        errors.append("release lacks prepared-owner/reader barrier")
    if not _complete_predicate_matches(m["_complete"]):
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


def _request_deadline_matches(method):
    block = method.body[0]
    if not isinstance(block, ast.With) or len(block.items) != 1 or ast.unparse(block.items[0].context_expr) != "self._lock":
        return False
    if not _body_matches(block, """\
        previously_delivered = self._delivered
        if primary is not None:
            self._ledger.primary(primary)
        if not self._requested:
            self._requested = True
            self._reason = reason
            self._cleanup_end = time.monotonic() + self._grace
            self._shutdown_end = self._cleanup_end + .05
            self._stop.set()
            if self._policy is None or not self._policy.drain:
                self._cancel_readers_locked()
            self._request.set()
    """):
        return False
    # There must be no second deadline write or request reset anywhere in the
    # method, including after the guarded block or in a nested helper.
    managed = {"self._requested", "self._cleanup_end", "self._shutdown_end"}
    writes = {node for node in ast.walk(method) if isinstance(node, ast.Attribute)
              and isinstance(node.ctx, ast.Store) and ast.unparse(node) in managed}
    guarded = {node for node in ast.walk(block.body[-1]) if isinstance(node, ast.Attribute)
               and isinstance(node.ctx, ast.Store) and ast.unparse(node) in managed}
    return len(writes) == 3 and writes == guarded


def _policy_wait_matches(method):
    stages = [node for node in method.body if isinstance(node, ast.For)]
    if len(stages) != 1:
        return False
    waits = [node for node in stages[0].body if isinstance(node, ast.While)]
    if len(waits) != 1 or waits[0].orelse or ast.unparse(waits[0].test) != "time.monotonic() < stage_end":
        return False
    # Only a done observation without configured child waiting may bypass the
    # protected child observation; a decoy positive condition is insufficient.
    return _body_matches(waits[0], """\
        observation = self._observe_action(index) if stage.action else ActionResult(True, True)
        if observation.failures:
            break
        if observation.done:
            if not stage.wait_child:
                break
            child_done: bool | None = None
            def observe_child() -> None:
                nonlocal child_done
                child_done = self._poll_child() is not None
            self._action(observe_child, ordinary=(OSError,), phase=FailurePhase.WAIT)
            if child_done is None or child_done:
                break
        _policy_pause(min(.005, max(0, stage_end - time.monotonic())))
    """)


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
    if not _request_deadline_matches(m["_shutdown"]):
        errors.append("one-time request/deadline guard")
    if not _policy_wait_matches(m["_run_policy"]):
        errors.append("configured child-wait control flow")
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
    if not _complete_predicate_matches(m["_complete"]):
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


@pytest.mark.parametrize("old,new,error", [
    ("if not stage.wait_child:", "if stage.wait_child:", "configured child-wait control flow"),
    ("if not stage.wait_child:\n                        break",
     "if not stage.wait_child:\n                        pass\n                    break", "configured child-wait control flow"),
    (_DEADLINE_GUARD,
     _DEADLINE_UNGUARDED.format(assignment="self._cleanup_end = time.monotonic() + self._grace"),
     "one-time request/deadline guard"),
    (_DEADLINE_GUARD,
     _DEADLINE_UNGUARDED.format(assignment="self._shutdown_end = time.monotonic() + self._grace"),
     "one-time request/deadline guard"),
    ("                self._requested = True", "                self._requested = False",
     "one-time request/deadline guard"),
    (_DEADLINE_GUARD,
     _DEADLINE_UNGUARDED.format(assignment="self._requested = False"),
     "one-time request/deadline guard"),
])
def test_policy_wait_and_one_time_deadline_ast_semantics(old, new, error):
    source = (ROOT / "docker/process.py").read_text()
    mutated = replace_snippet(source, old, new, unique=True)
    assert error in policy_proof(mutated)


@pytest.mark.parametrize("old,new", [
    ("_validate_policy(stop_policy, float(grace_seconds))", "pass"),
    ("stage.action.prepare()", "None"),
    ("end - policy.local_cleanup_seconds", "end + self._grace"),
    ("StopContext(self._reason, returncode, stage_end)", "StopContext(self._reason, self._proc, time.monotonic() + self._grace)"),
    ("self._action(observe_returncode, ordinary=(OSError,), phase=FailurePhase.WAIT)", "observe_returncode()"),
    ("returncode: int | None = None", "returncode = self._proc"),
    ("returncode = self._poll_child()", "returncode = self._proc"),
    ("action.start(context)", "action.start(self._proc)"),
    ("def start_action(action: ManagedAction = stage.action) -> None:\n                    context = StopContext(self._reason, returncode, stage_end)\n                    if action.start(context) is not None:\n                        raise TypeError(\"action start must return None\")",
     "def start_action(action: ManagedAction = self._proc) -> None:\n                    context = StopContext(self._reason, returncode, stage_end)\n                    if action.start(context) is not None:\n                        raise TypeError(\"action start must return None\")"),
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
    mutated = replace_snippet(source, old, new, unique=False)
    assert process_proof(mutated, (ROOT / "docker/filesystem/streams.py").read_text())


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
    ("self._safe(index) or self._descriptorless[index] or not callable(getattr(owner.stream, \"fileno\", None))",
     "True or not callable(getattr(owner.stream, \"fileno\", None))"),
    ("self._publish(self._complete())", "self._publish(True)"),
    ("while not self._finished.is_set():", "while False:"),
    ("            self._status = _outcome(self._ledger, self._status.complete)\n            if deliver:\n                self._delivered = True",
     "            if not self._delivered:\n                self._status = _outcome(self._ledger, self._status.complete)\n            if deliver:\n                self._delivered = True"),
    ("self._ledger.record(exc, phase=FailurePhase.SETUP, stream=tuple(ProcessStream)[index])", "pass"),
])
def test_new_module_evasions_fail_proof(old, new):
    source = (ROOT / "docker/process.py").read_text()
    mutated = replace_snippet(source, old, new, unique=False)
    assert process_proof(mutated, (ROOT / "docker/filesystem/streams.py").read_text())


@pytest.mark.parametrize("replacement", [
    "return not self._started[index] or not self._stopped[index].is_set()",
    "return not self._started[index] and self._stopped[index].is_set()",
    "return not self._started[index] or self._stopped[0].is_set()",
    "return True\n        return not self._started[index] or self._stopped[index].is_set()",
    "not self._started[index] or self._stopped[index].is_set()\n        return True",
])
def test_reader_barrier_predicate_ast_semantics(replacement):
    source = (ROOT / "docker/process.py").read_text()
    original = "return not self._started[index] or self._stopped[index].is_set()"
    errors = process_proof(replace_snippet(source, original, replacement, unique=True),
                           (ROOT / "docker/filesystem/streams.py").read_text())
    assert "no tracked reader-stop authority" in errors


@pytest.mark.parametrize("old,new,error", [
    ('if self._safe(index) or self._descriptorless[index] or not callable(getattr(owner.stream, "fileno", None)):',
     'if not (self._safe(index) or self._descriptorless[index] or not callable(getattr(owner.stream, "fileno", None))):',
     "release lacks prepared-owner/reader barrier"),
    ('if self._safe(index) or self._descriptorless[index] or not callable(getattr(owner.stream, "fileno", None)):\n                        owner.close()',
     'if self._safe(index) or self._descriptorless[index] or not callable(getattr(owner.stream, "fileno", None)):\n                        pass\n                    owner.close()',
     "release lacks prepared-owner/reader barrier"),
    ('if self._safe(index) or self._descriptorless[index] or not callable(getattr(owner.stream, "fileno", None)):\n                        owner.close()',
     'if self._safe(index) or self._descriptorless[index] or not callable(getattr(owner.stream, "fileno", None)):\n                        pass\n                    else:\n                        owner.close()',
     "release lacks prepared-owner/reader barrier"),
    ("if self._acquired[index] and not owner.released:",
     "if self._acquired[index] or not owner.released:", "release lacks prepared-owner/reader barrier"),
    ("(not self._acquired[i] or self._owners[i].released for i in range(2))",
     "(not self._acquired[i] or self._owners[i].released for i in range(0))", "false completed cleanup"),
    (_COMPLETE_RETURN, _COMPLETE_OR_FIRST, "false completed cleanup"),
    (_COMPLETE_RETURN, _COMPLETE_OR_SECOND, "false completed cleanup"),
    (_COMPLETE_RETURN, _COMPLETE_OR_THIRD, "false completed cleanup"),
    ("not prepared or (result.done and result.cleanup_complete)",
     "not prepared or (result.done or result.cleanup_complete)", "false completed cleanup"),
    (_COMPLETE_RETURN, _COMPLETE_DECOY, "false completed cleanup"),
])
def test_release_and_completion_ast_semantics(old, new, error):
    source = (ROOT / "docker/process.py").read_text()
    errors = process_proof(replace_snippet(source, old, new, unique=True),
                           (ROOT / "docker/filesystem/streams.py").read_text())
    assert error in errors, errors


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

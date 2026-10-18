"""6BD complete-tree process origin, prepared ownership and policy gates."""

import pytest

from tests.enforcement.process_convergence import (
    architecture_report,
    canonical_architecture_report,
    convergence_report,
)
from tests.enforcement.source_layout import insert_before, replace_snippet
from tests.enforcement.test_filesystem_convergence import _sources
from tests.enforcement.test_process_contract import (
    _COMPLETE_OR_FIRST,
    _COMPLETE_RETURN,
    _DEADLINE_GUARD,
    _DEADLINE_UNGUARDED,
)

_PREPEND_OWNERS = "self._owners = []"


@pytest.fixture(scope="module")
def sources():
    return _sources()


def test_complete_tree_process_ownership(sources):
    report = convergence_report(sources)
    assert report["errors"] == []
    assert {row["kind"] for row in report["ledger"]} >= {
        "prepared-child",
        "owned-launch",
        "managed-helper",
        "stdlib-run",
        "prepared-stream",
        "terminal-release",
        "retained-continuation",
    }
    assert all(row["path"] in sources and row["line"] > 0 for row in report["ledger"])


@pytest.mark.parametrize(
    "addition",
    [
        "import subprocess as sp\nfrom subprocess import PIPE as P\ndef f(argv):\n    factory = sp.Popen\n    child = factory(argv, stdout=P, stderr=P)\n    alias = child.stdout.close\n    alias()\n",
        "from subprocess import Popen as spawn, PIPE as channel\ndef f(argv):\n    child = spawn(argv, stdout=channel, stderr=channel)\n    stream = child.stderr\n    closer = stream.close\n    closer()\n",
        "from docker.filesystem.streams import OwnedStream\ndef f(child):\n    class OwnedStream:\n        def close(self): child.stdout.close()\n    owner = OwnedStream()\n    owner.close()\n",
        "from docker.filesystem.descriptors import OwnedDescriptor as D\ndef f(child, ops):\n    raw = child.stdout.fileno()\n    first = D(ops, raw, label='duplicate')\n",
        "from docker.filesystem.streams import OwnedStream as S\ndef cleanup(child):\n    owner = S(child.stdout, label='stdout')\n    owner.close()\ndef other_cleanup(child):\n    S(child.stdout, label='stdout').close()\n",
        "from docker.process import OwnedProcess as P\nclass Replacement(P):\n    def shutdown(self): pass\n",
        "from docker.process import OwnedProcess as P\ndef f(argv):\n    owner = P.launch(argv)\n    child = owner._proc\n    stop = child.kill\n    stop()\n",
        "from docker.process import OwnedProcess as P\ndef f(argv):\n    owner = P.launch(argv)\n    borrowed = owner._owners[0].stream\n    release = borrowed.close\n    release()\n",
    ],
)
def test_new_modules_and_aliases_grant_no_process_authority(sources, addition):
    changed = {**sources, "docker/new_process_consumer.py": addition}
    assert convergence_report(changed)["errors"]


@pytest.mark.parametrize("factory", ["P.launch", "acquire"])
@pytest.mark.parametrize(
    "operation",
    [
        "owner._proc.kill()",
        "owner._proc.wait()",
        "child = owner._proc\n        control = child.kill\n        control()",
        "child = owner._proc\n        control = child.wait\n        control()",
    ],
)
def test_context_managed_owners_grant_no_raw_child_control(sources, factory, operation):
    addition = (
        "from docker.process import OwnedProcess as P\n"
        "def f(argv):\n"
        "    acquire = P.launch\n"
        f"    with {factory}(argv) as owner:\n"
        f"        {operation}\n"
    )
    errors = convergence_report(
        {**sources, "docker/context_process_consumer.py": addition}
    )["errors"]
    assert any("borrowed private process authority" in error for error in errors), (
        errors
    )
    assert any("independent raw-child coordination" in error for error in errors), (
        errors
    )


@pytest.mark.parametrize(
    "call,error",
    [
        (
            "OwnedStream(stream=child.stdout, label='stdout')",
            "consumer/reset-per-cleanup pipe adoption",
        ),
        (
            "S(stream=child.stderr, label='stderr')",
            "consumer/reset-per-cleanup pipe adoption",
        ),
        (
            "OwnedDescriptor(ops=ops, fd=child.stdout.fileno(), label='duplicate')",
            "duplicate raw-fileno ownership",
        ),
        ("D(ops=ops, fd=raw, label='duplicate')", "duplicate raw-fileno ownership"),
        (
            "DirectoryDescriptor.adopt(ops=ops, fd=raw, label='duplicate')",
            "duplicate raw-fileno ownership",
        ),
        ("release(fd=raw)", "raw-fileno release"),
    ],
)
def test_keyword_arguments_cannot_bypass_pipe_ownership(sources, call, error):
    addition = (
        "from docker.filesystem.streams import OwnedStream, OwnedStream as S\n"
        "from docker.filesystem.descriptors import OwnedDescriptor, OwnedDescriptor as D, DirectoryDescriptor\n"
        "from os import close as release\n"
        "def f(child, ops):\n"
        "    raw = child.stderr.fileno()\n"
        f"    {call}\n"
    )
    errors = convergence_report(
        {**sources, "docker/keyword_process_consumer.py": addition}
    )["errors"]
    assert any(error in found for found in errors), errors


@pytest.mark.parametrize(
    "path,old,new",
    [
        ("docker/process.py", "if math.fsum(caps) > total:", "if False:"),
        (
            "docker/process.py",
            "time.monotonic() + stage.seconds, end - policy.local_cleanup_seconds",
            "time.monotonic() + self._grace, end - policy.local_cleanup_seconds",
        ),
        (
            "docker/process.py",
            "self._action_prepared[index] = True\n                        self._action_results[index] = ActionResult(False, False)",
            "self._action_prepared[index] = False\n                        self._action_results[index] = ActionResult(True, True)",
        ),
        (
            "docker/process.py",
            "self._acquired[index] = True",
            "self._acquired[index] = False",
        ),
        (
            "docker/process.py",
            "return not self._started[index] or self._stopped[index].is_set()",
            "return not self._started[index] or not self._stopped[index].is_set()",
        ),
        (
            "docker/process.py",
            "owner = self._owners[index]",
            "owner = OwnedStream(self._owners[index].stream, label='reset')",
        ),
        (
            "docker/process.py",
            'self._safe(index) or self._descriptorless[index] or not callable(getattr(owner.stream, "fileno", None))',
            'True or not callable(getattr(owner.stream, "fileno", None))',
        ),
        (
            "docker/process.py",
            'if self._safe(index) or self._descriptorless[index] or not callable(getattr(owner.stream, "fileno", None)):',
            'if not (self._safe(index) or self._descriptorless[index] or not callable(getattr(owner.stream, "fileno", None))):',
        ),
        ("docker/process.py", _COMPLETE_RETURN, _COMPLETE_OR_FIRST),
        ("docker/process.py", "while not self._finished.is_set():", "while False:"),
        ("docker/process.py", "self._publish(self._complete())", "self._publish(True)"),
        ("docker/process.py", "if not stage.wait_child:", "if True:"),
        ("docker/process.py", "if not stage.wait_child:", "if stage.wait_child:"),
        (
            "docker/process.py",
            _DEADLINE_GUARD,
            _DEADLINE_UNGUARDED.format(
                assignment="self._cleanup_end = time.monotonic() + self._grace"
            ),
        ),
        (
            "docker/process.py",
            "context = StopContext(self._reason, returncode, stage_end)",
            "context = StopContext(self._reason, returncode, time.monotonic() + self._grace)",
        ),
        ("docker/process.py", "result.done and result.cleanup_complete", "result.done"),
        (
            "docker/process.py",
            "self._status = _outcome(self._ledger, complete)",
            "self._status = LifecycleResult(complete)",
        ),
        (
            "docker/npm_environment/execution.py",
            "complete = complete and status.complete",
            "complete = True",
        ),
        (
            "docker/npm_environment/execution.py",
            "min(self.grace, remaining / 3)",
            "self.grace",
        ),
        (
            "docker/npm_environment/execution.py",
            "self.owner = operation.owner",
            "self.owner = None",
        ),
        ("docker/npm_environment/execution.py", "self.done.set()", "pass"),
        (
            "docker/npm_environment/lifecycle.py",
            "return _CapturedOperation(owner, outcome, failures)",
            "return _CapturedOperation(None, outcome, failures)",
        ),
    ],
)
def test_integrated_transfer_budget_and_retention_mutations(sources, path, old, new):
    changed = {**sources, path: replace_snippet(sources[path], old, new, unique=False)}
    assert convergence_report(changed)["errors"]


def test_integrated_owner_reset_before_completion_is_rejected(sources):
    path = "docker/process.py"
    text = insert_before(
        sources[path],
        "while not self._finished.is_set():",
        _PREPEND_OWNERS,
        unique=True,
    )
    assert convergence_report({**sources, path: text})["errors"]


@pytest.mark.parametrize(
    "path,addition",
    [
        ("docker/process.py", "\nimport docker.npm_environment.execution\n"),
        ("docker/filesystem/streams.py", "\nfrom docker import process\n"),
        ("docker/filesystem/operations.py", "\nfrom ..process import StopPolicy\n"),
        (
            "docker/filesystem/streams.py",
            "\nimport importlib as loader\nloader.import_module('docker.process')\n",
        ),
        ("docker/runtime_installer.py", "\nimport docker.process\n"),
    ],
)
def test_process_dependency_dag_mutations(sources, path, addition):
    assert convergence_report({**sources, path: sources[path] + addition})["errors"]


@pytest.mark.parametrize(
    "call",
    [
        "loader.import_module('..process', 'docker.filesystem')",
        "loader.import_module('..process', package='docker.filesystem')",
        "loader.import_module(name='..process', package='docker.filesystem')",
        "load('..process', 'docker.filesystem')",
        "load('..process', package='docker.filesystem')",
    ],
)
def test_relative_dynamic_reverse_imports_are_rejected_and_inventoried(sources, call):
    path = "docker/filesystem/streams.py"
    addition = (
        "\nimport importlib as loader\nload = loader.import_module\n" + call + "\n"
    )
    report = architecture_report({**sources, path: sources[path] + addition})
    assert any(
        "reverse dependency: docker/filesystem/streams.py: docker.process" in error
        for error in report["errors"]
    ), report["errors"]
    assert ["docker.filesystem.streams", "docker.process"] in report["dependency_edges"]


@pytest.mark.parametrize(
    "call",
    [
        "loader.import_module('.process', 'docker')",
        "loader.import_module('.process', package='docker')",
        "loader.import_module('..process', 'docker.filesystem')",
        "loader.import_module('..process', package='docker.filesystem')",
        "loader.import_module('docker.process')",
        "__import__('docker.process')",
    ],
)
def test_permitted_dynamic_imports_are_resolved_and_inventoried(sources, call):
    path = "docker/new_process_consumer.py"
    report = architecture_report(
        {**sources, path: "import importlib as loader\n" + call + "\n"}
    )
    assert report["errors"] == []
    assert ["docker.new_process_consumer", "docker.process"] in report[
        "dependency_edges"
    ]


@pytest.mark.parametrize(
    "old,new",
    [
        (
            "self._state = _RELEASED\n            stream = self._stream",
            "stream = self._stream",
        ),
        (
            "        with self._lock:\n            if self._state is not _LIVE:\n                return\n            self._state = _RELEASED\n            stream = self._stream",
            "        if True:\n            if self._state is not _LIVE:\n                return\n            self._state = _RELEASED\n            stream = self._stream",
        ),
        (
            "owner._stream = stream",
            "owner._stream = OwnedStream(stream, label='replacement')",
        ),
    ],
)
def test_foundation_transfer_and_synchronized_release_remain_required(
    sources, old, new
):
    path = "docker/filesystem/streams.py"
    changed = {**sources, path: replace_snippet(sources[path], old, new, unique=True)}
    assert convergence_report(changed)["errors"]


def test_generated_architecture_and_runtime_packaging_boundary(sources):
    from pathlib import Path
    from typing import get_type_hints
    from docker.process import OwnedProcess
    from docker.npm_environment.lifecycle import LifecycleOutcome, run_captured

    import json

    report = canonical_architecture_report(sources)
    assert report["errors"] == []
    root = Path(__file__).resolve().parents[2]
    inventory = root / "tests/fixtures" / "process-architecture-inventory.json"
    assert json.loads(inventory.read_text()) == report, (
        "regenerate the canonical architecture/pipe inventory after production changes"
    )
    assert set(report["source_ast_sha256"]) == set(sources)
    edges = {tuple(edge) for edge in report["dependency_edges"]}
    assert edges >= {
        ("docker.npm_environment.execution", "docker.process"),
        ("docker.npm_environment.lifecycle", "docker.process"),
        ("docker.process", "docker.filesystem.streams"),
        ("docker.process", "docker.filesystem.cleanup"),
    }
    assert not any(
        a.startswith("docker.filesystem") and b == "docker.process" for a, b in edges
    )
    assert not any(
        a == "docker.process"
        and b.startswith(
            ("docker.npm_environment", "docker.versioning", "docker.transactions")
        )
        for a, b in edges
    )
    assert not any(
        a == "docker.runtime_installer" and b == "docker.process" for a, b in edges
    )
    assert report["domain_fields"]["LifecycleOutcome"] == [
        "return_code",
        "errors",
        "stdout",
        "stderr",
    ]
    assert (
        get_type_hints(run_captured)["return"]
        == tuple[OwnedProcess | None, LifecycleOutcome]
    )
    assert report["process_surface"]["StopContext"]["fields"] == [
        "reason",
        "returncode",
        "deadline",
    ]
    assert report["process_surface"]["ActionResult"]["fields"] == [
        "done",
        "cleanup_complete",
        "failures",
    ]
    dockerfile = (Path(__file__).resolve().parents[2] / "Dockerfile").read_text()
    assert (
        "COPY docker/filesystem/ /usr/local/lib/pi-cli/docker/filesystem/" in dockerfile
    )
    assert "COPY docker/process.py" not in dockerfile
    assert "process" not in sources["docker/filesystem/__init__.py"]


@pytest.mark.parametrize("factory", ["P.launch", "acquire"])
@pytest.mark.parametrize(
    "operation",
    [
        "return owner.capture()",
        "collect = owner.capture\n        return collect()",
    ],
)
def test_new_alias_consumer_using_prepared_public_api_is_accepted(
    sources, factory, operation
):
    addition = (
        "from docker.process import OwnedProcess as P\n"
        "def f(argv):\n    acquire = P.launch\n"
        f"    with {factory}(argv) as owner:\n        {operation}\n"
    )
    report = convergence_report({**sources, "docker/new_process_consumer.py": addition})
    assert report["errors"] == []
    assert any(
        row["path"] == "docker/new_process_consumer.py"
        and row["kind"] == "owned-launch"
        for row in report["ledger"]
    )


def test_policy_clock_precedes_action_start(sources):
    path = "docker/process.py"
    old = "stage_end = min(time.monotonic() + stage.seconds, end - policy.local_cleanup_seconds)"
    # Keep a decoy phase assignment after initiation; token presence is not proof.
    text = replace_snippet(sources[path], old, "stage_end = end", unique=True)
    text = insert_before(
        text,
        "if action.start(context) is not None:",
        old,
        unique=True,
    )
    assert convergence_report({**sources, path: text})["errors"]

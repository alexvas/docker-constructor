"""Focused proofs that enforcement mutations survive harmless layout changes.

Each test names one layout-sensitive assertion from the Ruff adoption audit.
It first establishes that exact source text no longer matches a harmless
reformat, then shows that the structural mutation still makes the audit reject
the same invalid shape.  These tests fail while the mutation helpers still bind
exact formatter output.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.enforcement.source_layout import replace_snippet
from tests.enforcement.test_filesystem_convergence import (
    _sources,
    _static_violations,
)
from tests.enforcement.test_lifecycle_guide_links import _check_local_link
from tests.enforcement.test_process_contract import policy_proof, process_proof

ROOT = Path(__file__).resolve().parents[2]
STREAMS = (ROOT / "docker/filesystem/streams.py").read_text()


# One-time request/deadline guard: the added deadline write must not appear
# outside the guarded block.  The formatter wraps the following condition.
_DEADLINE_OLD = (
    "                self._request.set()\n"
    "        if threading.current_thread() is self._continuation or "
    "threading.current_thread() in self._readers:\n"
    "            return self.cleanup_status"
)
_DEADLINE_NEW = (
    "                self._request.set()\n"
    "        self._cleanup_end = time.monotonic() + self._grace\n"
    "        if threading.current_thread() is self._continuation or "
    "threading.current_thread() in self._readers:\n"
    "            return self.cleanup_status"
)

# Configured child-wait observation: replacing the protected action call loses
# its failure classification.  The formatter explodes the call arguments.
_ACTION_OLD = (
    "self._action(observe_returncode, ordinary=(OSError,), "
    "phase=FailurePhase.WAIT)"
)
_ACTION_NEW = "observe_returncode()"

# Keyword-only mutation: the ordinary classification must not gain OSError.
_KEYWORDS_OLD = (
    "ordinary=(), phase=FailurePhase.JOIN, "
    "stream=tuple(ProcessStream)[index]"
)
_KEYWORDS_NEW = (
    "ordinary=(OSError,), phase=FailurePhase.JOIN, "
    "stream=tuple(ProcessStream)[index]"
)

# Prepared-owner/reader barrier: drop the tracked release guard.
_GUARD_OLD = (
    'self._safe(index) or self._descriptorless[index] or '
    'not callable(getattr(owner.stream, "fileno", None))'
)
_GUARD_NEW = 'True or not callable(getattr(owner.stream, "fileno", None))'

# Release release predicate: negating it must not still grant a close.
_RELEASE_IF_OLD = (
    "                    if self._safe(index) or "
    'self._descriptorless[index] or not callable(getattr(owner.stream, "fileno", None)):\n'
    "                        owner.close()"
)
_RELEASE_IF_NEW = (
    "                    if not (self._safe(index) or "
    'self._descriptorless[index] or not callable(getattr(owner.stream, "fileno", None))):\n'
    "                        owner.close()"
)

# Completed-cleanup predicate: a top-level `or` must not report completion.
_COMPLETE_OLD = (
    "return self._reaped and all(self._safe(i) for i in range(2)) and all(\n"
    "            not self._acquired[i] or self._owners[i].released for i in range(2)) and all(\n"
    "            not prepared or (result.done and result.cleanup_complete)\n"
    "            for prepared, result in zip(self._action_prepared, self._action_results))"
)
_COMPLETE_NEW = (
    "return self._reaped or all(self._safe(i) for i in range(2)) and all(\n"
    "            not self._acquired[i] or self._owners[i].released for i in range(2)) and all(\n"
    "            not prepared or (result.done and result.cleanup_complete)\n"
    "            for prepared, result in zip(self._action_prepared, self._action_results))"
)

# Diagnostic provenance: replacing the bounded sanitizer must be rejected.
_NOTE_OLD = (
    "_render_close_failure(failure.error, secrets, "
    "tail_projector=tail_projector)"
)
_NOTE_NEW = "str(failure.error)"


def test_secure_walker_survives_harmless_line_shift() -> None:
    sources = _sources()
    path = "docker/filesystem/descriptors.py"
    source = sources[path]
    marker = "    def open_secure_path("
    assert marker in source
    shifted = source.replace(
        marker, "    # harmless layout probe\n\n" + marker, 1
    )
    assert shifted != source

    assert _static_violations({**sources, path: shifted}) == []

    extra_walker = {
        **sources,
        "docker/filesystem/layout_probe_walker.py": (
            "def open_secure_path():\n    return None\n"
        ),
    }
    assert any("secure walkers" in item for item in _static_violations(extra_walker))


def test_guide_links_identify_named_tests_without_line_numbers() -> None:
    guide = ROOT / "docs/process-lifecycle.md"
    _check_local_link(
        guide,
        "../tests/test_process_single_collection.py",
        "test_reject_repeated_and_mixed_collection_without_work",
    )
    with pytest.raises(AssertionError):
        _check_local_link(
            guide,
            "../tests/test_process_single_collection.py",
            "test_renamed",
        )


def test_deadline_guard_mutation_survives_wrapped_condition() -> None:
    source = (ROOT / "docker/process.py").read_text()
    condition = (
        "if threading.current_thread() is self._continuation or "
        "threading.current_thread() in self._readers:"
    )
    wrapped = (
        "if (\n"
        "            threading.current_thread() is self._continuation\n"
        "            or threading.current_thread() in self._readers\n"
        "        ):"
    )
    variant = source.replace(condition, wrapped)
    assert _DEADLINE_OLD not in variant

    mutated = replace_snippet(variant, _DEADLINE_OLD, _DEADLINE_NEW)
    assert "one-time request/deadline guard" in policy_proof(mutated)


def test_stop_policy_call_and_keyword_mutations_survive_exploded_arguments() -> None:
    source = (ROOT / "docker/process.py").read_text()
    exploded_action = (
        "self._action(\n"
        "                    observe_returncode, ordinary=(OSError,), "
        "phase=FailurePhase.WAIT\n"
        "                )"
    )
    action_variant = source.replace(_ACTION_OLD, exploded_action)
    assert _ACTION_OLD not in action_variant
    mutated = replace_snippet(action_variant, _ACTION_OLD, _ACTION_NEW)
    assert process_proof(mutated, STREAMS)

    exploded_keywords = (
        "ordinary=(),\n"
        "                phase=FailurePhase.JOIN,\n"
        "                stream=tuple(ProcessStream)[index],"
    )
    keyword_variant = source.replace(_KEYWORDS_OLD, exploded_keywords)
    assert _KEYWORDS_OLD not in keyword_variant
    mutated = replace_snippet(keyword_variant, _KEYWORDS_OLD, _KEYWORDS_NEW)
    assert process_proof(mutated, STREAMS)


def test_release_guard_mutations_survive_parenthesized_test() -> None:
    source = (ROOT / "docker/process.py").read_text()
    wrapped = (
        "                    if (\n"
        "                        self._safe(index)\n"
        "                        or self._descriptorless[index]\n"
        '                        or not callable(getattr(owner.stream, "fileno", None))\n'
        "                    ):\n"
        "                        owner.close()"
    )
    variant = source.replace(_RELEASE_IF_OLD, wrapped)
    assert _RELEASE_IF_OLD not in variant

    mutated = replace_snippet(variant, _GUARD_OLD, _GUARD_NEW)
    assert process_proof(mutated, STREAMS)

    mutated = replace_snippet(variant, _RELEASE_IF_OLD, _RELEASE_IF_NEW)
    assert "release lacks prepared-owner/reader barrier" in process_proof(mutated, STREAMS)


def test_completion_predicate_mutation_survives_wrapped_return() -> None:
    source = (ROOT / "docker/process.py").read_text()
    wrapped = (
        "return (\n"
        "            self._reaped\n"
        "            and all(self._safe(i) for i in range(2))\n"
        "            and all(\n"
        "                not self._acquired[i] or self._owners[i].released for i in range(2)\n"
        "            )\n"
        "            and all(\n"
        "                not prepared or (result.done and result.cleanup_complete)\n"
        "                for prepared, result in zip(self._action_prepared, self._action_results)\n"
        "            )\n"
        "        )"
    )
    variant = source.replace(_COMPLETE_OLD, wrapped)
    assert _COMPLETE_OLD not in variant

    mutated = replace_snippet(variant, _COMPLETE_OLD, _COMPLETE_NEW)
    assert "false completed cleanup" in process_proof(mutated, STREAMS)


def test_architecture_inventory_ignores_layout_and_rejects_semantic_change() -> None:
    import json

    from tests.enforcement.process_convergence import canonical_architecture_report

    sources = _sources()
    fixture = json.loads(
        (ROOT / "tests/fixtures" / "process-architecture-inventory.json").read_text(
            encoding="utf-8"
        )
    )
    assert canonical_architecture_report(sources) == fixture

    original = "self._publish(self._complete())"
    wrapped = "self._publish(\n            self._complete()\n        )"
    variant = {
        **sources,
        "docker/process.py": sources["docker/process.py"].replace(original, wrapped),
    }
    assert variant["docker/process.py"] != sources["docker/process.py"]
    assert canonical_architecture_report(variant) == fixture

    semantic = {
        **sources,
        "docker/process.py": sources["docker/process.py"].replace(
            original, "self._publish(True)"
        ),
    }
    assert canonical_architecture_report(semantic) != fixture


def test_architecture_inventory_detects_docstring_content_change() -> None:
    import json

    from tests.enforcement.process_convergence import canonical_architecture_report

    sources = _sources()
    fixture = json.loads(
        (ROOT / "tests/fixtures" / "process-architecture-inventory.json").read_text(
            encoding="utf-8"
        )
    )
    assert canonical_architecture_report(sources) == fixture

    path = "docker/filesystem/streams.py"
    original = (
        '"""Exclusively own one already-open fd-owning stream until release."""'
    )
    changed = (
        '"""Exclusively own one already-open fd-owning stream until disposal."""'
    )
    assert sources[path].count(original) == 1
    mutated = {**sources, path: sources[path].replace(original, changed)}
    assert canonical_architecture_report(mutated) != fixture


def test_npm_diagnostic_mutation_survives_exploded_call() -> None:
    source = (ROOT / "docker/npm_environment/execution.py").read_text()
    exploded = (
        "_render_close_failure(\n"
        "                failure.error, secrets, tail_projector=tail_projector\n"
        "            )"
    )
    variant = source.replace(_NOTE_OLD, exploded)
    assert _NOTE_OLD not in variant

    mutated = replace_snippet(variant, _NOTE_OLD, _NOTE_NEW)
    from tests.enforcement.test_npm_environment_process_contract import (
        diagnostic_proof,
    )
    import ast

    assert diagnostic_proof(ast.parse(mutated))

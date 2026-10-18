"""Focused BuildKit cache-boundary tests for the converted Dockerfile stages.

These tests parse the repository Dockerfile and prove, without a Docker daemon,
that BuildKit invalidation remains independent per artifact: changing the rtk
named-context input (bytes or digest) invalidates only the rtk stage and the
dependent final assembly, leaving the fd, rustup, uv, Pi, and OpenSpec stages
eligible for cache reuse — and symmetrically for fd.

The model mirrors BuildKit's cache keys: a stage depends on its ``FROM`` base
stage and on every ``COPY --from=<stage>`` source, and an artifact input (its
named-context ``COPY`` plus its digest/version build args) is confined to one
consumer stage.  A change to an artifact therefore invalidates its consumer
stage plus every stage that transitively depends on it, and nothing else.
"""

from __future__ import annotations

import re
import shlex
import unittest
from pathlib import Path

from docker.versioning.build_snapshot import _LOGICAL_NAMES

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")

# Named-context logical filename and digest/version build args per artifact.
ARTIFACT_STAGE = {
    "rtk": "rtk-prebuilt",
    "fd": "fd-prebuilt",
    "rustup": "toolchain",
    "uv": "toolchain",
}
ARTIFACT_FILE = {
    "rtk": "rtk.deb",
    "fd": "fd.deb",
    "rustup": "rustup-init",
    "uv": "uv.tar.gz",
}
ARTIFACT_ARGS = {
    "rtk": ("RTK_VERSION", "RTK_SHA256"),
    "fd": ("FD_VERSION", "FD_SHA256"),
    "rustup": ("RUSTUP_SHA256", "RUST_VERSION"),
    "uv": ("UV_SHA256", "UV_VERSION"),
}
# Stages that must stay cacheable when an unrelated artifact changes.
FINAL_ASSEMBLY = "runtime"


def _logical_instructions() -> list[str]:
    """Return logical Dockerfile instructions, joining line continuations."""
    result: list[str] = []
    current = ""
    for raw in DOCKERFILE.splitlines():
        line = raw.strip()
        if not current and (not line or line.startswith("#")):
            continue
        current = f"{current} {line}".strip()
        if current.endswith("\\"):
            current = current[:-1].rstrip()
            continue
        result.append(current)
        current = ""
    return result


def _from_parent(image: str) -> str | None:
    """Return the base stage/image of a FROM clause, stripping flags."""
    tokens = image.split()
    while tokens and tokens[0].startswith("--"):
        tokens.pop(0)
    return tokens[0] if tokens else None


def _stages() -> dict[str, dict[str, object]]:
    """Map stage name -> {'parent': image, 'body': [logical instructions]}."""
    stages: dict[str, dict[str, object]] = {}
    current: str | None = None
    for instr in _logical_instructions():
        keyword = instr.split(maxsplit=1)[0].upper()
        if keyword == "FROM":
            match = re.match(r"^FROM\s+(.+?)(?:\s+AS\s+(\S+))?\s*$", instr, re.I)
            assert match, f"unparsable FROM instruction: {instr!r}"
            name = match.group(2)
            if name:
                current = name
                stages[name] = {"parent": match.group(1).strip(), "body": []}
            else:
                current = None
        elif current is not None:
            body = stages[current]["body"]
            assert isinstance(body, list)
            body.append(instr)
    return stages


def _dependencies(stages: dict[str, dict[str, object]]) -> dict[str, set[str]]:
    """Map stage -> set of stages it directly depends on (FROM and COPY --from)."""
    names = set(stages)
    deps: dict[str, set[str]] = {name: set() for name in names}
    for name, info in stages.items():
        parent = _from_parent(str(info["parent"]))
        if parent in names:
            deps[name].add(parent)
        body = info["body"]
        assert isinstance(body, list)
        for instr in body:
            for source in re.findall(r"--from=(\S+)", instr):
                if source in names:
                    deps[name].add(source)
    return deps


def _ancestors(stage: str, deps: dict[str, set[str]]) -> set[str]:
    """Return the transitive set of stages *stage* depends on."""
    seen: set[str] = set()
    stack = sorted(deps[stage])
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(sorted(deps[node]))
    return seen


def _named_context_sources(instr: str) -> list[str]:
    """Return the source operands of a COPY reading from constructor-artifacts."""
    if "--from=constructor-artifacts" not in instr:
        return []
    body = instr[len("COPY") :].strip()
    operands = shlex.split(body)
    return [op for op in operands[:-1] if not op.startswith("--")]


def _named_context_sources_by_stage(
    stages: dict[str, dict[str, object]],
) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for name, info in stages.items():
        body = info["body"]
        assert isinstance(body, list)
        sources: list[str] = []
        for instr in body:
            sources.extend(_named_context_sources(instr))
        if sources:
            result[name] = sources
    return result


def _stages_touching_arg(stages: dict[str, dict[str, object]], arg: str) -> set[str]:
    """Return stages whose body declares or references *arg*."""
    result: set[str] = set()
    for name, info in stages.items():
        body = info["body"]
        assert isinstance(body, list)
        for instr in body:
            if (f"${{{arg}}}" in instr) or (
                re.search(r"\bARG\b", instr)
                and re.search(rf"\b{re.escape(arg)}\b", instr)
            ):
                result.add(name)
    return result


class TestIndependentArtifactStageInvalidation(unittest.TestCase):
    """A changed rtk or fd input invalidates only its own stage and the final
    assembly, never the fd/rustup/uv/Pi/OpenSpec stages (and symmetrically)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.stages = _stages()
        cls.deps = _dependencies(cls.stages)

    def test_named_context_filenames_match_snapshot_logical_names(self) -> None:
        by_stage = _named_context_sources_by_stage(self.stages)
        all_sources = {source for sources in by_stage.values() for source in sources}
        expected = set(_LOGICAL_NAMES.values()) | {
            # Derived Pi environment, launcher tree, and both evidence sets,
            # all confined to one isolated directory in the named context.
            "derived-environments/pi/opt/pi",
            "derived-environments/pi/pi-assembler-evidence.json",
            "derived-environments/pi/pi-launcher-evidence.json",
        }
        self.assertEqual(expected, all_sources)

    def test_each_artifact_named_context_copy_is_confined_to_one_stage(self) -> None:
        by_stage = _named_context_sources_by_stage(self.stages)
        for artifact, filename in ARTIFACT_FILE.items():
            stages = {name for name, sources in by_stage.items() if filename in sources}
            with self.subTest(artifact=artifact):
                self.assertEqual({ARTIFACT_STAGE[artifact]}, stages)

    def test_each_artifact_digest_and_version_args_are_confined_to_its_stage(
        self,
    ) -> None:
        for artifact, args in ARTIFACT_ARGS.items():
            for arg in args:
                with self.subTest(artifact=artifact, arg=arg):
                    self.assertEqual(
                        {ARTIFACT_STAGE[artifact]},
                        _stages_touching_arg(self.stages, arg),
                    )

    def test_rtk_input_invalidates_only_rtk_stage_and_final_assembly(self) -> None:
        for stage in ("fd-prebuilt", "toolchain", "pi-tools", "openspec-tools"):
            with self.subTest(stage=stage):
                self.assertNotIn("rtk-prebuilt", _ancestors(stage, self.deps))
                self.assertNotEqual("rtk-prebuilt", stage)
        self.assertIn("rtk-prebuilt", _ancestors(FINAL_ASSEMBLY, self.deps))

    def test_fd_input_invalidates_only_fd_stage_and_final_assembly(self) -> None:
        for stage in ("rtk-prebuilt", "toolchain", "pi-tools", "openspec-tools"):
            with self.subTest(stage=stage):
                self.assertNotIn("fd-prebuilt", _ancestors(stage, self.deps))
                self.assertNotEqual("fd-prebuilt", stage)
        self.assertIn("fd-prebuilt", _ancestors(FINAL_ASSEMBLY, self.deps))

    def test_final_assembly_consumes_every_independent_tool_stage(self) -> None:
        runtime_ancestors = _ancestors(FINAL_ASSEMBLY, self.deps)
        for stage in (
            "rtk-prebuilt",
            "fd-prebuilt",
            "toolchain",
            "pi-tools",
            "openspec-tools",
        ):
            with self.subTest(stage=stage):
                self.assertIn(stage, runtime_ancestors)

    def test_rtk_and_fd_stages_depend_only_on_base(self) -> None:
        self.assertEqual({"base"}, _ancestors("rtk-prebuilt", self.deps))
        self.assertEqual({"base"}, _ancestors("fd-prebuilt", self.deps))


if __name__ == "__main__":
    unittest.main()

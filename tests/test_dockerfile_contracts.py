"""Repository-specific Dockerfile contracts requiring no Docker daemon."""
from __future__ import annotations

import glob
import json
import re
import shlex
import unittest
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")


def _instructions(name: str) -> list[str]:
    """Return logical Dockerfile instructions of *name*, joining continuations."""
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
        if current.split(maxsplit=1)[0].upper() == name:
            result.append(current)
        current = ""
    return result


def _copy_sources(instruction: str) -> list[str]:
    """Return local source operands from one logical COPY instruction."""
    body = instruction.removeprefix("COPY").strip()
    if body.startswith("["):
        operands = json.loads(body)
        return operands[:-1]
    operands = shlex.split(body)
    while operands and operands[0].startswith("--"):
        if operands[0] == "--from" and len(operands) > 1:
            return []
        if operands[0].startswith("--from="):
            return []
        operands.pop(0)
    return operands[:-1]


def _dockerignore_regex(pattern: str) -> re.Pattern[str]:
    """Translate Docker's slash-aware glob syntax, including ``**``."""
    out = ""
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if index + 1 < len(pattern) and pattern[index + 1] == "*":
                index += 1
                if index + 1 < len(pattern) and pattern[index + 1] == "/":
                    index += 1
                    out += "(?:.*/)?"
                else:
                    out += ".*"
            else:
                out += "[^/]*"
        elif char == "?":
            out += "[^/]"
        elif char == "[":
            end = pattern.find("]", index + 1)
            if end == -1:
                out += r"\["
            else:
                group = pattern[index + 1:end]
                if group.startswith("!"):
                    group = "^" + group[1:]
                out += "[" + group.replace("\\", r"\\") + "]"
                index = end
        else:
            out += re.escape(char)
        index += 1
    return re.compile(rf"^{out}$")


DockerignorePattern = tuple[bool, bool, re.Pattern[str]]


def _dockerignore_patterns(lines: list[str]) -> list[DockerignorePattern]:
    patterns: list[DockerignorePattern] = []
    for raw in lines:
        if not raw or raw.startswith("#"):
            continue
        value = raw.strip()
        if not value or value == ".":
            continue
        negated = value.startswith("!")
        if negated:
            value = value[1:]
        elif value.startswith((r"\#", r"\!")):
            value = value[1:]
        value = value.strip("/")
        if value:
            # Docker applies slashless patterns to a file or directory name at
            # every depth; patterns containing a slash are context-root paths.
            patterns.append((negated, "/" not in value, _dockerignore_regex(value)))
    return patterns


def _is_dockerignored(path: str, patterns: list[DockerignorePattern]) -> bool:
    """Evaluate a context-relative path using ordered Docker ignore rules."""
    normalized = PurePosixPath(path.strip("/")).as_posix()
    candidates = [normalized]
    parent = PurePosixPath(normalized).parent
    while parent.as_posix() != ".":
        candidates.append(parent.as_posix())
        parent = parent.parent
    ignored = False
    for negated, basename_only, matcher in patterns:
        values = (
            [PurePosixPath(candidate).name for candidate in candidates]
            if basename_only else candidates
        )
        if any(matcher.fullmatch(value) for value in values):
            ignored = not negated
    return ignored


def _expanded_copy_paths(root: Path, source: str) -> list[Path]:
    # Docker may intentionally filter children of a copied directory. Only the
    # source operand itself must be visible; recursively requiring every child
    # would reject valid COPY operations.
    return [Path(value) for value in glob.glob(str(root / source), recursive=True)]


class TestDockerignoreSemantics(unittest.TestCase):
    def test_directory_glob_hides_copied_children(self) -> None:
        patterns = _dockerignore_patterns(["docker/*"])
        self.assertTrue(_is_dockerignored("docker/runtime_installer.py", patterns))

    def test_root_wildcard_hides_copy_source(self) -> None:
        patterns = _dockerignore_patterns(["*"])
        self.assertTrue(_is_dockerignored("docker", patterns))
        self.assertTrue(_is_dockerignored("docker/entrypoint.sh", patterns))

    def test_last_matching_negation_controls_visibility(self) -> None:
        visible = _dockerignore_patterns(["docker/*", "!docker/entrypoint.sh"])
        hidden_again = _dockerignore_patterns(
            ["docker/*", "!docker/entrypoint.sh", "docker/entrypoint.sh"]
        )
        self.assertFalse(_is_dockerignored("docker/entrypoint.sh", visible))
        self.assertTrue(_is_dockerignored("docker/entrypoint.sh", hidden_again))

    def test_double_star_matches_nested_sources(self) -> None:
        patterns = _dockerignore_patterns(["**/*.py"])
        self.assertTrue(_is_dockerignored("docker/versioning/model.py", patterns))

    def test_slashless_pattern_matches_basename_at_any_depth(self) -> None:
        patterns = _dockerignore_patterns(["*.md"])
        self.assertTrue(_is_dockerignored("docs/internal/notes.md", patterns))
        self.assertFalse(_is_dockerignored("docs/internal/notes.txt", patterns))

    def test_escaped_comment_and_negation_prefixes_are_literals(self) -> None:
        patterns = _dockerignore_patterns([r"\#generated", r"\!important"])
        self.assertTrue(_is_dockerignored("nested/#generated", patterns))
        self.assertTrue(_is_dockerignored("nested/!important", patterns))

    def test_ignored_child_does_not_hide_copied_directory(self) -> None:
        patterns = _dockerignore_patterns(["*.md"])
        self.assertFalse(_is_dockerignored("docker", patterns))
        self.assertTrue(_is_dockerignored("docker/docs/readme.md", patterns))


class TestDockerfileBuildContract(unittest.TestCase):
    def test_local_copy_sources_exist_and_are_visible_in_context(self) -> None:
        local_sources = [
            source
            for instruction in _instructions("COPY")
            for source in _copy_sources(instruction)
        ]
        self.assertTrue(local_sources)
        patterns = _dockerignore_patterns(
            (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        )
        for source in local_sources:
            with self.subTest(source=source):
                paths = _expanded_copy_paths(ROOT, source)
                self.assertTrue(paths, f"missing COPY source: {source}")
                hidden = [
                    path.relative_to(ROOT).as_posix()
                    for path in paths
                    if _is_dockerignored(path.relative_to(ROOT).as_posix(), patterns)
                ]
                self.assertFalse(
                    hidden,
                    f"COPY source {source!r} is excluded by .dockerignore: {hidden}",
                )

    def test_renderer_build_arguments_are_declared_and_consumed(self) -> None:
        # This is the renderer's reviewed project argument surface. Optional
        # corporate-network keys are included even when a particular build omits them.
        rendered = {
            "NODE_BASE_IMAGE", "RUST_VERSION", "RUST_PROFILE", "RUST_COMPONENTS",
            "RUSTUP_SHA256", "UV_VERSION", "UV_SHA256",
            "PYTHON_VERSION", "TY_VERSION", "RTK_VERSION", "RTK_SHA256",
            "FD_VERSION", "FD_SHA256", "PI_VERSION", "OPENSPEC_VERSION",
            "PI_ASSEMBLED_OUTPUT_IDENTITY", "PI_TREE_DIGEST",
            "PI_ASSEMBLER_EVIDENCE_DIGEST", "PI_ASSEMBLER_EVIDENCE_BYTES_DIGEST",
            "PI_LAUNCHER_EVIDENCE_DIGEST", "OH_MY_ZSH_VERSION", "PI_CORPORATE_PROXY_URL", "PI_CORPORATE_NO_PROXY",
            "CORPORATE_TRUST_ENABLED", "PI_CORPORATE_CA_PATH", "DEV_UID", "DEV_GID",
        }
        declared = set(re.findall(r"^ARG\s+([A-Z][A-Z0-9_]*)", DOCKERFILE, re.MULTILINE))
        self.assertEqual(rendered, declared)
        helper = (ROOT / "docker/corp-network-env.sh").read_text(encoding="utf-8")
        helper_arguments = {
            "PI_CORPORATE_PROXY_URL", "PI_CORPORATE_NO_PROXY",
            "CORPORATE_TRUST_ENABLED", "PI_CORPORATE_CA_PATH",
        }
        # RTK/FD versions are deliberate cache-identity inputs; artifact URL and
        # checksum perform the installation while a version change still busts
        # the corresponding stage.
        identity_arguments = {"RTK_VERSION", "FD_VERSION"}
        # Pi attestation values are consumed by the image-side verify script via
        # ARG-provided environment, not via ``${...}`` interpolation.
        pi_verification_arguments = {
            "PI_VERSION", "PI_ASSEMBLED_OUTPUT_IDENTITY", "PI_TREE_DIGEST",
            "PI_ASSEMBLER_EVIDENCE_DIGEST", "PI_ASSEMBLER_EVIDENCE_BYTES_DIGEST",
            "PI_LAUNCHER_EVIDENCE_DIGEST",
        }
        for name in rendered - helper_arguments - identity_arguments - pi_verification_arguments:
            with self.subTest(argument=name):
                self.assertRegex(DOCKERFILE, rf"\$\{{?{name}\}}?", f"unused ARG {name}")
        for name in helper_arguments:
            with self.subTest(helper_argument=name):
                self.assertIn(name, helper)
        verify_pi = (ROOT / "docker/verify-pi.mjs").read_text(encoding="utf-8")
        for name in pi_verification_arguments:
            with self.subTest(pi_verification_argument=name):
                self.assertIn(name, verify_pi)

    def test_constructor_target_stage_exists(self) -> None:
        stages = set(re.findall(r"^FROM\s+.+?\s+AS\s+(\S+)", DOCKERFILE, re.I | re.M))
        self.assertIn("runtime", stages)

    def test_pi_named_context_copies_use_isolated_paths(self) -> None:
        pi_copies = [
            instruction
            for instruction in _instructions("COPY")
            if "constructor-artifacts" in instruction
            and "derived-environments/pi/" in instruction
        ]
        self.assertEqual(3, len(pi_copies))
        self.assertIn(
            "COPY --from=constructor-artifacts derived-environments/pi/opt/pi /opt/pi",
            DOCKERFILE,
        )
        self.assertIn(
            "COPY --from=constructor-artifacts --chmod=0444 "
            "derived-environments/pi/pi-assembler-evidence.json "
            "/tmp/pi-assembler-evidence.json",
            DOCKERFILE,
        )
        self.assertIn(
            "COPY --from=constructor-artifacts --chmod=0444 "
            "derived-environments/pi/pi-launcher-evidence.json "
            "/tmp/pi-launcher-evidence.json",
            DOCKERFILE,
        )

    def test_runtime_home_copy_owns_all_descendants_for_custom_dev_identity(self) -> None:
        """Cross-stage COPY otherwise resets descendants to root:root.

        ``--chown`` is Docker's recursive copy-time ownership mechanism, so
        this contract covers every runtime-home descendant without a later
        whole-home ``chown -R``.
        """
        runtime_home_copies = (
            ("/home/dev/.local", "/home/dev/.local"),
            ("/home/dev/.rustup", "/home/dev/.rustup"),
            ("/home/dev/.cargo/bin", "/home/dev/.cargo/bin"),
            ("/home/dev/mcp", "/home/dev/mcp"),
        )
        for source, destination in runtime_home_copies:
            with self.subTest(source=source):
                self.assertIn(
                    f"COPY --chown=dev:dev --from=toolchain {source} {destination}",
                    DOCKERFILE,
                )
        self.assertNotRegex(DOCKERFILE, r"chown\\s+-R\\s+[^\\n]*?/home/dev(?:\\s|$)")

    def test_runtime_startup_files_and_entrypoint_agree(self) -> None:
        self.assertIn("COPY docker/entrypoint.sh /usr/local/bin/docker-entrypoint.sh", DOCKERFILE)
        self.assertEqual(
            ['ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]'],
            _instructions("ENTRYPOINT"),
        )
        self.assertNotIn("install-pi-extensions.sh", DOCKERFILE)

    def test_filesystem_foundation_is_copied_for_runtime_imports(self) -> None:
        """Phase 2 task 2.10 — the image layout ships ``docker/filesystem``.

        The image carries only Dockerfile-copied Python files, so the
        lightweight descriptor lifecycle package must land at the exact
        image path used by the runtime-installer lifecycle imports.
        """
        self.assertIn(
            "COPY docker/filesystem/ /usr/local/lib/pi-cli/docker/filesystem/",
            DOCKERFILE,
        )
        parsed = {
            tuple(
                operand
                for operand in shlex.split(instruction.removeprefix("COPY").strip())
                if not operand.startswith("--")
            )
            for instruction in _instructions("COPY")
        }
        self.assertIn(
            (
                "docker/filesystem/",
                "/usr/local/lib/pi-cli/docker/filesystem/",
            ),
            parsed,
        )

    def test_reviewed_inputs_and_retired_surfaces_are_not_baked(self) -> None:
        prohibited = (
            "docker-constructor.toml", "docker/versions.py", "docker/verify_stage_6",
            "docker-compose.yml", "install-pi-extensions.sh",
        )
        copies = "\n".join(_instructions("COPY"))
        for value in prohibited:
            with self.subTest(value=value):
                self.assertNotIn(value, copies)

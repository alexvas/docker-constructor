"""Migration contract tests — reject stale references to removed surface.

Stage 13 replaces old CLIs, wrappers, Compose fragments, generated
artifacts, inf-splitter settings, and separate phase inventories with
the single ``docker/docker-constructor.py`` facade.  These RED tests
document the removal contract: every scan MUST fail until the
referenced files and documentation are updated in 13.3–13.5.
"""

from __future__ import annotations

import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent

# ════════════════════════════════════════════════════════════════════
# Shell script scan roots
# ════════════════════════════════════════════════════════════════════

_SH_SCAN_ROOTS: list[str] = [
    "docker/",
]

# Shell scripts excluded entirely
_SH_EXCLUDED_FILES: frozenset[str] = frozenset()

# ════════════════════════════════════════════════════════════════════
# Documentation scan (beyond root READMEs)
# ════════════════════════════════════════════════════════════════════

_DOC_SCAN_ROOTS: list[str] = [
    "docker/",
]

# Main OpenSpec specs — authoritative, maintained documentation.
# The current change's delta specs (openspec/changes/<name>/) are
# excluded because they intentionally describe the migration.
_OPENSPEC_SPECS_DIR: str = "openspec/specs/"
_OPENSPEC_CHANGES_DIR: str = "openspec/changes/"

_DOC_FILE_EXCLUDED: frozenset[str] = frozenset()

# ════════════════════════════════════════════════════════════════════
# Configuration file scan (only maintained TOML)
# ════════════════════════════════════════════════════════════════════

# Compose YAML files are removed in 13.4 and are excluded.  Only the
# reviewed inventory and env template are maintained.
_CFG_SCAN_FILES: tuple[str, ...] = (
    "docker-constructor.toml",
    ".env.example",
)

# ════════════════════════════════════════════════════════════════════
# Python scan roots
# ════════════════════════════════════════════════════════════════════

_PY_SCAN_ROOTS = [
    "docker/",
    "tests/",
]

# Files are excluded **in their entirety**
_PY_EXCLUDED = {}

# Files with line-level allowlists (contextual mentions)
_PY_LINE_ALLOWLIST = frozenset(
    {
        # Docstring-only mention of what was extracted from
        "docker/tui.py",
        # Installer docstring describing what NOT to depend on
        "docker/runtime_installer.py",
        # This very test file documents the contract
        "tests/test_constructor_migration_contract.py",
        # Assert COMPOSE_FILE/env vars NOT present in rendered output
        "tests/test_constructor_build_vector.py",
        "tests/test_constructor_run_vector.py",
        # OpenSpec artifacts describing the change
        "openspec/",
        # Runtime verification correctly asserts these paths must NOT exist
        "docker/versioning/runtime_verification.py",
        "tests/test_constructor_runtime_verification.py",
    }
)

# Entire files excluded from the forbidden-runtime-paths scan because
# they document the absence contract (assert paths must not exist) or
# are legacy acceptance evidence.
_FORBIDDEN_PATHS_EXCLUDED_FILES = frozenset(
    {
        "docker/versioning/runtime_verification.py",
        "tests/test_constructor_runtime_verification.py",
        "tests/test_constructor_acceptance.py",
        "docker/constructor_cli.py",
    }
)


# ════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════


def _py_files() -> dict[str, pathlib.Path]:
    """Return ``{relpath: abspath}`` for every production Python file
    under scan roots, excluding whole-file exclusions."""
    result: dict[str, pathlib.Path] = {}
    for root_spec in _PY_SCAN_ROOTS:
        root = REPO / root_spec
        if root.is_dir():
            for p in root.rglob("*.py"):
                if p.is_file():
                    rel = str(p.relative_to(REPO))
                    if any(rel.startswith(ex) for ex in _PY_EXCLUDED):
                        continue
                    result[rel] = p
        elif root.is_file():
            rel = str(root.relative_to(REPO))
            if not any(rel.startswith(ex) for ex in _PY_EXCLUDED):
                result[rel] = root
    return result


def _lines(path: pathlib.Path) -> list[tuple[int, str]]:
    """Read *path* lines, returning ``[(lineno, text), …]``."""
    try:
        return list(enumerate(path.read_text(encoding="utf-8").splitlines(), 1))
    except UnicodeDecodeError:
        return []


# ════════════════════════════════════════════════════════════════════
# 13.1a — Old CLI surface
# ════════════════════════════════════════════════════════════════════

_OLD_CLI_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "versions.py used as user-facing command",
        re.compile(r"versions\.py\s+(compose|validate|check-updates|env|show|run)"),
    ),
    (
        "launch-pi.py reference",
        re.compile(r"launch-pi\.py"),
    ),
    (
        "build_wrapper.py reference",
        re.compile(r"build_wrapper\.py"),
    ),
    (
        "gen-models-json reference",
        re.compile(r"gen-models-json"),
    ),
]


class TestNoStaleCLIReferences(unittest.TestCase):
    """Production Python files MUST NOT reference old CLI entry points
    or wrappers that are being removed in Stage 13."""

    def test_no_stale_cli_references_in_docker_py(self) -> None:
        violations: list[str] = []
        for rel, path in sorted(_py_files().items()):
            allowlisted = rel in _PY_LINE_ALLOWLIST
            for lineno, line in _lines(path):
                for label, pat in _OLD_CLI_PATTERNS:
                    m = pat.search(line)
                    if not m:
                        continue
                    if allowlisted:
                        continue
                    violations.append(f"{rel}:{lineno}: {label} — {line.strip()[:100]}")
        if violations:
            self.fail(
                "Production files reference old CLI surface — update to "
                "docker/docker-constructor.py or remove:\n" + "\n".join(violations)
            )


# ════════════════════════════════════════════════════════════════════
# 13.1b — Compose surface
# ════════════════════════════════════════════════════════════════════

_COMPOSE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "COMPOSE_FILE env var",
        re.compile(r"COMPOSE_FILE"),
    ),
    (
        "docker-compose.yml reference",
        re.compile(r"docker-compose(?:\\.runtime)?\\.yml"),
    ),
    (
        "compose as subcommand",
        re.compile(r"versions\\.py\\s+compose"),
    ),
]

# Generated Compose fragment patterns — project-specific or temporary
# override files the old launcher created.
_GENERATED_FRAGMENT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "compose.projN.yml fragment",
        re.compile(r"compose\\.proj\\d+\\.yml"),
    ),
    (
        ".projN.yml hidden fragment",
        re.compile(r"\\.proj\\d+\\.yml"),
    ),
    (
        "generate_proj_fragment call",
        re.compile(r"generate_proj_fragment"),
    ),
    (
        "extra_fragments list",
        re.compile(r"extra_fragments"),
    ),
    (
        ".override.yml hidden fragment",
        re.compile(r"\\.override\\.yml"),
    ),
]


class TestNoStaleComposeReferences(unittest.TestCase):
    """Production Python files MUST NOT reference Compose files,
    ``COMPOSE_FILE``, or ``versions.py compose``."""

    def test_no_stale_compose_references_in_docker_py(self) -> None:
        violations: list[str] = []
        for rel, path in sorted(_py_files().items()):
            allowlisted = rel in _PY_LINE_ALLOWLIST
            for lineno, line in _lines(path):
                for label, pat in _COMPOSE_PATTERNS + _GENERATED_FRAGMENT_PATTERNS:
                    m = pat.search(line)
                    if not m:
                        continue
                    if allowlisted:
                        continue
                    violations.append(f"{rel}:{lineno}: {label} — {line.strip()[:100]}")
        if violations:
            self.fail(
                "Production files reference Compose or generated fragments — "
                "replace with direct Docker rendering:\n" + "\n".join(violations)
            )


# ════════════════════════════════════════════════════════════════════
# 13.1c — Inf-splitter settings
# ════════════════════════════════════════════════════════════════════

_INF_SPLITTER_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "inf-splitter.toml path",
        re.compile(r"inf-splitter\.toml"),
    ),
    (
        "/etc/inf-splitter path",
        re.compile(r"/etc/inf-splitter"),
    ),
    (
        "inf-splitter provider key",
        re.compile(r'"inf-splitter"'),
    ),
]


class TestNoStaleInfSplitterReferences(unittest.TestCase):
    """Production files MUST NOT reference ``/etc/inf-splitter/``,
    ``inf-splitter.toml``, or the ``inf-splitter`` provider key."""

    def test_no_stale_inf_splitter_references(self) -> None:
        violations: list[str] = []
        for rel, path in sorted(_py_files().items()):
            allowlisted = rel in _PY_LINE_ALLOWLIST
            for lineno, line in _lines(path):
                for label, pat in _INF_SPLITTER_PATTERNS:
                    m = pat.search(line)
                    if not m:
                        continue
                    if allowlisted:
                        continue
                    violations.append(f"{rel}:{lineno}: {label} — {line.strip()[:100]}")
        if violations:
            self.fail(
                "Production files reference inf-splitter — remove or "
                "migrate to docker-constructor model:\n" + "\n".join(violations)
            )


# ════════════════════════════════════════════════════════════════════
# 13.1d — Unified inventory (single source of truth)
# ════════════════════════════════════════════════════════════════════

_SEPARATE_INVENTORY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "versions.toml (pre-rename inventory)",
        re.compile(r"\bversions\.toml\b"),
    ),
    (
        "separate build inventory mention",
        re.compile(r"\bbuild[-_]inventory\.toml\b"),
    ),
    (
        "separate runtime inventory mention",
        re.compile(r"\bruntime[-_]inventory\.toml\b"),
    ),
]


class TestNoSeparatePhaseInventories(unittest.TestCase):
    """Production files MUST reference exactly one
    ``docker-constructor.toml`` as the reviewed inventory — never
    separate per-phase TOML files."""

    def test_no_separate_inventory_references(self) -> None:
        violations: list[str] = []
        for rel, path in sorted(_py_files().items()):
            allowlisted = rel in _PY_LINE_ALLOWLIST
            for lineno, line in _lines(path):
                for label, pat in _SEPARATE_INVENTORY_PATTERNS:
                    m = pat.search(line)
                    if not m:
                        continue
                    if allowlisted:
                        continue
                    violations.append(f"{rel}:{lineno}: {label} — {line.strip()[:100]}")
        if violations:
            self.fail(
                "Production files reference separate phase inventories — "
                "use docker-constructor.toml as the single source:\n"
                + "\n".join(violations)
            )


# ════════════════════════════════════════════════════════════════════
# 13.1e — Forbidden runtime configuration paths
# ════════════════════════════════════════════════════════════════════

# Paths inside the container that MUST NOT be referenced as readable
# or mounted from host-side reviewed/build artifacts.
_FORBIDDEN_CONTAINER_PATHS: list[tuple[str, re.Pattern[str]]] = [
    (
        "docker-constructor.toml mounted in container",
        re.compile(r"/run/pi-cli/docker-constructor\.toml"),
    ),
    (
        "build effective projection mounted in container",
        re.compile(r"/run/pi-cli/docker-constructor\.build\.effective\.toml"),
    ),
    (
        "host-side reviewed source reachable at runtime",
        re.compile(r"/usr/local/share/pi-cli/docker-constructor\.toml"),
    ),
]


class TestNoForbiddenRuntimeConfigurationPaths(unittest.TestCase):
    """Production files MUST NOT mount or reference reviewed inventory
    or build projections as readable inside the runtime container."""

    def test_no_forbidden_runtime_paths(self) -> None:
        violations: list[str] = []
        for rel, path in sorted(_py_files().items()):
            # These files correctly document/assert the absence contract
            if rel in _FORBIDDEN_PATHS_EXCLUDED_FILES:
                continue
            allowlisted = rel in _PY_LINE_ALLOWLIST
            # Also allow OpenSpec specs that describe what SHALL NOT happen
            if rel.startswith("openspec/"):
                continue
            for lineno, line in _lines(path):
                for label, pat in _FORBIDDEN_CONTAINER_PATHS:
                    m = pat.search(line)
                    if not m:
                        continue
                    if allowlisted:
                        continue
                    violations.append(f"{rel}:{lineno}: {label} — {line.strip()[:100]}")
        if violations:
            self.fail(
                "Production files reference forbidden runtime paths — "
                "these files MUST NOT be accessible inside the container:\n"
                + "\n".join(violations)
            )


# ════════════════════════════════════════════════════════════════════
# 13.1f — Documentation references
# ════════════════════════════════════════════════════════════════════

_DOC_FILES: tuple[str, ...] = (
    "README.md",
    "README.en.md",
    "README.zh.md",
)

_DOC_FORBIDDEN: list[tuple[str, re.Pattern[str]]] = [
    (
        "launch-pi.py reference in docs",
        re.compile(r"\./launch-pi\.py|python3\s+launch-pi\.py"),
    ),
    (
        "docker/versions.py compose in docs",
        re.compile(r"\./docker/versions\.py\s+compose"),
    ),
    (
        "docker/versions.py validate in docs",
        re.compile(r"\./docker/versions\.py\s+validate"),
    ),
    (
        "docker/versions.py env in docs",
        re.compile(r"\./docker/versions\.py\s+env"),
    ),
    (
        "docker/versions.py check-updates in docs",
        re.compile(r"\./docker/versions\.py\s+check-updates"),
    ),
    (
        "docker/versions.py show in docs",
        re.compile(r"\./docker/versions\.py\s+show"),
    ),
    (
        "build_wrapper.py in docs",
        re.compile(r"docker/build_wrapper\.py"),
    ),
    (
        "docker-compose in docs",
        re.compile(r"docker-compose(?:\.runtime)?\.yml"),
    ),
    (
        "COMPOSE_FILE in docs",
        re.compile(r"COMPOSE_FILE"),
    ),
    (
        "generated compose fragment in docs",
        re.compile(r"compose\.proj\d+\.yml"),
    ),
    (
        "versus additional projects limit in docs",
        re.compile(r"(?:до\s+двух|up to two)\s+(?:дополнительных\s+)?проектов"),
    ),
    (
        "two additional projects limit in docs",
        re.compile(r"two (?:additional )?projects"),
    ),
]


class TestNoStaleDocumentationReferences(unittest.TestCase):
    """READMEs MUST reference ``docker/docker-constructor.py`` commands,
    not ``versions.py``, ``launch-pi.py``, ``build_wrapper.py``, or
    Compose files."""

    def test_no_stale_references_in_readmes(self) -> None:
        violations: list[str] = []
        for doc_name in _DOC_FILES:
            doc_path = REPO / doc_name
            if not doc_path.is_file():
                continue
            for lineno, line in _lines(doc_path):
                for label, pat in _DOC_FORBIDDEN:
                    m = pat.search(line)
                    if not m:
                        continue
                    violations.append(
                        f"{doc_name}:{lineno}: {label} — {line.strip()[:100]}"
                    )
        if violations:
            self.fail(
                "README files reference removed or renamed commands — "
                "update to docker/docker-constructor.py interface:\n"
                + "\n".join(violations)
            )


# ════════════════════════════════════════════════════════════════════
# 13.1g — Shell script scan
# ════════════════════════════════════════════════════════════════════

_SH_FORBIDDEN: list[tuple[str, re.Pattern[str]]] = [
    (
        "build_wrapper.py in shell script",
        re.compile(r"build_wrapper\.py"),
    ),
    (
        "versions.py path in shell script",
        re.compile(r"versions\.py"),
    ),
    (
        "Compose in shell script",
        re.compile(r"\bCompose\b"),
    ),
    (
        "launch-pi.py in shell script",
        re.compile(r"launch-pi\.py"),
    ),
    (
        "docker-compose in shell script",
        re.compile(r"docker-compose"),
    ),
    (
        "COMPOSE_FILE in shell script",
        re.compile(r"COMPOSE_FILE"),
    ),
    (
        "inf-splitter in shell script",
        re.compile(r"inf-splitter"),
    ),
    (
        "generated compose fragment in shell script",
        re.compile(r"compose\.proj\d+\.yml"),
    ),
]

# Shell scripts with contextual allowlist lines
_SH_LINE_ALLOWLIST = frozenset(
    {
        # entrypoint.sh runs inside the container, so its workspace environment
        # comments document the launcher contract rather than user-facing CLI.
        # Only flag references to removed surface names.
        "docker/entrypoint.sh",
    }
)


def _sh_files() -> dict[str, pathlib.Path]:
    """Return ``{relpath: abspath}`` for every shell script under
    scan roots, excluding whole-file exclusions."""
    result: dict[str, pathlib.Path] = {}
    for root_spec in _SH_SCAN_ROOTS:
        root = REPO / root_spec
        if not root.is_dir():
            continue
        for p in root.rglob("*.sh"):
            if not p.is_file():
                continue
            rel = str(p.relative_to(REPO))
            if rel in _SH_EXCLUDED_FILES:
                continue
            result[rel] = p
    return result


class TestNoStaleShellScriptReferences(unittest.TestCase):
    """Shell scripts MUST NOT reference ``build_wrapper.py``,
    ``versions.py``, Compose, or ``launch-pi.py``."""

    def test_no_stale_references_in_shell_scripts(self) -> None:
        violations: list[str] = []
        for rel, path in sorted(_sh_files().items()):
            allowlisted = rel in _SH_LINE_ALLOWLIST
            for lineno, line in _lines(path):
                for label, pat in _SH_FORBIDDEN:
                    m = pat.search(line)
                    if not m:
                        continue
                    if allowlisted:
                        # Per-line judgement for entrypoint.sh:
                        # "Compose/launch-pi.py" and "Compose contract"
                        # are historical comments that must be updated.
                        pass
                    violations.append(f"{rel}:{lineno}: {label} — {line.strip()[:100]}")
        if violations:
            self.fail(
                "Shell scripts reference removed surface — update to "
                "docker/docker-constructor.py or remove obsolete files:\n"
                + "\n".join(violations)
            )


# ════════════════════════════════════════════════════════════════════
# 13.1h — Documentation beyond root READMEs
# ════════════════════════════════════════════════════════════════════

_NON_README_DOC_FORBIDDEN: list[tuple[str, re.Pattern[str]]] = [
    (
        "versions.py in maintained doc",
        re.compile(r"\./docker/versions\.py|python3\s+docker/versions\.py"),
    ),
    (
        "build_wrapper.py in maintained doc",
        re.compile(r"build_wrapper\.py"),
    ),
    (
        "launch-pi.py in maintained doc",
        re.compile(r"launch-pi\.py"),
    ),
    (
        "COMPOSE_FILE in maintained doc",
        re.compile(r"COMPOSE_FILE"),
    ),
    (
        "docker-compose in maintained doc",
        re.compile(r"docker-compose"),
    ),
    (
        "inf-splitter in maintained doc",
        re.compile(r"inf-splitter"),
    ),
    (
        "generated compose fragment in maintained doc",
        re.compile(r"compose\.proj\d+\.yml"),
    ),
]


def _maintained_doc_files() -> dict[str, pathlib.Path]:
    """Return maintained ``.md`` files under ``docker/`` (not READMEs,
    not verify_stage_6/, not OpenSpec artifacts)."""
    result: dict[str, pathlib.Path] = {}
    for root_spec in _DOC_SCAN_ROOTS:
        root = REPO / root_spec
        if not root.is_dir():
            continue
        for p in root.rglob("*.md"):
            if not p.is_file():
                continue
            rel = str(p.relative_to(REPO))
            if any(rel.startswith(ex) for ex in _DOC_FILE_EXCLUDED):
                continue
            result[rel] = p
    return result


class TestNoStaleMaintainedDocReferences(unittest.TestCase):
    """Maintained documentation under ``docker/`` MUST NOT reference
    removed CLIs, Compose, or inf-splitter."""

    def test_no_stale_references_in_maintained_docs(self) -> None:
        violations: list[str] = []
        for rel, path in sorted(_maintained_doc_files().items()):
            for lineno, line in _lines(path):
                for label, pat in _NON_README_DOC_FORBIDDEN:
                    m = pat.search(line)
                    if not m:
                        continue
                    violations.append(f"{rel}:{lineno}: {label} — {line.strip()[:100]}")
        if violations:
            self.fail(
                "Maintained documentation references removed surface:\n"
                + "\n".join(violations)
            )


# ════════════════════════════════════════════════════════════════════
# 13.1i — Main OpenSpec specs (authoritative maintained documentation)
# ════════════════════════════════════════════════════════════════════

_SPEC_FORBIDDEN: list[tuple[str, re.Pattern[str]]] = [
    (
        "versions.py as command in spec",
        re.compile(r"docker/versions\.py"),
    ),
    (
        "build_wrapper.py in spec",
        re.compile(r"docker/build_wrapper\.py"),
    ),
    (
        "launch-pi.py in spec",
        re.compile(r"launch-pi\.py"),
    ),
    (
        "inf-splitter in spec",
        re.compile(r"inf-splitter"),
    ),
    (
        "gen-models-json in spec",
        re.compile(r"gen-models-json"),
    ),
    (
        "docker-compose in spec",
        re.compile(r"docker-compose"),
    ),
    (
        "COMPOSE_FILE in spec",
        re.compile(r"COMPOSE_FILE"),
    ),
    (
        "compose.proj in spec",
        re.compile(r"compose\.proj"),
    ),
    (
        "Compose service/service reference in spec",
        re.compile(r"Compose\s+(service|command|configuration|fragment)"),
    ),
]


def _main_spec_files() -> dict[str, pathlib.Path]:
    """Return maintained OpenSpec spec files (not change deltas)."""
    result: dict[str, pathlib.Path] = {}
    specs_dir = REPO / _OPENSPEC_SPECS_DIR
    if specs_dir.is_dir():
        for p in specs_dir.rglob("*.md"):
            if p.is_file():
                rel = str(p.relative_to(REPO))
                result[rel] = p
    return result


def _is_negative_contract_line(line: str, match: re.Match[str]) -> bool:
    """True when the *match* term is governed by an explicit prohibition.

    Only ``SHALL NOT`` / ``MUST NOT`` and ``without … using/requiring``
    are accepted, and they must appear in the same clause as the match —
    a line that prohibits one thing while endorsing another does not
    exempt the endorsement.
    """
    start, end = match.start(), match.end()
    # Only treat ``.`` as a delimiter when followed by whitespace or
    # end-of-string — spec bullets embed dots in ``versions.py``.
    _clause_break = re.compile(r"[.;]\s")
    clause_start = 0
    for m2 in re.finditer(_clause_break, line[:start]):
        clause_start = m2.end()
    clause_end = len(line)
    m3 = re.search(_clause_break, line[end:])
    if m3:
        clause_end = end + m3.start()
    clause = line[clause_start:clause_end]
    # The negative language must appear *before* the match within the
    # clause — otherwise it could govern a later, unrelated term.
    prefix = clause[: end - clause_start]
    return bool(
        re.search(r"\b(SHALL NOT|MUST NOT)\b", prefix)
        or re.search(r"without\b.*\b(using|requiring)", prefix)
    )


class TestNoStaleMainSpecReferences(unittest.TestCase):
    """Authoritative ``openspec/specs/`` MUST NOT reference removed
    CLIs, Compose, inf-splitter, or generated fragments."""

    def test_no_stale_references_in_main_specs(self) -> None:
        violations: list[str] = []
        for rel, path in sorted(_main_spec_files().items()):
            for lineno, line in _lines(path):
                for label, pat in _SPEC_FORBIDDEN:
                    m = pat.search(line)
                    if not m:
                        continue
                    # Explicit negative contract statements (SHALL NOT,
                    # MUST NOT, without … using/requiring) governing
                    # the matched term are legitimate retirement
                    # language, not stale endorsements.
                    if _is_negative_contract_line(line, m):
                        continue
                    violations.append(f"{rel}:{lineno}: {label} — {line.strip()[:100]}")
        if violations:
            self.fail(
                "Main OpenSpec specs reference removed surface — "
                "update to docker/docker-constructor.py contract:\n"
                + "\n".join(violations)
            )


# ════════════════════════════════════════════════════════════════════
# 13.1j — Maintained configuration files
# ════════════════════════════════════════════════════════════════════

_CFG_FORBIDDEN: list[tuple[str, re.Pattern[str]]] = [
    (
        "versions.py in config",
        re.compile(r"versions\.py"),
    ),
    (
        "build_wrapper in config",
        re.compile(r"build_wrapper"),
    ),
    (
        "launch-pi in config",
        re.compile(r"launch-pi"),
    ),
    (
        "inf-splitter in config",
        re.compile(r"inf-splitter"),
    ),
    (
        "inf-splitter.toml path in config",
        re.compile(r"inf-splitter\.toml"),
    ),
    (
        "docker-compose in config",
        re.compile(r"docker-compose"),
    ),
    (
        "COMPOSE_FILE in config",
        re.compile(r"COMPOSE_FILE"),
    ),
    (
        "generated compose fragment in config",
        re.compile(r"compose\.proj\d+\.yml"),
    ),
    (
        "PROXY_PORT in config",
        re.compile(r"PROXY_PORT"),
    ),
]


def _cfg_files() -> dict[str, pathlib.Path]:
    """Return maintained configuration files (only reviewed inventory)."""
    result: dict[str, pathlib.Path] = {}
    for name in _CFG_SCAN_FILES:
        p = REPO / name
        if p.is_file():
            result[name] = p
    return result


class TestNoStaleConfigReferences(unittest.TestCase):
    """Maintained configuration files MUST NOT reference removed
    CLIs, Compose, or inf-splitter."""

    def test_no_stale_references_in_config_files(self) -> None:
        violations: list[str] = []
        for rel, path in sorted(_cfg_files().items()):
            for lineno, line in _lines(path):
                for label, pat in _CFG_FORBIDDEN:
                    m = pat.search(line)
                    if not m:
                        continue
                    violations.append(f"{rel}:{lineno}: {label} — {line.strip()[:100]}")
        if violations:
            self.fail(
                "Maintained configuration files reference removed surface:\n"
                + "\n".join(violations)
            )


# ════════════════════════════════════════════════════════════════════
# 13.2a — Shell script command contract (execution-aware)
# ════════════════════════════════════════════════════════════════════

# Shell scripts must invoke ``docker/docker-constructor.py`` (the
# facade), not old CLIs.  We track variable assignments and detect
# only lines that *execute* an old CLI — mere constants or variable
# definitions are not flagged.

_OLD_CLI_NAMES: frozenset[str] = frozenset(
    {
        "versions.py",
        "build_wrapper.py",
        "launch-pi.py",
        "gen-models-json.py",
    }
)

_FACADE_NAME: str = "docker-constructor.py"

# Shell scripts excluded from the command-contract scan.
_SH_COMMAND_EXCLUDED = frozenset(
    {
        # Being removed in 13.4
        "docker/apply-rootless-port-forward.sh",
    }
)


def _has_old_cli_name(s: str) -> str | None:
    """If *s* contains an old CLI name, return the matched name."""
    for name in _OLD_CLI_NAMES:
        if name in s:
            return name
    return None


def _parse_sh_assignments(
    lines: list[tuple[int, str]],
) -> dict[str, str]:
    """Collect simple ``VAR=value`` assignments (no line-continuation
    or complex expansions)."""
    assign: dict[str, str] = {}
    for _lineno, line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = re.match(r"^(\w+)=(.+?)(?:\s+#.*)?$", stripped)
        if m:
            val = m.group(2).strip()
            # Strip exactly one layer of matching quotes
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                val = val[1:-1]
            assign[m.group(1)] = val
    return assign


def _find_sh_executions(
    lines: list[tuple[int, str]],
    assignments: dict[str, str],
) -> list[tuple[int, str, str]]:
    """Return ``[(lineno, label, snippet), ...]`` for lines that
    *execute* an old CLI — direct path, interpreter invocation,
    or variable expansion in command position."""
    violations: list[tuple[int, str, str]] = []

    for lineno, line in lines:
        stripped = line.strip()
        # Skip blanks, comments, shebangs, and function definitions
        if not stripped or stripped.startswith("#") or stripped.startswith("#!"):
            continue
        # Skip lines that are purely function definitions
        if re.match(r"^\w+\(\)\s*\{", stripped):
            continue

        # Merge file-level assignments + this line's inline prefixes
        local_assign = dict(assignments)
        cmd_part = stripped
        # Capture inline ``VAR=value `` prefixes (e.g. ``A=1 B=2 cmd``)
        while True:
            m = re.match(
                r'^(\w+)=("[^"]*"|\'[^\']*\'|\S+)\s+(.*)',
                cmd_part,
            )
            if not m:
                break
            val = m.group(2)
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                val = val[1:-1]
            local_assign[m.group(1)] = val
            cmd_part = m.group(3)

        # Remove ``exec`` prefix
        if cmd_part.startswith("exec "):
            cmd_part = cmd_part[5:].lstrip()

        if not cmd_part:
            continue

        # Skip standalone variable assignments (no command word follows)
        if re.match(r'^\w+=("[^"]*"|\'[^\']*\'|\S+)$', cmd_part):
            continue

        # ── Direct command word ──
        first_word = cmd_part.split()[0] if cmd_part.split() else ""
        var_match = re.match(
            r'^\$\{(\w+)\}$|^\$(\w+)$|^"\$(\w+)"$',
            first_word,
        )
        if var_match:
            vn = var_match.group(1) or var_match.group(2) or var_match.group(3)
            if vn in local_assign:
                cli = _has_old_cli_name(local_assign[vn])
                if cli is not None:
                    violations.append((lineno, f"{cli} via ${vn}", line.strip()[:100]))
                    continue
        else:
            cli = _has_old_cli_name(first_word)
            if cli is not None:
                violations.append((lineno, f"{cli} as command", line.strip()[:100]))
                continue

        # ── Interpreter invocation ──
        m = re.match(r"(python3|python|bash|sh)\s+(.+)", cmd_part)
        if m:
            rest = m.group(2)
            first_arg = rest.strip().split()[0] if rest.strip() else ""
            cli = _has_old_cli_name(first_arg)
            if cli is not None:
                violations.append(
                    (lineno, f"{cli} via {m.group(1)}", line.strip()[:100])
                )
                continue
            vm = re.match(
                r'^\$\{(\w+)\}$|^\$(\w+)$|^"\$(\w+)"$',
                first_arg,
            )
            if vm:
                vn = vm.group(1) or vm.group(2) or vm.group(3)
                if vn in local_assign:
                    cli = _has_old_cli_name(local_assign[vn])
                    if cli is not None:
                        violations.append(
                            (
                                lineno,
                                f"{cli} via {m.group(1)} ${vn}",
                                line.strip()[:100],
                            )
                        )
                        continue

        # ── Command substitution ``$(...)`` ──
        for sub in re.finditer(r"\$\((.+?)\)", stripped):
            _check_inner_command(
                sub.group(1),
                lineno,
                "$(...)",
                local_assign,
                line,
                violations,
            )

        # ── Backtick substitution `` `...` `` ──
        for sub in re.finditer(r"`([^`]+)`", stripped):
            _check_inner_command(
                sub.group(1),
                lineno,
                "`...`",
                local_assign,
                line,
                violations,
            )

    return violations


def _check_inner_command(
    inner: str,
    lineno: int,
    ctx: str,
    local_assign: dict[str, str],
    line: str,
    violations: list[tuple[int, str, str]],
) -> None:
    """Check an inner command (from ``$(...)`` or backticks) for
    old-CLI execution."""
    inner = inner.strip()
    # Interpreter inside subshell
    im = re.match(r"(python3|python|bash|sh)\s+(.+)", inner)
    if im:
        first_arg = im.group(2).strip().split()[0] if im.group(2).strip() else ""
        cli = _has_old_cli_name(first_arg)
        if cli is not None:
            violations.append((lineno, f"{cli} via {ctx} subshell", line.strip()[:100]))
            return
        vm2 = re.match(
            r'^\$\{(\w+)\}$|^\$(\w+)$|^"\$(\w+)"$',
            first_arg,
        )
        if vm2:
            vn2 = vm2.group(1) or vm2.group(2) or vm2.group(3)
            if vn2 in local_assign:
                if _has_old_cli_name(local_assign[vn2]) is not None:
                    violations.append(
                        (
                            lineno,
                            f"{_has_old_cli_name(local_assign[vn2])} via {ctx} ${vn2}",
                            line.strip()[:100],
                        )
                    )
                    return
    # Direct old CLI path inside subshell
    for word in inner.split():
        cli = _has_old_cli_name(word)
        if cli is not None:
            violations.append((lineno, f"{cli} via {ctx} subshell", line.strip()[:100]))
            return


class TestShellScriptsInvokeFacadeNotOldCLIs(unittest.TestCase):
    """Shell scripts MUST invoke ``docker/docker-constructor.py``,
    not old CLIs.  Only actual process-execution lines are flagged
    — variable assignments and constants are not execution."""

    def test_shell_scripts_use_facade(self) -> None:
        violations: list[str] = []
        for rel, path in sorted(_sh_files().items()):
            if rel in _SH_COMMAND_EXCLUDED:
                continue
            file_lines = list(_lines(path))
            assignments = _parse_sh_assignments(file_lines)
            for lineno, label, snippet in _find_sh_executions(
                file_lines,
                assignments,
            ):
                violations.append(f"{rel}:{lineno}: {label} — {snippet}")
        if violations:
            self.fail(
                "Shell scripts invoke removed CLIs — use "
                "docker/docker-constructor.py instead:\n" + "\n".join(violations)
            )


# ════════════════════════════════════════════════════════════════════
# 13.2b — Internal test import contract (AST-based)
# ════════════════════════════════════════════════════════════════════

# Internal tests MUST import Python APIs directly, not invoke CLIs
# via subprocess or os.system.  We use AST analysis to detect actual
# process execution — descriptive string literals in assertions,
# docstrings, or fixtures are not flagged.

import ast as _ast  # noqa: E402

# Test files excluded from the subprocess-contract scan.
_TEST_SUBPROCESS_EXCLUDED: frozenset[str] = frozenset()


def _contains_any_cli_name(s: str) -> str | None:
    """If *s* contains an old CLI name or the facade, return it."""
    for name in _OLD_CLI_NAMES:
        if name in s:
            return name
    if _FACADE_NAME in s:
        return _FACADE_NAME
    return None


def _extract_string_literal(node: _ast.expr) -> str | None:
    """Return the string value of a simple ``ast.Constant(str)`` node."""
    if isinstance(node, _ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _extract_all_strings(node: _ast.expr) -> list[str]:
    """Recursively extract all string literals from an expression.
    Handles ``Constant``, ``List``, ``Tuple``, ``Call(str, ...)``,
    ``BinOp(Div)`` (Path-like joins), and ``JoinedStr``."""
    strings: list[str] = []
    if isinstance(node, _ast.Constant) and isinstance(node.value, str):
        strings.append(node.value)
    elif isinstance(node, (_ast.List, _ast.Tuple)):
        for elt in node.elts:
            strings.extend(_extract_all_strings(elt))
    elif isinstance(node, _ast.Call):
        for arg in node.args:
            strings.extend(_extract_all_strings(arg))
    elif isinstance(node, _ast.BinOp) and isinstance(node.op, _ast.Div):
        strings.extend(_extract_all_strings(node.left))
        strings.extend(_extract_all_strings(node.right))
    elif isinstance(node, _ast.JoinedStr):
        for value in node.values:
            if isinstance(value, _ast.Constant) and isinstance(value.value, str):
                strings.append(value.value)
    return strings


def _collect_cli_path_vars(tree: _ast.AST) -> dict[str, str]:
    """Collect module-level ``Name = expr`` where *expr* contains a
    CLI name."""
    result: dict[str, str] = {}
    for node in _ast.walk(tree):
        if not isinstance(node, _ast.Assign):
            continue
        strings = _extract_all_strings(node.value)
        for s in strings:
            cli = _contains_any_cli_name(s)
            if cli is not None:
                for target in node.targets:
                    if isinstance(target, _ast.Name):
                        result[target.id] = cli
                        break
    return result


def _iter_assign_targets(
    node: _ast.Assign,
) -> list[tuple[_ast.expr, str, list[str]]]:
    """Yield ``(target, name, extracted_strings)`` for each target
    in an assignment statement."""
    strings = _extract_all_strings(node.value)
    result: list[tuple[_ast.expr, str, list[str]]] = []
    for target in node.targets:
        if isinstance(target, _ast.Name):
            result.append((target, target.id, strings))
    return result


# ── Modules whose methods can launch external processes ──
_PROCESS_MODULES: dict[str, frozenset[str]] = {
    "subprocess": frozenset(
        {
            "run",
            "call",
            "check_call",
            "check_output",
            "Popen",
        }
    ),
    "os": frozenset(
        {
            "system",
            "popen",
            # exec* family — replaces current process
            "execl",
            "execle",
            "execlp",
            "execlpe",
            "execv",
            "execve",
            "execvp",
            "execvpe",
            # spawn* family
            "spawnl",
            "spawnle",
            "spawnlp",
            "spawnlpe",
            "spawnv",
            "spawnve",
            "spawnvp",
            "spawnvpe",
            # posix_spawn* family
            "posix_spawn",
            "posix_spawnp",
        }
    ),
}

# Set of os.exec* names whose first argument is the *path*, not argv[0].
_OS_EXEC_NAMES: frozenset[str] = frozenset(
    {
        "execl",
        "execle",
        "execlp",
        "execlpe",
        "execv",
        "execve",
        "execvp",
        "execvpe",
    }
)


def _resolve_call_target(
    node: _ast.Call,
    module_aliases: dict[str, str],
    direct_imports: dict[str, tuple[str, str]],
) -> tuple[str | None, str | None]:
    """Resolve a call to ``(module_name, method_name)`` or
    ``(None, None)`` if this is not a tracked process-launch call.

    Handles three import styles:
    - ``import subprocess`` → ``subprocess.run(...)``
    - ``import subprocess as sp`` → ``sp.run(...)``
    - ``from subprocess import run`` → ``run(...)``
    - ``from subprocess import Popen as Pop`` → ``Pop(...)``
    """
    # Style A: module.method(...) or alias.method(...)
    if isinstance(node.func, _ast.Attribute) and isinstance(
        node.func.value,
        _ast.Name,
    ):
        name = node.func.value.id
        method = node.func.attr
        if name in _PROCESS_MODULES:
            return (name, method)
        if name in module_aliases:
            mod = module_aliases[name]
            if mod in _PROCESS_MODULES:
                return (mod, method)
    # Style B: bare_name(...) from ``from module import name``
    elif isinstance(node.func, _ast.Name):
        name = node.func.id
        if name in direct_imports:
            mod, orig_method = direct_imports[name]
            return (mod, orig_method)
    return (None, None)


class _SubprocessCLIFinder(_ast.NodeVisitor):
    """Walk the AST and record every process-launch call whose argv
    refers to an old CLI or the facade.

    Covers ``import subprocess``, ``import subprocess as sp``,
    ``from subprocess import run/Popen/...``, ``os.system``,
    ``os.exec*``, ``os.spawn*``, and ``os.posix_spawn*``."""

    def __init__(self, var_values: dict[str, str]) -> None:
        super().__init__()
        self._var_values = var_values
        self._local_vars: dict[str, str] = {}
        # module_aliases: alias → canonical module name
        self._module_aliases: dict[str, str] = {}
        # direct_imports: bare name → (canonical_module, original_method)
        self._direct_imports: dict[str, tuple[str, str]] = {}
        self.violations: list[tuple[int, str]] = []

    # ── Import tracking ──

    def visit_Import(self, node: _ast.Import) -> None:  # type: ignore[override]
        for alias in node.names:
            mod = alias.name
            if mod not in _PROCESS_MODULES:
                continue
            asname = alias.asname or mod
            self._module_aliases[asname] = mod
        self.generic_visit(node)

    def visit_ImportFrom(  # type: ignore[override]
        self,
        node: _ast.ImportFrom,
    ) -> None:
        mod = node.module or ""
        if mod not in _PROCESS_MODULES:
            self.generic_visit(node)
            return
        methods = _PROCESS_MODULES[mod]
        for alias in node.names:
            if alias.name not in methods:
                continue
            asname = alias.asname or alias.name
            # Store (asname → (mod, original_name)) so we can
            # recover "Popen" from "Pop" in ``from subprocess
            # import Popen as Pop``.
            self._direct_imports[asname] = (mod, alias.name)
        self.generic_visit(node)

    # ── Function-local variable tracking ──

    def visit_FunctionDef(  # type: ignore[override]
        self,
        node: _ast.FunctionDef,
    ) -> None:
        saved = dict(self._local_vars)
        self._local_vars.clear()
        for stmt in node.body:
            if isinstance(stmt, _ast.Assign):
                for _target, name, strings in _iter_assign_targets(stmt):
                    for s in strings:
                        cli = _contains_any_cli_name(s)
                        if cli is not None:
                            self._local_vars[name] = cli
                            break
        self.generic_visit(node)
        self._local_vars = saved

    # ── Call detection ──

    def visit_Call(  # type: ignore[override]
        self,
        node: _ast.Call,
    ) -> None:
        mod, method = _resolve_call_target(
            node,
            self._module_aliases,
            self._direct_imports,
        )
        if mod is not None and method is not None:
            methods = _PROCESS_MODULES.get(mod, frozenset())
            if method in methods:
                # Scan every positional argument for CLI references.
                # For most calls argv is args[0], but os.exec* / os.spawn*
                # take (path, argv, ...) where the CLI name may appear in
                # args[1] (the argument list).  Stop at the first match
                # to avoid duplicate reports for the same call.
                for arg in node.args:
                    if self._check_argv(arg, f"{mod}.{method}", node.lineno):
                        break
        self.generic_visit(node)

    # ── Argument inspection ──

    def _check_argv(
        self,
        arg: _ast.expr | None,
        call_name: str,
        lineno: int,
    ) -> bool:
        """Check *arg* for CLI references.  Return ``True`` if a
        violation was recorded."""
        if arg is None:
            return False
        # Direct string literal
        if (s := _extract_string_literal(arg)) is not None:
            name = _contains_any_cli_name(s)
            if name is not None:
                self.violations.append((lineno, f"{call_name}('...{name}...')"))
                return True
        # Variable name — lookup in local then module-level table
        if isinstance(arg, _ast.Name):
            cli = self._local_vars.get(arg.id) or self._var_values.get(arg.id)
            if cli is not None:
                self.violations.append((lineno, f"{call_name}({arg.id} \u2192 {cli})"))
                return True
        # Extract all strings from the expression (handles str(Path/...))
        strings = _extract_all_strings(arg)
        for s in strings:
            name = _contains_any_cli_name(s)
            if name is not None:
                self.violations.append((lineno, f"{call_name}(...'{name}'...)"))
                return True
        return False


def _find_subprocess_cli_calls(
    filepath: pathlib.Path,
) -> list[tuple[int, str]]:
    """Return ``[(lineno, description), ...]`` for every
    process-launch call whose argv references an old CLI script or
    the facade."""
    try:
        tree = _ast.parse(filepath.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    var_values = _collect_cli_path_vars(tree)
    finder = _SubprocessCLIFinder(var_values)
    finder.visit(tree)
    return finder.violations


# ════════════════════════════════════════════════════════════════════
# 13.2b (cont.) — Fixture tests for the AST detector itself
# ════════════════════════════════════════════════════════════════════

import tempfile as _tempfile  # noqa: E402
import textwrap as _textwrap  # noqa: E402


class TestSubprocessCLIDetector(unittest.TestCase):
    """Focused fixture tests proving the AST detector flags real
    process invocations while allowing assertions, docstrings, and
    constants that merely mention CLI names."""

    def _check(
        self,
        code: str,
        *,
        expected: int = 0,
        label: str = "",
    ) -> list[tuple[int, str]]:
        """Parse *code* and return violations.
        Fails if the count does not match *expected*."""
        with _tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".py",
            delete=False,
        ) as f:
            f.write(_textwrap.dedent(code))
            tmp = pathlib.Path(f.name)
        try:
            violations = _find_subprocess_cli_calls(tmp)
        finally:
            tmp.unlink()
        if expected is not None and len(violations) != expected:
            self.fail(
                f"{label}: expected {expected} violation(s), got "
                f"{len(violations)}: {violations}"
            )
        return violations

    # ── Positive cases — MUST be caught ──

    def test_subprocess_run_literal(self) -> None:
        self._check(
            """\
            import subprocess
            subprocess.run(["python3", "docker/versions.py", "validate"])
            """,
            expected=1,
            label="subprocess.run with literal versions.py",
        )

    def test_subprocess_call_alias(self) -> None:
        self._check(
            """\
            import subprocess as sp
            sp.call(["docker/versions.py"])
            """,
            expected=1,
            label="subprocess.call via 'import as sp'",
        )

    def test_from_import_run(self) -> None:
        self._check(
            """\
            from subprocess import run
            run(["python3", "docker/build_wrapper.py"])
            """,
            expected=1,
            label="from subprocess import run",
        )

    def test_from_import_popen_as(self) -> None:
        self._check(
            """\
            from subprocess import Popen as Pop
            Pop(["launch-pi.py"])
            """,
            expected=1,
            label="from subprocess import Popen as Pop",
        )

    def test_subprocess_check_call(self) -> None:
        self._check(
            """\
            import subprocess
            subprocess.check_call(["docker/gen-models-json.py"])
            """,
            expected=1,
            label="subprocess.check_call",
        )

    def test_subprocess_check_output(self) -> None:
        self._check(
            """\
            import subprocess
            subprocess.check_output(["versions.py"])
            """,
            expected=1,
            label="subprocess.check_output",
        )

    def test_os_system(self) -> None:
        self._check(
            """\
            import os
            os.system("python3 docker/versions.py validate")
            """,
            expected=1,
            label="os.system",
        )

    def test_os_popen(self) -> None:
        self._check(
            """\
            import os
            os.popen("docker/versions.py")
            """,
            expected=1,
            label="os.popen",
        )

    def test_os_execv(self) -> None:
        self._check(
            """\
            import os
            os.execv("/usr/bin/python3", ["python3", "docker/versions.py"])
            """,
            expected=1,
            label="os.execv with versions.py",
        )

    def test_os_spawnv(self) -> None:
        self._check(
            """\
            import os
            os.spawnv(os.P_WAIT, "docker/build_wrapper.py", ["build_wrapper.py"])
            """,
            expected=1,
            label="os.spawnv",
        )

    def test_os_posix_spawn(self) -> None:
        self._check(
            """\
            import os
            os.posix_spawn("docker/versions.py", ["versions.py"], {})
            """,
            expected=1,
            label="os.posix_spawn",
        )

    def test_variable_indirection(self) -> None:
        self._check(
            """\
            import subprocess
            CMD = ["python3", "docker/versions.py", "validate"]
            subprocess.run(CMD)
            """,
            expected=1,
            label="subprocess.run via module-level var",
        )

    def test_str_path_div_indirection(self) -> None:
        self._check(
            """\
            import subprocess, sys
            from pathlib import Path
            subprocess.run([sys.executable, str(Path("docker") / "versions.py"), "validate"])
            """,
            expected=1,
            label="subprocess.run with str(Path/...)",
        )

    # ── Negative cases — must NOT be caught ──

    def test_assertion_mention_only(self) -> None:
        self._check(
            """\
            import unittest
            class T(unittest.TestCase):
                def test_it(self):
                    self.assertEqual(result, "docker/versions.py")
            """,
            expected=0,
            label="assertion mentioning versions.py (not execution)",
        )

    def test_docstring_mention_only(self) -> None:
        self._check(
            """\
            def helper():
                '''Runs docker/versions.py validate.'''
                pass
            """,
            expected=0,
            label="docstring mentioning versions.py (not execution)",
        )

    def test_constant_definition_only(self) -> None:
        self._check(
            """\
            _VERSIONS_PY = "docker/versions.py"
            _BUILD_WRAPPER = "docker/build_wrapper.py"
            """,
            expected=0,
            label="constant definitions (no subprocess call)",
        )

    def test_comment_only(self) -> None:
        self._check(
            """\
            # Old CLI: docker/versions.py was removed
            x = 1
            """,
            expected=0,
            label="comment mentioning versions.py",
        )

    def test_import_without_call(self) -> None:
        self._check(
            """\
            import subprocess
            import subprocess as sp
            from subprocess import run
            # None of these actually invoke a CLI
            x = subprocess  # type annotation-like usage
            """,
            expected=0,
            label="imports without actual calls",
        )

    def test_non_process_call(self) -> None:
        self._check(
            """\
            import subprocess
            result = subprocess.CompletedProcess(
                args=["docker/versions.py"], returncode=0
            )
            """,
            expected=0,
            label="subprocess.CompletedProcess (not a launcher)",
        )


class TestInternalTestsImportNotSubprocess(unittest.TestCase):
    """Internal tests MUST import Python modules directly, not
    invoke ``versions.py``, ``build_wrapper.py``, the facade, or
    other CLIs via ``subprocess`` or ``os.system``.

    Uses AST analysis to detect actual process execution —
    descriptive string literals in assertions, docstrings, or
    fixtures are not flagged."""

    def test_internal_tests_import_directly(self) -> None:
        violations: list[str] = []
        for rel, path in sorted(_py_files().items()):
            if rel in _TEST_SUBPROCESS_EXCLUDED:
                continue
            if not rel.startswith("tests/"):
                continue
            if rel == "tests/test_constructor_migration_contract.py":
                continue
            found = _find_subprocess_cli_calls(path)
            for lineno, desc in found:
                violations.append(f"{rel}:{lineno}: {desc}")
        if violations:
            self.fail(
                "Internal tests must import Python APIs, not invoke "
                "CLIs via subprocess:\n" + "\n".join(violations)
            )


# ════════════════════════════════════════════════════════════════════
# Dockerfile runtime-isolation contract
# ════════════════════════════════════════════════════════════════════

# Dockerfile patterns that bake non-runtime artifacts into the image.
_DOCKERFILE_BANNED_COPY = [
    (
        "docker-constructor.toml baked into image",
        re.compile(r"COPY.*docker-constructor\.toml.*\/usr\/"),
    ),
    (
        "docker/versions.py baked into image",
        re.compile(r"COPY\s+docker\/versions\.py\s+\/usr\/"),
    ),
]

# Shell runtime patterns that reference removed runtime paths.
_SHELL_RUNTIME_BANNED = [
    (
        "/usr/local/share/pi-cli/docker-constructor.toml",
        re.compile(r"/usr/local/share/pi-cli/docker-constructor\.toml"),
    ),
]


class TestDockerfileNoBakedInventory(unittest.TestCase):
    """Dockerfile MUST NOT COPY the reviewed inventory or the old
    ``docker/versions.py`` CLI into the runtime image."""

    def test_dockerfile_no_baked_inventory(self) -> None:
        dockerfile = REPO / "Dockerfile"
        if not dockerfile.is_file():
            return
        violations: list[str] = []
        for lineno, line in _lines(dockerfile):
            for label, pat in _DOCKERFILE_BANNED_COPY:
                if pat.search(line):
                    violations.append(f"Dockerfile:{lineno}: {label} — {line.strip()}")
        if violations:
            self.fail(
                "Dockerfile bakes removed artifacts into the runtime image:\n"
                + "\n".join(violations)
            )


class TestShellRuntimeNoStaleInventoryPaths(unittest.TestCase):
    """Shell scripts MUST NOT reference the removed baked-in inventory
    path ``/usr/local/share/pi-cli/docker-constructor.toml``."""

    def test_shell_no_stale_inventory_paths(self) -> None:
        violations: list[str] = []
        for rel, path in sorted(_sh_files().items()):
            for lineno, line in _lines(path):
                for label, pat in _SHELL_RUNTIME_BANNED:
                    if pat.search(line):
                        violations.append(f"{rel}:{lineno}: {label} — {line.strip()}")
        if violations:
            self.fail(
                "Shell scripts reference removed baked-in inventory path:\n"
                + "\n".join(violations)
            )


# ════════════════════════════════════════════════════════════════════
# install-pi-extensions.sh wrapper-removal contract
# ════════════════════════════════════════════════════════════════════

_INSTALL_PI_EXTENSIONS_DOCKERFILE_BANNED = [
    (
        "install-pi-extensions.sh COPY into image",
        re.compile(r"COPY\s+docker/install-pi-extensions\.sh"),
    ),
    (
        "install-pi-extensions.sh chmod in image",
        re.compile(r"chmod\s+\d+\s+/home/dev/install-pi-extensions\.sh"),
    ),
]

_INSTALL_PI_EXTENSIONS_SHELL_BANNED = [
    (
        "/home/dev/install-pi-extensions.sh referenced",
        re.compile(r"/home/dev/install-pi-extensions\.sh"),
    ),
]


class TestDockerfileNoInstallPiExtensionsWrapper(unittest.TestCase):
    """Dockerfile MUST NOT COPY ``docker/install-pi-extensions.sh``
    into the runtime image."""

    def test_dockerfile_no_install_pi_extensions_wrapper(self) -> None:
        dockerfile = REPO / "Dockerfile"
        if not dockerfile.is_file():
            return
        violations: list[str] = []
        for lineno, line in _lines(dockerfile):
            for label, pat in _INSTALL_PI_EXTENSIONS_DOCKERFILE_BANNED:
                if pat.search(line):
                    violations.append(f"Dockerfile:{lineno}: {label} — {line.strip()}")
        if violations:
            self.fail(
                "Dockerfile still copies the obsolete "
                "install-pi-extensions.sh wrapper:\n" + "\n".join(violations)
            )


class TestShellScriptsNoInstallPiExtensionsWrapper(unittest.TestCase):
    """Shell scripts MUST NOT reference the removed
    ``/home/dev/install-pi-extensions.sh`` image path."""

    def test_shell_scripts_no_install_pi_extensions_wrapper(self) -> None:
        violations: list[str] = []
        for rel, path in sorted(_sh_files().items()):
            for lineno, line in _lines(path):
                for label, pat in _INSTALL_PI_EXTENSIONS_SHELL_BANNED:
                    if not pat.search(line):
                        continue
                    # Negative checks asserting the path is absent are
                    # legitimate — they enforce the removal contract.
                    if "must be absent" in line or "test ! -f" in line:
                        continue
                    violations.append(f"{rel}:{lineno}: {label} — {line.strip()}")
        if violations:
            self.fail(
                "Shell scripts reference the obsolete "
                "/home/dev/install-pi-extensions.sh path:\n" + "\n".join(violations)
            )


class TestInstallPiExtensionsScriptFileAbsent(unittest.TestCase):
    """The wrapper script ``docker/install-pi-extensions.sh`` MUST NOT
    exist in the repository source tree."""

    def test_install_pi_extensions_script_file_absent(self) -> None:
        wrapper = REPO / "docker" / "install-pi-extensions.sh"
        if wrapper.is_file():
            self.fail(
                f"{wrapper} still exists in the repository; it must be "
                "removed as part of this change"
            )

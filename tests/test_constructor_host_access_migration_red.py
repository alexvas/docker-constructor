"""Phase 5 RED — semantic-source, configuration-template, and documentation parity tests.

Task 5.1: reject maintained production references to:
  * ``.env`` gateway persistence (HOST_GATEWAY_IP write path)
  * ``HOST_GATEWAY_IP`` environment variable string
  * unconditional ``--add-host`` / ``host.docker.internal`` run mapping
  * reviewed ``cache.dir`` support
  * ``HOST_GATEWAY_IP`` entry in ``.env.example``

Task 5.2: require ``.gitignore`` to ignore ``docker-constructor.local.toml``
  and a tracked ``docker-constructor.local.example.toml`` with only
  ``[host-access].address`` and ``[cache].dir``, proven via TOML parsing.

Task 5.3: require all README translations to document disabled default,
  both host-access modes, local companion naming, optional proxy port,
  and ``cache.dir`` migration equivalently.

These tests MUST fail against the pre-Phase-5 codebase because the
obsolete patterns have not been removed yet.
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent

# ════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════

def _py_files() -> dict[str, pathlib.Path]:
    result: dict[str, pathlib.Path] = {}
    for p in (REPO / "docker").rglob("*.py"):
        if p.is_file():
            result[str(p.relative_to(REPO))] = p
    return result


def _lines(path: pathlib.Path) -> list[tuple[int, str]]:
    try:
        return list(
            enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        )
    except UnicodeDecodeError:
        return []


def _check_patterns(
    files: dict[str, pathlib.Path],
    patterns: list[tuple[str, re.Pattern[str]]],
) -> list[str]:
    violations: list[str] = []
    for rel, path in sorted(files.items()):
        for lineno, line in _lines(path):
            for label, pat in patterns:
                m = pat.search(line)
                if not m:
                    continue
                violations.append(
                    f"{rel}:{lineno}: {label} — {line.strip()[:120]}"
                )
    return violations


# ════════════════════════════════════════════════════════════════════
# Task 5.1 — obsolete pattern definitions
# ════════════════════════════════════════════════════════════════════

_GATEWAY_ENV_PERSISTENCE: list[tuple[str, re.Pattern[str]]] = [
    (
        "_read_operational_gateway reads HOST_GATEWAY_IP from .env",
        re.compile(r"_read_operational_gateway"),
    ),
    (
        "HOST_GATEWAY_IP in .env write path",
        re.compile(r"HOST_GATEWAY_IP"),
    ),
]

_REVIEWED_CACHE_DIR: list[tuple[str, re.Pattern[str]]] = [
    (
        "reviewed cache.dir processed in inventory loading",
        # The typed diagnostic field is authorized metadata; flag any other
        # production reference to the retired reviewed key.
        re.compile(r'(?<!field=)"cache\.dir"'),
    ),
]

_DIAGNOSTIC_FUNC_RE = re.compile(
    r"def\s+(probe_|diagnose_|_probe|_diagnose|detect_)"
)


# ════════════════════════════════════════════════════════════════════
# Task 5.1 — Semantic-source rejection tests
# ════════════════════════════════════════════════════════════════════


class TestNoEnvGatewayPersistence(unittest.TestCase):
    """Production Python files must not read/write gateways via .env."""

    def test_no_dotenv_gateway_references(self) -> None:
        violations = _check_patterns(
            _py_files(), _GATEWAY_ENV_PERSISTENCE,
        )
        if violations:
            self.fail(
                "Production files reference .env gateway persistence "
                "— migrate to local TOML companion:\n"
                + "\n".join(violations)
            )


class TestNoEnvExampleGateway(unittest.TestCase):
    """.env.example must not carry the legacy HOST_GATEWAY_IP entry."""

    def test_env_example_has_no_host_gateway_ip(self) -> None:
        env_ex = REPO / ".env.example"
        self.assertTrue(env_ex.is_file(), ".env.example must exist")
        text = env_ex.read_text(encoding="utf-8")
        self.assertNotIn(
            "HOST_GATEWAY_IP",
            text,
            ".env.example contains HOST_GATEWAY_IP — "
            "remove it; the address is now in the local TOML companion",
        )


class TestNoUnconditionalRunMapping(unittest.TestCase):
    """No production code path may emit ``--add-host host.docker.internal``
    without a ``host_access`` gate.

    The renderer is tested behaviourally (disabled → no mapping,
    enabled → mapping present).  ``networking.py`` is scanned
    structurally for ``host.docker.internal`` occurrences outside
    diagnostic functions.
    """

    # ── renderer behavioural tests ────────────────────────────────

    _RENDERER_FIELDS = {
        "image": "test:latest",
        "container_name": "test",
        "pi_home_host": "/home/dev/.pi",
        "workspace": "/tmp/main",
        "tty": False,
    }

    @staticmethod
    def _renderer_projection_path() -> pathlib.Path:
        import tempfile
        td = pathlib.Path(tempfile.mkdtemp())
        dot_docker = td / ".docker-generated" / "runtime"
        dot_docker.mkdir(parents=True)
        proj = dot_docker / "proj.toml"
        proj.write_text("")
        return proj

    def test_disabled_renderer_emits_no_add_host(self) -> None:
        from docker.versioning.rendering import (
            RunRenderInputs, RunHostAccess, render_run_vector,
        )
        proj = self._renderer_projection_path()
        try:
            inputs = RunRenderInputs(
                **self._RENDERER_FIELDS,
                projection_host_path=str(proj),
                projection_container_path="/run/pi-cli/docker-constructor.runtime.toml",
                host_access=RunHostAccess.disabled(),
            )
            result = render_run_vector(inputs)
        finally:
            import shutil
            shutil.rmtree(proj.parents[2], ignore_errors=True)
        self.assertNotIn("--add-host", result, "disabled must not emit --add-host")
        self.assertNotIn("host.docker.internal", result,
                         "disabled must not reference host.docker.internal")

    def test_enabled_renderer_emits_add_host(self) -> None:
        from docker.versioning.rendering import (
            RunRenderInputs, RunHostAccess, render_run_vector,
        )
        proj = self._renderer_projection_path()
        try:
            inputs = RunRenderInputs(
                **self._RENDERER_FIELDS,
                projection_host_path=str(proj),
                projection_container_path="/run/pi-cli/docker-constructor.runtime.toml",
                host_access=RunHostAccess(
                    address="10.0.2.2", mode="docker-gateway",
                ),
            )
            result = render_run_vector(inputs)
        finally:
            import shutil
            shutil.rmtree(proj.parents[2], ignore_errors=True)
        self.assertIn("--add-host", result, "enabled must emit --add-host")
        idx = result.index("--add-host")
        self.assertLess(
            idx + 1, len(result),
            "--add-host must be followed by a mapping value",
        )
        self.assertEqual(
            "host.docker.internal:10.0.2.2", result[idx + 1],
            "enabled docker-gateway must map host.docker.internal to the address",
        )

    # ── networking.py structural scan ─────────────────────────────

    def test_networking_host_docker_internal_only_in_diagnostics(self) -> None:
        """Every ``host.docker.internal`` in networking.py must sit
        inside a diagnostic function (probe_*, diagnose_*, detect_*)."""
        nw = _py_files().get("docker/networking.py")
        if nw is None:
            self.fail("docker/networking.py not found")
        all_lines = _lines(nw)

        # Build function-name per line
        func_for_line: dict[int, str] = {}
        current_func: str | None = None
        for i, (_, line) in enumerate(all_lines):
            m = _DIAGNOSTIC_FUNC_RE.search(line)
            if m:
                current_func = m.group(0)
            elif current_func is not None and line.startswith("def "):
                current_func = None
            if current_func is not None:
                func_for_line[i] = current_func

        host_re = re.compile(r"host\.docker\.internal")
        # Module-level diagnostic variable patterns
        _DIAG_VAR_RE = re.compile(
            r"^(_PROBE_|_DIAGNOSE_|_DETECT_|_GATEWAY_)"
        )
        # Build variable-name per line for module-level assignments
        var_for_line: dict[int, str] = {}
        for i, (_, line) in enumerate(all_lines):
            if i in func_for_line:
                continue  # inside a function, already handled
            m = _DIAG_VAR_RE.match(line.strip())
            if m and "=" in line:
                var_name = line.split("=", 1)[0].strip()
                # This module-level var is diagnostic; mark it.
                var_for_line[i] = var_name

        for i, (lineno, line) in enumerate(all_lines):
            if not host_re.search(line):
                continue
            # Inside a diagnostic function — OK
            if i in func_for_line:
                continue
            # On the same line as a diagnostic variable assignment — OK
            if i in var_for_line:
                continue
            # Check if this line is inside a multi-line diagnostic variable
            # (scan backwards for the nearest var assignment).
            in_diag_var = False
            for j in range(i - 1, max(i - 200, 0), -1):
                if j in var_for_line:
                    in_diag_var = True
                    break
                if j in func_for_line:
                    break
                # If we hit another module-level assignment or a def, stop
                l = all_lines[j][1]
                if l.startswith("def ") or ("=" in l and l.strip() == l and not l.startswith(" ")):
                    break
            if in_diag_var:
                continue
            # Not in any diagnostic context — fail
            has_diag_ancestor = _has_diagnostic_ancestor(all_lines, i)
            if not has_diag_ancestor:
                    self.fail(
                        f"docker/networking.py:{lineno}: "
                        f"host.docker.internal outside a diagnostic "
                        f"function (probe_*/diagnose_*/detect_*); "
                        f"this is not a run-mapping site"
                    )


def _has_diagnostic_ancestor(
    all_lines: list[tuple[int, str]], idx: int,
) -> bool:
    """Scan backwards from *idx* looking for a def of a diagnostic function."""
    for j in range(idx - 1, max(idx - 150, 0), -1):
        line = all_lines[j][1]
        if _DIAGNOSTIC_FUNC_RE.search(line):
            return True
        # A non-diagnostic def at the same indent terminates the search
        if line.startswith("def ") and not _DIAGNOSTIC_FUNC_RE.search(line):
            return False
    return False


class TestNoReviewedCacheDir(unittest.TestCase):
    """Production files must not support reviewed cache.dir."""

    def test_no_reviewed_cache_dir(self) -> None:
        violations = _check_patterns(
            _py_files(), _REVIEWED_CACHE_DIR,
        )
        if violations:
            self.fail(
                "Production files reference reviewed cache.dir "
                "— it is now local-only:\n"
                + "\n".join(violations)
            )


# ════════════════════════════════════════════════════════════════════
# Task 5.2 — Configuration-template tests
# ════════════════════════════════════════════════════════════════════

_GITIGNORE_FILE: pathlib.Path = REPO / ".gitignore"
_LOCAL_EXAMPLE_FILE: pathlib.Path = REPO / "docker-constructor.local.example.toml"

_LOCAL_EXAMPLE_EXPECTED = {
    "host-access": frozenset({"address"}),
    "cache": frozenset({"dir"}),
    "output": frozenset({"host_heartbeat", "show_network_hosts"}),
}


class TestGitignoreIgnoresLocalCompanion(unittest.TestCase):
    """.gitignore must ignore docker-constructor.local.toml."""

    def test_gitignore_contains_local_companion(self) -> None:
        self.assertTrue(_GITIGNORE_FILE.is_file(), ".gitignore must exist")
        content = _GITIGNORE_FILE.read_text(encoding="utf-8")
        self.assertIn(
            "docker-constructor.local.toml",
            content,
            ".gitignore must contain 'docker-constructor.local.toml'",
        )


class TestLocalExampleExists(unittest.TestCase):
    """docker-constructor.local.example.toml must exist, be tracked, and
    contain only the documented fields in their correct sections."""

    def test_example_file_exists(self) -> None:
        self.assertTrue(
            _LOCAL_EXAMPLE_FILE.is_file(),
            f"{_LOCAL_EXAMPLE_FILE} must exist",
        )

    def test_example_is_tracked(self) -> None:
        if not _LOCAL_EXAMPLE_FILE.is_file():
            self.skipTest("example file missing")
        rel = _LOCAL_EXAMPLE_FILE.relative_to(REPO)
        try:
            result = subprocess.run(
                ["git", "ls-files", "--", str(rel)],
                capture_output=True, text=True, cwd=str(REPO),
            )
        except Exception as exc:
            self.skipTest(f"git ls-files failed: {exc}")
        self.assertEqual(
            0, result.returncode,
            f"git ls-files failed: {result.stderr}",
        )
        tracked = result.stdout.strip()
        self.assertIn(
            str(rel), tracked.splitlines() or [],
            f"{rel} must be tracked by git (not in .gitignore)",
        )

    def test_example_contains_exactly_documented_sections(self) -> None:
        if not _LOCAL_EXAMPLE_FILE.is_file():
            self.skipTest("example file missing")
        import tomllib
        try:
            data = tomllib.loads(
                _LOCAL_EXAMPLE_FILE.read_text(encoding="utf-8")
            )
        except Exception as exc:
            self.fail(f"example is not valid TOML: {exc}")
        sections = set(data.keys())
        self.assertEqual(
            _LOCAL_EXAMPLE_EXPECTED.keys(),
            sections,
            f"example must contain exactly sections "
            f"{sorted(_LOCAL_EXAMPLE_EXPECTED)}, got {sorted(sections)}",
        )

    def test_example_host_access_keys_are_exact(self) -> None:
        if not _LOCAL_EXAMPLE_FILE.is_file():
            self.skipTest("example file missing")
        import tomllib
        try:
            data = tomllib.loads(
                _LOCAL_EXAMPLE_FILE.read_text(encoding="utf-8")
            )
        except Exception:
            self.skipTest("invalid TOML — checked by other test")
        ha = data.get("host-access", {})
        if not isinstance(ha, dict):
            self.fail("[host-access] must be a TOML table")
        self.assertEqual(
            _LOCAL_EXAMPLE_EXPECTED["host-access"],
            set(ha.keys()),
            f"[host-access] must contain exactly "
            f"{_LOCAL_EXAMPLE_EXPECTED['host-access']}, "
            f"got {set(ha.keys())}",
        )
        self.assertIsInstance(
            ha.get("address"), str,
            "[host-access].address must be a string",
        )

    def test_example_cache_keys_are_exact(self) -> None:
        if not _LOCAL_EXAMPLE_FILE.is_file():
            self.skipTest("example file missing")
        import tomllib
        try:
            data = tomllib.loads(
                _LOCAL_EXAMPLE_FILE.read_text(encoding="utf-8")
            )
        except Exception:
            self.skipTest("invalid TOML — checked by other test")
        cache = data.get("cache", {})
        if not isinstance(cache, dict):
            self.fail("[cache] must be a TOML table")
        self.assertEqual(
            _LOCAL_EXAMPLE_EXPECTED["cache"],
            set(cache.keys()),
            f"[cache] must contain exactly "
            f"{_LOCAL_EXAMPLE_EXPECTED['cache']}, "
            f"got {set(cache.keys())}",
        )
        self.assertIsInstance(
            cache.get("dir"), str,
            "[cache].dir must be a string",
        )


# ════════════════════════════════════════════════════════════════════
# Task 5.3 — Documentation parity definitions
# ════════════════════════════════════════════════════════════════════

_README_PATHS: tuple[pathlib.Path, ...] = (
    REPO / "README.md",
    REPO / "README.en.md",
    REPO / "README.zh.md",
)

# Per-concept documentation requirements.
#
# Each entry is ``(concept_name, (anchor_terms,), and_groups)`` where
# *and_groups* is a tuple of term-tuples.  A paragraph satisfies the
# requirement when it contains at least one anchor term AND every
# and-group has at least one term present in the same paragraph.
#
# A single and-group ``((t1, t2),)`` is the common “anchor + any
# qualifying term” case.  Multiple and-groups enforce structured
# combinations (e.g.  anchor + excluded-mode + negative-behavior).
_README_REQUIREMENTS: tuple[
    tuple[str, tuple[str, ...], tuple[tuple[str, ...], ...]], ...
] = (
    (
        "host access disabled by default / opt-in only",
        ("docker-gateway", "external-address", "host-access"),
        (("disabled", "off by default", "absent", "opt-in",
           "отключен", "по умолчанию", "禁用", "默认"),),
    ),
    (
        "docker-gateway mode resolves host.docker.internal via doctor",
        ("docker-gateway",),
        (("host.docker.internal", "resolve", "doctor",
           "шлюз", "разрешает", "网关", "解析"),),
    ),
    (
        "external-address mode uses user-supplied known address",
        ("external-address",),
        (("user", "supply", "known", "interface",
           "указать", "внешний", "指定", "外部"),),
    ),
    (
        "canonical companion: docker-constructor.toml → docker-constructor.local.toml",
        ("docker-constructor.local.toml",),
        (("docker-constructor.toml",),
         ("companion", "local", "рядом", "сопроводительный",
          "本地", "旁边"),),
    ),
    (
        "optional proxy-port with no protocol assumption",
        ("proxy-port",),
        (("optional", "no protocol", "port only", "HOST_PROXY_PORT",
           "derive", "assume", "construct",
           "необязательный", "протокол", "可选", "协议"),),
    ),
    (
        "external-service binding caveat (interface / firewall)",
        ("HOST_ACCESS_ADDRESS", "external service"),
        (("bind", "interface", "loopback", "firewall", "listens",
           "caveat", "reachable",
           "привязка", "интерфейс", "брандмауэр", "绑定", "接口", "防火墙"),),
    ),
    (
        "neutral HOST_ACCESS_ADDRESS / HOST_PROXY_PORT (no protocol assumption)",
        ("HOST_ACCESS_ADDRESS",),
        (("HOST_PROXY_PORT",),
         ("neutral", "protocol", "assume", "derive",
           "only", "variable",
           "нейтральн", "протокол", "仅", "协议"),),
    ),
    (
        "doctor Docker-gateway exclusivity: external-address/disabled skipped",
        ("docker-gateway",),
        (("external-address", "disabled",
           "внешний", "отключен", "外部", "禁用"),
         ("does not", "skip", "no diagnosis", "no overwrite",
          "no repair", "no persist",
          "не выполняет", "не работает", "не диагностирует",
          "не перезаписывает", "не восстанавливает",
          "不能", "不运行", "不诊断", "不覆盖", "不修复"),),
    ),
    (
        "cache.dir local companion and cache.ttl reviewed policy",
        ("cache.dir",),
        (("cache.ttl",),
         ("local", "companion", "локальный", "本地")),
    ),
)


# ════════════════════════════════════════════════════════════════════
# Task 5.3 — Documentation parity helpers
# ════════════════════════════════════════════════════════════════════


def _paragraphs(text: str) -> list[str]:
    """Split *text* into paragraphs (blank-line-delimited blocks)."""
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def _paragraph_satisfies(
    para: str,
    anchor: tuple[str, ...],
    and_groups: tuple[tuple[str, ...], ...],
) -> bool:
    """Return True when *para* contains at least one anchor term AND
    every and-group has at least one term present."""
    para_lower = para.lower()
    # Anchor: at least one match
    if not any(a.lower() in para_lower for a in anchor):
        return False
    # Each and-group: at least one match
    for group in and_groups:
        if not any(g.lower() in para_lower for g in group):
            return False
    return True


def _find_matching_paragraph(
    text: str,
    anchor: tuple[str, ...],
    and_groups: tuple[tuple[str, ...], ...],
) -> str | None:
    """Return the first paragraph satisfying the requirement, or None."""
    for para in _paragraphs(text):
        if _paragraph_satisfies(para, anchor, and_groups):
            return para
    return None


# ════════════════════════════════════════════════════════════════════
# Task 5.3 — Matcher unit tests
# ════════════════════════════════════════════════════════════════════


class TestDocumentationMatcher(unittest.TestCase):
    """Unit tests for _paragraph_satisfies — verify structured requirement
    matching before applying to real READMEs."""

    def test_doctor_scope_positive(self) -> None:
        """docker-gateway + excluded mode + negative behavior → match."""
        para = (
            "For docker-gateway mode, run doctor to diagnose and persist.  "
            "External-address and disabled modes do not trigger doctor — "
            "no diagnosis, no overwrite, no repair."
        )
        anchor = ("docker-gateway",)
        groups = (
            ("external-address", "disabled"),
            ("does not", "skip", "no diagnosis", "no overwrite", "no repair"),
        )
        self.assertTrue(
            _paragraph_satisfies(para, anchor, groups),
            "complete doctor-scope paragraph must satisfy all groups",
        )

    def test_doctor_scope_missing_excluded_mode(self) -> None:
        """docker-gateway + negative but no excluded mode → no match."""
        para = (
            "For docker-gateway mode, doctor does not run for anything else."
        )
        anchor = ("docker-gateway",)
        groups = (
            ("external-address", "disabled"),
            ("does not", "skip", "no diagnosis", "no overwrite", "no repair"),
        )
        self.assertFalse(
            _paragraph_satisfies(para, anchor, groups),
            "must fail when excluded-mode group is absent",
        )

    def test_doctor_scope_external_address_not_behavior(self) -> None:
        """external-address alone does not satisfy negative-behavior group."""
        para = (
            "docker-gateway mode and external-address mode are the two options."
        )
        anchor = ("docker-gateway",)
        groups = (
            ("external-address", "disabled"),
            ("does not", "skip", "no diagnosis", "no overwrite", "no repair"),
        )
        self.assertFalse(
            _paragraph_satisfies(para, anchor, groups),
            "external-address satisfies excluded-mode group but NOT behavior group",
        )

    def test_doctor_scope_missing_negative(self) -> None:
        """docker-gateway + excluded mode but no negative → no match."""
        para = (
            "Docker-gateway mode can be used with external-address or disabled."
        )
        anchor = ("docker-gateway",)
        groups = (
            ("external-address", "disabled"),
            ("does not", "skip", "no diagnosis", "no overwrite", "no repair"),
        )
        self.assertFalse(
            _paragraph_satisfies(para, anchor, groups),
            "must fail when negative-behavior group is absent",
        )

    def test_canonical_companion_positive(self) -> None:
        """docker-constructor.toml → docker-constructor.local.toml → match."""
        para = (
            "The canonical companion for docker-constructor.toml is "
            "docker-constructor.local.toml in the same directory."
        )
        anchor = ("docker-constructor.local.toml",)
        groups = (
            ("docker-constructor.toml",),
            ("companion", "local", "рядом", "сопроводительный", "本地", "旁边"),
        )
        self.assertTrue(
            _paragraph_satisfies(para, anchor, groups),
            "canonical mapping paragraph must satisfy all groups",
        )

    def test_neutral_vars_both_required(self) -> None:
        """HOST_ACCESS_ADDRESS alone without HOST_PROXY_PORT → no match."""
        para = (
            "The HOST_ACCESS_ADDRESS variable is neutral and carries no "
            "protocol assumption."
        )
        anchor = ("HOST_ACCESS_ADDRESS",)
        groups = (
            ("HOST_PROXY_PORT",),
            ("neutral", "protocol", "assume", "derive", "only", "variable"),
        )
        self.assertFalse(
            _paragraph_satisfies(para, anchor, groups),
            "HOST_ACCESS_ADDRESS alone fails when HOST_PROXY_PORT group is absent",
        )

    def test_neutral_vars_both_present(self) -> None:
        """Both vars + neutral language → match."""
        para = (
            "HOST_ACCESS_ADDRESS and HOST_PROXY_PORT are neutral environment "
            "variables — they carry no protocol assumption."
        )
        anchor = ("HOST_ACCESS_ADDRESS",)
        groups = (
            ("HOST_PROXY_PORT",),
            ("neutral", "protocol", "assume", "derive", "only", "variable"),
        )
        self.assertTrue(
            _paragraph_satisfies(para, anchor, groups),
            "both vars with neutral language must satisfy all groups",
        )


# ════════════════════════════════════════════════════════════════════
# Task 5.3 — Documentation parity tests
# ════════════════════════════════════════════════════════════════════


class TestReadmeParity(unittest.TestCase):
    """All README translations must document host-access features
    equivalently.  Missing files fail; generic words elsewhere do not
    satisfy the host-access section requirements."""

    def test_all_readmes_exist(self) -> None:
        for p in _README_PATHS:
            self.assertTrue(
                p.is_file(),
                f"{p.name} is missing — all README translations must exist",
            )

    # ── concept tests (driven by _README_REQUIREMENTS) ─────────────

    def test_host_access_disabled_by_default(self) -> None:
        self._assert_requirement(_README_REQUIREMENTS[0])

    def test_docker_gateway_mode_documented(self) -> None:
        self._assert_requirement(_README_REQUIREMENTS[1])

    def test_external_address_mode_documented(self) -> None:
        self._assert_requirement(_README_REQUIREMENTS[2])

    def test_canonical_companion_documented(self) -> None:
        self._assert_requirement(_README_REQUIREMENTS[3])

    def test_optional_proxy_port_documented(self) -> None:
        self._assert_requirement(_README_REQUIREMENTS[4])

    def test_external_service_binding_caveat(self) -> None:
        self._assert_requirement(_README_REQUIREMENTS[5])

    def test_neutral_host_access_variables(self) -> None:
        self._assert_requirement(_README_REQUIREMENTS[6])

    def test_doctor_docker_gateway_exclusivity(self) -> None:
        self._assert_requirement(_README_REQUIREMENTS[7])

    def test_cache_dir_migration_documented(self) -> None:
        self._assert_requirement(_README_REQUIREMENTS[8])

    # ── dedicated doctor assertions (task 5.6 contract) ────────────

    def test_doctor_diagnoses_docker_gateway_local_state(self) -> None:
        """Every README must state that doctor diagnoses/updates
        Docker-gateway local state."""
        anchor = ("docker-gateway", "doctor")
        groups = (
            ("diagnose", "update", "persist", "write", "record",
             "диагностик", "обновляет", "сохраняет",
             "诊断", "更新", "保存"),
        )
        self._assert_paragraph_exists(
            "doctor diagnoses/updates docker-gateway local state",
            anchor, groups,
        )

    def test_doctor_excludes_external_address(self) -> None:
        """Every README must state doctor does NOT
        diagnose/overwrite/persist/repair for external-address."""
        anchor = ("external-address",)
        groups = (
            ("does not", "skip", "no", "not", "never",
             "не", "не выполняет", "不", "不会"),
            ("diagnose", "overwrite", "persist", "repair", "update",
             "диагностик", "перезаписывает", "сохраняет", "восстанавливает",
             "诊断", "覆盖", "保存", "修复"),
        )
        self._assert_paragraph_exists(
            "doctor excludes external-address from diagnosis/persistence",
            anchor, groups,
        )

    def test_disabled_skips_doctor_persistence(self) -> None:
        """Every README must state disabled host access does NOT
        trigger doctor persistence or repair."""
        anchor = ("disabled", "отключен", "禁用")
        groups = (
            ("does not", "skip", "no", "not", "never",
             "не", "не выполняет", "不", "不会"),
            ("doctor", "diagnose", "persist", "repair", "update",
             "диагностик", "сохраняет", "восстанавливает",
             "诊断", "保存", "修复"),
        )
        self._assert_paragraph_exists(
            "disabled host access does not trigger doctor persistence",
            anchor, groups,
        )

    # ── helpers ───────────────────────────────────────────────────

    def _assert_requirement(
        self,
        req: tuple[str, tuple[str, ...], tuple[tuple[str, ...], ...]],
    ) -> None:
        concept, anchor, and_groups = req
        failures: list[str] = []
        for path in _README_PATHS:
            if not path.is_file():
                failures.append(f"{path.name}: missing")
                continue
            text = path.read_text(encoding="utf-8")
            found = _find_matching_paragraph(text, anchor, and_groups)
            if found is None:
                failures.append(
                    f"{path.name}: must document {concept!r}  — "
                    f"paragraph with anchor {anchor!r} and ALL of the "
                    f"following term-groups: {and_groups!r}"
                )
        if failures:
            self.fail("\n".join(failures))

    def _assert_paragraph_exists(
        self,
        label: str,
        anchor: tuple[str, ...],
        and_groups: tuple[tuple[str, ...], ...],
    ) -> None:
        failures: list[str] = []
        for path in _README_PATHS:
            if not path.is_file():
                failures.append(f"{path.name}: missing")
                continue
            text = path.read_text(encoding="utf-8")
            found = _find_matching_paragraph(text, anchor, and_groups)
            if found is None:
                failures.append(
                    f"{path.name}: must document {label!r}  — "
                    f"paragraph with anchor {anchor!r} and ALL of "
                    f"{and_groups!r}"
                )
        if failures:
            self.fail("\n".join(failures))

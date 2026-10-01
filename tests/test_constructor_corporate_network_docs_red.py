"""RED contracts for Phase 4 documentation (task 4.1).

These tests follow the repository's README-parity documentation
convention (see ``test_constructor_host_access_migration_red.py``): every
README translation must document the corporate-network feature
equivalently, with each concept expressed in a single paragraph that
contains an anchor term and every required term-group.

The GREEN documentation (tasks 4.3–4.4) updates the three README
translations and the local-companion example; until then these tests fail.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]

_README_PATHS: tuple[Path, ...] = (
    _REPO / "README.md",
    _REPO / "README.en.md",
    _REPO / "README.zh.md",
)

# Per-concept documentation requirements.
#
# Each entry is ``(concept_name, (anchor_terms,), and_groups)`` where
# *and_groups* is a tuple of term-tuples.  A paragraph satisfies the
# requirement when it contains at least one anchor term AND every
# and-group has at least one term present in the same paragraph.
#
# Anchors are language-neutral tokens (file paths, TOML section names,
# URL schemes) wherever possible; wordy concepts carry per-language
# alternatives for the Russian, English, and Chinese translations.
_README_REQUIREMENTS: tuple[
    tuple[str, tuple[str, ...], tuple[tuple[str, ...], ...]], ...
] = (
    (
        "corporate trust uses the fixed local bundle and companion sections",
        (".docker-local/corporate-ca-bundle.crt", "corporate-ca-bundle.crt"),
        (
            ("[corporate-trust]", "corporate-trust"),
            ("[network.proxy]", "network.proxy"),
            ("companion", "local", "локальн", "本地", "docker-constructor.local"),
        ),
    ),
    (
        "bundle is a complete replacement for the system trust store",
        ("complete", "полн", "完整"),
        (
            ("replace", "replacement", "замен", "替换"),
            ("ca-certificates.crt", "system", "системн", "系统"),
        ),
    ),
    (
        "proxy supports http/socks5/socks5h with explicit no_proxy",
        ("socks5h", "socks5"),
        (
            ("http",),
            ("no_proxy", "NO_PROXY", "no-proxy", "bypass", "обход", "绕过"),
        ),
    ),
    (
        "proxy configuration forbids credentials",
        ("credential", "password", "userinfo", "учётн", "凭据"),
        (
            ("not", "no", "reject", "forbid", "не", "不", "禁止"),
            ("proxy", "прокси", "代理"),
        ),
    ),
    (
        "build-stage trust requires a rebuild",
        ("rebuild", "пересобр", "重新构建", "重建"),
        (
            ("build", "сборк", "构建"),
            ("bundle", "certificate", "ca-certificates", "сертификат", "证书"),
        ),
    ),
    (
        "restart/new launch receives the updated bundle without rebuild",
        ("restart", "new launch", "launch", "перезапуск", "重启", "重新启动"),
        (
            ("without", "no", "does not", "не", "不", "无需", "不需要"),
            ("rebuild", "пересобр", "重新构建", "重建"),
            ("bundle", "mount", "certificate", "сертификат", "证书"),
        ),
    ),
    (
        "SOCKS build support is best-effort",
        ("socks5", "socks5h"),
        (
            ("best-effort", "best effort", "may fail", "not guarantee",
             "не гарантир", "尽力", "可能"),
        ),
    ),
    (
        "Docker daemon/client proxy and trust are outside this feature",
        ("daemon", "демон", "守护", "registry", "реестр", "注册表", "FROM"),
        (
            ("not", "no", "outside", "does not", "не", "不", "вне", "外部"),
            ("configure", "control", "настраива", "配置", "pull"),
        ),
    ),
)


# ── documentation matcher helpers (mirror host-access migration) ───────


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
    if not any(a.lower() in para_lower for a in anchor):
        return False
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


class TestCorporateNetworkReadmeParity(unittest.TestCase):
    """All README translations must document the corporate-network
    feature equivalently.  Missing files fail; generic words elsewhere
    do not satisfy the corporate-network section requirements."""

    def test_all_readmes_exist(self) -> None:
        for p in _README_PATHS:
            self.assertTrue(
                p.is_file(),
                f"{p.name} is missing — all README translations must exist",
            )

    # ── concept tests (driven by _README_REQUIREMENTS) ─────────────

    def test_fixed_bundle_setup_documented(self) -> None:
        self._assert_requirement(_README_REQUIREMENTS[0])

    def test_complete_bundle_replacement_documented(self) -> None:
        self._assert_requirement(_README_REQUIREMENTS[1])

    def test_proxy_schemes_and_no_proxy_documented(self) -> None:
        self._assert_requirement(_README_REQUIREMENTS[2])

    def test_credential_free_proxy_documented(self) -> None:
        self._assert_requirement(_README_REQUIREMENTS[3])

    def test_build_rebuild_requirement_documented(self) -> None:
        self._assert_requirement(_README_REQUIREMENTS[4])

    def test_runtime_refresh_without_rebuild_documented(self) -> None:
        self._assert_requirement(_README_REQUIREMENTS[5])

    def test_socks_best_effort_documented(self) -> None:
        self._assert_requirement(_README_REQUIREMENTS[6])

    def test_docker_daemon_boundary_documented(self) -> None:
        self._assert_requirement(_README_REQUIREMENTS[7])

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
                    f"{path.name}: must document {concept!r} — "
                    f"paragraph with anchor {anchor!r} and ALL of the "
                    f"following term-groups: {and_groups!r}"
                )
        if failures:
            self.fail("\n".join(failures))


# Additional Phase 4 client-CA documentation contracts.  These supplement the
# original README-parity guards above; they intentionally do not replace them.
_CLIENT_CA_NAMES = (
    "NODE_EXTRA_CA_CERTS",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "PIP_CERT",
    "CURL_CA_BUNDLE",
)
_CLIENT_CA_PATH = "/etc/ssl/certs/ca-certificates.crt"


class TestCorporateNetworkClientCaDocumentation(unittest.TestCase):
    def _texts(self) -> list[tuple[str, str]]:
        return [(path.name, path.read_text(encoding="utf-8")) for path in _README_PATHS]

    @staticmethod
    def _policy(text: str) -> str:
        start = text.lower().index("node_extra_ca_certs")
        return text.lower()[start:start + 5000]

    def test_exact_five_variable_mapping_and_enabled_scope_are_documented(self) -> None:
        terms = {
            "README.en.md": ("build", "runtime", "enabled"),
            "README.md": ("сборки", "runtime", "включено"),
            "README.zh.md": ("构建", "运行时", "启用"),
        }
        for filename, text in self._texts():
            with self.subTest(filename=filename):
                for name in _CLIENT_CA_NAMES:
                    self.assertIn(f"`{name}={_CLIENT_CA_PATH}`", text)
                policy = self._policy(text)
                for term in terms[filename]:
                    self.assertIn(term, policy)

    def test_disabled_mode_preserves_inherited_values(self) -> None:
        terms = {
            "README.en.md": ("disabled", "inherited", "preserv"),
            "README.md": ("отключено", "унаследованные", "сохраня"),
            "README.zh.md": ("禁用", "继承", "保留"),
        }
        for filename, text in self._texts():
            with self.subTest(filename=filename):
                policy = self._policy(text)
                for term in terms[filename]:
                    self.assertIn(term, policy)

    def test_non_persistence_and_direct_launch_boundary_are_documented(self) -> None:
        terms = {
            "README.en.md": ("image `env`", "direct", "outside"),
            "README.md": ("образа `env`", "прямой запуск", "вне"),
            "README.zh.md": ("镜像 `env`", "直接启动", "不属于"),
        }
        for filename, text in self._texts():
            with self.subTest(filename=filename):
                policy = self._policy(text)
                for term in terms[filename]:
                    self.assertIn(term, policy)

    def test_launch_exceptions_do_not_exclude_required_runtime_verification(self) -> None:
        terms = {
            "README.en.md": (
                "standalone npm assembler container",
                "runtime ca-policy verification remains required",
                "separate from the two diagnostic launch exceptions",
                "inherited",
            ),
            "README.md": (
                "контейнер автономного npm-сборщика",
                "runtime-проверка политики ca остаётся обязательной",
                "не относится к двум диагностическим исключениям запуска",
                "унаследованные",
            ),
            "README.zh.md": (
                "独立 npm 组装器容器",
                "运行时 ca 策略验证仍是必需的",
                "独立于以下两个诊断启动例外",
                "继承",
            ),
        }
        for filename, text in self._texts():
            with self.subTest(filename=filename):
                policy = self._policy(text)
                self.assertIn("docker/versioning/verification.py", policy)
                self.assertIn("docker/networking.py", policy)
                for term in terms[filename]:
                    self.assertIn(term, policy)

    def test_enterprise_goal_and_node_augmentation_are_documented(self) -> None:
        terms = {
            "README.en.md": (
                "enterprise tls interception", "augments", "built-in",
                "does not replace", "system bundle",
            ),
            "README.md": (
                "корпоративным перехватом tls", "дополняет", "встроенные",
                "не заменяет", "системный bundle",
            ),
            "README.zh.md": (
                "企业 tls 拦截", "扩充", "内置", "不会替换", "系统 bundle",
            ),
        }
        for filename, text in self._texts():
            with self.subTest(filename=filename):
                policy = self._policy(text)
                for term in terms[filename]:
                    self.assertIn(term, policy)


if __name__ == "__main__":
    unittest.main()

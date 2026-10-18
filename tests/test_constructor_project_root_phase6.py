"""Phase 6 semantic and README contract checks."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
READMES = tuple(ROOT / name for name in ("README.md", "README.en.md", "README.zh.md"))
ACTIVE_ROOTS = (ROOT / "docker", ROOT / "scripts")
FORBIDDEN = (
    re.compile(r"--inventory\b"),
    re.compile(r"--main-project\b"),
    re.compile(r"--project(?!-directory)\b"),
    re.compile(r"(?<!\S)-m(?=\s+(?:/|\.{1,2}/|~))"),
    re.compile(r"--base-project-dir\b"),
    re.compile(r"\bBASE_PROJECT_DIR\b"),
    re.compile(r"\bPROJECT_PATH_"),
    re.compile(r"\bCheckoutBuildLock\b"),
    re.compile(r"\bacquire_checkout_build_lock\b"),
    re.compile(r"\bcheckout_root\s*[:=]"),
    re.compile(r"(?:mounted |runtime )project paths?"),
)


class TestSemanticContract(unittest.TestCase):
    def test_active_scripts_and_production_code_have_no_removed_contract(self) -> None:
        violations: list[str] = []
        for root in ACTIVE_ROOTS:
            for path in root.rglob("*"):
                if not path.is_file() or path.suffix not in {"", ".py", ".sh"}:
                    continue
                for line_no, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), 1
                ):
                    if any(pattern.search(line) for pattern in FORBIDDEN):
                        violations.append(
                            f"{path.relative_to(ROOT)}:{line_no}: {line.strip()}"
                        )
        self.assertEqual(
            [], violations, "removed contracts remain active:\n" + "\n".join(violations)
        )


class TestReadmeContract(unittest.TestCase):
    def test_every_translation_documents_project_and_workspace_contract(self) -> None:
        required = (
            "--project-directory",
            "docker-constructor.toml",
            "docker-constructor.local.toml",
            "Dockerfile",
            ".docker-local/",
            "WORKSPACE_ROOT",
            "--workspace",
            "--extra-workspace",
            "WORKSPACE_PATH_",
            ".docker-generated",
        )
        for readme in READMES:
            text = readme.read_text(encoding="utf-8")
            with self.subTest(readme=readme.name):
                for token in required:
                    self.assertIn(token, text)
                self.assertNotRegex(
                    text,
                    r"--inventory\b|--main-project\b|--project(?!-directory)\b|"
                    r"(?<!\S)-m(?=\s+(?:/|\.{1,2}/|~))|--base-project-dir\b|"
                    r"\bBASE_PROJECT_DIR\b|"
                    r"\bPROJECT_PATH_",
                )

    def test_every_translation_documents_default_cwd_project_selection(self) -> None:
        expected = (
            "текущий рабочий каталог по умолчанию",
            "current working directory by default",
            "默认使用当前工作目录",
        )
        for readme, phrase in zip(READMES, expected, strict=False):
            with self.subTest(readme=readme.name):
                self.assertIn(phrase, readme.read_text(encoding="utf-8"))

    def test_every_translation_documents_external_generated_output_behavior(
        self,
    ) -> None:
        expected = (
            (
                "внешнем пространстве состояния проекта",
                "неявный каталог `.docker-generated` не создаётся ни в проекте конструктора, ни в workspace",
            ),
            (
                "external project-state namespace",
                "no implicit `.docker-generated` directory is created in the constructor project or a workspace",
            ),
            (
                "外部项目状态命名空间",
                "不会在构造器项目或 workspace 中隐式创建 `.docker-generated` 目录",
            ),
        )
        for readme, phrases in zip(READMES, expected, strict=False):
            text = readme.read_text(encoding="utf-8")
            with self.subTest(readme=readme.name):
                for phrase in phrases:
                    self.assertIn(phrase, text)

    def test_translations_present_equivalent_supported_workflows(self) -> None:
        workflows = (
            ("build -y", "build -y", "build -y"),
            ("run --tui", "run --tui", "run --tui"),
            (
                "--workspace /path/to/primary",
                "--workspace /path/to/primary",
                "--workspace /path/to/primary",
            ),
            ("check-updates", "check-updates", "check-updates"),
            ("## Обслуживание", "## Maintenance", "## 维护"),
        )
        texts = tuple(readme.read_text(encoding="utf-8") for readme in READMES)
        for workflow in workflows:
            with self.subTest(workflow=workflow[0]):
                for text, token in zip(texts, workflow, strict=False):
                    self.assertIn(token, text)

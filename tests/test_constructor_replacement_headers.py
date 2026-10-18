"""Focused Phase 1 evidence: isolated replacement-header decoration.

These tests pin the CLI-presentation decoration path in
``docker.constructor_cli``:

* the visual fragment header ``# --- <display path> ---`` is wrapped
  exactly in ANSI SGR 90 followed immediately by reset when decoration
  is enabled;
* the manual-replacement section label, TOML table headers, and TOML
  body adjacent to a decorated header remain free of ANSI sequences;
* decoration is exact (no whitespace or extended variants), contained
  (reset immediately follows the header), and does not style disabled
  output.

They exercise the presentation layer only — fragment construction and
JSON serialization are covered elsewhere and are intentionally
untouched by this change.
"""

from __future__ import annotations

import re
import unittest

from docker import constructor_cli

_SGR_90 = "\x1b[90m"
_RESET = "\x1b[0m"
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

SECTION_LABEL = (
    "─── manual replacement blocks (review-only — not applied automatically) ───"
)

FRAGMENT_ONE = '# --- base.node ---\n[build.stages.base.node]\ntag = "25-trixie-slim"\n'


def _report(*fragment_text: str) -> str:
    """Return a plain check-updates text report ending in fragments."""
    return "\n".join(
        [
            "Updates: 1 outdated (1 applicable)",
            "",
            "-----",
            "TARGET  PROVIDER  CURR -> NEXT  STATUS  PUBLISHED",
            "-----",
            "",
            SECTION_LABEL,
            *fragment_text,
        ]
    )


class TestReplacementHeaderDecoration(unittest.TestCase):
    """1.1 — headers are wrapped exactly in SGR 90 + immediate reset."""

    def test_header_wrapped_exactly_when_enabled(self) -> None:
        decorated = constructor_cli._decorate_replacement_headers(
            FRAGMENT_ONE,
            enabled=True,
        )
        self.assertEqual(
            decorated,
            f"{_SGR_90}# --- base.node ---{_RESET}\n"
            "[build.stages.base.node]\n"
            'tag = "25-trixie-slim"\n',
        )

    def test_disabled_decoration_returns_plain_text(self) -> None:
        self.assertEqual(
            constructor_cli._decorate_replacement_headers(
                FRAGMENT_ONE,
                enabled=False,
            ),
            FRAGMENT_ONE,
        )


class TestReplacementHeaderNeighbours(unittest.TestCase):
    """1.2 — adjacent label, TOML headers, and TOML body stay ANSI-free."""

    def test_adjacent_label_table_and_body_stay_ansi_free(self) -> None:
        plain = _report(FRAGMENT_ONE)
        decorated = constructor_cli._decorate_replacement_headers(
            plain,
            enabled=True,
        )

        lines = decorated.split("\n")
        decorated_header = f"{_SGR_90}# --- base.node ---{_RESET}"
        self.assertIn(decorated_header, lines)
        header_idx = lines.index(decorated_header)

        label_line = lines[header_idx - 1]
        self.assertEqual(label_line, SECTION_LABEL)
        self.assertIsNone(_ANSI.search(label_line))

        table_line = lines[header_idx + 1]
        self.assertEqual(table_line, "[build.stages.base.node]")
        self.assertIsNone(_ANSI.search(table_line))

        body_line = lines[header_idx + 2]
        self.assertEqual(body_line, 'tag = "25-trixie-slim"')
        self.assertIsNone(_ANSI.search(body_line))

    def test_only_header_line_carries_ansi_in_report(self) -> None:
        plain = _report(FRAGMENT_ONE)
        decorated = constructor_cli._decorate_replacement_headers(
            plain,
            enabled=True,
        )
        ansi_lines = [line for line in decorated.split("\n") if _ANSI.search(line)]
        self.assertEqual(
            ansi_lines,
            [f"{_SGR_90}# --- base.node ---{_RESET}"],
        )


class TestReplacementHeaderIntrospection(unittest.TestCase):
    """1.4 — exact matching, reset containment, multiple fragments."""

    MULTI = (
        "# --- base.node ---\n"
        "[build.stages.base.node]\n"
        'tag = "25-trixie-slim"\n'
        "\n"
        "# --- pi-extensions.pi-read ---\n"
        "[runtime.pi-extensions.pi-read]\n"
        'version = "0.3.0"\n'
    )

    def test_multiple_fragments_each_decorate_independently(self) -> None:
        decorated = constructor_cli._decorate_replacement_headers(
            self.MULTI,
            enabled=True,
        )
        self.assertEqual(
            decorated,
            f"{_SGR_90}# --- base.node ---{_RESET}\n"
            "[build.stages.base.node]\n"
            'tag = "25-trixie-slim"\n'
            "\n"
            f"{_SGR_90}# --- pi-extensions.pi-read ---{_RESET}\n"
            "[runtime.pi-extensions.pi-read]\n"
            'version = "0.3.0"\n',
        )

    def test_reset_immediately_follows_each_decorated_header(self) -> None:
        decorated = constructor_cli._decorate_replacement_headers(
            self.MULTI,
            enabled=True,
        )
        for line in decorated.split("\n"):
            if not line.startswith(_SGR_90):
                continue
            self.assertTrue(
                line.endswith(_RESET),
                f"header missing immediate reset: {line!r}",
            )
            body = line[len(_SGR_90) : -len(_RESET)]
            self.assertRegex(body, r"^# --- \S+ ---$")
            # No ANSI is allowed inside the header body.
            self.assertIsNone(_ANSI.search(body))

    def test_exact_header_matching_rejects_variants(self) -> None:
        plain = "\n".join(
            [
                "# --- base.node ---",  # exact → decorated
                "  # --- base.node ---",  # leading whitespace → plain
                "# --- base.node --- ",  # trailing whitespace → plain
                "# --- base.node --- extra",  # trailing content → plain
                "# ---  ---",  # empty display path → plain
                "# ---base.node---",  # missing spaces → plain
                'title = "# --- base.node ---"',  # inline TOML value → plain
            ]
        )
        decorated = constructor_cli._decorate_replacement_headers(
            plain,
            enabled=True,
        )
        self.assertEqual(
            decorated,
            "\n".join(
                [
                    f"{_SGR_90}# --- base.node ---{_RESET}",
                    "  # --- base.node ---",
                    "# --- base.node --- ",
                    "# --- base.node --- extra",
                    "# ---  ---",
                    "# ---base.node---",
                    'title = "# --- base.node ---"',
                ]
            ),
        )

    def test_section_label_and_toml_stay_plain_with_multiple_fragments(
        self,
    ) -> None:
        plain = _report(self.MULTI)
        decorated = constructor_cli._decorate_replacement_headers(
            plain,
            enabled=True,
        )
        self.assertIn(SECTION_LABEL, decorated)
        for line in decorated.split("\n"):
            if line in (
                SECTION_LABEL,
                "[build.stages.base.node]",
                'tag = "25-trixie-slim"',
                "[runtime.pi-extensions.pi-read]",
                'version = "0.3.0"',
            ):
                self.assertIsNone(_ANSI.search(line))


if __name__ == "__main__":
    unittest.main()

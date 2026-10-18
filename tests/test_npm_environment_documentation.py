"""Phase 6 — documentation contracts for the locked npm assembler (task 6.3).

The assembler's public contracts must be documented in a single
developer-facing document covering the supported lock subset, the fixed npm
policy, the trust model, evidence and identity derivation, and the
consumer boundaries.  Each concept must be expressed in a paragraph that
contains an anchor term and every required term-group, so the documentation
cannot silently drift from the implemented behaviour.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_DOC = _REPO / "docs" / "npm-environment-assembler.md"

# Per-concept documentation requirements.
#
# Each entry is ``(concept_name, (anchor_terms,), and_groups)`` where
# *and_groups* is a tuple of term-tuples.  A paragraph satisfies the
# requirement when it contains at least one anchor term AND every and-group
# has at least one term present in the same paragraph.
_REQUIREMENTS: tuple[tuple[str, tuple[str, ...], tuple[tuple[str, ...], ...]], ...] = (
    (
        "supported lock subset",
        ("package-lock.json", "lockfileVersion"),
        (
            ("https", "HTTPS"),
            ("integrity", "SRI"),
            ("file:", "git:", "link:", "workspace:", "bundled"),
            ("exact",),
        ),
    ),
    (
        "fixed npm policy flags",
        ("npm ci",),
        (
            ("--ignore-scripts",),
            ("--no-bin-links",),
            ("--no-audit",),
            ("--no-fund",),
        ),
    ),
    (
        "pinned image and script-free trust model",
        ("sha256:",),
        (
            ("read-only", "readonly"),
            ("UID/GID", "uid/gid", "user"),
            ("opaque",),
        ),
    ),
    (
        "evidence and identity derivation",
        ("AssemblerInputIdentity",),
        (
            ("AssembledOutputIdentity",),
            ("output-tree digest", "canonicalTreeDigest", "tree digest"),
            ("evidence",),
        ),
    ),
    (
        "consumer boundaries",
        ("consumer-neutral",),
        (
            ("no generated executable link", "no consumer launcher", "launcher"),
            ("no Pi layout", "Pi layout", "layout"),
            ("never npm cache internals", "npm cache internals"),
        ),
    ),
)


def _paragraphs(text: str) -> list[str]:
    """Split *text* into paragraphs (blank-line-delimited blocks)."""
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def _paragraph_satisfies(
    para: str,
    anchor: tuple[str, ...],
    and_groups: tuple[tuple[str, ...], ...],
) -> bool:
    """Return True when *para* contains an anchor term AND every and-group."""
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
    """Return the first paragraph satisfying the requirement, or ``None``."""
    for para in _paragraphs(text):
        if _paragraph_satisfies(para, anchor, and_groups):
            return para
    return None


class TestAssemblerDocumentation(unittest.TestCase):
    def test_documentation_file_exists(self) -> None:
        self.assertTrue(
            _DOC.is_file(),
            f"{_DOC.name} is missing — the assembler contracts must be documented",
        )

    def test_supported_lock_subset_documented(self) -> None:
        self._assert_requirement(_REQUIREMENTS[0])

    def test_fixed_npm_policy_documented(self) -> None:
        self._assert_requirement(_REQUIREMENTS[1])

    def test_trust_model_documented(self) -> None:
        self._assert_requirement(_REQUIREMENTS[2])

    def test_evidence_and_identity_documented(self) -> None:
        self._assert_requirement(_REQUIREMENTS[3])

    def test_consumer_boundaries_documented(self) -> None:
        self._assert_requirement(_REQUIREMENTS[4])

    def _assert_requirement(
        self,
        req: tuple[str, tuple[str, ...], tuple[tuple[str, ...], ...]],
    ) -> None:
        concept, anchor, and_groups = req
        if not _DOC.is_file():
            self.fail(f"{_DOC.name}: missing")
        text = _DOC.read_text(encoding="utf-8")
        found = _find_matching_paragraph(text, anchor, and_groups)
        if found is None:
            self.fail(
                f"{_DOC.name}: must document {concept!r} — paragraph with "
                f"anchor {anchor!r} and ALL of the groups {and_groups!r}"
            )


if __name__ == "__main__":
    unittest.main()

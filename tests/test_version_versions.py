"""Test SemanticVersion parsing and ordering."""

from __future__ import annotations

import unittest

from docker.versioning.semver import (
    SemanticVersion,
    SemverError,
    parse,
)


class TestSemanticVersionParse(unittest.TestCase):
    def test_stable(self):
        sv = parse("1.2.3")
        self.assertEqual(sv.major, 1)
        self.assertEqual(sv.minor, 2)
        self.assertEqual(sv.patch, 3)
        self.assertEqual(sv.prerelease, ())
        self.assertTrue(sv.is_stable)
        self.assertFalse(sv.is_prerelease)

    def test_prerelease(self):
        sv = parse("1.2.3-alpha.1")
        self.assertEqual(sv.prerelease, ("alpha", "1"))
        self.assertFalse(sv.is_stable)
        self.assertTrue(sv.is_prerelease)

    def test_build(self):
        sv = parse("1.2.3+build.123")
        self.assertEqual(sv.build, ("build", "123"))
        self.assertTrue(sv.is_stable)

    def test_prerelease_and_build(self):
        sv = parse("1.2.3-beta.1+build.456")
        self.assertEqual(sv.prerelease, ("beta", "1"))
        self.assertEqual(sv.build, ("build", "456"))

    def test_str_stable(self):
        self.assertEqual(str(parse("1.2.3")), "1.2.3")

    def test_str_prerelease(self):
        self.assertEqual(str(parse("1.2.3-rc.1")), "1.2.3-rc.1")

    def test_zero_major(self):
        sv = parse("0.1.0")
        self.assertEqual(sv.major, 0)

    def test_large_numbers(self):
        sv = parse("999.999.999")
        self.assertEqual(sv.major, 999)

    def test_reject_leading_zero_major(self):
        with self.assertRaises(SemverError):
            parse("01.2.3")

    def test_reject_leading_zero_prerelease(self):
        with self.assertRaises(SemverError):
            parse("1.2.3-01")

    def test_reject_malformed_prerelease(self):
        with self.assertRaises(SemverError):
            parse("1.2.3-!!!")

    def test_reject_dangling_hyphen(self):
        with self.assertRaises(SemverError):
            parse("1.2.3-")

    def test_reject_dangling_plus(self):
        with self.assertRaises(SemverError):
            parse("1.2.3+")

    def test_reject_empty(self):
        with self.assertRaises(SemverError):
            parse("")

    def test_reject_wildcard(self):
        with self.assertRaises(SemverError):
            parse("1.2.*")

    def test_accept_prerelease_with_dots(self):
        sv = parse("1.0.0-alpha.1.beta")
        self.assertEqual(sv.prerelease, ("alpha", "1", "beta"))


class TestSemanticVersionOrdering(unittest.TestCase):
    def test_stable_gt_prerelease(self):
        stable = parse("1.0.0")
        pre = parse("1.0.0-alpha")
        self.assertGreater(stable, pre)
        self.assertLess(pre, stable)

    def test_version_compare_major_minor_patch(self):
        self.assertGreater(parse("2.0.0"), parse("1.9.9"))
        self.assertGreater(parse("1.2.0"), parse("1.1.9"))
        self.assertGreater(parse("1.2.3"), parse("1.2.2"))

    def test_equal(self):
        self.assertEqual(parse("1.2.3"), parse("1.2.3"))

    def test_prerelease_ordering_numeric(self):
        a = parse("1.0.0-1")
        b = parse("1.0.0-2")
        self.assertLess(a, b)

    def test_prerelease_numeric_lt_alpha(self):
        a = parse("1.0.0-1")
        b = parse("1.0.0-alpha")
        self.assertLess(a, b)

    def test_prerelease_shorter_is_lower(self):
        """Per semver.org: 1.0.0-alpha < 1.0.0-alpha.1 (shorter is lower)."""
        a = parse("1.0.0-alpha")
        b = parse("1.0.0-alpha.1")
        self.assertLess(a, b)

    def test_1_10_gt_1_9(self):
        """1.10.0 > 1.9.0 (not lexical)"""
        self.assertGreater(
            parse("1.10.0"),
            parse("1.9.0"),
        )

    def test_build_does_not_affect_ordering(self):
        a = parse("1.0.0+build.1")
        b = parse("1.0.0+build.2")
        self.assertEqual(a, b)
        self.assertFalse(a < b)
        self.assertFalse(b < a)

    def test_major_order(self):
        self.assertGreater(parse("10.0.0"), parse("9.0.0"))

    def test_minor_order(self):
        self.assertLess(parse("1.0.0"), parse("1.10.0"))

    def test_patch_order(self):
        self.assertGreater(parse("1.0.10"), parse("1.0.9"))

    def test_sort_mixed_versions(self):
        versions = [
            "1.0.0-alpha",
            "1.0.0",
            "1.0.0-beta",
            "0.9.9",
            "1.0.0-alpha.1",
            "2.0.0",
        ]
        parsed = [parse(v) for v in versions]
        sorted_versions = sorted(parsed)
        self.assertEqual(
            [str(v) for v in sorted_versions],
            ["0.9.9", "1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-beta", "1.0.0", "2.0.0"],
        )

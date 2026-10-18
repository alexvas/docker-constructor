"""Phase 6 tasks 6.2–6.3 — Pi release-asset URLs and strict SHA256SUMS.

Exact ``https://github.com/<repository>/releases/download/<prefix><version>/``
derivation and exact-name assets, plus strict SHA256SUMS parsing and
checksum-verified acquisition (redirects, missing assets, mismatches, and
transport failures).  No npm inference or alternate naming.
"""

from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR.parent))

from docker.versioning.model import PiReleaseSource
from docker.versioning.pi_release import (
    INSTALL_PACKAGE_FILENAME,
    INSTALL_PACKAGE_LOCK_FILENAME,
    SHA256SUMS_FILENAME,
    PiReleaseError,
    PiReleaseUrls,
    acquire_install_assets,
    derive_pi_release_urls,
    parse_sha256sums,
    required_install_digests,
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _source() -> PiReleaseSource:
    return PiReleaseSource(
        package="@earendil-works/pi-coding-agent",
        release_repository="earendil-works/pi",
        release_tag_prefix="v",
    )


class TestReleaseUrlDerivation(unittest.TestCase):
    def test_exact_base_and_three_asset_urls(self):
        urls = derive_pi_release_urls(_source(), "0.84.4")
        base = "https://github.com/earendil-works/pi/releases/download/v0.84.4"
        self.assertEqual(urls.base, base)
        self.assertEqual(urls.sha256sums, f"{base}/{SHA256SUMS_FILENAME}")
        self.assertEqual(urls.install_package, f"{base}/{INSTALL_PACKAGE_FILENAME}")
        self.assertEqual(
            urls.install_package_lock, f"{base}/{INSTALL_PACKAGE_LOCK_FILENAME}"
        )

    def test_no_alternate_naming_or_npm_inference(self):
        urls = derive_pi_release_urls(_source(), "1.2.3")
        for value in (
            urls.sha256sums,
            urls.install_package,
            urls.install_package_lock,
        ):
            self.assertIn("releases/download/v1.2.3/", value)
        self.assertNotIn("registry.npmjs.org", urls.base)
        self.assertNotIn("pi-linux-x64", urls.install_package)

    def test_tag_prefix_is_concatenated_not_replaced(self):
        src = PiReleaseSource(
            package="@earendil-works/pi-coding-agent",
            release_repository="earendil-works/pi",
            release_tag_prefix="v",
        )
        urls = derive_pi_release_urls(src, "0.84.4")
        self.assertIn("/download/v0.84.4", urls.sha256sums)
        self.assertIn("/download/v0.84.4/", urls.sha256sums)


class TestStrictSha256sumsParsing(unittest.TestCase):
    def test_parse_two_required_entries(self):
        raw = (
            f"{'a' * 64}  {INSTALL_PACKAGE_FILENAME}\n"
            f"{'b' * 64}  {INSTALL_PACKAGE_LOCK_FILENAME}\n"
        ).encode()
        entries = parse_sha256sums(raw)
        self.assertEqual(entries[INSTALL_PACKAGE_FILENAME], "a" * 64)
        self.assertEqual(entries[INSTALL_PACKAGE_LOCK_FILENAME], "b" * 64)

    def test_binary_mode_asterisk_accepted(self):
        raw = f"{'c' * 64} *{INSTALL_PACKAGE_FILENAME}\n".encode()
        self.assertEqual(parse_sha256sums(raw)[INSTALL_PACKAGE_FILENAME], "c" * 64)

    def test_uppercase_hex_normalized(self):
        raw = f"{'A' * 64}  {INSTALL_PACKAGE_FILENAME}\n".encode()
        self.assertEqual(parse_sha256sums(raw)[INSTALL_PACKAGE_FILENAME], "a" * 64)

    def test_blank_line_rejected(self):
        raw = f"{'a' * 64}  {INSTALL_PACKAGE_FILENAME}\n\n".encode()
        with self.assertRaises(PiReleaseError):
            parse_sha256sums(raw)

    def test_malformed_line_rejected(self):
        raw = b"not-a-checksum-line\n"
        with self.assertRaises(PiReleaseError):
            parse_sha256sums(raw)

    def test_duplicate_filename_rejected(self):
        raw = (
            f"{'a' * 64}  {INSTALL_PACKAGE_FILENAME}\n"
            f"{'b' * 64}  {INSTALL_PACKAGE_FILENAME}\n"
        ).encode()
        with self.assertRaises(PiReleaseError):
            parse_sha256sums(raw)

    def test_escaping_filename_rejected(self):
        raw = f"{'a' * 64}  ../{INSTALL_PACKAGE_FILENAME}\n".encode()
        with self.assertRaises(PiReleaseError):
            parse_sha256sums(raw)

    def test_empty_file_rejected(self):
        with self.assertRaises(PiReleaseError):
            parse_sha256sums(b"")

    def test_required_digests_missing_one_asset(self):
        entries = {INSTALL_PACKAGE_FILENAME: "a" * 64}
        with self.assertRaises(PiReleaseError) as ctx:
            required_install_digests(entries)
        self.assertIn(INSTALL_PACKAGE_LOCK_FILENAME, str(ctx.exception))


class TestAcquireInstallAssets(unittest.TestCase):
    def _urls(self) -> PiReleaseUrls:
        return derive_pi_release_urls(_source(), "0.84.4")

    def test_redirects_are_transparent_to_acquisition(self):
        package = b'{"name":"install-package"}'
        lock = b'{"lockfileVersion":3}'
        # The download callable is the transport boundary: it already follows
        # any redirect. Acquisition must not care which URL served the bytes.
        served = {
            self._urls().sha256sums: (
                f"{_sha256(package)}  {INSTALL_PACKAGE_FILENAME}\n"
                f"{_sha256(lock)}  {INSTALL_PACKAGE_LOCK_FILENAME}\n"
            ).encode(),
            self._urls().install_package: package,
            self._urls().install_package_lock: lock,
        }
        got_package, got_lock = acquire_install_assets(
            self._urls(),
            lambda url: served[url],
        )
        self.assertEqual(got_package, package)
        self.assertEqual(got_lock, lock)

    def test_digest_mismatch_rejected(self):
        package = b'{"name":"install-package"}'
        lock = b'{"lockfileVersion":3}'
        served = {
            self._urls().sha256sums: (
                f"{_sha256(package)}  {INSTALL_PACKAGE_FILENAME}\n"
                f"{'0' * 64}  {INSTALL_PACKAGE_LOCK_FILENAME}\n"
            ).encode(),
            self._urls().install_package: package,
            self._urls().install_package_lock: lock,
        }
        with self.assertRaises(PiReleaseError) as ctx:
            acquire_install_assets(self._urls(), lambda url: served[url])
        self.assertIn("mismatch", str(ctx.exception))

    def test_missing_asset_entry_rejected(self):
        package = b"{}"
        served = {
            self._urls().sha256sums: (
                f"{_sha256(package)}  {INSTALL_PACKAGE_FILENAME}\n"
            ).encode(),
        }
        with self.assertRaises(PiReleaseError) as ctx:
            acquire_install_assets(self._urls(), lambda url: served[url])
        self.assertIn("missing required asset", str(ctx.exception))

    def test_transport_failure_propagates(self):
        def download(url: str) -> bytes:
            raise PiReleaseError("artifact transport failed")

        with self.assertRaises(PiReleaseError) as ctx:
            acquire_install_assets(self._urls(), download)
        self.assertIn("transport failed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

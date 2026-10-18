"""Acceptance evidence for corporate-trust bootstrap before first network use.

The shell harness covers only ``corporate trust bootstrap → first network
action``. It intentionally does not model the later ``ca-certificates``
installation, which may create the system trust directory in disabled builds.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_VALID_BUNDLE = "-----BEGIN CERTIFICATE-----\nAQIDBAU=\n-----END CERTIFICATE-----\n"
_COPY = (
    "cp /tmp/corporate-ca/corporate-ca-bundle.crt /etc/ssl/certs/ca-certificates.crt"
)
_ENABLED = 'if [ "${CORPORATE_TRUST_ENABLED:-}" = "true" ]; then \\'

_HARNESS = """set -eu
if [ "${CORPORATE_TRUST_ENABLED:-}" = "true" ]; then
    sh "$VALIDATOR" "$BUNDLE"
    printf '%s\\n' validated >> "$EVENTS"
    mkdir -p "$CERT_DIR"
    printf '%s\\n' directory-created >> "$EVENTS"
    cp "$BUNDLE" "$DESTINATION"
    printf '%s\\n' bundle-installed >> "$EVENTS"
fi
test ! -e "$NETWORK_MARKER"
printf '%s\\n' network > "$NETWORK_MARKER"
printf '%s\\n' first-network-action >> "$EVENTS"
"""


class TestDockerfileCorporateTrustBootstrap(unittest.TestCase):
    def test_enabled_bootstrap_order_and_final_reapplication(self):
        lines = (_ROOT / "Dockerfile").read_text().splitlines()
        start = next(i for i, line in enumerate(lines) if line.strip() == _ENABLED)
        end = next(
            i
            for i, line in enumerate(lines[start + 1 :], start + 1)
            if line.strip() in {"fi", "fi; \\"}
        )
        block = lines[start : end + 1]
        validator = next(
            i
            for i, line in enumerate(block)
            if "sh /tmp/validate-corporate-bundle.sh" in line
        )
        mkdir = next(
            i for i, line in enumerate(block) if "mkdir -p /etc/ssl/certs" in line
        )
        initial_copy = next(i for i, line in enumerate(block) if _COPY in line)
        self.assertLess(validator, mkdir)
        self.assertLess(mkdir, initial_copy)

        text = "\n".join(lines)
        self.assertEqual(1, text.count("mkdir -p /etc/ssl/certs"))
        self.assertIn("mkdir -p /etc/ssl/certs", "\n".join(block))

        copies = [i for i, line in enumerate(lines) if _COPY in line]
        apt_update = next(i for i, line in enumerate(lines) if "apt-get update" in line)
        ca_install = next(
            i
            for i, line in enumerate(lines)
            if "ca-certificates" in line and "ca-certificates.crt" not in line
        )
        self.assertEqual(2, len(copies))
        self.assertLess(copies[0], apt_update)
        self.assertLess(apt_update, ca_install)
        self.assertLess(ca_install, copies[1])


class TestCorporateTrustDirectoryBootstrap(unittest.TestCase):
    def _run_harness(self, root: Path, enabled: bool) -> tuple[Path, Path]:
        """Run the Dockerfile-equivalent pre-network bootstrap fragment only."""
        bundle = root / "corporate-ca-bundle.crt"
        bundle.write_text(_VALID_BUNDLE)
        destination = root / "etc/ssl/certs/ca-certificates.crt"
        events = root / "events"
        marker = root / "network-marker"
        env = os.environ | {
            "VALIDATOR": str(_ROOT / "docker/validate-corporate-bundle.sh"),
            "BUNDLE": str(bundle),
            "CERT_DIR": str(destination.parent),
            "DESTINATION": str(destination),
            "EVENTS": str(events),
            "NETWORK_MARKER": str(marker),
            "CORPORATE_TRUST_ENABLED": "true" if enabled else "false",
        }
        subprocess.run(["sh", "-c", _HARNESS], check=True, env=env)
        return destination, events

    def test_enabled_bootstraps_missing_directory_before_network_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination, events = self._run_harness(root, True)
            self.assertEqual(
                [
                    "validated",
                    "directory-created",
                    "bundle-installed",
                    "first-network-action",
                ],
                events.read_text().splitlines(),
            )
            self.assertEqual(_VALID_BUNDLE, destination.read_text())

    def test_disabled_skips_pre_network_certificate_directory_bootstrap(self):
        """Disabled trust does not install a corporate bundle before network use.

        Later package installation is deliberately outside this harness and may
        create the distro certificate directory.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination, events = self._run_harness(root, False)
            self.assertFalse(destination.parent.exists())
            self.assertFalse(destination.exists())
            self.assertEqual(["first-network-action"], events.read_text().splitlines())

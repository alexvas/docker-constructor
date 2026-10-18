"""Phase 3 RED tests: mode-aware doctor and atomic local persistence.

All tests import the real production function and exercise boundary
conditions that are not yet implemented.  Every test must FAIL with a
clear diagnostic (ImportError / AttributeError / assertion failure)
that guides the GREEN implementation.
"""

import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Helpers — keep minimal; reuse production wiring.
# ---------------------------------------------------------------------------

_REAL_INVENTORY = (
    Path(__file__).resolve().parents[1] / "docker-constructor.toml"
).read_text()


def _write_inventory(root: Path, *, policy: str = "") -> Path:
    """Create a minimal inventory with optional [runtime.host-access]."""
    inventory = root / "docker-constructor.toml"
    inventory.write_text(_REAL_INVENTORY + "\n" + policy)
    return inventory


class TestDoctorModeAwareRed(unittest.TestCase):
    """3.1–3.3: Doctor dispatch by host-access mode.

    The ``orchestrate_doctor`` function (and/or its supporting
    functions) must accept a host-access policy and mode and dispatch
    accordingly.  These tests prove the new behaviour:

    * Disabled host access  → no probe, repair, or local write
    * External-address mode → no probe, repair, or local write;
      user address preserved
    * Docker-gateway mode   → diagnosis + atomic local write
    """

    # ------------------------------------------------------------------
    # 3.1  Disabled host access
    # ------------------------------------------------------------------

    def test_disabled_host_access_no_probe_or_write(self):
        """Doctor with disabled host access must perform no gateway
        probe, rootless planning, repair, or local write."""
        from docker.versioning.build_orchestration import (
            DoctorRequest,
            orchestrate_doctor,
        )

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            inv = _write_inventory(root_path)  # no [runtime.host-access] → disabled
            companion = root_path / "docker-constructor.local.toml"
            companion.write_text("")

            # Use bombs that record calls and raise — any invocation
            # proves an incorrect attempt to probe or mutate.
            probe_calls: list[dict] = []

            def bomb_diagnose(**kwargs: object) -> Any:
                probe_calls.append(dict(kwargs))
                raise AssertionError(
                    "diagnose_gateway must not be called for disabled host access"
                )

            def bomb_persist(*args: object, **kwargs: object) -> Any:
                probe_calls.append({"persist": True})
                raise AssertionError(
                    "persist_gateway must not be called for disabled host access"
                )

            def bomb_plan(*args: object, **kwargs: object) -> Any:
                probe_calls.append({"plan": True})
                raise AssertionError(
                    "plan_rootless_override must not be called for disabled host access"
                )

            # ---- inject bombs into the request ----
            req = DoctorRequest(
                apply_override=False,
                repair_consent=False,
                inventory_path=inv,
                _diagnose_gateway=bomb_diagnose,
                _plan_rootless_override=bomb_plan,
            )

            result = orchestrate_doctor(req)
            self.assertEqual(
                probe_calls,
                [],
                "no probe, plan, or persist call must occur for disabled mode",
            )
            self.assertEqual(result.exit_kind.value, "success")
            self.assertIsNotNone(result.message)
            self.assertIn("disabled", (result.message or "").lower())

    def test_disabled_host_access_with_explicit_enabled_false(self):
        """Explicit ``enabled = false`` must also skip all probe/repair."""
        from docker.versioning.build_orchestration import (
            DoctorRequest,
            orchestrate_doctor,
        )

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            inv = _write_inventory(
                root_path,
                policy="[runtime.host-access]\nenabled = false\n",
            )
            companion = root_path / "docker-constructor.local.toml"
            companion.write_text("")

            probe_calls: list[dict] = []

            def bomb_diagnose(**kwargs: object) -> Any:
                probe_calls.append(dict(kwargs))
                raise AssertionError("must not probe disabled")

            req = DoctorRequest(
                apply_override=False,
                repair_consent=False,
                inventory_path=inv,
                _diagnose_gateway=bomb_diagnose,
            )
            result = orchestrate_doctor(req)
            self.assertEqual(probe_calls, [])
            self.assertEqual(result.exit_kind.value, "success")
            self.assertIn("disabled", (result.message or "").lower())

    def test_explicit_inventory_not_readable_returns_config_error(self):
        """When ``--inventory`` points to a missing or malformed file,
        doctor must return a CONFIG error, not silently report disabled."""
        from docker.versioning.build_orchestration import (
            DoctorRequest,
            orchestrate_doctor,
        )

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            # Explicit inventory path that does not exist
            missing = root_path / "nosuch.toml"
            self.assertFalse(missing.exists())

            req = DoctorRequest(
                apply_override=False,
                repair_consent=False,
                inventory_path=missing,
            )
            result = orchestrate_doctor(req)
            self.assertEqual(
                result.exit_kind.value,
                "config",
                "missing inventory must be CONFIG, not success",
            )
            self.assertIn("cannot load", (result.message or "").lower())

    def test_explicit_inventory_malformed_returns_config_error(self):
        """When ``--inventory`` points to a syntactically invalid file,
        doctor must return a CONFIG error."""
        from docker.versioning.build_orchestration import (
            DoctorRequest,
            orchestrate_doctor,
        )

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            malformed = root_path / "bad.toml"
            malformed.write_text("this is not valid toml [[[")

            req = DoctorRequest(
                apply_override=False,
                repair_consent=False,
                inventory_path=malformed,
            )
            result = orchestrate_doctor(req)
            self.assertEqual(
                result.exit_kind.value,
                "config",
                "malformed inventory must be CONFIG, not success",
            )

    # ------------------------------------------------------------------
    # 3.2  External-address mode
    # ------------------------------------------------------------------

    def test_external_address_mode_no_probe_or_write(self):
        """Doctor with external-address mode must perform no gateway
        probe, rootless planning, repair, or local write, and must
        preserve the user-supplied address."""
        from docker.versioning.build_orchestration import (
            DoctorRequest,
            orchestrate_doctor,
        )

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            inv = _write_inventory(
                root_path,
                policy=(
                    '[runtime.host-access]\nenabled = true\nmode = "external-address"\n'
                ),
            )
            companion = root_path / "docker-constructor.local.toml"
            companion.write_text('[host-access]\naddress = "192.168.99.1"\n')

            probe_calls: list[dict] = []

            def bomb_diagnose(**kwargs: object) -> Any:
                probe_calls.append(dict(kwargs))
                raise AssertionError(
                    "diagnose_gateway must not be called for external-address mode"
                )

            req = DoctorRequest(
                apply_override=False,
                repair_consent=False,
                inventory_path=inv,
                _diagnose_gateway=bomb_diagnose,
            )
            result = orchestrate_doctor(req)
            self.assertEqual(probe_calls, [])
            self.assertEqual(result.exit_kind.value, "success")
            msg = result.message or ""
            self.assertIn("external", msg.lower())

            # Local companion must be untouched.
            after = companion.read_text()
            self.assertEqual(
                after,
                '[host-access]\naddress = "192.168.99.1"\n',
                "external-address mode must not mutate the local companion",
            )

    # ------------------------------------------------------------------
    # 3.3  Docker-gateway mode — diagnosis + atomic local write
    # ------------------------------------------------------------------

    def test_docker_gateway_mode_diagnosis_and_atomic_local_write(self):
        """Docker-gateway mode must run diagnosis and atomically write
        the successful concrete address to ``[host-access].address``
        in the resolved local companion."""
        from docker.networking import (
            DockerMode,
            GatewayDiagnosis,
            ProbeResult,
        )
        from docker.versioning.build_orchestration import (
            DoctorRequest,
            orchestrate_doctor,
        )

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            inv = _write_inventory(
                root_path,
                policy=(
                    '[runtime.host-access]\nenabled = true\nmode = "docker-gateway"\n'
                ),
            )
            companion = root_path / "docker-constructor.local.toml"

            # Pre-populate a recognised [cache].dir to prove it is
            # preserved during the atomic write.
            companion.write_text('[cache]\ndir = "/var/tmp/my-cache"\n')

            def fake_diagnose(**kwargs: object) -> GatewayDiagnosis:
                return GatewayDiagnosis(
                    mode=DockerMode.ROOTFUL,
                    probe_port=9999,
                    probe_token="tok",
                    lan_ip=None,
                    probes=(
                        ProbeResult(
                            candidate="10.0.2.2",
                            ok=True,
                            resolved_ip="10.0.2.2",
                            detail="",
                        ),
                    ),
                    chosen_gateway="10.0.2.2",
                    override_installed=False,
                    override_needed=False,
                )

            req = DoctorRequest(
                apply_override=False,
                repair_consent=False,
                inventory_path=inv,
                _diagnose_gateway=fake_diagnose,
            )
            result = orchestrate_doctor(req)

            # Diagnosis succeeded and the selected gateway was persisted
            self.assertEqual(result.exit_kind.value, "success")
            self.assertEqual(result.selected_gateway, "10.0.2.2")
            self.assertIsNotNone(result.persistence_result)
            self.assertTrue(
                result.persistence_result.written,  # type: ignore[union-attr]
                "successful diagnosis must persist the address",
            )

            # Companion must contain the diagnosed address
            after = companion.read_text()
            self.assertIn('address = "10.0.2.2"', after)
            self.assertIn(
                'dir = "/var/tmp/my-cache"', after, "[cache].dir must be preserved"
            )
            # Must not add reviewed-only fields
            self.assertNotIn("enabled", after)
            self.assertNotIn("mode", after)


class TestAtomicPersistenceRed(unittest.TestCase):
    """3.4–3.5, 3.13: Atomic local companion writes.

    These tests exercise the new atomic local-companion write path:

    * Successful update preserves recognised ``[cache].dir``
    * No unknown or reviewed-policy fields are added
    * Failure boundaries leave previous local file byte-for-byte intact
    * Symlink, temporary-file, unknown-key, and partial-write hazards
      are explicitly guarded.
    """

    # ------------------------------------------------------------------
    # 3.4  Preserve [cache].dir on successful write
    # ------------------------------------------------------------------

    def test_successful_write_preserves_cache_dir(self):
        """A successful doctor update must preserve recognised
        ``[cache].dir`` and never add unknown or reviewed-policy fields."""
        from docker.versioning.build_orchestration import (
            _persist_host_access_address,
        )

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            companion = root_path / "docker-constructor.local.toml"
            original = '[cache]\ndir = "/home/user/.cache/pi-cache"\n'
            companion.write_text(original)
            original_bytes = companion.read_bytes()

            result = _persist_host_access_address(
                companion_path=companion,
                address="172.17.0.2",
            )
            self.assertTrue(result.written, "write must succeed")
            after = companion.read_text()
            self.assertIn('address = "172.17.0.2"', after)
            self.assertIn(
                'dir = "/home/user/.cache/pi-cache"',
                after,
                "[cache].dir must be preserved",
            )
            # Must NOT add reviewed-only fields
            self.assertNotIn("enabled", after)
            self.assertNotIn("mode", after)
            self.assertNotIn("proxy-port", after)
            self.assertNotIn("proxy_port", after)
            self.assertNotIn("[runtime", after)

    def test_write_with_no_prior_companion(self):
        """When no prior companion exists, the write must create one
        with only the address."""
        from docker.versioning.build_orchestration import (
            _persist_host_access_address,
        )

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            companion = root_path / "docker-constructor.local.toml"
            # companion does not exist

            result = _persist_host_access_address(
                companion_path=companion,
                address="10.0.0.1",
            )
            self.assertTrue(result.written)
            after = companion.read_text()
            self.assertIn('address = "10.0.0.1"', after)
            self.assertIn("[host-access]", after)

    def test_write_preserves_comments_and_blank_lines(self):
        """The atomic write must preserve comments and blank lines
        in the existing local companion."""
        from docker.versioning.build_orchestration import (
            _persist_host_access_address,
        )

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            companion = root_path / "docker-constructor.local.toml"
            original = (
                "# my cache directory\n"
                "[cache]\n"
                'dir = "/tmp/c"\n'
                "\n"
                "# host address will go here\n"
            )
            companion.write_text(original)

            result = _persist_host_access_address(
                companion_path=companion,
                address="192.168.1.1",
            )
            self.assertTrue(result.written)
            after = companion.read_text()
            self.assertIn("# my cache directory", after)
            self.assertIn("# host address will go here", after)
            self.assertIn('address = "192.168.1.1"', after)
            self.assertIn("[host-access]", after)

    # ------------------------------------------------------------------
    # 3.5  Failure paths preserve prior local file
    # ------------------------------------------------------------------

    def test_failure_preserves_prior_companion_byte_for_byte(self):
        """Diagnosis, repair, serialisation, or atomic-rename failure
        must leave the previous local file byte-for-byte intact."""
        from docker.versioning.build_orchestration import (
            _persist_host_access_address,
        )

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            companion = root_path / "docker-constructor.local.toml"
            original = (
                "[cache]\n"
                'dir = "/tmp/original-cache"\n'
                "[host-access]\n"
                'address = "10.0.2.15"\n'
            )
            companion.write_text(original)
            original_bytes = companion.read_bytes()

            # Simulate a write failure by making the directory
            # read-only after creating the companion.
            root_path.chmod(0o555)
            try:
                result = _persist_host_access_address(
                    companion_path=companion,
                    address="172.17.0.99",
                )
                self.assertFalse(
                    result.written,
                    "write must fail when directory is read-only",
                )
            finally:
                root_path.chmod(0o755)

            # Companion must be byte-for-byte identical.
            after_bytes = companion.read_bytes()
            self.assertEqual(
                after_bytes,
                original_bytes,
                "failed write must not mutate the companion",
            )

    def test_rename_failure_preserves_prior_file(self):
        """When the atomic rename itself fails (e.g. directory
        permissions changed between write and rename), the prior
        file must remain intact."""
        from docker.versioning.build_orchestration import (
            _persist_host_access_address,
        )

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            companion = root_path / "docker-constructor.local.toml"
            original = '[host-access]\naddress = "10.0.0.10"\n'
            companion.write_text(original)
            original_bytes = companion.read_bytes()

            # Patch os.rename to simulate a failure after write
            real_rename = os.rename

            def failing_rename(src: str, dst: str) -> None:
                raise OSError("simulated rename failure")

            with patch.object(os, "rename", failing_rename):
                result = _persist_host_access_address(
                    companion_path=companion,
                    address="10.99.99.99",
                )
                self.assertFalse(
                    result.written,
                    "write must fail when rename fails",
                )

            # Companion must be byte-for-byte identical.
            after_bytes = companion.read_bytes()
            self.assertEqual(
                after_bytes,
                original_bytes,
                "rename failure must not mutate the companion",
            )

    # ------------------------------------------------------------------
    # 3.13  Regression: symlink and comment hazards
    # ------------------------------------------------------------------

    def test_comments_with_bracket_not_treated_as_sections(self):
        """Comments containing ``[`` must not trigger section detection."""
        from docker.versioning.build_orchestration import (
            _persist_host_access_address,
        )

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            companion = root_path / "docker-constructor.local.toml"
            original = "# this is a [host-access] comment\n# another [cache] comment\n"
            companion.write_text(original)

            result = _persist_host_access_address(
                companion_path=companion,
                address="10.0.0.1",
            )
            self.assertTrue(result.written)
            after = companion.read_text()
            self.assertIn(
                "# this is a [host-access] comment",
                after,
                "comment-like lines must be preserved",
            )
            self.assertIn("# another [cache] comment", after)
            self.assertIn('address = "10.0.0.1"', after)
            # The comment containing "[host-access]" must be preserved
            # exactly as-is and must NOT trigger section detection.
            self.assertIn(
                "# this is a [host-access] comment",
                after,
                "comment-like lines must be preserved",
            )

    def test_other_key_in_host_access_section_preserved(self):
        """Unrecognised keys in ``[host-access]`` must not be dropped
        — they are preserved for forward compatibility, even though
        ``load_local_config`` would reject them at read time."""
        from docker.versioning.build_orchestration import (
            _persist_host_access_address,
        )

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            companion = root_path / "docker-constructor.local.toml"
            original = '[host-access]\naddress = "10.0.0.2"\nother = true\n'
            companion.write_text(original)

            result = _persist_host_access_address(
                companion_path=companion,
                address="192.168.1.1",
            )
            self.assertTrue(result.written)
            after = companion.read_text()
            self.assertIn('address = "192.168.1.1"', after)
            self.assertIn("other = true", after, "unrecognised key must be preserved")

    def test_address_embedded_in_quoted_value_not_confused(self):
        """The string ``address`` inside a TOML value must not be
        mistaken for the ``address`` key."""
        from docker.versioning.build_orchestration import (
            _persist_host_access_address,
        )

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            companion = root_path / "docker-constructor.local.toml"
            original = (
                "[host-access]\n"
                'address = "10.0.0.3"\n'
                'note = "the address field is above"\n'
            )
            companion.write_text(original)

            result = _persist_host_access_address(
                companion_path=companion,
                address="10.0.2.2",
            )
            self.assertTrue(result.written)
            after = companion.read_text()
            self.assertIn('address = "10.0.2.2"', after)
            self.assertIn('note = "the address field is above"', after)
            # The "address" in the note value must not have been replaced.
            self.assertEqual(
                after.count("address ="),
                1,
                "only the real address key must be acknowledged",
            )

    def test_symlink_rejected_before_read_or_rename(self):
        """A symlink companion must be rejected before any read or
        rename — following the link would copy the target's content
        and replace the symlink with a regular file."""
        import os

        from docker.versioning.build_orchestration import (
            _persist_host_access_address,
        )

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            # Real file elsewhere — must never be read
            real = root_path / "real.local.toml"
            real.write_text('[cache]\ndir = "/tmp/untouchable"\n')
            real_bytes = real.read_bytes()

            # Symlink companion pointing to the real file
            companion = root_path / "docker-constructor.local.toml"
            os.symlink(str(real), str(companion))
            self.assertTrue(companion.is_symlink())

            result = _persist_host_access_address(
                companion_path=companion,
                address="10.0.0.1",
            )
            self.assertFalse(
                result.written,
                "symlink companion must be rejected",
            )
            self.assertIn("symlink", (result.error or "").lower())

            # Symlink must still point to the real file
            self.assertTrue(companion.is_symlink(), "symlink must not be replaced")

            # Real file must be byte-for-byte untouched
            self.assertEqual(
                real.read_bytes(), real_bytes, "target file must not be read or mutated"
            )

    def test_inline_comment_after_section_header_preserved_and_recognized(self):
        """A ``[host-access]  # comment`` header must be recognised
        as the host-access section.  The inline comment must be
        preserved and the existing address must be replaced, not
        duplicated."""
        from docker.versioning.build_orchestration import (
            _persist_host_access_address,
        )

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            companion = root_path / "docker-constructor.local.toml"
            original = (
                "[host-access]  # diagnosed gateway\n"
                'address = "10.0.0.1"\n'
                "[cache]\n"
                'dir = "/tmp/c"\n'
            )
            companion.write_text(original)

            result = _persist_host_access_address(
                companion_path=companion,
                address="10.0.2.2",
            )
            self.assertTrue(result.written)
            after = companion.read_text()

            # Inline comment preserved on the section header line
            self.assertIn("[host-access]  # diagnosed gateway", after)

            # Address replaced, not duplicated
            self.assertIn('address = "10.0.2.2"', after)
            self.assertNotIn('address = "10.0.0.1"', after)

            # Exactly one [host-access] section header
            self.assertEqual(
                after.count("[host-access]"),
                1,
                "must not duplicate the section header",
            )

            # [cache] section preserved
            self.assertIn("[cache]", after)
            self.assertIn('dir = "/tmp/c"', after)


class TestDoctorConsentRed(unittest.TestCase):
    """3.6: Rootless override consent remains explicit.

    Enabling host access, passing ``--yes`` alone, or setting
    ``repair_consent=True`` without ``apply_override=True`` must NOT
    trigger the override workflow.  Only explicit
    ``--apply-rootless-override`` combined with consent applies the
    override.

    These tests also verify that the mode-aware dispatch added in
    tasks 3.1–3.3 does not introduce consent side channels.
    """

    # ------------------------------------------------------------------
    # 3.6  Consent cannot be implied
    # ------------------------------------------------------------------

    def test_yes_alone_does_not_apply_override_in_docker_gateway_mode(self):
        """``repair_consent`` without ``apply_override`` must not
        trigger override application, even when host-access is enabled
        in docker-gateway mode."""
        from docker.networking import (
            DockerMode,
            GatewayDiagnosis,
            ProbeResult,
        )
        from docker.versioning.build_orchestration import (
            DoctorRequest,
            orchestrate_doctor,
        )

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            inv = _write_inventory(
                root_path,
                policy=(
                    '[runtime.host-access]\nenabled = true\nmode = "docker-gateway"\n'
                ),
            )
            companion = root_path / "docker-constructor.local.toml"
            companion.write_text("")

            def fake_diagnose(**kwargs: object) -> GatewayDiagnosis:
                return GatewayDiagnosis(
                    mode=DockerMode.ROOTLESS,
                    probe_port=9999,
                    probe_token="tok",
                    lan_ip=None,
                    probes=(
                        ProbeResult(
                            candidate="10.0.2.2",
                            ok=True,
                            resolved_ip="10.0.2.2",
                            detail="",
                        ),
                    ),
                    chosen_gateway="10.0.2.2",
                    override_installed=False,
                    override_needed=True,
                )

            # repair_consent=True BUT apply_override=False → no repair
            req = DoctorRequest(
                apply_override=False,
                repair_consent=True,
                inventory_path=inv,
                _diagnose_gateway=fake_diagnose,
            )
            result = orchestrate_doctor(req)
            self.assertFalse(
                result.repair_applied,
                "repair_consent without apply_override must not apply",
            )

    def test_apply_override_without_consent_denied(self):
        """``apply_override=True`` without ``repair_consent=True``
        must deny the repair."""
        from docker.networking import (
            DockerMode,
            GatewayDiagnosis,
            ProbeResult,
        )
        from docker.versioning.build_orchestration import (
            DoctorRequest,
            orchestrate_doctor,
        )

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            inv = _write_inventory(
                root_path,
                policy=(
                    '[runtime.host-access]\nenabled = true\nmode = "docker-gateway"\n'
                ),
            )
            companion = root_path / "docker-constructor.local.toml"
            companion.write_text("")

            def fake_diagnose(**kwargs: object) -> GatewayDiagnosis:
                return GatewayDiagnosis(
                    mode=DockerMode.ROOTLESS,
                    probe_port=9999,
                    probe_token="tok",
                    lan_ip=None,
                    probes=(
                        ProbeResult(
                            candidate="10.0.2.2",
                            ok=True,
                            resolved_ip="10.0.2.2",
                            detail="",
                        ),
                    ),
                    chosen_gateway="10.0.2.2",
                    override_installed=False,
                    override_needed=True,
                )

            req = DoctorRequest(
                apply_override=True,
                repair_consent=False,
                inventory_path=inv,
                _diagnose_gateway=fake_diagnose,
            )
            result = orchestrate_doctor(req)
            self.assertFalse(
                result.repair_applied,
                "apply_override without repair_consent must not apply",
            )
            self.assertIn("consent", (result.message or "").lower())

    def test_enabled_host_access_alone_does_not_imply_consent(self):
        """Enabling docker-gateway host access alone (no
        --apply-rootless-override, no --yes) must not trigger repair."""
        from docker.networking import (
            DockerMode,
            GatewayDiagnosis,
            ProbeResult,
        )
        from docker.versioning.build_orchestration import (
            DoctorRequest,
            orchestrate_doctor,
        )

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            inv = _write_inventory(
                root_path,
                policy=(
                    '[runtime.host-access]\nenabled = true\nmode = "docker-gateway"\n'
                ),
            )
            companion = root_path / "docker-constructor.local.toml"
            companion.write_text("")

            def fake_diagnose(**kwargs: object) -> GatewayDiagnosis:
                return GatewayDiagnosis(
                    mode=DockerMode.ROOTLESS,
                    probe_port=9999,
                    probe_token="tok",
                    lan_ip=None,
                    probes=(
                        ProbeResult(
                            candidate="10.0.2.2",
                            ok=True,
                            resolved_ip="10.0.2.2",
                            detail="",
                        ),
                    ),
                    chosen_gateway="10.0.2.2",
                    override_installed=False,
                    override_needed=True,
                )

            # Neither apply_override nor repair_consent
            req = DoctorRequest(
                apply_override=False,
                repair_consent=False,
                inventory_path=inv,
                _diagnose_gateway=fake_diagnose,
            )
            result = orchestrate_doctor(req)
            self.assertFalse(result.repair_applied)


if __name__ == "__main__":
    unittest.main()

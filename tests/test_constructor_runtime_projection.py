"""Stage 5.1 — Runtime projection resolver tests."""

import unittest

from docker.versioning.constraints import parse_constraint
from docker.versioning.effective import (
    EffectiveConfigError,
    OverrideValidationError,
    UnsupportedOverrideError,
    resolve_runtime,
)
from docker.versioning.model import (
    NpmArtifact,
    NpmSource,
    NpmUpdate,
    OverridePolicy,
    PiExtensionEntry,
    RuntimeInventory,
    RuntimeValidation,
    EffectiveRuntimeProjection,
)


_INT = "sha512-" + "A" * 86 + "=="


def _artifact(version: str, pkg: str = "p") -> NpmArtifact:
    url = f"https://registry.npmjs.org/{pkg}/-/{pkg.rsplit('/', 1)[-1]}-{version}.tgz"
    return NpmArtifact(url=url, integrity=_INT)


def _entry(pkg: str = "p", **kw) -> PiExtensionEntry:
    defaults = {
        "version": "1.0.0",
        "source": NpmSource(package=pkg),
        "update": NpmUpdate(stable_only=True),
        "artifacts": {
            "1.0.0": _artifact("1.0.0", pkg),
            "1.2.0": _artifact("1.2.0", pkg),
            "1.2.3-beta.1": _artifact("1.2.3-beta.1", pkg),
            "2.0.0": _artifact("2.0.0", pkg),
        },
        "validation": RuntimeValidation(metadata_file="package.json"),
        "override": OverridePolicy(
            constraint=parse_constraint(">=1.0.0"),
            allow_prerelease=True,
            scheme="numeric",
        ),
    }
    defaults.update(kw)
    return PiExtensionEntry(**defaults)


def _runtime(**kw) -> RuntimeInventory:
    defaults = {
        "pi_extensions": {
            "pi-read": _entry(pkg="@example/pi-read"),
        }
    }
    defaults.update(kw)
    return RuntimeInventory(**defaults)


class TestRuntimeResolver(unittest.TestCase):
    """Resolve_runtime produces EffectiveRuntimeProjection from RuntimeInventory."""

    # ── default resolution ──────────────────────────────────────────

    def test_default_version_used_when_no_overrides(self):
        _, p = resolve_runtime(_runtime(), {})
        self.assertEqual(p.extensions["pi-read"].version, "1.0.0")
        self.assertEqual(p.extensions["pi-read"].package, "@example/pi-read")
        self.assertTrue(
            p.extensions["pi-read"].artifact_id.startswith("sha512/"),
            "artifact_id must be derived from integrity",
        )
        self.assertEqual(p.extensions["pi-read"].integrity, _INT)

    def test_preserves_package_identity(self):
        _, p = resolve_runtime(_runtime(), {})
        self.assertEqual(p.extensions["pi-read"].package, "@example/pi-read")

    def test_preserves_metadata_file(self):
        _, p = resolve_runtime(_runtime(), {})
        self.assertEqual(p.extensions["pi-read"].metadata_file, "package.json")

    # ── override application ────────────────────────────────────────

    def test_override_selects_matching_artifact(self):
        _, p = resolve_runtime(
            _runtime(),
            {"runtime.pi-extensions.pi-read.version": "1.2.0"},
        )
        self.assertEqual(p.extensions["pi-read"].version, "1.2.0")
        self.assertEqual(p.extensions["pi-read"].integrity, _INT)

    def test_inherits_integrity_from_matching_artifact(self):
        _, p = resolve_runtime(
            _runtime(),
            {"runtime.pi-extensions.pi-read.version": "1.2.0"},
        )
        self.assertEqual(p.extensions["pi-read"].integrity, _INT)

    # ── override validation errors ──────────────────────────────────

    def test_rejects_moving_tag_override(self):
        with self.assertRaises(OverrideValidationError) as ctx:
            resolve_runtime(
                _runtime(),
                {"runtime.pi-extensions.pi-read.version": "latest"},
            )
        self.assertIn("moving tag", str(ctx.exception).lower())

    def test_rejects_bad_semver_override(self):
        with self.assertRaises(OverrideValidationError):
            resolve_runtime(
                _runtime(),
                {"runtime.pi-extensions.pi-read.version": "not-a-version"},
            )

    def test_rejects_version_without_matching_artifact(self):
        with self.assertRaises(EffectiveConfigError):
            resolve_runtime(
                _runtime(),
                {"runtime.pi-extensions.pi-read.version": "9.9.9"},
            )

    def test_rejects_version_that_fails_constraint(self):
        with self.assertRaises(OverrideValidationError):
            resolve_runtime(
                _runtime(
                    pi_extensions={
                        "pi-read": _entry(
                            pkg="@example/pi-read",
                            override=OverridePolicy(
                                constraint=parse_constraint(">=2.0.0"),
                                allow_prerelease=False,
                                scheme="numeric",
                            ),
                        ),
                    }
                ),
                {"runtime.pi-extensions.pi-read.version": "1.0.0"},
            )

    def test_rejects_prerelease_when_disallowed(self):
        with self.assertRaises(OverrideValidationError):
            resolve_runtime(
                _runtime(
                    pi_extensions={
                        "pi-read": _entry(
                            pkg="@example/pi-read",
                            override=OverridePolicy(
                                constraint=parse_constraint(">=1.0.0"),
                                allow_prerelease=False,
                                scheme="numeric",
                            ),
                        ),
                    }
                ),
                {"runtime.pi-extensions.pi-read.version": "1.2.3-beta.1"},
            )

    # ── override path validation ────────────────────────────────────

    def test_rejects_unknown_override_path(self):
        with self.assertRaises(UnsupportedOverrideError):
            resolve_runtime(
                _runtime(),
                {"runtime.pi-extensions.pi-read.foobar": "1.0.0"},
            )

    def test_rejects_override_for_nonexistent_extension(self):
        with self.assertRaises(UnsupportedOverrideError):
            resolve_runtime(
                _runtime(),
                {"runtime.pi-extensions.no-such.version": "1.0.0"},
            )

    def test_rejects_override_not_starting_with_prefix(self):
        with self.assertRaises(UnsupportedOverrideError):
            resolve_runtime(
                _runtime(),
                {"build.stages.toolchain.python.version": "3.14.0"},
            )

    # ── projection structure ────────────────────────────────────────

    def test_projection_is_effective_runtime_type(self):
        _, p = resolve_runtime(_runtime(), {})
        self.assertIsInstance(p, EffectiveRuntimeProjection)

    def test_projection_extensions_are_immutable(self):
        _, p = resolve_runtime(_runtime(), {})
        with self.assertRaises(TypeError):
            p.extensions["pi-read"] = p.extensions["pi-read"]  # type: ignore[index]

    # ── multiple extensions ─────────────────────────────────────────

    def test_multiple_extensions_ordered(self):
        _, p = resolve_runtime(
            _runtime(
                pi_extensions={
                    "z-ext": _entry(pkg="z"),
                    "a-ext": _entry(pkg="a"),
                },
            ),
            {},
        )
        self.assertEqual(set(p.extensions.keys()), {"z-ext", "a-ext"})

    def test_override_only_affects_target_extension(self):
        _, p = resolve_runtime(
            _runtime(
                pi_extensions={
                    "first": _entry(
                        pkg="a",
                        artifacts={
                            "1.0.0": _artifact("1.0.0", "a"),
                            "2.0.0": _artifact("2.0.0", "a"),
                        },
                        version="1.0.0",
                    ),
                    "second": _entry(
                        pkg="b",
                        artifacts={
                            "1.0.0": _artifact("1.0.0", "b"),
                            "3.0.0": _artifact("3.0.0", "b"),
                        },
                        version="1.0.0",
                    ),
                },
            ),
            {"runtime.pi-extensions.first.version": "2.0.0"},
        )
        self.assertEqual(p.extensions["first"].version, "2.0.0")
        self.assertEqual(p.extensions["second"].version, "1.0.0")

    # ── constraint is already a parsed Constraint object ─────────────

    def test_override_with_parsed_constraint_object(self):
        """The override policy's constraint field is a ``Constraint``
        object (not a raw string) from the canonical inventory parser.
        ``_validate_runtime_override`` must use it directly."""
        _, p = resolve_runtime(
            _runtime(
                pi_extensions={
                    "pi-read": _entry(
                        pkg="@example/pi-read",
                        override=OverridePolicy(
                            constraint=parse_constraint(">=2.0.0"),
                            allow_prerelease=False,
                            scheme="numeric",
                        ),
                        artifacts={
                            "2.0.0": _artifact("2.0.0", "@example/pi-read"),
                            "3.0.0": _artifact("3.0.0", "@example/pi-read"),
                        },
                        version="2.0.0",
                    ),
                },
            ),
            {"runtime.pi-extensions.pi-read.version": "3.0.0"},
        )
        self.assertEqual(p.extensions["pi-read"].version, "3.0.0")


if __name__ == "__main__":
    unittest.main()

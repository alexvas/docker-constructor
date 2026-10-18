"""Phase 1 — locked npm input contract (tasks 1.1–1.3).

Table-driven tests for the closed ``package-lock.json`` v3 model: valid
registry roots, scoped packages, nested placement, present-SRI validation and
accepted integrity-less exact HTTPS registry nodes (1.1), rejection of root
drift, unsatisfied ranges, missing nodes, unsafe paths/URLs, malformed or
missing SRI, file/git/link/workspace/bundled nodes, unsafe root ``bin``
targets, malformed role-specific metadata and unknown fields (1.2), and the
supported platform-optional omission model (1.3).  Also covers the
side-effect-free ``preflight`` API and keyed reviewed-root metadata.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
import unittest
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR.parent))

from docker.npm_environment import (
    LockedNpmError,
    LockfileV3,
    LockPackage,
    ReviewedRootMetadata,
    RootSpec,
    ValidatedAssemblyInput,
    parse_lockfile,
    preflight,
)

_NODE_VERSION = "24.18.0"
_NPM_VERSION = "11.16.0"


def _sri(alg: str = "sha512", nbytes: int = 64) -> str:
    return f"{alg}-{base64.b64encode(bytes([0xAB]) * nbytes).decode()}"


def _url(name: str, version: str) -> str:
    base = version.split("+", 1)[0]
    stem = name.rsplit("/", 1)[-1]
    return f"https://registry.npmjs.org/{name}/-/{stem}-{base}.tgz"


def _pkg(name: str, version: str, **extra: object) -> dict:
    node: dict = {
        "version": version,
        "resolved": _url(name, version),
        "integrity": _sri(),
    }
    node.update(extra)
    return node


def _pkg_no_integrity(name: str, version: str, **extra: object) -> dict:
    node: dict = {"version": version, "resolved": _url(name, version)}
    node.update(extra)
    return node


def _lock(
    *,
    roots: dict[str, str] | None = None,
    packages: dict[str, dict] | None = None,
    root_extra: dict | None = None,
) -> str:
    """Build a lockfile-v3 document with a root node and package nodes.

    *roots* becomes the root node ``dependencies`` map and is used to derive
    ``node_modules/<name>`` nodes automatically when not given explicitly.
    """
    root_node: dict = {"name": "root", "version": "1.0.0"}
    if roots:
        root_node["dependencies"] = dict(roots)
    if root_extra:
        root_node.update(root_extra)

    pkg_nodes: dict[str, dict] = {}
    if packages:
        pkg_nodes.update(packages)
    for name, version in (roots or {}).items():
        pkg_nodes.setdefault(f"node_modules/{name}", _pkg(name, version))

    return json.dumps(
        {
            "name": "root",
            "version": "1.0.0",
            "lockfileVersion": 3,
            "requires": True,
            "packages": {"": root_node, **pkg_nodes},
        }
    ).encode("utf-8")


def _roots(*pairs: tuple[str, str]) -> tuple[RootSpec, ...]:
    return tuple(RootSpec(name, version) for name, version in pairs)


class TestValidLockfileAcceptance(unittest.TestCase):
    """1.1 — valid registry roots, scoped packages, nested placement, SRI."""

    def test_simple_registry_root_is_accepted(self):
        lockfile = parse_lockfile(
            _lock(roots={"loose-envify": "1.4.0"}),
            platform="linux-x64",
            roots=_roots(("loose-envify", "1.4.0")),
        )
        self.assertIsInstance(lockfile, LockfileV3)
        self.assertEqual(lockfile.lockfile_version, 3)
        self.assertEqual([r.name for r in lockfile.roots], ["loose-envify"])
        self.assertEqual([p.name for p in lockfile.packages], ["loose-envify"])
        self.assertEqual(lockfile.omitted_optionals, ())

    def test_scoped_package_and_dependency(self):
        raw = _lock(
            roots={"@scope/tool": "1.2.3"},
            packages={
                "node_modules/@scope/tool": _pkg(
                    "@scope/tool",
                    "1.2.3",
                    dependencies={"@scope/helper": "^2.0.0"},
                ),
                "node_modules/@scope/helper": _pkg("@scope/helper", "2.0.1"),
            },
        )
        lockfile = parse_lockfile(
            raw, platform="linux-x64", roots=_roots(("@scope/tool", "1.2.3"))
        )
        names = sorted(p.name for p in lockfile.packages)
        self.assertEqual(names, ["@scope/helper", "@scope/tool"])

    def test_nested_placement_resolves_nearest_node(self):
        # strip-ansi depends on ansi-regex ^4.1.0, while the root pins
        # ansi-regex 3.0.1 — the ^4.1.0 edge must resolve to the nested node.
        raw = _lock(
            roots={"ansi-regex": "3.0.1", "strip-ansi": "5.2.0"},
            packages={
                "node_modules/strip-ansi": _pkg(
                    "strip-ansi", "5.2.0", dependencies={"ansi-regex": "^4.1.0"}
                ),
                "node_modules/strip-ansi/node_modules/ansi-regex": _pkg(
                    "ansi-regex", "4.1.1"
                ),
            },
        )
        lockfile = parse_lockfile(
            raw,
            platform="linux-x64",
            roots=_roots(("ansi-regex", "3.0.1"), ("strip-ansi", "5.2.0")),
        )
        paths = sorted(p.path for p in lockfile.packages)
        self.assertEqual(
            paths,
            [
                "node_modules/ansi-regex",
                "node_modules/strip-ansi",
                "node_modules/strip-ansi/node_modules/ansi-regex",
            ],
        )

    def test_valid_integrity_forms_accepted(self):
        for alg, nbytes in (("sha256", 32), ("sha384", 48), ("sha512", 64)):
            raw = _lock(
                roots={"pkg": "1.0.0"},
                packages={
                    "node_modules/pkg": _pkg(
                        "pkg", "1.0.0", integrity=_sri(alg, nbytes)
                    )
                },
            )
            lockfile = parse_lockfile(
                raw, platform="linux-x64", roots=_roots(("pkg", "1.0.0"))
            )
            self.assertEqual(lockfile.packages[0].integrity.split("-")[0], alg)

    def test_closed_parser_exists_and_is_immutable(self):
        lockfile = parse_lockfile(
            _lock(roots={"pkg": "1.0.0"}),
            platform="linux-x64",
            roots=_roots(("pkg", "1.0.0")),
        )
        pkg = lockfile.packages[0]
        self.assertIsInstance(pkg, LockPackage)
        self.assertIsInstance(pkg.dependencies, tuple)


class TestIntegrityLessNodes(unittest.TestCase):
    """1.1 — integrity-less exact HTTPS registry nodes are accepted and
    recorded explicitly."""

    def test_integrity_less_registry_node_accepted_and_recorded(self):
        raw = _lock(
            roots={"pkg": "1.0.0"},
            packages={"node_modules/pkg": _pkg_no_integrity("pkg", "1.0.0")},
        )
        lockfile = parse_lockfile(
            raw, platform="linux-x64", roots=_roots(("pkg", "1.0.0"))
        )
        self.assertEqual(lockfile.packages[0].integrity, "")
        self.assertEqual(len(lockfile.integrity_less), 1)
        record = lockfile.integrity_less[0]
        self.assertEqual(record.name, "pkg")
        self.assertEqual(record.path, "node_modules/pkg")
        self.assertEqual(record.version, "1.0.0")
        self.assertEqual(record.resolved, _url("pkg", "1.0.0"))

    def test_present_integrity_not_recorded_as_integrity_less(self):
        raw = _lock(roots={"pkg": "1.0.0"})
        lockfile = parse_lockfile(
            raw, platform="linux-x64", roots=_roots(("pkg", "1.0.0"))
        )
        self.assertEqual(lockfile.integrity_less, ())

    def test_integrity_less_preflight_binds_records(self):
        raw = _lock(
            roots={"pkg": "1.0.0"},
            packages={"node_modules/pkg": _pkg_no_integrity("pkg", "1.0.0")},
        )
        validated = preflight(
            raw,
            roots=_roots(("pkg", "1.0.0")),
            platform="linux-x64",
            node_version=_NODE_VERSION,
            npm_version=_NPM_VERSION,
        )
        self.assertEqual(len(validated.integrity_less), 1)
        self.assertEqual(validated.integrity_less[0].name, "pkg")

    def test_integrity_less_platform_omitted_node_recorded(self):
        # An integrity-less optional package excluded by os/cpu on linux is
        # absent from the installed closure but still recorded as both an
        # omission and an integrity-less node, preserved through preflight.
        raw = _lock(
            roots={"a": "1.0.0"},
            root_extra={"optionalDependencies": {"native": "2.0.0"}},
            packages={
                "node_modules/native": _pkg_no_integrity(
                    "native", "2.0.0", optional=True, os=["win32"]
                ),
            },
        )
        lockfile = parse_lockfile(
            raw, platform="linux-x64", roots=_roots(("a", "1.0.0"))
        )
        # Absent from the installed closure...
        self.assertEqual([p.name for p in lockfile.packages], ["a"])
        # ...but present as an evidence-backed omission...
        self.assertEqual(
            [(o.name, o.reason) for o in lockfile.omitted_optionals],
            [("native", "platform-inapplicable")],
        )
        # ...and recorded explicitly as integrity-less.
        self.assertEqual(len(lockfile.integrity_less), 1)
        record = lockfile.integrity_less[0]
        self.assertEqual(record.name, "native")
        self.assertEqual(record.path, "node_modules/native")
        self.assertEqual(record.version, "2.0.0")

        validated = preflight(
            raw,
            roots=_roots(("a", "1.0.0")),
            platform="linux-x64",
            node_version=_NODE_VERSION,
            npm_version=_NPM_VERSION,
        )
        self.assertEqual(validated.integrity_less, lockfile.integrity_less)


class TestReviewedRootMetadata(unittest.TestCase):
    """1.1 — functional ``bin``/``engines.node`` preserved per reviewed root."""

    def test_multiple_roots_preserved_separately(self):
        raw = _lock(
            roots={"a": "1.0.0", "b": "2.0.0"},
            packages={
                "node_modules/a": _pkg(
                    "a",
                    "1.0.0",
                    bin={"a": "dist/cli.js"},
                    engines={"node": ">=18"},
                ),
                "node_modules/b": _pkg(
                    "b",
                    "2.0.0",
                    bin={"b": "bin/b.js"},
                    engines={"node": ">=20"},
                ),
            },
        )
        lockfile = parse_lockfile(
            raw,
            platform="linux-x64",
            roots=_roots(("a", "1.0.0"), ("b", "2.0.0")),
        )
        self.assertEqual(len(lockfile.root_metadata), 2)
        by_name = {m.package_name: m for m in lockfile.root_metadata}
        self.assertEqual(by_name["a"].bin, (("a", "dist/cli.js"),))
        self.assertEqual(by_name["a"].engines_node, ">=18")
        self.assertEqual(by_name["b"].bin, (("b", "bin/b.js"),))
        self.assertEqual(by_name["b"].engines_node, ">=20")
        # Metadata is keyed: one root's metadata never aliases another's.
        self.assertNotEqual(by_name["a"].key, by_name["b"].key)
        self.assertEqual(by_name["a"].lock_path, "node_modules/a")
        self.assertEqual(by_name["b"].lock_path, "node_modules/b")

    def test_root_without_engines_has_none(self):
        raw = _lock(
            roots={"a": "1.0.0"},
            packages={"node_modules/a": _pkg("a", "1.0.0", bin={"a": "cli.js"})},
        )
        lockfile = parse_lockfile(
            raw, platform="linux-x64", roots=_roots(("a", "1.0.0"))
        )
        self.assertEqual(lockfile.root_metadata[0].engines_node, None)

    def test_transitive_bin_and_engines_not_preserved(self):
        raw = _lock(
            roots={"a": "1.0.0"},
            packages={
                "node_modules/a": _pkg("a", "1.0.0", dependencies={"b": "^1.0.0"}),
                "node_modules/b": _pkg(
                    "b",
                    "1.5.0",
                    bin={"b": "dist/b.js"},
                    engines={"node": "^12.17.0 || ^14.13 || >=16.0.0"},
                ),
            },
        )
        lockfile = parse_lockfile(
            raw, platform="linux-x64", roots=_roots(("a", "1.0.0"))
        )
        # Only the reviewed root carries metadata; transitive metadata is
        # validated and discarded.
        self.assertEqual([m.package_name for m in lockfile.root_metadata], ["a"])
        self.assertEqual(lockfile.root_metadata[0].bin, ())

    def test_root_metadata_is_immutable_dto(self):
        raw = _lock(
            roots={"a": "1.0.0"},
            packages={"node_modules/a": _pkg("a", "1.0.0", bin={"a": "cli.js"})},
        )
        lockfile = parse_lockfile(
            raw, platform="linux-x64", roots=_roots(("a", "1.0.0"))
        )
        meta = lockfile.root_metadata[0]
        self.assertIsInstance(meta, ReviewedRootMetadata)


class TestIgnoredMetadata(unittest.TestCase):
    """1.1 — license/funding/deprecated and transitive/manifest bin+engines are
    accepted-and-ignored after validation and absent from semantic DTOs."""

    def test_manifest_metadata_accepted_and_discarded(self):
        raw = _lock(
            roots={"a": "1.0.0"},
            root_extra={
                "engines": {"node": ">=18"},
                "license": "MIT",
                "funding": {"url": "https://example.com"},
                "deprecated": "use b instead",
            },
            packages={"node_modules/a": _pkg("a", "1.0.0")},
        )
        lockfile = parse_lockfile(
            raw, platform="linux-x64", roots=_roots(("a", "1.0.0"))
        )
        # Manifest engines must not substitute for the reviewed root's.
        self.assertEqual([m.package_name for m in lockfile.root_metadata], ["a"])
        self.assertEqual(lockfile.root_metadata[0].engines_node, None)

    def test_transitive_metadata_accepted_and_discarded(self):
        raw = _lock(
            roots={"a": "1.0.0"},
            packages={
                "node_modules/a": _pkg("a", "1.0.0", dependencies={"b": "^1.0.0"}),
                "node_modules/b": _pkg(
                    "b",
                    "1.5.0",
                    bin={"b": "cli.js"},
                    engines={"node": "18 || 20 || >=22"},
                    license="MIT",
                    funding=[{"type": "github", "url": "https://github.com/x/y"}],
                    deprecated="use c instead",
                ),
            },
        )
        lockfile = parse_lockfile(
            raw, platform="linux-x64", roots=_roots(("a", "1.0.0"))
        )
        # Ignored fields never appear in the DTO: LockPackage exposes no
        # bin/engines/license/funding/deprecated attributes.
        pkg_b = next(p for p in lockfile.packages if p.name == "b")
        for attr in ("bin", "engines", "license", "funding", "deprecated"):
            self.assertFalse(hasattr(pkg_b, attr), attr)

    def test_ignored_values_do_not_change_dto_content(self):
        def build(license_value: str) -> str:
            return _lock(
                roots={"a": "1.0.0"},
                packages={
                    "node_modules/a": _pkg("a", "1.0.0", dependencies={"b": "^1.0.0"}),
                    "node_modules/b": _pkg("b", "1.5.0", license=license_value),
                },
            )

        first = parse_lockfile(
            build("MIT"), platform="linux-x64", roots=_roots(("a", "1.0.0"))
        )
        second = parse_lockfile(
            build("Apache-2.0"), platform="linux-x64", roots=_roots(("a", "1.0.0"))
        )
        self.assertEqual(first.packages, second.packages)
        self.assertEqual(first.root_metadata, second.root_metadata)
        self.assertEqual(first.roots, second.roots)
        # Exact byte differences remain represented only by the digest.
        self.assertNotEqual(first.source_digest, second.source_digest)


class TestPreflight(unittest.TestCase):
    """1.1/1.2 — side-effect-free preflight and the validated assembly input."""

    def test_preflight_returns_digest_bound_validated_input(self):
        raw = _lock(roots={"a": "1.0.0"})
        validated = preflight(
            raw,
            roots=_roots(("a", "1.0.0")),
            platform="linux-x64",
            node_version=_NODE_VERSION,
            npm_version=_NPM_VERSION,
        )
        self.assertIsInstance(validated, ValidatedAssemblyInput)
        self.assertEqual(validated.lockfile_digest, hashlib.sha256(raw).hexdigest())
        self.assertEqual(validated.lockfile_bytes, raw)
        self.assertEqual(validated.platform, "linux-x64")
        self.assertEqual(validated.node_version, _NODE_VERSION)
        self.assertEqual(validated.npm_version, _NPM_VERSION)
        self.assertEqual([r.name for r in validated.roots], ["a"])
        self.assertEqual([p.name for p in validated.packages], ["a"])
        # No output path or evidence belongs to pure preflight.
        for attr in ("output_path", "evidence", "tree_digest"):
            self.assertFalse(hasattr(validated, attr), attr)

    def test_preflight_rejects_incompatible_reviewed_root_engine(self):
        raw = _lock(
            roots={"a": "1.0.0"},
            packages={"node_modules/a": _pkg("a", "1.0.0", engines={"node": ">=99"})},
        )
        with self.assertRaises(LockedNpmError) as ctx:
            preflight(
                raw,
                roots=_roots(("a", "1.0.0")),
                platform="linux-x64",
                node_version=_NODE_VERSION,
                npm_version=_NPM_VERSION,
            )
        self.assertEqual(ctx.exception.reason, "incompatible_node_engine")
        self.assertIn("a", ctx.exception.detail)
        self.assertIn("node_modules/a", ctx.exception.detail)

    def test_preflight_rejects_one_of_multiple_incompatible_roots(self):
        raw = _lock(
            roots={"a": "1.0.0", "b": "2.0.0"},
            packages={
                "node_modules/a": _pkg("a", "1.0.0", engines={"node": ">=18"}),
                "node_modules/b": _pkg("b", "2.0.0", engines={"node": ">=99"}),
            },
        )
        with self.assertRaises(LockedNpmError) as ctx:
            preflight(
                raw,
                roots=_roots(("a", "1.0.0"), ("b", "2.0.0")),
                platform="linux-x64",
                node_version=_NODE_VERSION,
                npm_version=_NPM_VERSION,
            )
        self.assertEqual(ctx.exception.reason, "incompatible_node_engine")
        self.assertIn("b", ctx.exception.detail)

    def test_transitive_engine_unsatisfied_is_accepted(self):
        # engine-strict is disabled: a valid transitive range the reviewed
        # Node does not satisfy is accepted with no custom diagnostic.
        raw = _lock(
            roots={"a": "1.0.0"},
            packages={
                "node_modules/a": _pkg("a", "1.0.0", dependencies={"b": "^1.0.0"}),
                "node_modules/b": _pkg("b", "1.5.0", engines={"node": ">=99"}),
            },
        )
        validated = preflight(
            raw,
            roots=_roots(("a", "1.0.0")),
            platform="linux-x64",
            node_version=_NODE_VERSION,
            npm_version=_NPM_VERSION,
        )
        self.assertEqual([m.package_name for m in validated.root_metadata], ["a"])

    def test_manifest_engine_does_not_constrain_reviewed_node(self):
        raw = _lock(
            roots={"a": "1.0.0"},
            root_extra={"engines": {"node": ">=99"}},
            packages={"node_modules/a": _pkg("a", "1.0.0")},
        )
        validated = preflight(
            raw,
            roots=_roots(("a", "1.0.0")),
            platform="linux-x64",
            node_version=_NODE_VERSION,
            npm_version=_NPM_VERSION,
        )
        self.assertEqual(validated.root_metadata[0].engines_node, None)

    def test_preflight_rejects_invalid_tool_version(self):
        raw = _lock(roots={"a": "1.0.0"})
        with self.assertRaises(LockedNpmError) as ctx:
            preflight(
                raw,
                roots=_roots(("a", "1.0.0")),
                platform="linux-x64",
                node_version="latest",
                npm_version=_NPM_VERSION,
            )
        self.assertEqual(ctx.exception.reason, "invalid_tool_version")

    def test_preflight_preserves_reviewed_node_version_as_caller_owned(self):
        raw = _lock(
            roots={"a": "1.0.0"},
            packages={"node_modules/a": _pkg("a", "1.0.0", engines={"node": ">=18"})},
        )
        validated = preflight(
            raw,
            roots=_roots(("a", "1.0.0")),
            platform="linux-x64",
            node_version="22.19.0",
            npm_version="11.16.0",
        )
        self.assertEqual(validated.node_version, "22.19.0")

    def test_preflight_preserves_exact_bytes_without_round_trip(self):
        canonical = _lock(roots={"a": "1.0.0"})
        raw = canonical + b"\n  "  # trailing whitespace must survive verbatim
        validated = preflight(
            raw,
            roots=_roots(("a", "1.0.0")),
            platform="linux-x64",
            node_version=_NODE_VERSION,
            npm_version=_NPM_VERSION,
        )
        # Byte-for-byte identical: no decode/encode round trip occurred.
        self.assertEqual(validated.lockfile_bytes, raw)
        self.assertIs(validated.lockfile_bytes, raw)
        self.assertEqual(validated.lockfile_digest, hashlib.sha256(raw).hexdigest())
        self.assertNotEqual(
            validated.lockfile_digest, hashlib.sha256(canonical).hexdigest()
        )


class TestLockRejection(unittest.TestCase):
    """1.2 — every unsafe/unsupported input is rejected before effects."""

    def _expect(
        self, raw: str, platform: str, roots: tuple[RootSpec, ...], reason: str
    ):
        with self.assertRaises(LockedNpmError) as ctx:
            parse_lockfile(raw, platform=platform, roots=roots)
        self.assertEqual(ctx.exception.reason, reason)

    def test_root_drift_wrong_locked_version(self):
        raw = _lock(
            roots={"pkg": "1.0.0"},
            packages={"node_modules/pkg": _pkg("pkg", "2.0.0")},
        )
        self._expect(raw, "linux-x64", _roots(("pkg", "1.0.0")), "root_drift")

    def test_root_drift_missing_provided_root(self):
        self._expect(
            _lock(roots={"a": "1.0.0", "b": "1.0.0"}),
            "linux-x64",
            _roots(("a", "1.0.0")),
            "root_drift",
        )

    def test_root_drift_extra_provided_root(self):
        self._expect(
            _lock(roots={"a": "1.0.0"}),
            "linux-x64",
            _roots(("a", "1.0.0"), ("b", "1.0.0")),
            "root_drift",
        )

    def test_root_range_mismatch(self):
        raw = _lock(
            roots={"pkg": "1.0.0"},
            packages={"node_modules/pkg": _pkg("pkg", "2.0.0")},
            root_extra={"dependencies": {"pkg": "^1.0.0"}},
        )
        # The declared range ^1.0.0 is not satisfied by provided 2.0.0 → drift.
        self._expect(raw, "linux-x64", _roots(("pkg", "2.0.0")), "root_range_mismatch")

    def test_unsatisfied_transitive_range(self):
        raw = _lock(
            roots={"a": "1.0.0"},
            packages={
                "node_modules/a": _pkg("a", "1.0.0", dependencies={"b": "^1.0.0"}),
                "node_modules/b": _pkg("b", "2.0.0"),
            },
        )
        self._expect(raw, "linux-x64", _roots(("a", "1.0.0")), "unsatisfied_range")

    def test_missing_required_node(self):
        raw = _lock(
            roots={"a": "1.0.0"},
            packages={
                "node_modules/a": _pkg("a", "1.0.0", dependencies={"b": "^1.0.0"})
            },
        )
        self._expect(raw, "linux-x64", _roots(("a", "1.0.0")), "missing_node")

    def test_unsafe_paths(self):
        for path in (
            "../escape",
            "node_modules/../escape",
            "/absolute",
            "node_modules/a//b",
            "node_modules/a\\b",
        ):
            raw = _lock(
                roots={},
                packages={path: _pkg("escape", "1.0.0")},
            )
            self._expect(raw, "linux-x64", (), "unsafe_path")

    def test_unsafe_urls(self):
        cases = [
            {"resolved": "http://registry.npmjs.org/a/-/a-1.0.0.tgz"},
            {"resolved": "file:../a-1.0.0.tgz"},
            {"resolved": "git+https://github.com/a/a.git"},
            {"resolved": "https://evil.example/a/-/a-1.0.0.tgz"},
            {"resolved": "https://registry.npmjs.org/a/-/b-1.0.0.tgz"},
        ]
        for extra in cases:
            raw = _lock(
                roots={"a": "1.0.0"},
                packages={"node_modules/a": {**_pkg("a", "1.0.0"), **extra}},
            )
            self._expect(raw, "linux-x64", _roots(("a", "1.0.0")), "invalid_resolved")

    def test_invalid_sri(self):
        raw = _lock(
            roots={"a": "1.0.0"},
            packages={"node_modules/a": _pkg("a", "1.0.0", integrity="sha256-!!!")},
        )
        self._expect(raw, "linux-x64", _roots(("a", "1.0.0")), "invalid_integrity")

    def test_missing_sri_without_valid_registry_url_rejected(self):
        # Missing SRI is only accepted alongside an exact version and a valid
        # HTTPS registry URL; a non-registry source is rejected as unsafe.
        raw = _lock(
            roots={"a": "1.0.0"},
            packages={
                "node_modules/a": {
                    "version": "1.0.0",
                    "resolved": "file:../a-1.0.0.tgz",
                }
            },
        )
        self._expect(raw, "linux-x64", _roots(("a", "1.0.0")), "invalid_resolved")

    def test_missing_sri_without_resolved_rejected(self):
        raw = _lock(
            roots={"a": "1.0.0"},
            packages={"node_modules/a": {"version": "1.0.0"}},
        )
        self._expect(raw, "linux-x64", _roots(("a", "1.0.0")), "invalid_resolved")

    def test_missing_sri_without_exact_version_rejected(self):
        raw = _lock(
            roots={"a": "1.0.0"},
            packages={
                "node_modules/a": {
                    "resolved": _url("a", "1.0.0"),
                }
            },
        )
        self._expect(raw, "linux-x64", _roots(("a", "1.0.0")), "invalid_version")

    def test_unsupported_source_features(self):
        for field, value in (
            ("link", True),
            ("inBundle", True),
            ("bundled", True),
            ("workspaces", ["packages/a"]),
        ):
            raw = _lock(
                roots={"a": "1.0.0"},
                packages={"node_modules/a": _pkg("a", "1.0.0", **{field: value})},
            )
            self._expect(raw, "linux-x64", _roots(("a", "1.0.0")), "unsupported_source")

    def test_non_semver_version_rejected(self):
        raw = _lock(
            roots={"a": "1.0.0"},
            packages={"node_modules/a": _pkg("a", "latest")},
        )
        self._expect(raw, "linux-x64", _roots(("a", "1.0.0")), "invalid_version")

    def test_duplicate_root_rejected(self):
        raw = _lock(roots={"a": "1.0.0"})
        self._expect(
            raw, "linux-x64", _roots(("a", "1.0.0"), ("a", "1.0.0")), "duplicate_root"
        )

    def test_wrong_lockfile_version_rejected(self):
        raw = json.dumps({"lockfileVersion": 2, "packages": {"": {}}}).encode("utf-8")
        self._expect(raw, "linux-x64", (), "unsupported_lockfile_version")

    def test_invalid_utf8_bytes_rejected_structurally(self):
        raw = b"\xff\xfe\xfa{not utf8}"
        with self.assertRaises(LockedNpmError) as ctx:
            parse_lockfile(raw, platform="linux-x64", roots=())
        self.assertEqual(ctx.exception.reason, "invalid_utf8")

    def test_unsafe_root_bin_targets_rejected(self):
        for bad in ("/abs", "../up", "a//b", "a\\b", "C:/x", "a/./b", "a/"):
            raw = _lock(
                roots={"a": "1.0.0"},
                packages={"node_modules/a": _pkg("a", "1.0.0", bin={"a": bad})},
            )
            self._expect(
                raw, "linux-x64", _roots(("a", "1.0.0")), "unsafe_executable_path"
            )

    def test_transitive_bin_unsafe_target_is_accepted(self):
        # Transitive bin is shape-validated only (--no-bin-links): an unsafe
        # target is not created as a link and is therefore accepted+discarded.
        raw = _lock(
            roots={"a": "1.0.0"},
            packages={
                "node_modules/a": _pkg("a", "1.0.0", dependencies={"b": "^1.0.0"}),
                "node_modules/b": _pkg("b", "1.5.0", bin={"b": "../evil"}),
            },
        )
        parse_lockfile(raw, platform="linux-x64", roots=_roots(("a", "1.0.0")))

    def test_malformed_bin_rejected(self):
        for bad in ("not-an-object", {"a": ""}, {"": "cli.js"}, {"a": 3}):
            raw = _lock(
                roots={"a": "1.0.0"},
                packages={"node_modules/a": _pkg("a", "1.0.0", bin=bad)},
            )
            self._expect(raw, "linux-x64", _roots(("a", "1.0.0")), "malformed_lockfile")

    def test_unknown_engine_key_rejected(self):
        for role_pkg in (
            None,
            {"node_modules/a": _pkg("a", "1.0.0", engines={"npm": ">=10"})},
        ):
            raw = _lock(
                roots={"a": "1.0.0"},
                root_extra={"engines": {"npm": ">=10"}} if role_pkg is None else None,
                packages=role_pkg,
            )
            self._expect(
                raw, "linux-x64", _roots(("a", "1.0.0")), "unsupported_engine_key"
            )

    def test_invalid_engine_range_rejected(self):
        for bad in ("", "not a range", "1.2.3.4"):
            raw = _lock(
                roots={"a": "1.0.0"},
                packages={"node_modules/a": _pkg("a", "1.0.0", engines={"node": bad})},
            )
            self._expect(
                raw, "linux-x64", _roots(("a", "1.0.0")), "invalid_engine_range"
            )

    def test_engines_not_object_rejected(self):
        raw = _lock(
            roots={"a": "1.0.0"},
            packages={"node_modules/a": _pkg("a", "1.0.0", engines=">=18")},
        )
        self._expect(raw, "linux-x64", _roots(("a", "1.0.0")), "malformed_lockfile")

    def test_malformed_license_rejected(self):
        raw = _lock(
            roots={"a": "1.0.0"},
            packages={"node_modules/a": _pkg("a", "1.0.0", license=123)},
        )
        self._expect(raw, "linux-x64", _roots(("a", "1.0.0")), "malformed_lockfile")

    def test_malformed_deprecated_rejected(self):
        raw = _lock(
            roots={"a": "1.0.0"},
            packages={"node_modules/a": _pkg("a", "1.0.0", deprecated=123)},
        )
        self._expect(raw, "linux-x64", _roots(("a", "1.0.0")), "malformed_lockfile")

    def test_malformed_funding_rejected(self):
        for bad in ({}, [], {"type": "github"}, 123, "", [{"no-url": True}]):
            raw = _lock(
                roots={"a": "1.0.0"},
                packages={"node_modules/a": _pkg("a", "1.0.0", funding=bad)},
            )
            self._expect(raw, "linux-x64", _roots(("a", "1.0.0")), "malformed_lockfile")

    def test_manifest_bin_rejected(self):
        raw = _lock(
            roots={"a": "1.0.0"},
            root_extra={"bin": {"a": "cli.js"}},
        )
        self._expect(raw, "linux-x64", _roots(("a", "1.0.0")), "unsupported_field")

    def test_package_dev_dependencies_rejected(self):
        raw = _lock(
            roots={"a": "1.0.0"},
            packages={
                "node_modules/a": _pkg("a", "1.0.0", devDependencies={"c": "^1.0.0"})
            },
        )
        self._expect(raw, "linux-x64", _roots(("a", "1.0.0")), "unsupported_field")


class TestOptionalOmissionModel(unittest.TestCase):
    """1.3 — platform-applicable and platform-omitted optional nodes."""

    def test_platform_applicable_optional_root_accepted(self):
        raw = _lock(
            roots={},
            root_extra={
                "dependencies": {"chalk": "4.1.2"},
                "optionalDependencies": {"fsevents": "2.3.3"},
            },
            packages={
                "node_modules/chalk": _pkg("chalk", "4.1.2"),
                "node_modules/fsevents": _pkg(
                    "fsevents", "2.3.3", optional=True, os=["darwin"]
                ),
            },
        )
        # On darwin the optional root is applicable and therefore required.
        lockfile = parse_lockfile(
            raw,
            platform="darwin-arm64",
            roots=_roots(("chalk", "4.1.2"), ("fsevents", "2.3.3")),
        )
        self.assertEqual(lockfile.omitted_optionals, ())
        self.assertIn("fsevents", [p.name for p in lockfile.packages])

    def test_platform_inapplicable_optional_root_omitted(self):
        raw = _lock(
            roots={},
            root_extra={
                "dependencies": {"chalk": "4.1.2"},
                "optionalDependencies": {"fsevents": "2.3.3"},
            },
            packages={
                "node_modules/chalk": _pkg("chalk", "4.1.2"),
                "node_modules/fsevents": _pkg(
                    "fsevents", "2.3.3", optional=True, os=["darwin"]
                ),
            },
        )
        # On linux the optional root is omitted — it must not be provided.
        lockfile = parse_lockfile(
            raw, platform="linux-x64", roots=_roots(("chalk", "4.1.2"))
        )
        self.assertEqual(
            [(o.name, o.reason) for o in lockfile.omitted_optionals],
            [("fsevents", "platform-inapplicable")],
        )

    def test_absent_optional_dependency_is_missing_node(self):
        raw = _lock(
            roots={},
            root_extra={
                "dependencies": {"a": "1.0.0"},
                "optionalDependencies": {"gone": "9.9.9"},
            },
            packages={"node_modules/a": _pkg("a", "1.0.0")},
        )
        with self.assertRaises(LockedNpmError) as ctx:
            parse_lockfile(raw, platform="linux-x64", roots=_roots(("a", "1.0.0")))
        self.assertEqual(ctx.exception.reason, "missing_node")

    def test_transitive_optional_inapplicable_omitted(self):
        raw = _lock(
            roots={"a": "1.0.0"},
            packages={
                "node_modules/a": _pkg(
                    "a",
                    "1.0.0",
                    optionalDependencies={"native": "^2.0.0"},
                ),
                "node_modules/native": _pkg(
                    "native", "2.0.0", optional=True, os=["win32"]
                ),
            },
        )
        lockfile = parse_lockfile(
            raw, platform="linux-x64", roots=_roots(("a", "1.0.0"))
        )
        self.assertEqual(
            [(o.name, o.reason) for o in lockfile.omitted_optionals],
            [("native", "platform-inapplicable")],
        )

    def test_optional_node_required_by_non_optional_edge_rejected(self):
        raw = _lock(
            roots={"a": "1.0.0"},
            packages={
                "node_modules/a": _pkg(
                    "a",
                    "1.0.0",
                    dependencies={"b": "^1.0.0"},
                    optionalDependencies={"c": "^1.0.0"},
                ),
                "node_modules/b": _pkg("b", "1.0.0", optional=True),
                "node_modules/c": _pkg("c", "1.0.0", optional=True),
            },
        )
        with self.assertRaises(LockedNpmError) as ctx:
            parse_lockfile(raw, platform="linux-x64", roots=_roots(("a", "1.0.0")))
        self.assertEqual(ctx.exception.reason, "optional_marked_required")

    def test_inapplicable_required_node_rejected(self):
        raw = _lock(
            roots={"a": "1.0.0"},
            packages={
                "node_modules/a": _pkg("a", "1.0.0", dependencies={"b": "^1.0.0"}),
                "node_modules/b": _pkg("b", "1.0.0", os=["darwin"]),
            },
        )
        with self.assertRaises(LockedNpmError) as ctx:
            parse_lockfile(raw, platform="linux-x64", roots=_roots(("a", "1.0.0")))
        self.assertEqual(ctx.exception.reason, "inapplicable_required_node")

    def test_omitted_optional_subtree_is_not_required(self):
        # ``native`` is an inapplicable optional package whose own dependency
        # ``gone`` is absent from the lock.  Because ``native`` is omitted on
        # linux, its subtree must not be required to exist or satisfy ranges.
        raw = _lock(
            roots={"a": "1.0.0"},
            packages={
                "node_modules/a": _pkg(
                    "a",
                    "1.0.0",
                    optionalDependencies={"native": "^2.0.0"},
                ),
                "node_modules/native": _pkg(
                    "native",
                    "2.0.0",
                    optional=True,
                    os=["win32"],
                    dependencies={"gone": "^1.0.0"},
                ),
            },
        )
        lockfile = parse_lockfile(
            raw, platform="linux-x64", roots=_roots(("a", "1.0.0"))
        )
        self.assertEqual(
            [(o.name, o.reason) for o in lockfile.omitted_optionals],
            [("native", "platform-inapplicable")],
        )


class TestPeerDependencies(unittest.TestCase):
    """Peer edges are part of the installed closure and validated."""

    def test_missing_required_peer_rejected(self):
        raw = _lock(
            roots={"a": "1.0.0"},
            packages={
                "node_modules/a": _pkg("a", "1.0.0", peerDependencies={"b": "^1.0.0"}),
            },
        )
        with self.assertRaises(LockedNpmError) as ctx:
            parse_lockfile(raw, platform="linux-x64", roots=_roots(("a", "1.0.0")))
        self.assertEqual(ctx.exception.reason, "missing_node")

    def test_unsatisfied_peer_range_rejected(self):
        raw = _lock(
            roots={"a": "1.0.0"},
            packages={
                "node_modules/a": _pkg("a", "1.0.0", peerDependencies={"b": "^1.0.0"}),
                "node_modules/b": _pkg("b", "2.0.0", peer=True),
            },
        )
        with self.assertRaises(LockedNpmError) as ctx:
            parse_lockfile(raw, platform="linux-x64", roots=_roots(("a", "1.0.0")))
        self.assertEqual(ctx.exception.reason, "unsatisfied_range")

    def test_optional_peer_missing_is_accepted(self):
        # npm never auto-installs an optional peer: absence is normal and
        # needs no omission evidence.
        raw = _lock(
            roots={"a": "1.0.0"},
            packages={
                "node_modules/a": _pkg(
                    "a",
                    "1.0.0",
                    peerDependencies={"b": "^1.0.0"},
                    peerDependenciesMeta={"b": {"optional": True}},
                ),
            },
        )
        lockfile = parse_lockfile(
            raw, platform="linux-x64", roots=_roots(("a", "1.0.0"))
        )
        self.assertEqual(lockfile.omitted_optionals, ())

    def test_optional_peer_satisfied_is_accepted(self):
        raw = _lock(
            roots={"a": "1.0.0"},
            packages={
                "node_modules/a": _pkg(
                    "a",
                    "1.0.0",
                    peerDependencies={"b": "^1.0.0"},
                    peerDependenciesMeta={"b": {"optional": True}},
                ),
                "node_modules/b": _pkg("b", "1.5.0", peer=True),
            },
        )
        lockfile = parse_lockfile(
            raw, platform="linux-x64", roots=_roots(("a", "1.0.0"))
        )
        self.assertEqual(lockfile.omitted_optionals, ())

    def test_peer_meta_for_undeclared_peer_ignored(self):
        # ``supports-color``-style meta references a name that is not a
        # declared peer; it is shape-validated and ignored (no edge).
        raw = _lock(
            roots={"a": "1.0.0"},
            packages={
                "node_modules/a": _pkg(
                    "a",
                    "1.0.0",
                    peerDependencies={"b": "^1.0.0"},
                    peerDependenciesMeta={"c": {"optional": True}},
                ),
                "node_modules/b": _pkg("b", "1.0.0", peer=True),
            },
        )
        lockfile = parse_lockfile(
            raw, platform="linux-x64", roots=_roots(("a", "1.0.0"))
        )
        self.assertEqual(lockfile.omitted_optionals, ())

    def test_root_peers_are_not_installed(self):
        # The root project's peerDependencies are supplied by the consumer
        # environment, never installed, so a missing root peer is not an error.
        raw = _lock(
            roots={"a": "1.0.0"},
            root_extra={"peerDependencies": {"b": "^1.0.0"}},
            packages={"node_modules/a": _pkg("a", "1.0.0")},
        )
        lockfile = parse_lockfile(
            raw, platform="linux-x64", roots=_roots(("a", "1.0.0"))
        )
        self.assertEqual(lockfile.omitted_optionals, ())

    def test_hoisted_peer_satisfies_peer_dependency(self):
        # Mirrors the pinned npm fixture (react-dom@18.2.0 -> react@18.3.1):
        # a root-level dependent's peer is hoisted to the root with
        # ``peer: true`` and satisfies the peer edge.
        raw = _lock(
            roots={"a": "1.0.0"},
            packages={
                "node_modules/a": _pkg(
                    "a", "1.0.0", peerDependencies={"peer": "^1.0.0"}
                ),
                "node_modules/peer": _pkg("peer", "1.0.0", peer=True),
            },
        )
        lockfile = parse_lockfile(
            raw, platform="linux-x64", roots=_roots(("a", "1.0.0"))
        )
        self.assertEqual(sorted(p.name for p in lockfile.packages), ["a", "peer"])

    def test_nested_peer_does_not_satisfy_peer_dependency(self):
        # ``node_modules/a/node_modules/peer`` must not satisfy ``a``'s peer
        # dependency: npm resolves peers from the declaring package's parent
        # level, never from inside the package itself, so the nested node is
        # disconnected from the root and rejected.
        raw = _lock(
            roots={"a": "1.0.0"},
            packages={
                "node_modules/a": _pkg(
                    "a", "1.0.0", peerDependencies={"peer": "^1.0.0"}
                ),
                "node_modules/a/node_modules/peer": _pkg("peer", "1.0.0", peer=True),
            },
        )
        with self.assertRaises(LockedNpmError) as ctx:
            parse_lockfile(raw, platform="linux-x64", roots=_roots(("a", "1.0.0")))
        self.assertEqual(ctx.exception.reason, "extra_lock_node")

    def test_peer_resolves_from_parent_of_nested_dependent(self):
        # A peer of a nested dependent resolves from the dependent's parent
        # level, not from the root: ``b``'s nested ``a`` finds its peer at
        # ``node_modules/b/node_modules/peer``.
        raw = _lock(
            roots={"b": "1.0.0"},
            packages={
                "node_modules/b": _pkg("b", "1.0.0", dependencies={"a": "^1.0.0"}),
                "node_modules/b/node_modules/a": _pkg(
                    "a", "1.0.0", peerDependencies={"peer": "^1.0.0"}
                ),
                "node_modules/b/node_modules/peer": _pkg("peer", "1.0.0", peer=True),
            },
        )
        lockfile = parse_lockfile(
            raw, platform="linux-x64", roots=_roots(("b", "1.0.0"))
        )
        self.assertEqual(sorted(p.name for p in lockfile.packages), ["a", "b", "peer"])


class TestClosurePackageSelection(unittest.TestCase):
    """``LockfileV3.packages`` holds only the installed closure."""

    def test_unreferenced_valid_package_rejected(self):
        # ``orphan`` is a well-formed registry node but nothing references it.
        raw = _lock(
            roots={"a": "1.0.0"},
            packages={
                "node_modules/a": _pkg("a", "1.0.0"),
                "node_modules/orphan": _pkg("orphan", "1.0.0"),
            },
        )
        with self.assertRaises(LockedNpmError) as ctx:
            parse_lockfile(raw, platform="linux-x64", roots=_roots(("a", "1.0.0")))
        self.assertEqual(ctx.exception.reason, "extra_lock_node")

    def test_omitted_optional_not_in_packages(self):
        # ``native`` is optional and darwin-only; on linux it is omitted and
        # must not appear among the installed closure packages.
        raw = _lock(
            roots={},
            root_extra={
                "dependencies": {"a": "1.0.0"},
                "optionalDependencies": {"native": "2.0.0"},
            },
            packages={
                "node_modules/a": _pkg("a", "1.0.0"),
                "node_modules/native": _pkg(
                    "native", "2.0.0", optional=True, os=["darwin"]
                ),
            },
        )
        lockfile = parse_lockfile(
            raw, platform="linux-x64", roots=_roots(("a", "1.0.0"))
        )
        self.assertEqual([p.name for p in lockfile.packages], ["a"])
        self.assertEqual(
            [(o.name, o.reason) for o in lockfile.omitted_optionals],
            [("native", "platform-inapplicable")],
        )

    def test_reachable_dependencies_remain_present(self):
        raw = _lock(
            roots={"a": "1.0.0"},
            packages={
                "node_modules/a": _pkg("a", "1.0.0", dependencies={"b": "^1.0.0"}),
                "node_modules/b": _pkg("b", "1.0.0"),
            },
        )
        lockfile = parse_lockfile(
            raw, platform="linux-x64", roots=_roots(("a", "1.0.0"))
        )
        self.assertEqual(sorted(p.name for p in lockfile.packages), ["a", "b"])

    def test_self_referencing_orphan_rejected(self):
        # ``selfref`` only references itself; it is not root-connected.
        raw = _lock(
            roots={"a": "1.0.0"},
            packages={
                "node_modules/a": _pkg("a", "1.0.0"),
                "node_modules/selfref": _pkg(
                    "selfref", "1.0.0", dependencies={"selfref": "^1.0.0"}
                ),
            },
        )
        with self.assertRaises(LockedNpmError) as ctx:
            parse_lockfile(raw, platform="linux-x64", roots=_roots(("a", "1.0.0")))
        self.assertEqual(ctx.exception.reason, "extra_lock_node")

    def test_mutually_referencing_orphans_rejected(self):
        # ``x`` and ``y`` reference each other but neither is root-connected.
        raw = _lock(
            roots={"a": "1.0.0"},
            packages={
                "node_modules/a": _pkg("a", "1.0.0"),
                "node_modules/x": _pkg("x", "1.0.0", dependencies={"y": "^1.0.0"}),
                "node_modules/y": _pkg("y", "1.0.0", dependencies={"x": "^1.0.0"}),
            },
        )
        with self.assertRaises(LockedNpmError) as ctx:
            parse_lockfile(raw, platform="linux-x64", roots=_roots(("a", "1.0.0")))
        self.assertEqual(ctx.exception.reason, "extra_lock_node")

    def test_dependency_below_omitted_optional_accepted(self):
        # ``helper`` sits beneath a root-reachable, platform-omitted optional
        # package.  It is a member of the lock graph (not an orphan) but is
        # excluded from the installed closure on linux.
        raw = _lock(
            roots={"a": "1.0.0"},
            packages={
                "node_modules/a": _pkg(
                    "a",
                    "1.0.0",
                    optionalDependencies={"native": "^2.0.0"},
                ),
                "node_modules/native": _pkg(
                    "native",
                    "2.0.0",
                    optional=True,
                    os=["win32"],
                    dependencies={"helper": "^1.0.0"},
                ),
                "node_modules/helper": _pkg("helper", "1.0.0"),
            },
        )
        lockfile = parse_lockfile(
            raw, platform="linux-x64", roots=_roots(("a", "1.0.0"))
        )
        self.assertEqual([p.name for p in lockfile.packages], ["a"])
        self.assertEqual(
            [(o.name, o.reason) for o in lockfile.omitted_optionals],
            [("native", "platform-inapplicable")],
        )


class TestOverlappingOptionalDependencies(unittest.TestCase):
    """A name in both ``dependencies`` and ``optionalDependencies`` is
    governed by the optional declaration."""

    def test_optional_range_is_authoritative(self):
        # The required edge ``^1.0.0`` would reject ``x@2.0.0``; the optional
        # edge ``^2.0.0`` must win so the node installs normally.
        raw = _lock(
            roots={"a": "1.0.0"},
            packages={
                "node_modules/a": _pkg(
                    "a",
                    "1.0.0",
                    dependencies={"x": "^1.0.0"},
                    optionalDependencies={"x": "^2.0.0"},
                ),
                "node_modules/x": _pkg("x", "2.0.0"),
            },
        )
        lockfile = parse_lockfile(
            raw, platform="linux-x64", roots=_roots(("a", "1.0.0"))
        )
        self.assertEqual(sorted(p.name for p in lockfile.packages), ["a", "x"])

    def test_overlapping_absent_optional_is_missing_node(self):
        raw = _lock(
            roots={"a": "1.0.0"},
            packages={
                "node_modules/a": _pkg(
                    "a",
                    "1.0.0",
                    dependencies={"x": "^1.0.0"},
                    optionalDependencies={"x": "^2.0.0"},
                ),
            },
        )
        with self.assertRaises(LockedNpmError) as ctx:
            parse_lockfile(raw, platform="linux-x64", roots=_roots(("a", "1.0.0")))
        self.assertEqual(ctx.exception.reason, "missing_node")

    def test_overlapping_inapplicable_optional_root_omitted(self):
        raw = _lock(
            roots={},
            root_extra={
                "dependencies": {"native": "^1.0.0"},
                "optionalDependencies": {"native": "^2.0.0"},
            },
            packages={
                "node_modules/native": _pkg(
                    "native", "2.0.0", optional=True, os=["win32"]
                ),
            },
        )
        lockfile = parse_lockfile(raw, platform="linux-x64", roots=_roots())
        self.assertEqual(
            [(o.name, o.reason) for o in lockfile.omitted_optionals],
            [("native", "platform-inapplicable")],
        )
        self.assertEqual([p.name for p in lockfile.packages], [])


if __name__ == "__main__":
    unittest.main()

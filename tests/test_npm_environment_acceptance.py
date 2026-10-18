"""Phase 6 — integrated assembler acceptance tests (tasks 6.1–6.2).

These tests exercise the public ``assemble_environment`` boundary end to end
with in-memory Docker executors:

* two independent locks that share one tarball download (the opaque
  assembler npm cache) still publish distinct trees and evidence (6.1);
* cold and warm cache behaviour, corruption recovery, a network outage with
  an already-valid environment, and an incomplete cache with outage fail
  structurally without partial publication (6.2).

No test touches a real Docker daemon, socket, or network.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR.parent))

# Repository/test path setup must run before these imports.
from docker.npm_environment import (  # noqa: E402
    AssemblyResult,
    LockedNpmError,
    ProcessResult,
    RootSpec,
    assemble_environment,
    assembler_script_digest,
    compute_assembler_identity,
    compute_assembler_input_identity,
    npm_policy_digest,
    preflight,
    publication,
    verify_output,
)

_IMAGE = "sha256:" + "a" * 64
_NODE = "24.18.0"
_NPM = "11.16.0"
_PLATFORM = "linux-x64"


def _sri(seed: str) -> str:
    """Deterministic, well-formed SRI for one package identity."""
    return "sha512-" + base64.b64encode(hashlib.sha512(seed.encode()).digest()).decode()


def _model_tarball_id(resolved: str, integrity: str | None) -> str:
    """Deterministic test-only identifier for one locked tarball.

    Identical ``resolved`` and ``integrity`` values always produce the same
    identifier, so two locks that share a tarball model it as the same fake
    cache entry.  This identifier is fake-executor instrumentation only; it
    does not represent npm's cache keys, filenames, or on-disk layout.
    """
    return (
        "model-tarball-"
        + hashlib.sha256(f"{resolved}\n{integrity or ''}".encode()).hexdigest()
    )


def _lock_tarballs(lock_bytes: bytes) -> list[tuple[str, str, str | None]]:
    """Extract ``(lock path, resolved, integrity)`` tarballs from a lockfile.

    The manifest node (``""``) has no ``resolved`` URL and is skipped; every
    remaining registry node becomes one modelled fake-executor cache entry.
    """
    data = json.loads(lock_bytes)
    refs: list[tuple[str, str, str | None]] = []
    for path, meta in data["packages"].items():
        resolved = meta.get("resolved")
        if not resolved:
            continue
        refs.append((path, resolved, meta.get("integrity")))
    return refs


def _url(name: str, version: str) -> str:
    stem = name.rsplit("/", 1)[-1]
    return f"https://registry.npmjs.org/{name}/-/{stem}-{version}.tgz"


def _lock(consumer: str, root: str, shared: str) -> bytes:
    """Build a registry-only lockfile-v3 for one independent consumer.

    *consumer* is the project manifest identity, *root* is the single
    reviewed root it declares, and *shared* is the transitive package both
    consumers download through the same opaque npm cache.
    """
    return json.dumps(
        {
            "name": consumer,
            "version": "1.0.0",
            "lockfileVersion": 3,
            "requires": True,
            "packages": {
                "": {
                    "name": consumer,
                    "version": "1.0.0",
                    "dependencies": {root: "1.0.0"},
                },
                f"node_modules/{root}": {
                    "version": "1.0.0",
                    "resolved": _url(root, "1.0.0"),
                    "integrity": _sri(f"{root}@1.0.0"),
                    "dependencies": {shared: "^1.0.0"},
                },
                f"node_modules/{shared}": {
                    "version": "1.0.0",
                    "resolved": _url(shared, "1.0.0"),
                    "integrity": _sri(f"{shared}@1.0.0"),
                },
            },
        }
    ).encode()


def _validated(raw: bytes, root: str) -> object:
    return preflight(
        raw,
        roots=(RootSpec(root, "1.0.0"),),
        platform=_PLATFORM,
        node_version=_NODE,
        npm_version=_NPM,
    )


def _assembler():
    return compute_assembler_identity(
        image_digest=_IMAGE,
        node_version=_NODE,
        npm_version=_NPM,
        script_digest=assembler_script_digest(),
        policy_digest=npm_policy_digest(),
        platform=_PLATFORM,
    )


def _write_pkg(
    root: Path,
    rel: str,
    name: str,
    version: str,
    dependencies: dict | None = None,
) -> None:
    p = root / rel / "package.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {"name": name, "version": version}
    if dependencies is not None:
        data["dependencies"] = dependencies
    p.write_text(json.dumps(data))


def _tree_spec(consumer: str, root: str, shared: str) -> dict:
    """Return on-disk package metadata for one consumer's assembled tree."""
    return {
        "": (consumer, "1.0.0", {root: "1.0.0"}),
        f"node_modules/{root}": (root, "1.0.0", {shared: "^1.0.0"}),
        f"node_modules/{shared}": (shared, "1.0.0", None),
    }


class PopulatingExecutor:
    """Fake Docker executor that writes the assembled tree into staging.

    The ``--volume`` arguments are parsed exactly like the production run
    vector: the writable ``/work`` mount is the staging workspace and the
    writable ``/cache`` mount is the opaque npm download cache.  On each run
    the executor writes an opaque test marker per locked tarball — derived
    from the lockfile's ``resolved`` and ``integrity`` values — inside the
    mounted cache directory.  Identical tarballs therefore share one marker,
    modelling a cache hit or miss without claiming any knowledge of npm's
    internal cache format.  Because both assemblies mount the same
    production cache path, a marker written by the first run is read — not
    re-written — by the second.
    """

    def __init__(self, spec: dict, marker: bytes, lock_bytes: bytes):
        self.spec = spec
        self.marker = marker
        self.tarballs = _lock_tarballs(lock_bytes)
        self.calls: list[tuple[str, ...]] = []
        self.events: list[tuple[str, str]] = []  # ("download"|"hit", lock path)
        self.staging: Path | None = None
        self.npm_cache: Path | None = None

    def run(self, argv: tuple[str, ...]):
        self.calls.append(argv)
        for i, token in enumerate(argv):
            if token != "--volume" or i + 1 >= len(argv):
                continue
            spec = argv[i + 1]
            host, container, _mode = spec.rsplit(":", 2)
            if container == "/work":
                self.staging = Path(host)
            elif container == "/cache":
                self.npm_cache = Path(host)
        if self.staging is None:
            raise AssertionError("no writable /work mount found in run vector")
        if self.npm_cache is None:
            raise AssertionError("no writable /cache mount found in run vector")
        for path, resolved, integrity in self.tarballs:
            key = _model_tarball_id(resolved, integrity)
            entry = self.npm_cache / key
            if entry.exists():
                self.events.append(("hit", path))
            else:
                entry.write_bytes(b"opaque fake-executor cache marker")
                self.events.append(("download", path))
        for rel, (name, version, dependencies) in self.spec.items():
            _write_pkg(self.staging, rel, name, version, dependencies)
        root_rel = next(rel for rel in self.spec if rel.startswith("node_modules/"))
        (self.staging / root_rel / "marker.txt").write_bytes(self.marker)
        return ProcessResult(argv, 0, "", "")


class NetworkOutageExecutor:
    """Simulates a network outage at the ``docker run`` boundary.

    The assembly invocation raises (no network), while the follow-up
    ``docker rm -f`` cleanup succeeds, mirroring a real outage where image
    pull or registry access fails but container cleanup still works.
    """

    def __init__(self):
        self.calls: list[tuple[str, ...]] = []
        self.assembly_calls: list[tuple[str, ...]] = []

    def run(self, argv: tuple[str, ...]):
        self.calls.append(argv)
        if len(argv) >= 3 and tuple(argv[:3]) == ("docker", "rm", "-f"):
            return ProcessResult(argv, 0, "", "")
        self.assembly_calls.append(argv)
        raise OSError("network down")


class _AcceptanceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="npm-env-acceptance-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.cache_root = self.base / "cache"
        self.cache_root.mkdir()
        self.assembler = _assembler()

    def _namespace(self):
        return publication.prepare_assembler_namespace(
            self.cache_root, self.assembler.digest
        )


class TestSharedDownloadIndependentOutputs(_AcceptanceTestCase):
    """Task 6.1 — two neutral consumers share downloads, keep outputs apart."""

    def test_two_independent_locks_share_one_tarball_download(self):
        raw_a = _lock("consumer-a", "app-a", "shared")
        raw_b = _lock("consumer-b", "app-b", "shared")
        validated_a = _validated(raw_a, "app-a")
        validated_b = _validated(raw_b, "app-b")

        executor_a = PopulatingExecutor(
            _tree_spec("consumer-a", "app-a", "shared"), b"a", raw_a
        )
        executor_b = PopulatingExecutor(
            _tree_spec("consumer-b", "app-b", "shared"), b"b", raw_b
        )

        result_a = assemble_environment(
            validated=validated_a,
            assembler=self.assembler,
            cache_root=self.cache_root,
            executor=executor_a,
        )
        result_b = assemble_environment(
            validated=validated_b,
            assembler=self.assembler,
            cache_root=self.cache_root,
            executor=executor_b,
        )

        # Both runs mount the same opaque production cache directory, so the
        # fake executor's marker written by the first run is the same marker
        # the second run observes.
        self.assertIsNotNone(executor_a.npm_cache)
        self.assertEqual(executor_a.npm_cache, executor_b.npm_cache)
        self.assertEqual(executor_a.npm_cache, self._namespace().npm_cache)

        # Both assemblies actually executed (each is a distinct cache miss).
        self.assertEqual(len(executor_a.calls), 1)
        self.assertEqual(len(executor_b.calls), 1)

        # The shared transitive tarball is modelled as exactly one download
        # followed by one cache hit: the first lock downloads it, the second
        # reads the same opaque marker.
        shared_path = "node_modules/shared"
        self.assertEqual(
            [e for e in executor_a.events if e[1] == shared_path],
            [("download", shared_path)],
        )
        self.assertEqual(
            [e for e in executor_b.events if e[1] == shared_path],
            [("hit", shared_path)],
        )
        # Each lock's own root tarball is distinct and fetched exactly once.
        self.assertEqual(executor_a.events.count(("download", "node_modules/app-a")), 1)
        self.assertEqual(executor_b.events.count(("download", "node_modules/app-b")), 1)
        self.assertNotIn(("hit", "node_modules/app-a"), executor_a.events)
        self.assertNotIn(("hit", "node_modules/app-b"), executor_b.events)

        self.assertNotEqual(result_a.input_identity, result_b.input_identity)
        self.assertNotEqual(result_a.output_identity, result_b.output_identity)
        self.assertNotEqual(result_a.tree_digest, result_b.tree_digest)
        self.assertNotEqual(result_a.environment_root, result_b.environment_root)

        evidence_a = result_a.evidence_path.read_bytes()
        evidence_b = result_b.evidence_path.read_bytes()
        self.assertNotEqual(evidence_a, evidence_b)

        marker_a = result_a.environment_root / "node_modules" / "app-a" / "marker.txt"
        marker_b = result_b.environment_root / "node_modules" / "app-b" / "marker.txt"
        self.assertEqual(marker_a.read_bytes(), b"a")
        self.assertEqual(marker_b.read_bytes(), b"b")

        # Both remain fully verifiable, and the shared ``shared`` tarball is
        # present in each independent published tree.
        for result in (result_a, result_b):
            self.assertIsNotNone(
                verify_output(
                    self._namespace(),
                    result.output_identity,
                    input_identity=result.input_identity,
                )
            )
            self.assertTrue(
                (
                    result.environment_root / "node_modules" / "shared" / "package.json"
                ).is_file()
            )


class TestCacheAcceptance(_AcceptanceTestCase):
    """Task 6.2 — cold/warm cache, recovery, outage, and incomplete cache."""

    def _assemble(self, spec: dict, marker: bytes):
        raw = _lock("consumer-a", "app-a", "shared")
        return assemble_environment(
            validated=_validated(raw, "app-a"),
            assembler=self.assembler,
            cache_root=self.cache_root,
            executor=PopulatingExecutor(spec, marker, raw),
        )

    def test_cold_cache_runs_executor_and_publishes(self):
        raw = _lock("consumer-a", "app-a", "shared")
        executor = PopulatingExecutor(
            _tree_spec("consumer-a", "app-a", "shared"), b"a", raw
        )
        result = assemble_environment(
            validated=_validated(raw, "app-a"),
            assembler=self.assembler,
            cache_root=self.cache_root,
            executor=executor,
        )
        self.assertEqual(len(executor.calls), 1)
        self.assertIsInstance(result, AssemblyResult)
        self.assertIsNotNone(
            verify_output(
                self._namespace(),
                result.output_identity,
                input_identity=result.input_identity,
            )
        )

    def test_warm_cache_skips_executor(self):
        first = self._assemble(_tree_spec("consumer-a", "app-a", "shared"), b"a")
        raw = _lock("consumer-a", "app-a", "shared")
        executor = PopulatingExecutor(
            _tree_spec("consumer-a", "app-a", "shared"), b"a", raw
        )
        hit = assemble_environment(
            validated=_validated(raw, "app-a"),
            assembler=self.assembler,
            cache_root=self.cache_root,
            executor=executor,
        )
        self.assertEqual(executor.calls, [])
        self.assertEqual(hit.output_identity, first.output_identity)
        self.assertEqual(hit.environment_root, first.environment_root)

    def test_corruption_recovery_republishes(self):
        first = self._assemble(_tree_spec("consumer-a", "app-a", "shared"), b"a")
        marker = first.environment_root / "node_modules" / "app-a" / "marker.txt"
        marker.chmod(0o600)
        marker.parent.chmod(0o700)
        marker.write_text("tampered")

        recovered = self._assemble(_tree_spec("consumer-a", "app-a", "shared"), b"a")

        self.assertEqual(recovered.output_identity, first.output_identity)
        self.assertEqual(
            (
                recovered.environment_root / "node_modules" / "app-a" / "marker.txt"
            ).read_bytes(),
            b"a",
        )
        quarantined = [
            p
            for p in self._namespace().outputs.iterdir()
            if p.name.startswith(".corrupt-")
        ]
        self.assertEqual(len(quarantined), 1)

    def test_network_outage_with_valid_environment_uses_cache(self):
        first = self._assemble(_tree_spec("consumer-a", "app-a", "shared"), b"a")
        outage = NetworkOutageExecutor()
        hit = assemble_environment(
            validated=_validated(_lock("consumer-a", "app-a", "shared"), "app-a"),
            assembler=self.assembler,
            cache_root=self.cache_root,
            executor=outage,
        )
        # The warm, fully verified environment is returned without touching
        # the network boundary at all.
        self.assertEqual(outage.calls, [])
        self.assertEqual(hit.output_identity, first.output_identity)

    def test_incomplete_cache_with_outage_fails_structured(self):
        raw = _lock("consumer-a", "app-a", "shared")
        validated = _validated(raw, "app-a")
        input_identity = compute_assembler_input_identity(validated, self.assembler)
        namespace = self._namespace()

        # A real, incomplete published candidate: a partial tree directory
        # with no evidence.json and no manifest.  Such committed bytes can
        # never verify, so they must not be reused.
        stale_identity = "b" * 64
        candidate = namespace.outputs / stale_identity
        partial_tree = candidate / publication.TREE_CHILD / "node_modules" / "shared"
        partial_tree.mkdir(parents=True, exist_ok=True)
        (partial_tree / "package.json").write_text(
            json.dumps({"name": "shared", "version": "1.0.0"})
        )

        # Membership alone is not authoritative: point the non-authoritative
        # input index at the incomplete candidate.
        index = publication.index_path(namespace, input_identity.digest)
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_text(json.dumps([stale_identity]))

        outage = NetworkOutageExecutor()
        with self.assertRaises(LockedNpmError) as ctx:
            assemble_environment(
                validated=validated,
                assembler=self.assembler,
                cache_root=self.cache_root,
                executor=outage,
            )

        self.assertEqual(ctx.exception.reason, "executor_failure")
        self.assertIn("network down", ctx.exception.detail)

        # Cache verification rejected the incomplete candidate and assembly
        # was attempted exactly once (the single non-cleanup invocation).
        self.assertEqual(len(outage.assembly_calls), 1)
        self.assertEqual(outage.assembly_calls[0][:2], ("docker", "run"))

        # The incomplete candidate was not treated as a valid environment and
        # no new committed output was published.
        self.assertIsNone(
            verify_output(namespace, stale_identity, input_identity=input_identity)
        )
        self.assertEqual(
            {p.name for p in namespace.outputs.iterdir()},
            {stale_identity},
        )


if __name__ == "__main__":
    unittest.main()

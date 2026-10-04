"""Phase 5 — concurrency, storage, and cache-hit contracts (task 5.2).

Identical inputs that produce different assembled bytes must retain one
input identity yet receive distinct output identities, never overwrite or
alias each other, and be referenceable together from the non-authoritative
input index.  Reuse requires post-lock selection plus full recomputation of
the output identity, tree digest, evidence digest, and no-follow tree
verification; corrupted or substituted tree/evidence candidates are never
reused.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR.parent))

from docker.npm_environment import (
    AssemblerEvidence,
    AssemblerEvidenceBody,
    AssemblyResult,
    LockedNpmError,
    ProcessResult,
    RootSpec,
    assembler_script_digest,
    assemble_environment,
    compute_assembled_output_identity,
    compute_assembler_identity,
    evidence_body_digest,
    find_cached_result,
    identity_coordination_lock,
    npm_policy_digest,
    parse_evidence,
    preflight,
    publication,
    read_index,
    serialize_evidence,
    verify_output,
)

_IMAGE = "sha256:" + "a" * 64
_NODE = "24.18.0"
_NPM = "11.16.0"
_PLATFORM = "linux-x64"


def _sri() -> str:
    return "sha512-" + base64.b64encode(bytes([0xAB]) * 64).decode()


def _url(name: str, version: str) -> str:
    stem = name.rsplit("/", 1)[-1]
    return f"https://registry.npmjs.org/{name}/-/{stem}-{version}.tgz"


def _pkg(name: str, version: str, **extra: object) -> dict:
    node: dict = {
        "version": version,
        "resolved": _url(name, version),
        "integrity": _sri(),
    }
    node.update(extra)
    return node


def _lock() -> bytes:
    return json.dumps(
        {
            "name": "root",
            "version": "1.0.0",
            "lockfileVersion": 3,
            "requires": True,
            "packages": {
                "": {"name": "root", "version": "1.0.0", "dependencies": {"a": "1.0.0"}},
                "node_modules/a": _pkg("a", "1.0.0", dependencies={"b": "^1.0.0"}),
                "node_modules/b": _pkg("b", "1.0.0"),
            },
        }
    ).encode()


def _validated():
    return preflight(
        _lock(),
        roots=(RootSpec("a", "1.0.0"),),
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


def _write_pkg(root: Path, path: str, name: str, version: str, **extra: object) -> None:
    p = root / path / "package.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {"name": name, "version": version}
    data.update(extra)
    p.write_text(json.dumps(data))


def _write_tree(root: Path, marker: bytes) -> None:
    _write_pkg(root, "node_modules/a", "a", "1.0.0", dependencies={"b": "^1.0.0"})
    _write_pkg(root, "node_modules/b", "b", "1.0.0")
    _write_pkg(root, "", "root", "1.0.0")
    (root / "node_modules" / "a" / "marker.txt").write_bytes(marker)


def _make_writable(path: Path) -> None:
    """Make an immutable published file writable for corruption tests."""
    path.chmod(0o600)
    path.parent.chmod(0o700)


def _populate(staging: Path, marker: bytes) -> None:
    _write_pkg(staging, "node_modules/a", "a", "1.0.0", dependencies={"b": "^1.0.0"})
    _write_pkg(staging, "node_modules/b", "b", "1.0.0")
    _write_pkg(staging, "", "root", "1.0.0")
    (staging / "node_modules" / "a" / "marker.txt").write_bytes(marker)


class PopulatingExecutor:
    """Fake Docker executor that writes the assembled tree into staging."""

    def __init__(self, marker: bytes):
        self.marker = marker
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: tuple[str, ...]):
        self.calls.append(argv)
        staging: Path | None = None
        for i, token in enumerate(argv):
            if token == "--volume" and i + 1 < len(argv):
                spec = argv[i + 1]
                if spec.endswith(":/work:rw"):
                    staging = Path(spec.rsplit(":", 2)[0])
        if staging is not None:
            _populate(staging, self.marker)
        return ProcessResult(argv, 0, "", "")


class _ConcurrencyTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="npm-env-concurrency-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.cache_root = self.base / "cache"
        self.cache_root.mkdir()
        self.validated = _validated()
        self.assembler = _assembler()

    def _assemble(self, marker: bytes):
        return assemble_environment(
            validated=self.validated,
            assembler=self.assembler,
            cache_root=self.cache_root,
            executor=PopulatingExecutor(marker),
        )

    def _publish(self, marker: bytes):
        """Publish a freshly assembled tree directly (no cache-hit lookup)."""
        from docker.npm_environment import (
            compute_assembler_input_identity,
            publish_environment,
        )

        tree = self.base / f"tree-{marker.decode()}"
        tree.mkdir()
        _write_tree(tree, marker)
        input_identity = compute_assembler_input_identity(
            self.validated, self.assembler
        )
        return publish_environment(
            validated=self.validated,
            tree_root=tree,
            namespace=self.namespace,
            input_identity=input_identity,
        )

    @property
    def namespace(self):
        return publication.prepare_assembler_namespace(
            self.cache_root, self.assembler.digest
        )


class TestDistinctOutputsShareInputIdentity(_ConcurrencyTestCase):
    def test_identical_inputs_distinct_bytes_get_distinct_output_identities(self):
        first = self._publish(b"one")
        second = self._publish(b"two")

        self.assertIsInstance(first, AssemblyResult)
        self.assertIsInstance(second, AssemblyResult)
        self.assertEqual(first.input_identity, second.input_identity)
        self.assertNotEqual(first.tree_digest, second.tree_digest)
        self.assertNotEqual(first.output_identity, second.output_identity)
        self.assertNotEqual(first.environment_root, second.environment_root)

    def test_distinct_outputs_do_not_overwrite_or_alias(self):
        first = self._publish(b"one")
        second = self._publish(b"two")
        self.assertEqual(
            (first.environment_root / "node_modules" / "a" / "marker.txt").read_bytes(),
            b"one",
        )
        self.assertEqual(
            (second.environment_root / "node_modules" / "a" / "marker.txt").read_bytes(),
            b"two",
        )
        self.assertTrue(first.environment_root.exists())
        self.assertTrue(second.environment_root.exists())

    def test_both_outputs_are_referenced_by_the_input_index(self):
        first = self._publish(b"one")
        second = self._publish(b"two")
        indexed = read_index(self.namespace, first.input_identity.digest)
        self.assertIn(first.output_identity, indexed)
        self.assertIn(second.output_identity, indexed)


class TestCacheHitSelection(_ConcurrencyTestCase):
    def test_verified_cache_hit_skips_executor(self):
        first = self._assemble(b"one")
        executor = PopulatingExecutor(b"one")
        hit = assemble_environment(
            validated=self.validated,
            assembler=self.assembler,
            cache_root=self.cache_root,
            executor=executor,
        )
        self.assertEqual(executor.calls, [])
        self.assertEqual(hit.output_identity, first.output_identity)
        self.assertEqual(hit.environment_root, first.environment_root)
        self.assertEqual(hit.tree_digest, first.tree_digest)
        self.assertEqual(hit.evidence_digest, first.evidence_digest)

    def test_cache_hit_recomputes_all_bindings(self):
        first = self._assemble(b"one")
        namespace = publication.prepare_assembler_namespace(
            self.cache_root, self.assembler.digest
        )
        result = verify_output(
            namespace,
            first.output_identity,
            input_identity=first.input_identity,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.output_identity, first.output_identity)
        self.assertEqual(result.tree_digest, first.tree_digest)
        self.assertEqual(result.evidence_digest, first.evidence_digest)


class TestCorruptedAndSubstitutedCandidates(_ConcurrencyTestCase):
    def test_corrupted_tree_is_not_reused(self):
        first = self._assemble(b"one")
        target = first.environment_root / "node_modules" / "a" / "marker.txt"
        _make_writable(target)
        target.write_text("tampered")
        self.assertIsNone(
            verify_output(
                self.namespace, first.output_identity, input_identity=first.input_identity
            )
        )
        self.assertIsNone(
            find_cached_result(namespace=self.namespace, input_identity=first.input_identity)
        )

    def test_substituted_evidence_is_not_reused(self):
        first = self._assemble(b"one")
        second = self._publish(b"two")
        # Swap second's evidence over first's output: the envelope no longer
        # matches the stored tree/manifest and must be rejected.
        second_evidence = (self.namespace.outputs / second.output_identity / "evidence.json").read_bytes()
        target = self.namespace.outputs / first.output_identity / "evidence.json"
        _make_writable(target)
        target.write_bytes(second_evidence)
        self.assertIsNone(
            verify_output(
                self.namespace, first.output_identity, input_identity=first.input_identity
            )
        )

    def test_substituted_manifest_is_not_reused(self):
        first = self._assemble(b"one")
        second = self._publish(b"two")
        # Swap the manifest: its digest no longer matches the stored tree.
        second_manifest = (self.namespace.outputs / second.output_identity / "manifest.json").read_bytes()
        target = self.namespace.outputs / first.output_identity / "manifest.json"
        _make_writable(target)
        target.write_bytes(second_manifest)
        self.assertIsNone(
            verify_output(
                self.namespace, first.output_identity, input_identity=first.input_identity
            )
        )

    def test_evidence_metadata_must_match_exact_manifest(self):
        # Evidence whose tree entries share the same content fields and
        # content-only tree digest, but whose entry metadata (mode/uid/gid)
        # differs from the stored manifest, must not be reused even when the
        # envelope is internally consistent (evidence digest and output
        # identity recomputed).
        first = self._assemble(b"one")
        out = self.namespace.outputs / first.output_identity
        evidence = parse_evidence((out / "evidence.json").read_bytes())
        manifest_bytes = (out / "manifest.json").read_bytes()

        entries = list(evidence.body.tree_entries)
        file_entry = next(e for e in entries if e.kind == "file")
        altered = dataclasses.replace(file_entry, mode=file_entry.mode ^ 0o400)
        entries[entries.index(file_entry)] = altered
        new_body = AssemblerEvidenceBody(
            input_identity=evidence.body.input_identity,
            tree_digest=evidence.body.tree_digest,
            tree_entries=tuple(sorted(entries)),
            packages=evidence.body.packages,
            omitted_optionals=evidence.body.omitted_optionals,
            integrity_less=evidence.body.integrity_less,
            root_metadata=evidence.body.root_metadata,
            npm_policy_flags=evidence.body.npm_policy_flags,
        )
        new_evidence_digest = evidence_body_digest(new_body)
        new_output_identity = compute_assembled_output_identity(
            first.input_identity, evidence.body.tree_digest, new_evidence_digest
        )
        new_evidence = AssemblerEvidence(
            output_identity=new_output_identity.digest,
            input_identity=first.input_identity,
            tree_digest=evidence.body.tree_digest,
            evidence_digest=new_evidence_digest,
            body=new_body,
        )

        # Build a fully populated output directory for the new identity whose
        # stored manifest (and tree) still carry the original metadata.
        new_out = self.namespace.outputs / new_output_identity.digest
        new_out.mkdir(mode=0o700)
        shutil.copytree(out / "tree", new_out / "tree")
        (new_out / "manifest.json").write_bytes(manifest_bytes)
        (new_out / "evidence.json").write_bytes(serialize_evidence(new_evidence))

        self.assertIsNone(
            verify_output(
                self.namespace,
                new_output_identity.digest,
                input_identity=first.input_identity,
            )
        )


class TestUnindexedCollision(_ConcurrencyTestCase):
    """Collision handling when the input index misses the existing output."""

    def _clear_index(self, input_identity_digest: str) -> None:
        index = publication.index_path(self.namespace, input_identity_digest)
        if index.exists():
            index.unlink()

    def _staging_workspaces(self) -> list[Path]:
        return [p for p in self.namespace.staging.iterdir() if p.is_dir()]

    def _committed_outputs(self) -> list[Path]:
        return [
            p
            for p in self.namespace.outputs.iterdir()
            if p.is_dir() and not p.name.startswith(".tmp-")
        ]

    def test_unindexed_collision_returns_existing_and_removes_redundant_staging(self):
        first = self._assemble(b"one")
        evidence_path = self.namespace.outputs / first.output_identity / "evidence.json"
        evidence_before = evidence_path.read_bytes()
        marker_before = (
            first.environment_root / "node_modules" / "a" / "marker.txt"
        ).read_bytes()

        # Omit the index entry so cache lookup misses and reassembly occurs.
        self._clear_index(first.input_identity.digest)

        hit = self._assemble(b"one")

        # The existing verified output is returned unchanged.
        self.assertEqual(hit.output_identity, first.output_identity)
        self.assertEqual(hit.environment_root, first.environment_root)
        self.assertEqual(evidence_path.read_bytes(), evidence_before)
        self.assertEqual(
            (first.environment_root / "node_modules" / "a" / "marker.txt").read_bytes(),
            marker_before,
        )
        # The redundant staging workspace no longer exists.
        self.assertEqual(self._staging_workspaces(), [])
        # No committed output was overwritten (exactly one output remains).
        self.assertEqual(
            self._committed_outputs(),
            [self.namespace.outputs / first.output_identity],
        )

    def test_collision_cleanup_failure_is_observable(self):
        first = self._assemble(b"one")
        evidence_path = self.namespace.outputs / first.output_identity / "evidence.json"
        evidence_before = evidence_path.read_bytes()
        marker_before = (
            first.environment_root / "node_modules" / "a" / "marker.txt"
        ).read_bytes()

        self._clear_index(first.input_identity.digest)
        staging_path = self.namespace.staging / first.input_identity.digest

        with mock.patch.object(
            publication, "_remove_redundant_tree", side_effect=OSError("delete failed")
        ):
            with self.assertRaises(LockedNpmError) as ctx:
                self._assemble(b"one")

        self.assertEqual(ctx.exception.reason, "collision_cleanup_failed")
        self.assertIn(str(staging_path), ctx.exception.detail)
        self.assertIn("delete failed", ctx.exception.detail)
        self.assertIn("residue", ctx.exception.detail)
        self.assertIn("verified successfully", ctx.exception.detail)
        # The verified committed output remains untouched.
        self.assertEqual(evidence_path.read_bytes(), evidence_before)
        self.assertEqual(
            (first.environment_root / "node_modules" / "a" / "marker.txt").read_bytes(),
            marker_before,
        )


class TestCorruptOutputRecovery(_ConcurrencyTestCase):
    """A corrupt committed output is quarantined and reconstructed."""

    def test_corrupt_candidate_recovered_on_reassembly(self):
        first = self._assemble(b"one")
        target = first.environment_root / "node_modules" / "a" / "marker.txt"
        _make_writable(target)
        target.write_text("tampered")

        recovered = self._assemble(b"one")

        # Locked reconstruction republishes a fully verified replacement at
        # the same identity; the corrupt bytes were not reused.
        self.assertEqual(recovered.output_identity, first.output_identity)
        self.assertIsNotNone(
            verify_output(
                self.namespace,
                recovered.output_identity,
                input_identity=first.input_identity,
            )
        )
        self.assertEqual(
            (
                recovered.environment_root
                / "node_modules"
                / "a"
                / "marker.txt"
            ).read_bytes(),
            b"one",
        )
        quarantined = [
            p
            for p in self.namespace.outputs.iterdir()
            if p.name.startswith(".corrupt-")
        ]
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(
            (
                quarantined[0] / "tree" / "node_modules" / "a" / "marker.txt"
            ).read_bytes(),
            b"tampered",
        )


class TestIndexHealing(_ConcurrencyTestCase):
    """The verified-collision path heals a missing or corrupt index."""

    def _remove_index(self, input_identity_digest: str) -> None:
        publication.index_path(
            self.namespace, input_identity_digest
        ).unlink(missing_ok=True)

    def _corrupt_index(self, input_identity_digest: str) -> None:
        publication.index_path(
            self.namespace, input_identity_digest
        ).write_text("not json")

    def _assert_healed_and_cache_hit(self, first) -> None:
        indexed = read_index(self.namespace, first.input_identity.digest)
        self.assertIn(first.output_identity, indexed)
        executor = PopulatingExecutor(b"one")
        hit = assemble_environment(
            validated=self.validated,
            assembler=self.assembler,
            cache_root=self.cache_root,
            executor=executor,
        )
        self.assertEqual(executor.calls, [])
        self.assertEqual(hit.output_identity, first.output_identity)

    def test_missing_index_healed_on_verified_collision(self):
        first = self._assemble(b"one")
        self._remove_index(first.input_identity.digest)

        hit = self._assemble(b"one")

        self.assertEqual(hit.output_identity, first.output_identity)
        self._assert_healed_and_cache_hit(first)

    def test_corrupt_index_healed_on_verified_collision(self):
        first = self._assemble(b"one")
        self._corrupt_index(first.input_identity.digest)

        hit = self._assemble(b"one")

        self.assertEqual(hit.output_identity, first.output_identity)
        self._assert_healed_and_cache_hit(first)


class TestCoordinationLock(unittest.TestCase):
    def test_identity_lock_is_exclusive_and_released(self):
        tmp = tempfile.TemporaryDirectory(prefix="npm-env-lock-")
        self.addCleanup(tmp.cleanup)
        cache_root = Path(tmp.name) / "cache"
        cache_root.mkdir()
        namespace = publication.prepare_assembler_namespace(cache_root, "a" * 64)
        # The lock composes the shared blocking capability under the
        # input-identity namespace and is released on scope exit.
        with mock.patch.object(
            publication.LockCapability,
            "acquire",
            wraps=publication.LockCapability.acquire,
        ) as acquire:
            with identity_coordination_lock(namespace, "b" * 64):
                self.assertTrue(
                    (namespace.locks / ("b" * 64 + ".lock")).is_file()
                )
        self.assertEqual(acquire.call_count, 1)
        self.assertIs(acquire.call_args.kwargs["policy"], publication.LockPolicy.BLOCK)
        self.assertEqual(acquire.call_args.kwargs["namespace"], "b" * 64)
        # Released: a fresh acquisition succeeds immediately.
        with identity_coordination_lock(namespace, "b" * 64):
            pass


if __name__ == "__main__":
    unittest.main()

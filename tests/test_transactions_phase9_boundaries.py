"""Phase 9 architecture boundaries for the L0-L3 filesystem substrate."""
from __future__ import annotations

import ast
import inspect
import json
import unittest
from pathlib import Path

from docker import transactions
from docker.transactions import capabilities, codec, regular
from docker.versioning import artifact_cache, build_cache, effective, project_state, rendering
from docker.npm_environment import publication

_REPO = Path(__file__).resolve().parents[1]
_MATRIX = _REPO / "tests/data/filesystem_writer_matrix.json"
_L2_CONTRACTS = {
    "atomic_no_clobber", "durable_no_clobber", "durable_replace",
    "validated_read", "durable_unlink",
}
_REQUIRED_DIRECT_ADOPTION = {
    "docker/versioning/build_cache.py": {"durable_replace", "durable_unlink"},
    "docker/versioning/build_generations.py": {"durable_no_clobber"},
    "docker/versioning/project_state.py": {"durable_no_clobber"},
    "docker/versioning/rendering.py": {"durable_replace"},
    "docker/versioning/effective.py": {"atomic_no_clobber"},
}


def _source(path: str) -> str:
    return (_REPO / path).read_text(encoding="utf-8")


class GenericAbstractionRejectionTests(unittest.TestCase):
    def test_no_generic_atomic_write(self) -> None:
        self.assertFalse(hasattr(regular, "atomic_write"))
        self.assertNotIn("def atomic_write", _source("docker/transactions/regular.py"))

    def test_l2_contracts_are_explicitly_named(self) -> None:
        self.assertTrue(_L2_CONTRACTS.issubset(set(dir(regular.RegularFileContracts))))
        self.assertIn("RegularFileContracts", transactions.__all__)

    def test_no_durability_boolean_parameters(self) -> None:
        for name in _L2_CONTRACTS:
            method = getattr(regular.RegularFileContracts, name)
            params = inspect.signature(method).parameters
            self.assertTrue({"durable", "durability", "fsync"}.isdisjoint(params), name)

    def test_no_project_wide_path_vfs(self) -> None:
        public = set(transactions.__all__)
        self.assertTrue({"VFS", "VirtualFilesystem", "Filesystem"}.isdisjoint(public))

    def test_capabilities_never_reconstruct_a_pathname(self) -> None:
        src = inspect.getsource(capabilities)
        self.assertNotIn("/proc/self/fd", src)
        self.assertNotIn("os.readlink", src)

    def test_no_shared_envelope_api(self) -> None:
        public = set(transactions.__all__)
        self.assertTrue(all("Envelope" not in name and "Journal" not in name for name in public))
        public_callables = {
            name for name, value in vars(codec).items()
            if not name.startswith("_") and callable(value) and getattr(value, "__module__", None) == codec.__name__
        }
        self.assertEqual({"encode", "decode"}, public_callables)

    def test_no_generic_tree_or_content_addressed_authority(self) -> None:
        public = set(transactions.__all__)
        forbidden = ("Tree", "Blob", "Digest", "ContentAddressed", "Snapshot", "Quarantine")
        self.assertFalse(any(any(token in name for token in forbidden) for name in public))

    def test_shared_layers_do_not_interpret_domain_paths_or_deletions(self) -> None:
        source = "\n".join(_source(f"docker/transactions/{name}.py") for name in (
            "capabilities", "codec", "locking", "posix", "regular",
        ))
        for token in ("committed-build", "sha256", "package-lock", "settings.json"):
            self.assertNotIn(token, source)


class ExceptionBoundaryTests(unittest.TestCase):
    def test_domain_packages_do_not_reexport_shared_exceptions(self) -> None:
        for module in (artifact_cache, build_cache, effective, project_state, rendering, publication):
            exported = set(getattr(module, "__all__", ()))
            self.assertTrue(all(not name.endswith(("CapabilityError", "PublicationError")) for name in exported))

    def test_domain_adapters_translate_shared_failures(self) -> None:
        self.assertIn("EffectiveConfigError", inspect.getsource(effective.create_runtime_projection))
        self.assertIn("ProjectStateError", inspect.getsource(project_state._publish_metadata))
        self.assertIn("EffectiveInventoryOutputError", inspect.getsource(rendering.write_effective_build))

    def test_build_domain_keeps_its_own_diagnostics(self) -> None:
        source = inspect.getsource(build_cache)
        self.assertIn("BuildCacheError", source)
        self.assertNotIn("raise PublicationError", source)


class RequiredAdoptionSetTests(unittest.TestCase):
    def test_every_required_consumer_adopts_its_l2_contract(self) -> None:
        for path, contracts in _REQUIRED_DIRECT_ADOPTION.items():
            source = _source(path)
            self.assertIn("RegularFileContracts", source, path)
            for contract in contracts:
                self.assertIn(f".{contract}(", source, f"{path}: {contract}")

    def test_user_directed_effective_inventory_output_is_not_required(self) -> None:
        source = inspect.getsource(rendering.write_effective_inventory)
        self.assertNotIn("RegularFileContracts", source)

    def test_adoption_set_is_a_migration_obligation_not_a_permission_boundary(self) -> None:
        self.assertIn("RegularFileContracts", inspect.getsource(publication))
        self.assertIn("durable_replace", inspect.getsource(publication._append_index))

    def test_runtime_artifact_protocol_composes_only_the_lock_leaf(self) -> None:
        source = inspect.getsource(artifact_cache)
        self.assertIn("LockCapability", source)
        self.assertNotIn("RegularFileContracts", source)

    def test_future_metadata_records_are_owned_by_their_change(self) -> None:
        plan = _source("openspec/changes/revalidate-update-metadata/design.md")
        self.assertIn("metadata domain", plan)
        self.assertIn("shared layer receives bytes", plan)


class SpecializationBoundaryTests(unittest.TestCase):
    def test_shared_substrate_exposes_no_domain_protocol_api(self) -> None:
        names = set(transactions.__all__)
        self.assertTrue(all(not any(word in name.lower() for word in (
            "tree", "blob", "snapshot", "quarantine", "index", "evidence",
        )) for name in names))

    def test_blob_publication_keeps_content_addressed_authority(self) -> None:
        self.assertIn("derive_cache_path", inspect.getsource(artifact_cache))
        self.assertIn("atomic_publish", inspect.getsource(artifact_cache.LocalCacheFilesystem))

    def test_npm_tree_commit_stays_domain_owned(self) -> None:
        source = _source("docker/npm_environment/publication.py")
        self.assertIn("os.rename(str(tmp), str(final))", source)

    def test_snapshot_and_confinement_remain_domain_owned(self) -> None:
        # Snapshots keep their own hard-link/copy, finalisation, and
        # recursive-cleanup authority.  They may use the internal
        # primary-preserving cleanup accumulator (Phase 9A) but must not adopt
        # the L2 regular-file contracts or L0/L1 path substrate.
        snapshot = _source("docker/versioning/build_snapshot.py")
        confinement = _source("docker/versioning/build_context_confinement.py")
        for forbidden in ("RegularFileContracts", "PosixFileOps", "DirectoryCapability"):
            self.assertNotIn(forbidden, snapshot)
        self.assertNotIn("docker.transactions", confinement)
        self.assertIn("shutil.rmtree", snapshot)

    def test_quarantine_and_recursive_cleanup_remain_domain_owned(self) -> None:
        self.assertIn("quarantine", inspect.getsource(artifact_cache).lower())
        self.assertIn("rmtree", _source("docker/npm_environment/publication.py"))

    def test_advisory_index_policy_remains_domain_owned(self) -> None:
        source = inspect.getsource(publication._append_index)
        self.assertIn("durable_replace", source)
        self.assertIn("except", source)

    def test_user_and_evidence_outputs_remain_domain_owned(self) -> None:
        self.assertNotIn("RegularFileContracts", inspect.getsource(rendering.write_effective_inventory))
        self.assertNotIn("docker.transactions", _source("docker/versioning/evidence.py"))


class WriterInventoryTests(unittest.TestCase):
    def test_every_detected_production_writer_is_classified(self) -> None:
        data = json.loads(_MATRIX.read_text(encoding="utf-8"))
        classified = {entry["module"] for entry in data["writers"]}
        writer_markers = (
            "os.write(", "os.replace(", "os.rename(", "os.link(",
            "os.unlink(", "os.chmod(", "os.mkdir(", "os.makedirs(",
            ".mkdir(", "os.rmdir(", "os.symlink(", "write_text(",
            "write_bytes(", "shutil.rmtree(", "fcntl.flock(",
            ".durable_replace(", ".durable_no_clobber(",
            ".atomic_no_clobber(", ".durable_unlink(",
        )
        detected = set()
        for path in (_REPO / "docker").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if any(marker in text for marker in writer_markers):
                detected.add(path.relative_to(_REPO).as_posix())
        self.assertEqual(set(), detected - classified)

    def test_every_inventory_entry_records_layer_and_authority(self) -> None:
        data = json.loads(_MATRIX.read_text(encoding="utf-8"))
        for entry in data["writers"]:
            self.assertTrue((_REPO / entry["module"]).is_file(), entry["module"])
            self.assertTrue(entry["owner"])
            self.assertTrue(entry["owning_layer"])
            self.assertTrue(entry["shared_layer"])
            self.assertTrue(entry.get("decision") or entry.get("mismatch_justification"), entry["module"])


class DirectSyscallBoundaryTests(unittest.TestCase):
    def test_only_the_l0_backend_issues_flock(self) -> None:
        offenders = []
        for path in (_REPO / "docker").rglob("*.py"):
            rel = path.relative_to(_REPO).as_posix()
            text = path.read_text(encoding="utf-8")
            if "fcntl.flock(" in text and rel != "docker/transactions/posix.py":
                offenders.append(rel)
        self.assertEqual([], offenders)

    def test_required_adoption_control_writers_have_no_duplicate_helper(self) -> None:
        for path in _REQUIRED_DIRECT_ADOPTION:
            tree = ast.parse(_source(path))
            names = {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
            self.assertTrue({"atomic_write", "durable_write", "write_atomic"}.isdisjoint(names), path)

    def test_justified_descends_are_documented(self) -> None:
        data = json.loads(_MATRIX.read_text(encoding="utf-8"))
        descents = [entry for entry in data["writers"] if entry["shared_layer"] in {"L0", "L1"}]
        self.assertTrue(descents)
        for entry in descents:
            self.assertTrue(entry.get("mismatch_justification"), entry["module"])


if __name__ == "__main__":
    unittest.main()

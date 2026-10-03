"""External project-state persistence contracts: projection and build transaction state."""
from __future__ import annotations
import hashlib, json, os, shutil, stat, tempfile, types, unittest
from pathlib import Path
from unittest import mock
from docker.versioning.effective import resolve_build_projection
from docker.versioning.inventory import load_inventory
from docker.versioning.rendering import EffectiveInventoryOutputError, write_effective_build
from docker.versioning.project_state import (ProjectState, ProjectStateError, resolve_project_state, validate_project_state)
import docker.versioning.build_cache as build_cache
from docker.versioning.cache_storage import (
    prepare_resolved_root,
    resolve_default_root,
    resolve_effective_root,
)
from docker.versioning.build_cache import (BuildCacheError, BuildTransactionError, UNCOMMITTED_TTL_SECONDS, acquire_constructor_project_build_lock, build_blob_path, commit_build_set, maintain_uncommitted_blobs, mark_uncommitted_blob, prepare_build_cache, publish_uncommitted_blob, publish_verified_blob, recover_abandoned_snapshots)
from docker.versioning.digest_identity import DigestIdentity
_REPO=Path(__file__).resolve().parents[1]


def _newest_generation_blobs(paths):
    candidates=sorted(entry.name for entry in paths.persistent_root.iterdir() if entry.name.startswith('committed-build-'))
    return json.loads((paths.persistent_root/candidates[-1]).read_text())['blobs']


def _generation_files(paths):
    return sorted(entry.name for entry in paths.persistent_root.iterdir() if entry.name.startswith('committed-build-'))

class ExternalPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'constructor'; self.root.mkdir(); self.cache=Path(self.tmp.name)/'cache'; self.cache.mkdir(mode=0o700)
        self.state=resolve_project_state(self.root,cache_root=self.cache)
        self.projection=resolve_build_projection(load_inventory(_REPO/'docker-constructor.toml').build,{})
    def identity(self,data): return DigestIdentity.from_hex('sha256',hashlib.sha256(data).hexdigest())

    def test_projection_atomically_replaces_only_external_generated_destination(self):
        dest=self.state.generated_root/'docker-constructor.build.effective.toml'
        dest.write_text('old'); os.chmod(dest,0o600)
        with mock.patch('os.replace',wraps=os.replace) as replace:
            result=write_effective_build(self.projection,repo_root=self.root,project_state=self.state)
        self.assertEqual(result,dest); self.assertNotIn('old',dest.read_text())
        # os.replace is invoked descriptor-relatively with one retained fd.
        src_name, dst_name = replace.call_args.args
        self.assertEqual(dst_name, 'docker-constructor.build.effective.toml')
        self.assertTrue(src_name.startswith('.build-effective-'), src_name)
        self.assertEqual(replace.call_args.kwargs['src_dir_fd'],
                         replace.call_args.kwargs['dst_dir_fd'])
        self.assertFalse((self.root/'.docker-generated').exists())

    def test_failed_publication_cleans_temporary_and_preserves_prior_external_state(self):
        dest=self.state.generated_root/'docker-constructor.build.effective.toml'
        dest.write_text('prior'); os.chmod(dest,0o600)
        before=set(self.state.generated_root.iterdir())
        with mock.patch('os.replace',side_effect=OSError('interrupted publication')):
            with self.assertRaises(OSError): write_effective_build(self.projection,repo_root=self.root,project_state=self.state)
        self.assertEqual(dest.read_text(),'prior'); self.assertEqual(set(self.state.generated_root.iterdir()),before)

    def test_symlink_swap_of_generated_during_publication_fails_closed(self):
        state=self.state
        outside=self.root.parent/'outside'; outside.mkdir()
        real_open=os.open; swapped=False
        def swap_before_generated(name, flags, *args, **kwargs):
            nonlocal swapped
            if name=='generated' and 'dir_fd' in kwargs and not swapped:
                swapped=True
                real=state.generated_root
                os.rename(real, real.with_name('generated-real'))
                os.symlink(str(outside), real, target_is_directory=True)
            return real_open(name, flags, *args, **kwargs)
        with mock.patch('os.open', side_effect=swap_before_generated):
            with self.assertRaises((EffectiveInventoryOutputError, OSError)):
                write_effective_build(self.projection, repo_root=self.root, project_state=self.state)
        self.assertTrue(swapped)
        # The external directory is untouched and no projection/temp escaped.
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((outside/'docker-constructor.build.effective.toml').exists())
        self.assertFalse((self.root/'.docker-generated').exists())

    def test_publication_rejects_symlinked_generated_without_repair(self):
        state=self.state; outside=self.root.parent/'outside'; outside.mkdir()
        real=state.generated_root; real.rmdir()
        state.generated_root.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(EffectiveInventoryOutputError):
            write_effective_build(self.projection, repo_root=self.root, project_state=self.state)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((state.namespace/'docker-constructor.build.effective.toml').exists())

    def test_publication_rejects_permissive_generated_without_repair(self):
        state=self.state
        os.chmod(state.generated_root, 0o755)
        self.addCleanup(os.chmod, state.generated_root, 0o700)
        with self.assertRaises(EffectiveInventoryOutputError):
            write_effective_build(self.projection, repo_root=self.root, project_state=self.state)
        self.assertEqual(stat.S_IMODE(state.generated_root.stat().st_mode), 0o755)
        self.assertFalse((state.generated_root/'docker-constructor.build.effective.toml').exists())

    def test_publication_rejects_foreign_owned_generated_without_repair(self):
        state=self.state; target=os.path.realpath(str(state.generated_root))
        real_fstat=os.fstat
        def foreign(fd, *a, **kw):
            value=real_fstat(fd, *a, **kw)
            try: name=os.path.realpath(os.readlink(f'/proc/self/fd/{fd}'))
            except OSError: name=''
            return types.SimpleNamespace(st_mode=value.st_mode, st_uid=os.geteuid()+1, st_gid=value.st_gid) if name==target else value
        with mock.patch('os.fstat', side_effect=foreign):
            with self.assertRaises(EffectiveInventoryOutputError):
                write_effective_build(self.projection, repo_root=self.root, project_state=self.state)
        self.assertFalse((state.generated_root/'docker-constructor.build.effective.toml').exists())

    def test_publication_rejects_symlink_or_nonregular_destination(self):
        state=self.state; dest=state.generated_root/'docker-constructor.build.effective.toml'
        outside=self.root.parent/'outside-dest'; outside.write_text('target')
        dest.symlink_to(outside)
        with self.assertRaises(EffectiveInventoryOutputError):
            write_effective_build(self.projection, repo_root=self.root, project_state=self.state)
        self.assertEqual(outside.read_text(), 'target')
        dest.unlink(); dest.mkdir()
        with self.assertRaises(EffectiveInventoryOutputError):
            write_effective_build(self.projection, repo_root=self.root, project_state=self.state)
        self.assertTrue(dest.is_dir())

    def _forged_state(self, **overrides):
        state = self.state
        fields = dict(project_path=state.project_path, cache_root=state.cache_root,
                      namespace=state.namespace, identity=state.identity,
                      generated_root=state.generated_root, runtime_root=state.runtime_root,
                      evidence_root=state.evidence_root,
                      build_artifacts_root=state.build_artifacts_root,
                      transactions_root=state.transactions_root)
        fields.update(overrides)
        return ProjectState(**fields)

    def _consistent_state_for_root(self, cache_root):
        ns = cache_root / 'projects' / self.state.namespace.name
        return ProjectState(
            project_path=self.state.project_path,
            cache_root=cache_root,
            namespace=ns,
            identity=self.state.identity,
            generated_root=ns / 'generated',
            runtime_root=ns / 'runtime',
            evidence_root=ns / 'evidence',
            build_artifacts_root=ns / 'build-artifacts',
            transactions_root=ns / 'transactions',
        )

    def _generated_snapshot(self):
        gen = self.state.generated_root
        entries = []
        for p in sorted(gen.rglob('*')):
            st = p.lstat()
            kind = 'dir' if p.is_dir() else ('symlink' if p.is_symlink() else 'file')
            entries.append((str(p.relative_to(gen)), kind, stat.S_IMODE(st.st_mode)))
        return tuple(entries)

    def _project_json_bytes(self, *, version=1, path=None, digest=None):
        doc = {"version": version,
               "canonical_path": str(self.state.project_path) if path is None else path,
               "sha256": self.state.identity if digest is None else digest}
        return (json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n").encode()

    def _write_project_json(self, *, version=1, path=None, digest=None):
        p = self.state.namespace / 'project.json'
        p.write_bytes(self._project_json_bytes(version=version, path=path, digest=digest))
        os.chmod(p, 0o600)

    def _foreign_project_json_fstat(self):
        target = os.path.realpath(str(self.state.namespace / 'project.json'))
        real_fstat = os.fstat
        def foreign(fd, *args, **kwargs):
            value = real_fstat(fd, *args, **kwargs)
            try:
                name = os.path.realpath(os.readlink(f'/proc/self/fd/{fd}'))
            except OSError:
                name = ''
            if name == target:
                return types.SimpleNamespace(st_mode=value.st_mode,
                                             st_uid=os.geteuid() + 1,
                                             st_gid=value.st_gid)
            return value
        return foreign

    def test_forged_identity_is_rejected_without_files(self):
        forged = self._forged_state(identity='0' * 64)
        with self.assertRaises(ProjectStateError):
            validate_project_state(forged)
        before = self._generated_snapshot()
        with self.assertRaises(EffectiveInventoryOutputError):
            write_effective_build(self.projection, repo_root=self.root, project_state=forged)
        self.assertEqual(self._generated_snapshot(), before)

    def test_forged_unrelated_cache_root_is_rejected_without_files(self):
        unrelated = self.root.parent / 'unrelated-cache'; unrelated.mkdir(mode=0o700)
        forged = self._forged_state(cache_root=unrelated)
        with self.assertRaises(ProjectStateError):
            validate_project_state(forged)
        with self.assertRaises(EffectiveInventoryOutputError):
            write_effective_build(self.projection, repo_root=self.root, project_state=forged)
        self.assertEqual(list(unrelated.iterdir()), [])

    def test_forged_unrelated_namespace_is_rejected_without_files(self):
        unrelated_ns = self.root.parent / 'unrelated-ns'; unrelated_ns.mkdir(mode=0o700)
        forged = self._forged_state(namespace=unrelated_ns)
        with self.assertRaises(ProjectStateError):
            validate_project_state(forged)
        with self.assertRaises(EffectiveInventoryOutputError):
            write_effective_build(self.projection, repo_root=self.root, project_state=forged)
        self.assertEqual(list(unrelated_ns.iterdir()), [])

    def test_forged_generated_root_is_rejected_without_files(self):
        forged_loc = self.root.parent / 'forged-generated'; forged_loc.mkdir(mode=0o700)
        forged = self._forged_state(generated_root=forged_loc)
        with self.assertRaises(ProjectStateError):
            validate_project_state(forged)
        with self.assertRaises(EffectiveInventoryOutputError):
            write_effective_build(self.projection, repo_root=self.root, project_state=forged)
        self.assertEqual(list(forged_loc.iterdir()), [])

    def test_replaced_namespace_with_missing_metadata_is_rejected(self):
        state = self.state
        moved = state.namespace.with_name(state.namespace.name + '-moved')
        state.namespace.rename(moved)
        state.namespace.mkdir(mode=0o700)
        with self.assertRaises(EffectiveInventoryOutputError):
            write_effective_build(self.projection, repo_root=self.root, project_state=self.state)
        self.assertEqual(list(state.namespace.iterdir()), [])
        self.assertEqual(list((moved / 'generated').iterdir()), [])

    def test_replaced_namespace_with_mismatched_metadata_is_rejected(self):
        state = self.state
        moved = state.namespace.with_name(state.namespace.name + '-moved')
        state.namespace.rename(moved)
        state.namespace.mkdir(mode=0o700)
        (state.namespace / 'project.json').write_bytes(self._project_json_bytes(digest='0' * 64))
        os.chmod(state.namespace / 'project.json', 0o600)
        with self.assertRaises(EffectiveInventoryOutputError):
            write_effective_build(self.projection, repo_root=self.root, project_state=self.state)
        self.assertEqual(sorted(p.name for p in state.namespace.iterdir()), ['project.json'])
        self.assertEqual(list((moved / 'generated').iterdir()), [])

    def test_projects_symlink_replacement_is_rejected_without_external_writes(self):
        state = self.state
        cache_projects = self.cache / 'projects'
        real_projects = self.cache / 'projects-real'
        cache_projects.rename(real_projects)
        external = self.root.parent / 'external-projects'; external.mkdir(mode=0o700)
        cache_projects.symlink_to(external, target_is_directory=True)
        # A valid-looking namespace and metadata live in the external target,
        # but the symlinked `projects` ancestor must never be followed.
        ns_name = state.namespace.name
        ext_ns = external / ns_name; ext_ns.mkdir(mode=0o700)
        (ext_ns / 'project.json').write_bytes(self._project_json_bytes())
        os.chmod(ext_ns / 'project.json', 0o600)
        with self.assertRaises(ProjectStateError):
            validate_project_state(self.state)
        with self.assertRaises(EffectiveInventoryOutputError):
            write_effective_build(self.projection, repo_root=self.root, project_state=self.state)
        # Nothing escaped into the external target; the symlink was not repaired.
        self.assertEqual(sorted(p.name for p in external.iterdir()), [ns_name])
        self.assertEqual(sorted(p.name for p in ext_ns.iterdir()), ['project.json'])
        self.assertTrue(cache_projects.is_symlink())

    def test_consistent_forged_cache_root_with_unsafe_cache_root_mode_is_rejected(self):
        unrelated = self.root.parent / 'unrelated-cache'; unrelated.mkdir(mode=0o755)
        ns = unrelated / 'projects' / self.state.namespace.name
        ns.mkdir(parents=True, mode=0o700)
        (ns / 'project.json').write_bytes(self._project_json_bytes())
        os.chmod(ns / 'project.json', 0o600)
        generated = ns / 'generated'; generated.mkdir(mode=0o700)
        forged = self._consistent_state_for_root(unrelated)
        before = tuple(sorted(p.name for p in generated.iterdir()))
        with self.assertRaises(ProjectStateError):
            validate_project_state(forged)
        with self.assertRaises(EffectiveInventoryOutputError):
            write_effective_build(self.projection, repo_root=self.root, project_state=forged)
        self.assertEqual(tuple(sorted(p.name for p in generated.iterdir())), before)
        self.assertFalse((generated / 'docker-constructor.build.effective.toml').exists())

    def test_consistent_forged_cache_root_with_unsafe_projects_mode_is_rejected(self):
        unrelated = self.root.parent / 'unrelated-cache'; unrelated.mkdir(mode=0o700)
        projects = unrelated / 'projects'; projects.mkdir(mode=0o755)
        ns = projects / self.state.namespace.name; ns.mkdir(mode=0o700)
        (ns / 'project.json').write_bytes(self._project_json_bytes())
        os.chmod(ns / 'project.json', 0o600)
        generated = ns / 'generated'; generated.mkdir(mode=0o700)
        forged = self._consistent_state_for_root(unrelated)
        before = tuple(sorted(p.name for p in generated.iterdir()))
        with self.assertRaises(ProjectStateError):
            validate_project_state(forged)
        with self.assertRaises(EffectiveInventoryOutputError):
            write_effective_build(self.projection, repo_root=self.root, project_state=forged)
        self.assertEqual(tuple(sorted(p.name for p in generated.iterdir())), before)
        self.assertFalse((generated / 'docker-constructor.build.effective.toml').exists())

    def test_missing_project_json_is_rejected_and_generated_unchanged(self):
        (self.state.namespace / 'project.json').unlink()
        before = self._generated_snapshot()
        with self.assertRaises(EffectiveInventoryOutputError):
            write_effective_build(self.projection, repo_root=self.root, project_state=self.state)
        self.assertEqual(self._generated_snapshot(), before)

    def test_symlinked_project_json_is_rejected_and_generated_unchanged(self):
        outside = self.root.parent / 'outside-project.json'; outside.write_text('{}\n')
        p = self.state.namespace / 'project.json'; p.unlink(); p.symlink_to(outside)
        before = self._generated_snapshot()
        with self.assertRaises(EffectiveInventoryOutputError):
            write_effective_build(self.projection, repo_root=self.root, project_state=self.state)
        self.assertEqual(outside.read_text(), '{}\n')
        self.assertEqual(self._generated_snapshot(), before)

    def test_non_regular_project_json_is_rejected_and_generated_unchanged(self):
        p = self.state.namespace / 'project.json'; p.unlink(); p.mkdir()
        before = self._generated_snapshot()
        with self.assertRaises(EffectiveInventoryOutputError):
            write_effective_build(self.projection, repo_root=self.root, project_state=self.state)
        self.assertEqual(self._generated_snapshot(), before)

    def test_permissive_project_json_is_rejected_and_generated_unchanged(self):
        p = self.state.namespace / 'project.json'; os.chmod(p, 0o644)
        self.addCleanup(os.chmod, p, 0o600)
        before = self._generated_snapshot()
        with self.assertRaises(EffectiveInventoryOutputError):
            write_effective_build(self.projection, repo_root=self.root, project_state=self.state)
        self.assertEqual(self._generated_snapshot(), before)
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o644)

    def test_foreign_owned_project_json_is_rejected_and_generated_unchanged(self):
        before = self._generated_snapshot()
        with mock.patch('os.fstat', side_effect=self._foreign_project_json_fstat()):
            with self.assertRaises(EffectiveInventoryOutputError):
                write_effective_build(self.projection, repo_root=self.root, project_state=self.state)
        self.assertEqual(self._generated_snapshot(), before)

    def test_wrong_schema_version_is_rejected_and_generated_unchanged(self):
        self._write_project_json(version=2)
        before = self._generated_snapshot()
        with self.assertRaises(EffectiveInventoryOutputError):
            write_effective_build(self.projection, repo_root=self.root, project_state=self.state)
        self.assertEqual(self._generated_snapshot(), before)

    def test_wrong_canonical_path_is_rejected_and_generated_unchanged(self):
        self._write_project_json(path=str(self.root.parent / 'elsewhere'))
        before = self._generated_snapshot()
        with self.assertRaises(EffectiveInventoryOutputError):
            write_effective_build(self.projection, repo_root=self.root, project_state=self.state)
        self.assertEqual(self._generated_snapshot(), before)

    def test_wrong_digest_is_rejected_and_generated_unchanged(self):
        self._write_project_json(digest='0' * 64)
        before = self._generated_snapshot()
        with self.assertRaises(EffectiveInventoryOutputError):
            write_effective_build(self.projection, repo_root=self.root, project_state=self.state)
        self.assertEqual(self._generated_snapshot(), before)

    def test_transaction_retains_explicit_cache_root_for_all_persistent_state(self):
        identity=self.identity(b'explicit-cache-root')
        with acquire_constructor_project_build_lock(self.root, cache_root=self.cache) as lock:
            publish_uncommitted_blob(identity, b'explicit-cache-root', constructor_project_root=self.root, lock=lock, verified_at=0, cache_root=self.cache)
            commit_build_set(self.root, {identity}, lock=lock, cache_root=self.cache)
        paths=prepare_build_cache(self.root, cache_root=self.cache)
        self.assertEqual(paths.namespace_root, self.state.namespace)
        self.assertTrue((paths.persistent_root/'build.lock').is_relative_to(self.state.namespace))
        self.assertTrue(any((paths.persistent_root/name).is_relative_to(self.state.build_artifacts_root) for name in _generation_files(paths)))
        self.assertTrue(build_blob_path(paths.blobs_root, identity).exists())

    def test_lock_lives_under_external_namespace_not_checkout(self):
        with acquire_constructor_project_build_lock(self.root, cache_root=self.cache) as lock:
            self.assertEqual(lock.namespace, self.state.namespace)
            self.assertEqual(lock.cache_root, self.cache)
            self.assertTrue((self.state.build_artifacts_root / 'build.lock').exists())
        self.assertFalse((self.root / '.docker-cache').exists())
        self.assertFalse((self.root / 'build.lock').exists())

    def test_lock_identity_is_the_canonical_resolved_project(self):
        link = Path(self.tmp.name) / 'project-link'
        link.symlink_to(self.root, target_is_directory=True)
        with acquire_constructor_project_build_lock(link, cache_root=self.cache) as lock:
            self.assertEqual(lock.constructor_project_root, self.root.resolve())
            # The canonical resolved path must satisfy the same lock.
            lock.assert_held_for(self.root, cache_root=self.cache)

    def test_lock_rejects_another_project(self):
        other = Path(self.tmp.name) / 'other'; other.mkdir()
        with acquire_constructor_project_build_lock(self.root, cache_root=self.cache) as lock:
            with self.assertRaises(BuildTransactionError):
                lock.assert_held_for(other, cache_root=self.cache)

    def test_lock_rejects_another_cache_root(self):
        other_cache = Path(self.tmp.name) / 'other-cache'; other_cache.mkdir(mode=0o700)
        with acquire_constructor_project_build_lock(self.root, cache_root=self.cache) as lock:
            with self.assertRaises(BuildTransactionError):
                lock.assert_held_for(self.root, cache_root=other_cache)

    def transaction_paths(self):
        # Transaction APIs currently resolve the configured default root; use
        # the matching ProjectState rather than reconstructing checkout paths.
        state=resolve_project_state(self.root)
        self.addCleanup(shutil.rmtree, state.namespace, ignore_errors=True)
        return state, prepare_build_cache(self.root)

    def test_markers_manifests_locks_and_snapshots_are_project_state_children(self):
        state, paths=self.transaction_paths(); one=self.identity(b'one')
        with acquire_constructor_project_build_lock(self.root) as lock:
            published=publish_uncommitted_blob(one,b'one',constructor_project_root=self.root,lock=lock,verified_at=0)
            marker=paths.markers_root/f'sha256:{one.hex_digest()}.json'
            snapshot=paths.generated_root/'interrupted'; snapshot.mkdir(); (snapshot/'partial').write_text('partial')
            self.assertTrue(published.is_relative_to(state.build_artifacts_root))
            self.assertTrue(marker.is_relative_to(state.build_artifacts_root)); self.assertTrue(marker.is_file())
            self.assertTrue((paths.persistent_root/'build.lock').is_relative_to(state.build_artifacts_root))
            recover_abandoned_snapshots(self.root,lock=lock)
            self.assertFalse(snapshot.exists())
            commit_build_set(self.root,{one},lock=lock)
        self.assertTrue(any((paths.persistent_root/name).is_relative_to(state.build_artifacts_root) for name in _generation_files(paths)))
        self.assertEqual(_newest_generation_blobs(paths), [f'sha256:{one.hex_digest()}'])
        self.assertFalse((self.root/'.docker-cache').exists()); self.assertFalse((self.root/'.docker-generated').exists())

    def test_retention_boundary_preserves_committed_blob_and_prior_state(self):
        state, paths=self.transaction_paths(); committed=self.identity(b'committed'); stale=self.identity(b'stale')
        with acquire_constructor_project_build_lock(self.root) as lock:
            publish_uncommitted_blob(committed,b'committed',constructor_project_root=self.root,lock=lock,verified_at=0)
            commit_build_set(self.root,{committed},lock=lock)
            publish_uncommitted_blob(stale,b'stale',constructor_project_root=self.root,lock=lock,verified_at=0)
            # Exactly TTL seconds old is retained; only older-than-TTL expires.
            maintain_uncommitted_blobs(self.root,lock=lock,now=UNCOMMITTED_TTL_SECONDS)
            self.assertTrue(build_blob_path(paths.blobs_root,stale).exists())
            self.assertTrue((paths.markers_root/f'sha256:{stale.hex_digest()}.json').exists())
            maintain_uncommitted_blobs(self.root,lock=lock,now=UNCOMMITTED_TTL_SECONDS+1)
        self.assertTrue(build_blob_path(paths.blobs_root,committed).exists())
        self.assertFalse(build_blob_path(paths.blobs_root,stale).exists())
        self.assertFalse((paths.markers_root/f'sha256:{stale.hex_digest()}.json').exists())
        self.assertEqual(_newest_generation_blobs(paths), [f'sha256:{committed.hex_digest()}'])

    def test_interrupted_blob_publication_retains_marker_for_cleanup(self):
        state, paths=self.transaction_paths(); identity=self.identity(b'interrupted')
        with acquire_constructor_project_build_lock(self.root) as lock:
            with mock.patch('docker.versioning.build_cache.publish_verified_blob', side_effect=OSError('interrupted')):
                with self.assertRaises(OSError): publish_uncommitted_blob(identity,b'interrupted',constructor_project_root=self.root,lock=lock,verified_at=0)
            marker=paths.markers_root/f'sha256:{identity.hex_digest()}.json'
            self.assertTrue(marker.is_file())
            maintain_uncommitted_blobs(self.root,lock=lock,now=0)
        self.assertFalse(marker.exists())
        self.assertFalse(build_blob_path(paths.blobs_root,identity).exists())
        self.assertTrue(paths.markers_root.is_relative_to(state.build_artifacts_root))

    def test_blob_and_temporary_publication_are_confined_and_cache_hit_is_immutable(self):
        paths=prepare_build_cache(self.root,cache_root=self.cache); identity=self.identity(b'one')
        first=publish_verified_blob(identity,b'one',constructor_project_root=self.root,cache_root=self.cache)
        second=publish_verified_blob(identity,b'one',constructor_project_root=self.root,cache_root=self.cache)
        self.assertEqual(first,second); self.assertTrue(first.is_relative_to(self.state.build_artifacts_root))
        self.assertTrue(paths.tmp_root.is_relative_to(self.state.build_artifacts_root))
        self.assertEqual(list(paths.tmp_root.iterdir()), [])

    def test_digest_mismatch_preserves_prior_blob_and_leaves_no_temporary_state(self):
        paths=prepare_build_cache(self.root,cache_root=self.cache); identity=self.identity(b'prior')
        prior=publish_verified_blob(identity,b'prior',constructor_project_root=self.root,cache_root=self.cache)
        with self.assertRaises(BuildCacheError): publish_verified_blob(identity,b'wrong',constructor_project_root=self.root,cache_root=self.cache)
        self.assertEqual(prior.read_bytes(),b'prior'); self.assertEqual(list(paths.tmp_root.iterdir()), [])
        self.assertFalse((self.root/'.docker-cache').exists()); self.assertFalse((self.root/'.docker-generated').exists())


class CustomCacheRootLifecycle(unittest.TestCase):
    """Lifecycle confinement when ``[cache].dir`` selects a dedicated root."""

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        base=Path(self.tmp.name)
        self.repo=base/'constructor'; self.repo.mkdir()
        self.custom_dir=base/'custom-cache-dir'
        self.home=base/'home'; self.home.mkdir()
        self.xdg=base/'xdg'
        self.env=mock.patch.dict(os.environ, {'XDG_CACHE_HOME': str(self.xdg), 'HOME': str(self.home)})
        self.env.start(); self.addCleanup(self.env.stop)
        # Mirror constructor_cli: resolve [cache].dir through the effective-root
        # selector, then harden it exactly as the CLI does before use.
        self.cache_root=resolve_effective_root(str(self.custom_dir), xdg_cache_home=str(self.xdg), home=self.home)
        self.cache_root=prepare_resolved_root(self.cache_root)
        self.default_root=resolve_default_root(str(self.xdg), home=self.home)

    def identity(self,data): return DigestIdentity.from_hex('sha256',hashlib.sha256(data).hexdigest())

    def test_complete_transaction_state_confined_to_custom_cache_dir(self):
        committed=self.identity(b'custom-lifecycle-committed'); uncommitted=self.identity(b'custom-lifecycle-uncommitted')
        with acquire_constructor_project_build_lock(self.repo, cache_root=self.cache_root) as lock:
            publish_uncommitted_blob(committed, b'custom-lifecycle-committed', constructor_project_root=self.repo, lock=lock, verified_at=0, cache_root=self.cache_root)
            publish_uncommitted_blob(uncommitted, b'custom-lifecycle-uncommitted', constructor_project_root=self.repo, lock=lock, verified_at=0, cache_root=self.cache_root)
            commit_build_set(self.repo, {committed}, lock=lock, cache_root=self.cache_root)
        state=resolve_project_state(self.repo, cache_root=self.cache_root)
        paths=prepare_build_cache(self.repo, cache_root=self.cache_root)
        namespace=state.namespace
        self.assertTrue(namespace.is_relative_to(self.cache_root))
        lock=paths.persistent_root/'build.lock'
        self.assertTrue(lock.exists()); self.assertTrue(lock.is_relative_to(namespace))
        blob=build_blob_path(paths.blobs_root, committed)
        self.assertTrue(blob.exists()); self.assertTrue(blob.is_relative_to(namespace))
        manifest=next(paths.persistent_root/name for name in _generation_files(paths))
        self.assertTrue(manifest.exists()); self.assertTrue(manifest.is_relative_to(namespace))
        marker=paths.markers_root/f'sha256:{uncommitted.hex_digest()}.json'
        self.assertTrue(marker.exists()); self.assertTrue(marker.is_relative_to(namespace))
        for child in (paths.blobs_root, paths.tmp_root, paths.generated_root, paths.markers_root):
            self.assertTrue(child.is_relative_to(namespace))
        self.assertEqual(list(paths.tmp_root.iterdir()), [])
        self.assertFalse(self.default_root.exists())
        self.assertFalse((self.repo/'.docker-cache').exists()); self.assertFalse((self.repo/'.docker-generated').exists())

    def test_abandoned_snapshot_recovery_with_custom_cache_dir(self):
        state=resolve_project_state(self.repo, cache_root=self.cache_root)
        snapshot=state.transactions_root/'abandoned-snapshot'; snapshot.mkdir(); (snapshot/'partial').write_text('partial')
        self.assertTrue(snapshot.is_relative_to(self.cache_root))
        with acquire_constructor_project_build_lock(self.repo, cache_root=self.cache_root) as lock:
            recover_abandoned_snapshots(self.repo, lock=lock, cache_root=self.cache_root)
        self.assertFalse(snapshot.exists())
        self.assertFalse(self.default_root.exists())

    def test_temporary_publication_file_stays_beneath_custom_cache_dir(self):
        state=resolve_project_state(self.repo, cache_root=self.cache_root)
        paths=prepare_build_cache(self.repo, cache_root=self.cache_root)
        identity=self.identity(b'custom-temp-file'); staged=[]
        def interrupting_replace(src, dst, *args, **kwargs):
            staged.extend(paths.namespace_root.rglob('.publish-*'))
            raise OSError('interrupted before publish')
        with mock.patch('docker.versioning.build_cache.os.replace', side_effect=interrupting_replace):
            with self.assertRaises(OSError):
                publish_verified_blob(identity, b'custom-temp-file', constructor_project_root=self.repo, cache_root=self.cache_root)
        self.assertTrue(staged)
        for path in staged:
            self.assertTrue(path.is_relative_to(paths.tmp_root))
            self.assertTrue(path.is_relative_to(state.namespace))
        self.assertEqual(list(paths.tmp_root.iterdir()), [])
        self.assertFalse(self.default_root.exists())

    def test_failed_publication_retains_marker_with_custom_cache_dir(self):
        state=resolve_project_state(self.repo, cache_root=self.cache_root)
        paths=prepare_build_cache(self.repo, cache_root=self.cache_root)
        identity=self.identity(b'custom-interrupted')
        with acquire_constructor_project_build_lock(self.repo, cache_root=self.cache_root) as lock:
            with mock.patch('docker.versioning.build_cache.publish_verified_blob', side_effect=OSError('interrupted')):
                with self.assertRaises(OSError):
                    publish_uncommitted_blob(identity, b'custom-interrupted', constructor_project_root=self.repo, lock=lock, verified_at=0, cache_root=self.cache_root)
            marker=paths.markers_root/f'sha256:{identity.hex_digest()}.json'
            self.assertTrue(marker.exists()); self.assertTrue(marker.is_relative_to(state.build_artifacts_root))
            maintain_uncommitted_blobs(self.repo, lock=lock, now=0, cache_root=self.cache_root)
        self.assertFalse(marker.exists())
        self.assertFalse(build_blob_path(paths.blobs_root, identity).exists())
        self.assertEqual(list(paths.tmp_root.iterdir()), [])
        self.assertFalse(self.default_root.exists())

    def test_no_operation_falls_back_to_the_default_cache_root(self):
        first=self.identity(b'no-fallback-direct'); second=self.identity(b'no-fallback-transaction')
        forbid=mock.Mock(side_effect=AssertionError('operation fell back to the default cache root'))
        with mock.patch('docker.versioning.build_cache.resolve_default_root', forbid), \
             mock.patch('docker.versioning.project_state.prepare_default_root', forbid):
            resolve_project_state(self.repo, cache_root=self.cache_root)
            prepare_build_cache(self.repo, cache_root=self.cache_root)
            publish_verified_blob(first, b'no-fallback-direct', constructor_project_root=self.repo, cache_root=self.cache_root)
            with acquire_constructor_project_build_lock(self.repo, cache_root=self.cache_root) as lock:
                publish_uncommitted_blob(second, b'no-fallback-transaction', constructor_project_root=self.repo, lock=lock, verified_at=0, cache_root=self.cache_root)
                recover_abandoned_snapshots(self.repo, lock=lock, cache_root=self.cache_root)
                maintain_uncommitted_blobs(self.repo, lock=lock, now=0, cache_root=self.cache_root)
                commit_build_set(self.repo, {second}, lock=lock, cache_root=self.cache_root)
        forbid.assert_not_called()
        self.assertFalse(self.default_root.exists())


class DescriptorRelativePublicationRaces(unittest.TestCase):
    """Race coverage for descriptor-relative JSON control-file publication."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'constructor'; self.root.mkdir()
        self.cache = self.base / 'cache'; self.cache.mkdir(mode=0o700)
        self.state = resolve_project_state(self.root, cache_root=self.cache)
        self.paths = prepare_build_cache(self.root, cache_root=self.cache)

    def identity(self, data):
        return DigestIdentity.from_hex('sha256', hashlib.sha256(data).hexdigest())

    def test_manifest_replaced_by_symlink_is_rejected_before_reading(self):
        identity = self.identity(b'live')
        with acquire_constructor_project_build_lock(self.root, cache_root=self.cache) as lock:
            publish_uncommitted_blob(identity, b'live', constructor_project_root=self.root, lock=lock, verified_at=0, cache_root=self.cache)
            commit_build_set(self.root, {identity}, lock=lock, cache_root=self.cache)
        generation = self.paths.persistent_root / _generation_files(self.paths)[0]
        outside = self.base / 'outside-manifest'
        outside.write_text('must-not-be-read')
        generation.unlink(); generation.symlink_to(outside)
        with acquire_constructor_project_build_lock(self.root, cache_root=self.cache) as lock:
            with self.assertRaises(BuildCacheError):
                commit_build_set(self.root, {identity}, lock=lock, cache_root=self.cache)
        self.assertEqual(outside.read_text(), 'must-not-be-read')

    def test_marker_destination_replaced_by_symlink_is_rejected_before_overwrite(self):
        identity = self.identity(b'payload')
        with acquire_constructor_project_build_lock(self.root, cache_root=self.cache) as lock:
            mark_uncommitted_blob(identity, self.root, lock=lock, verified_at=0, cache_root=self.cache)
        marker = self.paths.markers_root / f'sha256:{identity.hex_digest()}.json'
        outside = self.base / 'outside-marker'
        outside.write_text('target-bytes')
        marker.unlink(); marker.symlink_to(outside)
        with acquire_constructor_project_build_lock(self.root, cache_root=self.cache) as lock:
            with self.assertRaises(BuildTransactionError):
                mark_uncommitted_blob(identity, self.root, lock=lock, verified_at=1, cache_root=self.cache)
        self.assertEqual(outside.read_text(), 'target-bytes')

    def test_manifest_parent_replaced_by_symlink_is_rejected_before_publication(self):
        build_artifacts = self.paths.persistent_root
        evil = self.base / 'evil'; evil.mkdir(); (evil / 'sentinel').write_text('evil')
        build_artifacts.rename(self.base / 'build-artifacts-old')
        build_artifacts.symlink_to(evil, target_is_directory=True)
        with self.assertRaises(BuildCacheError):
            prepare_build_cache(self.root, cache_root=self.cache)
        self.assertEqual((evil / 'sentinel').read_text(), 'evil')

    def test_interrupted_generation_publication_preserves_prior_and_cleans_temp(self):
        first = self.identity(b'first'); second = self.identity(b'second')
        with acquire_constructor_project_build_lock(self.root, cache_root=self.cache) as lock:
            publish_uncommitted_blob(first, b'first', constructor_project_root=self.root, lock=lock, verified_at=0, cache_root=self.cache)
            commit_build_set(self.root, {first}, lock=lock, cache_root=self.cache)
            publish_uncommitted_blob(second, b'second', constructor_project_root=self.root, lock=lock, verified_at=0, cache_root=self.cache)
            with mock.patch('docker.transactions.posix.PosixFileOps.linkat', side_effect=OSError('interrupted generation publication')):
                with self.assertRaises(BuildCacheError):
                    commit_build_set(self.root, {first, second}, lock=lock, cache_root=self.cache)
        self.assertEqual(_newest_generation_blobs(self.paths), [f'sha256:{first.hex_digest()}'])
        self.assertEqual([p.name for p in self.paths.persistent_root.iterdir() if p.name.startswith('.transaction-')], [])
        self.assertTrue(build_blob_path(self.paths.blobs_root, second).exists())
        self.assertTrue((self.paths.markers_root / f'sha256:{second.hex_digest()}.json').exists())

    def test_hard_linked_generation_is_rejected_without_deletion(self):
        identity = self.identity(b'live')
        with acquire_constructor_project_build_lock(self.root, cache_root=self.cache) as lock:
            publish_uncommitted_blob(identity, b'live', constructor_project_root=self.root, lock=lock, verified_at=0, cache_root=self.cache)
            commit_build_set(self.root, {identity}, lock=lock, cache_root=self.cache)
        generation = self.paths.persistent_root / _generation_files(self.paths)[0]
        original = generation.read_text()
        os.link(generation, self.base / 'generation-hardlink')
        with acquire_constructor_project_build_lock(self.root, cache_root=self.cache) as lock:
            with self.assertRaises(BuildCacheError):
                commit_build_set(self.root, {identity}, lock=lock, cache_root=self.cache)
        self.assertEqual(generation.read_text(), original)
        self.assertTrue(build_blob_path(self.paths.blobs_root, identity).exists())

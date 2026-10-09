# Binding Implementation Contract

Every checkbox below is an atomic obligation. A task is complete only when its named deliverable exists and its stated verification succeeds. Production behavior, public APIs, import boundaries, persistent paths, modes, identities, error mappings, lock policy, publication ordering, durability, and domain authority not explicitly changed by a task remain unchanged. No production consumer-owned descriptor close is out of scope: every retained direct close must be a low-level POSIX primitive, a gated shared-owner internal release, or a verified ownership handoff.

The implementation phases form this dependency DAG:

```text
Phase 1: npm single-descriptor lifecycle ───────────┐
Phase 2: runtime/build-context lifecycle ───────────┤
Phase 3: versioning single-descriptor lifecycle ────┤
                    │                               │
                    ▼                               │
Phase 4: build-cache aggregate owner ───────────────┤
                    │                               │
                    ▼                               │
Phase 5: build-cache directory handoff ─────────────┤
                                                    ▼
Phase 6: residual production close migration ───────▶ Phase 7: convergence
```

Within every phase work proceeds strictly `RED → GREEN → INTROSPECT → VALIDATE`. RED tasks change tests only and demonstrate an unmet contract unless explicitly identified as passing characterization. GREEN tasks make the minimum production change required by that phase. INTROSPECT tasks bind static API, dependency, and ownership properties. VALIDATE tasks execute the phase gate. A phase may consume deliverables only from its declared predecessors.

## 1. npm Single-Descriptor Lifecycle

**Depends on:** none.

**Deliverables:** npm lockfile-input writing and tree-file hashing own each opened regular-file descriptor through `OwnedDescriptor`; active failures retain ordinary close failures secondarily; successful release is terminal; lockfile bytes, tree evidence, domain errors, and import boundaries remain stable.

### RED

- [x] 1.1 Add a lockfile-writing test injecting simultaneous `os.write` and ordinary close failures; require the exact original write `OSError` object, type, message, and cause state to remain primary without conversion to `LockedNpmError`, retain the close failure secondarily, and issue one close attempt, and verify the new case fails against the bare `finally` close.
- [x] 1.2 Add lockfile-writing tests for a sole ordinary close failure, close interruption, and a repeated cleanup attempt; require the existing raw close-exception behavior without `LockedNpmError` conversion, unchanged interruption identity, and no second close operation, and verify the terminal-lifecycle expectation fails before migration.
- [x] 1.3 Add passing tree-file characterization proving `_hash_file_entry()` maps file-entry open `OSError` to `LockedNpmError("tree_type_mismatch", ...)` with the original error as cause; separately inject ordinary close failure after stat failure and unsafe-type rejection, require the exact stat `OSError` to remain primary without conversion and the unsafe-type `LockedNpmError` to remain exact, retain close secondarily, issue one close attempt, and verify those new precedence cases fail before migration.
- [x] 1.4 Add tree-file tests injecting ordinary close failure after read failure and after hashing failure; require the exact read `OSError` or hashing exception to propagate unchanged without `LockedNpmError` conversion, retain close secondarily, and issue one close attempt, and verify the new precedence cases fail before migration.
- [x] 1.5 Add tree-file tests for a sole close failure, close interruption, and repeated cleanup; require the exact close `OSError` to propagate unchanged without `LockedNpmError` conversion, preserve interruption identity, make ownership terminal, and issue no repeated operation, and verify the terminal-lifecycle expectation fails before migration.

### GREEN

- [x] 1.6 Refactor `_write_lockfile()` to validate its label/backend before open, transfer the fd immediately to `OwnedDescriptor(PosixDescriptorOps(), ...)`, perform writes through the live owner, and release through the owner under shared precedence; verify tasks 1.1 and 1.2 pass without moving npm policy into the foundation.
- [x] 1.7 Refactor `_hash_file_entry()` to transfer the opened fd immediately to `OwnedDescriptor` while preserving the existing file-open `OSError` to `LockedNpmError("tree_type_mismatch", ...)` mapping and original cause, retaining unsafe-type `LockedNpmError`, propagating stat/read/hash/close exceptions raw, and releasing through the owner; verify tasks 1.3–1.5 pass with unchanged manifest output.

### INTROSPECT

- [x] 1.8 Add AST assertions that the two migrated functions contain no direct `os.close`, injected raw close, or bespoke release-state implementation, import only explicit `docker.filesystem` submodules, and leave recursion, hashing, lockfile semantics, and domain errors in npm modules; verify all assertions pass.

### VALIDATE

- [x] 1.9 Run the focused npm execution, tree, storage, manifest, serialization, publication, validation, and locked-environment suites; require exact lockfile bytes, canonical manifest bytes/digests, no-follow behavior, domain reasons/messages, and normal import behavior to pass before Phase 6.

## 2. Runtime Artifact and Build-Context Lifecycle

**Depends on:** none.

**Deliverables:** mounted runtime-artifact verification and private build-context writing use `OwnedDescriptor`; the image packages `docker/filesystem/` at `/usr/local/lib/pi-cli/docker/filesystem/`; runtime imports succeed from the isolated image layout without the repository on `sys.path`; validation, integrity, confinement, bytes, identities, context contents, and diagnostics do not drift except for the explicitly authorized sole-close `InstallError`/CLI mapping.

### RED

- [x] 2.1 Add runtime-installer tests that simulate `PermissionError` from the first `O_NOATIME` open for a foreign-owned blob and require fallback to the same no-follow read-only open without `O_NOATIME`; for fallback stat failure, non-regular type, or group/world-writable mode combined with ordinary close failure, require the existing `InstallError` to remain primary with close secondary and one close attempt, and verify masking cases fail before migration without adding any ownership rejection.
- [x] 2.2 Add runtime-installer tests injecting ordinary close failure after read, empty-artifact, and integrity failures; require each exact active failure to remain primary with close secondary and one close attempt, and verify masking cases fail before migration.
- [x] 2.3 Add runtime-installer tests proving `open_verified()` currently propagates a sole close `OSError` past callers that catch only `InstallError`, then define the new boundary contract: map that ordinary sole close failure to `InstallError(f"cannot close artifact at {_path}: {exc}")` with the original `OSError` as direct cause, preserve interruption identity and terminal ownership, issue no repeated close, execute no package installation, and make the CLI/result path report the normal controlled installation failure rather than an uncaught exception; verify the mapping, terminal, and caller-impact assertions are RED before migration.
- [x] 2.4 Add private build-context tests injecting ordinary close failure after write failure and defensive short-write failure; require the existing confinement failure to remain primary, close to remain secondary, and one close attempt, and verify masking cases fail before migration.
- [x] 2.5 Add private build-context tests for successful writing followed by sole close failure, close interruption, and repeated cleanup; require established error mapping, unchanged interruption identity, terminal ownership, and unchanged output bytes on success, and verify terminal ownership fails before migration.
- [x] 2.6 Add an isolated image-layout import test that stages only the Python files copied by `Dockerfile` under an equivalent `/usr/local/lib/pi-cli` root, launches a subprocess from outside the repository with `PYTHONPATH` scrubbed and the repository absent from `sys.path`, and imports `docker.runtime_installer` plus its `docker.filesystem` dependencies; verify the test fails before the filesystem package is copied.

### GREEN

- [x] 2.7 Refactor mounted runtime-artifact fd ownership to immediate `OwnedDescriptor` transfer while retaining no-follow open and stat/read/hash/integrity policy, and add the task 2.3 sole-close `OSError` to `InstallError` mapping at the `open_verified()` domain boundary without wrapping active-primary or process-control failures; verify tasks 2.1–2.3 pass.
- [x] 2.8 Refactor `_write_private()` to immediate `OwnedDescriptor` transfer while retaining exclusive creation, write loop, short-write detection, mode, path, and `ConfinementError` policy locally; verify tasks 2.4 and 2.5 pass.
- [x] 2.9 Add `COPY docker/filesystem/ /usr/local/lib/pi-cli/docker/filesystem/` to `Dockerfile` beside the existing versioning/runtime-installer copies, preserving root ownership and read/execute permissions; verify task 2.6 passes without adding the repository to the subprocess import path.

### INTROSPECT

- [x] 2.10 Add AST assertions that migrated runtime/build-context functions contain no direct close or local release-state machine, use explicit lightweight foundation imports, and add no runtime-artifact, integrity, path, or confinement authority to `docker.filesystem`; add a Dockerfile layout assertion for the exact `docker/filesystem/` source and `/usr/local/lib/pi-cli/docker/filesystem/` destination; verify all assertions pass.

### VALIDATE

- [x] 2.11 Run the isolated image-layout import test followed by focused runtime-installer, runtime-projection, build-context confinement, materialization, snapshot-consumer, Dockerfile-layout, and Docker build-generation suites; require artifact bytes, integrity, identity, paths, modes, packaged imports, context contents, and public diagnostics to pass before Phase 6.

## 3. Versioning Single-Descriptor Lifecycle

**Depends on:** none.

**Deliverables:** snapshot destination copy, artifact-cache temporary creation, and validated project-state ownership use `OwnedDescriptor` on success and failure; public construction, descriptor access, unlink policy, project-state behavior, and domain mappings remain compatible.

### RED

- [ ] 3.1 Add snapshot destination tests for operation failure plus close failure, successful copy plus sole close failure, close interruption, and repeated cleanup; require operation-primary precedence, terminal ownership, existing unlink policy, and one close attempt, and verify the owner contract fails before migration.
- [ ] 3.2 Add artifact-cache temporary-creation tests for sole child close failure, simultaneous parent close failure, interruption, and repeated cleanup; require the child failure to remain authoritative, parent cleanup to remain independent, terminal child ownership, and one attempt per fd, and verify the owner contract fails before migration.
- [ ] 3.3 Add `ValidatedProjectState` direct-close and context-exit tests for success, sole close failure, active primary plus close failure, interruption, and repeated close; require existing public behavior, primary precedence, terminal ownership, and one close attempt, and verify shared-owner delegation fails before migration.

### GREEN

- [ ] 3.4 Refactor snapshot destination copy to transfer the created fd immediately to `OwnedDescriptor`, use the owner in active-primary close-plus-unlink accumulation, and use terminal owner release on success; verify task 3.1 passes with unchanged digest, mode, fsync, and unlink semantics.
- [ ] 3.5 Refactor artifact-cache temporary creation to transfer the child fd immediately to `OwnedDescriptor` and retain the existing parent accumulator and domain policy; verify task 3.2 passes with unchanged path, mode, and creation behavior.
- [ ] 3.6 Refactor `ValidatedProjectState` to delegate namespace fd lifecycle to a private `OwnedDescriptor` while preserving its constructor, public attributes/properties, context-manager behavior, and domain mapping; verify task 3.3 passes.

### INTROSPECT

- [ ] 3.7 Add `inspect.signature` and AST assertions proving public snapshot/artifact/project-state interfaces are unchanged, migrated single releases contain no direct raw close or duplicate state machine, and explicit foundation imports introduce no aggregate or reverse dependency; verify all assertions pass.

### VALIDATE

- [ ] 3.8 Run snapshot, artifact-cache, project-state, rendering, effective-state, build-cleanup, and build-generation integration suites; require paths, bytes, modes, identities, publication, cleanup, exception graphs, and compatibility imports to pass before Phases 4 or 6.

## 4. Build-Cache Aggregate Owner

**Depends on:** Phase 3.

**Deliverables:** `docker.versioning.build_cache.BuildCacheState`, as returned by `open_build_cache_state()`, privately owns every retained fd through `OwnedDescriptor`; aggregate release becomes terminal before its first syscall; one `CleanupFailures` attempts all five independent owner releases; repeat close is a no-op; its existing constructor, `paths`, `persistent_fd`, `blobs_fd`, `tmp_fd`, `markers_fd`, `transactions_fd`, signatures, and return type remain compatible.

### RED

- [ ] 4.1 Obtain the actual `BuildCacheState` from `open_build_cache_state()` and add `BuildCacheState.close()` tests proving `persistent_fd`, `blobs_fd`, `tmp_fd`, `markers_fd`, and `transactions_fd` each receive one close attempt when the first, middle, or last release raises an ordinary failure; verify current stop-on-first-failure behavior fails.
- [ ] 4.2 Add tests against the `BuildCacheState` returned by `open_build_cache_state()` with several ordinary close failures; require all five attempts, first-failure authority, later failures as ordered secondary diagnostics, and no repeated operation, and verify current behavior fails.
- [ ] 4.3 Add tests against the returned `BuildCacheState` for an unexpected close defect and process-control interruption at each of its five release positions; require every independent release attempt and shared precedence without wrapping the authoritative object, and verify current behavior fails.
- [ ] 4.4 Add repeated direct/context cleanup tests for the returned `BuildCacheState`, requiring aggregate terminal state before the first close syscall and no operation after any success or failure outcome; verify current nonterminal lifecycle fails.

### GREEN

- [ ] 4.5 Refactor `BuildCacheState` initialization to create one private `OwnedDescriptor` for each of `persistent_fd`, `blobs_fd`, `tmp_fd`, `markers_fd`, and `transactions_fd` while preserving the existing constructor, `paths`, integer field values, and the `BuildCacheState` instance returned by `open_build_cache_state()`; verify construction and compatibility tests pass before changing release behavior.
- [ ] 4.6 Refactor `BuildCacheState.close()` to detach or terminally claim the complete five-owner set before release, run every `owner.close` through one `CleanupFailures(None)`, and make repeat close a no-op; verify tasks 4.1–4.4 pass.

### INTROSPECT

- [ ] 4.7 Add API/AST assertions proving `BuildCacheState` remains the class returned by `open_build_cache_state()`, its constructor/signatures and `paths`, `persistent_fd`, `blobs_fd`, `tmp_fd`, `markers_fd`, and `transactions_fd` fields are unchanged, exactly one shared owner state machine supplies per-fd terminal behavior, one aggregate accumulator supplies multi-release precedence, and no raw multi-close loop remains; verify all assertions pass.

### VALIDATE

- [ ] 4.8 Run direct `open_build_cache_state()`/`BuildCacheState` lifecycle and compatibility tests followed by build-cache publication, locking, maintenance, generation, retention, project-state, and consumer integration suites; require cache paths, identities, lock scope, modes, publication order, durability, and domain errors to pass before Phase 5 or 6.

## 5. Build-Cache Directory Handoff

**Depends on:** Phases 3 and 4.

**Deliverables:** `_open_relative_dir()` and `_bootstrap_constructor_project_lock_parent()` in `docker/versioning/build_cache.py` have complete executable fd ledgers; a child is tracked before fallible parent release; a releasing parent is terminal before close; failed parent close is not retried; opened children do not leak; direct close remains only as the principal verified handoff operation.

### RED

- [ ] 5.1 Add `_open_relative_dir()` fd-ledger tests for successful traversal and missing-without-create; account for every open, transfer, return, and close, and verify the current implementation lacks the required explicit terminal ownership evidence.
- [ ] 5.2 Add `_open_relative_dir()` tests for open, stat, mkdir, fchmod, and validation failures; require every still-owned fd to receive one release attempt under existing `BuildCacheError` mapping, and verify any unmet ledger case fails before migration.
- [ ] 5.3 Add `_open_relative_dir()` parent-close failure tests proving the terminal parent is not retried, the already-open child receives one release attempt, no child is returned, and close interruption remains authoritative; verify the ownership contract fails before migration where applicable.
- [ ] 5.4 Add tests targeting `docker.versioning.build_cache._bootstrap_constructor_project_lock_parent()` for unsafe child validation plus ordinary close failure; require the exact `BuildTransactionError` rejection to remain primary, close to remain secondary, and one child release attempt, and verify current masking behavior fails.
- [ ] 5.5 Add parent-close failure tests targeting `docker.versioning.build_cache._bootstrap_constructor_project_lock_parent()` proving the parent is terminal before close, the opened child is tracked and released once, the parent is not retried, independent namespace cleanup still runs, and interruption remains authoritative; verify current leak/retry behavior fails.
- [ ] 5.6 Add passing characterization tests against `_open_relative_dir()` and `_bootstrap_constructor_project_lock_parent()` in `docker/versioning/build_cache.py`, pinning validation order, owner/mode rules, missing-component creation, labels, path derivation, and public error text before production edits.

### GREEN

- [ ] 5.7 Compare the characterized `_open_relative_dir()` contract with `DirectoryDescriptor` factories/child methods in an executable decision test; require direct capability use where all semantics match and an `OwnedDescriptor` ledger where any pre-transfer or policy contract differs.
- [ ] 5.8 Refactor `_open_relative_dir()` according to task 5.7 so each child is capability- or owner-tracked before parent release, each parent is terminal before handoff close, and build-cache path/missing/mode/error policy remains local; verify tasks 5.1–5.3 pass.
- [ ] 5.9 Refactor `docker/versioning/build_cache.py::_bootstrap_constructor_project_lock_parent()` so validation rejection remains primary, child ownership is established before parent release, and namespace/parent/child cleanup follows the shared lifecycle without retry; verify tasks 5.4 and 5.5 pass.

### INTROSPECT

- [ ] 5.10 Add AST and fd-ledger assertions proving migrated handoffs contain no second generic secure walker or bespoke reusable release state, every remaining direct close is a classified principal handoff operation, and build-cache recursion, path derivation, lock policy, and domain errors remain outside `docker.filesystem`; verify all assertions pass.

### VALIDATE

- [ ] 5.11 Run the build-cache tests that directly exercise `docker/versioning/build_cache.py::_bootstrap_constructor_project_lock_parent()`, followed by build-lock, build-cache, artifact-materialization, project-state, cleanup, orchestration, and generation suites; require namespace identity, persistent layout, modes, locking, durability, publication, and public failure behavior to pass before Phase 6.

## 6. Residual Production Close Migration

**Depends on:** Phases 1, 2, 3, 4, and 5.

**Deliverables:** every explicit or implicit production descriptor release not migrated by Phases 1–5—including `os.fdopen(..., closefd=True)` and `tempfile.mkstemp()` ownership—has passing characterization for unaffected API/domain semantics and separate RED assertions for the required lifecycle before refactoring; transaction, npm-publication, artifact-cache, build-cache, project-state, snapshot, materialization, cleanup, storage, rendering, and persistent-cache consumers delegate release to shared ownership/cleanup; operation-primary precedence, terminal-before-close, and at-most-once release are required outcomes rather than preserved baseline defects.

### RED

- [ ] 6.1 For `docker/npm_environment/publication.py::{_durable_write,_fsync_dir,_fsync_tree,_atomic_publish}`, add passing characterization for bytes, publication visibility, fsync order, and exact domain mapping; separately require operation-primary precedence, terminal-before-close, and at-most-once release, and verify the terminal/repeat assertions and every currently masking precedence case are RED before migration.
- [ ] 6.2 For `docker/transactions/capabilities.py::{_adopt,open_regular,DirectoryCapability.close,FileCapability.close}`, add passing characterization for capability APIs, labels, typed errors, adoption, and interruption; prove the existing capability `close()` methods already pass terminal-before-close and at-most-once as shared-owner internals, separately require all three lifecycle properties for `_adopt`/`open_regular` cleanup, and verify their unowned-release assertions are RED before migration.
- [ ] 6.3 For `docker/transactions/locking.py::{_verify_unchanged,_release_descriptor}`, add passing characterization for mismatch errors, stage labels, lock semantics, and interruption; separately require operation-primary precedence, terminal-before-close, and at-most-once probe/release behavior, and verify terminal/retry assertions and any masking branch are RED before migration.
- [ ] 6.4 For `docker/transactions/regular.py::{durable_unlink,_validate_destination,_close_fd,_discard.close_descriptor}`, add passing characterization for validation, durability, typed errors, and public transaction APIs, and explicitly prove `_close_fd()` plus `_discard()`'s nested `close_descriptor()` already clear `state.fd` before close and suppress retries after success or failure; separately require those helpers to delegate release to the shared owner without weakening their passing terminal/at-most-once guarantees, require operation-primary precedence for all four sites, and verify missing shared-owner delegation plus any masking precedence branch—not the already-correct terminal/retry assertions—are RED before migration.
- [ ] 6.5 For artifact-cache readonly/traversal functions `inspect_verified_blob_readonly`, `_ensure_dir_private`, `_open_directory_chain`, `_remove_tree_at`, `stat_blob`, `read_bytes`, `inspect_and_digest`, `release_directory`, and `release_capability`, add passing characterization for validation, paths, return ownership, and public results; separately require operation-primary precedence, child tracking before handoff, terminal-before-close, and at-most-once release at every close site, and verify each unowned or unsafe-handoff assertion is RED before migration.
- [ ] 6.6 For artifact-cache mutation functions `ensure_secure_dir`, `create_temp`, `append_temp`, `finalize_temp`, `cleanup_temp`, `quarantine_or_remove`, `get_permissions`, `set_permissions`, `atomic_publish`, and `mkdtemp`, add passing characterization for paths, modes, rename/unlink/fsync ordering, and domain errors; separately require operation-primary precedence, terminal-before-close, and at-most-once release at every close site, and verify terminal/retry assertions and any masking branch are RED before migration.
- [ ] 6.7 For the artifact-cache lock `acquire` path, add passing characterization for lock transfer, context API, labels, and interruption; separately require operation-primary precedence, terminal-before-`self._ops.close`, and at-most-once release, and verify any missing terminal transition, retry exposure, or masking assertion is RED before migration.
- [ ] 6.8 For build-cache namespace/state functions `_open_namespace_fd`, `_validate_relative_dir`, `_ensure_relative_dir`, `prepare_build_cache`, `_open_validated_child`, `_validate_existing_blob`, `_validate_existing_marker`, `close_capability`, and `open_build_cache_state`, add passing characterization for validation order, paths, public state fields, return ownership, and exact errors; separately require operation-primary precedence, tracked handoff, terminal-before-close, and at-most-once release, and verify each unowned or unsafe-handoff assertion is RED before migration.
- [ ] 6.9 For build-cache functions `publish_verified_blob`, `_validate_blob_descriptor`, `_verify_published_blob`, `_open`, `acquire_constructor_project_build_lock`, `_open_blob_algorithm`, and `_remove_snapshot_tree`, add passing characterization for lock semantics, publication/durability, paths, and exact errors; separately require operation-primary precedence, terminal-before-close, and at-most-once release at every residual site, and verify terminal/retry assertions and any masking branch are RED before migration.
- [ ] 6.10 For project-state `_open_private_dir_at`, `_read_metadata`, and `resolve_project_state`, add passing characterization for metadata semantics, paths, public results, and return ownership; separately require operation-primary precedence, tracked parent/child transfer, terminal-before-close, and at-most-once release, and verify each unowned or unsafe-transfer assertion is RED before migration.
- [ ] 6.11 For every release branch in `validate_project_state`, retain and extend the passing characterization in `tests/test_transactions_phase9a_project_state_cleanup.py`: prove branch-specific validation errors and interruption identity, ownership cleared before each close, at-most-once release after success or failure, every independent release attempted, established failure precedence, and correct returned `ValidatedProjectState` ownership; separately require delegation of each release to the shared owner, and verify only the missing shared-owner delegation assertions—not the already-correct terminal, retry, independent-cleanup, or precedence guarantees—are RED before migration.
- [ ] 6.12 For snapshot `_open_validated_source`, `_validate_hard_link_destination`, and `create_artifact_snapshot`, add passing characterization for digest, modes, fsync, unlink policy, returned ownership, and exact `SnapshotError` mapping; separately require operation-primary precedence, terminal-before-close, and at-most-once source/staging release, and verify terminal/retry assertions and any masking branch are RED before migration.
- [ ] 6.13 For `docker/versioning/build_cleanup.py::{_close_leaf,_unlink_entry,_open_algorithm_directory}`, add passing characterization for traversal, unlink behavior, cleanup reports, and interruption; separately require operation-primary precedence, terminal-before-close, at-most-once release, and completion of independent cleanup, and verify nonterminal/retry/skipped-cleanup assertions and any masking branch are RED before migration.
- [ ] 6.14 For `cache_storage.py::{_release_file_and_capability,_release_file_keeping_primary}`, add passing characterization for public storage errors, file-before-capability ordering, and interruption; separately require operation-primary precedence, terminal-before-close, at-most-once release, and completion of both independent releases, and verify nonterminal/retry/skipped-release assertions and any masking branch are RED before migration.
- [ ] 6.15 For `rendering.py::{_validate_effective_destination,write_effective_build}`, add passing characterization for destination validation, output bytes, publication ordering, and public rendering errors; separately require operation-primary precedence, terminal-before-close, and at-most-once generated-fd release, and verify terminal/retry assertions and any masking branch are RED before migration.
- [ ] 6.16 For `build_materialization.py::materialize_artifact`, add passing characterization for streamed bytes, progress, digest verification, fsync/chmod/replace order, temporary-name cleanup, and `MaterializationError`; separately inject write/flush/file-object-close and descriptor-release failures, require the operation or file-object failure to remain primary, require immediate ownership of the `mkstemp()` fd plus terminal at-most-once release, and verify implicit `fdopen(closefd=True)` ownership and missing shared-owner assertions are RED before migration.
- [ ] 6.17 For `rendering.py::write_effective_inventory`, add passing characterization for TOML bytes, destination validation, temporary-name unlink, replace ordering, and public errors; separately inject write and file-object-close failures followed by descriptor-release failure, require operation-primary precedence and immediate ownership of the `mkstemp()` fd with terminal at-most-once release, and verify implicit `fdopen(closefd=True)` ownership and missing shared-owner assertions are RED before migration.
- [ ] 6.18 For `cache.py`'s persistent cache `set()` path, add passing characterization for JSON payload, permissions, temporary entry naming, publication order, and public behavior; separately inject JSON-write and file-object-close failures followed by descriptor-release failure, require operation-primary precedence, terminal at-most-once ownership of the fd returned by `open_private_entry()`, and no publication after failure, and verify implicit `fdopen(closefd=True)` ownership and missing shared-owner assertions are RED before migration.

### GREEN

- [ ] 6.19 Migrate the four characterized npm-publication functions to shared owners and cleanup while preserving the task 6.1 contract.
- [ ] 6.20 Migrate the characterized transaction capability release sites to `docker.filesystem` owners or an equivalent shared terminal owner while preserving the task 6.2 capability API and transfer contract.
- [ ] 6.21 Migrate the characterized transaction locking release sites to shared owners/cleanup while preserving the task 6.3 stage and lock contract.
- [ ] 6.22 Migrate the characterized regular-transaction release sites to shared owners/cleanup while preserving the task 6.4 durability and error contract.
- [ ] 6.23 Migrate the characterized artifact-cache readonly/traversal release sites to shared owners and verified handoffs while preserving the task 6.5 contract.
- [ ] 6.24 Migrate the characterized artifact-cache mutation release sites to shared owners and aggregate cleanup while preserving the task 6.6 ordering and durability contract.
- [ ] 6.25 Migrate the characterized artifact-cache lock release site to shared terminal ownership while preserving the task 6.7 lock API contract.
- [ ] 6.26 Migrate the characterized build-cache namespace/state release sites to shared owners and verified handoffs while preserving the task 6.8 contract.
- [ ] 6.27 Migrate the characterized build-cache publication, verification, lock, algorithm, and snapshot-tree release sites to shared owners/cleanup while preserving the task 6.9 contract.
- [ ] 6.28 Migrate project-state private-directory, metadata, and resolution release sites to shared owners/cleanup while preserving the task 6.10 contract.
- [ ] 6.29 Migrate every characterized `validate_project_state` release branch to shared owners while preserving its existing aggregate cleanup, ownership-clearing order, at-most-once behavior, failure precedence, branch semantics, and returned-state ownership proven by task 6.11.
- [ ] 6.30 Migrate the characterized residual snapshot release sites to shared owners/cleanup while preserving the task 6.12 contract.
- [ ] 6.31 Migrate the characterized build-cleanup release sites to shared owners/cleanup while preserving the task 6.13 traversal and report contract.
- [ ] 6.32 Migrate the characterized cache-storage release sites to shared owners/cleanup while preserving the task 6.14 storage contract.
- [ ] 6.33 Migrate the characterized rendering release sites to shared owners/cleanup while preserving the task 6.15 rendering and publication contract.
- [ ] 6.34 Migrate `materialize_artifact()` to adopt the `mkstemp()` fd immediately into `OwnedDescriptor`, use `os.fdopen(owner.fd, ..., closefd=False)`, and release the owner through shared precedence after file-object cleanup while preserving the task 6.16 materialization contract.
- [ ] 6.35 Migrate `write_effective_inventory()` to adopt the `mkstemp()` fd immediately into `OwnedDescriptor`, use `os.fdopen(owner.fd, ..., closefd=False)`, and release the owner through shared precedence before publication while preserving the task 6.17 rendering contract.
- [ ] 6.36 Migrate the persistent cache `set()` path to adopt the `open_private_entry()` fd immediately into `OwnedDescriptor`, use `os.fdopen(owner.fd, ..., closefd=False)`, and release the owner through shared precedence before publication while preserving the task 6.18 cache contract.

### INTROSPECT

- [ ] 6.37 Add a generated per-function ownership ledger covering tasks 6.1–6.18 using symbol-origin resolution for explicit closes, aliased `os.fdopen`, aliased `tempfile.mkstemp`, assigned close callables, backend-object aliases, and injected descriptor-operation parameters/fields; treat omitted `fdopen(closefd=...)` as `True`, require each `mkstemp()` fd to transfer before a fallible operation, and accept `closefd=False` only with a proven external shared owner; require every former consumer close to resolve to an `OwnedDescriptor`/equivalent owner action or verified handoff, permit an injected backend close only in a shared owner proven terminal before invocation, and fail unresolved close-like calls closed pending non-descriptor proof.

### VALIDATE

- [ ] 6.38 Run focused npm-publication, transactions, artifact-cache, build-cache, project-state, snapshot, build-materialization, build-cleanup, cache-storage, rendering, persistent-cache, locking, durability, and publication suites; require every RED characterization and all existing API compatibility assertions to pass before Phase 7.

## 7. Repository Convergence and Release Gate

**Depends on:** Phase 6.

**Deliverables:** one binding production-tree descriptor-ownership classification covering explicit close calls, `os.fdopen` adoption, and fd-returning `tempfile.mkstemp`; direct descriptor close only in POSIX primitives, gated shared-owner internal releases, or verified handoff operations; every production consumer-owned explicit or implicit release delegates to shared ownership/cleanup; no reverse dependencies or expanded foundation authority; complete verification evidence; all project checks green.

### RED

- [ ] 7.1 Extend `tests/test_descriptor_close_convergence.py` with violating fixtures for bare success/finally close, aliased POSIX close callables, aliased or injected backends, `os.fdopen(fd, ...)` with omitted/true `closefd`, aliased `fdopen`, `tempfile.mkstemp()` whose fd is used before owner transfer, aliased `mkstemp`, `fdopen(closefd=False)` without a proven external owner, nonterminal shared-owner/multi-close, unsafe handoff, aggregate import, and reverse dependency; verify symbol-origin tracking detects every evasion fixture while accepting terminal `OwnedDescriptor.close()`, a proven non-descriptor `.close()`, and `fdopen(closefd=False)` backed by a live shared owner.
- [ ] 7.2 Add a repository-wide fd-ledger fixture spanning all Phase 1–6 families, including `mkstemp` tuple-return adoption and file-object wrappers; require every injected open/create/transfer/wrap/close to have one final owner and verify the fixture detects implicit file-object release, missing immediate adoption, leak, retry, skipped independent cleanup, and consumer calls disguised as shared-owner internals.

### GREEN

- [ ] 7.3 Run convergence audits across the complete production tree; require zero residual consumer-owned direct closes, `fdopen(closefd=True)` adoptions, unowned `fdopen(closefd=False)` wrappers, or unadopted `mkstemp()` descriptors and fail rather than initiating an uncharacterized refactor if any site absent from Phase 1–6 appears.

### INTROSPECT

- [ ] 7.4 Record in `verification.md` every remaining production descriptor close, `os.fdopen` call, and `tempfile.mkstemp` call after alias resolution; require each close call to be a low-level POSIX primitive, terminal shared-owner internal release, or verified principal handoff, require each consumer `fdopen` to use `closefd=False` with a proven live shared owner, require each `mkstemp` fd to transfer immediately to that owner, and verify no bare or implicit consumer release remains.
- [ ] 7.5 Add API/dependency assertions and record the final module DAG, public signatures, compatibility fields, negative filesystem API surface, and three-category direct-close allowlist in `verification.md`; verify each claim is generated by executable introspection.
- [ ] 7.6 Run focused characterization comparisons for lockfile bytes, tree manifests/digests, runtime artifact integrity, build-context contents, cache/project paths and identities, modes, domain exceptions, lock scope, publication ordering, and durability; record the no-drift result in `verification.md`.

### VALIDATE

- [ ] 7.7 Run `ty check docker --python-version 3.14 --output-format concise` and record the passing output in `verification.md`.
- [ ] 7.8 Run `python -m unittest discover -s tests -p 'test_*.py'` and record total tests, skips, duration, and passing result in `verification.md`.
- [ ] 7.9 Run `git diff --check` and record the clean result in `verification.md`.
- [ ] 7.10 Run `openspec validate eliminate-unsafe-direct-descriptor-closes --strict` and require the change to remain valid before marking implementation complete.

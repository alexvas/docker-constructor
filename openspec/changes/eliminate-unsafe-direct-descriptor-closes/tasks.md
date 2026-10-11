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
Phase 6: residual production close migration ───────▶ Phase 6B-A: prevalidated streams
                                                    │
                                                    ▼
                                                Phase 6B-B: docker.process
                                                    │
                                                    ▼
                                                Phase 6B-C: consumer adapters
                                                    │
                                                    ▼
                                                Phase 6B-D: integrated convergence
                                                    │
                                                    ▼
                                                Phase 7: convergence
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

- [x] 3.1 Add snapshot destination tests for operation failure plus close failure, successful copy plus sole close failure, close interruption, and repeated cleanup; require operation-primary precedence, terminal ownership, existing unlink policy, and one close attempt, and verify the owner contract fails before migration.
- [x] 3.2 Add artifact-cache temporary-creation tests for sole child close failure, simultaneous parent close failure, interruption, and repeated cleanup; require the child failure to remain authoritative, parent cleanup to remain independent, terminal child ownership, and one attempt per fd, and verify the owner contract fails before migration.
- [x] 3.3 Add `ValidatedProjectState` direct-close and context-exit tests for success, sole close failure, active primary plus close failure, interruption, and repeated close; require existing public behavior, primary precedence, terminal ownership, and one close attempt, and verify shared-owner delegation fails before migration.

### GREEN

- [x] 3.4 Refactor snapshot destination copy to transfer the created fd immediately to `OwnedDescriptor`, use the owner in active-primary close-plus-unlink accumulation, and use terminal owner release on success; verify task 3.1 passes with unchanged digest, mode, fsync, and unlink semantics.
- [x] 3.5 Refactor artifact-cache temporary creation to transfer the child fd immediately to `OwnedDescriptor` and retain the existing parent accumulator and domain policy; verify task 3.2 passes with unchanged path, mode, and creation behavior.
- [x] 3.6 Refactor `ValidatedProjectState` to delegate namespace fd lifecycle to a private `OwnedDescriptor` while preserving its constructor, public attributes/properties, context-manager behavior, and domain mapping; verify task 3.3 passes.

### INTROSPECT

- [x] 3.7 Add `inspect.signature` and AST assertions proving public snapshot/artifact/project-state interfaces are unchanged, migrated single releases contain no direct raw close or duplicate state machine, and explicit foundation imports introduce no aggregate or reverse dependency; verify all assertions pass.

### VALIDATE

- [x] 3.8 Run snapshot, artifact-cache, project-state, rendering, effective-state, build-cleanup, and build-generation integration suites; require paths, bytes, modes, identities, publication, cleanup, exception graphs, and compatibility imports to pass before Phases 4 or 6.

## 4. Build-Cache Aggregate Owner

**Depends on:** Phase 3.

**Deliverables:** `docker.versioning.build_cache.BuildCacheState`, as returned by `open_build_cache_state()`, privately owns every retained fd through `OwnedDescriptor`; aggregate release becomes terminal before its first syscall; one `CleanupFailures` attempts all five independent owner releases; repeat close is a no-op; its existing constructor, `paths`, `persistent_fd`, `blobs_fd`, `tmp_fd`, `markers_fd`, `transactions_fd`, signatures, and return type remain compatible.

### RED

- [x] 4.1 Obtain the actual `BuildCacheState` from `open_build_cache_state()` and add `BuildCacheState.close()` tests proving `persistent_fd`, `blobs_fd`, `tmp_fd`, `markers_fd`, and `transactions_fd` each receive one close attempt when the first, middle, or last release raises an ordinary failure; verify current stop-on-first-failure behavior fails.
- [x] 4.2 Add tests against the `BuildCacheState` returned by `open_build_cache_state()` with several ordinary close failures; require all five attempts, first-failure authority, later failures as ordered secondary diagnostics, and no repeated operation, and verify current behavior fails.
- [x] 4.3 Add tests against the returned `BuildCacheState` for an unexpected close defect and process-control interruption at each of its five release positions; require every independent release attempt and shared precedence without wrapping the authoritative object, and verify current behavior fails.
- [x] 4.4 Add repeated direct/context cleanup tests for the returned `BuildCacheState`, requiring aggregate terminal state before the first close syscall and no operation after any success or failure outcome; verify current nonterminal lifecycle fails.

### GREEN

- [x] 4.5 Refactor `BuildCacheState` initialization to create one private `OwnedDescriptor` for each of `persistent_fd`, `blobs_fd`, `tmp_fd`, `markers_fd`, and `transactions_fd` while preserving the existing constructor, `paths`, integer field values, and the `BuildCacheState` instance returned by `open_build_cache_state()`; verify construction and compatibility tests pass before changing release behavior.
- [x] 4.6 Refactor `BuildCacheState.close()` to detach or terminally claim the complete five-owner set before release, run every `owner.close` through one `CleanupFailures(None)`, and make repeat close a no-op; verify tasks 4.1–4.4 pass.

### INTROSPECT

- [x] 4.7 Add API/AST assertions proving `BuildCacheState` remains the class returned by `open_build_cache_state()`, its constructor/signatures and `paths`, `persistent_fd`, `blobs_fd`, `tmp_fd`, `markers_fd`, and `transactions_fd` fields are unchanged, exactly one shared owner state machine supplies per-fd terminal behavior, one aggregate accumulator supplies multi-release precedence, and no raw multi-close loop remains; verify all assertions pass.

### VALIDATE

- [x] 4.8 Run direct `open_build_cache_state()`/`BuildCacheState` lifecycle and compatibility tests followed by build-cache publication, locking, maintenance, generation, retention, project-state, and consumer integration suites; require cache paths, identities, lock scope, modes, publication order, durability, and domain errors to pass before Phase 5 or 6.

## 5. Build-Cache Directory Handoff

**Depends on:** Phases 3 and 4.

**Deliverables:** `_open_relative_dir()` and `_bootstrap_constructor_project_lock_parent()` in `docker/versioning/build_cache.py` have complete executable fd ledgers; a child is tracked before fallible parent release; a releasing parent is terminal before close; failed parent close is not retried; opened children do not leak; direct close remains only as the principal verified handoff operation.

### RED

- [x] 5.1 Add `_open_relative_dir()` fd-ledger tests for successful traversal and missing-without-create; account for every open, transfer, return, and close, and verify the current implementation lacks the required explicit terminal ownership evidence.
- [x] 5.2 Add `_open_relative_dir()` tests for open, stat, mkdir, fchmod, and validation failures; require every still-owned fd to receive one release attempt under existing `BuildCacheError` mapping, and verify any unmet ledger case fails before migration.
- [x] 5.3 Add `_open_relative_dir()` parent-close failure tests proving the terminal parent is not retried, the already-open child receives one release attempt, no child is returned, and close interruption remains authoritative; verify the ownership contract fails before migration where applicable.
- [x] 5.4 Add tests targeting `docker.versioning.build_cache._bootstrap_constructor_project_lock_parent()` for unsafe child validation plus ordinary close failure; require the exact `BuildTransactionError` rejection to remain primary, close to remain secondary, and one child release attempt, and verify current masking behavior fails.
- [x] 5.5 Add parent-close failure tests targeting `docker.versioning.build_cache._bootstrap_constructor_project_lock_parent()` proving the parent is terminal before close, the opened child is tracked and released once, the parent is not retried, independent namespace cleanup still runs, and interruption remains authoritative; verify current leak/retry behavior fails.
- [x] 5.6 Add passing characterization tests against `_open_relative_dir()` and `_bootstrap_constructor_project_lock_parent()` in `docker/versioning/build_cache.py`, pinning validation order, owner/mode rules, missing-component creation, labels, path derivation, and public error text before production edits.

### GREEN

- [x] 5.7 Compare the characterized `_open_relative_dir()` contract with `DirectoryDescriptor` factories/child methods in an executable decision test; require direct capability use where all semantics match and an `OwnedDescriptor` ledger where any pre-transfer or policy contract differs.
- [x] 5.8 Refactor `_open_relative_dir()` according to task 5.7 so each child is capability- or owner-tracked before parent release, each parent is terminal before handoff close, and build-cache path/missing/mode/error policy remains local; verify tasks 5.1–5.3 pass.
- [x] 5.9 Refactor `docker/versioning/build_cache.py::_bootstrap_constructor_project_lock_parent()` so validation rejection remains primary, child ownership is established before parent release, and namespace/parent/child cleanup follows the shared lifecycle without retry; verify tasks 5.4 and 5.5 pass.

### INTROSPECT

- [x] 5.10 Add AST and fd-ledger assertions proving migrated handoffs contain no second generic secure walker or bespoke reusable release state, every remaining direct close is a classified principal handoff operation, and build-cache recursion, path derivation, lock policy, and domain errors remain outside `docker.filesystem`; verify all assertions pass.

### VALIDATE

- [x] 5.11 Run the build-cache tests that directly exercise `docker/versioning/build_cache.py::_bootstrap_constructor_project_lock_parent()`, followed by build-lock, build-cache, artifact-materialization, project-state, cleanup, orchestration, and generation suites; require namespace identity, persistent layout, modes, locking, durability, publication, and public failure behavior to pass before Phase 6.

## 6. Residual Production Close Migration

**Depends on:** Phases 1, 2, 3, 4, and 5.

**Deliverables:** every explicit or implicit production descriptor release not migrated by Phases 1–5—including `os.fdopen(..., closefd=True)` and `tempfile.mkstemp()` ownership—has passing characterization for unaffected API/domain semantics and separate RED assertions for the required lifecycle before refactoring; transaction, npm-publication, artifact-cache, build-cache, project-state, snapshot, materialization, cleanup, storage, rendering, and persistent-cache consumers delegate release to shared ownership/cleanup; operation-primary precedence, terminal-before-close, and at-most-once release are required outcomes rather than preserved baseline defects.

### RED

- [x] 6.1 For `docker/npm_environment/publication.py::{_durable_write,_fsync_dir,_fsync_tree,_atomic_publish}`, add passing characterization for bytes, publication visibility, fsync order, and exact domain mapping; separately require operation-primary precedence, terminal-before-close, and at-most-once release, and verify the terminal/repeat assertions and every currently masking precedence case are RED before migration.
- [x] 6.2 For `docker/transactions/capabilities.py::{_adopt,open_regular,DirectoryCapability.close,FileCapability.close}`, add passing characterization for capability APIs, labels, typed errors, adoption, and interruption; prove the existing capability `close()` methods already pass terminal-before-close and at-most-once as shared-owner internals, separately require all three lifecycle properties for `_adopt`/`open_regular` cleanup, and verify their unowned-release assertions are RED before migration.
- [x] 6.3 For `docker/transactions/locking.py::{_verify_unchanged,_release_descriptor}`, add passing characterization for mismatch errors, stage labels, lock semantics, and interruption; separately require operation-primary precedence, terminal-before-close, and at-most-once probe/release behavior, and verify terminal/retry assertions and any masking branch are RED before migration.
- [x] 6.4 For `docker/transactions/regular.py::{durable_unlink,_validate_destination,_close_fd,_discard.close_descriptor}`, add passing characterization for validation, durability, typed errors, and public transaction APIs, and explicitly prove `_close_fd()` plus `_discard()`'s nested `close_descriptor()` already clear `state.fd` before close and suppress retries after success or failure; separately require those helpers to delegate release to the shared owner without weakening their passing terminal/at-most-once guarantees, require operation-primary precedence for all four sites, and verify missing shared-owner delegation plus any masking precedence branch—not the already-correct terminal/retry assertions—are RED before migration.
- [x] 6.5 For artifact-cache readonly/traversal functions `inspect_verified_blob_readonly`, `_ensure_dir_private`, `_open_directory_chain`, `_remove_tree_at`, `stat_blob`, `read_bytes`, `inspect_and_digest`, `release_directory`, and `release_capability`, add passing characterization for validation, paths, return ownership, and public results; separately require operation-primary precedence, child tracking before handoff, terminal-before-close, and at-most-once release at every close site, and verify each unowned or unsafe-handoff assertion is RED before migration.
- [x] 6.6 For artifact-cache mutation functions `ensure_secure_dir`, `create_temp`, `append_temp`, `finalize_temp`, `cleanup_temp`, `quarantine_or_remove`, `get_permissions`, `set_permissions`, `atomic_publish`, and `mkdtemp`, add passing characterization for paths, modes, rename/unlink/fsync ordering, and domain errors; separately require operation-primary precedence, terminal-before-close, and at-most-once release at every close site, and verify terminal/retry assertions and any masking branch are RED before migration.
- [x] 6.7 For the artifact-cache lock `acquire` path, add passing characterization for lock transfer, context API, labels, and interruption; separately require operation-primary precedence, terminal-before-`self._ops.close`, and at-most-once release, and verify any missing terminal transition, retry exposure, or masking assertion is RED before migration.
- [x] 6.8 For build-cache namespace/state functions `_open_namespace_fd`, `_validate_relative_dir`, `_ensure_relative_dir`, `prepare_build_cache`, `_open_validated_child`, `_validate_existing_blob`, `_validate_existing_marker`, `close_capability`, and `open_build_cache_state`, add passing characterization for validation order, paths, public state fields, return ownership, and exact errors; separately require operation-primary precedence, tracked handoff, terminal-before-close, and at-most-once release, and verify each unowned or unsafe-handoff assertion is RED before migration.
- [x] 6.9 For build-cache functions `publish_verified_blob`, `_validate_blob_descriptor`, `_verify_published_blob`, `_open`, `acquire_constructor_project_build_lock`, `_open_blob_algorithm`, and `_remove_snapshot_tree`, add passing characterization for lock semantics, publication/durability, paths, and exact errors; separately require operation-primary precedence, terminal-before-close, and at-most-once release at every residual site, and verify terminal/retry assertions and any masking branch are RED before migration.
- [x] 6.10 For project-state `_open_private_dir_at`, `_read_metadata`, and `resolve_project_state`, add passing characterization for metadata semantics, paths, public results, and return ownership; separately require operation-primary precedence, tracked parent/child transfer, terminal-before-close, and at-most-once release, and verify each unowned or unsafe-transfer assertion is RED before migration.
- [x] 6.11 For every release branch in `validate_project_state`, retain and extend the passing characterization in `tests/test_transactions_phase9a_project_state_cleanup.py`: prove branch-specific validation errors and interruption identity, ownership cleared before each close, at-most-once release after success or failure, every independent release attempted, established failure precedence, and correct returned `ValidatedProjectState` ownership; separately require delegation of each release to the shared owner, and verify only the missing shared-owner delegation assertions—not the already-correct terminal, retry, independent-cleanup, or precedence guarantees—are RED before migration.
- [x] 6.12 For snapshot `_open_validated_source`, `_validate_hard_link_destination`, and `create_artifact_snapshot`, add passing characterization for digest, modes, fsync, unlink policy, returned ownership, and exact `SnapshotError` mapping; separately require operation-primary precedence, terminal-before-close, and at-most-once source/staging release, and verify terminal/retry assertions and any masking branch are RED before migration.
- [x] 6.13 For `docker/versioning/build_cleanup.py::{_close_leaf,_unlink_entry,_open_algorithm_directory}`, add passing characterization for traversal, unlink behavior, cleanup reports, and interruption; separately require operation-primary precedence, terminal-before-close, at-most-once release, and completion of independent cleanup, and verify nonterminal/retry/skipped-cleanup assertions and any masking branch are RED before migration.
- [x] 6.14 For `cache_storage.py::{_release_file_and_capability,_release_file_keeping_primary}`, add passing characterization for public storage errors, file-before-capability ordering, and interruption; separately require operation-primary precedence, terminal-before-close, at-most-once release, and completion of both independent releases, and verify nonterminal/retry/skipped-release assertions and any masking branch are RED before migration.
- [x] 6.15 For `rendering.py::{_validate_effective_destination,write_effective_build}`, add passing characterization for destination validation, output bytes, publication ordering, and public rendering errors; separately require operation-primary precedence, terminal-before-close, and at-most-once generated-fd release, and verify terminal/retry assertions and any masking branch are RED before migration.
- [x] 6.16 For `build_materialization.py::materialize_artifact`, add passing characterization for streamed bytes, progress, digest verification, fsync/chmod/replace order, temporary-name cleanup, and `MaterializationError`; separately inject write/flush/file-object-close and descriptor-release failures, require the operation or file-object failure to remain primary, require immediate ownership of the `mkstemp()` fd plus terminal at-most-once release, and verify implicit `fdopen(closefd=True)` ownership and missing shared-owner assertions are RED before migration.
- [x] 6.17 For `rendering.py::write_effective_inventory`, add passing characterization for TOML bytes, destination validation, temporary-name unlink, replace ordering, and public errors; separately inject write and file-object-close failures followed by descriptor-release failure, require operation-primary precedence and immediate ownership of the `mkstemp()` fd with terminal at-most-once release, and verify implicit `fdopen(closefd=True)` ownership and missing shared-owner assertions are RED before migration.
- [x] 6.18 For `cache.py`'s persistent cache `set()` path, add passing characterization for JSON payload, permissions, temporary entry naming, publication order, and public behavior; separately inject JSON-write and file-object-close failures followed by descriptor-release failure, require operation-primary precedence, terminal at-most-once ownership of the fd returned by `open_private_entry()`, and no publication after failure, and verify implicit `fdopen(closefd=True)` ownership and missing shared-owner assertions are RED before migration.

### GREEN

- [x] 6.19 Migrate the four characterized npm-publication functions to shared owners and cleanup while preserving the task 6.1 contract.
- [x] 6.20 Migrate the characterized transaction capability release sites to `docker.filesystem` owners or an equivalent shared terminal owner while preserving the task 6.2 capability API and transfer contract.
- [x] 6.21 Migrate the characterized transaction locking release sites to shared owners/cleanup while preserving the task 6.3 stage and lock contract.
- [x] 6.22 Migrate the characterized regular-transaction release sites to shared owners/cleanup while preserving the task 6.4 durability and error contract.
- [x] 6.23 Migrate the characterized artifact-cache readonly/traversal release sites to shared owners and verified handoffs while preserving the task 6.5 contract.
- [x] 6.24 Migrate the characterized artifact-cache mutation release sites to shared owners and aggregate cleanup while preserving the task 6.6 ordering and durability contract.
- [x] 6.25 Migrate the characterized artifact-cache lock release site to shared terminal ownership while preserving the task 6.7 lock API contract.
- [x] 6.26 Migrate the characterized build-cache namespace/state release sites to shared owners and verified handoffs while preserving the task 6.8 contract.
- [x] 6.27 Migrate the characterized build-cache publication, verification, lock, algorithm, and snapshot-tree release sites to shared owners/cleanup while preserving the task 6.9 contract.
- [x] 6.28 Migrate project-state private-directory, metadata, and resolution release sites to shared owners/cleanup while preserving the task 6.10 contract.
- [x] 6.29 Migrate every characterized `validate_project_state` release branch to shared owners while preserving its existing aggregate cleanup, ownership-clearing order, at-most-once behavior, failure precedence, branch semantics, and returned-state ownership proven by task 6.11.
- [x] 6.30 Migrate the characterized residual snapshot release sites to shared owners/cleanup while preserving the task 6.12 contract.
- [x] 6.31 Migrate the characterized build-cleanup release sites to shared owners/cleanup while preserving the task 6.13 traversal and report contract.
- [x] 6.32 Migrate the characterized cache-storage release sites to shared owners/cleanup while preserving the task 6.14 storage contract.
- [x] 6.33 Migrate the characterized rendering release sites to shared owners/cleanup while preserving the task 6.15 rendering and publication contract.
- [x] 6.34 Migrate `materialize_artifact()` to adopt the `mkstemp()` fd immediately into `OwnedDescriptor`, use `os.fdopen(owner.fd, ..., closefd=False)`, and release the owner through shared precedence after file-object cleanup while preserving the task 6.16 materialization contract.
- [x] 6.35 Migrate `write_effective_inventory()` to adopt the `mkstemp()` fd immediately into `OwnedDescriptor`, use `os.fdopen(owner.fd, ..., closefd=False)`, and release the owner through shared precedence before publication while preserving the task 6.17 rendering contract.
- [x] 6.36 Migrate the persistent cache `set()` path to adopt the `open_private_entry()` fd immediately into `OwnedDescriptor`, use `os.fdopen(owner.fd, ..., closefd=False)`, and release the owner through shared precedence before publication while preserving the task 6.18 cache contract.

### INTROSPECT

- [x] 6.37 Add a generated per-function ownership ledger covering tasks 6.1–6.18 using symbol-origin resolution for explicit closes, aliased `os.fdopen`, aliased `tempfile.mkstemp`, assigned close callables, backend-object aliases, and injected descriptor-operation parameters/fields; treat omitted `fdopen(closefd=...)` as `True`, require each `mkstemp()` fd to transfer before a fallible operation, and accept `closefd=False` only with a proven external shared owner; require every former consumer close to resolve to an `OwnedDescriptor`/equivalent owner action or verified handoff, permit an injected backend close only in a shared owner proven terminal before invocation, and fail unresolved close-like calls closed pending non-descriptor proof.

### VALIDATE

- [x] 6.38 Run focused npm-publication, transactions, artifact-cache, build-cache, project-state, snapshot, build-materialization, build-cleanup, cache-storage, rendering, persistent-cache, locking, durability, and publication suites; require every RED characterization and all existing API compatibility assertions to pass before Phase 7.

## 6B. Revised Process Ownership — Scope and Historical Evidence

**Depends on:** Phase 6. Subphases 6B-A → 6B-B → 6B-C → 6B-D are required before Phase 7; each follows RED → GREEN → INTROSPECT → VALIDATE.

**Deliverables:** bounded prevalidated `OwnedStream`; reusable `docker.process.OwnedProcess` owning preparation/launch, captured pipes, internally controlled readers and coordinated shutdown; compatibility adapters for streaming/captured npm clients; explicit incomplete cleanup with retained owners and observable eventual outcomes; aligned documentation and executable convergence. Process mechanics belong in `docker.process`, not filesystem or duplicated npm helpers. Docker removal, npm errors, redaction and staging remain domain policy. No process-tree/group, stdin, PTY, arbitrary external reader ownership or runtime-installer migration is included.

**Completion reconciliation:** original 6B.1 baseline characterization and 6B.12 general agent/config guidance carry forward as checked 6BA.1 and 6BD.1. Original 6B.2/6B.6/6B.15 are superseded by 6B-A; 6B.3–6B.5 and 6B.7–6B.10 by 6B-B/6B-C; 6B.11/6B.13/6B.14/6B.16/6B.17 by the documentation, introspection and validation tasks below. Their prior implementation/logs remain historical in `phase6b-verification.md`; removed checkboxes are not proof of revised completion. In particular, invalid-label self-close, self-cleaning adoption mocks and closing after a reader-join timeout are not accepted contracts. Checked Phases 1–6 are unchanged.

## 6B-A. Restore Prevalidated Stream Adoption

**Depends on:** Phase 6. **Deliverables:** rejection leaves caller-owned streams untouched; synchronized at-most-once release and the bounded public API are preserved.

### RED

- [x] 6BA.1 Retain the original 6B.1 passing baseline/inventory in `phase6b-verification.md`: consumer signatures, descriptor-less/missing streams, result/error shapes, decoding/tails, terminate/remove/reap order, grace/deadline bounds, supported workers, and bounded URL/secret redaction. This historical evidence exists; it does not mark the revised process architecture complete.
- [ ] 6BA.2 Replace invalid-label self-release expectations with rejection-before-transfer tests; inject actual label/interface and lock-construction failures, assert no close attempt or mutation of caller-owned streams, and retain operation-plus-close, sole-close, interruption-identity, context-exit, repeat/reentrant/concurrent release contracts. Demonstrate the new rejection assertions RED before production edits and record the log/status separately from historical results.

### GREEN

- [ ] 6BA.3 Restore `OwnedStream` label/interface validation and synchronization preparation before ownership transfer, remove rejection-triggered close, and preserve terminal claim-before-close/outside-lock behavior with `CleanupFailures`; verify 6BA.2 passes without constructor/API expansion, raw-fd ownership, retry, generic proxy or process policy.
- [ ] 6BA.4 Update the stream-adoption examples and rejection contract in `docs/filesystem-lifecycle.md`, preserving all raw-fd/directory/file-wrapper examples and the three direct-close categories; verify positive and negative examples execute and rejected caller streams remain untouched.

### INTROSPECT

- [ ] 6BA.5 Update `tests/test_filesystem_convergence.py` and owned-stream API assertions to prove the unchanged bounded public surface, validation/preparation before transfer, synchronized release, empty aggregate initializer, and no process/domain imports; run isolated imports and verify violating ownership fixtures remain rejected.

### VALIDATE

- [ ] 6BA.6 Run focused owned-stream, filesystem cleanup/API/convergence and lifecycle-doc example suites; preserve complete logs and exit statuses and record the restored contract in `phase6b-verification.md` before starting 6B-B. Do not treat affected legacy self-cleaning adoption tests as final acceptance; replace them in 6BC.2 before the migration gate.

## 6B-B. Reusable OwnedProcess in docker.process

**Depends on:** 6B-A. **Deliverables:** launch-centered ownership of one child, captured stdout/stderr and internal readers; prepared transfer, exhaustive shared-precedence cleanup, safe incomplete-state retention and observable outcomes.

### RED

- [ ] 6BB.1 Add launch/acquisition contracts for `docker.process`: invalid configuration and first/second lock/event/owner/continuation preparation and continuation-worker startup failure must occur before `Popen`; preparation/factory failure acquires no pipe and stops any prepared idle workers; successful launch followed by first/second transfer failure, reader construction/configuration/startup failure, supervisor failure and callback failure must account for every acquired pipe and boundedly reap the child. Inject real pre-transfer failures, never mocks that close candidates themselves; prove RED and save logs/status.
- [ ] 6BB.2 Add an ownership ledger and precedence matrix for first/second/both pipe cleanup failures and both transfer attempts, including first ordinary adoption failure followed by second `KeyboardInterrupt`, operation interruption followed by close defect/interruption in either position, sole close failure, and repeat/concurrent/reentrant cleanup. Require exact authoritative identity, every safe independent release once, all acquired pipes accounted for, no close retry and winning failures retained once; prove RED.
- [ ] 6BB.3 Add setup-shutdown fault tests for interruption/defect in reader `join`, timeout with a reader still alive, sibling close failure and reaping failure. Require independent cleanup despite join failure, no descriptor-backed stream release before reader-stop confirmation, bounded child cleanup and explicit incomplete status instead of success; prove RED with deterministic worker barriers.
- [ ] 6BB.4 Add incomplete-cleanup lifecycle tests: bounded observation timeout, eventual reader stop and sole release, permanent-reader retention until test-controlled release, failed launch with observation access, context exit with/without active failure, late close defect/interruption and repeated/concurrent observation. Require unchanged delivered results/errors, no late callbacks, the same retained owners and no duplicate diagnostics; prove RED and ensure every test releases its controlled workers.
- [ ] 6BB.5 Add generic real-pipe tests for large simultaneous stdout/stderr, descendant-held write ends, partial reader startup, cancellation/deadline races, reader failure and unrelated fd reuse. Require supported bounded completion, retained capture limits, reader-stop-before-release, child reaping and no surviving supported worker; record passing characterization separately from new RED contracts, never substituting descriptor-less doubles.

### GREEN

- [ ] 6BB.6 Implement private generic stream preparation/binding support only as needed by `OwnedProcess.launch`: prepare owner slots and synchronization before resource acquisition, use the same `OwnedStream` state machine, distinguish failed transfer from adopted ownership and retain a release authority for every acquired pipe. Verify 6BB.1/6BB.2 accounting with no fallible replacement constructor, consumer-close fallback, fourth direct-close exemption or changed public `OwnedStream` constructor.
- [ ] 6BB.7 Implement `docker.process.OwnedProcess.launch` and minimal process policy/result contracts for one direct child and captured stdout/stderr; prepare coordinator/continuation capacity before `Popen`, protect the whole launch/setup path and keep pipe owners private. Verify 6BB.1 and the preparation-before-launch ledger; do not add existing-process adoption as the acquisition entry point.
- [ ] 6BB.8 Implement owner-controlled cancellation-aware/non-blocking readers and bounded `capture`/byte-chunk `stream` operations, preserving callback isolation needed by the existing collector and tracking exactly which workers started. Verify 6BB.5; do not accept arbitrary externally owned readers or infer fd safety from descriptor-less seams.
- [ ] 6BB.9 Implement one coordinated shutdown/outcome ledger using `CleanupFailures` with explicit ordinary policies for adoption/setup, joins, independent releases and terminate/kill/reap. Apply precedence across both adoption attempts and finish independent safe actions before raising authority; verify 6BB.2/6BB.3 including exact interruption identity, close defects with a live child and reader-stop-before-release.
- [ ] 6BB.10 Implement retained per-process continuation and cleanup-status/`wait_cleanup(timeout)` observation: bound caller waits, retain active-reader owners, release after actual reader completion, publish late outcomes once and never restart budgets or retry close. Implement context-exit and failed-launch observation access described in design; verify 6BB.4 with no global owner registry, GC authority or late domain callbacks.
- [ ] 6BB.11 Document the generic process API in `docs/process-lifecycle.md`, including launch-centered usage, capture/stream/context examples, prepared ownership, state transitions, incomplete-result observation, late-error rules, supported reader boundaries and explicit exclusions; verify examples execute against the implementation and a stalled reader is never described as cleaned up.

### INTROSPECT

- [ ] 6BB.12 Add API/dependency and ownership-proof tests for `consumer → docker.process → explicit docker.filesystem submodules`: no npm/versioning/transactions/runtime-installer imports or Docker/redaction/path/publication policy in process, no process imports/policy in filesystem, no raw pipe closes or raw-fileno double ownership, one verified private stream-transfer implementation and only the intended process surface. Scan new-module evasions and prove a class name/import alone grants no exemption.

### VALIDATE

- [ ] 6BB.13 Run generic process unit/fault/real-pipe, ownership/API/dependency and executable process-guide suites plus the 6B-A gate; retain complete logs/statuses and record prepared acquisition, safe shutdown, precedence and late-outcome evidence in `phase6b-verification.md` before consumer migration.

## 6B-C. Streaming and Captured Consumer Adapters

**Depends on:** 6B-B. **Deliverables:** existing npm execution and captured cleanup APIs delegate process mechanics to `OwnedProcess`; domain order, output, redaction and controlled error shapes remain stable.

### RED

- [ ] 6BC.1 Extend the 6BA.1 characterization to pin both adapters' signatures, missing streams and injected factories, stdout/stderr decoding/tails, safe diagnostics, supported deadline/grace budgets, callback isolation and Docker terminate/remove/reap order. Preserve passing behavior separately from new RED delegation/incomplete-state assertions and record logs before changing adapters.
- [ ] 6BC.2 Remove self-cleaning adoption mocks in `tests/test_npm_environment_phase6b_streams.py`; use actual pre-transfer preparation/lock failures and injected post-launch transfer faults through the generic owner's seams in both adapters. Assert prelaunch faults never launch, every acquired pipe remains accounted for after launch, each safe stream receives one release attempt, and the child is boundedly reaped; first ordinary adoption failure plus second `KeyboardInterrupt` must preserve interruption identity. Prove new contracts RED.
- [ ] 6BC.3 Add combined consumer setup/join/close fault matrices: ordinary close, unexpected defect, interruption in either pipe, join interruption and live-reader timeout. Assert shared precedence/identity, sibling cleanup and reaping despite failures, no descriptor release while live, same-owner reuse, no supported workers left on completed shutdown and retained owners on incomplete shutdown; prove RED without dropping real-pipe cases.
- [ ] 6BC.4 Add adapter tests for controlled incomplete-cleanup reporting through unchanged result/list/outcome/error shapes, bounded URL/secret sanitization, retained continuation independent of generic-handle retention and no late public output/result mutation. Assert removal/staging policy still runs and removal failure cannot skip process cleanup; prove RED.

### GREEN

- [ ] 6BC.5 Migrate `SubprocessExecutor.run_streaming` and its hooks to a `docker.process` compatibility adapter, removing duplicated stream adoption/reader/process cleanup machinery while retaining Docker removal and domain collection/redaction policy. Verify 6BC.1–6BC.4 streaming cases and the unchanged configured cleanup order/bounds; no global registry or per-hook owner reconstruction.
- [ ] 6BC.6 Migrate `run_captured` and the necessary lifecycle helper adapters to unified process ownership, preserving characterized signatures, `LifecycleOutcome`/list behavior, missing/descriptor-less test seams and sanitized diagnostics. Remove unprotected joins and setup-primary-first ordering; verify 6BC.1–6BC.4 captured cases including ordinary/defect/interruption precedence and reader-stop-before-release.
- [ ] 6BC.7 Update `docs/npm-environment-assembler.md` to explain process-owner delegation, domain terminate/remove/reap responsibilities, supported reader completion, incomplete cleanup and late-outcome diagnostics; verify documented order and all examples against adapter tests.

### INTROSPECT

- [ ] 6BC.8 Add signature/AST/import assertions proving adapters delegate lifecycle machinery to `docker.process`, retain no bare pipe closes, independent raw-fileno owners, owner reset, duplicated generic reader/shutdown state machine or unsanitized generic error-note projection, and do not migrate `docker.runtime_installer`; verify consumer compatibility fields and factory/PIPE aliases remain classified.

### VALIDATE

- [ ] 6BC.9 Run focused npm execution, streaming, captured lifecycle, deadline, diagnostic/redaction, retained-tail, host execution and real-pipe adapter suites together with the generic owner gate. Preserve logs/statuses and compare output/API/order/budget/worker behavior in `phase6b-verification.md`; permit only prevalidated adoption, terminal/shared-precedence and explicit incomplete-cleanup corrections.

## 6B-D. Integrated Convergence, Guidance and Verification

**Depends on:** 6B-C. **Deliverables:** complete-tree process/pipe ownership proof, aligned guidance and executable examples, and revised 6B acceptance before Phase 7.

### RED

- [x] 6BD.1 Retain original 6B.12 root `AGENTS.md` and `openspec/config.yaml` mandatory filesystem-wrapper/lifecycle-test guidance, preserving existing rules and the guide link. These artifacts already require one filesystem owner and grant borrowed resources no release authority; process-specific amendments are separately pending in 6BD.4.
- [ ] 6BD.2 Extend alias-resolved convergence fixtures for the new `docker.process` module and adapters: reject bare/aliased pipe closes, consumer classes disguised as owners, duplicate raw-fileno ownership, reset-per-cleanup owners, skipped failed-transfer accounting, close under a live descriptor reader, lost deferred owners/outcomes and reverse/domain imports. Accept only mechanically proved prepared transfer, terminal synchronized owner release and safe completion/retention; demonstrate unmet gates RED.

### GREEN

- [ ] 6BD.3 Extend the production ownership ledger/API gate to the generic launch, private stream preparation/binding, reader-stop barrier, retained continuation and both adapters; verify 6BD.2 fixtures pass and no fourth direct-close category, name-based allowlist or historical function list hides new production ownership.
- [ ] 6BD.4 Amend `docs/filesystem-lifecycle.md`, root `AGENTS.md`, `openspec/config.yaml` and README links with process-owner selection and the process-guide reference while preserving raw-fd/directory/file-wrapper rules. Verify guidance selects `OwnedProcess` for subprocess coordination and one internal `OwnedStream` per pipe, explains incomplete cleanup and grants no new raw-close authority.

### INTROSPECT

- [ ] 6BD.5 Extend executable documentation/cross-reference tests across filesystem/process/assembler guides and all README variants, including negative examples for caller-composed unprotected launch, rejected-adoption close, raw-fileno ownership, repeated owner creation and closing beneath a live reader; verify examples agree with actual public APIs and convergence enforcement.
- [ ] 6BD.6 Record the final process/filesystem/domain DAG, public process surface, preserved consumer signatures and generated complete-tree pipe ledger; verify no process policy in filesystem, no domain policy in process and no runtime-installer process dependency or accidental runtime image-packaging requirement.

### VALIDATE

- [ ] 6BD.7 Run all 6B-A/B/C focused owner/process/adapter/fault/real-pipe suites plus complete-tree convergence, imports, examples and guidance checks; record complete logs/statuses and revised no-drift comparisons in `phase6b-verification.md`, keeping prior results explicitly historical rather than overwriting them as new acceptance.
- [ ] 6BD.8 Run `.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q`, preserving complete stdout/stderr and runner exit status; run `ty check docker --python-version 3.14 --output-format concise` and `git diff --check`. Record all results and incomplete/deferred-cleanup fault evidence in `phase6b-verification.md`; require successful verification before Phase 7, without silently excluding failures or claiming old logs prove the new architecture.

## 7. Repository Convergence and Release Gate

**Depends on:** Phase 6 and all revised subphases 6B-A through 6B-D.

**Deliverables:** one binding production-tree descriptor-ownership classification covering explicit close calls, `os.fdopen` adoption, fd-returning `tempfile.mkstemp`, and subprocess-created fd-owning streams; direct descriptor close only in POSIX primitives, gated shared-owner internal releases (including synchronized `OwnedStream` release), or verified handoff operations; every production consumer-owned explicit or implicit release delegates to shared ownership/cleanup; no reverse dependencies or expanded foundation domain authority beyond the declared stream lifecycle API; implemented and executable developer rules; complete verification evidence; all project checks green.

### RED

- [ ] 7.1 Extend `tests/test_descriptor_close_convergence.py` with violating fixtures for bare success/finally close, aliased POSIX close callables, aliased or injected backends, `os.fdopen(fd, ...)` with omitted/true `closefd`, aliased `fdopen`, `tempfile.mkstemp()` whose fd is used before owner transfer, aliased `mkstemp`, `fdopen(closefd=False)` without a proven external owner, nonterminal shared-owner/multi-close, unsafe handoff, aggregate import, reverse dependency, and the prepared-process/stream/deferred-cleanup evasions characterized in task 6BD.2; include newly added production-module fixtures so historical function lists cannot hide releases. Verify symbol-origin tracking detects every evasion fixture while accepting terminal `OwnedDescriptor.close()`, synchronized terminal `OwnedStream.close()`, a proven non-descriptor `.close()`, and `fdopen(closefd=False)` backed by a live shared owner.
- [ ] 7.2 Add a repository-wide fd-ledger fixture spanning all Phase 1–6 and 6B families, including `mkstemp` tuple-return adoption, file-object wrappers, prepared subprocess stdout/stderr transfer, reader-stop barriers, retained incomplete-cleanup owners, concurrent cleanup claims, and eventual winning outcomes; require every injected open/create/transfer/wrap/close to have one final owner and verify the fixture detects implicit unowned file-object release, missing immediate adoption, duplicate raw-fileno ownership, reset-per-cleanup owners, leak, retry, skipped independent cleanup, lost concurrent outcomes, and consumer calls disguised as shared-owner internals.

### GREEN

- [ ] 7.3 Run convergence audits across the complete production tree, including `docker.process`, private prepared-transfer machinery, retained cleanup continuations and aliased subprocess/PIPE stream origins; require zero residual consumer-owned direct closes, `fdopen(closefd=True)` adoptions, unowned `fdopen(closefd=False)` wrappers, unadopted `mkstemp()` descriptors, or unowned/doubly owned subprocess streams. Fail rather than initiating an uncharacterized refactor if any site absent from Phases 1–6 and 6B appears; require classification proof rather than blanket stream/name exemptions.

### INTROSPECT

- [ ] 7.4 Record in `verification.md` every remaining production descriptor close, `os.fdopen` call, `tempfile.mkstemp` call, and subprocess-created fd-owning stream after alias resolution; require each close call to be a low-level POSIX primitive, terminal shared-owner internal release, or verified principal handoff, each consumer `fdopen` to use `closefd=False` with a proven live shared owner, each `mkstemp` fd to transfer immediately to that owner, and each subprocess stream to retain one synchronized `OwnedStream` across all cleanup branches, including explicitly incomplete shutdown through the same process owner until safe deferred release. Verify no bare/implicit consumer release, raw-fileno double ownership, reset-per-cleanup adoption, or unsafe concurrent release remains.
- [ ] 7.5 Add API/dependency assertions and record the final module DAG, public signatures, compatibility fields, bounded `OwnedStream` addition, separate `docker.process` surface and incomplete-cleanup contract, negative filesystem/process domain authority, and three-category direct-close allowlist in `verification.md`; include the documentation-example/link and project-guidance checks from Phase 6B and verify each API/dependency/ownership claim is generated by executable introspection.
- [ ] 7.6 Run focused characterization comparisons for lockfile bytes, tree manifests/digests, runtime artifact integrity, build-context contents, cache/project paths and identities, modes, domain exceptions, lock scope, publication ordering, durability, and subprocess output/tails/decoding, bounded sanitized diagnostics, deadline/grace bounds, process cleanup order, reader completion, and real-pipe fd-reuse safety; record no drift except the explicitly authorized lifecycle corrections in `verification.md`.

### VALIDATE

- [ ] 7.7 Run `ty check docker --python-version 3.14 --output-format concise` and record the passing output in `verification.md`.
- [ ] 7.8 Run `python -m unittest discover -s tests -p 'test_*.py'` and record total tests, skips, duration, and passing result in `verification.md`.
- [ ] 7.9 Run `git diff --check` and record the clean result in `verification.md`.
- [ ] 7.10 Run `openspec validate eliminate-unsafe-direct-descriptor-closes --strict` and require the change to remain valid before marking implementation complete.

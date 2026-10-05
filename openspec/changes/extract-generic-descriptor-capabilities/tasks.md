# Binding Implementation Contract

Every checkbox below is an atomic obligation. A task is complete only when its named deliverable exists and its stated verification succeeds. Production behavior, public API, import boundaries, and compatibility not explicitly changed by a task remain unchanged.

The phases form this dependency DAG; a phase may use deliverables only from its listed predecessors:

```text
Phase 1 ─▶ Phase 2 ─▶ Phase 3 ─┬─▶ Phase 4 ───────────────┐
                               ├─▶ Phase 5 ─▶ Phase 6 ────┤
                               └─▶ Phase 7 ───────────────┤
                                                         ▼
                                                      Phase 8
```

Within each phase, work proceeds strictly `RED → GREEN → INTROSPECT → VALIDATE`. RED tasks modify tests only. New contract tests must demonstrate the unmet contract, while characterization tests must pass before production migration to establish a behavioral baseline. GREEN tasks make the minimum production change required by that phase. INTROSPECT tasks bind static API and architecture properties. VALIDATE tasks execute the phase gate.

## 1. Lightweight Cleanup Foundation

**Depends on:** none.

**Deliverables:** empty `docker/filesystem/__init__.py`; `docker/filesystem/cleanup.py` exposing `CleanupFailures`, `attach_secondary`, and `carry_secondary_diagnostics`; compatibility imports from the existing transaction paths; focused behavioral and import-isolation tests.

### RED

- [x] 1.1 Add `tests/test_filesystem_cleanup.py` truth-table tests for `docker.filesystem.cleanup.CleanupFailures`, covering no primary, ordinary primary, process-control primary, ordinary cleanup failure, unexpected cleanup defect, interruption, multiple independent actions, and terminal single-use; verify this new test module fails because the API does not exist.
- [x] 1.2 Add `tests/test_filesystem_cleanup.py` contracts for `attach_secondary` and `carry_secondary_diagnostics`, including frozen domain exceptions and bounded notes; verify these new cases fail because the API does not exist.
- [x] 1.3 Adapt `tests/test_transactions_phase9b_carry.py::CarrySecondaryDiagnosticsTests.test_operation_grants_no_filesystem_or_mapping_authority` to remove transaction-local `__module__` ownership filtering, assert `docker.transactions.errors.carry_secondary_diagnostics is docker.filesystem.cleanup.carry_secondary_diagnostics`, assert the shared function's defining module is `docker.filesystem.cleanup`, and apply the existing prohibited-filesystem-operation inspection to that shared function; verify the adapted test fails only because the foundation/re-export does not yet exist, without introducing a wrapper or rewriting `__module__`.
- [x] 1.4 Add an isolated-subprocess test proving `import docker.filesystem.cleanup` does not load `docker.transactions`, npm, versioning, CLI, or launcher modules and that `docker.filesystem` exports no aggregate API; verify the new case fails before the package exists.

### GREEN

- [x] 1.5 Create an empty `docker/filesystem/__init__.py` and verify importing `docker.filesystem` exposes no names owned by its cleanup, operations, or descriptors submodules.
- [x] 1.6 Implement `CleanupFailures`, `attach_secondary`, and `carry_secondary_diagnostics` in `docker/filesystem/cleanup.py` with the Phase 9A precedence and diagnostic-retention behavior; verify tasks 1.1 and 1.2 pass without changing transaction modules.
- [x] 1.7 Replace the implementations in `docker.transactions.cleanup` and the generic diagnostic portion of `docker.transactions.errors` with direct compatibility imports from `docker.filesystem.cleanup`; verify task 1.3 and existing transaction imports resolve to the same shared callable/class objects, with no transaction-local wrapper and no artificial `__module__` change.

### INTROSPECT

- [x] 1.8 Add AST assertions that `docker/filesystem/cleanup.py` imports only the standard library, that `docker/filesystem/__init__.py` has no imports or re-exports, and that no `docker.filesystem` module imports a domain or `docker.transactions`; verify the assertions pass.
- [x] 1.9 Compare the public signatures, defining modules, and object identity of the old transaction cleanup imports with the new cleanup API using `inspect.signature`; verify foundation ownership, compatibility, and absence of duplicate implementations or wrappers.

### VALIDATE

- [x] 1.10 Run `python -m unittest tests.test_filesystem_cleanup tests.test_transactions_phase9a_accumulator tests.test_transactions_phase9b_carry` and require all tests, including the migrated shared-function authority-boundary check, to pass before Phase 2 starts.

## 2. Descriptor Operations and Owned Lifecycle

**Depends on:** Phase 1.

**Deliverables:** `docker/filesystem/operations.py` with `DescriptorOps` and `PosixDescriptorOps`; `DescriptorError`, `UnsafeDescriptorError`, and `OwnedDescriptor` in `docker/filesystem/descriptors.py`; exact public signatures and irreversible ownership-state behavior.

### RED

- [x] 2.1 Add `tests/test_filesystem_descriptor_operations.py` signature and delegation contracts for every design-specified `DescriptorOps` and `PosixDescriptorOps` method; verify the module fails because the API does not exist.
- [x] 2.2 Add `tests/test_filesystem_owned_descriptors.py` constructor contracts for `DescriptorError(stage, message, *, cause=None)`, inherited `UnsafeDescriptorError` construction, and `OwnedDescriptor(ops, fd, *, label)`; verify exact field/chaining behavior and that invalid fd/label arguments retain caller ownership without operations.
- [x] 2.3 Add `tests/test_filesystem_owned_descriptors.py` contracts proving a directly constructed `OwnedDescriptor.close()` marks release before invoking close and never invokes close a second time after success or failure; verify the tests fail because the class does not exist.
- [x] 2.4 Add `tests/test_filesystem_owned_descriptors.py` contracts proving `OwnedDescriptor.detach()` returns the live fd, makes the source terminal without closing, and rejects subsequent access, detach, or close without issuing an operation; verify the tests fail.
- [x] 2.5 Add context-manager tests proving an ordinary close failure is sole on success, secondary to an active primary, and never prevents an independent later cleanup action; verify the tests fail.

### GREEN

- [x] 2.6 Implement the exact `DescriptorOps` protocol in `docker/filesystem/operations.py`; verify only its signature contracts pass.
- [x] 2.7 Implement `PosixDescriptorOps` as the production `DescriptorOps` adapter over the corresponding `os` operations; verify delegation and errno-preservation tests pass.
- [x] 2.8 Implement `DescriptorError(stage, message, *, cause=None)` and inherited `UnsafeDescriptorError` construction with stable fields and cause chaining; verify the error cases in task 2.2 pass.
- [x] 2.9 Implement directly constructible `OwnedDescriptor(ops, fd, *, label)` with pre-transfer argument validation and no filesystem validation; verify the ownership cases in task 2.2 pass.
- [x] 2.10 Implement the live/transferred/release-attempted state machine and guarded `fd`, `label`, and `released` properties in `OwnedDescriptor`; verify state-access tests pass.
- [x] 2.11 Implement `OwnedDescriptor.close()` as an irreversible at-most-once close attempt; verify task 2.3 passes.
- [x] 2.12 Implement `OwnedDescriptor.detach()` as explicit raw-fd ownership transfer; verify task 2.4 passes.
- [x] 2.13 Implement `OwnedDescriptor.__enter__` and `__exit__` using the Phase 1 cleanup precedence; verify task 2.5 passes.

### INTROSPECT

- [x] 2.14 Add `inspect.signature` assertions for `DescriptorOps`, `PosixDescriptorOps`, `DescriptorError`, `UnsafeDescriptorError`, and directly constructible `OwnedDescriptor` exactly as fixed in `design.md`; verify inherited error construction and absence of extra constructor options.
- [x] 2.15 Add AST assertions that operations and owned descriptors define no path derivation, recursion, durability, locking, transaction, cache, or npm authority; verify the foundation dependency boundary passes.

### VALIDATE

- [x] 2.16 Run `python -m unittest tests.test_filesystem_cleanup tests.test_filesystem_descriptor_operations tests.test_filesystem_owned_descriptors` and require all tests to pass before Phase 3 starts.

## 3. Directory Descriptor API

**Depends on:** Phase 2.

**Deliverables:** complete design-specified `DirectoryDescriptor` API; secure absolute path walk; failure-safe parent/child handoff; basename-only child operations; no recursive tree or durability authority.

### RED

- [x] 3.1 Add `tests/test_filesystem_directory_descriptors.py` construction-authority and public signature contracts proving direct `DirectoryDescriptor(...)` raises `TypeError` before ownership transfer, while `open_secure_path()` and `adopt()` are the only public construction seams; verify they fail because the class does not exist.
- [x] 3.2 Add secure-walk tests for absolute-path enforcement, component-relative no-follow opens, final type validation, optional effective-owner validation, and successful return of only the final live descriptor; verify they fail.
- [x] 3.3 Add secure-walk handoff tests proving a failed parent close is not retried and the already-open child receives exactly one close attempt and is never returned; verify they fail.
- [x] 3.4 Add secure-walk failure tests proving an open, stat, ownership, interruption, or unexpected defect releases every still-owned descriptor once under Phase 1 precedence; verify they fail.
- [x] 3.5 Add `child_basename()` tests rejecting empty, special, absolute, slash, alternate-separator, and NUL-containing names before any injected operation; verify they fail.
- [x] 3.6 Add separate behavior tests for existing-child `open_directory()`, exclusive `create_directory()`, and `open_or_create_directory()`, including mode and owner validation; verify they fail.
- [x] 3.7 Add primitive-operation tests for `stat_child()`, `list_names()`, `unlink_child()`, and `remove_child_directory()`, proving each acts on exactly one validated basename; verify they fail.

### GREEN

- [x] 3.8 Implement the module-private authority-token initializer and `DirectoryDescriptor.adopt()` so direct construction fails before ownership transfer, valid arguments transfer ownership before directory/owner validation, and validation failure releases the adopted fd once under Phase 1 precedence; verify construction-authority and adoption tests pass.
- [x] 3.9 Implement `DirectoryDescriptor.open_secure_path()` with absolute component walking and no-follow opens; verify task 3.2 passes.
- [x] 3.10 Implement secure-walk parent/child ownership handoff through `OwnedDescriptor`; verify tasks 3.3 and 3.4 pass without a repeated close or leaked child.
- [x] 3.11 Implement `DirectoryDescriptor.child_basename()` with the complete rejection contract; verify task 3.5 passes.
- [x] 3.12 Implement `DirectoryDescriptor.open_directory()` for one existing no-follow child; verify its focused cases in task 3.6 pass.
- [x] 3.13 Implement exclusive `DirectoryDescriptor.create_directory()` with explicit mode and post-open validation; verify its focused cases in task 3.6 pass.
- [x] 3.14 Implement `DirectoryDescriptor.open_or_create_directory()` without swallowing errors other than the missing-child branch; verify its focused cases in task 3.6 pass.
- [x] 3.15 Implement `stat_child()`, `list_names()`, `unlink_child()`, and `remove_child_directory()` as single-basename primitive delegations; verify task 3.7 passes.

### INTROSPECT

- [x] 3.16 Enforce the exact `DirectoryDescriptor` API with `inspect.signature`, including keyword-only `label`, `require_owner`, `mode`, and `follow_symlinks` parameters; verify no signature differs from `design.md`.
- [x] 3.17 Add AST/API-negative assertions that `docker.filesystem` exposes no `remove_tree`, recursive traversal, retry, absence-policy, atomic write, durability, lock, cache-root, namespace-layout, or domain-error API; verify all exclusions pass.
- [x] 3.18 Add an fd-ledger test that accounts for every injected open, detach, and close across successful and failing walks; verify every descriptor has exactly one final owner and no descriptor is leaked or closed twice.

### VALIDATE

- [x] 3.19 Run `python -m unittest tests.test_filesystem_cleanup tests.test_filesystem_descriptor_operations tests.test_filesystem_owned_descriptors tests.test_filesystem_directory_descriptors` and require all tests to pass before Phases 4, 5, or 7 start.

## 4. Transaction Capability Integration

**Depends on:** Phase 3.

**Deliverables:** transaction directory capability implemented through the foundation; preserved public `DirectoryCapability.from_fd(ops, fd, label)` with caller-owned pre-transfer validation; stable existing transaction imports and behavior; no second secure-walk or ownership-state implementation.

### RED

- [ ] 4.1 Add `tests/test_transactions_descriptor_integration.py` contracts for `DirectoryCapability.from_fd(ops, fd, label)` proving successful validation transfers sole ownership, failed `fstat` leaves the fd caller-owned and open, failed directory-type validation leaves it caller-owned and open, failed effective-owner validation leaves it caller-owned and open, and no failed validation path invokes close; also preserve existing imports, properties, close semantics, and fault injection, and verify only the shared-implementation assertion fails before migration.
- [ ] 4.2 Add a structural test proving `docker.transactions.capabilities` delegates directory ownership and secure walking to `DirectoryDescriptor` instead of defining a second state machine or component walker; verify it fails before migration.

### GREEN

- [ ] 4.3 Refactor `DirectoryCapability` to reuse or thinly specialize `DirectoryDescriptor` while retaining its transaction-specific authority/token contract and permanent public `from_fd(ops, fd, label)` signature; make `from_fd()` validate `fstat`, directory type, and effective ownership while the caller still owns the fd, then transfer ownership exactly once through the permanent module-internal validated-transfer seam without calling the consuming `DirectoryDescriptor.adopt()` or repeating validation; verify every validation failure preserves the caller-owned open fd, task 4.1 passes, `DirectoryDescriptor.adopt()` remains unchanged, and no duplicate ownership or secure-walk implementation is introduced.
- [ ] 4.4 Remove the superseded transaction-local directory ownership and secure-walk implementation; verify task 4.2 passes and regular-file capability behavior remains present.
- [ ] 4.5 Preserve all existing `docker.transactions` public imports and L1 error mappings through explicit compatibility wiring; verify importing each existing public name succeeds unchanged.

### INTROSPECT

- [ ] 4.6 Add an AST ownership test establishing `docker.filesystem.descriptors` as the sole implementation of directory release state and secure path walking; verify no transaction duplicate remains.
- [ ] 4.7 Compare pre-existing transaction capability signatures and exported names against the compatibility contract; verify the migration adds no new transaction aggregate export.

### VALIDATE

- [ ] 4.8 Run `python -m unittest tests.test_transactions_descriptor_integration tests.test_transactions_l0_posix tests.test_transactions_l1_capabilities tests.test_transactions_locking tests.test_transactions_l2_atomic tests.test_transactions_l2_durable tests.test_transactions_l2_lifecycle tests.test_transactions_phase9_boundaries` and require the existing `DirectoryCapability.from_fd()` signature, success-transfer, validation-failure ownership, and close-lifecycle tests to pass before Phase 8.
- [ ] 4.9 Run `python -m unittest tests.test_constructor_build_generation_integration tests.test_constructor_build_cleanup tests.test_transactions_phase9a_build_cache_cleanup tests.test_transactions_phase9a_project_state_cleanup tests.test_transactions_phase9a_specialized` and require every production consumer that transfers a raw fd through `DirectoryCapability.from_fd()` to pass before Phase 8.

## 5. npm Storage Migration

**Depends on:** Phase 3.

**Deliverables:** `docker/npm_environment/storage.py` uses `DirectoryDescriptor` for namespace and staging directory ownership; existing paths, modes, domain sequencing, and `LockedNpmError` diagnostics remain stable; no raw owned-directory close lifecycle remains.

### RED

- [ ] 5.1 Add npm storage tests for secure-walk parent-close failure proving no repeated close and no leaked next descriptor; verify the new cases fail against the raw handoff implementation.
- [ ] 5.2 Add staging-preparation tests proving fchmod/mkdir/open failures remain primary over ordinary descriptor-close failures and retain secondary diagnostics; verify the new cases fail.
- [ ] 5.3 Add recursive-removal tests proving traversal/removal failures remain primary, each opened directory closes once, and domain-owned absence behavior is unchanged; verify the new cases fail.
- [ ] 5.4 Add passing pre-migration characterization tests for namespace construction, pinning returned paths, `0700` modes, no-follow rejection, ownership rejection, `LockedNpmError.reason`, detail text, and operation order; verify these cases pass before production migration.

### GREEN

- [ ] 5.5 Replace `_open_directory_no_follow()` raw-fd walking with `DirectoryDescriptor.open_secure_path()` and existing domain error mapping; verify task 5.1 passes.
- [ ] 5.6 Replace `_create_or_open_child()` raw ownership with `DirectoryDescriptor.open_or_create_directory()` while preserving `0700`, labels, and `LockedNpmError` mapping; verify namespace characterization remains green.
- [ ] 5.7 Migrate `prepare_assembler_namespace()` descriptor ownership to nested capabilities without changing namespace sequencing or returned paths; verify task 5.4 passes.
- [ ] 5.8 Migrate `prepare_staging_workspace()` descriptor ownership to capabilities without changing exclusive creation or domain diagnostics; verify task 5.2 passes.
- [ ] 5.9 Migrate `_remove_entry()` and `remove_staging_workspace()` to primitive capability operations while retaining recursion and absence policy in `storage.py`; verify task 5.3 passes.

### INTROSPECT

- [ ] 5.10 Add an AST test proving `storage.py` has no raw `os.close()` for owned directory descriptors and imports no `docker.transactions`; verify it imports only the explicit lightweight foundation submodules plus its existing dependencies.
- [ ] 5.11 Add an API-negative test proving no npm path, assembler digest, namespace child, recursion, or `LockedNpmError` reason entered `docker.filesystem`; verify domain ownership remains in npm storage.

### VALIDATE

- [ ] 5.12 Run `python -m unittest tests.test_npm_environment_storage tests.test_transactions_phase8_npm_leaf_mechanics tests.test_transactions_phase8_npm_lock tests.test_npm_environment_phase9a_staging_cleanup` and require all tests to pass before Phase 6 or Phase 8.

## 6. npm Tree Migration

**Depends on:** Phases 3 and 5.

**Deliverables:** `docker/npm_environment/tree.py` uses `DirectoryDescriptor` for traversal, manifest construction, and verification; manifest bytes, containment rules, and domain errors remain stable; recursive traversal remains domain-owned.

### RED

- [ ] 6.1 Add tree traversal tests proving an enumeration/stat/hash failure remains primary over ordinary subdirectory-close failure and every opened directory receives one close attempt; verify the new cases fail against raw closes.
- [ ] 6.2 Add root-open and root-release tests proving validation failures preserve secondary close diagnostics and manifest/verification root descriptors are never closed twice; verify the new cases fail.
- [ ] 6.3 Add passing pre-migration characterization tests pinning canonical manifest bytes, canonical tree digest, entry ordering, no-follow behavior, symlink containment, unsafe-type rejection, and existing `LockedNpmError` mappings; verify these cases pass before migration.

### GREEN

- [ ] 6.4 Migrate `_open_tree_root()` to `DirectoryDescriptor.open_secure_path()` or `adopt()` as appropriate while preserving domain error mapping; verify the root-open cases in task 6.2 pass.
- [ ] 6.5 Migrate `_iter_entries()` directory ownership to `open_directory()` and `list_names()` while retaining recursion in `tree.py`; verify task 6.1 passes.
- [ ] 6.6 Migrate `build_tree_manifest()` and `verify_tree()` root lifecycle to capabilities without changing manifest or verification semantics; verify tasks 6.2 and 6.3 pass.

### INTROSPECT

- [ ] 6.7 Add an AST test proving `tree.py` has no raw `os.close()` for owned directory descriptors, imports no `docker.transactions`, and still owns its recursive traversal functions; verify all assertions pass.
- [ ] 6.8 Compare pre- and post-migration canonical fixture manifests and digests byte-for-byte; verify there is no serialized or identity drift.

### VALIDATE

- [ ] 6.9 Run the focused npm tree, validation, serialization, publication, and Phase 8 leaf-mechanics test modules selected by test discovery for `npm_environment`; require all selected tests to pass before Phase 8.

## 7. Cache Storage Migration

**Depends on:** Phase 3.

**Deliverables:** `docker/versioning/cache_storage.py` uses the lightweight directory foundation; cache resolution and security policy remain domain-owned; its allowlist admits only explicit `docker.filesystem` submodules and still rejects `docker.transactions` and cache consumers.

### RED

- [ ] 7.1 Add cache walker tests proving parent-close failure is not retried and an opened child is released once and never leaked; verify the new cases fail against the raw handoff implementation.
- [ ] 7.2 Add cache validation/creation tests proving ownership, type, mode, and publication failures remain primary over ordinary descriptor-close failures with secondary diagnostics retained; verify the new cases fail.
- [ ] 7.3 Add architecture tests allowing only the design-specified explicit `docker.filesystem` imports while continuing to reject `docker.transactions`, aggregate filesystem imports, and higher cache consumers; verify the positive adoption case fails before migration.
- [ ] 7.4 Add passing pre-migration characterization tests pinning pure path resolution, XDG behavior, `0700` hardening, parent non-mutation, recovery guidance, and `CacheStorageError`/`InventoryError` mapping; verify these cases pass before migration.

### GREEN

- [ ] 7.5 Migrate `_open_parent_fd()` traversal to `DirectoryDescriptor.open_secure_path()`/child operations while preserving cache-specific root policy and labels; verify task 7.1 passes.
- [ ] 7.6 Migrate cache directory inspection and create/secure helpers to capabilities while preserving ownership and mode policy; verify the relevant cases in tasks 7.2 and 7.4 pass.
- [ ] 7.7 Migrate `open_private_entry()`, `publish_private_entry()`, and explicit-XDG directory ownership only where covered by the directory foundation, leaving regular-file ownership and cache policy local; verify the remaining cases in task 7.2 pass.
- [ ] 7.8 Update `_CACHE_STORAGE_ALLOWED_IMPORTS` to admit the exact explicit lightweight submodules used by `cache_storage.py` and no aggregate package; verify task 7.3 passes.

### INTROSPECT

- [ ] 7.9 Add an AST test proving `cache_storage.py` has no raw `os.close()` for migrated owned-directory lifecycles and still contains all cache-root resolution and security-policy functions; verify the ownership boundary passes.
- [ ] 7.10 Run the no-filesystem-I/O guard over every pure resolution function and verify the capability migration introduced no filesystem access into the pure layer.
- [ ] 7.11 Add reverse-dependency assertions that `docker.filesystem` imports neither `docker.versioning` nor `docker.transactions` and that no cache consumer gains cache-root policy authority; verify the dependency direction passes.

### VALIDATE

- [ ] 7.12 Run `python -m unittest tests.versioning.test_cache_storage tests.versioning.test_cache_storage_security tests.test_ownership_cutover_phase5 tests.test_cache_root_ownership_phase3 tests.test_moved_local_state_obligations_phase5` and require all tests to pass before Phase 8.

## 8. Convergence, Audit, and Release Gate

**Depends on:** Phases 4, 5, 6, and 7.

**Deliverables:** one implementation of generic descriptor ownership and secure walking; compatibility and architecture audits; complete verification evidence in `openspec/changes/extract-generic-descriptor-capabilities/verification.md`; all project checks green.

### RED

- [ ] 8.1 Add a repository-wide AST contract enumerating every `docker.filesystem` public definition, compatibility re-export, migrated consumer import, forbidden reverse dependency, duplicate secure walker, and raw migrated owned-directory close; verify it detects an intentionally supplied violating fixture.
- [ ] 8.2 Add an isolated import-matrix contract for `docker.filesystem`, transaction compatibility paths, npm storage/tree, and cache storage that records loaded project modules and rejects aggregate or reverse loading; verify it detects an intentionally supplied violating fixture.

### GREEN

- [ ] 8.3 Remove any remaining duplicate generic directory ownership or secure-walk implementation identified by task 8.1 without migrating a new consumer or changing domain policy; verify the repository-wide AST contract passes.
- [ ] 8.4 Correct any remaining import leakage identified by task 8.2 without adding aggregate exports to `docker.filesystem`; verify the isolated import matrix passes.

### INTROSPECT

- [ ] 8.5 Run the repository-wide close inventory and record every remaining `os.close(fd)`/injected close as primitive, sole failure, deliberate swallow, protected cleanup, or explicitly out-of-scope consumer in `verification.md`; verify no migrated npm storage/tree or cache-storage owned-directory close remains unclassified.
- [ ] 8.6 Record the final module DAG, exact public API signatures, compatibility object identities, negative API surface, and cache-storage allowlist in `verification.md`; verify each recorded claim is generated from an executable introspection check.
- [ ] 8.7 Compare user-visible domain exceptions, persistent paths, modes, manifest bytes, identities, publication behavior, locks, and durability contracts against pre-change characterization tests; record the no-drift result in `verification.md`.

### VALIDATE

- [ ] 8.8 Run `ty check docker --python-version 3.14 --output-format concise` and record the passing output in `verification.md`.
- [ ] 8.9 Run `python -m unittest discover -s tests -p 'test_*.py'` and record the total, skips, duration, and passing result in `verification.md`.
- [ ] 8.10 Run `git diff --check` and record the clean result in `verification.md`.
- [ ] 8.11 Run `openspec validate extract-generic-descriptor-capabilities --strict` and require the change to remain valid before marking the implementation complete.

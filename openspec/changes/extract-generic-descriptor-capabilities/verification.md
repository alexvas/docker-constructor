# Verification: Extract Generic Descriptor Capabilities

This file contains implementation evidence. Planning requirements remain in the
proposal, design, specs, and tasks files.

## Phase 9 convergence audit

### Executable contracts

`python -m unittest tests.test_filesystem_convergence` passes 4 tests. The suite
parses every production Python module and launches a fresh interpreter for each
runtime import target. Its intentionally violating fixtures prove both audits
fail on aggregate/reverse imports, a second secure walker, and unclassified
`os.close`, injected `ops.close`, or member `self._ops.close` calls.

The focused characterization gate also passes:

```text
python -m unittest tests.test_filesystem_convergence tests.test_filesystem_cleanup \
  tests.test_filesystem_descriptor_operations tests.test_filesystem_owned_descriptors \
  tests.test_filesystem_directory_descriptors tests.test_transactions_descriptor_integration \
  tests.test_npm_environment_storage tests.test_npm_environment_tree \
  tests.versioning.test_cache_storage tests.versioning.test_cache_storage_security \
  tests.versioning.test_cache_storage_capabilities
Ran 381 tests in 1.692s — OK
```

### Close inventory

The repository inventory selected every production call whose target is
`os.close`, `ops.close`, or `self._ops.close`. The permanent Phase 9 AST gate
applies the same three-form detection to every migrated module, and its fixture
proves none of the injected forms can evade classification. The inventory found
103 calls: 2 primitive adapters or
regular-file primitives, 6 sole-failure calls, 8 protected cleanup calls, 0
deliberate swallows, and 87 explicitly out-of-scope consumers. The complete
location classification follows (line numbers are executable AST evidence):

- **primitive:** `docker/filesystem/operations.py:65`,
  `docker/npm_environment/tree.py:302` (the domain-owned regular-file hashing
  primitive, not an owned-directory lifecycle).
- **sole failure:** `docker/filesystem/descriptors.py:145`;
  `docker/transactions/capabilities.py:525`; `docker/transactions/locking.py:206`;
  `docker/transactions/posix.py:70`; `docker/transactions/regular.py:478,507`.
- **protected cleanup:** `docker/transactions/capabilities.py:175,403`;
  `docker/transactions/locking.py:200,244`;
  `docker/transactions/regular.py:298,460`;
  `docker/versioning/cache_storage.py:465,483` (pre-adoption caller-owned raw
  descriptors, both accumulated through `CleanupFailures`).
- **explicitly out-of-scope consumers:**
  `docker/runtime_installer.py:274`;
  `docker/npm_environment/execution.py:1050`;
  `docker/npm_environment/publication.py:312,328,353,541`;
  `docker/versioning/artifact_cache.py:402,411,420,925,934,979,985,988,1020,1055,1082,1091,1153,1162,1177,1188,1194,1217,1226,1244,1253,1271,1296,1313,1333,1342,1369,1370,1394,1511`;
  `docker/versioning/build_cache.py:214,249,290,296,299,365,400,453,488,506,548,550,563,571,698,714,723,771,808,843,1038,1041,1048,1056,1145,1397,1629`;
  `docker/versioning/build_cleanup.py:228,291,348,358`;
  `docker/versioning/build_context_confinement.py:234`;
  `docker/versioning/build_snapshot.py:143,187,204,248,573,619`;
  `docker/versioning/project_state.py:86,138,148,215,361,455,458,471,483,486,489`;
  `docker/versioning/rendering.py:1708,1848`.

No raw owned-directory close remains in migrated npm storage. Npm tree's only
raw close is the intentionally local regular-file hash descriptor. Cache
storage's two injected raw closes occur before capability adoption and use
protected cleanup; all adopted directory releases use capabilities.

### Final module DAG and API

The AST and isolated import checks generate and verify this direction:

```text
npm_environment.storage ─┐
npm_environment.tree ────┼──> docker.filesystem.{cleanup,operations,descriptors}
versioning.cache_storage ─┤                         ^
transactions.capabilities┘                         |
transactions.{cleanup,errors} ---------------------┘
```

`docker.filesystem` has no reverse dependency on transactions, npm, or
versioning. Its initializer is empty and exports no aggregate API. Cache storage
loads neither transactions nor npm, and transaction compatibility targets load
no npm or versioning domain consumer.

Normal imports of `docker.npm_environment.storage` and `.tree` intentionally
retain the existing eager `docker.npm_environment` package behavior. The
runtime test pins its exact 42-module project baseline and rejects any added or
missing module. This is a narrow existing-package exception, not a leaf-module
dependency allowance: the AST gate separately requires storage/tree to import
the explicit filesystem submodules and rejects any direct `docker.transactions`
import.

Executable `inspect.signature` contracts in
`tests.test_filesystem_{cleanup,descriptor_operations,owned_descriptors,directory_descriptors}`
verify these exact public definitions:

- cleanup: `CleanupFailures(primary)`, `run(action, *, ordinary)`, `complete()`;
  `attach_secondary(primary, secondary)` and
  `carry_secondary_diagnostics(target, source)`.
- operations: `DescriptorOps` and `PosixDescriptorOps` with `openat`, `close`,
  `fstat`, `fchmod`, `mkdirat`, `statat(*, follow_symlinks)`, `listdir`,
  `unlinkat`, and `rmdirat`.
- descriptors: `DescriptorError(stage, message, *, cause=None)`, inherited
  `UnsafeDescriptorError`, `OwnedDescriptor(ops, fd, *, label)`, and the
  design-specified `DirectoryDescriptor` class factories and basename-only
  child methods.

Object-identity assertions verify that transaction cleanup and diagnostic
compatibility imports are the foundation objects themselves, not wrappers.
Negative-surface assertions verify there is no recursive traversal, cache-root,
namespace, locking, durability, publication, retry, absence-policy, or domain
error API in the foundation.

The cache-storage AST allowlist is exactly:

```text
__future__, errno, os, pathlib, stat, errors, model,
docker.filesystem.cleanup, docker.filesystem.descriptors,
docker.filesystem.operations
```

### User-visible no-drift result

The 381-test focused gate above rechecked transaction compatibility and the npm
storage/tree and cache-storage characterization suites. It preserves domain
exception types/reasons, persistent namespace and cache paths, `0700` modes,
canonical manifest bytes and digests, identity behavior, no-follow containment,
publication semantics, ownership transfer, and close precedence. Existing
transaction locking and durability behavior is additionally covered by the
complete-suite release gate below. No user-visible drift was observed.

## Final release gates

### Type check (task 9.8)

```text
ty check docker --python-version 3.14 --output-format concise
All checks passed!
```

### Complete unit suite (task 9.9)

The exact required discovery command completed successfully:

```text
python -m unittest discover -s tests -p 'test_*.py'
Ran 5672 tests in 104.489s
OK (skipped=13)
```

A preceding attempt to add `/usr/bin/time -p` was unavailable in this image;
the required command itself reports the recorded duration above.

### Diff check (task 9.10)

```text
git diff --check
(clean; exit status 0)
```

### OpenSpec validation (task 9.11)

```text
openspec validate extract-generic-descriptor-capabilities --strict
Change 'extract-generic-descriptor-capabilities' is valid
```

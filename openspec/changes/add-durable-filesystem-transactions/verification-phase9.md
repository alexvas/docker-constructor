# Phase 9 — L0–L3 Boundary and Consumer Inventory (verification)

**Change:** `add-durable-filesystem-transactions`
**Phase:** 9 (tasks 9.1–9.9)
**Result:** PASS

## Enforced boundaries

`tests/test_transactions_phase9_boundaries.py` and
`tests/test_transactions_phase9_consumer_inventory.py` enforce that:

- L2 exposes separately named regular-file contracts, with no generic
  `atomic_write` or durability selector;
- the shared package exposes no project-wide path VFS, generic envelope,
  journal/recovery engine, tree publisher, content-addressed authority, or
  domain path/deletion interpretation;
- descriptor capabilities never reconstruct pathnames;
- shared failures are translated at domain boundaries while interruptions and
  raw causes remain observable through the existing adapters;
- the mandatory direct-L2 consumers are build control/generations, project
  identity metadata, effective build projection, and runtime projection;
- that set is a migration obligation, not a permission boundary: specialized
  L3 protocols may compose compatible leaves while retaining domain authority;
- blobs, npm trees, snapshots/confinement, quarantine/recursive cleanup,
  advisory indexes, and user/evidence outputs remain L3 protocols; and
- only `docker/transactions/posix.py` calls `fcntl.flock` directly.

Focused result:

```text
python -m unittest tests.test_transactions_phase9_boundaries \
  tests.test_transactions_phase9_consumer_inventory
Ran 37 tests in 0.066s
OK
```

## Production writer matrix

The machine-checked matrix is
`tests/data/filesystem_writer_matrix.json`. It classifies each detected
production Python filesystem writer by domain owner, owning L0–L3 layer,
highest compatible shared layer, operations, and either the compatible-leaf
decision or a semantic mismatch justification.

Key decisions:

- **L0/L1/L2 substrate:** `posix.py` is the sole syscall backend;
  capabilities validate retained descriptors; locking remains a distinct
  capability protocol; `regular.py` implements the complete leaf contracts.
- **Required direct L2:** generation manifests use durable no-clobber; build
  control and effective build projection use durable replacement/removal;
  project metadata uses durable no-clobber; runtime projection uses atomic
  no-clobber.
- **Compatible specialized reuse:** npm advisory index and private leaf reads
  reuse L2, while npm manifest/evidence writes descend because recursive seal
  ordering has a different durability boundary.
- **npm tree sealing:** `docker/npm_environment/validation.py` retains L3
  authority for recursive `os.chmod` enforcement across a validated npm tree;
  a multi-entry tree seal is incompatible with a single-file L2 contract.
- **Launcher staging:** `docker/launcher.py` retains domain-owned ephemeral
  authority for runtime temporary-directory creation; directory-tree staging,
  containment, and lifecycle are not L2 regular-file publication.
- **Justified specialized I/O:** content-addressed blobs, build cleanup
  batches, npm trees, snapshots/confinement, quarantine, recursive cleanup,
  evidence/user outputs, transport staging, launcher temporary directories,
  and installation/workspace trees retain L3 authority because their identity,
  commit unit, validation, recovery, retention, or lifecycle exceeds one L2
  regular-file operation.
- **Future metadata:** current metadata cache storage remains owned by the
  metadata domain; the downstream change adopts shared leaves without gaining
  a generic envelope, journal, build-generation recovery, or broad lock.

The inventory test scans production sources for write, rename, link, unlink,
recursive-delete, lock, and L2-contract markers. Its mutation coverage also
includes `os.chmod`, `os.mkdir`, `os.makedirs`, `Path.mkdir`-style calls,
`os.rmdir`, and `os.symlink`, and it fails if any detected production writer is
unclassified. Every L1/L0 descent in the matrix must carry a non-empty mismatch
justification.

## Direct syscall and duplicate-helper audit

```text
rg -n 'fcntl\.flock' docker --glob '*.py'
docker/transactions/posix.py:55:        fcntl.flock(fd, operation)
```

No required-adoption module defines `atomic_write`, `durable_write`, or
`write_atomic`. Specialized direct syscalls remain documented in the matrix
only where the complete L2 contract does not fit.

## Downstream plan reconciliation

The non-specification planning artifacts for
`add-locked-image-owned-pi-extensions` now state that the change consumes
shared locks, explicit regular-file contracts, and the canonical JSON codec,
while owning complete closed versioned sync-lock-journal and settings-sidecar
schemas plus their recovery, multi-target, and CAS protocols.

`revalidate-update-metadata` already states that metadata owns envelope schema
validation, request/auth identity, and cache-key authority; the shared layer
receives bytes and owned paths only, and metadata adopts neither a recoverable
journal nor a broad transaction lock. No downstream specification file was
modified.

## Dependency-direction review

Reviewed the complete dependency direction for cycles, unnecessary descent,
domain-authority leakage, generic APIs, false durability, exception drift,
undocumented writers, one-consumer abstractions without security benefit, and
accidental specialized-protocol migration.

Findings and resolutions:

1. The Pi-extension plan still described a shared journal-envelope and
   single-authority recovery substrate. Its design/tasks were reconciled to
   domain-owned journal/sidecar schemas and recovery over narrow shared
   primitives.
2. No direct production `flock` remained outside L0.
3. Expanding mutation detection found recursive npm mode enforcement in
   `docker/npm_environment/validation.py` and launcher runtime
   temporary-directory creation in `docker/launcher.py` that were previously
   omitted from the inventory matrix. Both are domain-owned L3/ephemeral
   operations with explicit L2 mismatch justifications; no production-code
   migration is required, and the final matrix scan has no unclassified writer.
4. No generic VFS, envelope, tree/content-addressed publisher, configurable
   durability API, or shared domain authority was found.
5. No dependency cycle or unjustified descent requiring production changes
   was found.

## Validation

Broad Phase 9 parity selection:

```text
python -m unittest \
  [L0-L2, locking, Phase 6-9 transaction modules, snapshot, confinement,
   evidence/output, cache, runtime cache, npm publication/storage/tree/
   cleanup/cancellation modules]
Ran 566 tests in 6.090s
OK (skipped=2)
```

Repository checks:

```text
npx pi-green-loop check --since HEAD --feedback
pi-green-loop: all checks passing
```

The configured checks are:

```text
ty check docker --python-version 3.14 --output-format concise
python -m unittest discover -s tests -p 'test_*.py'
```

Additional hygiene:

```text
git diff --check
python -m json.tool tests/data/filesystem_writer_matrix.json
```

Both passed. The required direct-L2 set, specialized L3 list,
highest-compatible-layer decisions, justified direct syscalls, and absence of
generic envelope/VFS/transaction authority are recorded above and enforced by
tests.

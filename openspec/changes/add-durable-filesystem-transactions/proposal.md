## Why

Constructor independently implements owner-private locking, durable replacement, and cleanup across build artifacts, runtime artifacts, and npm-environment publication. Those implementations provide uneven security and durability guarantees, while the build cache has no durable evidence from which to resume superseded-blob cleanup after interruption following manifest commit.

This change introduces one layered filesystem substrate, migrates matching consumers without changing their domain-specific concurrency policies, and makes build cleanup recoverable without a separate mutable cleanup journal.

## What Changes

- Add shared owner-private durable I/O, validated advisory locks, explicit blocking and fail-fast policies, and namespace-bound live-lock capabilities.
- **BREAKING** Replace the development build cache's mutable `committed-build.json` with immutable, no-clobber `committed-build-<20-digit-generation>.json` files ordered by a monotonic counter under the checkout build lock. The newest valid generation is authoritative; no legacy cache migration is provided.
- Use the retained previous build manifest as bounded durable cleanup evidence. On discovery, synchronize the generation directory before accepting any visible newest generation as authoritative. After publication, durably remove uncommitted markers for blobs admitted to the authoritative generation before superseded cleanup; discovery repeats this marker reconciliation and fsyncs the marker directory even when those markers are already absent. Any reconciliation failure blocks superseded cleanup and build work and returns `ExitKind.OPERATIONAL` (exit code `4`) without rolling back the authoritative generation. Then, and again before a later build if cleanup was interrupted, attempt every blob in `previous - current`; remove candidate blobs and markers in batches and fsync each affected parent directory once after its batch before durably removing the previous manifest. Newly materialized blobs that never enter a committed generation remain tracked by their uncommitted markers and are reclaimed by the existing fixed 30-day TTL policy if no later build commits them.
- Treat any incomplete pre-build or post-build cleanup as an operational failure: preserve the previous manifest, report all candidate failures, return `ExitKind.OPERATIONAL` (exit code `4`), and do not admit another build until recovery succeeds.
- Refactor runtime-artifact and npm-environment locks to use the shared layers while preserving identity-scoped blocking, immutable publication, quarantine, evidence validation, and cache ownership boundaries.
- Provide distinct regular-file contracts for atomic no-clobber publication, durable no-clobber publication, durable replacement, validated reads, and durable unlink. Require direct adoption for build control/generation files, project identity metadata, the effective build projection, and runtime projection publication; future metadata envelopes may adopt them in their owning change.
- Keep content-addressed build/runtime blob publication, npm immutable output trees, snapshots and build-context confinement, quarantine and recursive cleanup, advisory indexes, and user/evidence outputs as domain-owned protocols. Consumers SHALL prefer the highest shared layer whose complete contract fits: specialized L3 protocols may compose compatible L2 leaf contracts, or fall back to L1/L0 only where no higher contract is sufficient, without delegating domain identity, commit, collision, recovery, or lifecycle semantics.
- Provide a deterministic canonical JSON codec without a generic envelope schema. Build generations retain a build-owned closed schema; later multi-target journals, settings sidecars, and metadata cache envelopes remain wholly owned and versioned by their implementing changes.

## Capabilities

### New Capabilities
- `durable-filesystem-transactions`: Defines secure owner-private locks, durable filesystem primitives, recoverable immutable-generation cleanup, and bounded extension seams for Constructor-owned state.

### Modified Capabilities
- `build-artifact-materialization`: Changes the committed build-set representation and makes incomplete superseded-blob cleanup an operational failure that must recover before another build starts.

## Impact

Affected code includes a new `docker/transactions/` package plus `docker/versioning/build_cache.py`, `docker/versioning/project_state.py`, `docker/versioning/rendering.py` for the effective build projection, `docker/versioning/effective.py` for runtime projection publication, `docker/versioning/artifact_cache.py`, `docker/npm_environment/publication.py`, `docker/npm_environment/storage.py`, build orchestration, and CLI result mapping. Tests will cover each distinct regular-file contract, descriptor/syscall fault injection, generation recovery, aggregate cleanup diagnostics, exit code `4`, policy-preserving consumer migrations, and the explicit specialized-protocol boundary.

The change follows the archived `materialize-build-artifacts-on-host` work. Developers must delete the old development cache once using an explicit project instruction before the change is archived; after the cutover, runtime code will not inspect, adopt, reject, or delete `committed-build.json`, and generation-state discovery will ignore it. Planned `add-locked-image-owned-pi-extensions` work may consume shared locks, durable I/O, and the canonical JSON codec but owns its multi-target journal and settings-sidecar schemas and CAS semantics. Metadata-cache work may consume durable I/O and the codec without broad locks or build-generation cleanup and owns its complete cache-envelope schema.

## Why

Several filesystem leaf modules independently implement secure directory walking, descriptor ownership transfer, and raw `os.close()` cleanup. This duplicates the existing transaction capability mechanics and leaves real masking-close, descriptor-retry, and descriptor-leak paths in npm storage/tree and shared cache storage, while importing `docker.transactions` would pull the full L0–L2 substrate across deliberately lightweight dependency boundaries.

## What Changes

- Introduce a lightweight generic descriptor-capability foundation outside `docker.transactions`, limited to injected descriptor operations, owned descriptor lifecycle, secure directory traversal, basename-only child directory operations, validation, and primary-preserving exactly-once release.
- Keep recursive tree protocols, cache-root policy, npm namespace layout, durability, publication, locking, retries, absence policy, and domain error mapping outside the generic foundation.
- Refactor `docker.transactions` directory capabilities to consume or re-export the shared foundation rather than maintaining a parallel ownership implementation.
- Migrate `docker/npm_environment/storage.py` and `tree.py` from raw directory-descriptor ownership to the generic capabilities, preserving their domain errors and behavior while preventing close failures from masking active failures.
- Migrate the corresponding directory mechanics in `docker/versioning/cache_storage.py` and deliberately amend its acyclic-leaf import boundary to admit only the lightweight foundation, not the aggregate `docker.transactions` package.
- Audit the migrated descriptor handoffs and releases for at-most-once close attempts, no leaked child descriptors, deterministic failure precedence, and preservation of all independent cleanup actions.

## Capabilities

### New Capabilities
- `descriptor-capabilities`: Lightweight, domain-neutral contracts for secure directory descriptors, explicit ownership transfer, basename-only child operations, and primary-preserving release.

### Modified Capabilities
- `locked-npm-environment-assembly`: Require assembler storage and tree inspection to preserve active failures and descriptor ownership while retaining existing no-follow containment and domain diagnostics.
- `user-cache-storage`: Require cache-path descriptor traversal and release to preserve active failures without retrying ambiguous failed closes or broadening cache storage into a transaction-substrate consumer.

## Impact

Affected code includes a new lightweight descriptor foundation, `docker/transactions/capabilities.py` and its imports/re-exports, npm `storage.py` and `tree.py`, `versioning/cache_storage.py`, architecture allowlist tests, fault-injection tests, and existing callers or tests that patch raw `os` descriptor operations. No public CLI, cache layout, npm identity, publication format, lock policy, or durability contract changes are intended.

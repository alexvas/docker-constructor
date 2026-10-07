## Why

Several filesystem leaf modules independently implement secure directory walking, descriptor ownership transfer, and raw `os.close()` cleanup. This duplicates the existing transaction capability mechanics and leaves real masking-close, descriptor-retry, and descriptor-leak paths in npm storage/tree and shared cache storage, while importing `docker.transactions` would pull the full L0–L2 substrate across deliberately lightweight dependency boundaries.

## What Changes

- Introduce a lightweight generic descriptor-capability foundation outside `docker.transactions`, limited to injected descriptor operations, owned descriptor lifecycle, secure directory traversal, basename-only child directory operations, validation, and primary-preserving exactly-once release.
- Keep recursive tree protocols, cache-root policy, npm namespace layout, durability, publication, locking, retries, absence policy, and domain error mapping outside the generic foundation.
- Refactor `docker.transactions` directory capabilities to consume or re-export the shared foundation rather than maintaining a parallel ownership implementation.
- Unify the public transaction L1 error boundary so operational open, stat, read, and close failures are exposed as typed transaction errors with their original POSIX causes, while capability misuse, unsafe objects, process-control exceptions, ownership transfer, and cleanup precedence retain their distinct contracts. Once descriptor ownership has transferred to a transaction capability, consumers preserve its typed close-stage error rather than restoring a raw `OSError`; raw descriptor cleanup before transfer remains a POSIX-error boundary.
- Remove higher-layer compatibility adapters that replace a typed transaction or lock failure with its raw `OSError` cause when no confirmed public contract requires that raw exception type. Consumers may inspect a cause for absence, errno, safety, or domain classification, but otherwise preserve the typed failure unchanged or chain it directly from a domain error so stage, cause, and secondary cleanup diagnostics remain together.
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

Affected code includes a new lightweight descriptor foundation, `docker/transactions/capabilities.py` and its imports/re-exports, transaction error stages and L1 callers that currently observe raw `OSError`, npm `storage.py` and `tree.py`, `versioning/cache_storage.py`, architecture allowlist tests, fault-injection tests, and existing callers or tests that patch raw `os` descriptor operations. The L1 exception-type normalization is intentional, including the effective-build generated-directory release boundary: after successful capability adoption, a sole close failure is a `TransactionError` at the close stage and its original POSIX error remains inspectable through `.cause` and `.__cause__`; when another failure is active, that typed close error is retained as secondary diagnostic context. The same preservation rule applies at L2 and domain boundaries unless a separately documented and tested public contract requires a raw exception type; domain mappings chain the typed failure rather than bypassing it. No public CLI, cache layout, npm identity, publication format, lock policy, or durability contract changes are intended.

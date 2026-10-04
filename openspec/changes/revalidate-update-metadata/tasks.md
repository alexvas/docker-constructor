## 1. Validated Metadata Envelope

- [ ] 1.0 Require completed `add-durable-filesystem-transactions`, including its primary-preserving cleanup accumulator, and verify metadata persistence uses only its validated read, atomic replacement, durable removal, canonical codec, and cleanup-failure primitives—not build-generation recovery or a broad transaction lock.
- [ ] 1.1 Add RED cache-format tests for request/auth/representation identity, ETag, Last-Modified, body fetch time, validation time, atomic publication, corrupt entries, and unconditional deletion of removed-format TTL entries; verify the focused cache tests fail for the missing envelope behavior.
- [ ] 1.2 Implement the versioned validated metadata envelope and private disk persistence over shared durable-I/O primitives, retaining metadata-owned schema/request/auth validation, independent cache-key scope, path/removal authority, idempotent-absence decisions, and domain error mapping. Use the shared cleanup accumulator for owned descriptor cleanup so ordinary release failures remain secondary to an existing primary, unexpected defects and interruption remain authoritative, and no metadata-specific cleanup accumulator or duplicate atomic-write helper is introduced; verify the focused cache tests pass without weakening existing permission and auth-isolation tests.
- [ ] 1.3 Add RED tests proving each fresh HTTP 200 is acknowledged only after its owning parser accepts it, malformed data preserves the prior validated entry, and multi-request providers acknowledge release metadata and checksum assets independently; verify tests fail against immediate or all-at-once write-through behavior.
- [ ] 1.4 Add the parse-acknowledged publication boundary and verify provider adapters remain independently testable with injected transports.

## 2. Conditional Revalidation and Fallback

- [ ] 2.1 Add RED transport/provider tests for `If-None-Match`, ETag precedence, `If-Modified-Since`, full GET without validators, and `304` fresh provenance; verify every invocation records an upstream request.
- [ ] 2.2 Implement conditional request construction and `304` body reuse with refreshed validation time, and verify the revalidation tests pass.
- [ ] 2.3 Add RED fallback tests covering transport exceptions, timeouts, 408, 429, each 5xx class, malformed 200, absent fallback, and rejection of fallback for 401/403/404 and identity mismatch.
- [ ] 2.4 Implement bounded stale fallback from the last provider-validated envelope for retryable request failures and malformed HTTP 200 provider metadata; verify parser rejection leaves the fresh body uncommitted, records its diagnostic, and remains distinct from artifact/checksum/signature integrity failures, which never use stale substitution.
- [ ] 2.5 Add RED `--no-cache` tests proving unconditional requests, zero cache reads/writes, and no stale fallback, then implement the strict bypass and verify those tests pass.

## 3. Freshness Domain and Reporting

- [ ] 3.1 Add immutable four-state (`fresh`, `stale`, `unavailable`, `not-applicable`) freshness/provenance fields to provider and update result DTO contracts, with validation tests for stale reason/timestamps, fresh `304`, failed HTTP without fallback, skipped-before-request, and `git-ref` semantics.
- [ ] 3.2 Propagate and aggregate provenance through every provider and update coordination; add multi-request GitHub tests proving independent response acknowledgment and `unavailable > stale > fresh` precedence, including fresh release metadata plus stale checksum assets and a required subrequest without fallback, then verify candidate, applicability, ordering, suggestions, JSON policy payload, and exit-code regressions remain unchanged apart from additive freshness fields.
- [ ] 3.3 Add RED compact/detailed rendering tests for stale summary counts and complete `Details:` diagnostics while retaining exact established table columns and status values.
- [ ] 3.4 Implement text stale reporting and structured freshness serialization, and verify fresh-only output contains no stale warning.

## 4. TTL Removal and Validation

- [ ] 4.1 Add RED inventory and CLI tests requiring reviewed `[cache].ttl` to be rejected as an unsupported field while `--cache-ttl` remains unsupported, with no compatibility or migration path; verify local `[cache].dir` alone still resolves the separated `versioning` and `runtime-artifacts/blobs` namespaces.
- [ ] 4.2 Delete TTL policy, transport behavior, and removed-format cache entries through shared durable removal, update inventory examples/documentation, and verify endpoints without validators perform a full request on every run and no metadata-cache journal or global lock is created.
- [ ] 4.3 Run the complete provider, cache, update reporting, readonly behavior, inventory, CLI, and security test suites; verify all pass and an acceptance test demonstrates cached npm `0.84.2` is revalidated to newly published `0.84.3`.

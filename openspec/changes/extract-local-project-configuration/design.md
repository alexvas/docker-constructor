## Context

See `proposal.md` for motivation. The project has two fixed TOML inputs, reviewed `docker-constructor.toml` and local `docker-constructor.local.toml`, whose parsing failures require the same safe fail-fast treatment before their distinct schemas are applied. The current aggregate local-companion requirement is embedded in `runtime-host-access`, while parsing and consumption span inventory, host-access, cache storage, corporate networking, build, run, doctor, and projection construction. The selected project already owns the fixed companion path, and existing behavior requires closed validation before effects, strict reviewed/local separation, private cache roots, and narrow container inputs.

`docker-constructor.local.toml` is read by host-side Constructor orchestration. It is not a runtime projection and must never be passed into a container as an aggregate, although domain capabilities already derive specific Docker arguments, environment values, mounts of separate fixed files, and host paths from validated local state.

## Goals / Non-Goals

**Goals:**

- Establish one shared TOML document boundary for reviewed and local configuration.
- Establish one aggregate owner for companion loading and closed domain-schema composition.
- Preserve every existing accepted value, default, error boundary, cache safety rule, and Docker effect.
- Move field semantics to domain owners without introducing import cycles or duplicate parsers.
- Make host-only confinement explicit while retaining narrowly authorized derived inputs.
- Leave a stable prerequisite boundary for later local tables.

**Non-Goals:**

- Add `[output]` or any other new table or field.
- Change reviewed inventory, cache locations, permissions, host-access behavior, corporate-network behavior, Docker vectors, or effective projections.
- Copy or redesign domain validation inside the aggregate owner.
- Generalize the companion into an open plugin format.

## Decisions

### Share document parsing without sharing document schemas

Introduce one configuration-document boundary parameterized by resolved path and closed document role (`reviewed` or `local`). It performs byte decoding, one `tomllib` parse, typed error projection, and release gating. The reviewed inventory and local companion then apply separate owner-defined schemas. This centralizes behavior common to both files without merging their required/optional lifecycle, accepted fields, defaults, or projections.

Two independent wrappers around `tomllib` were rejected because their error projection and effect ordering could drift. One combined schema was rejected because reviewed portable policy and host-only local state have distinct ownership and publication rules.

### Use one aggregate loader with domain-owned closed schemas

The local-project configuration boundary resolves and parses the single companion once, rejects malformed TOML and unknown top-level tables, and dispatches each recognized table to its domain parser. The recognized set remains exactly `[host-access]`, `[cache]`, `[corporate-trust]`, and `[network.proxy]` for this change. Returned state is immutable and grouped by domain.

Domain parsers continue to own allowed fields, types, defaults, and semantic validation. This avoids both the current runtime-centric ownership and a new god module that understands every domain. Independent parsing of the file by each consumer was rejected because it permits inconsistent unknown-key handling, repeated I/O, and divergent effect ordering.

### Preserve validation as a shared document gate followed by owner schemas

The shared boundary parses each applicable document and retains its role and path. Local aggregate closure/dispatch and all reviewed/local domain validation then complete before any network, cache mutation, artifact materialization, container, or Docker effect. No complete configuration result is released while either applicable document remains invalid.

TOML-path uniqueness and table/value structural consistency are syntax concerns delegated to Python's standard-library `tomllib`, which rejects duplicate definitions before returning a lossy mapping. Constructor does not reimplement that parser logic or exhaustively duplicate standard-library conformance tests. One parser integration test covers representative duplicate handling; a two-role routing matrix proves both fixed documents use the same boundary. The boundary may inspect `TOMLDecodeError` internally but exports only a typed error with closed document role, resolved path, fixed classification, and either an invalid field or parser-provided numeric line/column coordinates. If no field or numeric location exists, only `malformed_toml` remains. Raw parser messages, source lines, tokens, values, excerpts, and the original exception are not publishable diagnostic data and do not leave this boundary.

CLI, JSON, SDK, and logging callers own presentation and may apply additional defense-in-depth redaction, but render only fields from the typed error. Returning raw parser exceptions for caller-side sanitization was rejected because every caller and generic exception path would become a potential disclosure boundary; treating reviewed TOML as safe was rejected because accidentally committed secrets must not be replicated into logs.

Combining reviewed and local domain validation in this boundary was rejected because it would blur schema ownership. Failing only when a consumer first uses one document or table was rejected because effects could begin before another applicable input is found invalid.

### Move cache-root semantics intact to user-cache-storage

`user-cache-storage` becomes authoritative for configured and default root resolution. A configured `[cache].dir` is checked for non-empty absolute form, lexically normalized, and rejected when it is root/home/XDG or an ancestor of XDG. The selected root and every existing constructor cache descendant encountered for preparation or use are inspected no-follow before creation, chmod, mutation, or use and rejected when symlinked, of an unsafe type, foreign-owned, or unsecurable. Existing XDG and `~/.cache` fallback, permissions, ownership, namespaces, children, and failure-before-effects behavior remain unchanged.

`docker-build-reproducibility` continues to own the reviewed/local source split, reviewed `cache.ttl`, projection exclusion, and consumer child-format mapping, but delegates root safety to cache storage. Keeping safety in reproducibility was rejected because the same root secures update, artifact, generated-state, and assembler consumers beyond image reproducibility.

### Leave runtime-host-access with only host-access state

`runtime-host-access` consumes validated local `[host-access]` state and reviewed `[runtime.host-access]` policy. It may produce only the existing authorized mapping, `HOST_ACCESS_ADDRESS`, `HOST_PROXY_PORT`, and launch arguments. It no longer owns cache, corporate-network, or aggregate local-file behavior.

The aggregate companion and its path never enter the runtime projection or container. This does not prohibit domain-owned effects such as derived proxy environment arguments or mounting the separate fixed corporate CA bundle; those are not exposure of the companion.

### Preserve a closed but evolvable registry

The aggregate implementation uses an explicit domain table registry rather than a permissive map. This change registers only the existing tables. A later change may add, for example, `[output]` by modifying the local-project-configuration capability and registering a domain-owned schema; unknown tables remain rejected at every released state.

Hard-coding future `[output]` here was rejected because this prerequisite is behavior-preserving and should not acquire observability scope.

## Risks / Trade-offs

- **[A moved clause is accidentally weakened]** → Map every sentence and scenario from the removed aggregate requirement to a destination delta and require focused parity tests before deletion.
- **[Aggregate and domain validation form an import cycle]** → Keep the aggregate registry dependent on narrow parser protocols and immutable domain results; domain modules do not import orchestration or the aggregate result.
- **[No-follow checks cover the root but miss an existing descendant]** → Keep root-and-descendant inspection in cache storage and prove no mutation or external effect precedes validation of each encountered entry.
- **[Host-only wording blocks legitimate corporate-network behavior]** → Distinguish the companion from separately authorized derived arguments and the fixed project-owned CA bundle.
- **[Concurrent observability work assumes the new owner too early]** → Implement, synchronize, and archive this predecessor before applying `improve-host-build-observability`.

## Migration Plan

1. Add characterization tests around the current aggregate parser, cache safety matrix, defaults, host-access independence, corporate-network independence, projection exclusion, and container boundaries.
2. Introduce immutable aggregate local-project configuration and domain parser registration without changing call sites.
3. Move cache-root resolution and safety ownership behind `user-cache-storage` while retaining compatibility adapters as needed.
4. Migrate facade commands and domain consumers to the aggregate result, preserving validation-before-effects ordering.
5. Remove runtime-host-access ownership and obsolete adapters only after parity tests pass.
6. Synchronize and archive this change before updating or applying `improve-host-build-observability`.

Rollback reverts the ownership extraction as one unit; no file format or persisted-state migration is required because accepted TOML and cache layout do not change.

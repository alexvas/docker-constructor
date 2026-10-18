# Proposal

## Why

Raw descriptor numbers crossing application boundaries make ownership reconstruction expensive and obscure who must release a resource. A small, enforceable ownership language can prove common cleanup structures locally, reserving review and focused tests for exceptional lifecycles instead of requiring a universal cross-application fd event journal.

## What Changes

- Extract an independently installable Python library project at `libs/resource-ownership/`, with distribution name `resource-ownership`, import package `resource_ownership`, its own `pyproject.toml`, `src/` layout, dependencies, build configuration and tests. It contains filesystem owners/factories/operations/borrowing/transfer/cleanup and a separate process-coordination layer; the architecture analyzer stays outside the library.
- Keep library and application in the same Git repository, but give the library an independent environment and unit/fault/contract commands. Build and test its wheel in isolation; the application consumes an installed dependency, optionally editable for local development. No PyPI publication or separate repository is required.
- **BREAKING**: confine raw fd acquisition, extraction, storage, transfer and syscall arguments to the runtime ownership implementation boundary. Raw primitive passing inside that package is allowed; the application escape prohibition applies to its public boundary and consumers, not its internals. Migrate application arguments, return values and fields to owners or non-owning capabilities; remove application reliance on `.fd`, `fileno()` and raw detach results.
- Add owner-returning acquisition factories and bounded descriptor operations, including read, write and stat, so application code needs neither an open-to-adoption gap nor raw syscall access. Keep policy, hashing, recursion, locks, durability sequencing and domain errors in consumers.
- Define five ownership forms: lexical scope, explicit responsibility transfer, managed storage, retained deferred cleanup, and not automatically proven. Distinguish owning from borrowing contracts and reject unclassified escapes.
- Add complete-tree symbol-origin and bounded control-flow checks for this language, plus a generated proof/review report. Inventory runtime internals separately without imposing the application raw-primitive prohibition on them; validate their lifecycle mechanisms with the package's own tests.
- Add executable change-based test selection: implementation-only library edits run its own suites; consumer-only edits run affected consumer suites; public-contract changes also run dependent contract checks; releases still run full integration. Treat uncertain change classifications conservatively. Review-approved cases remain visibly distinct from automatic proofs and are invalidated by relevant source or contract changes.
- Preserve terminal at-most-once release, failure precedence, independent cleanup and process reader-stop guarantees. Do not attempt general termination, arbitrary-thread stopping or proof that OS close succeeds.
- Cancel task 7.2 of `eliminate-unsafe-direct-descriptor-closes`, not mark it implemented. This change replaces its architectural validation direction; it does not promise to deliver the cancelled universal journal. Existing focused ledgers, convergence checks and other Phase 7 tasks remain required.

## Capabilities

### New Capabilities

- `resource-ownership-language`: application ownership forms, borrowing/transfer rules, bounded automatic validation and explicit review status.

### Modified Capabilities

- `descriptor-capabilities`: owner-returning acquisition and operation APIs; raw-descriptor construction/extraction restricted to implementation/test seams rather than application boundaries.

## Impact

Affects `docker/filesystem`, `docker/process`, transaction capabilities and all raw-fd consumers in npm, versioning and the runtime installer; enforcement, API baselines, packaged-runtime imports, developer guidance and executable examples must migrate together. This authorizes internal Python API breakage for ownership representation, not changes to CLI behavior, persistent bytes, paths, identities, modes, domain failures or process budgets.

Existing `docker.filesystem` and `docker.process` implementations move behind `resource_ownership` public entry points; temporary compatibility facades must not re-expose raw primitives. Filesystem mechanisms and process coordination remain separate layers inside that package; filesystem gains no subprocess/reader-thread coordinator or domain policy. The package cannot import application domains or their tests, and consumer tests cannot depend on package internals. Public-contract identity includes signatures, errors, ownership effects and documented behavior, not signatures alone. Fully encapsulated synchronous `subprocess.run` remains library-owned and requires no `OwnedProcess` migration. HTTP/probe and native-file lifecycle changes retain their own implementation scope; integration consumes their public contracts or requests explicit reconciliation, rather than silently absorbing their work.

The earlier close-migration change may finish independently: cancelling its task 7.2 adds no dependency on completion of this change. No implementation is included in this proposal.

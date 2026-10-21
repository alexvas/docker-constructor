# Design

## Context

See proposal.md for motivation. The project uses Python 3.14, unittest-compatible pytest tests and xdist with `--dist loadfile`; the normal full run uses eight worker processes. `docs/testing.md` and `docs/python-checks.md` define prerequisites, skip review and the mandatory full green gate. Design is required because this introduces persistent data and runner integration.

## Goals / Non-Goals

**Goals:** A small development-only command surface, complete declared-profile inventory and reproducible candidate comparisons; schema foundations that B can extend without reidentifying tests.

**Non-Goals:** LLM, coverage, semantic graph, automatic source changes, production runtime integration, scheduling daemon or dashboard.

## Decisions

### Storage and command boundary

Use `.test-catalog/catalog.sqlite3` inside the project, with the entire `.test-catalog/` directory excluded by a future `.gitignore` change. Keep durable domain logic in a development-tool module under `scripts/` with a thin CLI supporting init, collect, measure, compare, report, backup, export and import command families. Exact option spelling can be settled during implementation without changing the behavior contract.

SQLite is the single source of truth, including cards. Avoid duplicate YAML cards and a separate graph database. Use versioned transactional migrations, foreign keys and a bounded busy timeout. The controller is the only writer during a run; workers submit structured pytest reports to it. Other writers serialize or fail clearly. Long-running tests do not hold a write transaction open. Backup uses a consistent database snapshot rather than copying a live database file. Export/import uses a versioned portable representation and validates references and collisions before commit.

SQLite owns its internal connection descriptors; release the connection explicitly, since a transaction context alone is not a close. Do not extract or independently own those internal descriptors. All application-acquired streams/descriptors use the repository filesystem owners; runner/Git subprocesses use OwnedProcess. Integration tests must cover connection release as well as filesystem/process failure paths.

### Minimal relational model

Repository, source-state, file-version, logical-test, test-case, address-history, profile, collection, collection-membership, machine, resource-profile, run and observation are distinct records. Use generated stable internal IDs; nodeids are versioned addresses, not permanent identity. Track unreviewed/relevance/freshness separately, reserving extension space for B without building its graph now.

Source-state includes repository identity, base/current commit, working-tree fingerprint and file hashes; capture relevant untracked source as well as tracked changes. Ignore catalog-generated data in the fingerprint. Store Git blob IDs for committed file versions and observed-at commit for the repository snapshot. Do not enumerate every commit containing a blob. Detect source changes during a run and mark it ineligible for comparison. Use AST coordinates for available Python qualified symbols and explicit file/module fallbacks where no function exists. Ambiguous identity changes remain pending confirmation.

### Collection and runner integration

Declare profiles as command/environment inputs, not machine-dependent hidden defaults. Begin with ordinary suite collection and allow named opt-in profiles. Use pytest collection/report hooks rather than name-based source scanning or parsing terminal duration output. Preserve skipped cases and parametrized logical/case relationships. Collection success is per profile; absence is not deletion. Validate xdist controller/worker report completeness and preserve setup/call/teardown outcomes and the existing green skip policy.

### Measurement protocol

Primary path is repeated measurement of a selected candidate, not nightly whole-suite statistics. Default to one recorded warmup excluded from aggregates followed by five measured repetitions, configurable and stored with the run. Default isolated comparison uses xdist `-n 1 --dist loadfile`; ordinary serial pytest is a distinct profile. Support 1/2/8/16/32 workers without mandatory full matrices in A. Baseline/candidate runs execute sequentially in caller-prepared source states; do not automatically checkout or reset a developer's tree.

Store raw observations and derive sample count, arithmetic mean, median, range and dispersion; mark low sample counts and failures explicitly. No statistical-significance claim is inferred from five runs. Compare only matching machine/resource/toolchain/worker/distribution/instrumentation and selection protocols. Label isolated test improvement separately from suite improvement. Shared preparation is not precisely attributed until C.

### Machine snapshots

A machine has a stable user-provided or locally generated ID. Each run captures immutable allocated CPU cores/quota, CPU model, RAM limit, OS/container context and tool versions. Unknown attributes stay unknown; do not equate missing values with matching environments. One machine may have multiple resource profiles.

## Risks / Trade-offs

- [Noisy timing] → Preserve repetitions and dispersion; do not pool incompatible contexts or claim causality from one result.
- [Incomplete inventory] → Report profile coverage and collection failures, never infer deletion from absence alone.
- [Ignored database loss] → Provide verified backup/restore and portable export from A; it is not merely disposable cache.
- [Runner integration alters behavior] → Exercise unittest, parametrization, skips and xdist under the unchanged full gate.
- [Identity heuristics mismerge] → Prefer explicit unresolved identity over silent matching.

## Migration Plan

Create storage only on explicit initialization; do not create a DB during ordinary pytest or image runtime. Make migrations transactional and back up existing data before upgrades. Reject newer schema versions rather than downgrade destructively. Roll back by restoring the matching backup/tool version. Document first collection, candidate measurement and restore workflows before accepting A. B implementation is gated on accepted A and round-trip compatibility tests.

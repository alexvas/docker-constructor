# Design

## Context

See proposal.md for motivation. `tests/build_test_support.py` currently copies the real reviewed inventory into its supposedly hermetic temporary directory. `TestRunTransaction._make_fixture_toml()` copies the same file and appends a `pi-read` artifact table: after the extension was removed this creates an incomplete extension and a schema failure inherited by multiple launcher classes. Runtime-schema tests also read and modify the canonical TOML. Other failures pin current release metadata, runtime extension names, or progress totals.

The production configuration-validation boundary remains authoritative: tests must still traverse real TOML parsing and document-specific validation before effects. The dependency-bump commit changed only `docker-constructor.toml`; do not compensate by restoring removed dependencies or loosening production validation.

## Goals / Non-Goals

**Goals:** Preserve exact behavioral and negative-path coverage while making dependency versions and optional extension membership independent test inputs. Retain explicit smoke/contract coverage for the actual project inventory. Make fixture ownership clear and detect future accidental coupling.

**Non-Goals:** No production refactor, schema/API changes, version rollback, network-based release verification, wholesale deletion of inherited tests, blanket replacement of exact assertions with self-derived expectations, or new test framework/dependencies.

## Decisions

### 1. Two explicit test lanes

Behavioral tests use committed test-owned TOML under `tests/fixtures/` and a lightweight helper under `tests/`. Live-inventory tests deliberately read the repository's configuration and assert structural and cross-field invariants, not changing version literals or optional package membership. Classify every affected canonical read and shared-helper consumer, not just the currently failing methods.

Alternative: update hardcoded expectations after each bump. Rejected because it preserves coupling. Removing canonical coverage entirely is also rejected.

### 2. Stable baseline plus small named scenario fixtures

Provide a complete stable baseline valid under the current production schema, with fixed tool/source metadata and extensions exercising the existing launcher scenarios. Test-only `pi-read` and `pi-usage` names may remain in fixtures to preserve scenario diagnostics; their presence does not imply installation in the real project. Include default and alternate artifact versions, rejected neighboring versions, and deterministic bytes/digests. A minimal scenario means minimal for the tested production path, not bypassing mandatory schema fields.

Use helpers to create fresh per-test documents in temporary constructor-project directories, optionally with a minimal Dockerfile and local companion. Keep the existing `INVENTORY_PATH`/`DOCKERFILE_PATH` helper interface where safe to avoid unrelated churn, but its source becomes a committed fixture. Builders are simple test-data assemblers, not schema validators or replicas of production algorithms. Do not add a TOML dependency: small explicit templates/snippets with well-defined named inputs are sufficient. Never rewrite a live-inventory string with regex as a fixture baseline; controlled malformed snippets must assert the intended field was actually introduced.

Every positive fixture passes the real loader. Negative fixtures start from that valid baseline and introduce exactly the intended defect. Artifact integrity derives from local fixture bytes with standard-library hashing and base64, and transport mocks expose unexpected requests rather than fetching the network.

Alternative: construct only production DTOs. Rejected for integration tests because it bypasses document validation.

### 3. Preserve independent assertion oracles

Artifact-selection tests assert exact fixture URLs, ordering, platforms, and digests. Pi release tests assert fixed fixture source/package/repository/tag/provider behavior. Effective-state and environment tests assert fixed fixture values and extension mappings. Progress tests use a known target fixture and retain exact counts, target order, clearing, interruption, and diagnostic ordering.

Live-inventory tests validate successful loading, selected-artifact/platform consistency, Pi source contract, and propagation to effective state/environment using raw declared inputs as the oracle where appropriate. Do not compute expected output using the same selector/renderer under test. Current reviewed versions are not a universal policy; mandatory stage/schema rules remain enforced. Live visual-header tests enumerate declared owners independently from raw TOML, while fixed-fixture tests retain known owner-to-display mapping examples and negative header cases. Optional extension membership is not hardcoded in the live lane.

### 4. Coverage migration ledger and independence gate

During implementation write `verification.md` in the change directory, separate from proposal/design/tasks. Record each failed test or inherited family, its original assertion intent, its new fixture/live lane, and commands/results. No skip, expectedFailure, removed negative case, or reduced assertion is acceptable as a repair.

Add a narrow executable audit of migrated behavioral fixture sources rejecting repository-inventory reads/copies, with an explicit allowlist for named live-contract tests. Avoid an unrestricted regex that forbids fixture filenames or legitimate real-inventory checks. Add a subprocess regression that denies reads of the resolved repository `docker-constructor.toml` while loading test helpers and running representative migrated behavioral suites; importing fixtures must also be covered. This must not modify the repository configuration. Demonstrate independent fixture construction and mutation using a second test-owned inventory with changed version metadata and removed optional extensions; real-contract tests accept valid variants and still reject deliberately inconsistent ones. Assert these regressions fail against the original coupling.

Alternative: temporarily rewrite the repository inventory. Rejected due to destructive interference and parallel-run risk.

## Risks / Trade-offs

- [Shared support migration affects many passing tests] → Audit all consumers; migrate remaining behavioral assumptions coherently and run full discovery.
- [Stable fixtures drift from schema] → Validate every positive fixture through production loaders; intentional future schema changes can require fixture updates, dependency bumps should not.
- [Dynamic live expectations mask regressions] → Keep exact independent fixture assertions and negative cases; live checks compare distinct document stages rather than output to itself.
- [Removing extensions exposes unrelated fixture fragility] → Preserve full negative integrity/algorithm and pre-effect scenarios, not just successful launcher setup.
- [Inherited methods inflate failure counts] → Inventory fully qualified discovered cases and map inherited families explicitly; do not infer coverage preservation from totals alone.

## Migration Plan

1. Capture baseline failures and classify canonical reads, shared-helper consumers, and intended assertions.
2. Add stable fixtures, fresh-document helpers, and real-loader self-tests before migrating consumers.
3. Migrate launcher/build support and runtime schema tests, then exact artifact/Pi/progress/effective/visual tests.
4. Establish explicit live-inventory contract coverage and independence/negative regression gates.
5. Run focused suites, full discovery, static boundary audit, whitespace validation, and OpenSpec validation; store evidence separately.

Rollback is limited to reverting test-only implementation changes; the real inventory and production contracts are untouched. Any newly discovered production defect blocks completion and is reported for a separate authorized change.

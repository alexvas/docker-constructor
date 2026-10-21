# Tasks

## 1. Storage and identity foundation

- [ ] 1.1 Add development CLI/module boundaries and project-local SQLite initialization with ignored database/sidecars; verify ordinary pytest does not initialize storage and document the init command.
- [ ] 1.2 Implement transactional migrations, foreign keys and bounded concurrent-writer handling; verify rollback, newer-schema rejection and concurrent update tests; document upgrade behavior.
- [ ] 1.3 Implement consistent backup and versioned export/import; verify round-trip IDs, observations, invalid-reference rejection and failed-restore preservation; document recovery commands.
- [ ] 1.4 Implement stable logical-test/case IDs, address history and independent review/relevance/freshness fields; verify parametrization, unknown states and ambiguous-rename confirmation tests.
- [ ] 1.5 Implement committed/dirty source snapshots, Git blob addresses and AST symbol/range coordinates; verify untracked sources, nested/class/module code and source-changing-during-run tests; document provenance limits.
- [ ] 1.6 For storage/export paths, name DirectoryDescriptor for directories, OwnedDescriptor for acquired raw descriptors and OwnedStream for acquired fd-owning streams; explicitly close SQLite connections without owning internal fds; verify operation-plus-close, sole-close, interruption, repeat and independent cleanup plus applicable handoff/concurrency tests.

## 2. Complete collection and observations

- [ ] 2.1 Add named profile-aware pytest collection and controller-side persistence; verify unittest, skipped cases, parametrization, failed collection and cross-profile absence without false deletion; document inventory completeness.
- [ ] 2.2 Collect structured setup/call/teardown outcomes, warnings and worker completion; verify serial and xdist reports, lost-worker/incomplete-run behavior and unchanged green skip-policy handling.
- [ ] 2.3 Launch pytest and Git through OwnedProcess with launch-configured StopPolicy and shared shutdown budget; verify operation/cleanup failure, interruption, retained incomplete cleanup and repeated observation without independent wait/reap/close.
- [ ] 2.4 Add immutable machine/resource/toolchain profiles with allocated CPU and RAM limits; verify allocation changes and unknown metadata cannot silently pool samples; document machine-ID configuration.

## 3. Candidate measurement and reporting

- [ ] 3.1 Implement configurable warmup/repetition and sequential baseline/candidate measurement for test/file/set selections; verify excluded warmup, five-repeat default, dirty-state binding and preserved runner failures; document the reproducible workflow.
- [ ] 3.2 Support distinct serial and xdist modes including 1/2/8/16/32 workers; verify distribution/instrumentation/selection profiles remain distinct and source trees are never automatically reset or staged.
- [ ] 3.3 Implement mean/median/dispersion, sample counts and absolute/relative comparison with incompatibility/insufficient-data reporting; verify deterministic aggregate fixtures and rejected mismatched comparisons.
- [ ] 3.4 Add phase/total test and file rankings plus exportable reports; verify optimized tests retain history outside top-100 and reports distinguish isolated from suite results; document interpretation limits.

## 4. Integrated acceptance

- [ ] 4.1 Demonstrate full ordinary-profile inventory and repeated candidate comparison across two source states with backup/restore; preserve a self-contained acceptance summary including fingerprints, environment, commands, counts, warnings and actual statuses separately from specs.
- [ ] 4.2 Run scripts/check-green after each cohesive implementation phase and on the final candidate, capturing complete temporary output and actual status; require G and review all skips/xfails before declaring A accepted for B.

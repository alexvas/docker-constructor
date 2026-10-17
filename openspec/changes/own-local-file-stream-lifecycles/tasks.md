# Tasks

All tasks start unchecked. The staged proxy patch and prior green logs are historical baseline, not accepted implementation of this change. Work proceeds RED → GREEN → INTROSPECT → VALIDATE. Do not recreate already-fixed precedence failures merely to obtain RED; demonstrate RED for the unmet direct-target, preparation and complete-tree proof contracts.

The sole release owner for acquired file objects is their actual `OwnedStream`, not a proxy or a second integer-fd owner. Raw-fd `fdopen`, subprocess and delegated network lifecycles retain their separate owners and contracts.

## 1. RED — Characterization, acquisition and ownership proof

- [ ] 1.1 Generate an initial complete-production-tree local-file candidate inventory covering builtin/`io.open`/`Path.open`, aliases, defaults, keywords and native contexts; identify convenience/wrapper candidates and classify scope before migration. Verify the generator includes a newly added module and fails on an unresolved resource origin, recording findings separately from specs.
- [ ] 1.2 Pin all five named readers' successful bytes/results and open/read/parse/sole-close domain mappings, encoding, modes and digest behavior. Verify staged passing cases remain passing and record baseline logs without accepting proxy adoption.
- [ ] 1.3 Add RED tests for `OwnedStream.acquire(opener, *, label)`: invalid inputs, lock/storage preparation faults before factory execution, factory failure with no resource, exact returned-target adoption before inspection, and transfer/setup failure accounting. Inject ordinary, unexpected and control-flow faults; prove every actual returned stream has one prepared owner and no self-cleaning transfer mock.
- [ ] 1.4 Add RED actual-target lifecycle tests for operation-plus-close, sole-close, first/later interruption identity, repeat/reentrant/concurrent cleanup, independent sibling release and winning-close outcome preservation. Include real files and deterministic barriers; verify no proxy, native-context or raw-fileno release and no supported worker survives test teardown.
- [ ] 1.5 Add RED complete-tree mutation fixtures for aliased/assigned factories and closers, context bindings, delayed transfer, proxy adoption, native double release, duplicate owners, reset-per-cleanup ownership and lost outcomes. Require identical rejection in new modules and positive acceptance of the directly acquired file with its one terminal owner.

## 2. GREEN — Prepared direct stream acquisition

- [ ] 2.1 Implement the design-specified bounded `OwnedStream.acquire` API with validated preparation before factory invocation and direct binding of the actual returned stream; verify preparation/acquisition/direct-target tests from 1.3 pass without a close proxy, new domain policy or consumer private-helper calls.
- [ ] 2.2 Protect every post-acquisition validation/transfer failure through the same prepared authority and shared precedence, distinguish empty factory failure from acquired ownership, and preserve at-most-once close; verify 1.3–1.4 fault and concurrency matrices pass with exact selected/secondary identities.
- [ ] 2.3 Run existing already-open constructor rejection, process preparation/transfer, real-pipe reader-safety and retained-cleanup suites; require no changed constructor signature, stolen caller stream, process-private authority expansion or altered default process behavior, preserving full logs/statuses.
- [ ] 2.4 Document acquired versus already-open usage, caller factory responsibilities, preparation/rejection boundaries, actual-target ownership and concurrent terminal-claim limitations in the lifecycle guide. Execute examples and verify exact API/import assertions permit only the declared additive entry point.

## 3. GREEN — Local-file readers and image layout

- [ ] 3.1 Migrate `runtime_installer.read_projection` to direct acquired-stream ownership and remove native/proxy release authority; verify successful sorted entries, missing/parse/schema/integrity mappings and the full operation/close/interruption/repeat matrix remain correct.
- [ ] 3.2 Migrate `InstallContext.real_metadata().read` through the same acquired-stream API; verify missing/permission/read/JSON distinctions, exact cause relationships, object validation and the full lifecycle matrix without domain policy in filesystem.
- [ ] 3.3 Migrate `DiskCache.get` through direct ownership; verify ordinary cache-miss and sole-close behavior, successful response fields/text encoding, retained ordinary read authority and interruption propagation under the lifecycle matrix.
- [ ] 3.4 Migrate `_verify_hit` through direct ownership; verify existing stat/mode/hash/miss behavior and lifecycle precedence with real files and injected faults, without altering permissions, identities or publication.
- [ ] 3.5 Migrate `_load_build_projection` through direct ownership; verify raw open/parse failure behavior, loaded TOML values and exact interruption/ordinary-close authority in the lifecycle matrix.
- [ ] 3.6 Characterize and migrate every additional in-scope local-file site found by 1.1 using the same RED-to-GREEN fault discipline; verify inventory/ledger equality leaves no unresolved in-scope site. Pause for a decision on any newly identified nonmatching resource family rather than adding a name-based exemption.
- [ ] 3.7 Remove `_FileRelease` and proxy-specific structural assertions; remove or reduce `_owned_file.py` to owner-only composition and reconcile its Dockerfile copy accordingly. Verify no delegated close proxy remains, valid behavior regressions still pass, documentation names the resulting API, and isolated runtime-image imports succeed outside the repository with scrubbed `PYTHONPATH`.

## 4. INTROSPECT — Complete-tree convergence and ledger evidence

- [ ] 4.1 Implement the complete-tree local-file convergence gate under `tests/enforcement/` and make all 1.5 mutations pass; verify direct-target/preparation/terminal-release dataflow rather than class/import spelling, and require proof for every unresolved acquisition/release origin.
- [ ] 4.2 Add an executable repository local-file ledger tied to every in-scope static inventory row and actual production factory returns, transfers, borrowed uses and closes. Verify it detects missing/delayed adoption, implicit or double release, duplicate raw-fileno ownership, leaks, retries, skipped independent cleanup and lost concurrent outcomes; demonstrate positive real-file and failure paths.
- [ ] 4.3 Generate API/signature, module DAG, source-hash and runtime-packaging evidence; verify the exact additive stream surface, unchanged domain interfaces, no aggregate/reverse imports or process/domain policy in filesystem, and preserve generated-inventory equality checks.
- [ ] 4.4 Create this change's `verification.md` with every in-scope production acquisition/release classification, per-site sole owner, ledger and mutation evidence, no-drift comparisons and current logs/statuses. Explicitly distinguish delegated network sites and original raw-fd/process Phase 7 work without claiming either is proved by local-file coverage.
- [ ] 4.5 Run executable documentation/link and developer-guidance checks against the direct ownership implementation; verify no example authorizes proxy adoption, rejected-stream close, raw-fileno ownership or native double release, and cross-links preserve independent acceptance of both changes.

## 5. VALIDATE — Fresh integration and release checks

- [ ] 5.1 Run focused local-file fault/real-file/convergence/ledger, runtime-reader, cache, materialization, verification, image-layout, filesystem and process regression suites; record full logs/statuses and require no drift beyond the approved lifecycle corrections.
- [ ] 5.2 Run `.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q`, preserve complete stdout/stderr and runner exit status, and record totals/skips/warnings/duration in `verification.md`; require success without excluding failures or suppressing warnings.
- [ ] 5.3 Run `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'`, preserve complete stdout/stderr and runner exit status, and record totals/skips/duration in `verification.md`; require passing discovery independently of pytest.
- [ ] 5.4 Run `ty check docker --python-version 3.14 --output-format concise` and `git diff --check`; preserve full output/statuses and record passing results in `verification.md`.
- [ ] 5.5 Strictly validate this change and `eliminate-unsafe-direct-descriptor-closes`, verify all new requirements have current executable evidence, and record acceptance only for this change's delivered scope; do not mark original Phase 7 tasks complete from this result.

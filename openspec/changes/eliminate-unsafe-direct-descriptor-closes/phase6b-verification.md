# Phase 6B verification — subprocess stream ownership and developer rules

## Revised 6B-B evidence

Scope: generic process ownership only; consumer adapters, integrated 6B-D and
Phase 7 remain pending. No historical subprocess acceptance is relabeled.

6BB.1–6BB.5 RED: new generic launch/fault/retention/real-pipe contracts ran
before production changes. Command:
`.venv/bin/python -m pytest tests/test_process_lifecycle.py -n 8 --dist loadfile --tb=short -q`.
Complete stdout/stderr `/tmp/docker-constructor-6bb-red.HBP659.log`; exit 1:
64 errors in 1.05s, all because `docker.process` did not yet exist. Existing
consumer characterization remains in historical evidence below; these new
real-pipe cases are unmet generic contracts, not claimed passing baseline.

6BB.6–6BB.10 GREEN: private preallocated stream slots and no-allocation escrow
binding share OwnedStream's release state. A prestarted per-process continuation
owns shutdown and remains responsible after incomplete return. Internal
nonblocking descriptor readers have explicit stop barriers. Independent joins,
reaping and safe releases feed shared precedence; observation retains outcomes
without re-adoption, retry or mutation of delivered diagnostics.
Initial test run `/tmp/docker-constructor-6bb-green.WQ0pqI.log` exited 1
(63 passed, one failed): the second coordinator lock preparation seam was
missing. Added the operation coordination lock before launch, then the combined
owner/foundation gate passed: `/tmp/docker-constructor-6bb-check.yHskTs.log`,
exit 0, 314 passed and 1765 subtests passed in 4.01s.
The accompanying initial typecheck failed on classmethod-initialized attribute
inference; explicit attribute types corrected that failure (no suppression).

Expanded fault verification, same generic pytest command:
`/tmp/docker-constructor-6bb-faults.n19PXS.log`, exit 0:
80 passed in 1.42s. Includes genuine underlying stream-lock faults, start-then-
raise tracking, absent streams, both ordinary close failures, blocked winning
close, descriptor-less close-to-unblock, operation defects, no late callbacks,
retained real-descriptor-like reader barriers, eventual/controlled worker stop,
failed-launch/context observation and concurrent/reentrant cleanup.

6BB.11–6BB.12 documentation/introspection: `docs/process-lifecycle.md` exposes
only launch, context, bounded capture/byte streaming, shutdown and observation;
filesystem/assembler guides cross-link without claiming adapter migration.
Three examples execute in isolated subprocesses. Structural tests prove the
prepared pair, nonallocating binding, acquired/transferred distinction, stop
barrier, terminal attempts, reaped-child completion and retained continuation.
Counterexamples cover lost escrow, lost acquisition tracking, constructor
replacement, false completion, removed reader barriers/continuation, aliases
and counterfeit owner names/imports. Isolated import loads only process and
filesystem cleanup/streams, no domain or reverse dependencies.
An initial introspection run failed (9 failed, 336 passed, 1765 subtests;
`/tmp/docker-constructor-6bb-introspect.staiQF.log`, exit 1) because AST argument
nodes have no line number. The proof now restricts line inspection to call
nodes; no ownership assertion was removed. API/evasion/example gate:
`/tmp/docker-constructor-6bb-api.nnA22r.log`, exit 0, 18 passed in 1.09s.

Final review additionally preserves explicit action classifications when
merging/snapshotting the ledger (join OSError is a defect, not ordinary release
failure), treats terminate-grace expiry as the normal transition to kill,
installs an active interruption during a concurrent undelivered shutdown,
retains first late reap-observation defects, and uses the remaining observation
budget to join the continuation. Supervision cannot start before transfer/setup;
a factory returning the same stream twice gets one owner/release and a controlled
setup defect. All have regression coverage; no adapter was changed.

6BB.13 final acceptance (all commands preserve complete stdout/stderr):

| Check | Complete log | Runner status / result |
| --- | --- | --- |
| Generic process/fault/real-pipe/API/examples plus 6B-A gate | `/tmp/docker-constructor-6bb-focused.dUWvDt.log` | 0; 354 passed, 1765 subtests, 4.05s |
| `ty check docker --python-version 3.14 --output-format concise` | `/tmp/docker-constructor-6bb-typecheck.Vk6QcO.log` | 0; All checks passed |
| `.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q` | `/tmp/docker-constructor-pytest.Ee3ayP.log` | 0; 6608 passed, 13 skipped, 11 warnings, 17775 subtests, 32.41s |
| `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'` (configured project check) | `/tmp/docker-constructor-6bb-unittest.ViMQZP.log` | 0; 6405 tests, 13 skipped, 148.970s; OK |
| `git diff --check` | no output | 0; clean |

Focused command:
`.venv/bin/python -m pytest tests/test_process_lifecycle.py tests/test_process_contract.py tests/test_filesystem*.py tests/test_descriptor_close_convergence.py -n 8 --dist loadfile --tb=short -q`.
The final typecheck first failed with 3 missing `_duplicate_pipe` attribute
annotations (`/tmp/docker-constructor-6bb-typecheck.YAT2qY.log`, exit 1);
explicitly declaring that classmethod-prepared field fixed the issue.
A preliminary unittest attempt exceeded the harness's 120-second limit:
`/tmp/docker-constructor-6bb-unittest.Mo72GY.log`. It is unsuccessful/incomplete;
no runner exit status was captured after harness interruption. Rerunning with a
300-second allowance completed successfully (including the final run above).
Intermediate passing regression logs remain `/tmp/docker-constructor-pytest.5APfqH.log`
and `/tmp/docker-constructor-pytest.VuvVo8.log`; final acceptance is the later
log in the table. Fork-deprecation warnings are retained, not suppressed.

Generic incomplete-state tests control and release every stalled worker. A
never-stopping reader remains retained, not falsely complete; arbitrary injected
blocking reads/closes have no forced completion guarantee. Delivered snapshots
and authoritative exceptions are unchanged by late close/reader/reap outcomes.
This closes 6B-B only. Consumer signatures/output/removal order are untouched;
6B-C delegation and 6B-D integrated convergence remain pending.

## Revised 6B-A evidence

6BA.2 RED (before production edits):
`.venv/bin/python -m pytest tests/test_filesystem_owned_streams.py -n 8 --dist loadfile --tb=short -q`.
Complete stdout/stderr: `/tmp/docker-constructor-6ba-red.jtCkS3.log`; exit 1:
9 failed, 28 passed, 21 subtests passed. Invalid labels mutated/closed caller
streams, including candidates with close defects/interruptions; validation
also ran after lock preparation. Genuine lock-construction failures preserved
identity and left candidates untouched. Existing lifecycle tests remain.

6BA.3 GREEN: same owned-stream command; complete log
`/tmp/docker-constructor-6ba-green.ye8xB6.log`, exit 0:
28 passed, 30 subtests passed in 0.53s. Constructor validation and lock
preparation precede binding; rejected streams are not released. The existing
claim-before-close/outside-lock implementation and constructor remain bounded.

6BA.4–6BA.6 focused acceptance:
`.venv/bin/python -m pytest tests/test_filesystem*.py tests/test_descriptor_close_convergence.py -n 8 --dist loadfile --tb=short -q`.
Complete log `/tmp/docker-constructor-6ba-focused.PDSCDW.log`, exit 0:
250 passed, 1765 subtests passed in 3.96s. This includes executable stream
acceptance/rejection examples, unchanged raw-fd/directory/file-wrapper
examples, exact public surface, AST preparation-before-transfer/no-constructor-
close checks, synchronized terminal release proofs, empty aggregate initializer,
isolated import matrix and violating ownership fixtures.

Additional regression verification:
`.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q`.
Complete log `/tmp/docker-constructor-pytest.QW82dw.log`, exit 0:
6504 passed, 13 skipped, 11 warnings, 17775 subtests passed in 33.38s.
Warnings remain visible in the complete log (multithreaded fork deprecations).
`ty check docker --python-version 3.14 --output-format concise`: exit 0,
“All checks passed!”; `git diff --check`: exit 0, no output.

Scope is 6B-A only: no process owner or adapter implementation was undertaken.
Passing legacy self-cleaning adoption tests are not revised process acceptance;
their replacement remains pending in 6BC.2. No claim is made that old
post-launch ownership or join-timeout defects are repaired by prevalidation.

The results below are historical original-6B evidence, not acceptance of the
revised process architecture.

Scope: tasks 6B.1–6B.17 of change `eliminate-unsafe-direct-descriptor-closes`.
This file records the pre-change inventory/baseline and the post-change
focused verification evidence. It is not part of any task checkbox and does
not replace the Phase 7 release gates.

## 6B.1 Inventory and passing baseline (recorded before migration)

### Subprocess stream release inventory

| Site | Release | Origin |
| --- | --- | --- |
| `docker/npm_environment/execution.py::_close_stream_pipes` | `pipe.close()` | `Popen(..., PIPE)` stdout/stderr |
| `docker/npm_environment/execution.py::SubprocessExecutor.run_streaming` | `failed_pipe.close()` and five `_close_stream_pipes((stdout_pipe, stderr_pipe))` call sites | launched client pipes |
| `docker/npm_environment/lifecycle.py::close_descriptors` | `pipe.close()` | reaping / captured lifecycle |
| `docker/npm_environment/lifecycle.py::reap_process` | `close_descriptors(pipes)` | caller-supplied pipes |
| `docker/npm_environment/lifecycle.py::terminate_and_reap` | `reap_process(...)` | caller-supplied pipes |
| `docker/npm_environment/lifecycle.py::run_captured` | `close_descriptors((proc.stdout, proc.stderr))` | captured client pipes |

Baseline defect recorded by `tests/test_npm_environment_phase3_deadline.py`:
`test_reader_failure_closes_both_pipes_when_client_survives_kill` asserted
`proc.stdout.close_attempts >= 2` (idempotent repeat close). The required
replacement is a terminal exactly-once contract.

### Characterized consumer surface

Pinned passing (unchanged by migration):

- `DockerRunExecutor.run_streaming` keyword surface: `argv`, `secrets`,
  `sink`, `deadline_seconds`, `container_name`, `grace_seconds`,
  `cleanup_outcome`, `stream_factory`, `tail_projector`,
  `network_url_display`, `on_launched`.
- `lifecycle.close_descriptors(pipes)`, `reap_process(proc, *, policy, pipes)`,
  `terminate_and_reap(proc, *, policy, pipes)`, `run_captured(argv, *, policy)`
  signatures and list/`LifecycleOutcome` interfaces.
- Descriptor-less injected streams (no `fileno()`) via the blocking `read`
  compatibility seam; missing/`None` pipe handling.
- `ProcessResult` shape, stdout/stderr decoding and bounded tails, terminate →
  remove → reap → close order, deadline/grace bounds, worker shutdown, and
  bounded URL/secret redaction.

Pre-change focused baseline:
`.venv/bin/python -m pytest tests/test_npm_environment_execution.py
tests/test_npm_environment_phase3_deadline.py
tests/test_npm_environment_phase4_lifecycle.py
tests/test_npm_environment_streaming.py tests/test_filesystem_owned_streams.py`
→ exit 0; **206 passed, 386 subtests passed**. Log:
`/tmp/docker-constructor-phase6b-baseline.31qRwt.log`.

## 6B.6/6B.2 Bounded OwnedStream

`docker/filesystem/streams.py` adds only `OwnedStream` (plus the private
`__all__`). It acquires an already-open fd-owning stream and validates it: a
candidate with a callable `close()` but an invalid label is released through
the owner's own synchronized internal release before the label is rejected, so
a failed adoption never leaves an acquired stream without a release authority;
a candidate with no callable `close()` has no release authority and is left
untouched. It exposes borrowed live stream access (`stream`), terminal-state
inspection (`label`, `released`), `close()`, and context-manager cleanup.
`close()` takes a synchronized live-to-release-claimed transition under a
private lock and invokes the underlying close outside that lock, so
reentrant/concurrent/repeated cleanup issues exactly one underlying close and
never deadlocks. `__exit__` uses `CleanupFailures` for shared precedence. No
raw-fd accessor, detach/retry, proxy API, subprocess/reader/deadline policy,
or aggregate re-export exists.

## 6B.7–6B.10 Subprocess migration

- `run_streaming` adopts `proc.stdout`/`proc.stderr` into one `OwnedStream`
  each immediately after launch and reuses the same pair across
  reader-failure, deadline, interruption, reaping, and finalization.
- Stream adoption, `_make_streaming_reader()`, supervisor construction/start,
  and the launch callback all sit inside one protected post-launch setup; on
  failure the started supervisor is stopped/joined, each stream is released
  once, and the child is boundedly reaped.
- `_adopt_stream_owners` attempts every acquired stream exactly once; a
  successful adoption yields an owner, and a rejected adoption is released by
  the foundation owner itself (`OwnedStream` acquires before it validates).
  The adopted prefix then releases through its owners and the child is
  boundedly reaped, so a first failure cannot strand the sibling and no
  domain-layer direct close is required.
- `lifecycle.close_descriptors` now accepts `OwnedStream` owners; `reap_process`,
  `terminate_and_reap`, and `run_captured` thread one owner set through all
  cleanup paths. `run_captured` adopts before descriptor configuration and
  protects descriptor configuration, thread construction, and every
  `reader.start()`, tracking exactly which readers started.
- Real descriptor pipes are read with cancellation-aware `select`/`os.read`;
  once the client has exited, at most `grace_seconds` of no-data time elapses
  before EOF, so a descendant retaining a write end cannot block shutdown.
  Descriptor-less injected streams keep the blocking `read` seam.
- Ordinary reader/wait/domain failures remain primary over ordinary close
  failures; independent owners are each released once.

## Post-review hardening (setup protection and cleanup precedence)

This section records the follow-up hardening applied to the subprocess stream
boundary. Specification text is unchanged; the public consumer surface is
unchanged.

1. **Adoption-failure accounting.** `execution._adopt_stream_owners` and
   `lifecycle.run_captured` attempt every acquired stream exactly once and
   release the adopted prefix through its owners before reaping the child.
   A failed `OwnedStream` construction is release-safe inside the filesystem
   foundation: `OwnedStream` acquires the candidate before it validates, so a
   rejected acquisition is released through the owner's own synchronized
   internal release. The temporary `_release_acquired_stream` /
   `_release_acquired_pipe` adoption-boundary helpers and their
   convergence-test exemption were removed; the semantic contract now permits
   only the existing three direct-close categories.
2. **Whole-setup protection.** Every fallible operation after `Popen` in
   `run_streaming` (adoption, reader construction, supervisor construction and
   start, and the launch callback) shares `_finalize_setup_failure`, which
   stops/joins started workers before releasing streams and reaping. In
   `run_captured`, adoption precedes descriptor configuration and the setup
   path protects descriptor configuration, thread construction, and every
   `reader.start()`, joining descriptor-backed readers before releasing their
   streams and again after (for descriptor-less readers) before reaping.
3. **Cleanup precedence.** `_close_stream_owners`, `_close_blocking_stream_owners`,
   and `lifecycle.close_descriptors` attempt every owner (including after an
   interruption or unexpected defect) and classify failures with
   `CleanupFailures` using an explicit `ordinary=(OSError,)` policy.
   `_raise_close_authority` now installs the active operation failure in a
   `CleanupFailures` accumulator instead of selecting a close failure
   independently, so an existing operation `KeyboardInterrupt` keeps its
   identity over a later close defect or interruption.
4. **Reaping before close authority.** Both streaming finalization paths reap
   a still-live client before a close defect or interruption can take
   authority: the `collect_streams` failure path calls `_reap_if_live` before
   `_raise_close_authority`, and the normal path defers `_raise_close_authority`
   until after `proc.wait()`/the supervisor has established the return code.
5. **Captured-client setup precedence.** `_finish_captured_setup_failure`
   orders the returned `LifecycleOutcome.errors` with shared cleanup
   precedence (interruption, then unexpected defect, then the setup failure,
   then ordinary close failures) instead of always placing the setup error
   first, while still attempting every sibling release and the bounded
   terminate/reap through one accumulator and preserving the outcome
   interface and bounded diagnostics.

### Convergence follow-through

The stricter cleanup pattern required the Phase 6B gate to verify bound close
callables, not only direct `.close()` calls. `tests/test_descriptor_close_convergence.py`
now:

- inspects `failures.run(<owner>.close, ...)` and rejects a non-owner receiver,
  with fixtures for the accepted owner form and the rejected consumer form;
- recognizes owner iterables composed of `reversed(<owner sequence>)`, a
  tuple/list of owners, and a local variable annotated with a shared-owner
  type, closing three pre-existing owner-tracking gaps (`build_cache.py`); and
- no longer grants any adoption-boundary direct-close exemption; the removed
  helpers and the `_unadopted_stream_release_proof` allowance are deleted, so
  `docker/npm_environment/execution.py` and
  `docker/npm_environment/lifecycle.py` contain no unowned stream close.

`tests/test_transactions_l1_error_boundary.py` records the domain-neutral
`docker/npm_environment/lifecycle.py::pipe.close` release in
`RAW_CLOSE_ACTIONS`: it holds subprocess `OwnedStream` owners that keep the raw
`OSError` contract and never opens a transaction capability.

### New regression coverage

- Adoption: first- and second-stream adoption failures account for both
  streams exactly once and reap the child (`execution`), and first-/second-pipe
  adoption failures do the same for the captured client (`lifecycle`).
- Reader setup: first- and second-reader construction failures
  (`run_streaming`) and first-/second-reader start failures (`run_captured`,
  including a real-pipe partial-startup case) leave no surviving supported
  worker and no leaked stream.
- Cleanup precedence: an operation `KeyboardInterrupt` survives an ordinary
  close failure, an unexpected close defect, and a close interruption, in
  either stream position, preserving its identity; a close interruption
  outranks an ordinary operation failure.
- Reaping before authority: a collection setup failure and a normal-path
  close defect both reap a live client before the defect is raised.
- Captured setup precedence: a setup failure combined with an ordinary close
  failure keeps the setup failure primary, while an unexpected close defect or
  a close interruption takes authority with the setup failure retained; both
  streams release exactly once, the child is reaped, and no supported reader
  survives.
- Convergence: the Phase 6B gate scans `failures.run(<owner>.close, ...)` bound
  releases, recognizes reversed/tuple/annotated owner iterables, and grants no
  adoption-boundary exemption.

## 6B.11–6B.13 Developer rules

- `docs/filesystem-lifecycle.md`: wrapper-selection/ownership checklist,
  three direct-close categories, subprocess/reader-stop guidance, six
  executable examples, four negative examples, and failure precedence.
- Root `AGENTS.md`: mandatory wrapper/lifecycle-test rule linking the guide.
- `openspec/config.yaml`: project context plus `tasks` planning rules aligned
  to the root `AGENTS.md`.
- `README.md`, `README.en.md`, `README.zh.md`: Maintenance link to the guide
  and the root rule.
- `docs/npm-environment-assembler.md`: subprocess owner reuse,
  terminate/remove/reap/close order, deadline bounds, reader coordination,
  and sanitized cleanup diagnostics.

## 6B.14–6B.16 Convergence

- `tests/test_descriptor_close_convergence.py` extends the generated ledger
  with `_PHASE6B_STREAM_FUNCTIONS`/`_PHASE6B_STREAM_METHODS`, `OwnedStream`
  owner recognition (annotation, loop target, conditional binding, factory
  returns), duplicate integer-fd adoption detection, the synchronized
  foundation `OwnedStream.close` release, and violating fixtures for unowned
  pipe close, borrowed release, per-cleanup owner recreation, disguised
  consumer `OwnedStream`, and duplicate fd adoption.
- `tests/test_filesystem_convergence.py` adds the bounded
  `docker/filesystem/streams.py` public surface, isolated-import expectations,
  and the npm package import baseline entry.
- `tests/test_filesystem_lifecycle_docs.py` executes every positive guide
  example and checks the negative examples and cross-references.

## 6B.17 Focused validation evidence

| Check | Command | Result |
| --- | --- | --- |
| Focused Phase 6B suites | `.venv/bin/python -m pytest tests/test_filesystem_owned_descriptors.py tests/test_filesystem_owned_streams.py tests/test_filesystem_cleanup.py tests/test_filesystem_directory_descriptors.py tests/test_filesystem_convergence.py tests/test_filesystem_lifecycle_docs.py tests/test_descriptor_close_convergence.py tests/test_npm_environment_execution.py tests/test_npm_environment_streaming.py tests/test_npm_environment_phase3_deadline.py tests/test_npm_environment_phase4_lifecycle.py tests/test_npm_environment_phase6_introspection.py tests/test_npm_environment_phase6b_streams.py tests/test_retained_tail_phase6.py tests/test_locked_assembly_observability_phase8.py tests/test_bound_pi_assembly_execution_phase6_host.py tests/test_bound_pi_assembly_execution_phase6_registry.py tests/test_transactions_l1_error_boundary.py --tb=short -q` | exit 0; **590 passed, 8 skipped, 2212 subtests passed** in 39.89s. Log: `/tmp/docker-constructor-phase6b-focused.Q7WUhF.log` |
| Full suite | `.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q` | exit 0; **6499 passed, 13 skipped, 17766 subtests passed** in 32.45s. Log: `/tmp/docker-constructor-pytest.OTZuhY.log` |
| Type check | `ty check docker --python-version 3.14 --output-format concise` | exit 0; `All checks passed!` |
| Whitespace | `git diff --check` | exit 0; clean |

### No-drift comparison

- Output/public API: unchanged consumer signatures, `ProcessResult`,
  `LifecycleOutcome`, decoding/tails, redaction, and deadline/grace bounds.
- Authorized behavior corrections:
  - terminal at-most-once stream release (replacing the former repeat-close
    baseline), and the close-failure note now surfacing under the
    reader-failure cleanup hook;
  - `OwnedStream` acquires a candidate before validating, so a rejected
    adoption is released through the owner instead of being left untouched;
    a candidate with no callable `close()` still has no release authority;
  - `_raise_close_authority` applies shared precedence with the active
    operation failure, so an operation `KeyboardInterrupt` keeps its identity
    over a later close defect/interruption;
  - both streaming finalization paths reap a live child before raising a
    close defect/interruption; and
  - `run_captured`'s setup-failure `LifecycleOutcome.errors` uses shared
    cleanup precedence instead of always placing the setup error first.
- The pre-change baseline behavior change is the deadline reader-failure test,
  updated from "multiple stdout close attempts" to exactly-once.
- `openspec validate eliminate-unsafe-direct-descriptor-closes --strict`
  reports only the pre-existing long-requirement warnings in unchanged spec
  files. The Phase 6B change does not modify those specs.

## Revised 6BB stop-policy extension acceptance

The preceding original 6B.14–6B.17 results remain historical. In particular,
rejected-adoption self-release is not the revised 6BA contract. The following
is new evidence for generic 6BB.14–6BB.24, not Docker-wrapper acceptance or
consumer migration.

- Complete RED replay: `/tmp/6bb-policy-complete-red.vEkxw0.log`, exit 1,
  59 new contract failures, 120 passing baseline lifecycle tests.
- Focused extension/baseline/6BA gate: `/tmp/6bb-policy-gate.ujudo5.log`, exit 0,
  360 passed, 509 subtests passed, 4.10 seconds.
- Typecheck: `/tmp/6bb-policy-typecheck.lhWoGK.log`, exit 0.
- Launch-selected stages, phase action/wait caps and local reserve fit one
  original monotonic total budget; a fake clock pins three-stage boundaries.
- Separate stop/reader cancellation preserves bounded simultaneous final tails
  despite full callback spools; actual stopped-reader barriers gate release.
- Partially prepared/main/control resources retain one owner; pending action
  settlement or resources prevent false completion. Default-owned auxiliary
  children and late immutable action observations preserve independent cleanup.
- Baseline active-primary/first-interruption/publication-gap regressions pass;
  policy-stage matrices replay those guarantees without changing consumers.
- No consumer/domain module, filesystem API, Docker policy, runtime image or
  persistent representation changed. Default generic mode and existing
  terminate-CLI/remove/reap consumer behavior remain untouched.

Detailed implementation, mutation-proof and executable-example evidence:
[stop-policy-verification.md](stop-policy-verification.md).

### 6BB.25 full extension gate

| Check | Result / complete log |
| --- | --- |
| Full eight-worker pytest | exit 0; 6738 passed, 13 skipped, 11 warnings, 17775 subtests passed; 31.36s; `/tmp/docker-constructor-pytest.EqtRdn.log` |
| Typecheck | exit 0; All checks passed; `/tmp/6bb-policy-final-typecheck.FtzOnA.log` |
| Configured unittest discovery | exit 0; 6426 tests, 13 skipped; OK; 140.740s; `/tmp/6bb-policy-unittest.qx5ZDA.log` |
| Whitespace | exit 0; clean; `/tmp/6bb-policy-whitespace.SFHgs2.log` |

The optional green-loop launcher was unavailable (missing local runner package,
exit 1, `/tmp/6bb-policy-green-loop.2oWLI5.log`); its configured typecheck and
unittest commands passed when executed directly in the project environment.
No warnings were suppressed. All 6BB extension gates passed; 6BC/6BD/7 remain
pending and this does not accept any Docker-wrapper behavior.

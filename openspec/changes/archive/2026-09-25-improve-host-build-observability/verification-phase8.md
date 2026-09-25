# Phase 8 Verification — Locked Assembly Observability

Change: `improve-host-build-observability`
Phase: 8 (tasks 8.1–8.12)
Date: 2026-09-20
Scope: ordered presentation-neutral operational activity for the locked npm
assembly boundary, URL-free npm diagnostic collection with bounded sanitizer
and line-assembly state, conservative closed diagnostic selection, and the
accepted Phase 6 `--loglevel=http` production policy.

## Deliverables

- `docker/npm_environment/observability.py` (new)
  - `AssemblyActivity` structural protocol (`step(name, *, container_name)`
    and `cache_reuse()`), `NullAssemblyActivity`, and `NULL_ACTIVITY` keep the
    standalone `npm_environment` package free of host presentation imports
    while giving the orchestrator one instrumentation seam.
- `docker/versioning/assembly_activity.py` (new)
  - `HostAssemblyActivity` maps each closed step name to
    `HostStep`, creates one `HostActivityMonitor` per step, declares
    `expects_diagnostic_stream=True` only for `npm_execution`, carries the
    fixed `npm-assembler-<digest-prefix>` container as its safe logical
    resource, owns the existing fixed total assembly deadline, and reports a
    verified cache hit as one `CACHE_REUSE` success fact. `record_diagnostic`
    forwards every received stdout/stderr chunk to the live npm monitor before
    mailbox admission. No renderer, coalescer, timer, Docker/process/filesystem
    probe, or download claim exists here.
- `docker/versioning/npm_diagnostic_stream.py` (new)
  - `NpmDiagnosticStream` incrementally feeds the shared
    `DiagnosticProjector` one newline-delimited raw segment at a time, so each
    removed URL's normalized host and ordered ephemeral fingerprint attach to
    the diagnostic line that contained it.
  - Pending ambiguous URL/secret state is the projector's existing 8 KiB cap;
    decoded diagnostic-line assembly is capped at
    `DIAGNOSTIC_LINE_LIMIT_BYTES = 64 KiB`. Overflow emits exactly
    `[sanitized oversized diagnostic]` and discards through the next newline
    or stream termination; processing recovers afterward.
  - The retained per-stream tail is built only from URL-free projected text
    plus the fixed markers, keeps the existing `TAIL_BYTES` bound and
    truncation semantics, preserves original ordering/occurrences without
    grouping, and excludes discarded overflow fragments, normalized host
    metadata, and fingerprints.
  - `classify_npm_diagnostic` is a conservative closed classifier
    (`WARNING`/`TIMEOUT`/`RETRY`/`ERROR`/`STATUS`) used only for diagnostic
    priority; it never emits a Constructor lifecycle state.
  - `make_stream_factory` wires the per-chunk activity callback; `project_tail`
    has the same signature as the merely secret-redacting `redact_tail` so it
    can be injected at every retained/failure representation.
- `docker/npm_environment/streaming.py`
  - `StreamChunk` gains optional `hostnames` and `url_fingerprints`
    (structured branch only). `DiagnosticStream` is the collector's new
    stream protocol. `collect_streams(..., stream_factory=, tail_projector=)`
    admits either default `str` fragments or pre-projected `StreamChunk`
    items and routes reader-failure detail through the injected projector.
- `docker/npm_environment/publication.py`
  - `assemble_environment(..., stream_factory=, tail_projector=, activity=)`
    instruments `lock_wait`, `cache_lookup`, `cache_reuse`,
    `stale_stage_cleanup`, `validation`, and `publication`; a verified hit
    returns before any container or npm activity.
  - `publish_environment(..., activity=)` scopes validation and atomic
    publication. The verified-collision branch now lives in a nested
    `_publish_once` so a handled collision is not reported as a failed
    publication step.
- `docker/npm_environment/execution.py`
  - `assemble(..., stream_factory=, tail_projector=, activity=)` wraps
    `container_startup` and `npm_execution`; `_streaming_kwargs` forwards the
    projection arguments only to runners that declare them; the non-streaming
    fallback and `DockerRunExecutor.run_streaming` use the injected projector
    so no merely secret-redacted text reaches a returned or failure tail.
- `docker/versioning/pi_assembly.py`
  - `materialize_pi` creates one presentation-session `SessionUrlIdentity`,
    one `HostAssemblyActivity`, a structured npm sink (closed classification,
    normalized hosts, ephemeral fingerprints), an always-on projecting stream
    factory, and `project_tail` for JSON/SDK mode. The single coarse
    `LOCKED_ASSEMBLY` started/terminal lifecycle pair is unchanged.
- `docker/npm_environment/assembler.py`
  - `NPM_CI_FLAGS`, `NPM_CI_COMMAND`, and `ASSEMBLER_SCRIPT` gain
    `--loglevel=http` (accepted Phase 6 decision). The policy digest, script
    digest, assembler identity, input identity, evidence body identity, and
    output/cache identity all change, so prior-policy outputs cannot be reused.
- Tests: `tests/test_locked_assembly_observability_phase8.py`,
  `tests/test_npm_diagnostic_collection_phase8.py`,
  `tests/test_npm_logging_policy_phase8.py` (new), plus updated acceptance
  expectations in `test_constructor_pi_assembly.py`,
  `test_npm_environment_execution.py`,
  `test_npm_environment_serialization.py`, and
  `test_npm_http_logging_research.py`.

## RED evidence

Before implementation, the focused tests failed for the intended missing
behavior:

```text
ModuleNotFoundError: No module named 'docker.versioning.npm_diagnostic_stream'
ModuleNotFoundError: No module named 'docker.versioning.assembly_activity'
ImportError: Failed to import test module: test_locked_assembly_observability_phase8
ImportError: Failed to import test module: test_npm_diagnostic_collection_phase8
```

For task 8.10, the pre-existing rejection-branch expectations failed once the
accepted flag was added, which is the intended RED signal that forced the
accepted-branch update:

```text
FAIL: test_policy_flags_exact (test_npm_environment_execution)
AssertionError: Tuples differ: (... '--no-fund', '--loglevel=http') != (... '--no-fund')
FAIL: test_result_serialization_covers_every_field (test_npm_environment_serialization)
AssertionError: Lists differ: [... '--loglevel=http'] != [...]
```

Those rejection expectations were replaced with the accepted-branch assertions;
no branch preserving the prior command or identity remains.

## Green focused evidence

```bash
python -m unittest tests.test_locked_assembly_observability_phase8 \
  tests.test_npm_diagnostic_collection_phase8 \
  tests.test_npm_logging_policy_phase8
```

```text
....................................................................
----------------------------------------------------------------------
Ran 36 tests in 0.388s

OK
```

The 36 focused tests cover:

- 8.1 complete ordered step sequence
  (`lock_wait → cache_lookup → stale_stage_cleanup → container_startup →
  npm_execution → validation → publication`) with no coarse `HostPhaseEvent`
  emitted by the assembly boundary and failed steps still terminated with no
  surviving `host-activity-heartbeat` thread;
- 8.2 lock contention observed around the unchanged blocking lock with the
  holder still owning the lock when `lock_wait` starts and no timeout,
  polling, or ownership change;
- 8.3 verified cache reuse reported with no container/npm step;
- 8.4 npm execution declaring the expected diagnostic stream and safe
  container resource, per-raw-chunk activity observation, and heartbeat facts
  carrying diagnostic activity age and remaining deadline while claiming no
  current download/network activity and probing no Docker/process/filesystem
  state;
- 8.5 URL removal before the live branch and tail, split credentials across
  many one-byte chunks, percent-encoded URLs, incomplete prefixes at EOF and
  reader failure, 8 KiB pending-sanitizer cap, 64 KiB line-assembly cap,
  overflow markers, recovery after a safe boundary, and continued draining;
- 8.6 ungrouped occurrence/order preservation, host/fingerprint exclusion,
  unchanged tail byte bound and truncation, discarded-fragment exclusion, and
  URL-free tails even with no live sink;
- 8.7 closed warning/timeout/retry/error/status selection, no collector
  coalescing, structured-branch-only hosts/fingerprints, per-chunk factory
  wiring, and no stream-owned timer/worker;
- 8.10 accepted command/script/policy digest, evidence-body identity, and
  prior-policy assembler/input/cache identity rejection.

Related regression suites (streaming, concurrency/cache, publication,
execution, serialization, policy, Pi assembly, research harness, diagnostic
projection/identity/grouping):

```text
Ran 284 tests in 13.717s

OK
```

Full suite and typecheck (after the follow-up executor-failure detail fix):

```text
Ran 3902 tests in 46.514s

OK (skipped=13)
```

```bash
scripts/check-types   # ty check --output-format concise docker tests/typing
```

```text
All checks passed!
```

## INTROSPECT (task 8.11)

Review of the phase diff against the named invariants found and corrected two
issues before validation:

1. **Host/fingerprint association across lines.** The first implementation
   absorbed all projector facts after a whole `feed_bytes` call, so a URL on a
   later line could attach its host and fingerprint to an earlier line. This
   would have let two diagnostics with identical rendered text but different
   hidden URLs coalesce incorrectly. `feed_bytes` now feeds the projector one
   newline-delimited segment at a time (safe because `0x0A` is never part of a
   multibyte UTF-8 sequence) and absorbs facts per segment, and
   `test_host_facts_attach_to_their_own_line` locks the behavior in.
2. **JSON/SDK tail projection.** The projecting stream factory was originally
   created only when a live sink existed, so a structured caller with no sink
   would have retained a merely secret-redacted tail. The factory is now
   always installed so the returned/failure tail is always URL-free, while the
   activity monitors remain presentation-scoped. Task 8.6's
   `test_collect_streams_without_sink_returns_url_free_tail` covers this.
3. **Streaming executor failure detail.** The `executor_failure` detail was
   formatted with the merely secret-redacting `redact`, so an arbitrary
   streaming executor exception containing a URL reached `LockedNpmError` and
   the failure representation with only secret redaction. The except handler
   now routes the formatted detail through the same selected safe tail
   projector (`tail_projector` when supplied, otherwise `redact_tail`) with
   `effective_secrets`, preserving the bounded-detail behavior and error type.
   `test_executor_failure_detail_is_url_free` raises a URL-bearing
   `RuntimeError` from `run_streaming()`, asserts the detail contains no URL
   and no secret, and asserts the sanitized replacement is present. Before
   the fix the same input rendered
   `... fetching https://registry.example.com/pkg?token=abc ...`; after the
   fix it renders `... fetching <redacted> ...`.
4. **Cleanup-note URL sanitization.** The `collect_streams`
   interruption-cleanup and `on_reader_failure` cleanup notes were rendered by
   `_redacted_exception_detail(exc, secrets)` without a projector, so a
   cleanup hook that raised with a URL attached that URL to the primary
   failure note with only secret redaction. Both call sites now pass
   `tail_projector`, so the notes receive the same URL sanitization as the
   primary reader failure. `test_reader_failure_cleanup_note_is_url_free` and
   `test_interruption_cleanup_note_is_url_free` raise URL-bearing cleanup
   hooks carrying credentials, a query, and a fragment and assert neither the
   raised exception nor any `__notes__` contains any complete or partial URL,
   credential, query, or fragment.
5. **Misleading nested activity scopes.** `LOCK_WAIT` previously nested the
   whole locked assembly and `CONTAINER_STARTUP` nested `NPM_EXECUTION`, so
   those steps emitted wait/startup heartbeats and terminal facts for the
   entire build. `assemble_environment` now enters the unchanged blocking
   coordination lock while `LOCK_WAIT` is active and closes `LOCK_WAIT`
   immediately after acquisition, retaining the lock for lookup, assembly,
   validation, and publication via an `ExitStack`. `assemble` now enters
   `CONTAINER_STARTUP`, and the launch boundary is reported by a new
   `on_launched` callback that `DockerRunExecutor.run_streaming` invokes after
   the container client process exists; that callback closes
   `CONTAINER_STARTUP` as succeeded and opens `NPM_EXECUTION`, which stays
   active through stream collection and process completion. A launch failure
   closes `CONTAINER_STARTUP` as failed and never opens `NPM_EXECUTION`. The
   new ordered-event assertions in
   `test_operational_steps_terminate_before_next_step_starts` require
   `LOCK_WAIT started → LOCK_WAIT succeeded → CACHE_LOOKUP started` and
   `CONTAINER_STARTUP started → CONTAINER_STARTUP succeeded → NPM_EXECUTION
   started`, and `test_failed_container_launch_never_starts_npm_execution`
   proves an unsuccessful launch emits only
   `CONTAINER_STARTUP started`/`failed` with no `NPM_EXECUTION` event.
6. **URL-bearing cleanup-failure details.** `_cleanup_container` and
   `_remove_staging_safely` rendered their exception and captured-output
   details with the merely secret-redacting `redact`, so Docker `rm` stderr
   or a cleanup exception carrying a URL reached the attached
   `CleanupFailure` note with only secret redaction. Both helpers now accept
   an optional `tail_projector`, select
   `project = tail_projector if tail_projector is not None else redact_tail`,
   and route exception, stderr, and stdout details through `project(...)`.
   Container-absence detection still uses the original output, so
   sanitization never changes control flow. `assemble` and
   `assemble_environment` pass the effective `tail_projector` to every
   cleanup call, so container, assembly-staging, and publication-staging
   notes cannot bypass URL sanitization. Without an injected projector the
   previous secret-redacted fallback is unchanged. Regression coverage lives
   in `tests/test_npm_environment_cleanup.py`
   (`TestCleanupDetailProjection`: container exception and nonzero-stderr
   paths, staging `LockedNpmError` and arbitrary-exception paths, plus a
   fallback secret-redaction test) and
   `tests/test_npm_environment_publication_cleanup.py`
   (`test_publication_cleanup_url_is_projected`); each asserts the primary
   exception is unchanged, the note still names the failed operation and
   residue risk, and no URL, hostname, credential, query, fragment, or secret
   remains. Before the fix the staging detail was
   `... rm failed https://user:pass@registry.example.com/pkg?token=abc#frag
   <secret-redacted>`; after the fix the URL renders as `<redacted>`.

The remaining reviewed items are satisfied by construction and by focused
tests: no pre-projection tail write; no URL leakage across chunk boundaries;
8 KiB sanitizer and 64 KiB line bounds; non-blocking incremental draining;
fixed safe overflow, incomplete-token, and oversized-diagnostic markers;
recovery after a safe boundary; unchanged tail bound/truncation; no facade or
output-policy dependency in the collector; no assembler-owned presentation
timer or coalescing; conservative display-only classification with no
lifecycle derivation; unchanged blocking-lock semantics with no polling,
timeout, or ownership change; no Docker introspection; validated
container/resource labels; the accepted `--loglevel=http` policy applied
atomically; identity changes derived from the canonical constants rather than
hardcoded; no tail duplication; and unchanged cleanup ownership.

Cleanup-only exception strings from pipe-close/deadline notes in
`DockerRunExecutor.run_streaming` continue to use the existing
secret-redacting `_render_close_failure`/`redact_tail` renderer; they are
constructor-generated `OSError`-style messages, not streamed assembler
diagnostics, so they remain outside the URL-projection boundary defined for
npm stdout/stderr. The `collect_streams` interruption and
`on_reader_failure` cleanup notes, and the `_cleanup_container` /
`_remove_staging_safely` failure details, are inside that boundary and are
now URL-projected.

## VALIDATE (task 8.12)

- Input continues draining after the 8 KiB pending-sanitizer and 64 KiB
  line-assembly limits; both bounds are asserted during and after overflow.
- Live structured events and retained tails contain only URL-free safe text,
  fixed safe markers, normalized host facts (structured branch only), and
  ephemeral fingerprints (structured branch only). No fingerprint or session
  key enters a tail, failure, or evidence representation.
- Existing tail byte limits and truncation semantics are unchanged.
- All Phase 8 deliverables apply the accepted Phase 6 `--loglevel=http`
  decision; prior-policy assembler and input identities are rejected, and
  evidence/body digests differ from the prior policy.
- The embedded `--loglevel=http` observations are classified into the closed
  Phase 8 diagnostic set and conform to the Phase 7 ephemeral URL-identity
  contract.

## POST-VALIDATE HARDENING — per-line projector fact consumption

Two defects found after task 8.12, both rooted in the projector keeping a
cumulative hostname/fingerprint history that the collector sliced per chunk:

1. **Repeated hosts lost metadata.** `DiagnosticProjector` deduplicated
   hostnames against its whole-stream `_hostnames` list and the collector
   sliced `hostnames[_seen_hostnames:]`, so only the first line containing a
   host carried that hostname. Identical subsequent lines lost it.
2. **Unbounded metadata.** `_hostnames`/`_fingerprints` accumulated for the
   whole stream and each `_absorb_projector_facts` call copied the entire
   history (`hostnames[seen:]`) on every chunk, so a long-running URL-heavy
   stream retained and repeatedly copied metadata for completed lines.

### Changes

- `docker/versioning/diagnostic_projection.py`: added
  `DiagnosticProjector.take_facts()`, returning and clearing the facts
  extracted since the previous call, preserving fingerprint order and
  multiplicity. `hostnames`/`url_fingerprints` now report only *unconsumed*
  facts. Callers that never drain (`sanitize_diagnostic_text`,
  `project_structured_diagnostic`) still observe the complete set for one
  diagnostic, so single-complete-diagnostic behavior is unchanged.
- `docker/versioning/npm_diagnostic_stream.py`: removed `_seen_hostnames` and
  `_seen_fingerprints` and the per-chunk history copying/slicing.
  `_absorb_projector_facts` now calls `take_facts()` and, when the current
  line is not overflowing, deduplicates hostnames **within the line only**
  and appends fingerprints verbatim; the projector is therefore drained after
  every newline-delimited segment and never retains completed-line metadata.
  While a line overflows, consumed facts are discarded alongside its text
  until the newline; the fixed `[sanitized oversized diagnostic]` marker
  carries no metadata and the following line resumes normal collection.

### RED evidence

Reproducing the pre-fix cumulative slicing (no `take_facts` drain):

```
OLD behaviour per line: [(('registry.example.com',), 1), ((), 1)]
second line hostname lost: True
```

### Green focused evidence

New `TestConsumablePerLineFacts` in
`tests/test_npm_diagnostic_collection_phase8.py`:

- repeated identical URL-bearing lines each carry the hostname;
- multiple URLs preserve fingerprint order and repeated occurrences;
- byte-by-byte and whole-buffer feeds produce identical per-line metadata;
- 2000 URL-heavy lines retain no completed-line metadata
  (`_line_hostnames`/`_line_fingerprints` empty and `take_facts() ==
  ((), ())` between lines);
- an oversized URL-heavy line stays within the 8 KiB sanitizer and 64 KiB
  line bounds, emits exactly one metadata-free marker, leaks neither text nor
  hostname into the tail, and recovers the next line's host.

New `TestConsumableFacts` in `tests/test_host_diagnostic_projection.py` binds
`take_facts()` directly: it drains and preserves fingerprint order and
multiplicity, leaves nothing for the next diagnostic, and an undrained
projector still reports the complete single-diagnostic view.

```
Ran 27 tests in 0.300s
OK   # tests.test_npm_diagnostic_collection_phase8

Ran 96 tests in 0.550s
OK   # tests.test_host_diagnostic_projection
```

Focused verification (diagnostic projection, identity, collector, streaming,
locked-assembly observability, host events, download observability, grouping,
pi assembly, logging research):

```
Ran 292 tests in 13.408s
OK
```

`scripts/check-types` -> `All checks passed!`

Full suite:

```
Ran 3909 tests in 46.830s
OK (skipped=13)
```

Tails remain URL-free and contain neither host metadata nor fingerprints
(asserted by `test_tail_excludes_host_metadata_and_fingerprints`,
`test_tail_excludes_discarded_overflow_fragment`, and the new oversized-line
and repeated-host tests).

## POST-VALIDATE HARDENING — URL-safe deadline/supervisor/pipe-close notes

A gap remained in `docker/npm_environment/execution.py`: the deadline,
supervisor, and pipe-close failure helpers rendered exception details with
`_render_close_failure()`, which used only the secret-only `redact_tail()`.
A cleanup or close failure whose message carried a credential-bearing URL
therefore leaked the host, credentials, query, and fragment into the
timeout/interruption/supervisor notes, and `assemble()` re-raised those
exceptions without URL sanitization.

### Changes

- `_render_close_failure()` gained an optional keyword-only
  `tail_projector`.  When supplied it is used instead of `redact_tail()`;
  the bounded `READER_FAILURE_DETAIL_BYTES` limit and the secret-only
  fallback are preserved.
- `_attach_close_failure_notes()`, `_raise_close_failures()`,
  `_attach_bounded_cleanup_notes()`, `_attach_deadline_notes()`, and
  `_raise_supervisor_failure()` now accept and forward the projector to
  every detail they sanitize.
- `DockerRunExecutor.run_streaming()` passes its existing `tail_projector`
  to every one of those helpers and to every direct
  `_render_close_failure()` call, covering the timeout, interruption,
  supervisor-wait-failure, reader-failure, and normal-completion
  pipe-close paths.
- Cleanup ownership, deadlines, and primary exception classification are
  unchanged; `redact_tail` remains only as the no-projector fallback.

### RED evidence

Reproducing the pre-fix secret-only rendering on the real executor timeout
path (`terminate` raising an `OSError` that carries the URL):

```
PRIMARY: AssemblyTimeoutError
LEAKS URL (pre-fix): True
--- note ---
assembly exceeded the 0.2-second total deadline
deadline cleanup failed (OSError): terminate failed fetching https://user:pass@registry.example.com/pkg?token=abc#frag (<redacted>)
```

### Green focused evidence

New `TestUrlFreeFailureNotes` in
`tests/test_npm_environment_phase3_deadline.py` drives the real
`DockerRunExecutor.run_streaming()` with fake processes/pipes and
`tail_projector=project_tail`:

- URL-bearing `terminate()` cleanup failure: the `AssemblyTimeoutError`
  stays primary (`reason == "assembly_timeout"`) and the
  `deadline cleanup failed` note is URL- and secret-free;
- URL-bearing pipe-close failure during deadline cleanup: the timeout stays
  primary and every `pipe close failed` note is URL-free;
- URL-bearing supervisor `wait` failure: the `OSError` detail is bounded and
  URL-free.

New `TestReaderFailure` cases in `tests/test_npm_environment_streaming.py`:

- `test_run_streaming_reader_failure_close_note_is_url_free`: a reader
  failure whose cleanup close raises a credential-bearing URL keeps the
  `StreamReaderFailure` primary and the `on_reader_failure cleanup failed`
  note URL-free;
- `test_run_streaming_close_failure_detail_is_url_free`: a normal-completion
  pipe-close failure raises an `OSError` whose message is URL- and
  secret-free, after the subprocess is reaped.

```
Ran 3 tests ... TestUrlFreeFailureNotes     OK
Ran 15 tests ... TestReaderFailure          OK
Ran 271 tests in 19.601s                    OK   # focused 11-module run
```

`scripts/check-types` -> `All checks passed!`

Full suite:

```
Ran 3914 tests in 47.092s
OK (skipped=13)
```

Existing timeout, interruption, cleanup, and Phase 8 diagnostic tests remain
green, so interruption semantics and the constructor-owned timeout
classification are intact.

## POST-VALIDATE HARDENING — cancellation-aware reader finalization

A gap remained between the collector and its cancellation paths: the reader
worker finalized with `finish(abort=failed)`, where `failed` was true only
for the reader whose own `read`/`feed_bytes` raised.  Cancellation that is
delivered through a *forced* EOF — deadline termination, interruption, or a
sibling-reader failure forcing the blocked sibling to EOF — therefore looked
like a clean EOF, and an ambiguous pending prefix was flushed as ordinary
text.  Interrupting after `b"npm status %68%74"` emitted the encoded URL
prefix unchanged instead of `[sanitized incomplete token]`.

### Changes

- `docker/npm_environment/streaming.py` — `collect_streams()` gained a
  shared `abort_event: threading.Event | None` (a private event is created
  when omitted).  It is the one cancellation signal visible to both reader
  workers:
  - `record_failure()` sets it *before* the coordinator invokes
    `on_reader_failure()`, so a sibling forced to EOF by the cleanup
    finalizes as aborted;
  - the coordinating `except BaseException` (interruption) branch sets it
    *before* invoking `on_interruption()`;
  - each `drain()` reader now finalizes with
    `finish(abort=failed or abort_event.is_set())`, replacing
    `finish(abort=failed)`.  A genuine, unsignalled clean EOF still uses
    `abort=False`.
- `docker/npm_environment/execution.py` — `DockerRunExecutor.run_streaming()`
  creates the shared event and passes it to `collect_streams()`.  Its
  `bounded_streaming_cleanup()` sets the signal as its first action, so the
  deadline supervisor and every interruption-cleanup path signal abort
  before terminating the client or closing pipes.  Cleanup ownership,
  worker joins, and the `AssemblyTimeoutError`/`StreamReaderFailure`
  classification are unchanged.

### RED evidence (captured against the pre-fix code)

```
direct clean:      'npm status %68%74'                              tail= 'npm status %68%74'
direct abort=True: 'npm status [sanitized incomplete token]'        tail= 'npm status [sanitized incomplete token]'
collector sibling-failure live: ['npm status %68%74']   # leaked
```

A deadline-style external release without the shared signal (the pre-fix
timeout behavior) leaks the prefix, while setting the signal before
unblocking the readers fails closed:

```
PRE-FIX (no abort signal before unblock): 'npm status %68%74'
        leaks %68%74: True
POST-FIX (abort signal set before unblock): 'npm status [sanitized incomplete token]'
        leaks %68%74: False
```

### Green focused evidence

`tests/test_npm_diagnostic_collection_phase8.py` — new
`TestCollectionAbortFinalization` (collector level, `make_stream_factory`,
`abort_event`, SIGINT via `signal.pthread_kill`):

- `test_clean_eof_control_flushes_the_prefix` — an unsignalled clean EOF
  still flushes `%68%74` unchanged;
- `test_sibling_reader_failure_fails_closed` — stderr failure forces stdout
  to EOF; live chunks and the finalized stdout tail carry
  `[sanitized incomplete token]`, never `%68%74`;
- `test_interruption_fails_closed` — a real SIGINT to the coordinating
  thread finalizes the pending prefix as `[sanitized incomplete token]`;
- `test_caller_abort_signal_reaches_both_readers` — one injected signal set
  before unblocking forces *both* reader tails to fail closed.

`tests/test_npm_environment_phase3_deadline.py` — new
`TestForcedEofFinalization` drives the real
`DockerRunExecutor.run_streaming()` with a structured stream factory:

- `test_timeout_forces_abort_finalization_of_pending_prefix` — the deadline
  terminates a blocked reader holding `b"npm status %68%74"`; the
  `AssemblyTimeoutError` stays primary (`assembly_timeout`) and its retained
  tail carries `[sanitized incomplete token]`, never `%68%74`;
- `test_clean_unforced_eof_keeps_the_prefix` — the same prefix at an
  unforced clean EOF is retained as ordinary text.

Every new test also asserts `_assert_no_workers(self)`, confirming no reader
or dispatcher worker remains.

```
Ran 6 tests ... TestCollectionAbortFinalization + TestForcedEofFinalization   OK
Ran 277 tests in 19.998s    # focused 11-module run                        OK
```

`scripts/check-types` -> `All checks passed!`

Full suite:

```
Ran 3920 tests in 47.336s
OK (skipped=13)
```

Existing streaming, deadline/interruption, cleanup, and Phase 8 diagnostic
tests remain green, so clean-EOF behavior, cleanup ownership, and timeout
classification are intact.

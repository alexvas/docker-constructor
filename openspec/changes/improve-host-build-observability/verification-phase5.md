# Phase 5 Verification — Host Download Observability

Change: `improve-host-build-observability`
Phase: 5 (tasks 5.1–5.8)
Date: 2026-09-19
Scope: presentation-neutral start / verified-cache-hit / byte-progress /
terminal activity for the four reviewed build artifacts and the three
authoritative Pi release assets, with safe logical failure attribution and
unchanged acquisition, checksum, publication, and cleanup semantics.

## Deliverables

- `docker/versioning/host_progress.py`
  - `HostStepEvent.logical_resource: str | None` and
    `HostTransportProgressEvent.logical_resource: str | None` carry the closed
    safe asset name for acquisition steps and progress. Both are optional and
    type-checked, so non-asset steps remain valid.
- `docker/versioning/activity_monitor.py`
  - `HostActivityMonitor(..., logical_resource=None)` includes the resource in
    its start/terminal step facts.
  - The monitor publishes one cumulative `HostTransportProgressEvent` at
    heartbeat cadence (immediately before the heartbeat) and once at terminal,
    never once per yielded chunk. Diagnostic silence stays absent because the
    acquisition steps declare no expected diagnostic stream, and the heartbeat
    schedule is unchanged by byte activity.
- `docker/versioning/diagnostic_projection.py`
  - `project_host_acquisition_failure(...)` composes the bounded exception-type
    chain with the shared URL/hostname projection: the artifact URL is removed
    from the text and only its normalized hostname survives as a host fact.
  - `project_exception_type_chain(...)` traverses an exception-valued
    ``.reason`` before ``__cause__``/``__context__``, so
    ``URLError(TimeoutError(...))`` reports both types instead of only
    ``URLError``. Identity cycle detection, duplicate-name suppression, the
    four-unique-type cap, and message non-evaluation are unchanged.
  - Relationship reads go through the module-private
    `_read_exception_relationship(...)` helper: attribute access is guarded by
    `try/except BaseException` and only an exception-valued result is used, so
    a hostile exception that raises from ``reason``/``__cause__``/``__context__``
    cannot replace the original acquisition failure. Non-exception or raising
    reads simply fall through to the next relationship in precedence order and
    no value or caught exception is ever stringified.
- `docker/versioning/build_materialization.py`
  - `materialize_artifact(..., progress=None, on_cache_hit=None)` reports
    cumulative received bytes after validating each yielded body chunk and
    **before** the disk write/hash boundary, so a blocked or failed write
    cannot hide bytes already received from heartbeat and terminal progress. A
    verified cache hit reports reuse without adding a request.
  - `materialize_build_artifacts(..., event_sink=None, monitor_factory=None,
    failure_secrets=())` scopes each reviewed artifact with its own closed
    logical name and safe failure context. With no sink/factory the path stays
    a pure no-observability materialization.
  - Transport and materialization wraps now retain the exception cause
    (`from exc`) so the nested type chain can be projected without evaluating
    any message.
- `docker/versioning/pi_release.py`
  - `download_bytes(..., progress=None)` reports cumulative bytes per chunk.
  - `acquire_install_assets(..., activity=None)` opens one observation scope
    per closed logical asset and keeps digest verification inside that scope,
    so a checksum failure is attributed to the failing asset. Acquisition and
    checksum order, request count, and buffering are unchanged.
  - The `download` callback is overload-typed across three shapes: with no
    `activity` it keeps the legacy one-argument `download(url) -> bytes`
    contract; a definite `AssetActivityFactory` uses
    `Callable[[str, ProgressCallback | None], bytes]`, so a callback whose
    progress argument is mandatory is valid because that branch always passes
    it; and an optional `AssetActivityFactory | None` requires the
    `ProgressAwareDownload` protocol (optional *progress*), because that value
    may take either runtime branch. A callback that requires two arguments is
    therefore never advertised as valid when `activity` may be `None`.
- `docker/versioning/pi_assembly.py`
  - `materialize_pi` observes `SHA256SUMS`, the install package, and the
    install lock through the same safe activity/failure context, with the
    proxy URL and corporate trust path registered as redaction secrets.
- `tests/typing/pi_release_download_contract.py` (new)
  - Static typing contract for the download callback overloads, checked by
    `scripts/check-types`. Positive cases use `assert_type`; negative cases
    carry a blanket `ty: ignore` whose `unused-ignore-comment` warning fires
    if a rejected combination is ever accepted.
- `docker/versioning/build_orchestration.py`
  - Forwards `event_sink` and `failure_secrets` to the artifact materializer
    **independently** (signature inspection, `**kwargs` detected once), so an
    injected materializer that accepts `event_sink` but not `failure_secrets`
    no longer receives an unexpected keyword; legacy materializers that accept
    neither keep their existing call shape.
- New tests `tests/test_host_download_observability.py` (17 tests) and four
  materializer-keyword tests in `tests/test_constructor_build_orchestration.py`.

## Interpretive decision recorded

The delta specifications require *distinct per-asset activity under a closed
logical asset name* (for example "each acquisition SHALL expose start and
terminal activity under its distinct closed logical asset name", and Pi
progress "SHALL identify the corresponding closed logical asset"). The Phase 1
operational DTOs carried no logical resource on step/progress facts, so Phase 5
adds an optional `logical_resource` to `HostStepEvent` and
`HostTransportProgressEvent`. Production code derives the value from the
validated `DiagnosticLogicalResource`, keeping the closed reviewed-artifact and
Pi-asset name sets authoritative. No event accepts a raw URL or an arbitrary
label.

## RED evidence (tasks 5.1–5.4)

The tests were run against the pre-Phase-5 tree in a detached worktree at
`HEAD` (Phases 1–4 only), without touching the index:

```
$ git worktree add --detach /tmp/dc-phase5-red HEAD
$ cp tests/test_host_download_observability.py /tmp/dc-phase5-red/tests/
$ /…/.venv/bin/python -m unittest tests.test_host_download_observability
ImportError: cannot import name 'project_host_acquisition_failure' from
'docker.versioning.diagnostic_projection'
Ran 1 test in 0.000s
FAILED (errors=1)
```

Before Phase 5, `HostStepEvent`/`HostTransportProgressEvent` had no
`logical_resource`, `materialize_artifact` had no `progress`/`on_cache_hit`
boundary, `materialize_build_artifacts` emitted nothing, `download_bytes` took
no progress callback, and `acquire_install_assets` had no per-asset scope. The
focused behaviors therefore had no production path to satisfy.

Behavioral RED for streamed chunks (same detached worktree):

```
$ python _probe.py   # record 2500 bytes, drive the first heartbeat, finish
['HostStepEvent', 'HostHeartbeatEvent', 'HostStepEvent']
```

At `HEAD` the coordinator published no byte-progress fact for the recorded
chunks; after Phase 5 the same sequence yields
`['HostStepEvent', 'HostTransportProgressEvent', 'HostHeartbeatEvent', 'HostStepEvent']`.

## Boundary coverage

| Spec boundary | Production path exercised | Assertion |
| --- | --- | --- |
| Reviewed artifact start/terminal | `materialize_build_artifacts` (streamed miss) | per-asset `STARTED`/`SUCCEEDED` with `logical_resource`, reviewed order, `expects_diagnostic_stream=False` |
| Verified cache hit | `materialize_build_artifacts` (second run) | `CACHE_REUSE/SUCCEEDED` per asset, zero transport calls, no progress |
| Mandatory chunk progress | `HostActivityMonitor.record_transport_progress` + heartbeat | cumulative bytes and `transport_progress` last activity for the next heartbeat; progress precedes the heartbeat |
| Single-chunk compatibility | `materialize_build_artifacts` with a one-chunk body | exactly one cumulative update, exactly one request |
| No-observable-chunk / no-sink allowance | empty-body transport and no-sink run | start/terminal still reported; progress omitted |
| Diagnostic silence omission | acquisition monitors | every heartbeat has `diagnostic_silence_seconds is None` |
| Unchanged heartbeat cadence | fake clock, progress between beats | `elapsed_seconds == [3, 4, 5]` |
| Blocked write boundary | `materialize_build_artifacts` with a gated `write` | the yielded chunk is counted before the write; the next heartbeat exposes it with `transport_progress` activity and a preceding `HostTransportProgressEvent` |
| Failed write boundary | `materialize_build_artifacts` with a failing `write` | terminal progress includes the yielded chunk, the artifact step fails, temp data is removed, and no blob is published |
| Materializer keyword: `event_sink` only | injected artifact materializer without `failure_secrets` | build succeeds; `event_sink` received |
| Materializer keyword: neither | legacy materializer without either keyword | build succeeds |
| Materializer keyword: both / `**kwargs` | injected materializer accepting both or `**kwargs` | receives the expected `event_sink` and `failure_secrets` |
| Artifact failure attribution | `materialize_build_artifacts` transport failure | `HostStructuredDiagnostic(ERROR)` with the artifact name, bounded type chain, no URL/proxy/credential/message |
| Registered secret redaction | failure with `failure_secrets` | proxy host/port absent from text |
| Message non-evaluation | `project_host_acquisition_failure` with a `__str__` bomb | projection succeeds |
| Exception chain: `URLError` reason | `project_exception_type_chain(URLError(TimeoutError(...)))` | `("URLError", "TimeoutError")` |
| Exception chain: reason precedence | `.reason` set alongside cause/context | reason wins over cause and context |
| Exception chain: non-exception reason | `URLError("text")` plus cause/context | falls back to cause, then context |
| Exception chain: reason cycle | `TimeoutError.reason -> URLError` | terminates |
| Exception chain: reason message bomb | `URLError(_MessageBombError(...))` | type name only, message never evaluated |
| Hostile relationship reads | exception subclasses that raise from `.reason`, `__cause__`, or `__context__` access | projection never raises, root type still returned, message never evaluated, and the next safe relationship is still traversed |
| urllib artifact acquisition | real `UrllibStreamingTransport`, `urlopen` raises `URLError(TimeoutError("private detail"))` | diagnostic names `uv` plus `URLError`/`TimeoutError`, hostname separate, private message/URL removed, temp cleaned, no blob |
| urllib Pi acquisition | real transport through `materialize_pi` | diagnostic names `SHA256SUMS` plus `URLError`/`TimeoutError`, no private message or `github.com`, assembly never runs |
| Hostile transport failure | `materialize_build_artifacts` with an error that raises on `.reason` access | the original `MaterializationError` stays the operation failure (with the hostile error as `__cause__`), a safe `ERROR` diagnostic names `uv` plus `_HostileRelationshipError`, and the terminal `FAILED` step fact is still emitted |
| Pi per-asset order | `acquire_install_assets` activity scopes | `SHA256SUMS` → package → lock, enter/exit each once |
| Pi progress forwarding | `acquire_install_assets` with an activity factory and a two-argument callback | the scope-yielded progress callback is forwarded for all three assets |
| Pi legacy callback contract | `acquire_install_assets` with no activity and a one-positional-argument callback | every asset is fetched via `download(url)`; a second argument would raise `TypeError` |
| Callback typing: legacy + no activity | `ty check tests/typing` positive case | accepted |
| Callback typing: mandatory two-argument + definite activity | `ty check tests/typing` positive case | accepted (that branch always passes the observer) |
| Callback typing: optional-progress + definite/optional activity | `ty check tests/typing` positive cases | accepted for `AssetActivityFactory` and `AssetActivityFactory \| None` |
| Callback typing: one-argument + activity factory | `ty check tests/typing` negative case | `no-matching-overload` (ignore used) |
| Callback typing: mandatory two-argument + `activity=None` | `ty check tests/typing` negative case | `no-matching-overload` (ignore used) |
| Callback typing: mandatory two-argument + optional activity | `ty check tests/typing` negative case | `no-matching-overload` (ignore used) |
| Pi verification attribution | package digest mismatch | SHA256SUMS succeeds; package fails; lock never acquired |
| Pi production integration | `materialize_pi` success and package failure | three distinct asset scopes; failure names `pi-coding-agent-install-package.json`; request order unchanged |

## INTROSPECT (task 5.7)

Reviewed the phase diff against the listed hazards:

- **Extra network calls / duplicate downloads:** none. The Pi test asserts the
  exact three-URL order and the artifact test asserts zero calls on a verified
  hit and exactly one call per streamed artifact.
- **Eager buffering:** none added. `download_bytes` already accumulated chunks;
  only a per-chunk counter was added. `materialize_artifact` still streams to
  the temporary file.
- **Required byte totals / percentages / rates:** none. Progress is a
  cumulative count only; no total, percentage, or rate is computed.
- **URL retention:** the failure text is produced by the shared projector, so
  the URL is removed and only its normalized hostname is retained as a fact.
  Tests assert the URL, scheme, userinfo, port, path, query, and credentials are
  absent from the event text.
- **Checksum reordering / changed cache admission / cleanup regression:** none.
  Verification remains inside the acquisition scope and in the same order;
  `_verify_hit`, atomic publication, `mark_uncommitted_blob`, and the temporary
  file cleanup are untouched.
- **No new presentation behavior:** the facade still owns rendering; the new
  facts are presentation-neutral and sink-optional.
- **Legacy callback contract regression:** reverting the unobserved branch to
  `download(url, None)` fails the legacy-contract tests with
  `TypeError: ... takes 1 positional argument but 2 were given` (5 errors),
  confirming the one-argument contract is enforced rather than incidentally
  satisfied.
- **Typing-contract sensitivity:** removing the definite-activity overload
  makes the mandatory two-argument positive case fail with
  `no-matching-overload`, confirming that overload is what admits the shape
  and that the optional-activity `ProgressAwareDownload` overload alone does
  not.
- **Independent materializer keywords:** reintroducing the coupled forwarding
  (adding `failure_secrets` whenever `event_sink` is accepted) fails
  `test_materializer_accepting_only_event_sink_still_executes` with
  `TypeError: ... unexpected keyword argument 'failure_secrets'` — the exact
  reported regression.
- **Progress before the write boundary:** reverting the counter to after
  `output.write`/`digest.update` fails both write-boundary tests (blocked
  write and write failure).
- **Exception-reason traversal:** reverting
  `project_exception_type_chain` to cause/context-only traversal fails six
  tests; the Pi diagnostic degrades to
  `SHA256SUMS: acquisition failed (PiReleaseError -> MaterializationError -> URLError)`,
  losing `TimeoutError`.
- **Hostile relationship reads:** reverting `_read_exception_relationship` to
  direct `getattr(current, "reason", None)` / `current.__cause__` /
  `current.__context__` access fails seven tests (six projection, one
  integration) with `RuntimeError: hostile relationship access`; in the
  integration case that hostile error replaces the acquisition failure instead
  of being contained by the safe projection.
- **Closed logical resources:** restoring the open "string or None" check in
  `HostStepEvent`/`HostTransportProgressEvent` fails the rejection assertions
  with `AssertionError: ValueError not raised` (13 subtest failures for the
  arbitrary step/progress labels), confirming the closed contract is enforced
  at the DTO boundary rather than incidentally satisfied by callers.

No acquisition-observability defects required correction.

## Closed logical-resource enforcement (follow-up)

The operational `logical_resource` field was previously validated as any
`str | None`, so an unapproved asset label could enter `HostStepEvent`,
`HostTransportProgressEvent`, or `HostStructuredDiagnostic`. The closed
contract now lives in one lower-level module shared by operational events and
the safe projector without a circular import:

- `docker/versioning/logical_resource.py` (new, dependency-free): closed
  `REVIEWED_ARTIFACT_NAMES`, `PI_RELEASE_ASSET_NAMES`, the
  `DiagnosticResourceKind` enum, the `DiagnosticLogicalResource` DTO, and the
  `is_approved_logical_resource_name`/`require_approved_logical_resource`
  validators. Only the approved reviewed-artifact names, Pi-release asset
  names, and the fixed `npm-assembler-<digest-prefix>` form pass; `None` stays
  valid for non-asset steps.
- `docker/versioning/host_progress.py` imports the validator and applies it to
  `HostStepEvent`, `HostTransportProgressEvent`, and `HostStructuredDiagnostic`;
  non-strings stay `TypeError` and unapproved strings raise `ValueError`.
- `docker/versioning/activity_monitor.py` validates its `logical_resource`
  with the same closed helper so a bad identity is rejected at construction
  rather than failing later inside heartbeat emission.
- `docker/versioning/diagnostic_projection.py` imports and re-exports the
  shared constants and DTO instead of maintaining a second copy; all safe
  projection behavior is unchanged.
- `docker/versioning/build_materialization.py` and
  `docker/versioning/pi_assembly.py` pass the validated `resource.name` to the
  monitor and cache-hit step events, while the emitted payload stays the safe
  display name consumers already expect.

| Coverage | Test | Expected |
| --- | --- | --- |
| Approved names accepted | every reviewed artifact, every Pi asset, and an assembler container form | step and progress events carry the same safe name |
| `HostStepEvent` rejection | arbitrary/`../`/newline/empty/assembler-malformed labels | `ValueError`; non-string `TypeError` |
| `HostTransportProgressEvent` rejection | same label set | `ValueError`; non-string `TypeError` |
| `HostStructuredDiagnostic` rejection | unapproved string / non-string | `ValueError` / `TypeError` |
| `None` preserved | non-asset npm step and progress, and a diagnostic without a resource | `logical_resource is None` |

## Commands and results (task 5.8)

```
$ python -m unittest tests.test_host_operational_events \
    tests.test_host_diagnostic_projection tests.test_host_download_observability \
    tests.test_host_activity_monitor tests.test_constructor_build_materialization \
    tests.test_constructor_pi_assembly tests.test_constructor_pi_release \
    tests.test_constructor_build_orchestration
Ran 320 tests in 3.298s
OK

$ python -m unittest discover -s tests -p 'test_*.py'
Ran 3796 tests in 30.370s
OK (skipped=13)

$ scripts/check-types
All checks passed!

$ git diff HEAD --check
(clean)
```

Recorded: effect counts are unchanged (one request per streamed artifact, the
exact three Pi requests in order, zero requests on a verified hit); progress is
mandatory for every chunk the existing boundary yields; omission applies only
to the compatibility allowance (no observable chunks) and the no-sink path.
Host downloads declare `expects_diagnostic_stream=False`, so their heartbeat
facts never carry diagnostic silence.

## Files changed

- `docker/versioning/logical_resource.py` (new shared closed-resource contract)
- `docker/versioning/host_progress.py`
- `docker/versioning/activity_monitor.py`
- `docker/versioning/diagnostic_projection.py`
- `docker/versioning/build_materialization.py`
- `docker/versioning/pi_release.py`
- `docker/versioning/pi_assembly.py`
- `scripts/check-types` (also checks the `tests/typing` contract)
- `tests/typing/pi_release_download_contract.py` (new static typing contract)
- `docker/versioning/build_orchestration.py`
- `tests/test_constructor_pi_release.py` (callbacks stay one-argument,
  proving the legacy contract is still supported)
- `tests/test_constructor_build_orchestration.py` (four materializer-keyword
  forwarding tests)
- `tests/test_host_diagnostic_projection.py` (five exception-`.reason`
  traversal tests)
- `tests/test_host_operational_events.py` (closed logical-resource acceptance
  and rejection tests)
- `tests/test_host_download_observability.py` (new)
- `openspec/changes/improve-host-build-observability/tasks.md` (5.1–5.8 checked)

No specification file was edited.

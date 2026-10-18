# Phase 7 preflight: network-resource defects delegated

## Status

The network-resource defects identified during preflight have been explicitly
delegated to a separate change by the user. **Phase 7 proceeds without these
delegated defects in its scope and does not depend on their resolution.**
The original pause for these findings is lifted; the file name is retained for
traceability. No Phase 7 task is marked complete by this scope decision.

## Delegated scope

The follow-up change is
[own-http-response-and-probe-server-lifecycles](../own-http-response-and-probe-server-lifecycles/proposal.md).
Its [tasks](../own-http-response-and-probe-server-lifecycles/tasks.md) own both
the completed minimal repairs (0.1–0.8) and the remaining lifecycle improvements.
The delegated defects and their associated characterization/enforcement are:

- Buffered metadata response/error-response ownership in
  `docker/versioning/transports.py::build_transports._ProductionHttp.request`,
  including release preparation and cleanup-failure handling.
- Streamed artifact response/error-response ownership in
  `docker/versioning/artifact_cache.py::HttpStreamingTransport.fetch_chunks`
  and deterministic iterator finalization in `_materialize_one`.
- Probe listener and worker lifecycle in
  `docker/networking.py::HostProbeServer.start/stop`, including partial startup,
  readiness, independent cleanup, concurrent/repeated stop, deferred safe release
  and retained incomplete outcomes; associated diagnosis teardown precedence
  belongs to the same delegated work.

This is a delegation of these specific network-resource families, not an
exclusion of their entire modules from other Phase 7 checks. Other descriptor,
file-wrapper and subprocess ownership paths remain in the original audit scope.
Delegated sites are not declared safe, non-descriptor or mechanically proved
merely because their implementation is in the standard library.

## Minimal repairs already delivered

The delegated defects are partially repaired:

- Buffered and streaming acquisition HTTPError responses now receive release
  through `OwnedStream`.
- Artifact consumption now explicitly finalizes closeable chunk iterators,
  including retained generators after processing failure.
- Probe-server partial startup now cleans its listener; minimal sequential stop
  attempts independent cleanup actions and prevents repeated/reentrant release.

The minimal patch added 29 regressions and passed the full suite with
7862 passed, 13 skipped and 11 warnings, plus typecheck, diff check and strict
validation. See [minimal-fix-verification.md](../own-http-response-and-probe-server-lifecycles/minimal-fix-verification.md)
for complete logs, limitations and intermediate unsuccessful runs.

Pre-acquisition HTTP preparation, ordinary-response shared ownership, actual
serving-loop readiness, full concurrent stop coordination, worker-safe deferred
release, retained incomplete outcomes and expanded network-origin enforcement
remain pending in the delegated change. They are not prerequisites for Phase 7
of `eliminate-unsafe-direct-descriptor-closes`. The original change still must
complete its own remaining in-scope audit and release checks; neither the minimal
repairs nor this delegation constitute Phase 7 acceptance.

## Original preflight findings

The findings below describe the state before the partial repairs above; they
remain historical context, not a claim that every original defect still exists.

- `docker/versioning/transports.py::build_transports._ProductionHttp.request`
  acquires response streams through `urllib.request.urlopen`. Normal responses
  use their native context manager rather than a filesystem owner. The
  `urllib.error.HTTPError` branch reads the error response body but neither
  explicitly closes it nor transfers it to an owner. This error-response
  lifecycle is not characterized by the named Phase 1–6/6B migrations.
- `docker/versioning/artifact_cache.py::HttpStreamingTransport.fetch_chunks`
  also acquires a response through `urlopen` and relies on its native context
  manager. Generator early termination, operation-plus-release failures and
  ownership of this HTTP response need characterization before migration or
  acceptance as an equivalent owner. The Phase 6 artifact-cache function list
  does not include this transport method.
- `docker/networking.py::HostProbeServer.start/stop` acquires a
  `http.server.ThreadingHTTPServer` and releases its listening socket through
  `server_close()`. This is a resource-release candidate beyond literal
  `.close()` matching, not a proven non-descriptor operation. Its shutdown,
  thread-start failure, independent cleanup and repeat-release contracts are
  not among the named migrations. This preflight does not claim that every
  native server cleanup path leaks; it identifies an unclassified family.

The existing recorded `subprocess.run` ownership classification does not by
itself prove these HTTP/socket lifecycles. No blanket stream, stdlib or
name-based exemption has been added.

## Deterministic HTTPError reproduction (before repair)

This records the original failing reproduction; the repaired buffered path now
closes the body and is covered by the follow-up regressions.

Executed using the project virtual environment without network access or an
OS descriptor acquisition. A retained `HTTPError` contains an `io.BytesIO`
body; `urlopen` is patched to raise that exact error. The production transport
returns its expected 404 response, but the body is still open at return.
Explicit final cleanup disposes of the descriptor-less double, independently
of the assertion outcome.

```python
import io
from unittest.mock import patch
from urllib.error import HTTPError
from docker.versioning.transports import build_transports

body = io.BytesIO(b'not found')
error = HTTPError('https://example.invalid/resource', 404, 'Not Found', {}, body)
try:
    with patch('urllib.request.urlopen', side_effect=error):
        result = build_transports(no_cache=True).http.request('GET', error.url)
    print(f'Returned status: {result.status}', flush=True)
    print(f'Returned body: {result.body!r}', flush=True)
    print(f'HTTPError body closed after request: {body.closed}', flush=True)
    assert body.closed, 'HTTPError response body remains unreleased after production request returns'
finally:
    error.close()
```

Complete stdout/stderr and exit status:
`/tmp/docker-constructor-phase7-http-preflight.ebMIxU.log`.

```text
Returned status: 404
Returned body: b'not found'
HTTPError body closed after request: False
AssertionError: HTTPError response body remains unreleased after production request returns
Preflight runner exit status: 1
```

This is an unsuccessful preflight demonstrating missing explicit response
release, not a passing verification or a real-network fd-leak measurement.
The full pytest, unittest-discovery and final release checks have not been run
for Phase 7. Previous green results remain historical.

## Phase 7 continuation

Resume this change's Phase 7 audit and release checks for the remaining scope,
recording the network-resource families above as delegated rather than unresolved
blockers or proved-safe releases. Do not require completion of
`own-http-response-and-probe-server-lifecycles` before accepting the remaining
Phase 7 obligations. Its own tasks and acceptance criteria govern the delegated
work independently; unrelated newly discovered defects still require explicit
classification and scope decisions.

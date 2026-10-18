# Phase 7 verification

## Checked state and scope

Production source: revision `c3d97183f91271d9c10733a61495b83a67350850`,
unchanged during this session. New implementation consists only of
`tests/enforcement/release_inventory.py` and `test_release_inventory.py`.
The generated [release inventory](phase7-release-inventory.json) includes SHA-256
fingerprints for all 106 production Python files, all resolved process dependency
edges/public signatures/domain fields, 152 close references (including deferred
callables and a backend default), eight implicit descriptor calls, 21 process
ledger entries and 69 factory candidates. References/candidates are not all
releases or acquisitions; the records deliberately distinguish these concepts.

Environment: Linux x86_64; project `.venv`, CPython 3.14.7. No Docker daemon,
network service or built container was required. Temporary complete stdout/stderr
logs were retained for diagnosis; no temporary path is a durable evidence link.
Earlier phase evidence and earlier results in `phase7-alias-verification.md` are
historical, not substituted for the fresh checks below.

## 7.3–7.4: production-site review and convergence

`release_inventory(_sources())` scans every production file, without limiting
scanning to historical function names. It applies the existing origin-resolved
fdopen, immediate-mkstemp-adoption and duplicate-adoption checks to **every**
function, and the complete-tree process proof to the same source mapping.
Current results: zero implicit descriptor violations and zero process convergence
errors. The new-module regression demonstrates that assigned factory aliases
are inventoried and unsafe fdopen/mkstemp use is rejected.

Every close reference's exact path, line, enclosing scope, spelling, resolved
origin and call-versus-reference status is in the generated inventory. The
following review supplies its release disposition; it does not turn unresolved
receivers into automatically proven owners:

| Production references | Review / release authority |
| --- | --- |
| `filesystem/operations.py::PosixDescriptorOps.close`, `transactions/posix.py::PosixFileOps.close` | Two low-level POSIX primitive calls. |
| `filesystem/descriptors.py::OwnedDescriptor.close` | `_state` becomes `_RELEASED` before `_ops.close`; repeat release is inert. |
| `filesystem/streams.py::OwnedStream.close` | Synchronized terminal claim, underlying release outside lock; winning outcome retained by process coordinator. |
| `transactions/capabilities.py::FileCapability.close` | `_closed=True` before backend release; ordinary mapping remains local. Equivalent shared terminal owner, not a consumer exemption. |
| Other `filesystem/descriptors.py` and owner context exits | Calls/callbacks on the same retained descriptor/capability; child tracked before parent release. No raw principal handoff close remains. |
| `transactions/capabilities.py` other references | Directory capability delegates to `_descriptor`; adoption/open rejection releases the prepared local owner. Context exits use that authority. |
| `transactions/locking.py`, `transactions/regular.py` | Shared local owner or validated capability; `_close_fd`/nested discard callback take the stored owner rather than reconstructing it. Independent cleanup uses the existing accumulator. |
| `npm_environment/publication.py`, `storage.py` | Shared descriptor/capability calls. `handle`/`workspace` come from DirectoryDescriptor child factories; nested identity-lock callbacks retain their existing capability/directory. |
| `versioning/artifact_cache.py` in-scope file paths | Local descriptor owners, secure directory capabilities and retained identity-lock capabilities; file-object callbacks below do not own the descriptor. Network generator finalization is delegated separately. |
| `versioning/build_cache.py` | Individual descriptor owners; aggregate BuildCacheState retains five private owners; `_BuildStorageHandle` uses returned capability; ConstructorProjectBuildLock retains its original capability and generation directory; recovery closes the returned BuildCacheState. No reset-per-cleanup authority. |
| `versioning/build_cleanup.py` | Shared descriptor owners and returned directory capabilities; deferred cleanup captures these capabilities. |
| `versioning/build_snapshot.py`, `project_state.py` | Local descriptor owners; ValidatedProjectState delegates to `_owner` and its exits reuse that owner. |
| `versioning/cache_storage.py` | DirectoryDescriptor factory/child results (`entry`, `current`, `previous`, `capability`); paired file/directory cleanup retains independent owners. |
| `versioning/build_materialization.py`, `rendering.py`, `cache.py` | Shared descriptor releases plus borrowed file-wrapper closes, detailed below. |
| `process.py::_release_safe.release` | Existing private prepared stream owner, not a new wrapper. Descriptor-backed reader stop barrier precedes release. `_continue_cleanup` retains that same owner and action resources until settlement. |
| `versioning/host_presentation.py::HostPresentationSession.shutdown` | Non-descriptor mailbox close: mailbox is constructed as PresentationMailbox; close seals admission and notifies its condition, not an OS resource. |
| `npm_environment/execution.py::assemble.begin_npm_execution` | Non-descriptor ExitStack close of the entered host activity scope; no child pipe release authority. |
| `versioning/effective.py::Filesystem.__init__` | `os.close` is a stored default callable, not a release call. Existing facade retains injection compatibility; no invocation was found. |
| `_owned_file.py::_FileRelease.close`, `networking.py::HostProbeServer.stop` | Delegated origins, **not passing release classifications**. See below. |

There are five retained direct descriptor-release implementations: two primitives
and three terminal shared-owner internals. The third permitted category,
verified principal handoff, has no remaining raw release call here. Consumer
owner calls and `closefd=False` wrapper closes are not additional raw-close
categories. No bare consumer raw release, implicit fdopen adoption, raw-fileno
second owner, per-cleanup pipe adoption or unsafe live-reader release was found
in the reviewed production sites.

### All fdopen and mkstemp sites

The eight exact call expressions and lines are generated, not transcribed:

| Site | Owner and ordering |
| --- | --- |
| `artifact_cache.py::LocalCacheFilesystem.read_bytes`, `inspect_and_digest` | `fd_owner.fd`, `rb`, `closefd=False`; wrapper cleanup precedes the sole descriptor release. |
| `build_cache.py::publish_verified_blob` | `fd_owner.fd`, `wb`, `closefd=False`; flush/fsync and wrapper release precede descriptor release/publication. |
| `build_materialization.py::materialize_artifact` | `mkstemp` transfers immediately to `output_owner`; `fdopen(output_owner.fd, wb, closefd=False)`; independent temporary-entry cleanup, no publication after failure. |
| `rendering.py::write_effective_inventory` | `mkstemp` transfers immediately to `owner`; text `fdopen(owner.fd, closefd=False)`; close/release before replace. |
| `cache.py::DiskCache.set` | returned `open_private_entry` fd transfers to `owner`; text `fdopen(owner.fd, closefd=False)`; wrapper/owner failure prevents publication. |

Thus six wrappers have external live owners and both mkstemp fds transfer
immediately. The raw-fd acquisitions in npm execution/tree/publication, runtime
artifact verification, private build context, artifact/build cache, snapshot and
project state remain covered by the focused Phases 1–6 lifecycle matrices.
Subprocess factories/PIPE aliases, prepared stream transfer, managed helper,
shared staged deadline, reader drain/stop barrier, synchronized release and
retained incomplete continuation are generated in the process ledger and tested
by the integrated mutations. `fileno` in the process reader is borrowed access,
not an adoption or independent release.

### Delegated origins — no passing ownership classification

The `factory_candidates` array intentionally retains complete-tree candidates,
including backend descriptor opens and non-file `read_bytes` methods; candidate
spelling alone does not classify a resource. Review found these delegated groups:

* [own-http-response-and-probe-server-lifecycles](../own-http-response-and-probe-server-lifecycles/proposal.md):
  buffered `transports.py::build_transports._ProductionHttp.request` urlopen and
  HTTPError response; `artifact_cache.py::HttpStreamingTransport.fetch_chunks`
  urlopen/HTTPError and `_materialize_one` iterator finalization;
  `build_materialization.py::UrllibStreamingTransport.stream` opener response;
  `networking.py::HostProbeServer.start/stop` ThreadingHTTPServer, listener proxy,
  server_close and worker coordination. These are not proved safe by this gate.
* [own-local-file-stream-lifecycles](../own-local-file-stream-lifecycles/proposal.md):
  `_owned_file.py` proxy and the five reader origins in runtime_installer
  (`read_projection`, `read`), cache (`get`), build_materialization (`_verify_hit`)
  and verification (`_load_build_projection`). Also native Path read_text/read_bytes
  families inventoried in networking, constructor_cli, configuration validation,
  pi_assembly, runtime_verification, evidence, inventory, confinement,
  orchestration, npm validation and smoke. No native-reader or staged-proxy
  structure test is counted as a passing release classification here.
* `runtime_installer.py` tarfile input is an explicit `io.BytesIO` in-memory
  object; extraction's encapsulated local-file operations remain in the native
  family, not a new application raw-fd lifecycle.
* Fully encapsulated synchronous subprocess.run calls are the separately
  approved library-owned boundary in `phase7-alias-verification.md`, not a new
  raw-close category or permission to supply unowned live application streams.

No additional unknown resource family was found. These are origin-specific
allocations, not exclusions of their containing modules.

### Bounded analysis limitations and actual-site disposition

Task 7.1 does **not** prove arbitrary owner identity/liveness through containers,
invoked closures, callbacks or arbitrary Python control flow. General analysis
remains deferred to `introduce-ownership-language`; task 7.2 is cancelled, not
implemented. The inventory lists callback references even where the bounded
local gate cannot establish their receiver. Actual retained callbacks were
reviewed through their enclosing owner construction/storage and focused fault,
repeat-cleanup and handoff tests: directory close_parent, identity-lock release
closures, regular-file stored-owner discard, BuildCacheState owner tuple,
process private-owner release closure and publication/cache file wrappers.
None was accepted solely because its name resembles an owner. These are
source-state-specific reviews plus regression evidence, not a claim of sound
whole-program analysis. Unknown future receivers still require review/proof.

## 7.5: API, dependency and guidance evidence

`architecture_report` generates the full dependency edge list, exact process
surface/signatures and adapter signatures/fields in the inventory. Executable
assertions in test_release_inventory bind the complete-tree coverage, implicit
origin checks, returned owner annotation and LifecycleOutcome compatibility.
The existing filesystem/process/adapter API and integrated convergence suites
bind exact enums, frozen records, signatures, preparation, single collection,
strict action transport, negative imports and retained cleanup observations.

DAG: consumer → docker.process → explicit docker.filesystem submodules;
consumer filesystem imports remain explicit. No filesystem process/domain
policy, generic process Docker/npm/versioning policy, reverse dependency or
runtime-installer process dependency. The filesystem addition is bounded
OwnedStream (adoption, borrowed stream, terminal state, close/context exit),
not a process API or changed raw-fd file-wrapper rule.

`run_captured(argv, *, policy)` returns the authorized
`tuple[OwnedProcess | None, LifecycleOutcome]`; its inputs and the domain fields
`return_code, errors, stdout, stderr` remain unchanged. The generated process
surface includes launch/capture/stream/shutdown, cleanup_status/wait_cleanup and
strict policy/action/value records. Local completion and failure presence are
independent; incomplete cleanup retains the same owners/continuation and only
status/wait observation can report late settlement. No second collection,
exception snapshot, late callback, fresh budget or external-success inference.

Executable documentation/guidance coverage: enforcement lifecycle-doc,
guide-link, process-guidance and integrated convergence tests execute examples,
resolve concrete test links, bind all README variants/AGENTS/config and reject
unprotected launch, raw fileno ownership, closing beneath live readers,
whole-shutdown overrides and per-stage fresh budgets.

## Verification commands and results

New test-source fingerprints (production fingerprints are in the inventory):

* `tests/enforcement/release_inventory.py` SHA-256
  `9db1cfb1a2221c224041fe787c5c6d9f5c8b8cc01ddcfdb2a8bfbe6607776b9e`
* `tests/enforcement/test_release_inventory.py` SHA-256
  `5dc794b531850e40e388d2d27407ab1529be8620d0c7f4b63301bc295a4940b2`

### Historical pause: unsuccessful task 7.6 selection (resolved below)

Exact command (complete stdout/stderr captured temporarily, status preserved):

```bash
.venv/bin/python -m pytest tests/test_descriptor_close_phase6_*.py tests/test_owned_process*.py tests/test_npm*.py tests/test_constructor_build*.py tests/test_constructor_runtime*.py tests/test_build_context_confinement.py tests/test_constructor_project_state*.py tests/test_transactions*.py -n 8 --dist loadfile --tb=short -q
```

Exit **5**, `no tests ran in 0.49s`. Diagnosis: the shell pattern
`tests/test_owned_process*.py` has no matching file and was passed literally.
This is a test-selection error, not an observed production regression, and is
**unsuccessful verification**. Implementation paused without checking task 7.6.
The user subsequently authorized correcting the selection and continuing;
current results are recorded below, without replacing this unsuccessful run.

### Pre-test-addition baseline (not final acceptance)

Before adding the two inventory test files:

* `.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q`:
  exit 0, 7,897 passed, 13 skipped, 18,026 subtests passed, 11 warnings, 52.80s.
  Warnings: Python multiprocessing fork in a multithreaded process, eight
  build-lock/bootstrap cases and three transaction locking cases. No warning
  suppression or test exclusions.
* `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'`:
  exit 0, 6,465 tests in 177.064s, OK, 13 skipped. Expected negative-fixture
  CLI/installer diagnostics appear in the complete output; they are not runner
  failures. Pytest-only tests are not covered by unittest discovery.

These results describe the unchanged production revision and pre-addition test
state; they do not complete the post-edit release gates. An initial new-inventory
focused run passed two tests in 124.07s; subsequent caching of AST alias maps and
factory-candidate inventory changes were verified by the final enforcement run
below. No production code was changed to obtain passing results.

Final post-edit checks are recorded below. Initial exploration checks were
successful but are not substituted for final verification after new tests.

* `.venv/bin/python -m pytest tests/enforcement -n 8 --dist loadfile --tb=short -q`:
  exit 0; 296 passed, 669 subtests passed in 26.84s; no reported warnings/skips.
  Includes complete-tree process/implicit release inventory, API, dependency,
  examples, guide links, guidance and violating new-module fixtures.

Reproduce the generated inventory from the repository root:

```python
# .venv/bin/python - (execute this block on stdin)
import json
from pathlib import Path
from tests.enforcement.release_inventory import release_inventory
from tests.enforcement.test_filesystem_convergence import _sources
report = release_inventory(_sources())
assert report['errors'] == report['implicit_descriptor_errors'] == []
Path('openspec/changes/eliminate-unsafe-direct-descriptor-closes/'
     'phase7-release-inventory.json').write_text(json.dumps(report, indent=2) + '\n')
```

Generation exited 0: 106 files, 152 close references, eight implicit calls,
21 process ledger rows, 69 factory candidates, no process/implicit errors.

## Resumed release verification

Checked revision: `3bebc34b1632d443431e58abe03ffafdfb8743c3` (clean source
state at resumption). Environment remains Linux x86_64, `.venv` CPython 3.14.7.
No implementation/test source changes were needed; only task checkboxes and
this evidence record change during resumption. All full logs are captured
locally until diagnosis/evidence preparation completes; runner statuses are
preserved. Subsequent evidence-only edits do not change the checked code.

### 7.6 — corrected focused characterization

```bash
.venv/bin/python -m pytest tests/test_descriptor_close_phase6_*.py tests/test_process*.py tests/test_npm*.py tests/test_constructor_build*.py tests/test_constructor_runtime*.py tests/test_build_context_confinement.py tests/test_constructor_project_state*.py tests/test_transactions*.py -n 8 --dist loadfile --tb=short -q
```

Exit 0: **4,092 passed, 4 skipped, 7,541 subtests passed, 11 warnings in
41.02s**. The corrected process pattern matches existing test files; no failing
test was excluded. Warnings are multiprocessing fork-from-multithreaded-process
DeprecationWarnings: three transaction-lock cases and eight build bootstrap/lock
cases. No warning suppression.

These suites retain the characterized lockfile/tree bytes and digests, artifact
integrity, context contents, paths/identities/modes, domain errors, lock scope,
publication/durability, output/tails/decoding/redaction, diagnostics, cleanup
ordering, total/stage budgets, reserved cleanup, incomplete local/helper
observations, reader draining/barriers and real-pipe/fd-reuse safety. No drift
was observed beyond the authorized lifecycle/API corrections already covered by
the regression expectations. This is regression comparison, not a new universal
liveness proof; the delegated families and analysis limits above remain.

### 7.7 — typecheck

`ty check docker --python-version 3.14 --output-format concise`: exit 0,
`All checks passed!`.

### 7.8 — explicit unittest discovery

`.venv/bin/python -m unittest discover -s tests -p 'test_*.py'`: exit 0,
**6,465 tests in 176.102s, OK (skipped=13)**. The project virtual environment
supplies `python`. Expected negative-fixture CLI/installer diagnostics occur
in full output; they are not runner failures. Pytest-only cases require the
separate complete pytest gate below.

### Complete pytest and green-loop

`.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q`: exit 0,
**7,899 passed, 13 skipped, 18,026 subtests passed, 11 warnings in 52.40s**.
Warnings are the same fork-from-multithreaded-process DeprecationWarnings
(three transaction-lock and eight build bootstrap/lock cases), not suppressed.
This includes the two new inventory tests and all enforcement/example tests.

`PATH="$PWD/.venv/bin:$PATH" PYTEST_ADDOPTS='-n 8 --dist loadfile --tb=short -q' npx pi-green-loop check --feedback`:
exit 0, `pi-green-loop: all checks passing`. The wrapper reports no separate
result counts; explicit project checks above remain the acceptance evidence.

### 7.9 — whitespace check

Initial `git diff --check`: exit 2, one new blank line at EOF in this evidence
file. This formatting-only issue was corrected; no code/test change was needed.
Final `git diff --check`: exit 0, no output (clean).

### 7.10 — strict change validation

`openspec validate eliminate-unsafe-direct-descriptor-closes --strict`: exit 0,
`Change 'eliminate-unsafe-direct-descriptor-closes' is valid`.

All active Phase 7 gates passed for the checked implementation. Delegated
network/native-file delivery and deferred general liveness analysis remain
outside this change's acceptance boundary; task 7.2 remains cancelled, not
implemented. No changes were staged, unstaged or committed during resumption.

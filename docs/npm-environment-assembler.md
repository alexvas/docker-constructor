# Locked npm environment assembler

The locked npm environment assembler is the single consumer-neutral,
deterministic-input, host-evidenced boundary for materializing exact npm
dependency closures from reviewed roots and standard `package-lock.json` v3
files. It is implemented under `docker/npm_environment/`.

The generic launch-centered process API is documented in
[process-lifecycle.md](process-lifecycle.md). Streaming execution and captured
cleanup clients delegate their process lifecycle to that owner; Docker,
redaction, error translation and staging policy remain in assembler adapters.

## Supported lock subset

The assembler accepts only exact root package versions and a matching
`package-lock.json` with `lockfileVersion` 3. Every accepted package node is
an HTTPS registry node with an exact version, a resolved URL, optional valid
SRI integrity, and a safe deterministic installation path; `file:`, `git:`,
`link:`, `workspace:`, and `bundled` nodes are rejected.

- Non-manifest registry packages may omit `integrity` only when they have an
  exact version and a validated credential-free HTTPS registry `resolved`
  URL. Each such package is recorded explicitly as integrity-less.
- Traversal, unsafe executable-path, malformed SRI, missing SRI, and
  unknown-field inputs are rejected before network, Docker execution, cache
  mutation, or publication.
- The project manifest node at `packages[""]`, every reviewed root package
  entry, and every remaining transitive package entry use separate closed
  accepted-field sets.

## Fixed npm policy

The container runs `npm ci` with exactly the fixed policy flags
`--ignore-scripts --no-bin-links --no-audit --no-fund --loglevel=http`.
The HTTP logging mode was accepted by the pinned npm 11.16.0 research because
its live observations remained useful and sanitizable; it is part of the
canonical policy digest and not a user-facing override. Lifecycle scripts
never execute, even when a locked package declares an install script, and no
reviewed root's `bin` metadata causes the assembler or npm to create an
executable link. `engine-strict` remains disabled; only `engines.node`
declarations attached to reviewed roots are authoritative compatibility
constraints and are enforced before effects.

npm's own request and retry behaviour is bounded by reviewed finite limits
rendered only into the assembler container environment: a 300000 ms request
timeout (`npm_config_fetch_timeout`), 3 retries (`npm_config_fetch_retries`),
a 10000 ms minimum retry delay (`npm_config_fetch_retry_mintimeout`), and a
60000 ms maximum retry delay (`npm_config_fetch_retry_maxtimeout`). A
reviewed total assembly duration of 1800 seconds is included in the
canonical policy identity. Enforcement of this constructor-owned outer
deadline is implemented in Phase 3 of bound-pi-assembly-execution. These
limits are fixed reviewed policy (no user-facing override) and are folded
into the canonical policy digest, so changing any of them changes assembler
identity and invalidates outputs assembled under the prior policy.

## Trust model

The assembler runs from a pinned immutable Node image reference — a bare
`sha256:<64 lowercase hex>` digest or a repository-qualified
`<registry>/<repository>@sha256:<64 lowercase hex>` (optionally with a
descriptive tag). A tag-only or otherwise mutable reference is rejected
before any cache, staging, executor, or Docker activity. The container runs
as the invoking host UID/GID (or `0:0` under rootless Docker), with a
private HOME, narrow read-only inputs, an opaque download cache, and one
writable staging output. npm verifies locked SRI when present; when an
accepted lock entry omits SRI, pinned npm's native registry integrity
behaviour applies and the omission is recorded in evidence. The assembler
does not claim that an integrity-less lock entry cryptographically pins the
downloaded tarball bytes.

## Evidence and identities

Side-effect-free preflight returns an immutable validated input and a stable
`AssemblerInputIdentity` derived from canonical reviewed roots, the exact
lockfile-byte digest, and the assembler identity (image digest, Node and npm
versions, script and policy digests, and platform). `AssemblerInputIdentity`
identifies inputs only and is not a content address for assembled bytes.

After independent post-install validation, the assembler derives a canonical
output-tree digest and a canonical assembler-evidence-body digest, then
derives `AssembledOutputIdentity` from
`(assemblerInputIdentity, canonicalTreeDigest, assemblerEvidenceDigest)`.
Different assembled trees or evidence bodies always produce different output
identities. The immutable evidence envelope and the successful
`AssemblyResult` bind the input identity, tree digest, evidence digest, and
output identity together with the complete closure, optional omissions,
integrity-less records, keyed reviewed-root metadata, fixed flags, and
canonical tree hashes. Host evidence records the bytes actually published.

## Consumer boundaries

The result is consumer-neutral. It identifies the environment root, both
identities, both digests, roots, validated executable declarations and
`engines.node` metadata keyed by reviewed-root package identity and lock
path, the complete package closure, image digest, asserted tool versions,
policy and script digests, platform, input hashes, fixed flags, optional
omissions, integrity-less records, canonical output-tree hashes, and the
evidence path. It contains no generated executable link, no consumer launcher,
no Pi layout, no extension settings, and no lock-refresh behaviour. Dependent
changes own launcher location, contents, layout, retention, and UX by
consuming only the public result boundary and never npm cache internals.

## Cache recovery

The assembler keeps one owner-private namespace per assembler identity with
an opaque, disposable npm download cache that is never authority. Immutable
published environments live only under their `AssembledOutputIdentity`. A
non-authoritative input-identity index may reference zero, one, or multiple
output identities; membership alone never establishes a cache hit. Reuse
requires no-follow tree verification plus recomputation of the input
identity, tree digest, evidence digest, and output identity. A corrupt or
substituted published candidate is never reused; recovery happens only
through locked replacement assembly. A corrupt committed output is
quarantined, never followed or deleted, and republished at the same identity.
Removing the npm download cache affects performance only.

## Subprocess stream ownership and shutdown

`DockerRunExecutor.run_streaming` launches one `OwnedProcess` and collects once
through typed stream callbacks. Generic ownership prepares one filesystem
`OwnedStream` per pipe before acquisition and owns readers, deadline, local
signals/reap, safe release and retained continuation. Adapters do not adopt raw
pipes, reconstruct owners or run independent reader/supervisor/reap loops.
The public standalone `collect_streams` arbitrary-byte API remains supported;
it is not the subprocess adapter's lifecycle path.

The domain compatibility action preserves local terminate → named-container
removal → local wait/reap. Its helper has a separate default-policy owner and
fits the remaining stage allocation. Three grace intervals are allocated to
removal and three to local cleanup within the same six-grace total; concurrent
calls do not restart clocks or requests. This is not a Docker wrapper or a new
cooperative container-stop policy. Container absence/exit interpretation,
identity and staging/publication remain domain responsibilities.

`run_captured(argv: Sequence[str], *, policy: LifecyclePolicy)` retains its input
parameters and returns `tuple[OwnedProcess | None, LifecycleOutcome]`, never a raw
Popen or pipes. LifecycleOutcome still has return_code/errors/stdout/stderr.
Ordinary/unexpected failures come from the sole returned generic value; bytes
are decoded with replacement and exact selected failures are retained. Prepared
factory/setup failure returns the same failed owner; earlier preparation may
have no owner. Missing streams are controlled setup failures, not empty success.
Late cleanup never changes delivered domain output/errors. The configured client
label for an ordinary final-reap timeout remains domain diagnostic policy, not a
new generic provenance field or reclassification of helper failures.

For a short captured command, consume the decoded outcome. The returned owner
is for cleanup observation, not for reading pipes or collecting a second time.
This example uses Python so it can run without Docker:

```python
import sys

from docker.npm_environment.lifecycle import LifecyclePolicy, run_captured

owner, outcome = run_captured(
    (sys.executable, "-c", "print('ready')"),
    policy=LifecyclePolicy(grace_seconds=5),
)
assert outcome.errors == []
assert outcome.return_code == 0
assert outcome.stdout == "ready\n"
assert owner is not None and owner.cleanup_status.complete
```

Authority is selected before domain projection. Ordinary deadline cleanup errors
remain secondary to AssemblyTimeoutError. Unexpected join defects keep authority
even after timeout; actual control flow always propagates with exact first
identity after independent cleanup. Local non-control-flow READ may use the
established StreamReaderFailure mapping; JOIN/RELEASE/ACTION and READ_FAILED alone
are not reader authority. Helper kinds are explicit and parent context is
ACTION/None, never a main-reader stream.

Secondary process-cleanup note prefixes are exactly `{subject} {operation} failed`.
stream STDOUT/STDERR/None maps to stdout/stderr/process. Phase maps as follows:

| phase | operation |
| --- | --- |
| SETUP | setup |
| READ | read |
| JOIN | join |
| RELEASE | release |
| WAIT | wait |
| ACTION | action |
| CALLBACK | callback |
| REPORTER | reporter |
| COORDINATION | coordination |

Kind, timeout and exception type/text do not affect prefix selection. A rebased
helper uses `process action failed`. Details are separately bounded and
secret/URL-sanitized; no guessed supervisor/deadline labels, additional
reportable failures or duplicate diagnostics are introduced. Callback reporter
messages and incomplete-cleanup observation keep their separate contracts.

Each DiagnosticStream finalizes independently: EOF uses abort=False;
READ_FAILED/ABORTED uses abort=True. A clean stdout EOF is not revised by sibling
failure. Buffered chunks precede terminal notification, and generic raw snapshot
tails are never replayed into a sink. ABORTED does not prove reader stop or allow
closing a descriptor beneath a live reader. Known descriptor-less test streams
may need sole-owner close to wake their reader, with its actual stop confirmed
inside the remaining budget. Incomplete cleanup retains the same owner and is
observed through cleanup_status/wait_cleanup, not another collection.

Interruption has no generic output snapshot. The domain collector independently
attaches bounded sanitized retained_capture for CLI/host reporting, along with
cleanup diagnostics. Facade error translation preserves that selected capture
and bounded notes without a raw cause chain. Removal and staging cleanup still
run. Late failures are new lifecycle observations, not mutation or new output.

## Executable streaming example

The streaming adapter applies domain redaction while collecting output. Supply
known secrets explicitly; callers do not need to construct internal collectors
or pipe owners. Like the captured example, this runs without Docker:

```python
import sys

from docker.npm_environment.execution import DockerRunExecutor

result = DockerRunExecutor().run_streaming(
    (sys.executable, "-c", "print('token=example-secret')"),
    secrets=("example-secret",),
    deadline_seconds=5,
)
assert result.return_code == 0
assert "token=" in result.stdout
assert "example-secret" not in result.stdout
```

## Executable contract scenarios

Keep fault injection in tests rather than in usage examples. Concrete scenarios:

- [Captured same-owner projection](../tests/test_npm_environment_orthogonal_projection.py#L148 "test_captured_projects_returned_errors_and_same_owner"): consume returned errors without recapture.
- [Timeout versus join authority](../tests/test_npm_environment_phase6bc_streaming.py#L72 "test_timeout_projection_preserves_unexpected_join_authority_but_not_ordinary_release"): ordinary release stays secondary; unexpected join stays authoritative.
- [Abort-safe tails and cleanup notes](../tests/test_npm_environment_phase6bc_streaming.py#L231 "test_incomplete_streaming_keeps_abort_safe_tails_and_sanitized_delivered_notes"): retain sanitized diagnostics during incomplete cleanup.
- [Independent EOF and no raw replay](../tests/test_npm_environment_phase6bc_streaming.py#L155 "test_clean_stdout_eof_then_stderr_failure_finalizes_independently_without_raw_replay"): sibling failure does not revise clean stdout.
- [Interruption and staging cleanup](../tests/test_npm_environment_phase6bc_streaming.py#L424 "test_assembly_preserves_every_control_flow_family_and_cleans_staging"): preserve control-flow identity and cleanup diagnostics.
- [CLI interruption capture](../tests/test_retained_tail_phase6.py#L1441 "TestInterruptionEndToEnd.test_text_interruption_reports_selected_tail_once"): report the domain-selected retained tail once.

See [filesystem-lifecycle.md](filesystem-lifecycle.md) for descriptor release and
[process-lifecycle.md](process-lifecycle.md) for executable generic API contracts.

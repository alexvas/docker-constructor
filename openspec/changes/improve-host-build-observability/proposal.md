## Why

Host-side build materialization can spend many minutes acquiring artifacts, waiting for coordination, or running locked npm assembly while the interactive CLI shows only a coarse `started` line. Users cannot distinguish healthy silent work, registry retries, lock contention, or a stalled subprocess without inspecting processes and containers manually, and transport failures omit the logical asset and useful nested exception classification.

## What Changes

- Report the full host-side pipeline through presentation-neutral operational activity events alongside the unchanged coarse phase lifecycle: release/build-artifact acquisition, coordination-lock wait, cache lookup, assembler-container startup, `npm ci`, validation, publication, and Docker transition.
- Add configurable interactive heartbeat presentation with `interactive`, `lines`, and `off` modes; report elapsed time, applicable stdout/stderr silence, last activity, and remaining fixed deadline without claiming unavailable percentage progress or treating silence as inactivity. Diagnostic silence applies only to operations that declare an expected diagnostic stream; host downloads instead report observed transport progress, and npm is never described as actively downloading from silence or human-readable output alone.
- Render selected redacted npm warnings, errors, retries, timeouts, and status diagnostics live while keeping Constructor lifecycle as typed control events; serialize transient progress as clear → diagnostic → restore.
- Run a bounded research spike against pinned npm 11.16.0 to determine whether `loglevel=http` provides useful, timely, sanitizable cache-hit, cache-miss, retry, and timeout observations; adopt it as reviewed logging policy only if explicit acceptance criteria pass.
- Preserve exact-repeat coalescing while adding conservative interactive numeric diagnostic updates: when otherwise identical diagnostics differ in exactly one numeric token, replace the mutable slot without a repetition suffix; keep each changed value as a separate durable line in `lines` mode and retain the original sanitized sequence in bounded failure tails.
- Distinguish diagnostics that contained different hidden URLs with session-keyed opaque fingerprints computed after removing userinfo, query, and fragment while preserving normalized host, path, and any explicitly supplied port; never render, persist, or report those fingerprints.
- Report cumulative host-download byte counts whenever the existing boundary yields observable chunks; permit omission only for compatible transports that cannot expose chunks without semantic changes, and never require content length, percentages, transfer rates, Docker network inspection, or filesystem/CPU probes.
- Improve all host-pipeline failure reports by reusing the existing bounded redacted diagnostic tail.
- Identify failed logical build artifacts and Pi release assets, include a bounded chain of exception type names, and never expose URL paths, query strings, userinfo, proxy details, or exception messages.
- Add an opt-in setting to show normalized network hostnames consistently in live and failure diagnostics; hostname display remains disabled by default.
- Plan an explicit modification to the noninteractive-output specification so `lines` mode has defined behavior instead of accidentally bypassing structured-output and no-live-sink guarantees.
- Preserve the existing locked-assembly timeout, blocking coordination semantics, cleanup behavior, JSON isolation, SDK compatibility, and redaction guarantees; preserve assembler policy identity unless the research gate accepts `loglevel=http`, in which case bind the logging-policy change into a new identity.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `docker-build-output`: Add detailed host activity, heartbeat and transient-line presentation, hostname policy, bounded contextual failure reporting, explicit interactive/noninteractive behavior, and the table-specific contract for local `[output]` settings.
- `local-project-configuration`: Extend the closed set of supported local companion tables with `[output]`, while preserving host-only confinement and reviewed-state isolation.
- `docker-build-reproducibility`: Add presentation-neutral per-asset acquisition activity, safe failure context, and already-observable streamed-chunk progress for the authoritative Pi `SHA256SUMS`, install package, and install lockfile without changing release URL derivation, acquisition/checksum order, request effects, or verification semantics.
- `build-artifact-materialization`: Add mandatory byte progress for already-observable streamed chunks, a compatibility omission for transports that cannot expose chunks without semantic change, and safe logical-asset/transport-type diagnostics.
- `locked-npm-environment-assembly`: Add operational activity reporting and safe selected live npm diagnostics while reusing the existing bounded redacted tail.

## Impact

- Affects host progress/event DTOs, facade rendering and output configuration, ephemeral diagnostic URL identity, build/Pi orchestration, streaming HTTP transports, Pi release acquisition, npm stream dispatch, and failure rendering.
- Depends on `extract-local-project-configuration`; implementation SHALL begin only after that predecessor is implemented, synchronized, and archived so `[output]` extends the prerequisite-owned host-only companion contract.
- Adds no external dependency and does not inspect Docker network counters.
- Extends the internal presentation-neutral event interface without changing `HostPhaseEvent`; existing optional sinks and callers that omit presentation remain compatible.
- Requires deterministic clock/wait injection and tests for event ordering, heartbeat timing, redaction, URL sanitization, exception-chain bounds, output-mode isolation, transient rendering, cancellation, and failure-tail reuse.
- May change the locked npm policy digest and invalidate reuse of prior assembled outputs only if the pinned-npm research gate accepts `loglevel=http`; the recorded research decision determines that implementation branch.

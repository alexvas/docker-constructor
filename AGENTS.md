# Repository agent rules

## Verification evidence

Store verification results and implementation evidence separately from
specification files. Keep durable evidence concise and self-contained: checked
revision or source-state fingerprint, exact commands, relevant environment,
exit statuses, result counts, significant warnings, and limitations. Historical
results describe only their checked source state; changes require fresh checks.

Capture complete test stdout/stderr temporarily for failure diagnosis and
summary preparation, preserving the runner's exit status. Full logs need not
be retained permanently or committed; temporary local files or retention-limited
CI artifacts are sufficient. Do not use container-local temporary paths as
durable evidence links. Summarizing logs must not hide failures or warnings,
replace required checks, or turn a nonzero runner status into a passing result.

## Descriptor and stream lifecycle (mandatory)

Any new code that acquires an operating-system descriptor or an already
fd-owning stream MUST name exactly one `docker.filesystem` owner and release
through that owner:

- **raw integer fd** (`os.open`, `os.dup`, `os.pipe`, `tempfile.mkstemp`, or a
  factory returning an fd): adopt it immediately into `OwnedDescriptor`, with
  constructor inputs prepared before the acquisition;
- **directory**: use the `DirectoryDescriptor` capability
  (`open_secure_path`/`adopt` and its child methods);
- **subprocess coordination**: use `docker.process.OwnedProcess.launch`,
  which prepares one internal `OwnedStream` per captured pipe; never compose
  unprotected launch/adoption/readers or independently signal, wait/reap or
  release its borrowed child/pipes;
- **other already fd-owning stream**: adopt it into `OwnedStream`; do not
  also adopt its `fileno()`;
- **Python file object over a raw fd**: use
  `os.fdopen(owner.fd, ..., closefd=False)` and keep the `OwnedDescriptor` as
  the sole release authority.

Never retry a close, never give one fd two owners, and never rely on garbage
collection or a borrowed stream for release. Preserve an active
operation/domain failure over an ordinary close failure, and attempt every
independent release exactly once. Import explicit `docker.filesystem`
submodules, never the aggregate package.

Select configured `StopPolicy` stages at launch, not by overriding shutdown.
Stage work and child waiting share one total shutdown budget and local-cleanup
reserve. Incomplete cleanup retains the same owners and continuation; observe
it through `cleanup_status` / `wait_cleanup`, without retry or new budgets.
Managed action `done` and `cleanup_complete` are separate observations: local
child exit proves neither external-work success nor settled helper resources.
See `docs/process-lifecycle.md`; process/domain policy never belongs in the
filesystem foundation.

Every new acquisition, transfer, wrapper, or cleanup branch MUST add tests for
operation-plus-close failure, sole-close failure, interruption, and repeat
cleanup, plus independent cleanup, handoff, or concurrency where applicable.

See `docs/filesystem-lifecycle.md` for the full contract and executable
examples. Enforcement checks live in `tests/enforcement/`; add new checks
there instead of extending a file list here.

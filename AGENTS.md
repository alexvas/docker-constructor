# Repository agent rules

## Descriptor and stream lifecycle (mandatory)

Any new code that acquires an operating-system descriptor or an already
fd-owning stream MUST name exactly one `docker.filesystem` owner and release
through that owner:

- **raw integer fd** (`os.open`, `os.dup`, `os.pipe`, `tempfile.mkstemp`, or a
  factory returning an fd): adopt it immediately into `OwnedDescriptor`, with
  constructor inputs prepared before the acquisition;
- **directory**: use the `DirectoryDescriptor` capability
  (`open_secure_path`/`adopt` and its child methods);
- **already fd-owning stream** (for example `subprocess.Popen(..., PIPE)`
  stdout/stderr): adopt it into `OwnedStream`; do not also adopt its
  `fileno()`;
- **Python file object over a raw fd**: use
  `os.fdopen(owner.fd, ..., closefd=False)` and keep the `OwnedDescriptor` as
  the sole release authority.

Never retry a close, never give one fd two owners, and never rely on garbage
collection or a borrowed stream for release. Preserve an active
operation/domain failure over an ordinary close failure, and attempt every
independent release exactly once. Import explicit `docker.filesystem`
submodules, never the aggregate package.

Every new acquisition, transfer, wrapper, or cleanup branch MUST add tests for
operation-plus-close failure, sole-close failure, interruption, and repeat
cleanup, plus independent cleanup, handoff, or concurrency where applicable.

See `docs/filesystem-lifecycle.md` for the full contract and executable
examples. The production-tree convergence gates in
`tests/test_descriptor_close_convergence.py` and
`tests/test_filesystem_convergence.py` enforce this rule.

# Locked npm environment assembler

The locked npm environment assembler is the single consumer-neutral,
deterministic-input, host-evidenced boundary for materializing exact npm
dependency closures from reviewed roots and standard `package-lock.json` v3
files. It is implemented under `docker/npm_environment/`.

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

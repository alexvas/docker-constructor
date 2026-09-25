# Capability: locked-npm-environment-assembly

## Purpose

Provide one consumer-neutral, deterministic-input and host-evidenced boundary for assembling exact npm dependency closures from reviewed roots and standard lockfiles.

## Requirements

### Requirement: Accept only reviewed locked npm inputs
The assembler SHALL require exact root package versions and a matching `package-lock.json` with `lockfileVersion` 3. It SHALL accept only HTTPS registry package nodes with exact versions, resolved URLs, optional valid SRI integrity, safe deterministic installation paths, and supported dependency metadata. The lock project manifest node at `packages[""]`, every reviewed root package entry selected by the plural `RootSpec` inputs, and every remaining transitive package entry SHALL use separate closed accepted-field sets. Each reviewed root SHALL resolve to one exact lock package path, and its functional metadata SHALL be keyed in the immutable DTO and evidence by both canonical root package identity and resolved lock path; metadata from one reviewed root SHALL NOT overwrite, alias, or stand in for another.

The project manifest node SHALL accept only its enumerated project identity and dependency-declaration fields plus `engines`, `license`, `funding`, and `deprecated`. Manifest `engines` SHALL be a closed object containing only optional `node`; when present, `engines.node` SHALL be a non-empty syntax-valid supported strict npm/node-semver range and SHALL then be discarded as non-authoritative. Manifest `license`, `funding`, and `deprecated` SHALL be shape-validated and discarded. Manifest metadata SHALL NOT be substituted for metadata of any reviewed root. Root `peerDependencies` and `peerDependenciesMeta` SHALL be shape-validated on the manifest node, but root peers SHALL NOT be installed as root edges: they are outside the assembler-managed closure and remain the consumer's responsibility, and SHALL NOT appear in the synthesized assembler manifest.

For every reviewed root package entry, `bin` and `engines.node` SHALL be validated functional metadata. Each root `bin` SHALL be a closed object whose keys and values are non-empty strings and whose values are unambiguous safe relative paths; the parser SHALL reject absolute paths, `..` components, empty path components or values, backslashes, and any other executable path that can escape or be interpreted inconsistently. Each root `engines` SHALL be a closed object; when `engines.node` is present it SHALL be a non-empty supported strict node-semver range. Validated executable declarations and validated `engines.node` SHALL be preserved separately for every reviewed root under its package identity/path key. Each root `license`, `funding`, and `deprecated` SHALL be explicitly accepted-and-ignored after shape validation and SHALL NOT affect semantic DTO inputs or assembly output.

For transitive package nodes, `bin`, `license`, `funding`, and `deprecated` SHALL be explicitly accepted-and-ignored after shape validation. Transitive `bin` SHALL be an object with non-empty string keys and non-empty string values. `license` and `deprecated` SHALL each be strings. `funding` SHALL be either a non-empty string, a closed object with required non-empty string `url` and optional non-empty string `type`, or a non-empty array whose entries each use one of those string/object forms. The same `license`, `funding`, and `deprecated` shapes SHALL apply when those ignored fields occur on the project manifest or a reviewed root. Malformed values SHALL be rejected. None of these ignored values SHALL be preserved in a DTO or influence semantic DTO inputs, dependency or placement validation, engine compatibility, assembler execution or npm flags, executable-link creation, tree evidence, or assembled output. Exact byte differences remain represented only by the lockfile digest required by assembler input identity. Ignored `bin` SHALL NOT create executable links.

Transitive `engines` SHALL be accepted-and-ignored metadata after validation. It SHALL be a closed object containing only optional `node`; when present, `engines.node` SHALL be a non-empty string whose syntax is valid under the supported strict npm/node-semver range implementation, including comparator conjunctions and `||` disjunctions exercised by the pinned published Pi fixture. It SHALL then be discarded and SHALL NOT be preserved in DTOs or evidence or influence compatibility decisions, semantic identity inputs, assembler execution, npm flags, executable links, tree evidence, or assembled output. Exact byte differences remain represented only by the lockfile digest required by assembler input identity. npm normally treats dependency engine mismatches as warnings unless `engine-strict` is enabled; Constructor SHALL NOT enable `engine-strict`, so a syntactically valid transitive range SHALL be accepted even when the reviewed Node version does not satisfy it. Constructor SHALL emit no custom transitive-engine compatibility diagnostic. Native output from the pinned npm execution, including any `EBADENGINE` warning npm independently emits, is not suppressed, deduplicated, or promoted into a Constructor diagnostic contract. Only `engines.node` declarations attached to reviewed roots SHALL be authoritative, and every declared reviewed-root range SHALL be enforced. Malformed ranges and unsupported engine keys SHALL be rejected before effects. Every unknown field on the manifest, reviewed-root, or transitive role remains rejected. A non-manifest registry package MAY omit `integrity` only when it has an exact version and a validated credential-free HTTPS registry `resolved` URL. If `integrity` is present it SHALL be valid SRI and remain authoritative for package bytes; if absent, the immutable DTO and validated input SHALL explicitly mark that package as integrity-less by package identity, lock path, exact version, and resolved URL. Missing integrity SHALL NOT be accepted for a node lacking a valid registry URL or exact version. Constructor SHALL NOT claim that an integrity-less lock entry cryptographically pins registry-served package bytes or guarantees byte-identical cold reconstruction; pinned npm's native registry integrity behavior applies, and post-install canonical tree hashes SHALL record the bytes actually published. The assembler SHALL reject root drift, missing closure nodes, traversal, `file:`, git, link, workspace, bundled, unsafe root executable declarations, malformed accepted metadata, or unknown metadata before network or output mutation. Platform-inapplicable optional nodes MAY be omitted only when recorded explicitly.

#### Scenario: Validating a complete registry lock
- **WHEN** exact roots match a complete lockfile-v3 registry closure
- **THEN** preflight SHALL accept the inputs and derive a deterministic assembler input identity

#### Scenario: Rejecting an unsupported lock node
- **WHEN** any lock node uses an unsupported source, unsafe path, malformed SRI, lacks integrity without an exact version and valid HTTPS registry URL, or does not satisfy its parent dependency range
- **THEN** assembly SHALL fail before network, Docker execution, cache mutation, or publication

#### Scenario: Accepting an integrity-less official registry node
- **WHEN** a reviewed-root or transitive package has an exact version and valid credential-free HTTPS registry `resolved` URL but omits `integrity`
- **THEN** preflight SHALL accept it and record its package identity, lock path, version, and resolved URL as integrity-less
- **AND** output evidence SHALL preserve that omission and the canonical hashes of bytes actually assembled
- **AND** SHALL NOT claim the lock cryptographically pins those package bytes

#### Scenario: Preserving executable declarations for multiple reviewed roots
- **WHEN** two reviewed roots resolve to distinct lock package paths and each declares a safe `bin` map
- **THEN** validation SHALL preserve each declaration separately under that root's canonical package identity and resolved lock path
- **AND** metadata from one root SHALL NOT overwrite or alias metadata from the other
- **AND** validation SHALL NOT create an executable link or assign a consumer-specific installation path

#### Scenario: Rejecting an unsafe root executable declaration
- **WHEN** a root `bin` value is empty, absolute, contains `..`, an empty component or a backslash, or is otherwise ambiguous or escaping
- **THEN** validation SHALL fail before network, Docker execution, cache mutation, or publication

#### Scenario: Accepting published transitive metadata
- **WHEN** a published lockfile transitive node contains shape-valid `bin`, syntax-valid `engines.node`, `license`, `funding`, or `deprecated` metadata
- **THEN** validation SHALL accept and discard all five fields
- **AND** SHALL preserve none of their values in the closed DTO or validation evidence

#### Scenario: Proving ignored transitive metadata is inert
- **WHEN** two otherwise identical accepted locks differ only in shape-valid ignored `bin`, `license`, `funding`, or `deprecated` values or syntax-valid ignored `engines.node` ranges
- **THEN** their parsed DTO content, canonical roots and semantic assembly inputs SHALL be equal apart from the exact lock-byte digest
- **AND** the ignored values SHALL NOT change assembler identity, npm flags, executable links, tree evidence, or assembled output
- **AND** assembler input identity MAY differ only because its contract includes the exact lockfile bytes

#### Scenario: Accepting an incompatible transitive Node engine
- **WHEN** a transitive package declares a syntax-valid `engines.node` range, including a range with `||`, that the reviewed Node version does not satisfy
- **THEN** validation SHALL accept and discard that range because `engine-strict` is not enabled
- **AND** SHALL NOT change npm flags, compatibility decisions, or assembled output

#### Scenario: Validating and discarding manifest engines
- **WHEN** the project manifest node declares syntax-valid `engines.node`
- **THEN** validation SHALL discard it without constraining the reviewed Node version or replacing any reviewed root's engine metadata

#### Scenario: Excluding root peer dependencies from the installed closure
- **WHEN** the project manifest node declares `peerDependencies` (optionally with `peerDependenciesMeta`)
- **THEN** validation SHALL shape-validate them and SHALL NOT install them as root edges
- **AND** the synthesized assembler manifest SHALL NOT declare them, so `npm ci` cannot install or require a package outside the validated closure

#### Scenario: Rejecting malformed or unknown role metadata
- **WHEN** a manifest, reviewed-root, or transitive node contains an unknown field, malformed `bin`, `engines`, `license`, `funding`, or `deprecated`, an unsupported engine key, or a syntactically invalid engine range
- **THEN** validation SHALL fail before network, Docker execution, cache mutation, or publication

#### Scenario: Enforcing every reviewed-root Node engine before effects
- **WHEN** multiple reviewed roots declare `engines.node` and the caller-supplied reviewed exact Node version fails to satisfy any one root's range
- **THEN** validation SHALL identify the incompatible root package and lock path and fail before Docker execution, npm, network access, cache mutation, or publication
- **AND** SHALL require all other declared reviewed-root ranges to be satisfied as well
- **AND** SHALL preserve the reviewed Node version as caller-owned configuration rather than deriving a second expected version

#### Scenario: Accepting a pinned published Pi lockfile
- **WHEN** the byte-exact `tests/data/` fixture copied from its recorded immutable published Pi install-lock source is validated for `linux-x64` using roots derived from exact versions of its top-level locked nodes
- **THEN** parsing SHALL accept the complete reachable closure and produce the expected canonical roots
- **AND** every optional omission SHALL be backed by locked platform metadata and have reason `platform-inapplicable`
- **AND** repeated parsing of the same bytes and repeated identity derivation with the same assembler identity SHALL produce equal models and identities
- **AND** the test SHALL read and assert the Pi version from the fixture's own root package metadata
- **AND** fixture provenance SHALL record the exact published source corresponding to that embedded version and SHA-256 of the checked-in `tests/data/` bytes
- **AND** this specification SHALL NOT pin a particular fixture version without reading `/opt/pi` or any installed agent

### Requirement: Separate side-effect-free input preflight from assembly
The assembler SHALL expose a side-effect-free input-validation/preflight operation receiving the exact lockfile bytes, plural `RootSpec` values, target platform, and caller-owned reviewed exact Node and npm versions. It SHALL complete closed parsing, root and closure validation, transitive-metadata handling, extraction of every reviewed root's metadata keyed by canonical package identity and resolved lock path, and satisfaction of every declared reviewed-root `engines.node` range. It SHALL return an immutable validated assembly input binding the exact lockfile digest, canonical roots, platform, reviewed tool versions, validated closure, optional omissions, integrity-less registry-node records, and keyed root metadata. It SHALL perform no Docker execution, network access, cache mutation, staging, identity locking, or publication and SHALL expose no assembled output path or output evidence.

The Docker-backed assembly operation SHALL accept only that validated input together with exact lock bytes and assembler identity inputs whose digest, roots, platform, and reviewed tool versions match its bindings. A mismatch or substitution SHALL fail before effects; assembly SHALL NOT silently reparse different bytes or replace the preflight result.

#### Scenario: Inspecting root metadata before assembly
- **WHEN** side-effect-free preflight succeeds for multiple reviewed roots
- **THEN** the caller SHALL receive all keyed root `bin` and `engines.node` metadata before Docker-backed assembly
- **AND** no Docker, npm, network, cache, staging, lock, publication, output path, or output evidence SHALL exist

#### Scenario: Rejecting substituted preflight input
- **WHEN** Docker-backed assembly receives lock bytes, roots, platform, or reviewed tool versions that differ from the validated input bindings
- **THEN** assembly SHALL fail before Docker or any other effect

### Requirement: Distinguish assembler input identity from assembled output identity
Preflight SHALL derive a stable `AssemblerInputIdentity` only from canonical reviewed roots, the exact lockfile-byte digest, and the assembler identity (which itself binds the assembler's image digest, Node/npm versions, script/policy digests, and platform). The validated closure, optional omissions, integrity-less records, and keyed root metadata SHALL NOT be separate identity components; identity derivation re-verifies them against a fresh preflight result rather than including them in identity semantics. This identity SHALL identify inputs only and SHALL NOT be described or used as a content address for assembled bytes.

After complete tree and evidence validation, assembly SHALL derive a canonical output-tree digest and a canonical assembler-evidence-body digest. The evidence body used for its digest SHALL exclude the enclosing assembled-output-identity field. It SHALL derive `AssembledOutputIdentity` canonically from `(assemblerInputIdentity, canonicalTreeDigest, assemblerEvidenceDigest)`. Different canonical trees or evidence bodies SHALL produce different output identities even when assembler input identity is identical. The immutable evidence envelope and successful result SHALL bind the input identity, tree digest, evidence digest, and assembled output identity.

#### Scenario: Distinguishing reconstructions with identical inputs
- **WHEN** identical validated inputs produce different assembled bytes or different canonical evidence bodies
- **THEN** both reconstructions SHALL retain the same assembler input identity
- **AND** SHALL receive different assembled output identities
- **AND** SHALL NOT overwrite, alias, or validate as one another

### Requirement: Assemble with one pinned standalone npm boundary
The assembler SHALL consume caller-owned reviewed exact Node and npm versions through the validated assembly input. Side-effect-free preflight SHALL already have validated every preserved reviewed-root `engines.node` range against the reviewed Node version and identified any incompatible root by package identity and resolved lock path. The container image reference SHALL be a canonical immutable reference: a bare `sha256:<64 lowercase hex>` digest, `<registry>/<repository>@sha256:<64 lowercase hex>`, or `<registry>/<repository>:<tag>@sha256:<64 lowercase hex>`. In repository-qualified forms the digest is authoritative and an optional tag is descriptive only. A tag-only reference, malformed digest or repository name, uppercase or non-hex digest value, userinfo, or any other mutable reference SHALL fail before cache, staging, executor, or Docker activity. After rechecking the validated-input bindings, it SHALL run a standalone container from the reviewed Node image digest, assert its actual Node and npm versions equal those reviewed values before `npm ci`, and execute `npm ci` in empty private staging with exactly the policy flags `--ignore-scripts`, `--no-bin-links`, `--no-audit`, and `--no-fund`. The synthesized project manifest SHALL copy only `dependencies`, `devDependencies`, and `optionalDependencies` — the dependency classes the validated closure treats as installed root edges — and SHALL NOT copy `peerDependencies`, so `npm ci` cannot install or require a package outside the validated closure. Install-script metadata MAY exist, but lifecycle scripts SHALL never execute. Reviewed-root `bin` metadata SHALL NOT cause the assembler or npm to create executable links; launcher construction belongs to a consumer. The container SHALL run as the invoking host UID/GID — under rootless Docker the invoking host user maps to container UID/GID `0`, so the container runs as `0:0` — receive only narrow read-only inputs, opaque cache access, controlled network policy, and one writable staging output.

#### Scenario: Running under rootless Docker
- **WHEN** the Docker daemon is rootless
- **THEN** the assembler SHALL detect rootless mode via `docker info` and run the container as `0:0`, because the invoking host user maps to container UID/GID `0` in rootless user namespaces
- **AND** the owner-private host bind mounts SHALL remain writable by the container

#### Scenario: Assembling a cache miss
- **WHEN** no fully verified assembled output is selected through the non-authoritative input-identity index
- **THEN** the standalone assembler SHALL install the locked graph in private staging with the fixed policy
- **AND** SHALL NOT build or mutate a consumer image

#### Scenario: Rejecting a mutable or malformed image reference
- **WHEN** the assembler image reference is a tag, a malformed digest, an uppercase or non-hex digest value, userinfo, or any other mutable reference
- **THEN** assembly SHALL fail before cache, staging, executor, or Docker activity
- **AND** SHALL accept only `sha256:<64 lowercase hex>`, `<registry>/<repository>@sha256:<64 lowercase hex>`, or `<registry>/<repository>:<tag>@sha256:<64 lowercase hex>`

#### Scenario: Blocking script execution
- **WHEN** a locked package declares an install script
- **THEN** its package bytes MAY be installed
- **AND** no lifecycle script SHALL execute

### Requirement: Independently validate and publish assembled trees
After the container exits successfully, the Constructor SHALL independently compare the assembled installation paths, package names, versions, dependency closure, optional omissions, filesystem entry types, symlink containment, ownership, permissions, and absence of extras with the validated lock. For every preserved executable declaration of every reviewed root, resolution of its path through the assembled filesystem SHALL remain contained within the assembled environment; a dangling or escaping symlink target SHALL be rejected. This check SHALL validate metadata and installed bytes without creating a launcher or executable link. The Constructor SHALL create a canonical per-entry tree manifest with hashes, write host-owned evidence, remove write permissions, and atomically publish only a fully validated environment.

#### Scenario: Publishing a valid tree
- **WHEN** assembled output exactly matches the validated lock and filesystem policy
- **THEN** the Constructor SHALL atomically publish it under its assembled output identity with canonical evidence
- **AND** SHALL NOT publish or overwrite immutable output storage solely under assembler input identity

#### Scenario: Rejecting unexpected output
- **WHEN** output contains an extra package, wrong version, unsafe symlink, special file, ownership mismatch, or changed bytes
- **THEN** publication SHALL fail and committed environments SHALL remain unchanged

### Requirement: Revalidate and coordinate environment reuse
A published environment SHALL be reusable only after no-follow inspection, complete canonical tree-manifest verification, recomputation of canonical tree and evidence digests, and verification that they and assembler input identity derive the selected assembled output identity. An assembler-input-identity lookup index MAY reference zero, one, or multiple assembled output identities but SHALL be non-authoritative; index membership alone SHALL NOT establish reuse. Concurrent assembly of one input identity SHALL coordinate through a private input-identity lock; cancellation, interruption, or failure SHALL clean mutable staging, preserve prior committed environments, and report structured failure without persisting local network configuration.

#### Scenario: Reusing a valid environment
- **WHEN** an input-index candidate names a published output whose tree digest, evidence digest, input identity, and assembled output identity all recompute and match
- **THEN** assembly SHALL return that existing immutable output without npm or network execution

#### Scenario: Detecting cache corruption
- **WHEN** any published entry differs from its manifest
- **THEN** the environment SHALL not be reused
- **AND** recovery SHALL occur only through locked replacement assembly

### Requirement: Emit a consumer-neutral assembly result
A successful result SHALL identify the environment root, assembler input identity, assembled output identity, canonical output-tree digest, canonical assembler-evidence digest, roots, validated executable declarations and `engines.node` metadata keyed by reviewed-root package identity and resolved lock path, complete package closure, assembler image digest, asserted tool versions, policy and script digests, platform, input hashes, fixed flags, optional omissions, integrity-less registry-node records, canonical output-tree hashes, and evidence path. It SHALL contain no generated executable link, consumer-specific Pi layout, extension settings, build-context, CLI-guard, or lock-refresh behavior.

#### Scenario: Consuming assembled evidence
- **WHEN** a consumer receives a successful assembly result
- **THEN** it SHALL have sufficient immutable paths and evidence to construct its own layout without inspecting npm cache internals

### Requirement: Bound assembler network and process execution
The assembler SHALL apply deterministic reviewed limits to npm registry request duration, retry count, retry delay, and total Docker-backed assembly duration. These limits SHALL be part of the fixed assembler policy identity. Exhausting npm request or retry limits SHALL produce a structured assembler failure containing bounded, redacted npm diagnostics rather than waiting indefinitely. Exceeding the constructor-owned total assembly deadline SHALL produce a timeout-specific structured operational failure.

#### Scenario: Registry request remains unresponsive
- **WHEN** a locked package request does not complete within the reviewed npm request and retry limits
- **THEN** npm assembly SHALL terminate finitely with a structured assembler failure containing bounded, redacted npm diagnostics
- **AND** SHALL NOT require Constructor to classify the npm failure as network-specific
- **AND** SHALL NOT publish the partial environment

#### Scenario: Total assembly deadline expires
- **WHEN** the assembler container remains active beyond the reviewed total assembly deadline
- **THEN** the constructor SHALL terminate the execution and force-remove the named assembler container
- **AND** SHALL report an actionable timeout failure

#### Scenario: Fixed limits affect assembler identity
- **WHEN** any reviewed npm request, retry, or total execution limit changes
- **THEN** the assembler policy identity SHALL change
- **AND** an output assembled under the prior policy SHALL NOT validate as an output of the new policy

### Requirement: Retain bounded redacted assembler diagnostics
Live display, coalescing, and refresh scenarios below describe healthy, promptly serviced presentation. Deduplication and coalescing SHALL be best effort under overload, state reset, or renderer failure: extra groups/repeated diagnostics or absent live output are permitted on those degraded paths. Normal-path coalescing SHALL remain supported and tested. Sanitization, safe identity matching, capture bounds, primary execution results, and cleanup SHALL NOT be weakened by this qualification. The retained tail SHALL remain independent of live delivery and SHALL NOT be reconciled with renderer history. Every final failure report SHALL include its nonempty tail once under an explicit retained-context label warning that live output may be repeated; empty tails SHALL produce no diagnostic section.

The assembler SHALL make ongoing execution observable to an authorized text-mode caller while retaining only the existing bounded diagnostic tail. Before display, return, persistence, or inclusion in a failure report, diagnostics SHALL redact resolved proxy endpoints, trust paths, caller-supplied secrets, and disallowed URL components. Diagnostic collection SHALL NOT grow without bound for a long-running process. Presentation SHALL surface each first safe npm warning, error, retry, timeout, or status line that is admitted to the bounded presentation mailbox. It SHALL coalesce every sequence of identical admitted final safe rendered diagnostic lines regardless of warning, error, retry, status, or timeout classification and SHALL count admitted occurrences including the first. Under mailbox saturation, ordinary diagnostic admission SHALL be best-effort, and a rendered repetition count SHALL NOT claim to include occurrences dropped before admission. Warnings and errors SHALL NOT bypass coalescing or be forced into individual ungrouped live writes. Typed Constructor lifecycle/control events SHALL NOT be treated as diagnostic lines or coalesced. In `lines` mode it SHALL use a fixed one-second monotonic window, emit the first diagnostic immediately, and emit `<diagnostic> (repeated N times)` for a group total `N >= 2` at window end or immediately before a different diagnostic or terminal event; a single-occurrence group SHALL emit no summary. In interactive mode it SHALL maintain one mutable diagnostic slot, initially without a suffix, increment the group total for every identical repeat, and update the slot at the next at-most-one-second TUI refresh using the same canonical suffix. When consecutive diagnostics have identical presentation identity and text except for exactly one changed numeric token, the latest diagnostic SHALL replace that mutable slot without a repetition suffix and reset its exact-repeat count to one; numeric values need not be monotonic. Numeric matching SHALL examine the entire maximal numeric-looking sequence and SHALL NOT extract a valid-looking substring from an invalid sequence. A valid numeric token SHALL consist of one or more ASCII digits followed by zero or more `.` plus one-or-more-digit segments, with neither adjacent boundary an ASCII letter, digit, underscore, dot, plus, or minus. Thus `1`, `42`, `1.2`, and `10.20.30` are valid, while signed `-1`, `+1`, and `-1.2`, incomplete `.1` and `1.`, malformed `1..2`, and identifier-embedded `item1` are invalid in full and SHALL NOT participate in numeric grouping. Surrounding nonnumeric text SHALL be byte-identical. The interactive group SHALL remain mutable until an identity/template mismatch, omission notice, different diagnostic, or terminal event finalizes it; only its latest value SHALL be durably written, and a later identical diagnostic SHALL start a new group. `lines` mode SHALL NOT apply numeric-variant grouping and SHALL preserve each changed numeric value as a separate durable line. Before either retained or live output leaves the collector, the decoded stream SHALL undergo secret redaction followed by boundary-safe URL sanitization, normalized-host extraction, and ephemeral URL-identity derivation. Each complete URL SHALL contribute an ordered fixed-width fingerprint produced by a cryptographic keyed digest with a fresh random presentation-session key. Fingerprint input SHALL remove userinfo, query, and fragment, exclude proxy information, and preserve scheme, normalized hostname, path, and any explicitly supplied port including an explicit default port. Fingerprint order and multiplicity SHALL be preserved. Pending sanitizer state for an ambiguous URL or secret candidate SHALL be capped at 8 KiB, and diagnostic-line assembly SHALL be capped at 64 KiB of decoded UTF-8 text per unterminated line. The collector SHALL continue draining after either limit is reached. A candidate exceeding its limit SHALL emit only `[sanitized oversized token]` and discard its remaining content until a safe token boundary; a diagnostic line exceeding its limit SHALL emit only `[sanitized oversized diagnostic]` and discard its remaining content until newline or stream termination. Processing SHALL resume after that safe boundary. At EOF, reader failure, or cancellation, an unresolved URL or secret candidate SHALL emit only `[sanitized incomplete token]`, never ordinary candidate text, and any pending line SHALL be finalized only through the same bounded sanitizer. The original ordering and occurrences of the resulting URL-free sanitized text and fixed safe replacement markers SHALL enter the existing byte-bounded per-stream tail without grouping; only the structured live branch SHALL carry ephemeral fingerprints and later be grouped for presentation. Session keys and fingerprints SHALL never enter rendered text, retained tails, failure reports, persistence, evidence, or policy identity and SHALL never permit cross-session correlation. Existing tail byte limits and truncation semantics SHALL remain unchanged. Merely secret-redacted pre-projection text SHALL NOT appear in returned, persisted, attached, or failure representations. One facade presentation actor SHALL exclusively own coalescing, presentation deadlines, and host terminal writes through one ordered bounded inbox with independently bounded admission budgets for control and telemetry. Diagnostic saturation SHALL NOT consume reserved lifecycle/terminal/final-report capacity. On the internal enqueue path, short mutex-protected admission is permitted; producers SHALL NOT wait for queue capacity, I/O, rendering, or consumer acknowledgement, and no rendering, arbitrary callback invocation, completion wait, flush, or join SHALL occur under inbox/producer serialization locks. The known internal enqueue path SHALL avoid a redundant asynchronous dispatcher while arbitrary external SDK callbacks SHALL retain their existing isolation contract.

Dropped live diagnostics SHALL produce a bounded non-coalesced notice with a subsequent admitted diagnostic/control event or normal close when presentation is functioning. Counts SHALL be exact only when known; uncertainty or saturation SHALL use a lower bound or a generic omission notice. Loss accounting SHALL NOT reconstruct a transcript or alter retained tails.

A step-terminal event SHALL be admitted only after stream readers finish safe sanitizer finalization, any upstream dispatcher is drained, and heartbeat production is stopped/joined. On the healthy path the actor SHALL finalize its pending diagnostic group before rendering that terminal and SHALL persist across successful steps. Session close SHALL stop acceptance and wake the consumer independently of queue capacity, without an enqueued shutdown sentinel. Normal close SHALL drain accepted events, finalize display, acknowledge completion, and join before BuildKit or return, with no output after successful completion acknowledgement.

Control-admission failure SHALL explicitly disable presentation and wake its actor without consuming another slot or changing the primary result. Renderer exceptions SHALL discard pending display state and disable output without retrying the failed stream while the actor can still complete. All presentation completion waits and joins SHALL share one five-second monotonic budget; repeated cleanup SHALL NOT restart it. If renderer I/O remains blocked, expiry SHALL cancel further presentation and allow execution/return to continue without an unbounded wait or synchronous CLI fallback. A daemon worker or an in-flight write MAY outlive this degraded boundary, but after unblocking no further renderer operation SHALL begin after cancellation is observed. Domain cleanup, timeout, SDK data, and retained capture guarantees SHALL remain independent of this presentation-only degradation.

#### Scenario: Long-running npm installation emits output
- **WHEN** npm writes stdout or stderr during authorized interactive text-mode assembly
- **THEN** safe redacted warnings, errors, retries, timeouts, and status output SHALL become visible before process completion
- **AND** each received stdout or stderr chunk SHALL count as diagnostic activity for silence tracking and last-activity age
- **AND** the constructor SHALL retain only the existing bounded redacted diagnostic tail

#### Scenario: Repeated retry status
- **WHEN** npm emits identical diagnostic lines of any closed diagnostic classification in `lines` mode within one monotonic second
- **THEN** presentation SHALL emit the first admitted line immediately and count each admitted occurrence including the first
- **AND** a group total of at least two admitted occurrences SHALL emit `<diagnostic> (repeated N times)` at window end or before a different diagnostic or terminal event
- **AND** a single-occurrence group SHALL emit no summary
- **AND** a different diagnostic SHALL follow the flushed summary so observable order is preserved
- **AND** identical warnings, errors, and timeout diagnostics SHALL use the same coalescing rule
- **AND** typed Constructor lifecycle/control events SHALL remain individual and uncoalesced
- **AND** retained failure diagnostics SHALL receive the original ungrouped ordering and occurrences after secret redaction and URL sanitization

#### Scenario: Interactive repeated diagnostic updates one slot
- **WHEN** interactive presentation receives identical repeated diagnostic lines of any closed diagnostic classification
- **THEN** the first occurrence SHALL populate one mutable diagnostic slot without a suffix
- **AND** each admitted repeat SHALL increment the admitted-occurrence count
- **AND** the next TUI refresh, no later than one second afterward, SHALL replace that slot with `<diagnostic> (repeated N times)` for admitted total `N`
- **AND** neither the first occurrence nor any repeat SHALL create a durable line while the group remains mutable
- **AND** a different diagnostic or terminal event SHALL finalize the group with exactly one ordered durable write containing the latest total
- **AND** warning and error classification SHALL NOT change this mutable-first behavior
- **AND** a later identical diagnostic SHALL start a new group

#### Scenario: Interactive numeric diagnostic updates one slot
- **WHEN** consecutive admitted interactive diagnostics have the same phase, step, stream, classification, logical resource, ordered URL-fingerprint tuple, and rendered text except for exactly one changed numeric token
- **THEN** the latest diagnostic SHALL replace the mutable slot without a repetition suffix
- **AND** its exact-repeat count SHALL reset to one, while an exact repeat of that latest value SHALL resume canonical repetition counting
- **AND** numeric monotonicity SHALL NOT be required
- **AND** matching SHALL consume the entire maximal numeric-looking sequence and SHALL NOT extract a valid-looking substring from a signed, incomplete, malformed dot-separated, or identifier-embedded sequence
- **AND** all surrounding nonnumeric text SHALL be byte-identical
- **AND** an identity/template mismatch or omission notice SHALL finalize the group
- **AND** finalization SHALL durably write only the latest interactive value
- **AND** every received stdout/stderr chunk SHALL already have reset diagnostic silence and updated latest diagnostic activity before mailbox admission or grouping

#### Scenario: Numeric diagnostic values remain separate in lines mode
- **WHEN** `lines` mode receives diagnostics that differ in exactly one numeric token
- **THEN** each changed numeric value SHALL be emitted as its own durable line
- **AND** only exact repeats SHALL use the existing one-second repetition window

#### Scenario: Hidden URLs remain distinct without disclosure
- **WHEN** diagnostics with the same rendered safe text contain different sanitized URL identities
- **THEN** their ordered session-keyed fingerprint tuples SHALL keep their exact-repeat and numeric-update groups distinct
- **AND** userinfo, query, fragment, and proxy information SHALL contribute nothing to fingerprint input
- **AND** an explicit port, including an explicit default port, SHALL remain part of fingerprint input while an absent default port SHALL NOT be synthesized
- **AND** no fingerprint or session key SHALL appear in user-visible or retained output, failure reports, persistence, or evidence

#### Scenario: Coalescing window ends without another diagnostic
- **WHEN** a `lines`-mode diagnostic group with at least two total occurrences is pending and no later diagnostic arrives before the one-second window ends
- **THEN** the single facade presentation worker SHALL emit the canonical total-count summary when its mailbox wait reaches the window deadline
- **AND** assembler execution and facade presentation SHALL create no separate coalescing timer thread

#### Scenario: Diagnostic mailbox saturates
- **WHEN** diagnostic producers fill the bounded best-effort telemetry capacity
- **THEN** further ordinary diagnostics MAY be dropped without blocking a producer
- **AND** diagnostic saturation SHALL NOT consume the fixed bounded control capacity used for lifecycle, terminal, timeout, cancellation, or final-report events
- **AND** repetition suffixes SHALL count only admitted occurrences and SHALL NOT claim exact producer-side multiplicity
- **AND** functioning presentation SHALL emit a bounded non-coalesced omission notice with a subsequent admitted diagnostic/control or normal close
- **AND** the notice SHALL state the count only when known, otherwise a lower bound or a generic loss warning
- **AND** terminal processing SHALL remain ordered after every earlier admitted event

#### Scenario: Fixed bounded control capacity is exhausted
- **WHEN** a terminal or final-report control event cannot be admitted within the fixed bounded control capacity
- **THEN** the adapter SHALL explicitly disable presentation and wake its actor without another queue slot
- **AND** no producer SHALL wait for rendering or an unadmitted event's acknowledgement
- **AND** close SHALL remain available and all presentation completion waiting SHALL share the five-second budget
- **AND** the primary success, failure, timeout, or cancellation result SHALL remain unchanged

#### Scenario: Coalescing terminates with healthy presentation lifecycle
- **WHEN** a host step completes with a pending diagnostic group
- **THEN** readers SHALL finish safe finalization, any upstream dispatcher SHALL drain, and heartbeat production SHALL stop/join before terminal admission
- **AND** the actor SHALL process earlier admitted events and finalize the group before terminal rendering
- **AND** it SHALL remain available for the next successful host step
- **WHEN** the session ends after any facade final failure report is submitted
- **THEN** close SHALL end acceptance without requiring a queue slot, even if the inbox is full
- **AND** the actor SHALL drain, finalize, acknowledge completion, and join before native output or return within the shared completion budget
- **AND** no output SHALL occur after successful completion acknowledgement

#### Scenario: Presentation cannot finish within its budget
- **WHEN** a renderer write or flush stalls during assembly output or session completion
- **THEN** readers SHALL continue draining and retaining safe bounded diagnostics independently of the actor
- **AND** the facade SHALL stop waiting after one shared five-second completion budget without restarting it on repeated cleanup
- **AND** it SHALL cancel subsequent rendering without retrying output synchronously to the same stream
- **AND** a daemon thread and its already-started write MAY outlive return, with cancellation checked before later renderer operations
- **AND** existing subprocess deadline and container/staging cleanup guarantees SHALL remain unchanged

#### Scenario: Retained context repeats earlier live output
- **WHEN** assembly fails after some or all retained diagnostics were already shown live
- **THEN** the final report SHALL still contain the existing nonempty bounded tail once with a retained-context repeat warning
- **AND** no delivery hash, occurrence receipt, or live-history filtering SHALL be required
- **AND** drops, earlier identical diagnostics, numeric replacement, and tail truncation SHALL NOT cause the report to hide retained context
- **AND** an empty structured tail SHALL remain empty rather than being replaced with the summary

#### Scenario: Structured caller executes assembly
- **WHEN** assembler execution has no authorized live text sink, including JSON mode or an SDK caller that omits presentation
- **THEN** assembler output SHALL NOT be interleaved with caller output
- **AND** a failure SHALL include the bounded redacted diagnostic tail

#### Scenario: URL spans diagnostic chunks
- **WHEN** a URL, credential, secret, or disallowed URL component spans decoder or input-chunk boundaries
- **THEN** the collector SHALL withhold an ambiguous token prefix only within the 8 KiB pending-sanitizer limit until it can be sanitized
- **AND** SHALL NOT flush an unresolved candidate as ordinary text
- **AND** no complete or partial disallowed URL component, credential, or secret SHALL enter the retained tail or structured live event
- **AND** existing retained-tail byte bounds and truncation behavior SHALL remain unchanged

#### Scenario: Arbitrarily long URL has no terminating delimiter
- **WHEN** an arbitrarily long URL-shaped candidate arrives across any number of chunks without a terminating delimiter
- **THEN** pending sanitizer state SHALL remain bounded at 8 KiB while input continues draining
- **AND** the collector SHALL emit only `[sanitized oversized token]` for the candidate and discard its remaining content until a safe token boundary
- **AND** processing SHALL recover after that boundary
- **AND** no candidate fragment SHALL enter a structured live event or retained tail

#### Scenario: Arbitrarily long diagnostic has no newline
- **WHEN** an arbitrarily long diagnostic arrives without a newline
- **THEN** diagnostic-line assembly SHALL remain bounded at 64 KiB while input continues draining
- **AND** the collector SHALL emit only `[sanitized oversized diagnostic]` for that diagnostic and discard its remaining content until newline or stream termination
- **AND** processing SHALL recover after a subsequent newline
- **AND** no discarded diagnostic fragment SHALL enter a structured live event or retained tail

#### Scenario: Stream terminates with an ambiguous sensitive prefix
- **WHEN** EOF, reader failure, or cancellation occurs while an ambiguous URL, encoded URL, credential, or secret prefix or an unterminated diagnostic line is pending
- **THEN** unresolved sensitive candidates SHALL be replaced only with `[sanitized incomplete token]` and SHALL NOT be flushed as ordinary text
- **AND** any pending diagnostic line SHALL be finalized only through bounded secret redaction and URL sanitization
- **AND** pending sanitizer and line-assembly state SHALL remain within their fixed limits
- **AND** no complete or partial URL, credential, or secret SHALL enter a structured live event or retained tail

#### Scenario: Diagnostic contains network configuration
- **WHEN** assembler output contains a configured proxy endpoint, trust path, credential, or disallowed URL component
- **THEN** every displayed, returned, persisted, attached, and failure-report representation SHALL receive only secret-redacted and URL-sanitized text
- **AND** structured diagnostics SHALL separate normalized source-host facts from URL-free text independently of output configuration
- **AND** only the facade SHALL apply hostname-display policy

### Requirement: Distinguish canonical and serialized assembler evidence digests
The constructor SHALL retain the canonical assembler evidence-body digest as the value bound into assembled-output identity and semantic in-image verification. It SHALL separately compute and carry the SHA-256 digest of the exact serialized assembler evidence bytes used for host snapshot admission. Snapshot admission SHALL verify the serialized bytes against only the serialized-evidence digest and SHALL independently parse and validate the canonical body digest. Both digest bindings SHALL be represented in the derived build input without substituting one meaning for the other.

#### Scenario: Assembler evidence enters the build snapshot
- **WHEN** a verified assembled environment is admitted into the constructor snapshot
- **THEN** the exact evidence bytes SHALL match the serialized-evidence digest
- **AND** the parsed evidence body SHALL match its canonical evidence-body digest and assembled-output identity
- **AND** changing either digest binding SHALL invalidate the derived build input

### Requirement: Clean failed assembler executions without discarding reusable cache
On timeout, cancellation, interruption, executor failure, or nonzero exit, the assembler SHALL force-remove its named container and remove mutable staging. It SHALL preserve prior immutable published environments and the shared opaque npm download cache. Abandoned same-input staging SHALL be detected under the existing private input-identity coordination boundary and handled without adopting it as valid output.

#### Scenario: User interrupts assembly
- **WHEN** the invoking user interrupts a running assembler container
- **THEN** the container SHALL be force-removed and mutable staging SHALL be removed
- **AND** the interruption SHALL propagate to the caller

#### Scenario: Assembly fails after downloads
- **WHEN** npm downloads package bytes but assembly subsequently fails or times out
- **THEN** no partial environment SHALL be published
- **AND** safe opaque npm cache entries and prior published environments SHALL remain available

#### Scenario: Same-input staging is abandoned
- **WHEN** locked assembly encounters mutable staging left by an execution that no longer owns the coordinated operation
- **THEN** it SHALL fail closed or securely replace that staging under the input-identity lock
- **AND** SHALL NOT treat abandoned staging as evidence of a completed environment

### Requirement: Expose locked-assembly operational activity
Locked environment assembly SHALL expose presentation-neutral operational activity for coordination-lock wait, cache lookup and reuse, stale staging cleanup, named-container startup, `npm ci` execution, output validation, and publication. Waiting for the existing blocking coordination lock SHALL report elapsed wait without changing lock acquisition semantics. `npm ci` activity SHALL derive from its stdout and stderr rather than Docker network, CPU, or filesystem probes. The existing fixed total deadline and cleanup behavior SHALL remain unchanged.

#### Scenario: Assembly waits for coordination
- **WHEN** another execution holds the same input-identity coordination lock
- **THEN** activity SHALL identify coordination wait and elapsed duration
- **AND** SHALL NOT impose a new lock timeout or alter serialization semantics

#### Scenario: npm produces no diagnostics
- **WHEN** the named assembler container remains alive without stdout or stderr
- **THEN** activity SHALL identify elapsed execution, diagnostic silence, the last observed diagnostic activity age when one exists, safe container name, and remaining fixed deadline
- **AND** SHALL NOT claim that npm or its registry connection is inactive

#### Scenario: Cached environment is reusable
- **WHEN** locked assembly finds a fully verified cached environment after coordination
- **THEN** activity SHALL report cache reuse
- **AND** no container or npm activity SHALL be reported

#### Scenario: Assembly fails
- **WHEN** locked assembly times out or exits unsuccessfully
- **THEN** the failure report SHALL identify the active operational step
- **AND** SHALL reuse the existing bounded redacted diagnostic tail once per final report, explicitly labeled as retained context that may repeat live output
- **AND** renderer failure MAY prevent visible output but SHALL NOT discard the structured failure or change the primary result
- **AND** existing container and staging cleanup guarantees SHALL apply

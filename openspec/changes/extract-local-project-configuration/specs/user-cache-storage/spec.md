## MODIFIED Requirements

### Requirement: Secure constructor-owned cache paths
A local `[cache].dir` SHALL be a non-empty absolute path identifying a dedicated constructor-owned root for all machine-local persistent constructor caches. The cache resolver SHALL lexically normalize the absolute path before safety checks and SHALL reject a root equal to `XDG_CACHE_HOME`, the invoking user's home directory, or the filesystem root, or that is an ancestor of `XDG_CACHE_HOME`. Before the selected root is created, secured, or used, the cache-storage boundary SHALL inspect an existing root and every existing constructor cache descendant encountered for preparation or use with no-follow semantics and SHALL reject any such root or descendant that is a symlink, non-directory where a directory is required, or cannot be secured by the invoking user. The resolved root and constructor-created descendants SHALL use owner-only `0700` permissions; HTTP cache files SHALL use `0600`, and verified artifact blobs SHALL remain non-writable at `0444`. The constructor SHALL NOT chmod an existing parent, including an existing `XDG_CACHE_HOME`. Every invalid root SHALL fail with actionable recovery guidance before cache mutation, network access, artifact publication, container execution, or Docker execution.

#### Scenario: Creating a missing cache subtree
- **WHEN** a valid dedicated constructor cache root or subtree does not exist
- **THEN** the constructor SHALL create it with owner-only permissions
- **AND** SHALL NOT alter permissions of any pre-existing parent directory

#### Scenario: Using an existing dedicated local cache root
- **WHEN** normalized `[cache].dir` identifies an existing directory owned by the invoking user
- **THEN** the constructor SHALL inspect it without following a symlink and require or set its mode to `0700`
- **AND** SHALL NOT alter any parent directory

#### Scenario: Rejecting an empty or relative local cache root
- **WHEN** `[cache].dir` is empty or is not an absolute path
- **THEN** local configuration validation SHALL reject it before cache mutation
- **AND** SHALL identify `[cache].dir` as requiring an absolute dedicated directory

#### Scenario: Rejecting XDG_CACHE_HOME as a local cache root
- **WHEN** normalized `[cache].dir` equals `XDG_CACHE_HOME`
- **THEN** validation SHALL reject it before cache mutation
- **AND** SHALL instruct the user to select a dedicated child such as `${XDG_CACHE_HOME}/docker-constructor-custom`
- **AND** SHALL NOT change permissions on `XDG_CACHE_HOME`

#### Scenario: Rejecting shared or dangerous local cache roots
- **WHEN** normalized `[cache].dir` equals the invoking user's home directory or filesystem root, or is an ancestor of `XDG_CACHE_HOME`
- **THEN** validation SHALL reject it before cache mutation
- **AND** SHALL identify the configured path and instruct the user to choose a dedicated owned directory

#### Scenario: Rejecting a symlinked local cache root without following it
- **WHEN** an existing selected `[cache].dir` entry is a symlink
- **THEN** cache-storage validation SHALL reject it with no-follow semantics
- **AND** SHALL perform no cache mutation, network request, artifact publication, container execution, or Docker execution

#### Scenario: Rejecting an unsafe existing cache descendant
- **WHEN** an existing constructor cache descendant encountered for preparation or use is a symlink, is not a directory where one is required, or cannot be secured by the invoking user
- **THEN** cache-storage validation SHALL reject it with no-follow semantics before mutating the root or any descendant
- **AND** SHALL perform no network request, artifact publication, container execution, or Docker execution
- **AND** SHALL identify the offending descendant with actionable recovery guidance

#### Scenario: Rejecting an unsecurable local cache root
- **WHEN** `[cache].dir` identifies an existing directory that the invoking user does not own or cannot secure
- **THEN** the operation SHALL fail before network, artifact publication, or Docker execution
- **AND** SHALL identify the configured path and instruct the user to choose or create a dedicated owned directory

#### Scenario: Encountering mapped or foreign cache ownership
- **WHEN** an existing constructor cache directory is accessible through group permissions but cannot be set to owner-only mode by the invoking user
- **THEN** the operation SHALL fail before network, artifact publication, or Docker execution
- **AND** the diagnostic SHALL name the offending path and instruct the user to restore ownership or remove that stale cache subtree

### Requirement: Store persistent constructor caches and external constructor-project state under private roots
The constructor SHALL use a normalized validated local `[cache].dir` as its persistent cache root when configured, independently of `[host-access]`, and SHALL derive separate named child directories beneath it. Otherwise it SHALL use `${XDG_CACHE_HOME}/docker-constructor` when `XDG_CACHE_HOME` is non-empty and absolute. When that explicit XDG directory is missing, the constructor SHALL first create it with owner-only `0700` permissions; when it already exists, it SHALL require it to be a writable directory without changing its permissions. It SHALL use `~/.cache/docker-constructor` only when `XDG_CACHE_HOME` is empty or non-absolute. An explicit absolute XDG path that is a non-directory or not writable SHALL fail with an actionable error and SHALL NOT silently fall back. Resolving defaults SHALL NOT require creating `docker-constructor.local.toml`.

The constructor SHALL store HTTP update-discovery data only in the `versioning` child and verified runtime artifacts only in the `runtime-artifacts/blobs` child. For operations with a selected constructor project, the constructor SHALL place implicit generated build projections, runtime projections, default evidence output, persistent build artifact blobs, committed build manifests, locks, uncommitted markers, temporary downloads, and per-build snapshots beneath one private namespace rooted at `<resolved-cache-root>/projects/<safe-constructor-project-basename>-<canonical-constructor-project-path-hash-prefix>/`. The namespace identity SHALL be the canonical absolute path of the selected constructor project; primary and extra workspaces are mounted workspaces and SHALL NOT receive separate runtime-projection namespaces during that launch. The safe basename SHALL be a deterministic filesystem-safe rendering used only for discovery, while the complete SHA-256 digest of the canonical constructor-project path SHALL be authoritative identity. Owner-private versioned `project.json` metadata SHALL record that canonical path and complete identity and SHALL be verified before any namespace child is read, created, recovered, or mutated. A short-name collision or mismatched, malformed, unsafe, or missing metadata for an existing namespace SHALL fail without adopting, replacing, or deleting that namespace. Generated files, build-artifact state, and ephemeral transactions SHALL use distinct children, and build-artifact retention SHALL neither inspect nor mutate another constructor-project namespace or the global `runtime-artifacts` and `versioning` namespaces. Per-build snapshots SHALL be removed after success, failure, or later abandoned-transaction recovery. Normal build, run, verification, and default evidence operations SHALL NOT create `.docker-cache`, `.docker-generated`, or another constructor-generated directory beneath the selected constructor project or any primary or extra workspace; an explicit user-selected output destination remains a user-directed output.

#### Scenario: Using a configured local cache root without host access
- **WHEN** host access is disabled and the local companion declares a valid absolute dedicated `[cache].dir`
- **THEN** cache consumers SHALL use the normalized validated directory as their shared cache root
- **AND** SHALL NOT require `[host-access]` state

#### Scenario: Using a valid existing XDG cache home
- **WHEN** no local cache-root override is configured and `XDG_CACHE_HOME` is non-empty, absolute, and identifies a writable directory
- **THEN** update discovery SHALL use `${XDG_CACHE_HOME}/docker-constructor/versioning`
- **AND** runtime artifact materialization, dry-run inspection, and artifact mount planning SHALL use `${XDG_CACHE_HOME}/docker-constructor/runtime-artifacts/blobs`
- **AND** SHALL NOT change permissions on the existing `XDG_CACHE_HOME` directory

#### Scenario: Creating a missing explicit XDG cache home
- **WHEN** no local cache-root override is configured and `XDG_CACHE_HOME` is non-empty, absolute, and missing
- **THEN** the constructor SHALL create `XDG_CACHE_HOME` with owner-only `0700` permissions before creating its `docker-constructor` child
- **AND** SHALL use that child's `versioning` and `runtime-artifacts/blobs` paths

#### Scenario: Falling back from an absent or relative XDG cache home
- **WHEN** no local cache-root override is configured and `XDG_CACHE_HOME` is empty or non-absolute
- **THEN** update discovery SHALL use `~/.cache/docker-constructor/versioning`
- **AND** runtime artifact materialization, dry-run inspection, and artifact mount planning SHALL use `~/.cache/docker-constructor/runtime-artifacts/blobs`
- **AND** SHALL NOT require creation of the local companion

#### Scenario: Rejecting an unusable explicit XDG cache home
- **WHEN** no local cache-root override is configured and absolute `XDG_CACHE_HOME` identifies a non-directory or is not writable
- **THEN** the operation SHALL fail before cache mutation, network, artifact publication, or Docker execution
- **AND** SHALL identify `XDG_CACHE_HOME` and SHALL NOT fall back to `~/.cache`

#### Scenario: Keeping generated output outside the constructor project
- **WHEN** a launch with one selected constructor project and one primary workspace and zero or more extra workspaces creates a runtime projection or an evidence command writes to its default destination
- **THEN** it SHALL use only the external namespace identified by the canonical path of the selected constructor project
- **AND** it SHALL NOT create a separate runtime-projection namespace or generated entry for a workspace merely because it participates in the launch
- **AND** it SHALL NOT mutate the constructor project, primary workspace, or any extra workspace

#### Scenario: Preparing external constructor-project state
- **WHEN** an operation requires generated project state or host-materialized build artifacts
- **THEN** the constructor SHALL resolve and validate the invoking user's configured or default constructor cache root using this cache-storage policy
- **AND** SHALL derive one project namespace from the canonical absolute path of the selected constructor project, its safe basename, and its complete path digest
- **AND** SHALL verify or atomically create owner-private identity metadata before creating distinct generated, build-artifact, or transaction children
- **AND** SHALL leave the selected constructor project, primary workspace, and every extra workspace unchanged

#### Scenario: Finding constructor-project state during diagnosis
- **WHEN** an operator inspects `<resolved-cache-root>/projects`
- **THEN** each namespace name SHALL expose the safe basename of its constructor project and a collision-resistant canonical-path-hash prefix
- **AND** its owner-private `project.json` SHALL expose the complete canonical constructor-project path, complete digest identity, and metadata schema version

#### Scenario: Rejecting unsafe or mismatched project state
- **WHEN** a required cache or generated-state path is symlinked, escapes the resolved cache root or selected constructor-project namespace, has an unsafe type or ownership, cannot be secured for the invoking user, or has identity metadata inconsistent with the canonical constructor-project path
- **THEN** the operation SHALL fail before network access, state mutation, publication, container execution, or Docker execution
- **AND** SHALL identify the selected constructor project and namespace without mutating the conflicting entry, constructor project, primary workspace, or any extra workspace

#### Scenario: Isolating workspaces during launch
- **WHEN** one constructor project, one primary workspace, and one or more extra workspaces participate in a launch
- **THEN** runtime projection and launcher-generated control state SHALL exist only beneath the external namespace identified by the canonical path of the selected constructor project
- **AND** no namespace or generated entry SHALL be created for a workspace solely because it was mounted for the launch
- **AND** the constructor project and the primary-workspace directory and all extra-workspace directories SHALL remain unmodified

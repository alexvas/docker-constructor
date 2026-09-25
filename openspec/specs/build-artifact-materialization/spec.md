# build-artifact-materialization Specification

## Purpose

Define secure host-side acquisition, transactional BuildKit delivery, and bounded project-scoped retention outside the project checkout for reviewed artifacts used to construct Docker images.

## Requirements

### Requirement: Materialize reviewed build artifacts on the host
Before Docker execution, the constructor SHALL materialize every selected `linux-amd64` rustup, uv, rtk, and fd artifact from its exact reviewed URL, verify its reviewed SHA-256 digest while streaming, and atomically publish valid bytes into the selected private external project namespace's content-addressed cache. A missing, unavailable, malformed, or mismatched artifact SHALL prevent Docker execution, and partial or corrupt downloads SHALL be removed immediately.

#### Scenario: Reusing a verified selected blob
- **WHEN** the selected digest already exists as a safe regular non-writable cache blob and revalidation matches the reviewed digest
- **THEN** the constructor SHALL reuse it without downloading the artifact

#### Scenario: Materializing a cache miss
- **WHEN** a selected digest is absent
- **THEN** the constructor SHALL stream the exact reviewed URL into temporary storage contained by the selected external project namespace
- **AND** SHALL atomically publish it only after the SHA-256 digest matches

#### Scenario: Rejecting an integrity failure
- **WHEN** downloaded bytes do not match the reviewed SHA-256 digest
- **THEN** the build SHALL fail before Docker execution
- **AND** neither the invalid bytes nor a committed cache reference SHALL remain

### Requirement: Deliver selected artifacts and verified derived environments through a named build context
Each non-dry-run build SHALL create an owner-private immutable per-transaction snapshot containing only the selected SHA-256-verified artifacts, a deterministic artifact manifest, and derived assembled environments with their assembler canonical evidence and any required consumer evidence after host-side validation against their assembly results. The snapshot SHALL finalize selected prebuilt-artifact files as `0444`, finalize each derived-environment file with every write bit removed while preserving its assembler- or consumer-evidence-validated executable bits, and remove every write bit from snapshot directories before named-context import. The invoking host user's Docker client SHALL read that snapshot and pass it as a dedicated BuildKit named context without relaxing checkout or ancestor permissions. Dockerfile stages SHALL obtain imported copies from the named context rather than host cache paths or network URLs and SHALL make those in-build copies readable by the stage user. Stages SHALL reverify each selected prebuilt artifact against its reviewed digest before installation, and SHALL verify each derived environment against the expected assembled output identity, canonical output-tree digest, canonical assembler-evidence digest, and deterministic consumer-launcher-evidence digest supplied by a post-materialization transaction/build-plan attestation, then against its assembler canonical evidence and required consumer evidence, before copying it into its final layout.

#### Scenario: Rendering a selected build snapshot
- **WHEN** all selected artifacts and derived environments have completed their required host validation
- **THEN** the build vector SHALL identify the finalized transaction snapshot as the dedicated named context
- **AND** the snapshot SHALL expose stable logical filenames only for selected prebuilt artifacts
- **AND** the snapshot SHALL expose each derived environment and its evidence only beneath its isolated derived-environment path
- **AND** selected prebuilt-artifact files SHALL have mode `0444`
- **AND** derived-environment files SHALL retain their canonical-evidence-validated executable bits while having no write bit for owner, group, or other
- **AND** snapshot directories SHALL have no write bit for owner, group, or other

#### Scenario: Cleaning a hard-linked snapshot without mutating cache blobs
- **GIVEN** a finalized snapshot payload is a hard link to a verified persistent cache blob
- **WHEN** the snapshot is cleaned after build success, failure, or interruption
- **THEN** cleanup SHALL NOT chmod, chown, truncate, or otherwise mutate the shared payload inode
- **AND** SHALL change permissions only on snapshot directories as required for removal
- **AND** SHALL unlink only the snapshot pathname
- **AND** the cache blob SHALL remain invoking-user-owned, mode `0444`, digest-valid, and reusable by the next build

#### Scenario: Reusing cached blobs after independent rootless-Docker permission maintenance
- **GIVEN** a successful build has cleaned its transaction snapshot
- **AND** verified cache blobs remain in the external constructor-project namespace
- **WHEN** an operator independently performs recursive ownership and permission maintenance on the constructor project and workspaces between builds, such as assigning them to `docker-dev:docker-dev` and applying `g+rwX`
- **AND** the original invoking user retains the required read and traversal access
- **THEN** the next build SHALL reuse digest-valid cache blobs without downloading them again
- **AND** the cache blobs SHALL remain invoking-user-owned and mode `0444`
- **AND** project-scoped operator maintenance SHALL NOT modify the external project namespace
- **AND** the build SHALL NOT report the reusable blobs as unsafe or corrupt

#### Scenario: Verifying an imported derived environment
- **WHEN** a host-validated derived environment with its assembler canonical evidence and required consumer evidence is imported through the named context
- **THEN** the consuming Dockerfile stage SHALL first require `DerivedEnvironment` and match the environment’s assembled output identity, canonical tree digest, assembler-evidence digest, and consumer-launcher-evidence digest to their expected values supplied by the post-materialization transaction/build-plan attestation, then verify both evidence sets before final-layout copy
- **AND** an invalid, incomplete, evidence-mismatched, or substituted environment/evidence pair SHALL fail the stage without entering the final image

#### Scenario: Consuming an imported snapshot with a remapped user
- **WHEN** the invoking host user has imported the snapshot into BuildKit and a build stage runs as `dev` under a different numeric UID
- **THEN** `dev` SHALL read the selected copies inside the BuildKit filesystem
- **AND** SHALL not receive a bind mount or direct path to the host snapshot or private cache-control state

#### Scenario: Preserving project and cache-root boundaries
- **WHEN** the invoking user imports a snapshot from the owner-private external project namespace while the source project is owned by a different user
- **THEN** named-context import SHALL rely on that invoking user's existing read access to project inputs and owner access to external state
- **AND** the constructor SHALL NOT require ownership of or broaden permissions on the project, its parents, the home directory, or cache-root parents

#### Scenario: Rendering a dry-run prospective context
- **WHEN** a dry-run renders the planned Docker build
- **THEN** its structured context SHALL be `{ "name": "constructor-artifacts", "state": "prospective", "path": null, "attestation": { "state": "prospective" } }`
- **AND** text output SHALL display `--build-context constructor-artifacts=<prospective:not-materialized>` beneath an explicit `Planned build (not executable)` label
- **AND** the value SHALL be presentation metadata rather than a filesystem path or executable argument
- **AND** dry-run SHALL perform no lock acquisition, directory creation, artifact inspection, download, snapshot publication, cache mutation, or Docker capability probe

#### Scenario: Rejecting an unresolved context in execution
- **WHEN** executable argument rendering receives a build plan with any prospective context
- **THEN** it SHALL reject the plan before producing executable argv or invoking Docker
- **AND** SHALL require every named context to be materialized with a real platform-native path and one valid closed attestation: `NoDerivedEnvironment` or `DerivedEnvironment(assembledOutputIdentity, canonicalTreeDigest, assemblerEvidenceDigest, consumerLauncherEvidenceDigest)`

#### Scenario: Rendering prospective context across host platforms
- **WHEN** the same dry-run is rendered under POSIX and Windows host path semantics
- **THEN** its prospective structured context and display token SHALL be byte-identical
- **AND** neither renderer SHALL interpret the display token as a host path

#### Scenario: Lacking named-context support
- **WHEN** Docker does not support BuildKit named contexts
- **THEN** the constructor SHALL fail with actionable prerequisite guidance before artifact download or Docker build execution

#### Scenario: Detecting a prebuilt-artifact boundary integrity failure
- **WHEN** bytes received by a Dockerfile stage do not match the supplied reviewed digest
- **THEN** that stage SHALL fail
- **AND** the artifact SHALL NOT enter the final image

### Requirement: Serialize project build transactions
The constructor SHALL permit at most one active image build transaction per canonical project identity, using the lock inside that project's external state namespace. A competing build SHALL fail or wait under a deterministic bounded locking policy without materializing, committing, or garbage-collecting artifacts concurrently.

#### Scenario: Starting a competing build
- **WHEN** another build transaction holds the selected constructor project's build lock
- **THEN** the new build SHALL NOT mutate the artifact cache, snapshot state, or committed live set
- **AND** SHALL report that the checkout already has an active build

#### Scenario: Recovering an abandoned transaction
- **WHEN** a later build finds a snapshot whose owning transaction no longer holds a live lock
- **THEN** it SHALL remove the abandoned snapshot before creating its own
- **AND** SHALL leave verified blobs available as uncommitted cache entries

### Requirement: Retain one committed build live set
After and only after Docker successfully builds the image and the constructor successfully removes that transaction's snapshot, the constructor SHALL atomically replace the selected external project namespace's committed build manifest with the complete selected build-input digest set. It SHALL then immediately delete every blob removed from the previous committed build set. Snapshot cleanup failure SHALL prevent commit and post-commit garbage collection, preserve the prior committed build manifest and its blobs, and leave newly verified blobs reusable as uncommitted entries. Shared XDG runtime artifacts SHALL have separate ownership and SHALL NOT be consulted, protected, or deleted by build-cache retention. Existing Docker images SHALL remain independent of source artifact retention, and no image label or historical build generation SHALL be required.

#### Scenario: Committing a successful changed build transaction
- **WHEN** Docker successfully builds an image with a new selected artifact set
- **AND** the constructor successfully removes that transaction's snapshot
- **THEN** the constructor SHALL atomically commit that set
- **AND** SHALL delete every blob superseded from the prior committed build set

#### Scenario: Preserving the prior set after Docker failure
- **WHEN** Docker build fails or is interrupted
- **THEN** the prior committed build manifest and its blobs SHALL remain intact
- **AND** newly verified blobs SHALL remain uncommitted

#### Scenario: Preserving the prior set after snapshot cleanup failure
- **WHEN** Docker successfully builds an image
- **AND** removal of that transaction's snapshot fails
- **THEN** the constructor SHALL NOT commit the selected build-input digest set or garbage-collect superseded blobs
- **AND** the prior committed build manifest and its blobs SHALL remain intact
- **AND** newly verified blobs SHALL remain reusable as uncommitted entries

### Requirement: Expire only abandoned verified artifacts by fixed policy
A verified project-scoped build blob that is not referenced by the selected constructor project's committed build set and was never superseded through a successful build commit SHALL be retained as uncommitted for a fixed 30 days (2,592,000 seconds) from its verified publication. Expired uncommitted build blobs and markers SHALL be removed during later build-cache maintenance. The TTL SHALL NOT be configurable through reviewed inventory, local configuration, or command options, and SHALL NOT govern shared XDG runtime artifacts.

#### Scenario: Retrying within the retention period
- **WHEN** a failed build is retried before its verified uncommitted blob is 30 days old
- **THEN** the constructor SHALL reuse the valid blob without downloading it again

#### Scenario: Collecting an expired uncommitted blob
- **WHEN** an unreferenced verified uncommitted blob is older than 2,592,000 seconds
- **THEN** later cache maintenance SHALL remove the blob and its marker

#### Scenario: Protecting a committed build blob from TTL
- **WHEN** a blob in the selected external project namespace appears in that project's committed build set
- **THEN** uncommitted TTL SHALL NOT remove it regardless of file age

#### Scenario: Leaving runtime artifact retention independent
- **WHEN** build-cache commit or GC runs
- **THEN** it SHALL NOT inspect, protect, modify, or delete any shared XDG runtime artifact or infer cross-cache identity

### Requirement: Report safe reviewed-artifact acquisition activity
Host-side reviewed build-artifact acquisition SHALL expose presentation-neutral start, success, and failure activity associated with the artifact's safe logical name. Whenever the existing acquisition boundary yields body chunks without an additional probe, buffering, or semantic change, each yielded chunk SHALL update cumulative received-byte progress and latest transport activity for the next heartbeat. Byte progress MAY be omitted only for a compatible transport that cannot expose chunk observation without changing its semantics. Acquisition SHALL NOT require a known total size and SHALL NOT infer percentage or transfer rate when those values are unavailable.

#### Scenario: Streaming acquisition exposes received bytes
- **WHEN** a cache-miss artifact download yields body chunks through the existing streaming boundary
- **THEN** each yielded chunk SHALL update cumulative received-byte progress and latest transport activity for the next heartbeat
- **AND** SHALL identify transport progress and last byte activity without reporting diagnostic silence
- **AND** SHALL NOT require content length or claim a completion percentage

#### Scenario: Compatible transport cannot expose chunks without semantic change
- **WHEN** an injected compatible transport cannot expose chunk observation without an additional network operation, buffering, or changed transport semantics
- **THEN** acquisition SHALL continue without byte-progress events
- **AND** SHALL still report logical start and terminal activity

#### Scenario: Reviewed artifact transport fails
- **WHEN** transport fails while acquiring rustup, uv, rtk, or fd
- **THEN** the failure SHALL identify that reviewed logical artifact
- **AND** SHALL apply the shared safe network diagnostic presentation policy
- **AND** partial bytes SHALL remain subject to existing cleanup guarantees

#### Scenario: Artifact is a verified cache hit
- **WHEN** a selected artifact is safely reused from the verified cache
- **THEN** activity SHALL identify cache reuse
- **AND** SHALL NOT report network byte progress

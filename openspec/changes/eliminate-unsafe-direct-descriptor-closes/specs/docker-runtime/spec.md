## ADDED Requirements

### Requirement: Preserve runtime artifact failures across descriptor release
Protected runtime artifact installation SHALL own the single opened mounted-artifact descriptor through an explicit at-most-once lifecycle. The runtime image SHALL package the lightweight `docker.filesystem` modules required by that lifecycle under `/usr/local/lib/pi-cli/docker/filesystem/`, so importing the installed runtime code does not depend on the source repository. An ordinary close failure SHALL NOT replace an active stat, safety-validation, read, integrity, identity, or installation failure, and the descriptor SHALL receive one release attempt on every terminal path. Artifact selection, no-follow opening, read-only mount assumptions, integrity verification, and existing runtime error types and messages SHALL remain unchanged.

#### Scenario: Installed runtime resolves filesystem lifecycle modules
- **WHEN** the runtime installer is imported from the image's `/usr/local/lib/pi-cli` Python layout
- **AND** the source repository is absent from `sys.path`
- **THEN** its `docker.filesystem` lifecycle imports SHALL resolve from `/usr/local/lib/pi-cli/docker/filesystem/`
- **AND** import SHALL NOT depend on a host checkout or ambient `PYTHONPATH`

#### Scenario: Runtime artifact validation and close both fail
- **WHEN** stat, type, mode, identity, or integrity validation of the opened artifact fails
- **AND** releasing the descriptor reports an ordinary close failure
- **THEN** the validation or integrity failure SHALL remain authoritative
- **AND** the close failure SHALL remain observable as secondary diagnostic context
- **AND** the descriptor SHALL not be closed again

#### Scenario: Foreign-owned artifact uses the no-atime compatibility fallback
- **WHEN** opening a host-owned artifact with `O_NOATIME` reports `PermissionError`
- **THEN** installation SHALL retry the same no-follow read-only open without `O_NOATIME`
- **AND** SHALL apply the existing type, mode, read, and integrity checks to the fallback descriptor
- **AND** SHALL NOT reject the artifact based on file ownership

#### Scenario: Runtime artifact read and close both fail
- **WHEN** reading the mounted artifact fails
- **AND** release also reports an ordinary close failure
- **THEN** the read failure SHALL remain authoritative
- **AND** the close failure SHALL remain observable as secondary diagnostic context

#### Scenario: Sole runtime artifact release fails
- **WHEN** artifact verification otherwise succeeds
- **AND** releasing the descriptor fails
- **THEN** `open_verified()` SHALL map the ordinary close `OSError` to `InstallError(f"cannot close artifact at {_path}: {exc}")`
- **AND** the original close `OSError` SHALL be the direct cause
- **AND** process-control interruption SHALL remain unwrapped
- **AND** callers that catch `InstallError` and the CLI/result path SHALL report the normal controlled installation failure rather than an uncaught exception
- **AND** package installation SHALL NOT execute from the verified bytes

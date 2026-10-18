# Capability: docker-runtime

## Purpose
Define the current Docker image, compose service, and container startup behavior for the π environment.

## Requirements

### Requirement: Expose only effective runtime dependency metadata to the container
The project SHALL derive a narrow effective runtime projection from the reviewed `runtime` section of `docker-constructor.toml` after applying validated runtime overrides. Each reviewed runtime extension SHALL own an artifact catalog keyed by exact version, with exact artifact identity and integrity in every entry, and its selected default version SHALL have a matching catalog entry. Before container launch, the host SHALL materialize each selected artifact into a verified content-addressed cache beneath the resolved machine-local constructor cache root. The projection SHALL contain only effective package identity, version, canonical mounted-artifact identity, checksum/integrity, and validation metadata required for runtime installation; it SHALL exclude downloadable URLs, host cache paths, the reviewed source, unselected artifact alternatives, build entries, and host-only runtime metadata.

#### Scenario: Mounting runtime dependency configuration and artifacts
- **WHEN** the constructor facade launches a container
- **THEN** it SHALL validate and mount the generated effective runtime projection read-only at `/run/pi-cli/docker-constructor.runtime.toml`
- **AND** it SHALL mount each unique selected artifact as an individual read-only file beneath `/run/pi-cli/runtime-artifacts`
- **AND** it SHALL NOT mount the artifact cache root, an unselected artifact, `docker-constructor.toml`, or a host-side effective build projection

#### Scenario: Isolating concurrent runtime projections
- **WHEN** multiple runtime launches use different effective selections
- **THEN** each launch SHALL atomically create and mount a private projection beneath the selected constructor project's external project-state runtime namespace
- **AND** no projection SHALL be created beneath the constructor project or any primary or extra workspace
- **AND** one launch SHALL NOT rewrite another launch's mounted projection
- **AND** each launch SHALL mount only its selected immutable cache blobs
- **AND** the launcher SHALL remove its private projection after Docker exits or launch fails without deleting shared published cache blobs

#### Scenario: Applying a runtime override
- **WHEN** a supported runtime override is supplied to `run`
- **THEN** the resolver SHALL validate it against policy in the reviewed runtime source entry
- **AND** it SHALL select the reviewed artifact catalog entry whose exact version key matches the effective overridden version
- **AND** host launch planning SHALL retain that entry's exact URL and integrity for materialization
- **AND** the mounted projection SHALL contain the selected version, canonical mounted-artifact identity, and matching integrity but SHALL omit the URL and host path
- **AND** an otherwise policy-valid version with no matching reviewed catalog entry SHALL be rejected before materialization
- **AND** the resolver SHALL NOT discover artifacts over the network, synthesize an artifact URL, or reuse integrity from another version
- **AND** the reviewed `docker-constructor.toml` SHALL remain unchanged

#### Scenario: Installing Pi extensions from mounted artifacts
- **WHEN** the entrypoint or protected installer detects a missing or mismatched configured Pi extension
- **THEN** it SHALL locate the selected artifact only beneath the fixed mounted-artifact root
- **AND** it SHALL reject a missing, writable, symlinked, non-regular, traversal, identity-mismatched, or integrity-mismatched artifact
- **AND** it SHALL verify the declared checksum/integrity before package execution
- **AND** package execution SHALL consume the exact verified bytes without reopening a mutable named file
- **AND** it SHALL install idempotently into the mounted Pi home
- **AND** it SHALL validate installed package identity, version, and ownership after installation

#### Scenario: Avoiding runtime artifact downloads
- **WHEN** protected runtime installation requires an artifact
- **THEN** container code SHALL read the individually mounted read-only blob
- **AND** it SHALL NOT download the artifact, query a registry, consume the reviewed URL, or create a download workspace

#### Scenario: Rejecting host-only metadata at runtime
- **WHEN** the effective runtime projection is generated and validated
- **THEN** its closed DTO schema SHALL reject downloadable URLs, host cache roots or paths, build entries, update-provider metadata, update/override policy, and source fields not needed for mounted-artifact validation
- **AND** runtime code SHALL NOT require those fields

### Requirement: Expose one constructor CLI facade
The project SHALL expose `docker/docker-constructor.py` as the sole supported user-facing command entry point. The facade SHALL provide primary commands `build`, `run`, and `check-updates` and auxiliary commands `validate`, `show`, `doctor`, and `verify`; it SHALL NOT expose a `schema` command.

#### Scenario: Delegating a facade command
- **WHEN** a user invokes any supported constructor command with command-specific or global flags
- **THEN** the facade SHALL parse and validate user arguments, coordinate prompts, render output, and map errors to exit codes
- **AND** it SHALL delegate inventory, networking, project selection, provider, Docker orchestration, and verification behavior to internal APIs

#### Scenario: Verifying through the facade
- **WHEN** a user invokes `docker-constructor.py verify` with selected verification flags
- **THEN** internal verification APIs SHALL execute the requested checks and return structured results
- **AND** the facade SHALL only select checks and present those results

#### Scenario: Avoiding competing entry points
- **WHEN** maintained documentation or repository-owned automation invokes constructor behavior
- **THEN** it SHALL use `docker/docker-constructor.py`
- **AND** it SHALL NOT invoke `versions.py`, `launch-pi.py`, standalone verification scripts, Compose, or `build_wrapper.py` as user-facing commands

### Requirement: Build a developer runtime image
The system SHALL provide a multi-stage Docker image for running π inside an isolated container.

#### Scenario: Builder stage installs developer tooling
- **WHEN** the image is built
- **THEN** the builder stage uses `node:24-trixie-slim`
- **AND** installs developer tooling including git, vim, curl, jq, ripgrep, build-essential, Rust, uv, ty, pi, rtk, and fd
- **AND** bootstraps the MCP Yarn workspace under `/home/dev/mcp`

#### Scenario: Runtime stage includes interactive shell environment
- **WHEN** the runtime stage is built
- **THEN** it uses `node:24-trixie-slim`
- **AND** installs runtime tools including git, vim, less, bat, jq, ripgrep, openssh-client, gh, rpm, gosu, socat, bash, and zsh
- **AND** configures locale `ru_RU.UTF-8`
- **AND** exposes `pi` on the runtime `PATH`

### Requirement: Provide Python through uv
The system SHALL install an explicit uv-managed CPython runtime whose configured version is 3.14.6 or newer, with exactly 3.14.6 as the default build input. The runtime SHALL expose direct `python` and `python3` executables and SHALL NOT implement either command through `uv run`, a project-aware wrapper, or a shell alias. The system SHALL NOT advertise or create a standalone `pip` command or alias; package operations SHALL be documented through explicit `uv pip` subcommands.

#### Scenario: Running python3 in the container
- **WHEN** a user invokes `python3` from any working directory
- **THEN** the configured uv-managed CPython interpreter executes directly
- **AND** invocation SHALL NOT discover or synchronize a project environment through `uv run`
- **AND** the interpreter version SHALL be 3.14.6 or newer

#### Scenario: Building with the default Python version
- **WHEN** the image is built without a Python version override
- **THEN** the image SHALL install exactly CPython 3.14.6
- **AND** build-time verification SHALL confirm the installed version and executable path

#### Scenario: Building with a supported Python override
- **WHEN** the image is built with an available Python version override equal to or newer than 3.14.6
- **THEN** the image SHALL install and expose the requested version
- **AND** Python-backed uv tool installation SHALL use that configured Python selection explicitly

#### Scenario: Building with an unsupported older Python override
- **WHEN** the image is built with a Python version override older than 3.14.6
- **THEN** the build SHALL fail with a clear version-requirement error

#### Scenario: Installing Python packages
- **WHEN** documentation instructs a user to perform a Python package operation
- **THEN** it SHALL use an explicit `uv pip` subcommand and target context
- **AND** it SHALL NOT rely on a `pip` or `pip3` alias supplied by the image

### Requirement: Reuse shared operating-system setup across image stages
The multi-stage Docker image SHALL derive builder and runtime assembly from a shared base stage for their common operating-system packages and user setup, while builder-only packages SHALL remain outside the final runtime lineage when they are not runtime requirements.

#### Scenario: Building builder and runtime descendants
- **WHEN** Docker builds the tool builder and runtime image
- **THEN** common Debian package and dev-user setup SHALL originate from the same cached base layers
- **AND** common package installation SHALL NOT execute independently in both descendants

### Requirement: Preserve the runtime interface after tool isolation
The runtime image SHALL expose Pi, OpenSpec, Rust/Cargo tools, uv, ty, rtk, and fd on the existing runtime `PATH` after their build stages or installation prefixes are isolated. The Rust toolchain SHALL use rustup's minimal profile and SHALL explicitly include the stable-toolchain `rustfmt` and `clippy` components. `rtk` and `fd` SHALL be supplied by verified pinned prebuilt release artifacts, and `rtk` integration setup SHALL remain available.

#### Scenario: Running isolated tools in the final image
- **WHEN** the final runtime container is started as user `dev`
- **THEN** `pi`, `openspec`, `cargo`, `rustc`, `rustfmt`, `uv`, `ty`, `rtk`, and `fd` SHALL resolve from `PATH`
- **AND** each required command SHALL execute without requiring build-stage cache mounts
- **AND** `cargo clippy --version` SHALL succeed

#### Scenario: Verifying the minimal Rust profile
- **WHEN** the runtime verification script checks the Rust installation
- **THEN** the active stable toolchain SHALL be available through rustup
- **AND** `rustfmt` SHALL be installed as an explicit component
- **AND** `clippy` SHALL be installed as an explicit component
- **AND** the verification SHALL fail with an actionable error if either component is unavailable

#### Scenario: Running prebuilt Rust tools
- **WHEN** the final runtime container is started as user `dev`
- **THEN** `rtk --version` and `fd --version` SHALL report the pinned release versions

#### Scenario: Configuring rtk integration
- **WHEN** the prebuilt `rtk` executable is assembled into the image
- **THEN** the existing `rtk init -g --agent pi` integration SHALL be applied
- **AND** telemetry SHALL remain disabled
- **AND** generated integration files SHALL be owned by `dev`

#### Scenario: Runtime network isolation
- **WHEN** the final runtime stage is assembled
- **THEN** it SHALL copy the verified `rtk` and `fd` executables from artifact stages
- **AND** SHALL NOT download or install either release from the network

### Requirement: Run as a configurable dev user
The system SHALL create and use a `dev` user whose UID and GID can be aligned with the host.

#### Scenario: Preparing the dev user during image build
- **WHEN** `docker/setup-dev-user.sh` runs with `DEV_UID` and `DEV_GID`
- **THEN** it ensures group `dev` exists with the requested GID
- **AND** ensures user `dev` exists with the requested UID, home directory `/home/dev`, and shell `zsh` when available

### Requirement: Configure an interactive zsh environment
The system SHALL install a pinned oh-my-zsh setup for the `dev` user.

#### Scenario: Setting up zsh
- **WHEN** `docker/setup-zsh.sh` runs
- **THEN** it clones oh-my-zsh at the configured pinned git ref
- **AND** writes a minimal `.zshrc` with the `git` plugin enabled
- **AND** loads only the Pi prompt fragment from `.pi-zsh-prompt`

#### Scenario: Only an obsolete prompt customization exists
- **WHEN** an obsolete prompt customization exists but `.pi-zsh-prompt` does not
- **THEN** zsh setup SHALL not load the obsolete customization

### Requirement: Include bundled π skills and extensions
The system SHALL make bundled π assets available in the runtime home directory through constructor-managed runtime setup and SHALL NOT ship the obsolete `/home/dev/install-pi-extensions.sh` compatibility endpoint.

#### Scenario: Omitting the legacy extension setup wrapper
- **WHEN** the runtime image is assembled
- **THEN** it SHALL NOT copy `docker/install-pi-extensions.sh` into the image
- **AND** `/home/dev/install-pi-extensions.sh` SHALL be absent

#### Scenario: Installing supported extensions after mounting the Pi home
- **WHEN** `docker/docker-constructor.py run` launches a container with the effective runtime projection and `/home/dev/.pi` mounted
- **THEN** the root entrypoint SHALL repair mounted Pi-home ownership before privilege drop
- **AND** the protected internal runtime installer SHALL run as user `dev`
- **AND** it SHALL install or validate the configured Pi extensions from exact verified local artifacts
- **AND** each extension SHALL match the projected package identity and explicit version
- **AND** the setup SHALL target the mounted Pi home rather than an image-layer directory

#### Scenario: Registering rtk integration
- **WHEN** constructor-managed runtime setup completes extension installation against the mounted Pi home
- **THEN** the entrypoint SHALL apply `rtk init -g --agent pi` as user `dev`
- **AND** it SHALL disable rtk telemetry
- **AND** repeated container launches SHALL not duplicate configuration

#### Scenario: Rejecting an unavailable Pi home
- **WHEN** the effective runtime projection is present but the expected Pi home is not mounted
- **THEN** protected runtime setup SHALL fail with an actionable error before launching Pi
- **AND** it SHALL not install extensions or rtk configuration into the image-provided `/home/dev/.pi`

#### Scenario: Following the supported migration path
- **WHEN** a user needs runtime extension setup after removal of the image-local wrapper
- **THEN** maintained documentation SHALL direct the user through `docker/docker-constructor.py run`
- **AND** it SHALL NOT present direct internal installer invocation as a second public constructor facade

### Requirement: Present a consistent Pi container interface
The project SHALL identify the developer image, direct Docker build command, launcher command, and supported documentation as Pi-oriented interfaces without requiring a Compose service.

#### Scenario: Following documented build instructions
- **WHEN** a user follows a build command from any maintained README translation
- **THEN** the command SHALL invoke `./docker/docker-constructor.py build`
- **AND** SHALL produce the canonical tagged Pi runtime image

#### Scenario: Following documented run instructions
- **WHEN** a user follows runtime launch instructions from any maintained README translation
- **THEN** the launcher SHALL invoke the Pi image and Pi CLI through direct Docker
- **AND** SHALL NOT require Compose files or a Compose service

### Requirement: Use the Pi prompt path exclusively
The shell setup SHALL use `/home/dev/.pi-zsh-prompt` as the only project-owned prompt configuration path.

#### Scenario: Pi prompt customization exists
- **WHEN** the dev home contains `/home/dev/.pi-zsh-prompt`
- **THEN** zsh SHALL load that prompt fragment

### Requirement: Verify image-provided runtime home ownership
The runtime smoke check SHALL inspect required image-provided paths under `/home/dev` and SHALL fail when any inspected entry is not owned by `dev:dev`. Verification SHALL be read-only and SHALL NOT follow symbolic links.

#### Scenario: Runtime ownership is correct
- **WHEN** `docker/verify-runtime.sh` checks a built image without host volumes
- **THEN** it SHALL inspect required Pi, uv/Python, Rust/Cargo, MCP, work, and npm paths
- **AND** SHALL run with startup ownership repair disabled
- **AND** SHALL succeed when every inspected entry is owned by `dev:dev`

#### Scenario: Runtime ownership mismatch is present
- **WHEN** an inspected image-provided path contains an entry not owned by `dev:dev`
- **THEN** `docker/verify-runtime.sh` SHALL print an actionable mismatch identifying the path
- **AND** SHALL exit unsuccessfully without changing ownership

### Requirement: Verify tools against the effective inventory
The runtime image SHALL expose a root-owned read-only effective version inventory, and runtime verification SHALL compare installed tools and extensions with those effective values.

#### Scenario: Verifying configured runtime tools
- **WHEN** the runtime smoke check runs as `dev`
- **THEN** it SHALL verify Node, Rust/Cargo, rustfmt/clippy, uv, Python, ty, Pi, OpenSpec, rtk, and fd against the effective inventory
- **AND** SHALL report actionable version mismatches

#### Scenario: Verifying configured Pi extensions
- **WHEN** the protected Pi extension setup script installs extensions into the mounted Pi home
- **THEN** it SHALL obtain package identity from each entry's npm source metadata and its version from the same effective inventory entry
- **AND** runtime extension verification SHALL compare installed package metadata with those values
- **AND** neither the runtime entry nor the setup script SHALL duplicate the npm package identity

### Requirement: Preserve direct stable Python selection
The effective inventory and runtime SHALL preserve the direct uv-managed Python contract: the default version SHALL be exactly `3.14.6`; supported overrides SHALL be stable numeric `X.Y.Z` values at or above `3.14.6`; and no standalone pip interface SHALL be exposed.

#### Scenario: Verifying direct Python executables
- **WHEN** runtime verification checks `python` and `python3`
- **THEN** both SHALL resolve directly to the effective uv-managed CPython interpreter
- **AND** SHALL NOT resolve through `uv run`, project-aware wrappers, or shell aliases

#### Scenario: Rejecting an unsupported Python selector
- **WHEN** the effective configuration contains a prerelease, non-numeric selector, or version older than `3.14.6`
- **THEN** validation SHALL fail before the Docker build starts

### Requirement: Verify runtime host access conditionally
Runtime verification SHALL derive host-access expectations from validated reviewed policy and its resolved local address rather than assuming every container has a host mapping.

#### Scenario: Verifying enabled host access
- **WHEN** runtime verification inspects a container launched with enabled host access
- **THEN** it SHALL verify that `host.docker.internal` resolves to the configured address
- **AND** SHALL verify that `HOST_ACCESS_ADDRESS` equals that address
- **AND** SHALL verify `HOST_PROXY_PORT` equals the reviewed proxy port when one is configured
- **AND** SHALL NOT require a protocol-specific connection to the proxy port

#### Scenario: Verifying disabled host access
- **WHEN** runtime verification inspects a container launched with disabled host access
- **THEN** it SHALL skip the positive host mapping requirement
- **AND** SHALL verify that constructor launch did not set `HOST_ACCESS_ADDRESS` or `HOST_PROXY_PORT`

### Requirement: Document opt-in host connectivity
Maintained documentation SHALL describe host access as an optional runtime capability rather than a build or ordinary launch prerequisite.

#### Scenario: Following host-access documentation
- **WHEN** a user needs a container to reach a service on the host
- **THEN** all maintained README translations SHALL explain the Docker-gateway and external-address modes
- **AND** SHALL explain the local TOML companion and optional `HOST_PROXY_PORT`
- **AND** SHALL NOT instruct ordinary build or run users to configure a gateway

### Requirement: Inject optional local corporate network settings at launch
The constructor SHALL incorporate validated optional local corporate trust and proxy settings into the direct Docker run vector independently of runtime dependency projections and host-access policy. An enabled trust bundle SHALL be mounted as one additional read-only file at `/etc/ssl/certs/ca-certificates.crt`; a configured proxy SHALL be emitted only through the defined proxy environment-variable contract. Neither configuration SHALL expose the reviewed inventory or alter runtime artifact mount restrictions.

#### Scenario: Launching with local corporate settings
- **WHEN** a launch has valid enabled corporate trust and a valid local proxy
- **THEN** the direct Docker vector SHALL include the read-only system-trust bundle mount and required proxy environment variables
- **AND** SHALL retain the existing runtime projection and selected artifact mount behavior

#### Scenario: Launching without local corporate settings
- **WHEN** corporate trust and proxy are both absent or disabled locally
- **THEN** the direct Docker vector SHALL not include a corporate trust mount or any proxy environment variable
- **AND** SHALL preserve existing host-access behavior

### Requirement: Mount host workspaces 1:1
The system SHALL run the container directly against host workspace directories without remapping their paths or using Compose fragments. Public CLI, domain, TUI, rendering, diagnostics, verification, tests, and maintained documentation SHALL use primary/extra workspace terminology and SHALL NOT retain main/additional project aliases.

#### Scenario: Launching with a primary workspace
- **WHEN** `docker-constructor run --workspace PATH` starts the Pi image through `docker run`
- **THEN** `WORKSPACE_PATH_1` SHALL identify the selected primary workspace
- **AND** the container working directory SHALL be set to `WORKSPACE_PATH_1`
- **AND** the same absolute host path SHALL be bind-mounted into the same absolute path inside the container

#### Scenario: Adding extra workspaces
- **WHEN** the user repeats `--extra-workspace PATH`
- **THEN** each selected path SHALL be added as a separate 1:1 bind mount
- **AND** corresponding consecutive `WORKSPACE_PATH_2..N` environment values SHALL be passed to the container

#### Scenario: Rejecting duplicate workspaces
- **WHEN** normalized primary and extra workspace selections contain the same path more than once
- **THEN** runtime configuration SHALL fail with an actionable workspace-selection error
- **AND** SHALL NOT silently promote, discard, or renumber a duplicate

### Requirement: Require workspace selection only for runtime launch
Workspace paths and 1:1 bind mounts SHALL be runtime launch inputs rather than image-build prerequisites.

#### Scenario: Launching with an interactively selected workspace
- **WHEN** the launcher starts a runtime container after the user selects a primary workspace
- **THEN** it SHALL configure that workspace as `WORKSPACE_PATH_1`, the working directory, and a 1:1 bind mount
- **AND** extra selected workspaces SHALL remain optional runtime mounts

#### Scenario: Running without a selected primary workspace
- **WHEN** a runtime run operation is requested without `WORKSPACE_PATH_1` or an equivalent launcher selection
- **THEN** runtime configuration SHALL fail with an actionable workspace-selection error
- **AND** build-only configuration SHALL remain unaffected

### Requirement: Repair mounted workspace ownership on startup
The system SHALL be able to fix ownership and access permissions of mounted workspace worktrees before dropping privileges. The runtime SHALL explicitly install `util-linux` to provide `mountpoint`. The launcher interface SHALL define `WORKSPACE_PATH_*` as host-directory bind mounts. `CHOWN_WORK_ON_START` repair SHALL consider only configured `WORKSPACE_PATH_*` paths, SHALL require each target to be a container mount point (`mountpoint -q`), and SHALL NOT scan or modify ordinary image-layer directories, fixed runtime home paths, or non-workspace mounts.

#### Scenario: CHOWN_WORK_ON_START enabled with mounted workspace paths
- **WHEN** the container starts as root with `CHOWN_WORK_ON_START` set to `1` or `true`
- **THEN** the entrypoint SHALL inspect each configured `WORKSPACE_PATH_*` variable
- **AND** SHALL repair a target only when `mountpoint -q` confirms it is a container mount point
- **AND** for each repaired workspace mount SHALL recursively change ownership only for files and directories not owned by `dev:dev`
- **AND** for each repaired workspace mount SHALL grant user and group read/write access to files and read/write/traversal access to directories
- **AND** SHALL preserve executable-file intent rather than making every regular file executable
- **AND** the recursive scans SHALL NOT follow symbolic links
- **AND** repaired workspace paths SHALL be marked as global Git safe directories for user `dev`
- **AND** the entrypoint SHALL finally execute the requested command as `dev`

#### Scenario: Non-workspace home path is mounted
- **WHEN** `CHOWN_WORK_ON_START` is enabled and `/home/dev/.pi`, `/home/dev/.cargo`, `/home/dev/.npm`, `/home/dev/.npm-global`, or another non-`WORKSPACE_PATH_*` path is mounted
- **THEN** the entrypoint SHALL NOT traverse, repair, or register that mounted path

#### Scenario: Configured workspace path is not a mount point
- **WHEN** `CHOWN_WORK_ON_START` is enabled and a configured `WORKSPACE_PATH_*` exists but `mountpoint -q` fails
- **THEN** the entrypoint SHALL skip ownership repair, permission repair, and safe-directory registration for that path
- **AND** SHALL NOT traverse or modify its contents

#### Scenario: Mount detection is unavailable
- **WHEN** `CHOWN_WORK_ON_START` is enabled but the `mountpoint` command is unavailable
- **THEN** the entrypoint SHALL print an actionable error
- **AND** SHALL exit unsuccessfully before executing the requested command

#### Scenario: CHOWN_WORK_ON_START disabled
- **WHEN** `CHOWN_WORK_ON_START` is neither `1` nor `true`
- **THEN** the entrypoint SHALL perform no ownership or access-permission repair
- **AND** SHALL still execute the requested command as `dev`

### Requirement: Preserve image-provided home ownership with workspace mounts
The runtime image SHALL assemble image-provided artifacts under `/home/dev` with ownership matching the configured `dev` user and group. Runtime assembly SHALL NOT recursively change ownership of the complete `/home/dev` tree.

#### Scenario: Assembling runtime home artifacts
- **WHEN** the runtime stage copies Pi, uv/Python, Rust, Cargo binaries, and MCP artifacts from build stages
- **THEN** the copied artifacts SHALL be owned by the runtime `dev` user and group
- **AND** newly created runtime directories SHALL be created with targeted `dev:dev` ownership
- **AND** assembly SHALL NOT execute `chown -R` over `/home/dev`

#### Scenario: Building with custom dev identity
- **WHEN** the image is built with non-default `DEV_UID` and `DEV_GID` values
- **THEN** image-provided paths under `/home/dev` SHALL resolve to the configured runtime `dev` user and group

#### Scenario: Starting with host workspace bind mounts
- **WHEN** the runtime container starts with workspace worktrees mounted from host directories at configured `WORKSPACE_PATH_*` paths
- **THEN** the entrypoint ownership and access repair policy SHALL remain available for those workspace bind mounts
- **AND** removal of build-time whole-home chown SHALL NOT weaken workspace-mount repair

### Requirement: Document the workspace-oriented runtime image
The Russian, English, and Chinese README files SHALL present the current Docker development environment around the primary user workflows of building the image, launching it with interactive workspace selection, updating managed components, and performing occasional maintenance. Internal implementation invariants and image-development diagnostics SHALL NOT interrupt those primary workflows.

#### Scenario: Comparing translated documentation
- **WHEN** the maintained README translations are reviewed
- **THEN** their supported commands, workflow order, component taxonomy, update process, and maintenance guidance SHALL be equivalent
- **AND** references to removed files, options, environment variables, services, build targets, and proxy bootstrap behavior SHALL be absent

#### Scenario: Following the primary workflow structure
- **WHEN** a user opens a maintained README
- **THEN** build, interactive launch, and component update SHALL appear as the three primary actions
- **AND** a separate general setup section SHALL NOT be required before understanding those actions
- **AND** detailed Dockerfile/cache-development notes, BuildKit cache experiments, shell-prompt internals, Python implementation details, and Python override examples SHALL NOT appear in the primary user flow

#### Scenario: Selecting workspaces interactively
- **WHEN** a user follows the launch instructions
- **THEN** documentation SHALL use the executable launcher entry point
- **AND** SHALL explain primary-workspace working-directory selection, optional extra 1:1 mounts, and the mounted Pi home

#### Scenario: Maintaining host state
- **WHEN** a user consults maintenance guidance
- **THEN** image verification, mounted extension refresh, host permission repair, Docker storage cleanup, and user-facing cache cleanup SHALL be grouped under Maintenance
- **AND** permission repair SHALL use `<docker-dev>:<docker-dev>` placeholders and recursive `ug+rwX` semantics
- **AND** every translation SHALL identify `docker-dev` as the user/group typically mapped to host UID/GID `100999` by rootless Docker
- **AND** every translation SHALL show how to add the host user to the `docker-dev` group
- **AND** documentation SHALL warn users to choose the intended host owner before changing shared worktrees

#### Scenario: Troubleshooting current failures
- **WHEN** a user reads troubleshooting guidance
- **THEN** it SHALL focus on actionable current failures such as host EACCES
- **AND** SHALL omit obsolete-name, missing-extra-workspace, and ordinary host-gateway guidance from the primary troubleshooting list

### Requirement: Preserve runtime artifact failures across descriptor release
Protected runtime artifact installation SHALL explicitly own its mounted-artifact descriptor and attempt release once on every terminal path. Ordinary close failures SHALL NOT replace active operation failures. The image SHALL package required filesystem lifecycle modules independently of the source repository. Existing artifact policy and runtime diagnostics SHALL remain unchanged.

#### Scenario: Preserve runtime artifact policy during ownership migration
- **WHEN** mounted-artifact descriptor ownership is migrated
- **THEN** artifact selection, no-follow opening, read-only mount assumptions, integrity verification and existing runtime error types and messages SHALL remain unchanged except for the specified sole-close mapping
- **AND** ordinary release failure SHALL NOT replace an active stat, safety-validation, read, integrity, identity or installation failure
- **AND** the single opened descriptor SHALL receive one release attempt on every terminal path

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

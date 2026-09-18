"""Project launcher — workspace selection, pi-N allocation, and
run-vector assembly for ``docker-constructor.py run``.

This module owns:
  - :class:`WorkspaceSelection` — resolved workspace paths ready for rendering
  - :func:`resolve_workspace_selection` — CLI-flag → selection with precedence
  - :func:`allocate_pi_name` — lowest-free pi-N from container inspection
  - :func:`build_run_inputs` — selection → :class:`RunRenderInputs`
  - :class:`RunRequest` / :class:`RunResult` — run-transaction DTOs
  - :func:`orchestrate_run` — the full run transaction

All Docker interaction happens through injected boundaries
(:class:`ContainerNameInspector`, :class:`RunExecutor`).
Filesystem effects — inventory loading and projection-file
creation — are owned by :func:`orchestrate_run` via injected
(:class:`ProjectionFactory`) or path-based boundaries, never
performed ad-hoc by the module.
"""

from __future__ import annotations

import enum
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, Mapping, Sequence

import docker.versioning.artifact_cache as artifact_cache
from docker.versioning.dispatch_types import ExitKind
from docker.versioning.model import _derive_artifact_id
from docker.versioning.project_state import resolve_project_state
from docker.versioning.rendering import (
    RunHostAccess,
    RunRenderInputs,
    plan_artifact_mounts,
    plan_dry_run_artifact_mounts,
)
from types import MappingProxyType

if TYPE_CHECKING:
    from docker.versioning.effective import RuntimeProjectionHandle
    from docker.versioning.model import EffectiveRuntimeProjection


class ExecutionMode(enum.Enum):
    """Explicit mode for the Docker process boundary.

    ``CAPTURED`` — stdout/stderr are captured for diagnostics;
    no ``--tty``/``--interactive`` flags are passed to Docker.

    ``INTERACTIVE`` — stdin/stdout/stderr inherit the host
    terminal.  The mode controls stream inheritance; it does
    **not** guarantee which Docker flags (``--tty``,
    ``--interactive``) appear — those are chosen separately
    based on ``tty`` and ``stdin_open``."""
    CAPTURED = "captured"
    INTERACTIVE = "interactive"


# ═══════════════════════════════════════════════════════════════════
# Exceptions
# ═══════════════════════════════════════════════════════════════════


class NoWorkspaceError(Exception):
    """Raised when no primary workspace can be determined."""


class ContainerInspectError(Exception):
    """Raised when Docker container inspection fails."""


# ═══════════════════════════════════════════════════════════════════
# Value objects
# ═══════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class WorkspaceSelection:
    """Resolved workspace selection ready for launch-vector assembly.

    *workspace* is always a non-empty absolute path.
    *extra_workspaces* preserves insertion order with no duplicates.
    """

    workspace: str
    extra_workspaces: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.workspace or not os.path.isabs(self.workspace):
            raise ValueError(
                f"workspace must be a non-empty absolute path, "
                f"got {self.workspace!r}"
            )
        for i, p in enumerate(self.extra_workspaces):
            if not p or not os.path.isabs(p):
                raise ValueError(
                    f"extra_workspaces[{i}] must be a non-empty "
                    f"absolute path, got {p!r}"
                )


# ═══════════════════════════════════════════════════════════════════
# Injected boundaries
# ═══════════════════════════════════════════════════════════════════


class ContainerNameInspector(Protocol):
    """Injected Docker container-name inspection boundary.

    Returns an unordered set of existing container names visible
    to ``docker ps -a``.  Raises :class:`ContainerInspectError` when
    Docker is unavailable or output cannot be parsed.
    """

    def list_names(self) -> set[str]:
        ...


class WorkspaceSelector(Protocol):
    """Injected interactive workspace-selection boundary (TUI).

    Returns a resolved :class:`WorkspaceSelection` or ``None`` when
    the user cancels.  The caller owns validation and error mapping.
    """

    def select(self) -> WorkspaceSelection | None:
        ...


class ProjectionFactory(Protocol):
    """Injectable projection-file creation boundary.

    Receives a resolved effective projection and a parent directory
    and returns a context-manager handle that owns the created file.
    The caller promises to enter and exit the handle.
    """

    def __call__(
        self,
        projection: EffectiveRuntimeProjection,
        *,
        parent_dir: str,
    ) -> RuntimeProjectionHandle:
        ...


# ═══════════════════════════════════════════════════════════════════
# Public API (stubs — to be implemented in 11.4)
# ═══════════════════════════════════════════════════════════════════


def resolve_workspace_selection(
    *,
    workspace: str | None = None,
    extra_workspaces: tuple[str, ...] = (),
    tui: bool = False,
    workspace_root: str | None = None,
    selector: WorkspaceSelector | None = None,
) -> WorkspaceSelection:
    """Resolve primary and extra workspaces from CLI flags.

    Paths are lexically normalized to absolute paths without resolving
    symlinks. Explicit CLI selection takes precedence over TUI selection.
    """
    resolved_workspace: str | None = None
    resolved_extra: tuple[str, ...] = ()

    if workspace is not None:
        resolved_workspace = workspace
        resolved_extra = tuple(extra_workspaces)
    elif tui and selector is not None:
        sel = selector.select()
        if sel is None:
            raise NoWorkspaceError("No primary workspace selected (TUI cancelled)")
        resolved_workspace = sel.workspace
        resolved_extra = sel.extra_workspaces
    else:
        raise NoWorkspaceError(
            "No primary workspace specified. Use --workspace / -w "
            "to select a workspace directory, or --tui to choose interactively."
        )

    if not resolved_workspace:
        raise ValueError("workspace must be a non-empty path")
    if any(not path for path in resolved_extra):
        raise ValueError("extra workspaces must be non-empty paths")
    # Use lexical absolute normalization only: workspace symlink spelling is
    # part of the bind-mount contract and must not be collapsed with realpath.
    resolved_workspace = os.path.abspath(os.path.normpath(resolved_workspace))
    normalized_extra = tuple(
        os.path.abspath(os.path.normpath(path)) for path in resolved_extra
    )
    seen: set[str] = {resolved_workspace}
    deduped: list[str] = []
    for path in normalized_extra:
        if path in seen:
            raise ValueError(
                f"Duplicate workspace path after normalisation: {path!r}"
            )
        seen.add(path)
        deduped.append(path)

    return WorkspaceSelection(
        workspace=resolved_workspace,
        extra_workspaces=tuple(deduped),
    )


def allocate_pi_name(inspector: ContainerNameInspector) -> str:
    """Return the lowest free ``pi-N`` container name.

    Inspects existing containers via *inspector* and returns the
    smallest N ≥ 1 such that ``pi-N`` is not in use.  Allocation is
    best-effort — another process may claim the name before
    ``docker run`` executes.
    """
    try:
        names = inspector.list_names()
    except Exception:
        raise

    # Parse pi-N numbers from existing names
    taken: set[int] = set()
    for name in names:
        if not name.startswith("pi-"):
            continue
        suffix = name[3:]
        # Must be a positive integer with no leading zeros
        # (pi-01 reserves pi-1 because int("01") == 1)
        if not suffix:
            continue
        try:
            n = int(suffix)
        except ValueError:
            continue
        if n >= 1:
            taken.add(n)

    if not taken:
        return "pi-1"

    # Find the lowest gap (or next after max)
    max_taken = max(taken)
    for n in range(1, max_taken + 2):
        if n not in taken:
            return f"pi-{n}"

    # Should never reach here — fallback safe
    return f"pi-{max_taken + 1}"


def build_run_inputs(
    *,
    selection: WorkspaceSelection,
    image: str,
    container_name: str,
    pi_home_host: str,
    projection_host_path: str,
    projection_container_path: str,
    tty: bool = True,
    stdin_open: bool = True,
    chown_on_start: str | None = None,
) -> RunRenderInputs:
    """Build :class:`~docker.versioning.rendering.RunRenderInputs`
    from a resolved :class:`WorkspaceSelection` and runtime parameters.

    Host-access inputs are always disabled — callers that need host
    access must construct ``RunRenderInputs`` with an explicit
    ``host_access`` argument.
    """
    return RunRenderInputs(
        image=image,
        container_name=container_name,
        pi_home_host=pi_home_host,
        projection_host_path=projection_host_path,
        projection_container_path=projection_container_path,
        workspace=selection.workspace,
        extra_workspaces=selection.extra_workspaces,
        tty=tty,
        stdin_open=stdin_open,
        chown_on_start=chown_on_start,
    )


# ═══════════════════════════════════════════════════════════════════
# Run transaction
# ═══════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class ProcessResult:
    """Captured subprocess outcome — immutable and daemon-independent."""

    argv: tuple[str, ...]
    return_code: int
    stdout: str = ""
    stderr: str = ""


class RunExecutor(Protocol):
    """Injected Docker execution boundary for ``docker run``.

    Accepts a fully rendered argument vector and returns a
    :class:`ProcessResult`.  The executor never modifies the vector
    or prompts the user.
    """

    def run(self, argv: tuple[str, ...], *,
            interactive: bool = False) -> ProcessResult:
        ...


class ArtifactByteFetcher(Protocol):
    """Injected byte-fetch boundary for runtime artifact
    downloads.

    Called once per selected artifact cache-miss.  Returns the
    raw bytes from the reviewed URL.  Test doubles return
    controlled bytes so digest-mismatch and network-error
    paths are deterministic."""

    def __call__(self, url: str) -> bytes: ...


class ProcessRunner:
    """Injectable process-execution boundary.

    Subclass and override ``run`` for in-memory fakes that return
    :class:`ProcessResult` instead of invoking a real subprocess.
    """

    def run(self, argv: Sequence[str], *,
            mode: ExecutionMode = ExecutionMode.CAPTURED) -> ProcessResult:
        """Execute *argv* and return a structured result.

        ``mode`` controls whether stdout/stderr are captured or
        the host terminal is inherited."""
        import subprocess
        if mode is ExecutionMode.CAPTURED:
            proc = subprocess.run(
                list(argv), text=True, capture_output=True, check=False,
            )
        else:
            proc = subprocess.run(
                list(argv), text=True, capture_output=False,
                stdin=None, stdout=None, stderr=None, check=False,
            )
        return ProcessResult(
            argv=tuple(argv),
            return_code=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
        )


@dataclass(frozen=True)
class RunRequest:
    """Immutable all inputs for a run transaction.

    Covers task 11.2: runtime overrides, private projection creation,
    read-only mount, gateway mapping, TTY modes, dry-run, Docker
    failure, and projection cleanup.
    """

    inventory_path: str
    """Path to ``docker-constructor.toml``."""

    image: str
    """Canonical image to run (e.g. ``pi-cli-pi:latest``)."""

    selection: WorkspaceSelection
    """Resolved workspace selection from :func:`resolve_workspace_selection`."""

    pi_home_host: str
    """Host path to the Pi home directory."""

    repo_root: str | None = None
    """Legacy generated-state plumbing pending Phase 3.

    This field must never select inventory companions or other project-owned
    inputs.
    """

    project_root: str | None = None
    """Selected constructor-project root owning companions and ``.docker-local``."""

    overrides: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({}),
    )
    """Runtime override map (``runtime.pi-extensions.<name>.version=...``)."""

    tty: bool = True
    """Allocate a pseudo-TTY."""

    stdin_open: bool = True
    """Keep STDIN open (``--interactive``)."""

    chown_on_start: str | None = None
    """Value for ``CHOWN_WORK_ON_START`` env var."""

    command: tuple[str, ...] = ()
    """Command to pass through to the container entrypoint."""

    dry_run: bool = False
    """When ``True``, render the vector but do not invoke Docker."""

    executor: RunExecutor | None = None
    """Injected run executor; ``None`` means execution impossible."""

    inspector: ContainerNameInspector | None = None
    """Injected container-name inspector for pi-N allocation."""

    _create_projection: ProjectionFactory | None = None
    """Injected projection-file factory; defaults to
    :func:`~docker.versioning.effective.create_runtime_projection`."""

    _artifact_fetcher: ArtifactByteFetcher | None = None
    """Injected byte-fetch boundary for deterministic
    materialization tests.  ``None`` selects the real
    download transport; a test double returns controlled
    bytes so digest-mismatch paths are network-independent."""

    _artifact_cache_root: str | None = None
    """Test-only exact runtime-artifact blob-root injection seam."""

    _artifact_locks_root: str | None = None
    """Optional test-only runtime-artifact lock-root injection seam."""

    _artifact_tmp_root: str | None = None
    """Optional test-only runtime-artifact temporary-root injection seam."""

    _constructor_cache_root: str | None = None
    """Optional test-only external constructor project-state root."""

    def __post_init__(self) -> None:
        if not isinstance(self.overrides, MappingProxyType):
            object.__setattr__(self, "overrides", MappingProxyType(
                dict(self.overrides),
            ))


@dataclass(frozen=True)
class RunResult:
    """Outcome of :func:`orchestrate_run` — structural, not rendered."""

    exit_kind: ExitKind
    """Operational exit kind before facade mapping."""

    message: str | None = None
    """Human-readable diagnostic."""

    run_args: tuple[str, ...] = ()
    """Rendered ``docker run`` argument vector."""

    display_string: str | None = None
    """Shell-escaped display string for dry-run output."""

    process_result: ProcessResult | None = None
    """Captured subprocess outcome when execution was performed."""

    projection_path: str | None = None
    """Host path to the private runtime projection (for test assertions)."""

    projection_hash: str | None = None
    """SHA-256 content hash of the projection (for identity checks)."""

    container_name: str | None = None
    """Allocated pi-N name (for test assertions)."""

    artifact_cache_hits: tuple[str, ...] = ()
    """Artifact IDs already present and valid in the cache (dry-run only)."""

    artifact_cache_misses: tuple[str, ...] = ()
    """Artifact IDs not yet materialized in the cache (dry-run only)."""


def _resolve_host_access(
    policy: object | None,
    local_host_access: object | None,
) -> RunHostAccess | None:
    """Resolve host-access rendering inputs from reviewed policy
    and the single shared local aggregate result.

    Returns ``None`` when the policy is enabled but the local
    ``[host-access]`` slice has no address. The caller must translate
    ``None`` into a config-failure ``RunResult``. The caller passes only
    the host-access domain slice; this function never reopens or re-parses
    the local companion.
    """
    if policy is None:
        return RunHostAccess.disabled()
    enabled = getattr(policy, "enabled", False)
    if not enabled:
        return RunHostAccess.disabled()

    mode = getattr(policy, "mode", None)
    proxy_port = getattr(policy, "proxy_port", None)
    if not mode:
        return RunHostAccess.disabled()

    if local_host_access is None:
        return None

    addr = getattr(local_host_access, "address", None)
    if not addr or not isinstance(addr, str):
        return None

    try:
        return RunHostAccess(address=addr, mode=mode, proxy_port=proxy_port)
    except ValueError:
        return None


def orchestrate_run(request: RunRequest) -> RunResult:
    """Execute the full run transaction.

    1. Load and validate the reviewed inventory.
    2. Apply runtime overrides → :class:`EffectiveRuntimeProjection`.
    3. If *dry_run*: render the ``docker run`` display string, return —
       no projection file is created and no Docker process is invoked.
    4. Create a private runtime projection file (read-only mount).
    5. Allocate a pi-N container name.
    6. Render the ``docker run`` argument vector.
    7. Execute via *executor*, clean up projection, return result.
    8. On any failure during steps 4–7: clean up projection,
       propagate error.
    """
    import shlex
    from pathlib import Path

    from docker.versioning.effective import (
        create_runtime_projection,
        resolve_runtime,
    )
    from docker.versioning.errors import (
        EffectiveConfigError,
        OverrideValidationError,
        UnsupportedOverrideError,
    )
    from docker.versioning.inventory import (
        InventoryError,
        load_project_configuration,
        resolve_corporate_trust_bundle_path,
        validate_corporate_trust_bundle,
    )
    from docker.versioning.rendering import render_run_vector

    # ── Step 1: validate constructor-project identity ───────
    if request.project_root is None:
        return RunResult(
            exit_kind=ExitKind.CONFIG,
            message="project_root is required for generated runtime state",
        )
    constructor_project = Path(request.project_root).resolve()
    if Path(request.inventory_path).resolve().parent != constructor_project:
        return RunResult(
            exit_kind=ExitKind.CONFIG,
            message="project_root must contain the selected inventory",
        )

    # ── Step 2: load both fixed configuration documents ────
    # The shared transaction parses and validates the reviewed inventory
    # and any present local companion before any effect, and returns one
    # aggregate local result consumed by every domain below.
    try:
        inventory, local_corporate = load_project_configuration(
            Path(request.inventory_path),
        )
    except (InventoryError, OSError, ValueError, KeyError) as exc:
        return RunResult(
            exit_kind=ExitKind.CONFIG,
            message=f"Failed to load configuration: {exc}",
        )

    # Corporate trust bundle host path — resolved only on the enabled path.
    # An enabled but missing or malformed fixed bundle fails closed before
    # any runtime effect, exactly as for build planning.
    local_project_root = (
        Path(request.project_root) if request.project_root is not None else None
    )
    if local_corporate.corporate_trust.enabled:
        if local_project_root is None:
            return RunResult(
                exit_kind=ExitKind.CONFIG,
                message=(
                    "corporate trust is enabled but project_root is absent; "
                    "cannot resolve <project-root>/.docker-local/"
                    "corporate-ca-bundle.crt"
                ),
            )
        try:
            validate_corporate_trust_bundle(
                resolve_corporate_trust_bundle_path(local_project_root)
            )
        except InventoryError as exc:
            return RunResult(exit_kind=ExitKind.CONFIG, message=str(exc))
    corporate_trust_bundle: str | None = None
    if local_corporate.corporate_trust.enabled and local_project_root is not None:
        corporate_trust_bundle = os.path.abspath(
            resolve_corporate_trust_bundle_path(local_project_root)
        )
    proxy_url = local_corporate.network_proxy.url
    proxy_no_proxy = local_corporate.network_proxy.no_proxy

    # All persistent runtime-artifact state lives beneath the shared,
    # secured constructor cache root; projections and evidence use the
    # selected constructor project's external generated-state namespace.
    from docker.versioning.cache_storage import (
        prepare_resolved_root, resolve_effective_root,
        runtime_artifacts_blobs_child, runtime_artifacts_locks_child,
        runtime_artifacts_tmp_child,
    )
    xdg_cache_home = os.environ.get("XDG_CACHE_HOME")
    cache_home = Path(os.path.expanduser("~"))
    local_cache_dir = getattr(
        getattr(local_corporate, "cache", None), "dir", None,
    )
    try:
        resolved_cache_root = (Path(request._constructor_cache_root)
            if request._constructor_cache_root is not None else resolve_effective_root(
                local_cache_dir, xdg_cache_home=xdg_cache_home, home=cache_home,
            ))
        if request._artifact_cache_root is None:
            runtime_cache_root = str(runtime_artifacts_blobs_child(resolved_cache_root))
            runtime_locks_root = str(runtime_artifacts_locks_child(resolved_cache_root))
            runtime_tmp_root = str(runtime_artifacts_tmp_child(resolved_cache_root))
        else:
            # Keep arbitrary test blob roots opaque; never infer project state
            # from them. Lock/tmp seams are explicit when isolation matters.
            runtime_cache_root = request._artifact_cache_root
            runtime_locks_root = request._artifact_locks_root or runtime_cache_root
            runtime_tmp_root = request._artifact_tmp_root or runtime_cache_root
    except Exception as exc:
        return RunResult(
            exit_kind=ExitKind.CONFIG,
            message=f"Failed to prepare runtime artifact cache: {exc}",
        )

    # Dry-run needs only a prospective namespace. Real execution defers state
    # preparation until all input validation has completed.
    project_state = None
    if request.dry_run:
        try:
            project_state = resolve_project_state(
                constructor_project, cache_root=resolved_cache_root, create=False,
            )
        except Exception as exc:
            return RunResult(exit_kind=ExitKind.CONFIG,
                             message=f"Failed to resolve constructor project state: {exc}")
    runtime_projection_root = str(project_state.runtime_root) if project_state else ""

    # ── Step 1c: resolve host-access policy ─────────────────
    host_access = _resolve_host_access(
        getattr(inventory.runtime, "host_access", None),
        local_corporate.host_access,
    )
    if host_access is None:
        return RunResult(
            exit_kind=ExitKind.CONFIG,
            message="Host access is enabled but the local companion "
                    "is missing or has no [host-access] address. "
                    "Run 'doctor' first or set [host-access].address "
                    "in the local companion.",
        )

    # ── Step 2: apply overrides ──────────────────────────────
    try:
        selected_artifacts, effective = resolve_runtime(
            inventory.runtime,
            request.overrides,
        )
    except (
        UnsupportedOverrideError,
        OverrideValidationError,
        EffectiveConfigError,
        ValueError,
    ) as exc:
        return RunResult(
            exit_kind=ExitKind.CONFIG,
            message=str(exc),
        )

    # ── Step 3: dry-run ─────────────────────────────────────
    if request.dry_run:
        import dataclasses
        assert project_state is not None

        try:
            # Compute projection hash from the resolved effective
            # projection for display/identity purposes.
            proj_raw: dict[str, object] = {
                "extensions": {
                    name: dataclasses.asdict(entry)
                    for name, entry in effective.extensions.items()
                }
            }
            projection_hash = hashlib.sha256(
                json.dumps(
                    proj_raw, sort_keys=True, default=str,
                ).encode("utf-8")
            ).hexdigest()
            # Plan artifact mounts from the resolved selection without
            # materialization — host paths are deterministic cache
            # locations, container targets are fixed beneath the
            # runtime-artifacts root.
            dry_run_mounts = plan_dry_run_artifact_mounts(
                selected_artifacts, cache_root=runtime_cache_root,
            )
            # Inspect the cache read-only — verify each unique
            # blob's bytes against its declared SRI integrity
            # using the no-follow, descriptor-relative inspection
            # API.  Corrupt, missing, symlinked, or non-regular
            # entries are reported as planned misses.
            seen: set[str] = set()
            hits: list[str] = []
            misses: list[str] = []
            for art in selected_artifacts:
                if art.integrity in seen:
                    continue
                seen.add(art.integrity)
                artifact_id = _derive_artifact_id(art.integrity)
                if artifact_cache.inspect_verified_blob_readonly(
                    art.integrity,
                    cache_root=runtime_cache_root,
                ):
                    hits.append(artifact_id)
                else:
                    misses.append(artifact_id)
            # Render a dummy projection for display purposes only.
            # No file is ever created.
            projection_container_path = (
                "/run/pi-cli/docker-constructor.runtime.toml"
            )
            render_inputs = RunRenderInputs(
                image=request.image,
                container_name="pi-N",
                pi_home_host=request.pi_home_host,
                projection_host_path=(
                    str(project_state.runtime_root / "projection.toml")
                ),
                projection_container_path=projection_container_path,
                workspace=request.selection.workspace,
                extra_workspaces=request.selection.extra_workspaces,
                host_access=host_access,
                tty=request.tty,
                stdin_open=request.stdin_open,
                command=request.command,
                chown_on_start=request.chown_on_start,
                artifact_mounts=dry_run_mounts,
                validate_artifact_sources=False,
                corporate_trust_bundle=corporate_trust_bundle,
                proxy_url=proxy_url,
                proxy_no_proxy=proxy_no_proxy,
                project_state_runtime_root=str(project_state.runtime_root),
            )
            run_args = render_run_vector(render_inputs)
            display = shlex.join(run_args)
        except Exception as exc:
            return RunResult(
                exit_kind=ExitKind.CONFIG,
                message=f"Dry-run render failed: {exc}",
            )
        return RunResult(
            exit_kind=ExitKind.SUCCESS,
            run_args=run_args,
            display_string=display,
            projection_hash=projection_hash,
            artifact_cache_hits=tuple(hits),
            artifact_cache_misses=tuple(misses),
        )

    # ── Step 3b: materialize unique selected artifacts ─────
    try:
        project_state = resolve_project_state(
            constructor_project,
            cache_root=prepare_resolved_root(resolved_cache_root), create=True,
        )
        runtime_projection_root = str(project_state.runtime_root)
    except Exception as exc:
        return RunResult(exit_kind=ExitKind.CONFIG,
                         message=f"Failed to resolve constructor project state: {exc}")
    # selected_artifacts from resolve_runtime already deduplicates by
    # integrity.  Each entry in the projection retains its independent
    # package/version/metadata identity.

    try:
        artifact_mounts = ()
        if selected_artifacts:
            class _InjectedTransport:
                def fetch_chunks(self, url: str):
                    assert request._artifact_fetcher is not None
                    data = request._artifact_fetcher(url)
                    if not isinstance(data, bytes):
                        raise TypeError("artifact fetcher must return bytes")
                    yield data

            transport = (_InjectedTransport() if request._artifact_fetcher
                         else artifact_cache.HttpStreamingTransport())
            if request._artifact_cache_root is not None:
                os.makedirs(runtime_tmp_root, mode=0o700, exist_ok=True)
            configured_root = os.path.abspath(runtime_cache_root)
            if os.path.islink(configured_root):
                raise artifact_cache.ArtifactMaterializationError(
                    "containment", "runtime artifact cache root is a symlink",
                )
            root = os.path.realpath(configured_root)
            blobs = artifact_cache.materialize_selected_artifacts(
                selected_artifacts,
                transport=transport,
                filesystem=artifact_cache.LocalCacheFilesystem(),
                lock_factory=artifact_cache.FileIdentityLockFactory(
                    root, lock_root=runtime_locks_root,
                ),
                temp_dir=artifact_cache.LocalTemporaryDirectory(),
                temp_root=runtime_tmp_root,
                cache_root=root,
            )
            artifact_mounts = plan_artifact_mounts(blobs.values())
    except Exception as exc:
        return RunResult(
            exit_kind=ExitKind.OPERATIONAL,
            message=f"Failed to materialize runtime artifacts: {exc}",
        )

    # ── boundary validation ────────────────────────────────
    if request.executor is None:
        return RunResult(
            exit_kind=ExitKind.OPERATIONAL,
            message="No executor configured",
        )
    if request.inspector is None:
        return RunResult(
            exit_kind=ExitKind.OPERATIONAL,
            message="No container inspector configured",
        )

    # ── Step 4: create projection ───────────────────────────
    factory = request._create_projection
    if factory is None:
        # The real factory generates a unique non-existent path inside the
        # selected constructor project's external runtime namespace. Do NOT
        # pre-create a file — create_runtime_projection uses atomic hard-link
        # promotion with no-clobber semantics.
        def _real_factory(
            projection: EffectiveRuntimeProjection, *, parent_dir: str
        ) -> RuntimeProjectionHandle:
            # Keep generated projections in the selected constructor project's
            # external namespace even when persistent cache roots are redirected.
            # Supply a unique non-existent path for atomic no-clobber publish.
            import uuid
            from docker.versioning.effective import Filesystem
            return create_runtime_projection(
                projection,
                host_path=os.path.join(parent_dir, f"runtime-{uuid.uuid4().hex}.toml"),
                _fs=Filesystem(runtime_root=parent_dir),
            )

        factory = _real_factory

    try:
        handle = factory(
            effective,
            parent_dir=runtime_projection_root,
        )
    except Exception as exc:
        return RunResult(
            exit_kind=ExitKind.CONFIG,
            message=f"Failed to create runtime projection: {exc}",
        )

    projection_hash: str | None = None
    try:
        with handle:
            # Capture projection identity
            projection_path = getattr(handle, "path", None)
            projection_hash = getattr(handle, "content_hash", None)
            projection_container_path = (
                "/run/pi-cli/docker-constructor.runtime.toml"
            )

            # ── Step 5: allocate pi-N ────────────────────────
            try:
                container_name = allocate_pi_name(request.inspector)
            except Exception as exc:
                return RunResult(
                    exit_kind=ExitKind.OPERATIONAL,
                    message=f"Failed to allocate container name: {exc}",
                )

            # ── Step 6: render vector ────────────────────────
            render_inputs = RunRenderInputs(
                image=request.image,
                container_name=container_name,
                pi_home_host=request.pi_home_host,
                projection_host_path=projection_path or "",
                projection_container_path=projection_container_path,
                workspace=request.selection.workspace,
                extra_workspaces=request.selection.extra_workspaces,
                host_access=host_access,
                tty=request.tty,
                stdin_open=request.stdin_open,
                command=request.command,
                chown_on_start=request.chown_on_start,
                artifact_mounts=artifact_mounts,
                corporate_trust_bundle=corporate_trust_bundle,
                proxy_url=proxy_url,
                proxy_no_proxy=proxy_no_proxy,
                project_state_runtime_root=str(project_state.runtime_root),
            )
            run_args = render_run_vector(render_inputs)

            # ── Step 7: execute ──────────────────────────────
            try:
                result = request.executor.run(
                    run_args,
                    interactive=(request.tty or request.stdin_open),
                )
            except Exception as exc:
                return RunResult(
                    exit_kind=ExitKind.OPERATIONAL,
                    message=str(exc),
                    run_args=run_args,
                    projection_path=projection_path,
                    projection_hash=projection_hash,
                    container_name=container_name,
                )

            if result.return_code == 0:
                return RunResult(
                    exit_kind=ExitKind.SUCCESS,
                    run_args=run_args,
                    process_result=result,
                    projection_path=projection_path,
                    projection_hash=projection_hash,
                    container_name=container_name,
                )
            else:
                return RunResult(
                    exit_kind=ExitKind.OPERATIONAL,
                    message=f"Container exited with code "
                            f"{result.return_code}",
                    run_args=run_args,
                    process_result=result,
                    projection_path=projection_path,
                    projection_hash=projection_hash,
                    container_name=container_name,
                )

    finally:
        # Step 8: projection is cleaned up by the context manager.
        # Handle attributes are captured above before __exit__ runs.
        pass


# ═══════════════════════════════════════════════════════════════════
# Docker-backed boundaries
# ═══════════════════════════════════════════════════════════════════


class DockerContainerInspector:
    """Real :class:`ContainerNameInspector` backed by ``docker ps -a``
    through an injectable :class:`ProcessRunner`."""

    _ARGV = ["docker", "ps", "-a", "--format", "{{.Names}}"]

    def __init__(self, runner: ProcessRunner) -> None:
        self._runner = runner

    def list_names(self) -> set[str]:
        """Run ``docker ps -a --format '{{.Names}}'`` and return the
        set of container names."""
        try:
            result = self._runner.run(list(self._ARGV),
                                      mode=ExecutionMode.CAPTURED)
        except OSError as exc:
            raise ContainerInspectError(str(exc)) from exc

        if result.return_code != 0:
            raise ContainerInspectError(
                result.stderr.strip() or "docker ps failed"
            )

        lines = result.stdout.split("\n")
        names: set[str] = set()
        for line in lines:
            stripped = line.strip()
            if stripped:
                names.add(stripped)
        return names


class DockerRunExecutor:
    """Real :class:`RunExecutor` backed by ``docker run ...``
    through an injectable :class:`ProcessRunner`."""

    def __init__(self, runner: ProcessRunner) -> None:
        self._runner = runner

    def run(self, argv: tuple[str, ...], *,
            interactive: bool = False) -> ProcessResult:
        """Execute the rendered ``docker`` argument vector."""
        mode = (
            ExecutionMode.INTERACTIVE if interactive
            else ExecutionMode.CAPTURED
        )
        return self._runner.run(list(argv), mode=mode)

"""Internal build and doctor orchestration — Stage 9.

This module owns:

* build inputs → effective projection → build vector rendering
* gateway diagnosis → rootless-override planning → persistence for doctor
* Docker execution through an injected ``ProcessRunner``
* explicit repair intent (doctor) with consent enforcement

It does **not** own argument parsing, user prompts, generic rendering,
exit-code selection, or terminal inspection — those remain in the facade
(``docker.constructor_cli``).

All side-effecting operations accept injectable fakes so tests require
neither a Docker daemon nor systemd.

**Stage 9.3 GREEN**: Real implementations of ``orchestrate_build``
and ``orchestrate_doctor`` with planning/execution separation.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field, replace as dataclass_replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence, TypedDict, cast

from docker.npm_environment.errors import LockedNpmError
from docker.networking import (
    DockerMode,
    GatewayDiagnosis,
    OverrideFailure,
    OverrideState,
    PersistenceResult,
    BuildOutputPolicy,
    ProcessResult,
    ProcessRunner,
    RootlessOverridePlan,
    diagnose_gateway,
    plan_rootless_override,
    apply_rootless_override,
)
from docker.versioning.dispatch_types import ExitKind
from docker.versioning.build_cache import (
    BuildCacheError, ConstructorProjectBuildLock, acquire_constructor_project_build_lock,
    commit_build_set, maintain_uncommitted_blobs,
    recover_abandoned_snapshots,
)
from docker.versioning.build_context_confinement import (
    BuildContextConfinement,
    ConfinementError,
    MaterializedConfinement,
    cleanup_build_context_confinement,
    materialize_build_context_confinement,
    plan_build_context_confinement,
)
from docker.versioning.cache_storage import prepare_project_root, resolve_effective_root
from docker.versioning.build_materialization import (
    HostNetworkPolicy,
    MaterializationError,
    StreamingTransport,
    UrllibStreamingTransport,
    materialize_build_artifacts,
    select_build_artifacts,
)
from docker.versioning.build_snapshot import (
    DerivedEnvironmentSource, MaterializedSnapshot, SnapshotError,
    cleanup_artifact_snapshot, create_artifact_snapshot,
)
from docker.versioning.effective import (
    EffectiveBuildProjection,
    resolve_build_projection,
)
from docker.versioning.errors import (
    InventoryError,
    UnsupportedOverrideError,
    VersionConfigError,
)
from docker.versioning.inventory import (
    load_project_configuration,
    resolve_corporate_trust_bundle_path,
    validate_corporate_trust_bundle,
)
from docker.versioning.model import BuildLocalInputs, HostAccessPolicy, Inventory
from docker.versioning.project_state import resolve_project_state
from docker.versioning.rendering import (
    BuildRenderInputs,
    CacheControls,
    DerivedEnvironment,
    _docker_platform,
    Materialized,
    Prospective,
    render_build_vector,
    render_command_display,
    render_prospective_build_display,
    write_effective_build,
)
from docker.versioning.pi_assembly import (
    PiAssemblyError,
    PiAssemblyRequest,
    PiMaterialization,
    materialize_pi,
)
from docker.versioning.pi_consumer import PiConsumerError
from docker.versioning.host_progress import (
    HostDiagnosticStream, HostEventSink, HostFailureContext, HostPhase,
    HostPhaseEvent, HostPhaseState, HostStep, lookup_host_failure, emit, guard_sink,
)
from docker.versioning.diagnostic_projection import project_exception_type_chain


# ═══════════════════════════════════════════════════════════════════════
# Injectables
# ═══════════════════════════════════════════════════════════════════════


class BuildExecutor(Protocol):
    """Injected Docker build execution — accepts the exact :func:`render_build_vector`
    tuple (not a list) and returns a :class:`~docker.networking.ProcessResult`.

    Distinct from :class:`~docker.networking.ProcessRunner`, which uses
    ``list[str]`` for gateway / service operations."""

    def run(self, argv: tuple[str, ...]) -> ProcessResult: ...


class SubprocessBuildExecutor:
    """Production executor with a fixed, typed output policy."""

    def __init__(self, output_policy: BuildOutputPolicy = BuildOutputPolicy.CAPTURED) -> None:
        if not isinstance(output_policy, BuildOutputPolicy):
            raise ValueError("output_policy must be a BuildOutputPolicy")
        self.output_policy = output_policy

    def run(self, argv: tuple[str, ...]) -> ProcessResult:
        """Execute *argv* via ``subprocess.run``.

        Raises:
            FileNotFoundError: when the ``docker`` binary is missing.
            OSError: on permission or other low-level failures.
        """
        if self.output_policy is BuildOutputPolicy.CAPTURED:
            completed = subprocess.run(
                list(argv), shell=False, text=True, capture_output=True,
            )
        else:
            completed = subprocess.run(list(argv), shell=False, text=True)
        return ProcessResult(
            argv=argv,
            return_code=completed.returncode,
            stdout=(completed.stdout or "") if self.output_policy is BuildOutputPolicy.CAPTURED else "",
            stderr=(completed.stderr or "") if self.output_policy is BuildOutputPolicy.CAPTURED else "",
            output_policy=self.output_policy,
        )


# ═══════════════════════════════════════════════════════════════════════
# Build orchestration types
# ═══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class BuildRequest:
    """Immutable all inputs for a build transaction.

    Covers task 9.1 item 7: inventory path, overrides, platform, tag,
    context/Dockerfile, target, cache/pull/progress, UID/GID, confirmation,
    dry-run.
    """

    inventory_path: str
    """Path to ``docker-constructor.toml``."""

    platform: str = "linux-amd64"
    """Target platform (``linux-amd64`` or ``linux-arm64``)."""

    tag: str | None = None
    """Optional image-tag override (default: ``pi-cli-pi:latest``)."""

    overrides: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({}),
    )
    """Override map for the build projection (PATH=VALUE pairs)."""

    target: str = "runtime"
    """Dockerfile target stage name."""

    context: str | None = None
    """Build context directory (default: parent of inventory)."""

    dockerfile: str | None = None
    """Path to the Dockerfile relative to context.

    ``None`` (default) means Docker resolves ``Dockerfile`` at the
    build-context root — matching the repository layout.
    """

    cache: bool = True
    """Enable Docker build cache."""

    pull: bool = False
    """Force pull base images (``--pull``)."""

    progress: str = "auto"
    """Progress output style: ``auto``, ``plain``, or ``tty``."""

    output_policy: BuildOutputPolicy = BuildOutputPolicy.CAPTURED
    """Build output policy. Captured is the backwards-compatible default."""

    uid: int | None = None
    """Host user UID injected via ``--build-arg DEV_UID=...``.

    Must be a non-negative integer.  ``None`` means use the default
    from ``BuildRenderInputs`` (usually 1000).
    """

    gid: int | None = None
    """Host user GID injected via ``--build-arg DEV_GID=...``.

    Must be a non-negative integer.  ``None`` means use the default
    from ``BuildRenderInputs`` (usually 1000).
    """

    confirmed: bool = False
    """Facade-obtained confirmation — an immutable boolean decision.

    ``True`` means the user explicitly agreed or ``--yes`` was active.
    Orchestration **never** prompts; it only enforces this flag.

    When ``False`` (and ``dry_run=False``) the transaction must return
    a ``SUCCESS`` cancellation without invoking any side effects
    (diagnosis, persistence, publication, or Docker execution).
    """

    dry_run: bool = False
    """When ``True``, render the build vector but do not invoke Docker."""

    runner: BuildExecutor | None = None
    """Injected build executor; ``None`` means execution impossible."""

    gateway_probe_image: str = "alpine:3.20"
    """Deprecated compatibility field; builds never use gateway probes."""

    _diagnose_gateway: Callable[..., GatewayDiagnosis] | None = None
    """Deprecated compatibility injection; builds never invoke it."""

    repo_root: str | None = None
    """Legacy generated-state plumbing pending Phase 3.

    This field must never select inventory companions or other project-owned
    inputs.
    """

    project_root: str | None = None
    """Selected constructor-project root owning companions and ``.docker-local``."""

    # ── injectable projection boundary (faked in tests) ──────────────
    _publish_projection: Callable[..., PublishResult] | None = None
    """Injectable projection publication — writes effective build projection.

    Returns a ``PublishResult`` or raises ``PublishError`` on failure.
    """

    _materialize_artifacts: Callable[..., object] | None = None
    """Injectable host materializer used before projection publication/Docker."""

    _transport_factory: Callable[[HostNetworkPolicy], StreamingTransport] | None = None
    """Injectable host transport factory (receives the resolved network policy)."""

    _named_context_supported: Callable[[], bool] | None = None
    """Injectable BuildKit named-context capability probe."""

    _materialize_pi: Callable[..., object] | None = None
    """Injectable host Pi materializer (preflight + assembly + launcher)."""

    _assembler_executor: object | None = None
    """Injectable assembler ``RunExecutor`` (defaults to DockerRunExecutor)."""

    event_sink: HostEventSink | None = None
    """Optional facade-owned host materialization presentation sink."""

    host_presentation_complete: Callable[[], None] | None = None
    """Facade-owned shutdown hook run before Docker/native output.

    It carries no presentation policy; the facade uses it to finalize the
    transient host region and join its single presentation worker before any
    native BuildKit output.  It is always secondary to the build result.
    """

    def __post_init__(self) -> None:
        """Normalize ``overrides`` to an immutable mapping.

        A frozen dataclass still stores the caller-owned dict; a caller
        holding a reference to the original dict could mutate the request
        after construction.  This normalizes any mutable ``dict`` to a
        ``MappingProxyType``."""
        if not isinstance(self.output_policy, BuildOutputPolicy):
            raise ValueError("output_policy must be a BuildOutputPolicy")
        object.__setattr__(self, "event_sink", guard_sink(self.event_sink))
        if not isinstance(self.overrides, MappingProxyType):
            object.__setattr__(self, "overrides", MappingProxyType(
                dict(self.overrides),
            ))


@dataclass(frozen=True)
class PublishResult:
    """Result of projection publication — task 12."""

    published_path: str
    """Canonical host-side path where the effective projection was written."""


@dataclass(frozen=True)
class PublishError(Exception):
    """Publication failure — task 12 (atomic write failure, etc.)."""

    detail: str


@dataclass(frozen=True)
class BuildResult:
    """Outcome of ``orchestrate_build()`` — structural, not rendered."""

    exit_kind: ExitKind
    """Operational exit kind before facade mapping."""

    message: str | None = None
    """Human-readable diagnostic."""

    build_args: tuple[str, ...] = ()
    """Rendered ``docker build`` argument vector (dry-run or pre-execution)."""

    display_string: str | None = None
    """Shell-escaped human-readable display string (distinct from executable tuple)."""

    process_result: ProcessResult | None = None
    """Captured subprocess outcome when execution was performed."""

    publish_result: PublishResult | None = None
    """Publication outcome when projection was written."""

    host_failure: HostFailureContext | None = None
    """Structured host failure context for the facade failure report."""


# ═══════════════════════════════════════════════════════════════════════
# Doctor / repair types
# ═══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class DoctorResult:
    """Outcome of ``orchestrate_doctor()`` — structural, not rendered."""

    exit_kind: ExitKind
    """Operational exit kind before facade mapping."""

    message: str | None = None
    """Human-readable diagnostic."""

    initial_diagnosis: GatewayDiagnosis | None = None
    """Raw ``GatewayDiagnosis`` from the first probe run."""

    override_plan: RootlessOverridePlan | None = None
    """Derived ``RootlessOverridePlan`` (``None`` when not applicable)."""

    repair_applied: bool = False
    """``True`` when a rootless override was successfully applied."""

    repair_failure: OverrideFailure | None = None
    """Structured ``OverrideFailure`` when repair could not complete."""

    post_repair_diagnosis: GatewayDiagnosis | None = None
    """``GatewayDiagnosis`` after repair (``None`` when repair not performed)."""

    selected_gateway: str | None = None
    """IP address of the chosen gateway after successful diagnosis."""

    persistence_result: PersistenceResult | None = None
    """Outcome of persisting the selected gateway for future runs."""


class _DiagnoseGatewayKwargs(TypedDict, total=False):
    probe_image: str
    probe_timeout: int
    _runner: ProcessRunner


@dataclass(frozen=True)
class DoctorRequest:
    """Immutable all inputs for a doctor transaction.

    Mirrors the typed request pattern of ``BuildRequest`` for symmetry.
    """

    apply_override: bool = False
    """Explicit repair intent — only ``True`` when ``--apply-rootless-override``
    is passed.  ``--yes`` alone must **not** imply repair."""

    repair_consent: bool = False
    """User consent for repair obtained by the facade before this call.

    The facade handles all prompting; the orchestration receives an
    immutable boolean.  True means the user explicitly confirmed
    the repair intent.  Ignored when apply_override is False.
    """

    probe_image: str = "alpine:3.20"
    """Docker image used for gateway probe containers."""

    probe_timeout: int | None = None
    """Timeout (seconds) for each gateway probe container (1–300)."""

    inventory_path: Path | None = None
    """Resolved inventory path for policy-aware doctor dispatch.

    When ``None``, doctor returns a disabled/no-op result — no
    implicit gateway diagnosis is performed without a reviewed
    docker-gateway policy."""

    # -- injectables (all default to ``None`` = use real implementations) --

    runner: ProcessRunner | None = None
    """Injected process runner for probe containers / service commands."""

    _diagnose_gateway: Callable[..., GatewayDiagnosis] | None = None
    _plan_rootless_override: Callable[..., RootlessOverridePlan] | None = None
    _apply_rootless_override: Callable[..., OverrideFailure | None] | None = None


# ═══════════════════════════════════════════════════════════════════════
# Internal planning DTO (Stage 9.3)
# ═══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class BuildTransactionPlan:
    """Immutable output of ``plan_build`` — everything needed to
    display or execute a build, computed without side effects."""

    exit_kind: ExitKind
    """``SUCCESS`` when planning completed, ``CONFIG`` on validation failure."""

    message: str | None = None
    """Diagnostic when planning fails."""

    build_args: tuple[str, ...] = ()
    """Rendered ``docker build`` argument vector."""

    display_string: str | None = None
    """Human-readable shell-escaped display string."""

    render_inputs: BuildRenderInputs | None = None
    """Inputs used to produce *build_args* (available for publication)."""

    inventory: Inventory | None = None
    """Loaded inventory (available for effective-projection serialisation)."""

    effective_projection: EffectiveBuildProjection | None = None
    """Resolved build projection (available for publication)."""

    host_network_policy: HostNetworkPolicy = HostNetworkPolicy()
    """Validated host transport policy, separate from Docker rendering."""

    cache_root: Path | None = None
    """Resolved invoking-user constructor cache root for execution."""


# ═══════════════════════════════════════════════════════════════════════
# Plan / execute (Stage 9.3)
# ═══════════════════════════════════════════════════════════════════════


def _publish_projection_default(projection, *, repo_root: Path, project_state) -> PublishResult:
    """Default publisher — writes only beneath resolved external state."""
    try:
        written = write_effective_build(
            projection, repo_root=repo_root, project_state=project_state,
        )
        return PublishResult(published_path=str(written))
    except Exception as exc:
        raise PublishError(detail=str(exc)) from exc


def plan_build(
    request: BuildRequest,
    *,
    inventory: Inventory | None = None,
    local_inputs: BuildLocalInputs | None = None,
) -> BuildTransactionPlan:
    """Load, validate, resolve, and render — no side effects.

    Returns a ``BuildTransactionPlan``.  When validation fails the plan
    carries ``exit_kind=CONFIG`` and a diagnostic message; the caller
    must not proceed to execution.

    ``inventory`` and ``local_inputs`` are the domain-owned configuration
    slices.  When both are supplied (the facade's single-transaction
    path) no document is read here.  When both are omitted this function
    performs its own one-shot load so direct callers keep working.  A
    partial pair is a programming error.
    """
    # 1. Load inventory (CONFIG on missing / invalid TOML / bad schema)
    inv_path = Path(request.inventory_path)
    local_project_root = (
        Path(request.project_root) if request.project_root is not None else None
    )
    if inventory is None and local_inputs is None:
        try:
            inventory, local_config = load_project_configuration(inv_path)
        except (VersionConfigError, OSError, ValueError, KeyError) as exc:
            return BuildTransactionPlan(
                exit_kind=ExitKind.CONFIG,
                message=str(exc),
            )
        local_inputs = BuildLocalInputs.from_local_config(local_config)
    elif inventory is None or local_inputs is None:
        raise ValueError("inventory and local_inputs must be supplied together")

    # 1b. Validate local corporate settings (proxy + enabled trust bundle)
    # before any side effect; proxy validation raises InventoryError from
    # the loader and an enabled invalid bundle raises from bundle validation.
    # The repository root is mandatory for enabled trust and is never
    # inferred from the inventory path.
    try:
        if local_inputs.corporate_trust_enabled:
            if local_project_root is None:
                raise InventoryError(
                    "corporate trust is enabled but project_root is absent; "
                    "cannot resolve <project-root>/.docker-local/corporate-ca-bundle.crt"
                )
            validate_corporate_trust_bundle(
                resolve_corporate_trust_bundle_path(local_project_root)
            )
    except InventoryError as exc:
        return BuildTransactionPlan(exit_kind=ExitKind.CONFIG, message=str(exc))

    # Resolve the same configured/default cache root used by runtime execution.
    try:
        cache_root = resolve_effective_root(
            local_inputs.cache_dir, xdg_cache_home=os.environ.get("XDG_CACHE_HOME"),
            home=Path.home(),
        )
    except Exception as exc:
        return BuildTransactionPlan(exit_kind=ExitKind.CONFIG, message=str(exc))

    # 2. Resolve effective build projection (validates overrides inline)
    try:
        projection = resolve_build_projection(
            inventory.build,
            request.overrides,
            platform=request.platform,
        )
    except (VersionConfigError, UnsupportedOverrideError, ValueError, KeyError) as exc:
        return BuildTransactionPlan(
            exit_kind=ExitKind.CONFIG,
            message=str(exc),
        )

    # 3. Build render inputs
    build_context = request.context if request.context is not None else str(inv_path.parent.absolute())
    tag = request.tag if request.tag is not None else "pi-cli-pi:latest"

    try:
        docker_platform = _docker_platform(request.platform)
        render_inputs = BuildRenderInputs(
            build_context=build_context,
            projection=projection,
            target_stage=request.target,
            image_tag=tag,
            platform=docker_platform,
            cache=CacheControls(enabled=request.cache),
            pull=request.pull,
            progress=request.progress,
            dockerfile=request.dockerfile,
            dev_uid=request.uid if request.uid is not None else 1000,
            dev_gid=request.gid if request.gid is not None else 1000,
            proxy_url=local_inputs.network_proxy_url,
            proxy_no_proxy=local_inputs.network_proxy_no_proxy,
            corporate_trust_enabled=local_inputs.corporate_trust_enabled,
            named_context=Prospective(),
        )

        # Validate fields shared by executable and prospective plans without
        # attempting argv rendering (which correctly rejects Prospective).
        if not build_context.strip():
            raise ValueError("build_context must not be empty")
        if not tag.strip():
            raise ValueError("image_tag must not be empty")
        if render_inputs.dev_uid < 0 or render_inputs.dev_gid < 0:
            raise ValueError("dev_uid and dev_gid must be >= 0")
        if request.progress not in ("auto", "plain", "tty"):
            raise ValueError("unsupported progress mode")

        # Dry-runs are deliberately non-executable and perform no probe,
        # cache, filesystem, network, or Docker operation.
        if request.dry_run:
            build_args = ()
            display_string = render_prospective_build_display(render_inputs)
        else:
            # The real path is assigned only after host materialization.
            build_args = ()
            display_string = None
    except (ValueError, VersionConfigError) as exc:
        return BuildTransactionPlan(
            exit_kind=ExitKind.CONFIG,
            message=str(exc),
        )

    return BuildTransactionPlan(
        exit_kind=ExitKind.SUCCESS,
        build_args=build_args,
        display_string=display_string,
        render_inputs=render_inputs,
        inventory=inventory,
        effective_projection=projection,
        host_network_policy=HostNetworkPolicy(
            proxy_url=local_inputs.network_proxy_url,
            ca_bundle=(
                local_project_root / ".docker-local" / "corporate-ca-bundle.crt"
                if local_inputs.corporate_trust_enabled
                and local_project_root is not None else None
            ),
        ),
        cache_root=cache_root,
    )


def _default_named_context_supported() -> bool:
    """Probe the exact ``docker build`` interface used by this constructor."""
    completed = subprocess.run(
        ["docker", "build", "--help"], shell=False, text=True,
        capture_output=True,
    )
    return completed.returncode == 0 and "--build-context" in completed.stdout


# Pi/assembler/consumer exceptions normalized to ``SnapshotError`` at the Pi
# materialization boundary so ``execute_build`` never depends on
# implementation-specific exception types.
_PI_MATERIALIZATION_ERRORS = (
    PiAssemblyError,
    PiConsumerError,
    LockedNpmError,
    OSError,
    ValueError,
)


def _exception_chain(reason: BaseException):
    """Yield *reason* and its cause/context chain without repeats."""
    seen: set[int] = set()
    current: BaseException | None = reason
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = getattr(current, "__cause__", None) or getattr(
            current, "__context__", None
        )


def _host_failure_context(reason: BaseException) -> HostFailureContext:
    """Build structured failure context from the preserved active step.

    Attribution is structural: the active phase, step, safe logical resource,
    and normalized host facts are attached at the boundary where they were
    known and retrieved through the exception's cause/context chain.  Nothing
    here inspects exception classes or message wording.  The bounded sanitized
    tail is reused verbatim from the deepest failure that already carries one
    (for example a locked-assembly ``detail``).
    """
    marker = lookup_host_failure(reason)
    if marker is not None:
        phase = marker.phase
        step = marker.step
        logical_resource = marker.logical_resource
        hostnames = marker.hostnames
    else:
        phase = HostPhase.RELEASE_ACQUISITION
        step = HostStep.ARTIFACT_ACQUISITION
        logical_resource = None
        hostnames = ()
    summary = "host operation failed"
    tail = ""
    tail_stream = None
    timeout_retained_context = False
    missing = object()
    for current in _exception_chain(reason):
        candidate_summary = getattr(current, "summary", None)
        candidate_tail = getattr(current, "diagnostic_tail", missing)
        if isinstance(candidate_summary, str) and candidate_summary:
            summary = candidate_summary
        if candidate_tail is not missing:
            # An explicitly empty structured tail is authoritative.  It means
            # the assembler produced no retained diagnostics.
            if isinstance(candidate_tail, str):
                tail = candidate_tail
                raw_stream = getattr(current, "diagnostic_stream", None)
                if raw_stream in ("stdout", "stderr"):
                    tail_stream = HostDiagnosticStream(raw_stream)
        elif isinstance(current, LockedNpmError):
            # Compatibility is restricted to genuinely legacy objects lacking
            # the structured field, never an explicitly empty tail.
            detail = getattr(current, "detail", None)
            if isinstance(detail, str) and detail:
                tail = detail
        if getattr(current, "reason", None) == "assembly_timeout":
            timeout_retained_context = True
        if summary != "host operation failed" or candidate_tail is not missing or tail:
            break
    return HostFailureContext(
        phase=phase,
        step=step,
        summary=summary,
        tail=tail,
        logical_resource=logical_resource,
        hostnames=hostnames,
        exception_types=project_exception_type_chain(reason),
        tail_stream=tail_stream,
        timeout_retained_context=timeout_retained_context,
    )


def _materialize_pi_for_build(
    request: BuildRequest,
    plan: BuildTransactionPlan,
    projection: EffectiveBuildProjection,
    transport: StreamingTransport,
    cache_root: Path,
) -> PiMaterialization:
    """Materialize the reviewed Pi environment before snapshot creation.

    Uses the injected Pi materializer when present, otherwise the production
    ``materialize_pi`` pipeline with a real ``DockerRunExecutor``.  Every
    Pi/assembler/consumer exception is normalized to ``SnapshotError`` so
    ``execute_build`` treats it as a single materialization-boundary failure.
    """
    try:
        if request._materialize_pi is not None:
            kwargs = dict(
                transport=transport,
                cache_root=cache_root,
                executor=request._assembler_executor,
                uid=request.uid,
                gid=request.gid,
                proxy_url=plan.host_network_policy.proxy_url,
                proxy_no_proxy=(
                    plan.render_inputs.proxy_no_proxy
                    if plan.render_inputs is not None else None
                ),
                corporate_trust_bundle=(
                    str(plan.host_network_policy.ca_bundle)
                    if plan.host_network_policy.ca_bundle is not None else None
                ),
            )
            # The event protocol is optional: old injected materializers keep
            # their existing call shape, while sink-aware implementations opt in.
            try:
                import inspect
                parameters = inspect.signature(request._materialize_pi).parameters
            except (TypeError, ValueError):
                parameters = {}
            if "event_sink" in parameters or any(
                parameter.kind is parameter.VAR_KEYWORD
                for parameter in parameters.values()
            ):
                kwargs["event_sink"] = request.event_sink
            return cast(PiMaterialization, request._materialize_pi(projection, **kwargs))
        from docker.npm_environment.execution import DockerRunExecutor
        return materialize_pi(PiAssemblyRequest(
            projection=projection,
            transport=transport,
            cache_root=cache_root,
            executor=request._assembler_executor or DockerRunExecutor(),
            uid=request.uid,
            gid=request.gid,
            proxy_url=plan.host_network_policy.proxy_url,
            proxy_no_proxy=(
                plan.render_inputs.proxy_no_proxy
                if plan.render_inputs is not None else None
            ),
            corporate_trust_bundle=(
                str(plan.host_network_policy.ca_bundle)
                if plan.host_network_policy.ca_bundle is not None else None
            ),
            event_sink=request.event_sink,
        ))
    except _PI_MATERIALIZATION_ERRORS as exc:
        raise SnapshotError(f"Pi materialization failed: {exc}") from exc


def execute_build(
    plan: BuildTransactionPlan,
    request: BuildRequest,
) -> BuildResult:
    """Publish the effective build projection and execute Docker.

    Callers must have already validated ``plan.exit_kind == SUCCESS``
    and confirmed ``request.confirmed is True`` (and that this is not
    a dry-run). Gateway diagnosis belongs to the explicit ``doctor``
    transaction and is intentionally not a build precondition.
    """
    build_args = plan.build_args
    display_string = plan.display_string
    if request.project_root is None:
        return BuildResult(
            exit_kind=ExitKind.CONFIG,
            message="project_root is required for generated build state",
            build_args=build_args,
            display_string=display_string,
        )
    constructor_project = Path(request.project_root).resolve()

    # 1. Confine the host-only constructor documents to the host before any
    # materialization, publication, or Docker invocation. Fail closed when the
    # effective context or Dockerfile cannot be confined safely.
    assert plan.render_inputs is not None
    try:
        confinement = plan_build_context_confinement(
            inventory_path=Path(request.inventory_path),
            context=plan.render_inputs.build_context,
            dockerfile=plan.render_inputs.dockerfile,
        )
    except ConfinementError as exc:
        return BuildResult(
            exit_kind=ExitKind.CONFIG,
            message=f"build context confinement rejected: {exc}",
            build_args=build_args, display_string=display_string,
        )

    # 2. Fail before download, publication, or a Docker build when the local
    # client cannot import BuildKit named contexts.
    supported = request._named_context_supported or _default_named_context_supported
    try:
        if not supported():
            raise SnapshotError("Docker BuildKit named-context support is required")
    except (OSError, SnapshotError) as exc:
        return BuildResult(
            exit_kind=ExitKind.OPERATIONAL,
            message=("Docker BuildKit named contexts are required; use a Docker "
                     "build interface that advertises --build-context "
                     f"({exc})"),
            build_args=build_args, display_string=display_string,
        )

    # 2. Materialize every selected artifact before publishing any reference
    # or invoking Docker. The transport is the sole owner of host HTTP policy.
    materialize = request._materialize_artifacts or materialize_build_artifacts
    projection = plan.effective_projection
    if projection is None:
        return BuildResult(
            exit_kind=ExitKind.OPERATIONAL,
            message="build artifact materialization failed: missing effective projection",
            build_args=build_args,
            display_string=display_string,
        )
    # Resolve constructor identity once; all implicit build state shares it.
    snapshot: MaterializedSnapshot | None = None
    lock: ConstructorProjectBuildLock | None = None
    confinement_artifacts: MaterializedConfinement | None = None

    def cleanup_confinement() -> None:
        nonlocal confinement_artifacts
        current = confinement_artifacts
        confinement_artifacts = None
        cleanup_build_context_confinement(current)

    try:
        if plan.cache_root is None:
            raise SnapshotError("missing resolved constructor cache root")
        project_state = resolve_project_state(
            constructor_project, cache_root=prepare_project_root(plan.cache_root),
        )
        lock = acquire_constructor_project_build_lock(
            constructor_project, cache_root=project_state.cache_root,
        )
        recover_abandoned_snapshots(
            constructor_project, lock=lock, cache_root=project_state.cache_root,
            project_state=project_state,
        )
        maintain_uncommitted_blobs(
            constructor_project, lock=lock, cache_root=project_state.cache_root,
            project_state=project_state,
        )
        if confinement.active:
            # Recovery removes every entry in the transaction root, so the
            # generated confinement state is published only after it runs and
            # before any artifact materialization, publication, or Docker call.
            confinement_artifacts = materialize_build_context_confinement(
                confinement, generated_root=project_state.transactions_root,
            )
        selected_artifacts = tuple(select_build_artifacts(projection))
        transport_factory = request._transport_factory or UrllibStreamingTransport
        transport = transport_factory(plan.host_network_policy)
        materialize_kwargs: dict[str, object] = dict(
            constructor_project_root=constructor_project, cache_root=project_state.cache_root,
            project_state=project_state, transport=transport, lock=lock,
        )
        # The event protocol is optional: old injected materializers keep their
        # existing call shape, while sink-aware implementations opt in.  Each
        # optional keyword is forwarded independently so an implementation that
        # accepts ``event_sink`` but not ``failure_secrets`` still works.
        try:
            import inspect
            materialize_parameters = inspect.signature(materialize).parameters
        except (TypeError, ValueError):
            materialize_parameters = {}
        accepts_kwargs = any(
            parameter.kind is parameter.VAR_KEYWORD
            for parameter in materialize_parameters.values()
        )
        if accepts_kwargs or "event_sink" in materialize_parameters:
            materialize_kwargs["event_sink"] = request.event_sink
        if accepts_kwargs or "failure_secrets" in materialize_parameters:
            materialize_kwargs["failure_secrets"] = tuple(
                value for value in (
                    plan.host_network_policy.proxy_url,
                    (
                        str(plan.host_network_policy.ca_bundle)
                        if plan.host_network_policy.ca_bundle is not None else None
                    ),
                ) if value is not None
            )
        materialized = cast(Callable[..., object], materialize)(
            projection, **materialize_kwargs
        )
        if not isinstance(materialized, (tuple, list)) or not all(
            isinstance(path, Path) for path in materialized
        ):
            raise SnapshotError("artifact materializer returned invalid blob paths")
        blobs = tuple(materialized)
        # Materialize the reviewed Pi environment (acquisition → preflight →
        # Docker-backed assembly → consumer launcher) before snapshot creation.
        pi_materialization = _materialize_pi_for_build(
            request, plan, projection, transport, project_state.cache_root,
        )
        if plan.render_inputs is None:
            raise SnapshotError("missing build rendering inputs")
        attestation = DerivedEnvironment(
            assembled_output_identity=pi_materialization.output_identity,
            canonical_tree_digest=pi_materialization.tree_digest,
            assembler_evidence_digest=pi_materialization.assembler_evidence_digest,
            assembler_evidence_bytes_digest=pi_materialization.assembler_evidence_bytes_digest,
            consumer_launcher_evidence_digest=pi_materialization.launcher_evidence_digest,
        )
        derived = DerivedEnvironmentSource(
            environment_root=pi_materialization.result.environment_root,
            launcher_contents=pi_materialization.launcher_plan.contents,
            launcher_mode=pi_materialization.launcher_plan.mode,
            assembler_evidence=pi_materialization.result.evidence_path.read_bytes(),
            launcher_evidence=pi_materialization.launcher_evidence.data,
            assembler_evidence_digest=attestation.assembler_evidence_digest,
            assembler_evidence_bytes_digest=attestation.assembler_evidence_bytes_digest,
            launcher_evidence_digest=attestation.consumer_launcher_evidence_digest,
            assembled_output_identity=attestation.assembled_output_identity,
            canonical_tree_digest=attestation.canonical_tree_digest,
        )
        snapshot = create_artifact_snapshot(
            selected_artifacts, blobs, constructor_project_root=constructor_project,
            cache_root=project_state.cache_root, project_state=project_state,
            derived=derived,
        )
        render_inputs = dataclass_replace(plan.render_inputs, named_context=Materialized(
            str(snapshot.path), attestation,
        ))
        if confinement_artifacts is not None:
            # The generated Dockerfile copy carries the Dockerfile-specific
            # ignore file that forces the source documents out of the context.
            render_inputs = dataclass_replace(
                render_inputs, dockerfile=str(confinement_artifacts.dockerfile),
            )
        build_args = render_build_vector(render_inputs)
        display_string = render_command_display(build_args)
    except ConfinementError as exc:
        try:
            cleanup_confinement()
        except BaseException:
            pass
        if lock is not None:
            lock.release()
        return BuildResult(
            exit_kind=ExitKind.CONFIG,
            message=f"build context confinement failed: {exc}",
            build_args=build_args, display_string=display_string,
        )
    except (MaterializationError, BuildCacheError, SnapshotError, OSError, ValueError) as exc:
        failure = _host_failure_context(exc)
        try:
            cleanup_confinement()
        except BaseException:
            pass
        cleanup_detail = ""
        try:
            cleanup_artifact_snapshot(snapshot)
        except (SnapshotError, OSError) as cleanup_exc:
            cleanup_detail = f"; snapshot cleanup failed: {cleanup_exc}"
        if lock is not None:
            lock.release()
        return BuildResult(
            exit_kind=ExitKind.OPERATIONAL,
            message=(
                f"build artifact materialization failed: {exc}{cleanup_detail}"
            ),
            build_args=build_args,
            display_string=display_string,
            host_failure=failure,
        )
    except BaseException:
        try:
            cleanup_confinement()
        except BaseException:
            # Never replace interruption/unexpected primary failure with cleanup.
            pass
        try:
            cleanup_artifact_snapshot(snapshot)
        except BaseException:
            # Never replace interruption/unexpected primary failure with cleanup.
            pass
        if lock is not None:
            lock.release()
        raise

    # 4–5. Snapshot cleanup is the final transaction precondition. A live-set
    # commit is allowed only after Docker succeeds and cleanup succeeds.
    def finish_snapshot() -> None:
        nonlocal snapshot
        current = snapshot
        snapshot = None
        cleanup_artifact_snapshot(current)

    try:
        publish = request._publish_projection
        try:
            if publish is not None:
                publish_result = publish(plan.effective_projection, repo_root=constructor_project)
            else:
                publish_result = _publish_projection_default(
                    plan.effective_projection, repo_root=constructor_project,
                    project_state=project_state,
                )
        except Exception as exc:
            try:
                finish_snapshot()
            except (SnapshotError, OSError) as cleanup_exc:
                return BuildResult(
                    exit_kind=ExitKind.OPERATIONAL,
                    message=f"failed to clean transaction snapshot: {cleanup_exc}",
                    build_args=build_args, display_string=display_string,
                )
            return BuildResult(
                exit_kind=ExitKind.OPERATIONAL,
                message=f"failed to publish effective projection: {getattr(exc, 'detail', str(exc))}",
                build_args=build_args, display_string=display_string,
                host_failure=HostFailureContext(
                    phase=HostPhase.DERIVED_VALIDATION,
                    step=HostStep.PUBLICATION,
                    summary="failed to publish effective projection",
                    exception_types=project_exception_type_chain(exc),
                ),
            )

        runner = request.runner or SubprocessBuildExecutor(request.output_policy)
        emit(request.event_sink, HostPhaseEvent(HostPhase.DOCKER_TRANSITION, HostPhaseState.STARTED))
        emit(request.event_sink, HostPhaseEvent(HostPhase.DOCKER_TRANSITION, HostPhaseState.SUCCEEDED))
        if request.host_presentation_complete is not None:
            try:
                request.host_presentation_complete()
            except Exception:
                # Host presentation shutdown is always secondary to the build.
                pass
        try:
            proc = runner.run(build_args)
        except FileNotFoundError as exc:
            try:
                finish_snapshot()
            except (SnapshotError, OSError) as cleanup_exc:
                return BuildResult(exit_kind=ExitKind.OPERATIONAL,
                    message=f"failed to clean transaction snapshot: {cleanup_exc}",
                    build_args=build_args, display_string=display_string,
                    publish_result=publish_result)
            return BuildResult(exit_kind=ExitKind.OPERATIONAL,
                message=f"docker executable not found: {exc}", build_args=build_args,
                display_string=display_string, publish_result=publish_result)
        except OSError as exc:
            try:
                finish_snapshot()
            except (SnapshotError, OSError) as cleanup_exc:
                return BuildResult(exit_kind=ExitKind.OPERATIONAL,
                    message=f"failed to clean transaction snapshot: {cleanup_exc}",
                    build_args=build_args, display_string=display_string,
                    publish_result=publish_result)
            return BuildResult(exit_kind=ExitKind.OPERATIONAL,
                message=f"docker execution failed: {exc}", build_args=build_args,
                display_string=display_string, publish_result=publish_result)

        if proc.return_code != 0:
            try:
                finish_snapshot()
            except (SnapshotError, OSError) as cleanup_exc:
                return BuildResult(exit_kind=ExitKind.OPERATIONAL,
                    message=f"failed to clean transaction snapshot: {cleanup_exc}",
                    build_args=build_args, display_string=display_string,
                    process_result=proc, publish_result=publish_result)
            message = (f"build exited with code {proc.return_code}"
                if getattr(proc, "output_policy", BuildOutputPolicy.CAPTURED) is BuildOutputPolicy.STREAMED
                else (proc.stderr or f"build exited with code {proc.return_code}"))
            return BuildResult(exit_kind=ExitKind.OPERATIONAL, message=message,
                build_args=build_args, display_string=display_string,
                process_result=proc, publish_result=publish_result)

        try:
            finish_snapshot()
        except (SnapshotError, OSError) as exc:
            return BuildResult(exit_kind=ExitKind.OPERATIONAL,
                message=f"failed to clean transaction snapshot: {exc}",
                build_args=build_args, display_string=display_string,
                process_result=proc, publish_result=publish_result)

        try:
            assert lock is not None
            commit_build_set(
                constructor_project, {selected.identity for selected in selected_artifacts},
                lock=lock, cache_root=project_state.cache_root,
                project_state=project_state,
            )
        except BuildCacheError as exc:
            return BuildResult(exit_kind=ExitKind.OPERATIONAL,
                message=f"failed to commit successful build artifacts: {exc}",
                build_args=build_args, display_string=display_string,
                process_result=proc, publish_result=publish_result)
        return BuildResult(exit_kind=ExitKind.SUCCESS, message="image build completed",
            build_args=build_args, display_string=display_string,
            process_result=proc, publish_result=publish_result)
    finally:
        primary_exception = sys.exc_info()[0] is not None
        try:
            cleanup_confinement()
        except BaseException:
            if not primary_exception:
                raise
        try:
            if snapshot is not None:
                cleanup_artifact_snapshot(snapshot)
        except BaseException:
            if not primary_exception:
                raise
            # A cleanup problem is secondary to an interrupt/primary failure.
        finally:
            if lock is not None:
                lock.release()


# ═══════════════════════════════════════════════════════════════════════
# Public orchestration (Stage 9.3)
# ═══════════════════════════════════════════════════════════════════════


def orchestrate_build(
    request: BuildRequest,
    *,
    inventory: Inventory | None = None,
    local_inputs: BuildLocalInputs | None = None,
) -> BuildResult:
    """Orchestrate a complete build transaction.

    Flow:
    1. Plan — load, validate, resolve, render (no side effects)
    2. If plan fails: return CONFIG
    3. If dry-run: return plan with SUCCESS
    4. If not confirmed: return SUCCESS cancellation
    5. Execute — publish and invoke Docker
    """
    plan = plan_build(request, inventory=inventory, local_inputs=local_inputs)
    if plan.exit_kind != ExitKind.SUCCESS:
        return BuildResult(
            exit_kind=plan.exit_kind,
            message=plan.message,
            build_args=plan.build_args,
            display_string=plan.display_string,
        )

    if request.dry_run:
        return BuildResult(
            exit_kind=ExitKind.SUCCESS,
            build_args=plan.build_args,
            display_string=plan.display_string,
        )

    if not request.confirmed:
        return BuildResult(
            exit_kind=ExitKind.SUCCESS,
            message="build not confirmed",
        )

    return execute_build(plan, request)


def _persist_host_access_address(
    companion_path: Path,
    address: str,
    *,
    _fs: Any = None,
    _tmp_suffix: str | None = None,
) -> PersistenceResult:
    """Atomically write ``[host-access].address`` to the local companion.

    Preserves all other recognised sections (including ``[cache]``),
    comments, and blank lines.  Never adds reviewed-policy fields
    (``enabled``, ``mode``, ``proxy-port``).

    Returns a ``PersistenceResult`` — callers inspect ``.written`` to
    decide whether the operation succeeded.
    """
    import os
    import time

    if _fs is None:
        from docker.networking import Filesystem as _Fs
        _fs = _Fs()

    if not address or not address.strip():
        return PersistenceResult(
            path=companion_path, address=address,
            written=False, error="address must be non-empty",
        )

    if _fs.is_symlink(companion_path):
        return PersistenceResult(
            path=companion_path, address=address,
            written=False,
            error="local companion must not be a symlink; replace it with a real file",
        )

    unique = (
        _tmp_suffix
        if _tmp_suffix is not None
        else f"{os.getpid()}.{int(time.time() * 1_000_000)}"
    )
    tmp = companion_path.with_name(f"{companion_path.name}.tmp.{unique}")

    # Read-and-update existing content, preserving everything except
    # [host-access].address.
    lines: list[str] = []
    if _fs.is_file(companion_path):
        lines = _fs.read_text(companion_path).splitlines()

    new_lines: list[str] = []
    in_host_access = False
    address_written = False

    for line in lines:
        stripped = line.strip()
        # Detect section headers — tolerate trailing inline comments
        section_name = _parse_toml_section_header(stripped)
        if section_name is not None:
            in_host_access = (section_name == "host-access")
            new_lines.append(line)
            if in_host_access and not address_written:
                new_lines.append(f'address = "{address}"')
                address_written = True
            continue

        # Skip comment lines — must not trigger key detection
        if stripped.startswith("#"):
            new_lines.append(line)
            continue

        if in_host_access and _is_toml_key_line(stripped, "address"):
            # Replace existing address line only when not already written
            if not address_written:
                new_lines.append(f'address = "{address}"')
                address_written = True
            # else: skip duplicate address lines — section header
            # insertion already handled it
            continue

        new_lines.append(line)

    if not address_written:
        new_lines.append("")
        new_lines.append("[host-access]")
        new_lines.append(f'address = "{address}"')

    content = "\n".join(new_lines) + "\n"

    try:
        _fs.write_text(tmp, content)
        _fs.rename(tmp, companion_path)
    except OSError as exc:
        if _fs.is_file(tmp):
            try:
                _fs.delete(tmp)
            except OSError:
                pass
        return PersistenceResult(
            path=companion_path, address=address,
            written=False, error=str(exc),
        )
    finally:
        if _fs.is_file(tmp):
            try:
                _fs.delete(tmp)
            except OSError:
                pass

    return PersistenceResult(
        path=companion_path, address=address, written=True,
    )


def _is_toml_key_line(stripped: str, key: str) -> bool:
    """True when *stripped* is a TOML assignment to *key*."""
    if "=" not in stripped:
        return False
    left = stripped.split("=", 1)[0].strip()
    return left == key


def _parse_toml_section_header(stripped: str) -> str | None:
    """Return the section name if *stripped* is a TOML section header.

    Handles trailing inline comments: ``[host-access] # comment``.
    Returns ``None`` when *stripped* is not a section header.
    """
    # TOML inline comments start with # outside a string.
    # Section headers have no string values, so splitting on # is safe.
    bare = stripped.split("#", 1)[0].strip()
    if bare.startswith("[") and bare.endswith("]"):
        return bare[1:-1].strip()
    return None


def _resolve_doctor_host_access(
    inventory_path: Path | None,
) -> tuple[str | None, Path | None, str | None]:
    """Resolve the host-access mode and companion path for doctor.

    Returns ``(mode, companion_path, error)``.

    * ``error`` is not ``None`` — the inventory was explicitly supplied
      but is unreadable, malformed, or failed validation.  The caller
      must return a ``CONFIG`` ``DoctorResult`` with the error message.
    * ``mode=None`` — policy is disabled or inventory was not supplied;
      doctor finishes successfully without probing.
    * ``mode="docker-gateway"``, ``companion=<Path>`` — normal
      docker-gateway flow with atomic local persistence.
    * ``mode="external-address"`` — no probing; user address is used.
    """
    if inventory_path is None:
        # Without a reviewed inventory the host-access policy cannot
        # be determined — no implicit gateway diagnosis.
        return None, None, None
    try:
        inv, _local = load_project_configuration(Path(inventory_path))
    except Exception as exc:
        return None, None, f"cannot load inventory: {exc}"
    ha = getattr(inv.runtime, "host_access", None)
    if ha is None:
        return None, None, None
    if not isinstance(ha, HostAccessPolicy):
        return None, None, None
    if not ha.enabled:
        return None, None, None
    mode = ha.mode
    if not mode:
        return None, None, None
    from docker.versioning.inventory import resolve_local_companion_path
    companion = resolve_local_companion_path(inventory_path)
    return mode, companion, None


def _persist_selected_gateway(
    gateway: str | None,
    *,
    companion: Path | None = None,
) -> tuple[PersistenceResult | None, str | None]:
    """Persist a verified gateway to the local companion."""
    if gateway is None:
        return None, None
    path = companion
    if path is None:
        return None, None
    try:
        result = _persist_host_access_address(path, gateway)
    except Exception as exc:
        return None, str(exc)
    if not result.written:
        return result, result.error or "unknown error"
    return result, None


def orchestrate_doctor(request: DoctorRequest) -> DoctorResult:
    """Orchestrate a complete doctor (gateway diagnosis + optional repair).

    Flow:
    0. Resolve host-access mode from reviewed inventory.
       - disabled / absent → success, no probing
       - external-address → success, no probing, preserve local address
       - docker-gateway → continue to step 1
    1. Initial diagnosis (only for docker-gateway mode)
    2. Derive override plan (always, even without repair)
    3. If ``apply_override`` and ``repair_consent`` and not rootful:
       a. Skip apply when plan state is MATCHING (already no-op)
       b. Apply override
       c. Re-diagnose on success
    4. If ``apply_override`` but rootful: return POLICY
    5. Otherwise return diagnosis-only result
    """
    # 0. Mode-aware dispatch
    mode, companion, resolve_error = _resolve_doctor_host_access(request.inventory_path)
    if resolve_error is not None:
        return DoctorResult(
            exit_kind=ExitKind.CONFIG,
            message=resolve_error,
        )
    if mode is None:
        return DoctorResult(
            exit_kind=ExitKind.SUCCESS,
            message="Host access is disabled — no gateway diagnosis needed.",
        )
    if mode == "external-address":
        return DoctorResult(
            exit_kind=ExitKind.SUCCESS,
            message="Host access is external-address — no gateway diagnosis needed. "
                    "The locally configured address is used as-is.",
        )
    # docker-gateway mode continues below

    # 1. Initial diagnosis
    diagnose = request._diagnose_gateway or diagnose_gateway
    diagnose_kwargs = _DiagnoseGatewayKwargs()
    if request.probe_image is not None:
        diagnose_kwargs["probe_image"] = request.probe_image
    if request.probe_timeout is not None:
        diagnose_kwargs["probe_timeout"] = request.probe_timeout
    if request.runner is not None:
        diagnose_kwargs["_runner"] = request.runner
    try:
        initial = diagnose(**diagnose_kwargs)
    except Exception as exc:
        return DoctorResult(
            exit_kind=ExitKind.OPERATIONAL,
            message=f"gateway diagnosis failed: {exc}",
        )
    gateway = initial.resolved_address
    initial_persistence, error = _persist_selected_gateway(gateway, companion=companion)
    if error is not None:
        return DoctorResult(
            exit_kind=ExitKind.OPERATIONAL,
            message=f"cannot persist gateway: {error}",
            initial_diagnosis=initial,
            selected_gateway=gateway,
            persistence_result=initial_persistence,
        )

    if gateway is None:
        # Diagnosis-only (no repair intent) → permanently unreachable
        if not request.apply_override:
            detail = "no route"
            if initial.probes:
                first = initial.probes[0]
                if first.detail:
                    detail = first.detail
            return DoctorResult(
                exit_kind=ExitKind.OPERATIONAL,
                message=f"no working gateway IP found: {detail}",
                initial_diagnosis=initial,
                selected_gateway=None,
            )
        # Repair requested and rootless → the override exists precisely
        # to fix this connectivity failure.  Continue through plan/apply.
        if initial.mode != DockerMode.ROOTLESS:
            return DoctorResult(
                exit_kind=ExitKind.OPERATIONAL,
                message="no working gateway IP found and Docker is not rootless",
                initial_diagnosis=initial,
                selected_gateway=None,
            )

    # 2. Derive override plan — always (planning is read-only).
    #    Uses the injected fake when present, otherwise the real
    #    ``plan_rootless_override``.
    plan_fn = request._plan_rootless_override or plan_rootless_override
    try:
        override_plan = plan_fn(_mode=initial.mode)
    except Exception as exc:
        return DoctorResult(
            exit_kind=ExitKind.OPERATIONAL,
            message=f"override planning failed: {exc}",
            initial_diagnosis=initial,
            selected_gateway=gateway,
        )

    # 3. Repair not requested → diagnosis-only success
    if not request.apply_override:
        return DoctorResult(
            exit_kind=ExitKind.SUCCESS,
            initial_diagnosis=initial,
            override_plan=override_plan,
            selected_gateway=gateway,
            persistence_result=initial_persistence,
            repair_applied=False,
        )

    # 4. Repair requested but Docker is rootful → POLICY
    if initial.mode == DockerMode.ROOTFUL:
        return DoctorResult(
            exit_kind=ExitKind.POLICY,
            message="rootless override not applicable: Docker is rootful",
            initial_diagnosis=initial,
            override_plan=override_plan,
            selected_gateway=gateway,
            persistence_result=initial_persistence,
            repair_applied=False,
        )

    # 5. Repair requested — check consent
    if not request.repair_consent:
        return DoctorResult(
            exit_kind=ExitKind.SUCCESS,
            message="repair consent denied",
            initial_diagnosis=initial,
            override_plan=override_plan,
            selected_gateway=gateway,
            persistence_result=initial_persistence,
            repair_applied=False,
        )

    # 6. Already matching — no-op
    #    If the override is already installed but the gateway is still
    #    unreachable, the no-op repair cannot restore connectivity.
    if override_plan is not None and override_plan.state == OverrideState.MATCHING:
        if gateway is None:
            return DoctorResult(
                exit_kind=ExitKind.OPERATIONAL,
                message=(
                    "gateway unreachable and rootless override "
                    "already matching; repair cannot help"
                ),
                initial_diagnosis=initial,
                override_plan=override_plan,
                selected_gateway=None,
                repair_applied=False,
            )
        return DoctorResult(
            exit_kind=ExitKind.SUCCESS,
            initial_diagnosis=initial,
            override_plan=override_plan,
            selected_gateway=gateway,
            persistence_result=initial_persistence,
            repair_applied=False,
        )

    # 7. Apply override
    apply_fn = request._apply_rootless_override or apply_rootless_override
    try:
        failure = apply_fn(plan=override_plan, consent=request.repair_consent)
    except Exception as exc:
        return DoctorResult(
            exit_kind=ExitKind.OPERATIONAL,
            message=f"override application failed: {exc}",
            initial_diagnosis=initial,
            override_plan=override_plan,
            selected_gateway=gateway,
            persistence_result=initial_persistence,
            repair_applied=False,
        )
    if failure is not None:
        return DoctorResult(
            exit_kind=ExitKind.OPERATIONAL,
            message=f"override application failed: {failure.detail}",
            initial_diagnosis=initial,
            override_plan=override_plan,
            repair_failure=failure,
            selected_gateway=gateway,
            persistence_result=initial_persistence,
            repair_applied=False,
        )

    # 8. Re-diagnose after successful repair
    try:
        post = diagnose(**diagnose_kwargs)
    except Exception as exc:
        return DoctorResult(
            exit_kind=ExitKind.OPERATIONAL,
            message=f"post-repair diagnosis failed: {exc}",
            initial_diagnosis=initial,
            override_plan=override_plan,
            selected_gateway=gateway,
            persistence_result=initial_persistence,
            repair_applied=True,
        )

    post_gateway = post.resolved_address
    persistence, error = _persist_selected_gateway(post_gateway, companion=companion)
    if error is not None:
        return DoctorResult(
            exit_kind=ExitKind.OPERATIONAL,
            message=f"cannot persist gateway: {error}",
            initial_diagnosis=initial,
            override_plan=override_plan,
            post_repair_diagnosis=post,
            selected_gateway=post_gateway,
            persistence_result=persistence,
            repair_applied=True,
        )
    return DoctorResult(
        exit_kind=ExitKind.SUCCESS if post_gateway else ExitKind.OPERATIONAL,
        message=None if post_gateway else "gateway unreachable after repair",
        initial_diagnosis=initial,
        override_plan=override_plan,
        post_repair_diagnosis=post,
        selected_gateway=post_gateway,
        persistence_result=persistence,
        repair_applied=True,
    )


# ═══════════════════════════════════════════════════════════════════════
# Public doctor API (Stage 9.4)
# ═══════════════════════════════════════════════════════════════════════


def diagnose_doctor(
    *,
    probe_image: str | None = None,
    probe_timeout: int | None = None,
    _diagnose_gateway: Callable[..., GatewayDiagnosis] | None = None,
    _plan_rootless_override: Callable[..., RootlessOverridePlan] | None = None,
) -> DoctorResult:
    """Run gateway diagnosis without repair — always read-only.

    Returns a ``DoctorResult`` with the initial diagnosis and override
    plan, but ``repair_applied`` is always ``False``.
    """
    request = DoctorRequest(
        apply_override=False,
        repair_consent=False,
        probe_image=probe_image or "alpine:3.20",
        probe_timeout=probe_timeout,
        _diagnose_gateway=_diagnose_gateway,
        _plan_rootless_override=_plan_rootless_override,
    )
    return orchestrate_doctor(request)


def repair_rootless(
    *,
    consent: bool,
    probe_image: str | None = None,
    probe_timeout: int | None = None,
    _diagnose_gateway: Callable[..., GatewayDiagnosis] | None = None,
    _plan_rootless_override: Callable[..., RootlessOverridePlan] | None = None,
    _apply_rootless_override: Callable[..., OverrideFailure | None] | None = None,
) -> DoctorResult:
    """Diagnose gateway and apply rootless override when applicable.

    *consent* must be ``True`` for the override to be applied.
    When Docker is rootful, returns ``POLICY``.
    """
    request = DoctorRequest(
        apply_override=True,
        repair_consent=consent,
        probe_image=probe_image or "alpine:3.20",
        probe_timeout=probe_timeout,
        _diagnose_gateway=_diagnose_gateway,
        _plan_rootless_override=_plan_rootless_override,
        _apply_rootless_override=_apply_rootless_override,
    )
    return orchestrate_doctor(request)

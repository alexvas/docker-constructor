"""Host-only Pi release acquisition, assembly, and launcher materialization.

This module wires the reviewed Pi release contract to the standalone locked
npm-environment assembler and the Pi consumer launcher:

1. Derive the exact release-asset URLs from the reviewed release contract.
2. Acquire and checksum-verify the two installation assets (never exposed to
   BuildKit).
3. Run the side-effect-free assembler preflight with the exact lock bytes,
   the exact install-package bytes (bound to the lockfile root), the
   reviewed root, and the caller-owned reviewed Node/npm versions.
4. Select ``bin.pi`` from the keyed reviewed-root metadata.
5. Run Docker-backed assembly with binding rechecks and no launcher creation.
6. Plan the consumer launcher (containment-verified) and its evidence.

Every side effect — download, assembler execution, and cache publication — is
injectable, so tests can drive the full pipeline without a Docker daemon.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
from pathlib import Path

from docker.npm_environment import (
    AssemblyResult,
    CorporateNetworkPolicy,
    RootSpec,
    assemble_environment,
    assembler_script_digest,
    build_tree_manifest,
    compute_assembler_identity,
    npm_policy_digest,
    preflight,
)
from docker.npm_environment.streaming import _internal_direct_enqueue_sink
from docker.versioning.activity_monitor import HostActivityMonitor
from docker.versioning.assembly_activity import HostAssemblyActivity
from docker.versioning.build_materialization import StreamingTransport
from docker.versioning.diagnostic_identity import SessionUrlIdentity
from docker.versioning.diagnostic_projection import (
    DiagnosticLogicalResource,
    DiagnosticResourceKind,
    project_host_acquisition_failure,
)
from docker.versioning.host_progress import (
    HostDiagnosticStream,
    HostEventSink,
    InternalDirectHostEventSink,
    HostPhase,
    HostPhaseEvent,
    HostPhaseState,
    HostStep,
    HostStepState,
    HostStructuredDiagnostic,
    attach_host_failure,
    emit,
    guard_sink,
)
from docker.versioning.npm_diagnostic_stream import (
    classify_npm_diagnostic,
    make_stream_factory,
    project_tail,
)
from docker.versioning.model import EffectiveBuildProjection, PiReleaseSource
from docker.versioning.pi_consumer import (
    LauncherEvidence,
    LauncherPlan,
    launcher_evidence,
    plan_launcher,
    select_pi_metadata,
)
from docker.versioning.pi_release import (
    INSTALL_PACKAGE_FILENAME,
    INSTALL_PACKAGE_LOCK_FILENAME,
    SHA256SUMS_FILENAME,
    PiReleaseError,
    ProgressCallback,
    acquire_install_assets,
    derive_pi_release_urls,
    download_bytes,
)

# Versioning uses ``linux-amd64``; the assembler uses npm's ``linux-x64``.
_ASSEMBLER_PLATFORM = "linux-x64"


class PiAssemblyError(RuntimeError):
    """Pi acquisition, preflight, assembly, or launcher failure."""


@dataclass(frozen=True)
class PiAssemblyRequest:
    """Inputs for host Pi materialization."""

    projection: EffectiveBuildProjection
    transport: StreamingTransport
    cache_root: str | Path
    executor: object
    """Injected ``RunExecutor`` for the assembler's Docker run."""
    uid: int | None = None
    gid: int | None = None
    proxy_url: str | None = None
    proxy_no_proxy: str | None = None
    corporate_trust_bundle: str | None = None
    event_sink: HostEventSink | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_sink", guard_sink(self.event_sink))


@dataclass(frozen=True)
class PiMaterialization:
    """Fully materialized Pi environment and its attestation bindings."""

    result: AssemblyResult
    launcher_plan: LauncherPlan
    launcher_evidence: LauncherEvidence
    output_identity: str
    tree_digest: str
    assembler_evidence_digest: str
    """Canonical evidence-body digest bound into assembled-output identity."""
    assembler_evidence_bytes_digest: str
    """SHA-256 of the exact serialized assembler-evidence bytes."""
    launcher_evidence_digest: str


def _corporate_network(request: PiAssemblyRequest) -> CorporateNetworkPolicy:
    return CorporateNetworkPolicy(
        proxy_url=request.proxy_url,
        proxy_no_proxy=request.proxy_no_proxy,
        corporate_trust_bundle=request.corporate_trust_bundle,
    )


def materialize_pi(request: PiAssemblyRequest) -> PiMaterialization:
    """Acquire, preflight, assemble, and evidence the reviewed Pi environment.

    Performs no BuildKit snapshot exposure of the installation assets and no
    npm/network activity inside BuildKit.  All four attested values are
    computed only after successful host-side assembly and launcher planning.
    """
    projection = request.projection
    release = projection.pi_release
    source = PiReleaseSource(
        package=release.package,
        release_repository=release.release_repository,
        release_tag_prefix=release.release_tag_prefix,
    )

    # 1. Exact release-asset URLs and checksum-verified acquisition.
    emit(request.event_sink, HostPhaseEvent(HostPhase.RELEASE_ACQUISITION, HostPhaseState.STARTED))
    urls = derive_pi_release_urls(source, projection.pi_version)
    asset_urls = {
        SHA256SUMS_FILENAME: urls.sha256sums,
        INSTALL_PACKAGE_FILENAME: urls.install_package,
        INSTALL_PACKAGE_LOCK_FILENAME: urls.install_package_lock,
    }
    failure_secrets = tuple(
        value for value in (request.proxy_url, request.corporate_trust_bundle)
        if value is not None
    )

    @contextmanager
    def _asset_activity(name: str):
        """Scope one reviewed Pi asset's acquisition with safe failure context."""
        resource = DiagnosticLogicalResource(
            DiagnosticResourceKind.PI_RELEASE_ASSET, name
        )
        monitor = (
            HostActivityMonitor(
                phase=HostPhase.RELEASE_ACQUISITION,
                step=HostStep.RELEASE_ACQUISITION,
                expects_diagnostic_stream=False,
                sink=request.event_sink,
                logical_resource=resource.name,
            )
            if request.event_sink is not None
            else None
        )
        try:
            yield (
                monitor.record_transport_progress
                if monitor is not None
                else lambda _received_bytes: None
            )
        except BaseException as exc:
            attach_host_failure(
                exc,
                phase=HostPhase.RELEASE_ACQUISITION,
                step=HostStep.RELEASE_ACQUISITION,
                logical_resource=resource.name,
            )
            emit(
                request.event_sink,
                project_host_acquisition_failure(
                    phase=HostPhase.RELEASE_ACQUISITION,
                    step=HostStep.RELEASE_ACQUISITION,
                    logical_resource=resource,
                    reason=exc,
                    url=asset_urls[name],
                    secrets=failure_secrets,
                ),
            )
            if monitor is not None:
                monitor.finish(HostStepState.FAILED)
            raise
        else:
            if monitor is not None:
                monitor.finish(HostStepState.SUCCEEDED)

    def _download(url: str, progress: ProgressCallback | None = None) -> bytes:
        return download_bytes(request.transport, url, progress=progress)

    try:
        package_bytes, lock_bytes = acquire_install_assets(
            urls, _download,
            activity=_asset_activity,
        )
    except BaseException as exc:
        emit(request.event_sink, HostPhaseEvent(HostPhase.RELEASE_ACQUISITION, HostPhaseState.FAILED))
        if isinstance(exc, PiReleaseError):
            raise PiAssemblyError(f"Pi release acquisition failed: {exc}") from exc
        raise
    emit(request.event_sink, HostPhaseEvent(HostPhase.RELEASE_ACQUISITION, HostPhaseState.SUCCEEDED))

    # 2. Side-effect-free preflight with the exact official lock bytes and
    # the exact official install-package bytes (both are bound assembler
    # inputs — the package manifest is never downloaded-and-discarded).
    roots = (RootSpec(release.package, projection.pi_version),)
    validated = preflight(
        lock_bytes,
        package_bytes=package_bytes,
        roots=roots,
        platform=_ASSEMBLER_PLATFORM,
        node_version=projection.node.node_version,
        npm_version=projection.node.npm_version,
    )

    # 3. Select bin.pi only from the exact reviewed root's keyed metadata.
    metadata = select_pi_metadata(validated, package=release.package)

    # 4. Docker-backed assembly with binding rechecks; creates no launcher.
    assembler = compute_assembler_identity(
        image_digest=projection.node.image,
        node_version=projection.node.node_version,
        npm_version=projection.node.npm_version,
        script_digest=assembler_script_digest(),
        policy_digest=npm_policy_digest(),
        platform=_ASSEMBLER_PLATFORM,
    )
    emit(request.event_sink, HostPhaseEvent(HostPhase.LOCKED_ASSEMBLY, HostPhaseState.STARTED))
    corporate_network = _corporate_network(request)
    session_identity = SessionUrlIdentity()
    activity = HostAssemblyActivity(request.event_sink)

    def _structured_sink(chunk) -> None:
        emit(
            request.event_sink,
            HostStructuredDiagnostic(
                phase=HostPhase.LOCKED_ASSEMBLY,
                step=HostStep.NPM_EXECUTION,
                stream=HostDiagnosticStream(chunk.stream),
                classification=classify_npm_diagnostic(chunk.text),
                text=chunk.text,
                hostnames=chunk.hostnames,
                logical_resource=activity.current_container_name,
                url_fingerprints=chunk.url_fingerprints,
            ),
        )

    # Only the nominal facade-owned inbox adapter is safe for reader-thread
    # direct admission. Arbitrary SDK callbacks remain behind the dispatcher.
    stream_sink = (
        _internal_direct_enqueue_sink(_structured_sink)
        if isinstance(request.event_sink, InternalDirectHostEventSink)
        else _structured_sink
    )

    try:
        result = assemble_environment(
            validated=validated,
            assembler=assembler,
            cache_root=request.cache_root,
            executor=request.executor,
            uid=request.uid,
            gid=request.gid,
            corporate_network=corporate_network,
            sink=stream_sink if request.event_sink is not None else None,
            stream_factory=make_stream_factory(
                corporate_network.secrets(),
                session_identity,
                on_chunk=(
                    activity.record_diagnostic
                ),
            ),
            tail_projector=project_tail,
            activity=activity,
        )
    except BaseException:
        emit(request.event_sink, HostPhaseEvent(HostPhase.LOCKED_ASSEMBLY, HostPhaseState.FAILED))
        raise
    emit(request.event_sink, HostPhaseEvent(HostPhase.LOCKED_ASSEMBLY, HostPhaseState.SUCCEEDED))

    # 5–6. Consumer launcher/evidence and the attested-tree verification.
    emit(request.event_sink, HostPhaseEvent(HostPhase.DERIVED_VALIDATION, HostPhaseState.STARTED))
    try:
        launcher_plan = plan_launcher(metadata, environment_root=result.environment_root)
        evidence = launcher_evidence(launcher_plan)
        recomputed_tree_digest = build_tree_manifest(result.environment_root).digest
        if recomputed_tree_digest != result.tree_digest:
            raise PiAssemblyError("assembled Pi tree digest does not match the attested value")
    except BaseException as exc:
        attach_host_failure(
            exc,
            phase=HostPhase.DERIVED_VALIDATION,
            step=HostStep.VALIDATION,
        )
        emit(request.event_sink, HostPhaseEvent(HostPhase.DERIVED_VALIDATION, HostPhaseState.FAILED))
        raise
    emit(request.event_sink, HostPhaseEvent(HostPhase.DERIVED_VALIDATION, HostPhaseState.SUCCEEDED))

    evidence_bytes = result.evidence_path.read_bytes()
    return PiMaterialization(
        result=result,
        launcher_plan=launcher_plan,
        launcher_evidence=evidence,
        output_identity=result.output_identity,
        tree_digest=result.tree_digest,
        assembler_evidence_digest=result.evidence_digest,
        assembler_evidence_bytes_digest=hashlib.sha256(evidence_bytes).hexdigest(),
        launcher_evidence_digest=evidence.digest,
    )


__all__ = [
    "PiAssemblyError",
    "PiAssemblyRequest",
    "PiMaterialization",
    "materialize_pi",
]

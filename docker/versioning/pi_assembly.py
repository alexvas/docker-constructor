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
import functools
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
from docker.npm_environment.streaming import (
    SplitDiagnosticSink,
    StreamChunk,
    _internal_direct_enqueue_sink,
)
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
    HostDiagnosticClassification,
    HostDiagnosticEnvelope,
    HostDiagnosticPrefix,
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
from docker.versioning.model import (
    EffectiveBuildProjection, NetworkUrlDisplay, PiReleaseSource,
)
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


def structured_diagnostic_for(
    chunk: StreamChunk, *, logical_resource: str | None
) -> HostStructuredDiagnostic:
    """Build the external-safe structured diagnostic for one finalized chunk.

    The external DTO carries only the URL-free, path-free projected ``text``;
    it never receives the mode-selected local text or a fetch group key.
    """
    return HostStructuredDiagnostic(
        phase=HostPhase.LOCKED_ASSEMBLY,
        step=HostStep.NPM_EXECUTION,
        stream=HostDiagnosticStream(chunk.stream),
        classification=classify_npm_diagnostic(chunk.text),
        text=chunk.text,
        hostnames=chunk.hostnames,
        logical_resource=logical_resource,
        url_fingerprints=chunk.url_fingerprints,
    )


def presentation_envelope_for(
    chunk: StreamChunk, *, logical_resource: str | None
) -> HostDiagnosticEnvelope:
    """Build the internal presentation-only envelope for one finalized chunk.

    The envelope carries the mode-selected local text (falling back to the
    URL-free projection when the collector selected none) and, only for a
    recognized fetch, the policy-specific group key and canonical latency-free
    text.  It is delivered only to the nominal internal presentation actor and
    is deliberately absent from the external SDK event contract.
    """
    return HostDiagnosticEnvelope(
        phase=HostPhase.LOCKED_ASSEMBLY,
        step=HostStep.NPM_EXECUTION,
        stream=HostDiagnosticStream(chunk.stream),
        classification=classify_npm_diagnostic(chunk.text),
        text=chunk.local_text if chunk.local_text is not None else chunk.text,
        hostnames=chunk.hostnames,
        logical_resource=logical_resource,
        url_fingerprints=chunk.url_fingerprints,
        fetch_key=chunk.fetch_key,
        fetch_text=chunk.fetch_text,
    )


def route_committed_prefix(
    chunk: StreamChunk,
    *,
    text: str,
    internal_sink: InternalDirectHostEventSink | None,
    logical_resource: str | None,
    finalized: bool = False,
    overflowed: bool = False,
) -> None:
    """Admit one cumulative presentation-only committed prefix.

    A committed prefix is not a complete npm diagnostic: it reaches only the
    authorized internal presentation actor and is never routed to an external
    SDK callback.  ``text`` is the whole mode-selected line committed so far;
    the actor replaces the provisional snapshot rather than appending it.
    Best-effort live presentation never affects assembly.
    """
    if internal_sink is None:
        return
    prefix = HostDiagnosticPrefix(
        phase=HostPhase.LOCKED_ASSEMBLY,
        step=HostStep.NPM_EXECUTION,
        stream=HostDiagnosticStream(chunk.stream),
        text=text,
        logical_resource=logical_resource,
        finalized=finalized,
        overflowed=overflowed,
    )
    try:
        internal_sink.admit_prefix(prefix)
    except Exception:
        # Best-effort live presentation never affects assembly.
        pass


def route_finalized_diagnostic(
    chunk: StreamChunk,
    *,
    internal_sink: InternalDirectHostEventSink | None,
    sdk_sink: HostEventSink | None,
    logical_resource: str | None,
) -> None:
    """Fan one finalized, non-overflowed chunk out to independent channels.

    A nominal internal presentation actor, when present, receives the internal
    envelope carrying the mode-selected local text and any recognized fetch
    identity.  The optional SDK sink, when present, *independently* receives
    the URL-free, path-free structured diagnostic.  The two channels are
    isolated: an internal admission failure never prevents SDK delivery, and an
    SDK callback failure never affects internal presentation or assembly.
    Internal-only fields (fetch identity and selected local text) never reach
    the SDK sink.
    """
    if internal_sink is not None:
        try:
            internal_sink.admit_diagnostic(
                presentation_envelope_for(
                    chunk, logical_resource=logical_resource
                )
            )
        except Exception:
            # Best-effort live presentation never affects SDK delivery or
            # assembly.
            pass
    if sdk_sink is not None:
        emit(
            sdk_sink,
            structured_diagnostic_for(chunk, logical_resource=logical_resource),
        )


def overflow_diagnostic_for(
    chunk: StreamChunk, *, logical_resource: str | None
) -> HostStructuredDiagnostic:
    """Build one bounded, safe truncation diagnostic for an oversized line.

    An oversized line is not a complete npm diagnostic: it is classified as a
    neutral status and carries no hostname or URL-fingerprint metadata, so
    nothing derived from the discarded suffix escapes and no warning, retry,
    timeout, error, or npm-fetch event can be inferred from the incomplete
    text.
    """
    return HostStructuredDiagnostic(
        phase=HostPhase.LOCKED_ASSEMBLY,
        step=HostStep.NPM_EXECUTION,
        stream=HostDiagnosticStream(chunk.stream),
        classification=HostDiagnosticClassification.STATUS,
        text=chunk.text,
        hostnames=(),
        logical_resource=logical_resource,
        url_fingerprints=(),
    )


def route_overflow_diagnostic(
    chunk: StreamChunk,
    *,
    internal_sink: InternalDirectHostEventSink | None,
    sdk_sink: HostEventSink | None,
    logical_resource: str | None,
) -> None:
    """Finalize one overflowed line on independent channels.

    The internal actor, when present, receives the record boundary it needs to
    stop provisional rendering of the committed prefix and overflow marker
    (it already displayed them), carrying the complete mode-selected local
    representation.  The SDK sink, when present, independently receives
    exactly one bounded, safe overflow diagnostic.  A provisional prefix is
    never routed to the SDK sink.
    """
    if internal_sink is not None:
        local_text = (
            chunk.local_text
            if chunk.local_text is not None
            else chunk.text
        )
        prefix = HostDiagnosticPrefix(
            phase=HostPhase.LOCKED_ASSEMBLY,
            step=HostStep.NPM_EXECUTION,
            stream=HostDiagnosticStream(chunk.stream),
            text=local_text,
            logical_resource=logical_resource,
            finalized=True,
            overflowed=True,
        )
        try:
            internal_sink.admit_prefix(prefix)
        except Exception:
            # Best-effort live presentation never affects SDK delivery or
            # assembly.
            pass
    if sdk_sink is not None:
        emit(
            sdk_sink,
            overflow_diagnostic_for(chunk, logical_resource=logical_resource),
        )


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
    sdk_event_sink: HostEventSink | None = None
    """Optional independent SDK structured-diagnostic sink."""
    network_url_display: NetworkUrlDisplay = NetworkUrlDisplay.REDACTED

    def __post_init__(self) -> None:
        if not isinstance(self.network_url_display, NetworkUrlDisplay):
            raise ValueError("network_url_display must be a NetworkUrlDisplay")
        object.__setattr__(self, "event_sink", guard_sink(self.event_sink))
        object.__setattr__(
            self, "sdk_event_sink", guard_sink(self.sdk_event_sink)
        )


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

    #: Provisional prefix text accumulated per stream for the internal
    #: presentation route.  Each emitted snapshot is self-contained, so
    #: moving a prefix event never loses an earlier committed segment.
    prefix_buffers: dict[str, list[str]] = {}

    def _admit_prefix(
        chunk, *, text: str, finalized: bool, overflowed: bool
    ) -> None:
        """Route one presentation-only prefix to the internal actor only."""
        route_committed_prefix(
            chunk,
            text=text,
            internal_sink=internal_sink,
            logical_resource=activity.current_container_name,
            finalized=finalized,
            overflowed=overflowed,
        )

    def _route_internal(chunk) -> None:
        """Admit internal presentation directly on the reader thread.

        Provisional prefixes are accumulated here, *before* any lossy queue,
        and the per-stream buffer is reset at every finalized record
        (including an overflow record) so provisional snapshots can never
        span unrelated records even when the external SDK channel is
        backlogged or dropping.  Each admitted snapshot is cumulative, so
        mailbox supersession replaces the provisional rendering instead of
        combining it with an unrelated line.  This path only touches the
        bounded, non-blocking presentation inbox and never an SDK callback.
        """
        if not chunk.finalized:
            # A committed prefix is not a complete npm diagnostic: its
            # mode-selected ``local_text`` fragment reaches only the
            # authorized internal presentation actor and is never routed to
            # an external SDK callback.  A chunk that advances only the safe
            # structured projection carries no local fragment and is skipped.
            if internal_sink is None or chunk.local_text is None:
                return
            buffer = prefix_buffers.setdefault(chunk.stream, [])
            buffer.append(chunk.local_text)
            _admit_prefix(
                chunk,
                text="".join(buffer),
                finalized=False,
                overflowed=chunk.overflowed,
            )
            return
        # A finalized line ends the logical record; its prefix snapshot is no
        # longer needed.  Reset before admitting the finalized envelope so the
        # next record's provisional snapshot starts empty.
        prefix_buffers.pop(chunk.stream, None)
        if chunk.overflowed:
            # The internal actor finalizes the overflow boundary (it already
            # displayed the committed prefix and overflow marker) carrying the
            # complete mode-selected local representation.
            route_overflow_diagnostic(
                chunk,
                internal_sink=internal_sink,
                sdk_sink=None,
                logical_resource=activity.current_container_name,
            )
            return
        route_finalized_diagnostic(
            chunk,
            internal_sink=internal_sink,
            sdk_sink=None,
            logical_resource=activity.current_container_name,
        )

    def _route_sdk(chunk) -> None:
        """Deliver one finalized, safe diagnostic to the external SDK sink.

        Runs only on the dispatcher thread (the SDK callback is never invoked
        on a reader thread).  Non-finalized chunks do not reach this handler
        (see :class:`SplitDiagnosticSink`), and only URL-free, path-free
        :class:`HostStructuredDiagnostic` events are emitted, so provisional
        prefixes and internal-only fetch identity never leak externally.
        """
        if not chunk.finalized:
            return
        if chunk.overflowed:
            # Exactly one bounded, safe truncation diagnostic; a provisional
            # prefix is never routed to the SDK sink.
            route_overflow_diagnostic(
                chunk,
                internal_sink=None,
                sdk_sink=sdk_sink,
                logical_resource=activity.current_container_name,
            )
            return
        route_finalized_diagnostic(
            chunk,
            internal_sink=None,
            sdk_sink=sdk_sink,
            logical_resource=activity.current_container_name,
        )

    internal_sink = (
        request.event_sink
        if isinstance(request.event_sink, InternalDirectHostEventSink)
        else None
    )
    # The SDK structured channel is independent of the presentation inbox.  An
    # explicit ``sdk_event_sink`` is honored even when the facade event sink is
    # the internal actor; a plain (non-internal) ``event_sink`` remains the SDK
    # channel for backward compatibility.
    sdk_sink = request.sdk_event_sink
    if sdk_sink is None and internal_sink is None:
        sdk_sink = request.event_sink

    # Presentation-only sessions admit prefixes and finalized envelopes
    # directly from the reader threads (bounded, non-blocking).  When an SDK
    # channel is also configured, the two paths stay separate: the internal
    # actor is admitted directly and only finalized, safe diagnostics enter the
    # asynchronous SDK dispatcher, so SDK backlog can never delay, drop, or
    # reorder internal presentation (and a dropped external record can never
    # make the prefix buffer span records).
    if internal_sink is not None and sdk_sink is None:
        stream_sink = _internal_direct_enqueue_sink(_route_internal)
    elif internal_sink is not None and sdk_sink is not None:
        stream_sink = SplitDiagnosticSink(_route_internal, _route_sdk)
    elif sdk_sink is not None:
        # No internal actor: the external channel still delivers only the
        # finalized, safe diagnostics; provisional prefix traffic never enters
        # the external queue.
        stream_sink = SplitDiagnosticSink(None, _route_sdk)
    else:
        stream_sink = None

    try:
        result = assemble_environment(
            validated=validated,
            assembler=assembler,
            cache_root=request.cache_root,
            executor=request.executor,
            uid=request.uid,
            gid=request.gid,
            corporate_network=corporate_network,
            sink=stream_sink,
            stream_factory=make_stream_factory(
                corporate_network.secrets(),
                session_identity,
                on_chunk=(
                    activity.record_diagnostic
                ),
                network_url_display=request.network_url_display,
            ),
            tail_projector=functools.partial(
                project_tail,
                network_url_display=request.network_url_display,
            ),
            activity=activity,
            network_url_display=request.network_url_display,
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
    "overflow_diagnostic_for",
    "presentation_envelope_for",
    "route_committed_prefix",
    "route_finalized_diagnostic",
    "route_overflow_diagnostic",
    "structured_diagnostic_for",
]

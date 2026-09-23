"""Host-side selection and streaming materialization of reviewed build artifacts."""
from __future__ import annotations

import hashlib
import os
import ssl
import stat
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Protocol, Sequence

from docker.versioning.activity_monitor import HostActivityMonitor
from docker.versioning.build_cache import (
    BLOB_EXTENSION, BuildCacheError, ConstructorProjectBuildLock, build_blob_path,
    mark_uncommitted_blob, prepare_build_cache,
)
from docker.versioning.diagnostic_projection import (
    DiagnosticLogicalResource, DiagnosticResourceKind,
    project_host_acquisition_failure,
)
from docker.versioning.digest_identity import DigestIdentity
from docker.versioning.host_progress import (
    HostEventSink, HostPhase, HostStep, HostStepEvent, HostStepState,
    attach_host_failure, emit,
)
from docker.versioning.project_state import ProjectState
from docker.versioning.model import EffectiveBuildProjection


class MaterializationError(RuntimeError):
    """Artifact selection, transport, or integrity failure safe for display."""


@dataclass(frozen=True)
class SelectedBuildArtifact:
    name: str
    url: str
    identity: DigestIdentity


@dataclass(frozen=True)
class HostNetworkPolicy:
    proxy_url: str | None = None
    ca_bundle: Path | None = None


class StreamingTransport(Protocol):
    def stream(self, url: str) -> Iterable[bytes]: ...


class UrllibStreamingTransport:
    """One host HTTP policy implementation; response bodies are never buffered."""
    def __init__(self, policy: HostNetworkPolicy = HostNetworkPolicy()) -> None:
        handlers: list[urllib.request.BaseHandler] = []
        if policy.proxy_url is not None:
            handlers.append(urllib.request.ProxyHandler({
                "http": policy.proxy_url, "https": policy.proxy_url,
            }))
        if policy.ca_bundle is not None:
            try:
                context = ssl.create_default_context(cafile=os.fspath(policy.ca_bundle))
            except (OSError, ssl.SSLError):
                raise MaterializationError(
                    "host artifact transport configuration failed"
                ) from None
            handlers.append(urllib.request.HTTPSHandler(context=context))
        self._opener = urllib.request.build_opener(*handlers)

    def stream(self, url: str) -> Iterable[bytes]:
        try:
            with self._opener.open(url) as response:
                while chunk := response.read(1024 * 1024):
                    yield chunk
        except Exception as exc:
            # Never include the request URL (or proxy URL) in diagnostics.
            # The cause is retained so the shared projection can report its
            # bounded exception-type chain without evaluating any message.
            raise MaterializationError(
                f"artifact transport failed ({type(exc).__name__})"
            ) from exc


def select_build_artifacts(projection: EffectiveBuildProjection) -> tuple[SelectedBuildArtifact, ...]:
    """Select exactly the four effective artifacts supported by this change."""
    if projection.platform != "linux-amd64":
        raise MaterializationError(
            f"unsupported build-artifact platform {projection.platform!r}; expected 'linux-amd64'"
        )
    values = (
        ("rustup", projection.rust.rustup),
        ("uv", projection.uv.artifact),
        ("rtk", projection.rtk.artifact),
        ("fd", projection.fd.artifact),
    )
    try:
        return tuple(SelectedBuildArtifact(
            name=name, url=artifact.url,
            identity=DigestIdentity.from_hex("sha256", artifact.sha256),
        ) for name, artifact in values)
    except (TypeError, ValueError) as exc:
        raise MaterializationError(f"invalid reviewed build-artifact digest: {exc}") from None


def _verify_hit(path: Path, identity: DigestIdentity) -> bool:
    try:
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode) != 0o444:
            return False
        digest = hashlib.new(identity.algorithm)
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        return digest.digest() == identity.digest_bytes
    except (FileNotFoundError, OSError):
        return False


def materialize_artifact(
    selected: SelectedBuildArtifact, *, constructor_project_root: str | Path,
    transport: StreamingTransport, cache_root: str | Path | None = None,
    project_state: ProjectState | None = None, lock: ConstructorProjectBuildLock | None = None,
    progress: Callable[[int], object] | None = None,
    on_cache_hit: Callable[[], None] | None = None,
) -> Path:
    """Reuse a verified hit or stream, verify, and atomically publish a miss.

    ``progress`` receives the cumulative received bytes after every chunk the
    existing streaming boundary yields; it is never called for a verified
    cache hit. ``on_cache_hit`` reports verified reuse without adding a request.
    """
    paths = prepare_build_cache(constructor_project_root, cache_root=cache_root, project_state=project_state)
    destination = build_blob_path(paths.blobs_root, selected.identity)
    if _verify_hit(destination, selected.identity):
        if on_cache_hit is not None:
            on_cache_hit()
        return destination
    if destination.exists() or destination.is_symlink():
        raise MaterializationError(f"unsafe or corrupt cached artifact {selected.name!r}")

    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".materialize-", dir=paths.tmp_root)
    temporary = Path(temporary_name)
    digest = hashlib.new(selected.identity.algorithm)
    received = 0
    try:
        with os.fdopen(fd, "wb") as output:
            for chunk in transport.stream(selected.url):
                if not isinstance(chunk, bytes):
                    raise MaterializationError("artifact transport yielded non-byte data")
                # Count the yielded bytes before the write/hash boundary: a
                # blocked or failed disk write must not hide bytes already
                # received from heartbeat or terminal progress.
                received += len(chunk)
                if progress is not None:
                    progress(received)
                output.write(chunk)
                digest.update(chunk)
            output.flush()
            os.fsync(output.fileno())
        if digest.digest() != selected.identity.digest_bytes:
            raise MaterializationError(
                f"integrity check failed for artifact {selected.name!r}"
            )
        os.chmod(temporary, 0o444)
        os.replace(temporary, destination)
        if not _verify_hit(destination, selected.identity):
            try: destination.unlink()
            except OSError: pass
            raise MaterializationError(f"published artifact {selected.name!r} failed verification")
        if lock is not None:
            try:
                mark_uncommitted_blob(
                    selected.identity, constructor_project_root, lock=lock,
                    cache_root=cache_root, project_state=project_state,
                )
            except BaseException:
                try: destination.unlink()
                except OSError: pass
                raise
        return destination
    except MaterializationError:
        raise
    except BaseException as exc:
        raise MaterializationError(
            f"artifact materialization failed for {selected.name!r} ({type(exc).__name__})"
        ) from exc
    finally:
        try: temporary.unlink()
        except FileNotFoundError: pass


def materialize_build_artifacts(
    projection: EffectiveBuildProjection, *, constructor_project_root: str | Path,
    transport: StreamingTransport, cache_root: str | Path | None = None,
    project_state: ProjectState | None = None, lock: ConstructorProjectBuildLock | None = None,
    event_sink: HostEventSink | None = None,
    monitor_factory: Callable[[str], HostActivityMonitor] | None = None,
    failure_secrets: Sequence[str] = (),
) -> tuple[Path, ...]:
    """Materialize all reviewed artifacts with per-asset operational activity.

    Each reviewed artifact is one ``artifact_acquisition`` operation carrying
    its closed logical name. Every chunk already yielded by the streaming
    transport updates cumulative received bytes and latest transport activity
    for the next heartbeat; a verified cache hit reports cache reuse and adds
    no request. Failures are attributed to the logical artifact through the
    shared safe projection. When no ``event_sink``/``monitor_factory`` is
    supplied this stays a pure no-observability materialization.
    """
    results: list[Path] = []
    for selected in select_build_artifacts(projection):
        observed = event_sink is not None or monitor_factory is not None
        resource = (
            DiagnosticLogicalResource(
                DiagnosticResourceKind.REVIEWED_ARTIFACT, selected.name
            )
            if observed else None
        )
        logical_name: str | None = resource.name if resource is not None else None
        monitor: HostActivityMonitor | None = None
        if observed:
            assert logical_name is not None
            if monitor_factory is not None:
                monitor = monitor_factory(logical_name)
            else:
                monitor = HostActivityMonitor(
                    phase=HostPhase.RELEASE_ACQUISITION,
                    step=HostStep.ARTIFACT_ACQUISITION,
                    expects_diagnostic_stream=False,
                    sink=event_sink,
                    logical_resource=logical_name,
                )

        def on_cache_hit(name: str | None = logical_name) -> None:
            emit(
                event_sink,
                HostStepEvent(
                    HostPhase.RELEASE_ACQUISITION, HostStep.CACHE_REUSE,
                    HostStepState.SUCCEEDED, False, name,
                ),
            )

        try:
            results.append(materialize_artifact(
                selected, constructor_project_root=constructor_project_root,
                transport=transport, cache_root=cache_root,
                project_state=project_state, lock=lock,
                progress=(
                    monitor.record_transport_progress if monitor is not None else None
                ),
                on_cache_hit=on_cache_hit,
            ))
        except BaseException as exc:
            failure_resource = resource or DiagnosticLogicalResource(
                DiagnosticResourceKind.REVIEWED_ARTIFACT, selected.name
            )
            attach_host_failure(
                exc,
                phase=HostPhase.RELEASE_ACQUISITION,
                step=HostStep.ARTIFACT_ACQUISITION,
                logical_resource=failure_resource.name,
            )
            if observed:
                emit(
                    event_sink,
                    project_host_acquisition_failure(
                        phase=HostPhase.RELEASE_ACQUISITION,
                        step=HostStep.ARTIFACT_ACQUISITION,
                        logical_resource=failure_resource,
                        reason=exc,
                        url=selected.url,
                        secrets=failure_secrets,
                    ),
                )
                if monitor is not None:
                    monitor.finish(HostStepState.FAILED)
            raise
        if monitor is not None:
            monitor.finish(HostStepState.SUCCEEDED)
    return tuple(results)

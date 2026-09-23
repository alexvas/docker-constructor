"""Host-side operational activity for the locked npm assembly boundary.

Phase 8 of ``improve-host-build-observability``.  :class:`HostAssemblyActivity`
implements the neutral ``npm_environment`` step-scope protocol with the Phase 1
operational event protocol and the Phase 3 activity monitor:

* every locked-assembly boundary enters one closed
  :class:`~docker.versioning.host_progress.HostStep` scope;
* the ``npm_execution`` scope declares an expected diagnostic stream, carries
  the fixed ``npm-assembler-<digest-prefix>`` container as its safe logical
  resource, and owns the existing fixed total assembly deadline, so its
  heartbeat exposes diagnostic silence, latest ``diagnostic`` activity age,
  and remaining time;
* every received stdout/stderr chunk is observed through
  :meth:`record_diagnostic` before any mailbox admission, so diagnostic
  silence resets and the latest activity becomes ``diagnostic`` without
  changing the heartbeat cadence;
* a verified cache hit is reported as a closed ``cache_reuse`` fact with no
  container or npm activity.

The activity object never probes Docker, processes, or the filesystem, never
claims that npm is currently downloading, and owns no renderer, coalescer, or
presentation timer.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Callable, Iterator

from docker.npm_environment.assembler import ASSEMBLY_TOTAL_TIMEOUT_SECONDS
from docker.versioning.activity_monitor import HeartbeatWaiter, HostActivityMonitor
from docker.versioning.host_progress import (
    HostEventSink,
    HostPhase,
    HostStep,
    HostStepEvent,
    HostStepState,
    attach_host_failure,
    emit,
)


class HostAssemblyActivity:
    """Instrument one locked-assembly execution with operational events."""

    def __init__(
        self,
        event_sink: HostEventSink | None,
        *,
        phase: HostPhase = HostPhase.LOCKED_ASSEMBLY,
        clock: Callable[[], float] = time.monotonic,
        waiter_factory: Callable[[], HeartbeatWaiter] | None = None,
        deadline_seconds: float = float(ASSEMBLY_TOTAL_TIMEOUT_SECONDS),
    ) -> None:
        if not isinstance(phase, HostPhase):
            raise TypeError("phase must be a HostPhase member")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if waiter_factory is not None and not callable(waiter_factory):
            raise TypeError("waiter_factory must be callable or None")
        self._sink = event_sink
        self._phase = phase
        self._clock = clock
        self._waiter_factory = waiter_factory
        self._deadline_seconds = float(deadline_seconds)
        self._npm_monitor: HostActivityMonitor | None = None
        self._container_name: str | None = None

    @property
    def npm_monitor(self) -> HostActivityMonitor | None:
        """The live ``npm_execution`` monitor while that step is active."""
        return self._npm_monitor

    @property
    def current_container_name(self) -> str | None:
        """The safe container name of the active assembly run, if any."""
        return self._container_name

    def record_diagnostic(self) -> None:
        """Observe one received stdout/stderr chunk.

        Called for every raw chunk before projection, line assembly, or
        mailbox admission.  Safe to call outside the ``npm_execution`` scope.
        """
        monitor = self._npm_monitor
        if monitor is not None:
            monitor.record_diagnostic()

    def cache_reuse(self) -> None:
        """Report a verified cache hit without any container or npm activity."""
        emit(
            self._sink,
            HostStepEvent(
                self._phase,
                HostStep.CACHE_REUSE,
                HostStepState.SUCCEEDED,
                False,
            ),
        )

    @contextmanager
    def step(
        self, name: str, *, container_name: str | None = None
    ) -> Iterator[None]:
        """Scope one closed locked-assembly step with start/terminal facts."""
        step = HostStep(name)
        expects_diagnostic_stream = step is HostStep.NPM_EXECUTION
        if container_name is not None:
            self._container_name = container_name
        monitor = (
            HostActivityMonitor(
                phase=self._phase,
                step=step,
                expects_diagnostic_stream=expects_diagnostic_stream,
                sink=self._sink,
                clock=self._clock,
                waiter=(
                    self._waiter_factory()
                    if self._waiter_factory is not None
                    else None
                ),
                deadline_seconds=(
                    self._deadline_seconds if expects_diagnostic_stream else None
                ),
                logical_resource=container_name,
            )
            if self._sink is not None
            else None
        )
        if expects_diagnostic_stream and monitor is not None:
            self._npm_monitor = monitor
        try:
            yield
        except BaseException as exc:
            # Preserve the active phase/step/resource structurally while the
            # failure propagates (including through later wrapping).
            attach_host_failure(
                exc, phase=self._phase, step=step, logical_resource=container_name
            )
            if monitor is not None:
                monitor.finish(HostStepState.FAILED)
            raise
        else:
            if monitor is not None:
                monitor.finish(HostStepState.SUCCEEDED)
        finally:
            if expects_diagnostic_stream:
                self._npm_monitor = None
            if container_name is not None:
                self._container_name = None


__all__ = ["HostAssemblyActivity"]

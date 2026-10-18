"""Captured-client compatibility projection over docker.process ownership.

This adapter decodes bounded values and retains the established domain outcome
shape. It owns no raw child/pipes, independent readers, supervisor or reap loop.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Sequence

from docker.process import (
    CleanupIncomplete,
    FailureInfo,
    FailureKind,
    FailurePhase,
    OwnedProcess,
)

_CAPTURE_TAIL_BYTES = 64 * 1024


@dataclass(frozen=True)
class LifecyclePolicy:
    grace_seconds: float
    process_label: str = "client"
    captured_output_bytes: int = _CAPTURE_TAIL_BYTES


@dataclass
class LifecycleOutcome:
    return_code: int | None = None
    errors: list[BaseException] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""


def _label_final_reap(failure: FailureInfo, policy: LifecyclePolicy) -> None:
    if (
        failure.kind is FailureKind.ORDINARY
        and failure.phase is FailurePhase.WAIT
        and type(failure.error) is subprocess.TimeoutExpired
    ):
        # Initial terminate-wait expiry is a normal transition. Poll/wait-only
        # observation expiry is not an ordinary WAIT failure. This declared
        # combination identifies final reap in the supported local plan.
        # Preserve the exact error/kind and existing domain client detail.
        failure.error.cmd = (
            f"{policy.process_label} did not exit after SIGKILL "
            f"within {policy.grace_seconds:g}s"
        )


@dataclass(frozen=True)
class _CapturedOperation:
    """Bounded private kind handoff, without new public domain fields."""

    owner: OwnedProcess | None
    outcome: LifecycleOutcome
    failures: tuple[FailureInfo, ...]


def _run_captured(
    argv: Sequence[str], *, policy: LifecyclePolicy
) -> _CapturedOperation:
    owner: OwnedProcess | None = None
    outcome = LifecycleOutcome()
    try:
        owner = OwnedProcess.launch(
            tuple(argv),
            factory=subprocess.Popen,
            deadline_seconds=policy.grace_seconds,
            grace_seconds=2 * policy.grace_seconds,
            capture_limit=policy.captured_output_bytes,
        )
        result = owner.capture()
    except Exception as exc:
        # Only early validation/preparation can fail without a value result.
        # Known raised boundary policy, never inference for returned helpers.
        handle = getattr(exc, "cleanup", None)
        if isinstance(handle, OwnedProcess):
            owner = handle
            failures = handle.cleanup_status.failures
        else:
            kind = (
                FailureKind.ORDINARY
                if isinstance(exc, OSError)
                else FailureKind.UNEXPECTED
            )
            failures = (FailureInfo(exc, kind, FailurePhase.SETUP),)
        outcome.errors.extend(failure.error for failure in failures)
        return _CapturedOperation(owner, outcome, failures)
    except BaseException as exc:
        if owner is not None:
            try:
                object.__setattr__(exc, "cleanup", owner)
            except BaseException:
                pass  # Optional observation must not replace first control flow.
        raise
    outcome.return_code = result.returncode
    outcome.stdout = result.stdout.decode(errors="replace")
    outcome.stderr = result.stderr.decode(errors="replace")
    failures = result.lifecycle.failures
    for failure in failures:
        _label_final_reap(failure, policy)
    outcome.errors.extend(failure.error for failure in failures)
    if not result.lifecycle.complete and not any(
        isinstance(error, CleanupIncomplete) for error in outcome.errors
    ):
        incomplete = CleanupIncomplete(owner)
        outcome.errors.append(incomplete)
        failures += (
            FailureInfo(incomplete, FailureKind.ORDINARY, FailurePhase.COORDINATION),
        )
    return _CapturedOperation(owner, outcome, failures)


def run_captured(
    argv: Sequence[str],
    *,
    policy: LifecyclePolicy,
) -> tuple[OwnedProcess | None, LifecycleOutcome]:
    """Collect once, returning the original owner and unchanged domain fields.

    Ordinary/unexpected failures are projected from returned values. Actual
    control flow propagates unchanged, without an exception output snapshot.
    Later cleanup observation never mutates the delivered domain outcome.
    """
    operation = _run_captured(argv, policy=policy)
    return operation.owner, operation.outcome

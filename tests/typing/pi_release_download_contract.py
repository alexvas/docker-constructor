"""Static typing contract for ``acquire_install_assets`` download callbacks.

This module is type-checked by ``scripts/check-types``; it is never imported at
runtime and its name does not match the ``unittest`` discovery pattern.

Positive cases use :func:`typing.assert_type`.  Negative cases carry a blanket
``ty: ignore`` directive: if the call ever becomes accepted, ``ty``'s
``unused-ignore-comment`` warning fails the check, so the directive itself is
the assertion that the invalid combination stays rejected.
"""
from __future__ import annotations

import contextlib
from typing import assert_type

from docker.versioning.pi_release import (
    AssetActivityFactory,
    PiReleaseUrls,
    ProgressCallback,
    acquire_install_assets,
)

#: A callback that cannot accept a second argument (the legacy shape).
def _legacy(url: str) -> bytes:
    return b""


#: The production adapter shape: an optional progress observer.
def _progress_aware(url: str, progress: ProgressCallback | None = None) -> bytes:
    return b""


#: A callback that *requires* the progress observer.  Valid with a definite
#: activity scope (which always passes it); invalid without one or with an
#: optional activity value (which may skip the observer call).
def _mandatory_progress(url: str, progress: ProgressCallback) -> bytes:
    return b""


#: An activity observer factory.
def _activity(name: str) -> contextlib.AbstractContextManager[ProgressCallback | None]:
    raise NotImplementedError


def positive_cases(urls: PiReleaseUrls) -> None:
    """The callback shapes the public overloads accept."""
    # Legacy one-argument callback with no observation.
    assert_type(acquire_install_assets(urls, _legacy), tuple[bytes, bytes])
    assert_type(acquire_install_assets(urls, _legacy, activity=None), tuple[bytes, bytes])
    # Optional-progress callback with no observation (one-argument call).
    assert_type(acquire_install_assets(urls, _progress_aware), tuple[bytes, bytes])
    assert_type(
        acquire_install_assets(urls, _progress_aware, activity=None),
        tuple[bytes, bytes],
    )
    # Optional-progress callback inside a definite observation scope.
    assert_type(
        acquire_install_assets(urls, _progress_aware, activity=_activity),
        tuple[bytes, bytes],
    )
    # A callback with a *mandatory* progress argument is valid when the
    # activity scope is definitely present, because that branch always calls
    # ``download(url, progress)``.
    assert_type(
        acquire_install_assets(urls, _mandatory_progress, activity=_activity),
        tuple[bytes, bytes],
    )
    # Optional-progress callback with an optional observation value: the
    # callback safely supports either runtime branch.
    factory: AssetActivityFactory | None = _activity
    assert_type(
        acquire_install_assets(urls, _progress_aware, activity=factory),
        tuple[bytes, bytes],
    )


def negative_cases(urls: PiReleaseUrls) -> None:
    """Combinations the public overloads must keep rejecting."""
    # A one-argument callback cannot serve an observation scope (the branch
    # always calls ``download(url, progress)``).
    acquire_install_assets(urls, _legacy, activity=_activity)  # ty: ignore
    # A callback that requires a second argument cannot serve the
    # no-activity branch (runtime would call ``download(url)``).
    acquire_install_assets(urls, _mandatory_progress, activity=None)  # ty: ignore
    # ... nor an optional observation value, which may be None at runtime.
    factory: AssetActivityFactory | None = _activity
    acquire_install_assets(urls, _mandatory_progress, activity=factory)  # ty: ignore

"""Private lifecycle seam for the local-file readers in the CLI and image.

The caller retains opening, parsing and error-mapping policy. Prepare the sole
release authority before invoking its opener; never enter the native stream
context manager or independently own the underlying descriptor.
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import Protocol

from docker.filesystem.streams import OwnedStream


class _Closeable(Protocol):
    def close(self) -> None: ...


class _FileRelease[T: _Closeable]:
    __slots__ = ("stream",)

    def __init__(self) -> None:
        self.stream: T | None = None

    def close(self) -> None:
        if self.stream is not None:
            self.stream.close()


@contextmanager
def owned_file[T: _Closeable](opener: Callable[[], T], *, label: str) -> Generator[T]:
    release: _FileRelease[T] = _FileRelease()
    owner = OwnedStream(release, label=label)
    with owner:
        # Binding performs no inspection, validation or replacement allocation.
        release.stream = stream = opener()
        yield stream

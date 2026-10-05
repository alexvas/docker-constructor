"""Generic descriptor-relative operations protocol and POSIX adapter.

``DescriptorOps`` is the injectable seam the descriptor foundation depends on.
``PosixDescriptorOps`` is the production adapter: every method delegates to the
corresponding descriptor-relative ``os`` call and preserves the raised
``OSError`` subclass, ``errno``, and chaining inputs unchanged.  This module is
domain-neutral and imports only the standard library, so it can be reused
without loading ``docker.transactions`` or a domain package.
"""
from __future__ import annotations

import os
from typing import Protocol


class DescriptorOps(Protocol):
    """Descriptor-relative operations required by the descriptor foundation."""

    def openat(
        self,
        directory_fd: int | None,
        name: str,
        flags: int,
        mode: int = 0,
    ) -> int: ...

    def close(self, fd: int) -> None: ...

    def fstat(self, fd: int) -> os.stat_result: ...

    def fchmod(self, fd: int, mode: int) -> None: ...

    def mkdirat(self, directory_fd: int, name: str, mode: int) -> None: ...

    def statat(
        self,
        directory_fd: int,
        name: str,
        *,
        follow_symlinks: bool,
    ) -> os.stat_result: ...

    def listdir(self, fd: int) -> list[str]: ...

    def unlinkat(self, directory_fd: int, name: str) -> None: ...

    def rmdirat(self, directory_fd: int, name: str) -> None: ...


class PosixDescriptorOps:
    """Production ``DescriptorOps`` adapter over descriptor-relative ``os``."""

    __slots__ = ()

    def openat(
        self,
        directory_fd: int | None,
        name: str,
        flags: int,
        mode: int = 0,
    ) -> int:
        return os.open(name, flags, mode, dir_fd=directory_fd)

    def close(self, fd: int) -> None:
        os.close(fd)

    def fstat(self, fd: int) -> os.stat_result:
        return os.fstat(fd)

    def fchmod(self, fd: int, mode: int) -> None:
        os.fchmod(fd, mode)

    def mkdirat(self, directory_fd: int, name: str, mode: int) -> None:
        os.mkdir(name, mode, dir_fd=directory_fd)

    def statat(
        self,
        directory_fd: int,
        name: str,
        *,
        follow_symlinks: bool,
    ) -> os.stat_result:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=follow_symlinks)

    def listdir(self, fd: int) -> list[str]:
        return os.listdir(fd)

    def unlinkat(self, directory_fd: int, name: str) -> None:
        os.unlink(name, dir_fd=directory_fd)

    def rmdirat(self, directory_fd: int, name: str) -> None:
        os.rmdir(name, dir_fd=directory_fd)

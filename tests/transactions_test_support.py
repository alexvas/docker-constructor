"""Shared helpers for the durable-filesystem-transactions Phase 1 tests.

The real L0 backend is :class:`PosixFileOps`.  :class:`InjectedOps` delegates
to it while recording call order and injecting deterministic faults or
partial writes, so the L1/L2 contracts can be exercised across every
open/write/fsync/link/rename/unlink/chmod/close boundary.
"""
from __future__ import annotations

import os

from docker.transactions.posix import PosixFileOps


class InjectedOps(PosixFileOps):
    """A delegating L0 backend with deterministic fault injection."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.counts: dict[str, int] = {}
        # method -> exception instance or callable(count) -> exception | None
        self.failures: dict[str, object] = {}
        # method -> callable(*args) invoked immediately before delegation
        self.hooks: dict[str, object] = {}
        self.partial_writes: int | None = None
        # callable(real_stat_result) -> stat_result, applied by fstat
        self.fstat_override = None

    def _record(self, name: str, *args: object) -> None:
        self.calls.append((name, args))
        self.counts[name] = self.counts.get(name, 0) + 1
        hook = self.hooks.get(name)
        if hook is not None:
            hook(*args)
        spec = self.failures.get(name)
        if spec is not None:
            exc = spec(self.counts[name]) if callable(spec) else spec
            if exc is not None:
                raise exc

    # -- recorded, injectable L0 surface --------------------------------
    def openat(self, dir_fd, name, flags, mode=0o777):
        self._record("openat", dir_fd, name, flags, mode)
        return super().openat(dir_fd, name, flags, mode)

    def read(self, fd, size):
        self._record("read", fd, size)
        return super().read(fd, size)

    def write(self, fd, data):
        self._record("write", fd, data)
        if self.partial_writes is not None:
            chunk = bytes(data[: self.partial_writes])
            return super().write(fd, chunk)
        return super().write(fd, data)

    def fstat(self, fd):
        self._record("fstat", fd)
        info = super().fstat(fd)
        if self.fstat_override is not None:
            return self.fstat_override(info)
        return info

    def fsync(self, fd):
        self._record("fsync", fd)
        return super().fsync(fd)

    def linkat(self, src_dir_fd, src, dst_dir_fd, dst, *, follow_symlinks=False):
        self._record("linkat", src_dir_fd, src, dst_dir_fd, dst)
        return super().linkat(src_dir_fd, src, dst_dir_fd, dst, follow_symlinks=follow_symlinks)

    def renameat(self, old_dir_fd, old, new_dir_fd, new):
        self._record("renameat", old_dir_fd, old, new_dir_fd, new)
        return super().renameat(old_dir_fd, old, new_dir_fd, new)

    def unlinkat(self, dir_fd, name):
        self._record("unlinkat", dir_fd, name)
        return super().unlinkat(dir_fd, name)

    def chmod(self, dir_fd, name, mode, *, follow_symlinks=False):
        self._record("chmod", dir_fd, name, mode)
        return super().chmod(dir_fd, name, mode, follow_symlinks=follow_symlinks)

    def fchmod(self, fd, mode):
        self._record("fchmod", fd, mode)
        return super().fchmod(fd, mode)

    def close(self, fd):
        self._record("close", fd)
        return super().close(fd)

    # -- inspection helpers ---------------------------------------------
    def reset(self) -> None:
        self.calls.clear()
        self.counts.clear()

    @property
    def order(self) -> list[str]:
        return [name for name, _ in self.calls]

    def arg_pairs(self, method: str) -> list[tuple[object, ...]]:
        return [args for name, args in self.calls if name == method]

    def names(self, method: str) -> list[object]:
        return [args[1] for args in self.arg_pairs(method)]


def temporary_entries(directory: str) -> list[str]:
    """Every still-owned temporary publication entry under *directory*."""
    return sorted(name for name in os.listdir(directory) if name.startswith(".transaction-"))

"""L0 descriptor-relative POSIX backend.

``PosixFileOps`` is an internal fault-injection surface, not a project-wide
virtual filesystem or a domain-facing god object.  Every method delegates
directly to the corresponding descriptor-relative POSIX call and preserves
the raised ``OSError`` subclass, ``errno``, and chaining inputs unchanged.
"""
from __future__ import annotations

import errno
import os


class PosixFileOps:
    """Descriptor-relative POSIX operations preserving raw failure behavior."""

    __slots__ = ()

    def openat(self, dir_fd, name, flags, mode=0o777):
        return os.open(name, flags, mode, dir_fd=dir_fd)

    def read(self, fd, size):
        return os.read(fd, size)

    def write(self, fd, data):
        return os.write(fd, data)

    def fstat(self, fd):
        return os.fstat(fd)

    def fsync(self, fd):
        os.fsync(fd)

    def linkat(self, src_dir_fd, src, dst_dir_fd, dst, *, follow_symlinks=False):
        os.link(
            src, dst,
            src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    def renameat(self, old_dir_fd, old, new_dir_fd, new):
        os.rename(old, new, src_dir_fd=old_dir_fd, dst_dir_fd=new_dir_fd)

    def unlinkat(self, dir_fd, name):
        os.unlink(name, dir_fd=dir_fd)

    def chmod(self, dir_fd, name, mode, *, follow_symlinks=False):
        os.chmod(name, mode, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    def fchmod(self, fd, mode):
        os.fchmod(fd, mode)

    def flock(self, fd, operation):
        import fcntl
        fcntl.flock(fd, operation)

    def close(self, fd):
        os.close(fd)

    def write_all(self, fd, data) -> None:
        """Write every byte of *data*, tolerating kernel short writes."""
        view = memoryview(data)
        while view:
            written = self.write(fd, view)
            if written <= 0:
                raise OSError(errno.EIO, "short write while publishing private state")
            view = view[written:]

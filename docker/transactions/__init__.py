"""Shared durable-filesystem-transactions substrate (L0-L2).

L0 is the internal descriptor-relative POSIX backend (:class:`PosixFileOps`).
L1 adds validated directory and regular-file capabilities retaining live
descriptors.  L2 exposes distinct complete regular-file contracts.  The
canonical JSON codec grants deterministic bytes and generic decoding only.
"""
from __future__ import annotations

from .capabilities import DirectoryCapability, FileCapability
from .codec import decode, encode
from .errors import (
    CapabilityError,
    DestinationExists,
    LockContention,
    LockError,
    TransactionError,
    UnsafeFileError,
)
from .locking import LockCapability, LockPolicy
from .posix import PosixFileOps
from .regular import RegularFileContracts

__all__ = [
    "CapabilityError",
    "DestinationExists",
    "DirectoryCapability",
    "FileCapability",
    "LockCapability",
    "LockContention",
    "LockError",
    "LockPolicy",
    "PosixFileOps",
    "RegularFileContracts",
    "TransactionError",
    "UnsafeFileError",
    "decode",
    "encode",
]

"""Compatibility import for the domain-neutral cleanup accumulator.

The implementation now lives in :mod:`docker.filesystem.cleanup` so the
lightweight foundation can be reused without loading the aggregate durable
transaction substrate.  This module re-exports the shared class unchanged;
callers that imported ``CleanupFailures`` from here keep working.
"""
from __future__ import annotations

from docker.filesystem.cleanup import CleanupFailures

__all__ = ["CleanupFailures"]

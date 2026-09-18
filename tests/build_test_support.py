"""Hermetic inventory fixtures for tests that do not exercise local companions."""
from __future__ import annotations

import atexit
import hashlib
import shutil
import tempfile
import uuid
from pathlib import Path

from docker.versioning.build_cache import publish_verified_blob
from docker.versioning.build_materialization import SelectedBuildArtifact
from docker.versioning.digest_identity import DigestIdentity

from tests.pi_fixtures import (
    fake_pi_materialization,
    no_network_transport_factory,
)

__all__ = [
    "DIGEST_VALID_ARTIFACT_BYTES",
    "DOCKERFILE_PATH",
    "INVENTORY_PATH",
    "digest_valid_selected_artifacts",
    "publish_digest_valid_artifacts",
    "fake_pi_materialization",
    "fixture_directory",
    "no_network_transport_factory",
]


_TEMPORARY_DIRECTORY = tempfile.TemporaryDirectory()
_FIXTURE_DIRECTORY = tempfile.TemporaryDirectory()
atexit.register(_FIXTURE_DIRECTORY.cleanup)
atexit.register(_TEMPORARY_DIRECTORY.cleanup)


def fixture_directory(prefix: str) -> Path:
    """Create a unique test fixture directory beneath one managed root."""
    path = Path(_FIXTURE_DIRECTORY.name) / f"{prefix}{uuid.uuid4().hex}"
    path.mkdir()
    return path


DIGEST_VALID_ARTIFACT_BYTES = {
    "rustup": b"test-rustup-artifact", "uv": b"test-uv-artifact",
    "rtk": b"test-rtk-artifact", "fd": b"test-fd-artifact",
}


def digest_valid_selected_artifacts(_projection=None):
    """Return test-only artifact identities derived from local fixture bytes."""
    return tuple(
        SelectedBuildArtifact(
            name=name, url=f"https://test.invalid/{name}",
            identity=DigestIdentity.from_hex(
                "sha256", hashlib.sha256(data).hexdigest(),
            ),
        )
        for name, data in DIGEST_VALID_ARTIFACT_BYTES.items()
    )


def publish_digest_valid_artifacts(
    projection=None, *, constructor_project_root, cache_root=None, project_state=None, **_kwargs,
):
    """Publish real digest-verified blobs for orchestration fixture builds."""
    return tuple(
        publish_verified_blob(
            selected.identity, DIGEST_VALID_ARTIFACT_BYTES[selected.name],
            constructor_project_root=constructor_project_root, cache_root=cache_root,
            project_state=project_state,
        )
        for selected in digest_valid_selected_artifacts(projection)
    )


INVENTORY_PATH = Path(_TEMPORARY_DIRECTORY.name) / "docker-constructor.toml"
shutil.copyfile(Path(__file__).resolve().parents[1] / "docker-constructor.toml", INVENTORY_PATH)

# The selected project's Dockerfile is a required build input; builds invoked
# without the facade still need a readable Dockerfile so build-context
# confinement can generate its transaction-owned copy.
DOCKERFILE_PATH = INVENTORY_PATH.parent / "Dockerfile"
DOCKERFILE_PATH.write_bytes(b"FROM scratch\n")

"""Deterministic, hermetic Pi release fixtures shared across tests.

Every release asset — ``SHA256SUMS``, ``pi-coding-agent-install-package.json``,
``pi-coding-agent-install-package-lock.json`` — plus the assembled tree and the
consumer launcher/evidence are produced locally.  Nothing in this module
performs network I/O.

Phase 6 unit tests and the acceptance suites import from here instead of
re-deriving the reviewed release bytes, so there is exactly one authoritative
fixture construction path.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

from docker.versioning.pi_release import (
    INSTALL_PACKAGE_FILENAME,
    INSTALL_PACKAGE_LOCK_FILENAME,
    PiReleaseUrls,
)

PI_PACKAGE = "@earendil-works/pi-coding-agent"
PI_VERSION = "0.84.4"
PI_BIN_TARGET = "dist/bundle/cli.js"
PI_LOCK_PATH = f"node_modules/{PI_PACKAGE}"

_DATA_DIR = Path(__file__).resolve().parent / "data"
_TMP = tempfile.TemporaryDirectory()
atexit.register(_TMP.cleanup)


def fixture_directory(prefix: str) -> Path:
    """Create a unique fixture directory beneath one managed temp root."""
    path = Path(_TMP.name) / f"{prefix}{uuid.uuid4().hex}"
    path.mkdir()
    return path


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pi_install_package_bytes() -> bytes:
    """Deterministic synthetic install-package manifest matching the lock."""
    return (
        json.dumps(
            {
                "name": "@earendil-works/pi-coding-agent-install",
                "version": PI_VERSION,
                "dependencies": {PI_PACKAGE: PI_VERSION},
            },
            sort_keys=True,
        )
        + "\n"
    ).encode()


def pi_install_lock_bytes() -> bytes:
    """The reviewed official install lockfile checked into ``tests/data``."""
    return (_DATA_DIR / "pi_install_lock_0.84.4.json").read_bytes()


def pi_sha256sums_bytes(
    *,
    package: bytes | None = None,
    lock: bytes | None = None,
) -> bytes:
    """``sha256sum``-format ``SHA256SUMS`` over the two install assets."""
    package = package if package is not None else pi_install_package_bytes()
    lock = lock if lock is not None else pi_install_lock_bytes()
    return (
        f"{_sha256(package)}  {INSTALL_PACKAGE_FILENAME}\n"
        f"{_sha256(lock)}  {INSTALL_PACKAGE_LOCK_FILENAME}\n"
    ).encode()


class FakePiReleaseTransport:
    """Serves exactly the three release assets; any other URL fails loudly.

    Records the download order so callers can assert that acquisition precedes
    Docker-backed assembly.
    """

    def __init__(
        self,
        urls: PiReleaseUrls,
        *,
        package: bytes | None = None,
        lock: bytes | None = None,
    ) -> None:
        self.urls = urls
        self.package = package if package is not None else pi_install_package_bytes()
        self.lock = lock if lock is not None else pi_install_lock_bytes()
        self.sha256sums = pi_sha256sums_bytes(package=self.package, lock=self.lock)
        self.downloads: list[str] = []

    def stream(self, url: str):
        self.downloads.append(url)
        if url == self.urls.sha256sums:
            yield self.sha256sums
        elif url == self.urls.install_package:
            yield self.package
        elif url == self.urls.install_package_lock:
            yield self.lock
        else:
            raise AssertionError(f"unexpected Pi release URL requested: {url}")


class NoNetworkTransport:
    """Transport that fails immediately on any outbound request.

    Used as the ``_transport_factory`` of acceptance tests so a future change
    that drops the injected Pi materializer fails fast instead of reaching the
    live GitHub release endpoint.
    """

    def __init__(self, policy: object = None) -> None:
        pass

    def stream(self, url: str):
        raise AssertionError(f"unexpected outbound network request: {url}")


def no_network_transport_factory(policy: object = None) -> NoNetworkTransport:
    """Return a transport whose ``stream`` raises on any URL."""
    return NoNetworkTransport(policy)


def assembled_pi_tree() -> Path:
    """Build a minimal assembled Pi tree whose ``bin.pi`` target exists."""
    env_root = fixture_directory("pi-env")
    target = env_root / PI_LOCK_PATH / PI_BIN_TARGET
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("#!/usr/bin/env node\nconsole.log('pi')\n")
    return env_root


def build_pi_attestation(
    *,
    environment_root: Path | None = None,
) -> SimpleNamespace:
    """Build one self-consistent derived-environment attestation bundle.

    Derives real assembler evidence, the assembled output identity, a real
    consumer launcher, and launcher evidence from the reviewed lockfile and
    install-package fixtures for the given (or a freshly assembled) tree.  The
    returned bundle carries every value the snapshot admission re-validates.
    No Docker daemon or network is used.
    """
    from docker.npm_environment import (
        AssemblerEvidence,
        AssemblerEvidenceBody,
        RootSpec,
        assembler_script_digest,
        build_tree_manifest,
        compute_assembled_output_identity,
        compute_assembler_identity,
        compute_assembler_input_identity,
        evidence_body_digest,
        npm_policy_digest,
        npm_policy_flags,
        preflight,
        serialize_evidence,
    )
    from docker.versioning.pi_consumer import (
        launcher_evidence,
        plan_launcher,
        select_pi_metadata,
    )

    validated = preflight(
        pi_install_lock_bytes(),
        package_bytes=pi_install_package_bytes(),
        roots=(RootSpec(PI_PACKAGE, PI_VERSION),),
        platform="linux-x64",
        node_version="24.18.0",
        npm_version="11.16.0",
    )
    assembler = compute_assembler_identity(
        image_digest="sha256:" + "0" * 64,
        node_version="24.18.0",
        npm_version="11.16.0",
        script_digest=assembler_script_digest(),
        policy_digest=npm_policy_digest(),
        platform="linux-x64",
    )
    input_identity = compute_assembler_input_identity(validated, assembler)

    root = environment_root if environment_root is not None else assembled_pi_tree()
    manifest = build_tree_manifest(root)
    body = AssemblerEvidenceBody(
        input_identity=input_identity,
        tree_digest=manifest.digest,
        tree_entries=manifest.entries,
        packages=validated.packages,
        omitted_optionals=validated.omitted_optionals,
        integrity_less=validated.integrity_less,
        root_metadata=validated.root_metadata,
        npm_policy_flags=npm_policy_flags(),
    )
    evidence_digest = evidence_body_digest(body)
    output_identity = compute_assembled_output_identity(
        input_identity,
        manifest.digest,
        evidence_digest,
    )
    evidence_bytes = serialize_evidence(
        AssemblerEvidence(
            output_identity=output_identity.digest,
            input_identity=input_identity,
            tree_digest=manifest.digest,
            evidence_digest=evidence_digest,
            body=body,
        )
    )

    metadata = select_pi_metadata(validated, package=PI_PACKAGE)
    launcher_plan = plan_launcher(metadata, environment_root=root)
    launcher_ev = launcher_evidence(launcher_plan)

    return SimpleNamespace(
        environment_root=root,
        tree_digest=manifest.digest,
        output_identity=output_identity.digest,
        assembler_evidence=evidence_bytes,
        assembler_evidence_digest=evidence_digest,
        assembler_evidence_bytes_digest=hashlib.sha256(evidence_bytes).hexdigest(),
        launcher_plan=launcher_plan,
        launcher_evidence=launcher_ev,
        launcher_evidence_digest=launcher_ev.digest,
    )


def fake_pi_materialization(*args, **kwargs) -> object:
    """Injectable Pi materializer returning a real, self-consistent result.

    Builds a tiny assembled tree whose ``bin.pi`` target exists, plus real
    assembler evidence, a real consumer launcher, and the four attestation
    values, so ``execute_build`` can admit the derived environment into a
    snapshot without a Docker daemon or npm network.  The transport and
    executor arguments are intentionally ignored.
    """
    parts = build_pi_attestation()

    evidence_dir = fixture_directory("pi-evidence")
    evidence_path = evidence_dir / "assembler-evidence.json"
    evidence_path.write_bytes(parts.assembler_evidence)

    return SimpleNamespace(
        result=SimpleNamespace(
            environment_root=parts.environment_root,
            evidence_path=evidence_path,
        ),
        launcher_plan=parts.launcher_plan,
        launcher_evidence=parts.launcher_evidence,
        output_identity=parts.output_identity,
        tree_digest=parts.tree_digest,
        assembler_evidence_digest=parts.assembler_evidence_digest,
        assembler_evidence_bytes_digest=parts.assembler_evidence_bytes_digest,
        launcher_evidence_digest=parts.launcher_evidence_digest,
    )


__all__ = [
    "FakePiReleaseTransport",
    "NoNetworkTransport",
    "PI_BIN_TARGET",
    "PI_LOCK_PATH",
    "PI_PACKAGE",
    "PI_VERSION",
    "assembled_pi_tree",
    "build_pi_attestation",
    "fake_pi_materialization",
    "fixture_directory",
    "no_network_transport_factory",
    "pi_install_lock_bytes",
    "pi_install_package_bytes",
    "pi_sha256sums_bytes",
]

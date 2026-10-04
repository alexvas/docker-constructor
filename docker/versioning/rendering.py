"""Deterministic build-environment rendering from effective configuration.

Converts an ``EffectiveConfiguration`` into Compose build arguments and
a generated TOML inventory file.  No Docker, no network, no subprocess.
"""
from __future__ import annotations

import errno
import io
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Iterable, Mapping, Optional, Sequence, TextIO

from docker.transactions.capabilities import DirectoryCapability
from docker.transactions.errors import (
    STAGE_VALIDATE,
    STAGE_VALIDATE_DESTINATION,
    TransactionError,
    UnsafeFileError,
    attach_secondary,
)
from docker.transactions.posix import PosixFileOps
from docker.transactions.regular import RegularFileContracts

from .effective import (
    EffectiveBuildProjection,
    EffectiveConfiguration,
    to_plain_data,
)
from .corporate_network import CLIENT_CA_ENVIRONMENT, SYSTEM_CA_BUNDLE
from .errors import EffectiveConfigError

if TYPE_CHECKING:
    from .artifact_cache import SelectedArtifact, VerifiedCacheBlob
    from .project_state import ProjectState

# ---------------------------------------------------------------------------
# Immutable rendering input models (Stage 6)
# ---------------------------------------------------------------------------

# Fixed container-side destination for the host Pi home mount.
# The container user is always ``dev`` regardless of the host user.
_CONTAINER_PI_HOME = "/home/dev/.pi"

# Fixed read-only container root for runtime artifact mounts.
_RUNTIME_ARTIFACT_ROOT = "/run/pi-cli/runtime-artifacts"

# No-follow directory-open flags shared by the descriptor-relative publisher.
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)

# No-follow, non-blocking validation of an existing destination leaf.  The
# destination is only ever inspected through a retained descriptor; no path is
# reconstructed from it.
_VALIDATE_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


@dataclass(frozen=True)
class ArtifactMount:
    """Post-materialization mount specification for a single
    verified runtime artifact blob.

    The *container_target* MUST be a canonical path beneath
    ``/run/pi-cli/runtime-artifacts``, derived solely from the
    validated integrity digest.  Traversal segments, absolute
    paths outside the canonical root, and non-canonical
    targets are rejected before the renderer emits any mount
    argument."""

    host_path: str
    """Absolute host path to the verified blob in the cache."""

    container_target: str
    """Canonical read-only mount target inside the container
    (e.g. ``"/run/pi-cli/runtime-artifacts/sha512/<digest>"``)."""


def plan_artifact_mounts(
    verified_blobs: "Iterable[VerifiedCacheBlob]",
) -> "tuple[ArtifactMount, ...]":
    """Build one ``ArtifactMount`` per verified materialized blob.

    *host_path* is passed through directly from each blob — the
    planner does **not** reconstruct cache paths.  *container_target*
    is derived from the blob's integrity via ``_derive_artifact_id``,
    always beneath the fixed ``/run/pi-cli/runtime-artifacts`` root.

    Duplicate integrities are collapsed into a single mount.
    Results are sorted by *container_target* for determinism.

    This is the **production mount-planning boundary** — every
    ``ArtifactMount`` consumed by the rendering and launcher
    stages MUST originate here.  No filesystem access; pure
    string computation.
    """
    from .model import _derive_artifact_id

    seen: set[str] = set()
    mounts: list[ArtifactMount] = []
    for blob in verified_blobs:
        if blob.integrity in seen:
            continue
        seen.add(blob.integrity)
        artifact_id = _derive_artifact_id(blob.integrity)
        mounts.append(ArtifactMount(
            host_path=blob.host_path,
            container_target=f"{_RUNTIME_ARTIFACT_ROOT}/{artifact_id}",
        ))
    mounts.sort(key=lambda m: m.container_target)
    return tuple(mounts)


@dataclass(frozen=True)
class CacheControls:
    """Docker build cache controls.

    *enabled* is ``True`` to use the default BuildKit cache; ``False``
    adds ``--no-cache`` to the build command.
    """
    enabled: bool = True


@dataclass(frozen=True)
class NoDerivedEnvironment:
    """Closed attestation for selected prebuilt artifacts only."""


@dataclass(frozen=True)
class DerivedEnvironment:
    assembled_output_identity: str
    canonical_tree_digest: str
    assembler_evidence_digest: str
    assembler_evidence_bytes_digest: str
    consumer_launcher_evidence_digest: str

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) and value for value in (
            self.assembled_output_identity, self.canonical_tree_digest,
            self.assembler_evidence_digest, self.assembler_evidence_bytes_digest,
            self.consumer_launcher_evidence_digest,
        )):
            raise ValueError("derived-environment attestation values must be non-empty")


def is_platform_native_path(path: str, path_module: object = os.path) -> bool:
    """Validate an absolute host path using the selected path semantics.

    Kept separate from ``Prospective`` so its display token can never become a
    candidate filesystem path. ``ntpath`` and ``posixpath`` are accepted by
    tests; production uses the current host's ``os.path``.
    """
    if not isinstance(path, str) or not path or "\x00" in path:
        return False
    isabs = getattr(path_module, "isabs", None)
    normpath = getattr(path_module, "normpath", None)
    if not callable(isabs) or not callable(normpath) or not isabs(path):
        return False
    normalized = normpath(path)
    return normalized not in (".", "")


@dataclass(frozen=True)
class Materialized:
    path: str
    attestation: NoDerivedEnvironment | DerivedEnvironment

    def __post_init__(self) -> None:
        if not is_platform_native_path(self.path):
            raise ValueError("materialized named context requires an absolute platform-native path")
        if not isinstance(self.attestation, (NoDerivedEnvironment, DerivedEnvironment)):
            raise ValueError("materialized named context has an invalid closed attestation")


@dataclass(frozen=True)
class Prospective:
    name: str = "constructor-artifacts"

    @property
    def path(self) -> None:
        return None

    @property
    def attestation(self) -> dict[str, str]:
        return {"state": "prospective"}


NamedBuildContext = Materialized | Prospective


@dataclass(frozen=True)
class BuildRenderInputs:
    """Immutable input for ``docker build`` command rendering.

    Every attribute maps to a concrete Docker CLI argument.  The
    renderer produces a ``tuple[str, ...]`` argument vector from this
    container without invoking Docker, resolving overrides, or reading
    ``docker-constructor.toml``.
    """

    build_context: str
    """Repository root — the Docker build context path."""

    projection: EffectiveBuildProjection
    """Resolved effective build projection from Stage 4."""

    target_stage: str
    """Dockerfile stage name (e.g. ``"runtime"``)."""

    image_tag: str
    """Canonical image tag applied via ``--tag``."""

    platform: str
    """Docker platform string (e.g. ``"linux/amd64"``)."""

    cache: CacheControls = CacheControls()
    """Cache behaviour."""

    pull: bool = False
    """Always pull base images (``--pull``)."""

    progress: str = "auto"
    """BuildKit progress mode: ``"auto"``, ``"plain"``, or ``"tty"``."""

    dockerfile: Optional[str] = None
    """Relative path to the Dockerfile inside *build_context*.

    When ``None`` (the default) Docker uses ``Dockerfile`` in the
    build-context root.  Set to ``"docker/Dockerfile"`` or similar when
    the Dockerfile is not at the repository root.
    """

    dev_uid: int = 1000
    """Host user UID injected as ``DEV_UID`` build argument.

    Controls file ownership inside the built image so that the ``dev``
    container user matches the host user's UID.
    """

    dev_gid: int = 1000
    """Host user GID injected as ``DEV_GID`` build argument."""

    proxy_url: Optional[str] = None
    """Configured credential-free proxy URL, copied verbatim under every
    proxy build-argument name.  ``None`` emits no proxy arguments."""

    proxy_no_proxy: Optional[str] = None
    """Optional bypass list emitted under ``NO_PROXY`` and ``no_proxy`` only
    when explicitly configured.  ``None`` emits no bypass arguments."""

    corporate_trust_enabled: bool = False
    """True when the resolved local companion enables corporate trust.  When
    true the renderer injects client CA-path build arguments pointing at the
    final system-bundle path; when false it injects none."""

    named_context: NamedBuildContext | None = None
    """Required isolated BuildKit input; executable rendering accepts only
    ``Materialized`` contexts and never turns a prospective display token into a path."""


@dataclass(frozen=True)
class RunHostAccess:
    """Immutable host-access rendering inputs for ``docker run``.

    Constructors are intentionally closed:

    * ``RunHostAccess.disabled()`` — no host mapping or environment.
    * ``RunHostAccess(address=..., mode="docker-gateway", proxy_port=None)``
    * ``RunHostAccess(address=..., mode="external-address", proxy_port=None)``

    ``address`` must be a plain IPv4 or IPv6 address; ``host-gateway``
    and empty addresses are rejected.  ``proxy_port``, when set, must
    be an integer in 1–65535.  ``mode`` must match the reviewed policy.
    """

    address: str | None = None
    mode: str | None = None
    proxy_port: int | None = None

    def __post_init__(self) -> None:
        if self.address is None and self.mode is None and self.proxy_port is None:
            return  # disabled — all fields None
        if not self.address or not isinstance(self.address, str):
            raise ValueError("RunHostAccess.address must be a non-empty IP address")
        if self.address == "host-gateway":
            raise ValueError(
                "RunHostAccess.address must be a resolved IP address, "
                "not the Docker host-gateway token"
            )
        if self.mode not in ("docker-gateway", "external-address"):
            raise ValueError(
                f"RunHostAccess.mode must be 'docker-gateway' or "
                f"'external-address', got {self.mode!r}"
            )
        if self.mode == "external-address" and self.address == "host-gateway":
            raise ValueError(
                "external-address mode rejects host-gateway; supply a "
                "resolved IPv4 or IPv6 address"
            )
        if self.proxy_port is not None:
            if not isinstance(self.proxy_port, int) or not (1 <= self.proxy_port <= 65535):
                raise ValueError(
                    f"proxy_port must be an integer 1–65535, got {self.proxy_port!r}"
                )

    @staticmethod
    def disabled() -> "RunHostAccess":
        """Return a disabled sentinel — no host mapping or environment."""
        return RunHostAccess()

    @property
    def is_enabled(self) -> bool:
        """True when host access is enabled (non-None address)."""
        return self.address is not None


@dataclass(frozen=True)
class RunRenderInputs:
    """Immutable input for ``docker run`` command rendering.

    Every attribute maps to a concrete Docker CLI argument or mount.
    The renderer produces a ``tuple[str, ...]`` argument vector from
    this container without invoking Docker, discovering projects,
    probing gateways, or prompting the user.
    """

    image: str
    """Canonical image to run."""

    container_name: str
    """Container name assigned via ``--name``."""

    projection_host_path: str
    """Host path to the private runtime projection file.

    This is the path returned by :meth:`RuntimeProjectionHandle.path`.
    The caller must keep the handle alive while the container runs.
    """

    projection_container_path: str
    """Fixed read-only mount path inside the container
    (e.g. ``"/run/pi-cli/docker-constructor.runtime.toml"``).
    """

    pi_home_host: str
    """Host path to the Pi home directory (e.g. ``"~/.pi"`` or
    ``"/home/alice/.pi"``).  Mounted into the container at the fixed
    destination ``/home/dev/.pi`` — the paths are intentionally
    asymmetric because the container user is always ``dev``.
    """

    workspace: str
    """Host path to the primary workspace.  Bound as ``WORKSPACE_PATH_1`` and
    set as the container working directory.
    """

    extra_workspaces: tuple[str, ...] = ()
    """Extra host workspace paths bound as ``WORKSPACE_PATH_2``,
    ``WORKSPACE_PATH_3``, etc.
    """

    host_access: RunHostAccess = RunHostAccess()
    """Policy-derived host-access inputs.  Default is ``RunHostAccess.disabled()``
    which suppresses ``--add-host`` and every host-access environment variable."""

    tty: bool = True
    """Allocate a pseudo-TTY (``--tty``)."""

    stdin_open: bool = True
    """Keep STDIN open (``--interactive``)."""

    command: tuple[str, ...] = ()
    """Command and arguments to pass through to the container entrypoint.
    An empty tuple means the image's default ``CMD`` is used.
    """

    chown_on_start: Optional[str] = None
    """Value for ``CHOWN_WORK_ON_START`` environment variable inside
    the container.  ``None`` omits the variable; a string like ``"1"``
    or ``"0"`` adds ``--env CHOWN_WORK_ON_START=<value>``.
    """

    artifact_mounts: tuple[ArtifactMount, ...] = ()
    """Verified runtime artifact blobs to mount as individual
    read-only volumes.  An empty tuple means no artifact mounts
    (dry-run or pre-materialization error).  Every mount target
    is validated for canonical form before rendering."""

    validate_artifact_sources: bool = True
    """When ``False`` (dry-run), filesystem checks on
    ``host_path`` are skipped; only target canonical form and
    destination collisions are validated.  Defaults to ``True``
    to enforce regular-file/symlink checks before Docker execution."""

    corporate_trust_bundle: Optional[str] = None
    """Absolute host path to the fixed corporate trust bundle.  When set,
    the renderer adds a read-only bind mount over the container system CA
    bundle and emits the closed client CA environment mapping; ``None``
    (disabled) emits neither."""

    proxy_url: Optional[str] = None
    """Configured credential-free proxy URL, copied verbatim under every
    standard proxy variable.  ``None`` emits no proxy variables."""

    proxy_no_proxy: Optional[str] = None
    """Optional bypass list emitted under ``NO_PROXY`` and ``no_proxy``
    only when explicitly configured (and only when *proxy_url* is set)."""

    project_state_runtime_root: Optional[str] = None
    """Verified external project-state runtime child for containment."""


# ── platform helpers ────────────────────────────────────────────────

# Mapping from internal platform names to Docker platform strings.
_PLATFORM_MAP: Mapping[str, str] = MappingProxyType({
    "linux-amd64": "linux/amd64",
    "linux-arm64": "linux/arm64",
})


def _docker_platform(internal: str) -> str:
    """Translate an internal platform name to a Docker platform string."""
    try:
        return _PLATFORM_MAP[internal]
    except KeyError:
        raise EffectiveConfigError(
            f"unknown platform: {internal!r}"
        )


# ── build-arg emission (ordered, no generic traversal) ──────────────

# Deterministic order matching every Dockerfile ARG declaration.
_BUILD_ARG_ORDER: tuple[tuple[str, str], ...] = (
    ("NODE_BASE_IMAGE", "node.image"),
    ("RUST_VERSION", "rust.version"),
    ("RUST_PROFILE", "rust.profile"),
    ("RUST_COMPONENTS", "rust.components"),
    ("RUSTUP_SHA256", "rust.rustup.sha256"),
    ("UV_VERSION", "uv.version"),
    ("UV_SHA256", "uv.artifact.sha256"),
    ("PYTHON_VERSION", "python_version"),
    ("TY_VERSION", "ty_version"),
    ("RTK_VERSION", "rtk.version"),
    ("RTK_SHA256", "rtk.artifact.sha256"),
    ("FD_VERSION", "fd.version"),
    ("FD_SHA256", "fd.artifact.sha256"),
    ("PI_VERSION", "pi_version"),
    ("OPENSPEC_VERSION", "openspec_version"),
    ("OH_MY_ZSH_VERSION", "oh_my_zsh_revision"),
)


def _resolve_build_arg(proj: EffectiveBuildProjection, path: str) -> str:
    """Resolve a dotted path against the projection to a string value.

    ``tuple[str, ...]`` fields (e.g. ``rust.components``) are joined
    with a single space.
    """
    parts = path.split(".")
    obj: object = proj
    for part in parts:
        obj = getattr(obj, part)
    if isinstance(obj, tuple):
        return " ".join(obj)
    return str(obj)


def _emit_build_args(args: list[str], proj: EffectiveBuildProjection) -> None:
    """Append ``--build-arg NAME=VALUE`` pairs to *args* in
    ``_BUILD_ARG_ORDER``."""
    for arg_name, field_path in _BUILD_ARG_ORDER:
        raw = _resolve_build_arg(proj, field_path)
        args.extend(("--build-arg", f"{arg_name}={raw}"))


# Deterministic proxy build-argument order (task 2.4/2.8).  The endpoint is
# carried through a constructor-specific argument (never a same-named proxy
# ARG, which an inherited base-image ENV would override) and the Dockerfile
# helper exports it into every standard proxy variable, so SOCKS support stays
# best-effort instead of being claimed.
_PROXY_URL_ARG = "PI_CORPORATE_PROXY_URL"
_PROXY_BYPASS_ARG = "PI_CORPORATE_NO_PROXY"

# Fixed container-side system CA bundle.  The Dockerfile receives it via a
# constructor-specific build argument (never a same-named SSL_CERT_FILE or
# NODE_EXTRA_CA_CERTS ARG, which an inherited base-image ENV would override).
_SYSTEM_CA_BUNDLE = SYSTEM_CA_BUNDLE
_CORPORATE_CA_PATH_ARG = "PI_CORPORATE_CA_PATH"
# Explicit signal that gates the Dockerfile trust replacement, so a stale
# bundle in the build context cannot change trust without enabled intent.
_CORPORATE_TRUST_ENABLED_ARG = "CORPORATE_TRUST_ENABLED"

# Standard proxy variable names emitted at runtime (task 3.2/3.6).  The
# endpoint is copied verbatim across every variable, so SOCKS support stays
# best-effort instead of being claimed.
_PROXY_RUN_URL_NAMES: tuple[str, ...] = (
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
)
_PROXY_RUN_BYPASS_NAMES: tuple[str, ...] = ("NO_PROXY", "no_proxy")


def _emit_proxy_build_args(args: list[str], inputs: BuildRenderInputs) -> None:
    """Append configured proxy ``--build-arg`` pairs in deterministic order.

    Emits nothing when no proxy URL is configured; emits the bypass list only
    when ``proxy_no_proxy`` is explicitly set.
    """
    if inputs.proxy_url is None:
        return
    args.extend(("--build-arg", f"{_PROXY_URL_ARG}={inputs.proxy_url}"))
    if inputs.proxy_no_proxy is not None:
        args.extend(("--build-arg", f"{_PROXY_BYPASS_ARG}={inputs.proxy_no_proxy}"))


def _emit_corporate_trust_build_args(
    args: list[str], inputs: BuildRenderInputs,
) -> None:
    """Append corporate-trust build arguments only when trust is enabled.

    Emits an explicit ``CORPORATE_TRUST_ENABLED=true`` signal that gates the
    Dockerfile trust replacement, plus a constructor-specific CA-path argument
    that the Dockerfile conditionally exports into the client variables.  All
    are supplied as build arguments (never persisted as image ``ENV``) and
    disabled builds receive none, so a stale bundle or an inherited client
    variable cannot alter default behavior.
    """
    if not inputs.corporate_trust_enabled:
        return
    args.extend(("--build-arg", f"{_CORPORATE_TRUST_ENABLED_ARG}=true"))
    args.extend(("--build-arg", f"{_CORPORATE_CA_PATH_ARG}={_SYSTEM_CA_BUNDLE}"))


# The four post-materialization derived-environment attestation values passed
# to the Dockerfile so it can verify the copied Pi tree, launcher, and both
# evidence sets against the host-attested bindings.
_PI_ATTESTATION_ARGS: tuple[tuple[str, str], ...] = (
    ("PI_ASSEMBLED_OUTPUT_IDENTITY", "assembled_output_identity"),
    ("PI_TREE_DIGEST", "canonical_tree_digest"),
    ("PI_ASSEMBLER_EVIDENCE_DIGEST", "assembler_evidence_digest"),
    ("PI_ASSEMBLER_EVIDENCE_BYTES_DIGEST", "assembler_evidence_bytes_digest"),
    ("PI_LAUNCHER_EVIDENCE_DIGEST", "consumer_launcher_evidence_digest"),
)


def _emit_pi_attestation_args(args: list[str], inputs: BuildRenderInputs) -> None:
    """Append the four derived-environment attestation build arguments.

    Emitted only for a materialized ``DerivedEnvironment`` context; a
    ``NoDerivedEnvironment`` or prospective context emits none.
    """
    context = inputs.named_context
    if not isinstance(context, Materialized):
        return
    if not isinstance(context.attestation, DerivedEnvironment):
        return
    for arg_name, field in _PI_ATTESTATION_ARGS:
        args.extend(("--build-arg", f"{arg_name}={getattr(context.attestation, field)}"))


# ── validation ──────────────────────────────────────────────────────


def _validate_build_projection(proj: EffectiveBuildProjection) -> None:
    """Reject projection fields that are missing or empty."""
    # Every scalar version/revision field must be non-empty.
    for label, value in (
        ("NODE_BASE_IMAGE", proj.node.image),
        ("NODE_VERSION", proj.node.node_version),
        ("NPM_VERSION", proj.node.npm_version),
        ("RUST_VERSION", proj.rust.version),
        ("RUST_PROFILE", proj.rust.profile),
        ("UV_VERSION", proj.uv.version),
        ("PYTHON_VERSION", proj.python_version),
        ("TY_VERSION", proj.ty_version),
        ("RTK_VERSION", proj.rtk.version),
        ("FD_VERSION", proj.fd.version),
        ("PI_VERSION", proj.pi_version),
        ("PI_PACKAGE", proj.pi_release.package),
        ("PI_RELEASE_REPOSITORY", proj.pi_release.release_repository),
        ("PI_RELEASE_TAG_PREFIX", proj.pi_release.release_tag_prefix),
        ("OPENSPEC_VERSION", proj.openspec_version),
        ("OH_MY_ZSH_VERSION", proj.oh_my_zsh_revision),
    ):
        if not value.strip():
            raise EffectiveConfigError(f"{label} is empty")

    # Rust components must not be empty.
    if not proj.rust.components:
        raise EffectiveConfigError("RUST_COMPONENTS is empty")

    # Every artifact must have a non-empty URL and SHA256.
    for label, art in (
        ("RUSTUP", proj.rust.rustup),
        ("UV", proj.uv.artifact),
        ("RTK", proj.rtk.artifact),
        ("FD", proj.fd.artifact),
    ):
        if not art.url.strip():
            raise EffectiveConfigError(f"{label}_URL is empty")
        if not art.sha256.strip():
            raise EffectiveConfigError(f"{label}_SHA256 is empty")


# ── run-vector helpers ──────────────────────────────────────────────

_PROJECTION_CANONICAL_CONTAINER_PATH: str = (
    "/run/pi-cli/docker-constructor.runtime.toml"
)


def _validate_run_inputs(inputs: RunRenderInputs) -> None:
    """Validate all ``RunRenderInputs`` fields before rendering."""
    # Required string fields must be non-empty.
    for label, value in (
        ("image", inputs.image),
        ("container_name", inputs.container_name),
        ("workspace", inputs.workspace),
    ):
        if not value.strip():
            raise ValueError(f"{label} must not be empty")

    # Extra workspaces must not contain empty entries.
    for i, p in enumerate(inputs.extra_workspaces):
        if not p.strip():
            raise ValueError(f"extra_workspaces[{i}] is empty")

    # Duplicate workspace paths (primary versus extra, or among extras).
    all_projects = [inputs.workspace] + list(inputs.extra_workspaces)
    seen: set[str] = set()
    for p in all_projects:
        if p in seen:
            raise ValueError(f"duplicate workspace path: {p}")
        seen.add(p)

    # Workspace and Pi-home paths must be absolute.
    for label, value in (
        ("workspace", inputs.workspace),
        ("pi_home_host", inputs.pi_home_host),
    ):
        if not value.startswith("/"):
            raise ValueError(f"{label} must be an absolute path, got {value!r}")
    for i, p in enumerate(inputs.extra_workspaces):
        if not p.startswith("/"):
            raise ValueError(
                f"extra_workspaces[{i}] must be an absolute path, got {p!r}"
            )

    # Projection container path must be canonical.
    if inputs.projection_container_path != _PROJECTION_CANONICAL_CONTAINER_PATH:
        raise ValueError(
            f"projection_container_path must be "
            f"{_PROJECTION_CANONICAL_CONTAINER_PATH!r}, "
            f"got {inputs.projection_container_path!r}"
        )

    # Projection host path must be under the verified external runtime child
    # and must not be a forbidden file.
    _validate_projection_host_path(
        inputs.projection_host_path, inputs.project_state_runtime_root,
    )

    # Destination mount collisions: every dst must be unique across
    # Pi home, projection, and all project mounts.
    all_dsts: set[str] = {_CONTAINER_PI_HOME, inputs.projection_container_path}
    for p in all_projects:
        if p in all_dsts:
            raise ValueError(
                f"workspace path {p!r} collides with a fixed mount destination"
            )
        all_dsts.add(p)

    artifact_root = "/run/pi-cli/runtime-artifacts"
    seen_sources: set[str] = set()
    for mount in inputs.artifact_mounts:
        if inputs.validate_artifact_sources:
            # Reject missing, symlink, or non-regular *before* realpath so
            # the defined error message surfaces even for missing blobs.
            if not os.path.isabs(mount.host_path):
                raise ValueError(
                    "artifact mount source must be canonical and absolute"
                )
            if not os.path.isfile(mount.host_path) or os.path.islink(mount.host_path):
                raise ValueError(
                    "artifact mount source must be a regular non-symlink file"
                )
            source: str
            try:
                source = os.path.realpath(mount.host_path)
            except OSError:
                raise ValueError(
                    "artifact mount source must be a regular non-symlink file"
                )
            if source != mount.host_path:
                raise ValueError(
                    "artifact mount source must be canonical and absolute"
                )
        else:
            # Dry-run: no filesystem access — use absolute path directly.
            source = os.path.abspath(mount.host_path)

        target = os.path.normpath(mount.container_target)
        if not target.startswith(artifact_root + "/") or target != mount.container_target:
            raise ValueError("artifact mount target must be canonical beneath fixed root")
        if source in seen_sources or target in all_dsts or source == target:
            raise ValueError("duplicate or aliased artifact mount")
        seen_sources.add(source)
        all_dsts.add(target)

    # Corporate trust bundle must be an absolute host path.
    if (
        inputs.corporate_trust_bundle is not None
        and not inputs.corporate_trust_bundle.startswith("/")
    ):
        raise ValueError(
            "corporate_trust_bundle must be an absolute path, got "
            f"{inputs.corporate_trust_bundle!r}"
        )

    # Proxy inputs are only meaningful together.
    if inputs.proxy_url is not None and not inputs.proxy_url.strip():
        raise ValueError("proxy_url must not be empty")
    if inputs.proxy_no_proxy is not None and inputs.proxy_url is None:
        raise ValueError("proxy_no_proxy requires proxy_url to be configured")

    # Corporate trust mount destination must not collide with any other
    # mount destination (Pi home, projection, projects, or artifacts).
    if (
        inputs.corporate_trust_bundle is not None
        and _SYSTEM_CA_BUNDLE in all_dsts
    ):
        raise ValueError(
            "corporate trust bundle destination collides with another "
            "mount destination"
        )


def plan_dry_run_artifact_mounts(
    selected_artifacts: "Iterable[SelectedArtifact]",
    *,
    cache_root: str,
) -> "tuple[ArtifactMount, ...]":

    """Build artifact mounts for dry-run display from resolution output.

    Derives *host_path* from the explicitly resolved runtime-artifact
    cache root
    (the deterministic cache location) and *container_target* from
    ``_RUNTIME_ARTIFACT_ROOT``.  No filesystem access — the blobs may
    not exist yet.  Duplicate integrities are collapsed; results are
    sorted by *container_target*.
    """
    from .model import _derive_artifact_id

    resolved_root = cache_root

    seen: set[str] = set()
    mounts: list[ArtifactMount] = []
    for art in selected_artifacts:
        if art.integrity in seen:
            continue
        seen.add(art.integrity)
        artifact_id = _derive_artifact_id(art.integrity)
        mounts.append(ArtifactMount(
            host_path=os.path.abspath(
                os.path.join(
                    resolved_root,
                    artifact_id,
                )
            ),
            container_target=f"{_RUNTIME_ARTIFACT_ROOT}/{artifact_id}",
        ))
    mounts.sort(key=lambda m: m.container_target)
    return tuple(mounts)


def _validate_projection_host_path(host_path: str, runtime_root: str | None = None) -> None:
    """Reject projection host paths that are forbidden or outside an
    external project-state ``runtime`` child."""
    import os

    # Must be absolute.
    if not host_path.startswith("/"):
        raise ValueError(
            f"projection_host_path must be absolute, got {host_path!r}"
        )

    normalized = os.path.normpath(host_path)
    parts = normalized.split(os.sep)

    if len(parts) < 3 or parts[-2] != "runtime":
        raise ValueError(
            f"projection_host_path must name a runtime child, got {host_path!r}"
        )
    # Reject a deceptive legacy substring even for structural callers.
    if any(".docker-generated" in part and part != ".docker-generated" for part in parts):
        raise ValueError("projection_host_path contains an unsafe legacy path segment")
    # Only orchestration owns an authoritative namespace descriptor. Direct
    # renderer unit callers remain structural; execution always supplies root.
    if runtime_root is not None:
        root = os.path.normpath(runtime_root)
        if (".docker-generated" in parts or not os.path.isabs(root)
                or os.path.commonpath((root, normalized)) != root):
            raise ValueError("projection_host_path escapes verified external project-state")

    # Reject effective build projection.
    if "build.effective" in parts[-1]:
        raise ValueError(
            f"projection_host_path must not reference effective build "
            f"projection, got {host_path!r}"
        )

    # Reject docker-constructor.toml explicitly (the reviewed source).
    if parts[-1] == "docker-constructor.toml":
        raise ValueError(
            f"projection_host_path must not be docker-constructor.toml, "
            f"got {host_path!r}"
        )


def _emit_run_mount(
    args: list[str],
    type_: str,
    src: str,
    dst: str,
    *,
    readonly: bool = False,
) -> None:
    """Append a ``--mount`` argument to *args*."""
    parts = [f"type={type_}", f"src={src}", f"dst={dst}"]
    if readonly:
        parts.append("readonly")
    args.extend(("--mount", ",".join(parts)))


def _emit_client_ca_run_env(args: list[str]) -> None:
    """Append the closed constructor-owned client CA environment mapping.

    Every name and value is a fixed module constant; no caller-supplied or
    host-derived value is accepted.  The caller must emit this only on the
    enabled corporate-trust path.
    """
    for name, value in CLIENT_CA_ENVIRONMENT:
        args.extend(("--env", f"{name}={value}"))


def _emit_proxy_run_env(args: list[str], inputs: RunRenderInputs) -> None:
    """Append the standard proxy ``--env`` pairs for a configured proxy.

    Emits nothing when no proxy URL is configured; emits the bypass list only
    when ``proxy_no_proxy`` is explicitly set.  Every standard
    uppercase/lowercase HTTP, HTTPS, and ALL variable carries the exact URL,
    and both ``NO_PROXY`` forms carry the explicit bypass list.
    """
    if inputs.proxy_url is None:
        return
    for name in _PROXY_RUN_URL_NAMES:
        args.extend(("--env", f"{name}={inputs.proxy_url}"))
    if inputs.proxy_no_proxy is not None:
        for name in _PROXY_RUN_BYPASS_NAMES:
            args.extend(("--env", f"{name}={inputs.proxy_no_proxy}"))


# ── existing rendering functions ────────────────────────────────────


def render_build_vector(inputs: BuildRenderInputs) -> tuple[str, ...]:
    """Render a deterministic ``docker build`` argument vector.

    Returns a ``tuple[str, ...]`` suitable for ``subprocess.run``.
    The renderer never invokes Docker, reads ``docker-constructor.toml``,
    or resolves overrides.

    Raises:
        EffectiveConfigError: a required projection field is missing
            or empty, or the command platform does not match the
            projection platform.
    """
    # 1.  Validate platform match between command and projection.
    expected_platform = _docker_platform(inputs.projection.platform)
    if inputs.platform != expected_platform:
        raise EffectiveConfigError(
            f"platform mismatch: command expects {inputs.platform!r}, "
            f"projection built for {expected_platform!r}"
        )

    # 2.  Validate required projection fields and image tag.
    _validate_build_projection(inputs.projection)
    if not inputs.image_tag.strip():
        raise ValueError("image_tag must not be empty")
    if not inputs.target_stage.strip():
        raise ValueError("target_stage must not be empty")
    if not inputs.build_context.strip():
        raise ValueError("build_context must not be empty")
    if inputs.progress not in ("auto", "plain", "tty"):
        raise ValueError(
            f"unsupported progress mode {inputs.progress!r}; "
            f"expected 'auto', 'plain', or 'tty'"
        )

    # An executable Docker invocation has no representation for an unresolved
    # plan.  Keep prospective contexts exclusively in dry-run presentation.
    if not isinstance(inputs.named_context, Materialized):
        raise EffectiveConfigError(
            "executable build rendering requires a materialized constructor-artifacts context"
        )

    # 3.  Build the deterministic argument vector.
    args: list[str] = ["docker", "build"]

    # Flags — always in this order.
    args.extend(("--tag", inputs.image_tag))
    args.extend(("--target", inputs.target_stage))
    args.extend(("--platform", inputs.platform))
    args.extend(("--progress", inputs.progress))

    if inputs.dockerfile is not None:
        args.extend(("--file", inputs.dockerfile))

    if not inputs.cache.enabled:
        args.append("--no-cache")

    if inputs.pull:
        args.append("--pull")

    args.extend(("--build-context", f"constructor-artifacts={inputs.named_context.path}"))

    # Build arguments — deterministic order matching the Dockerfile ARGs.
    _emit_build_args(args, inputs.projection)

    # Pi derived-environment attestation — materialized builds only.
    _emit_pi_attestation_args(args, inputs)

    # Optional proxy build arguments (absent when no proxy is configured).
    _emit_proxy_build_args(args, inputs)

    # Corporate trust signal + client CA-path arguments — only when enabled.
    _emit_corporate_trust_build_args(args, inputs)

    # DEV_UID / DEV_GID — host-user identity, not from the projection.
    if inputs.dev_uid < 0:
        raise ValueError(f"dev_uid must be >= 0, got {inputs.dev_uid}")
    if inputs.dev_gid < 0:
        raise ValueError(f"dev_gid must be >= 0, got {inputs.dev_gid}")
    args.extend(("--build-arg", f"DEV_UID={inputs.dev_uid}"))
    args.extend(("--build-arg", f"DEV_GID={inputs.dev_gid}"))

    # Build context is always the final positional argument.
    args.append(inputs.build_context)

    return tuple(args)


def render_run_vector(inputs: RunRenderInputs) -> tuple[str, ...]:
    """Render a deterministic ``docker run`` argument vector.

    Returns a ``tuple[str, ...]`` suitable for ``subprocess.run``.
    The renderer never invokes Docker, discovers projects, probes
    gateways, or prompts the user.
    """
    # 1.  Validate all inputs before rendering.
    _validate_run_inputs(inputs)

    # 2.  Build the deterministic argument vector.
    args: list[str] = ["docker", "run", "--rm"]

    # --name
    args.extend(("--name", inputs.container_name))

    # TTY / stdin flags
    if inputs.tty:
        args.append("--tty")
    if inputs.stdin_open:
        args.append("--interactive")

    # Mounts — ordered: Pi home, projection, primary workspace, extra workspaces.
    _emit_run_mount(args, "bind", inputs.pi_home_host, _CONTAINER_PI_HOME)
    _emit_run_mount(
        args, "bind",
        inputs.projection_host_path,
        inputs.projection_container_path,
        readonly=True,
    )
    _emit_run_mount(args, "bind", inputs.workspace, inputs.workspace)
    for p in inputs.extra_workspaces:
        _emit_run_mount(args, "bind", p, p)
    for mount in sorted(inputs.artifact_mounts, key=lambda item: item.container_target):
        _emit_run_mount(
            args, "bind", mount.host_path, mount.container_target, readonly=True,
        )

    # Corporate trust — read-only system CA bundle mount and the closed
    # client CA environment mapping (enabled only).  Disabled launches emit
    # neither, so inherited image client CA values remain untouched.
    if inputs.corporate_trust_bundle is not None:
        _emit_run_mount(
            args, "bind",
            inputs.corporate_trust_bundle,
            _SYSTEM_CA_BUNDLE,
            readonly=True,
        )
        _emit_client_ca_run_env(args)

    # Working directory
    args.extend(("--workdir", inputs.workspace))

    # Environment: WORKSPACE_PATH_*
    all_projects = (inputs.workspace,) + inputs.extra_workspaces
    for i, proj_path in enumerate(all_projects, start=1):
        args.extend(("--env", f"WORKSPACE_PATH_{i}={proj_path}"))

    if inputs.chown_on_start is not None:
        args.extend(("--env", f"CHOWN_WORK_ON_START={inputs.chown_on_start}"))

    # Host access — conditionally emitted
    if inputs.host_access.is_enabled:
        args.extend(("--add-host", f"host.docker.internal:{inputs.host_access.address}"))
        args.extend(("--env", f"HOST_ACCESS_ADDRESS={inputs.host_access.address}"))
        if inputs.host_access.proxy_port is not None:
            args.extend(("--env", f"HOST_PROXY_PORT={inputs.host_access.proxy_port}"))

    # Corporate proxy — standard proxy variables (enabled only).
    _emit_proxy_run_env(args, inputs)

    # Image
    args.append(inputs.image)

    # Command passthrough
    args.extend(inputs.command)

    return tuple(args)


def render_prospective_build_display(
    inputs: BuildRenderInputs, *, path_semantics: object = os.path,
) -> str:
    """Render an explicitly non-executable, platform-neutral dry-run plan.

    ``path_semantics`` is a test seam only: prospective contexts must never
    pass a value into it because they have no filesystem path.
    """
    if not isinstance(inputs.named_context, Prospective):
        raise ValueError("prospective display requires a prospective named context")
    if inputs.named_context.path is not None:
        raise ValueError("prospective context must not enter path handling")
    if not callable(getattr(path_semantics, "isabs", None)):
        raise ValueError("path_semantics must provide isabs")
    expected_platform = _docker_platform(inputs.projection.platform)
    if inputs.platform != expected_platform:
        raise EffectiveConfigError("platform mismatch in prospective build plan")
    _validate_build_projection(inputs.projection)
    if not inputs.image_tag.strip() or not inputs.target_stage.strip() or not inputs.build_context.strip():
        raise ValueError("prospective build requires tag, target, and primary context")
    args = ["docker", "build", "--tag", inputs.image_tag, "--target", inputs.target_stage,
            "--platform", inputs.platform, "--progress", inputs.progress]
    if inputs.dockerfile is not None:
        args.extend(("--file", inputs.dockerfile))
    if not inputs.cache.enabled:
        args.append("--no-cache")
    if inputs.pull:
        args.append("--pull")
    args.extend(("--build-context", "constructor-artifacts=<prospective:not-materialized>"))
    _emit_build_args(args, inputs.projection)
    _emit_proxy_build_args(args, inputs)
    _emit_corporate_trust_build_args(args, inputs)
    if inputs.dev_uid < 0 or inputs.dev_gid < 0:
        raise ValueError("dev_uid and dev_gid must be >= 0")
    args.extend(("--build-arg", f"DEV_UID={inputs.dev_uid}", "--build-arg", f"DEV_GID={inputs.dev_gid}"))
    args.append(inputs.build_context)
    display = render_command_display(tuple(args))
    # This is presentation metadata, not shell input; retain the required
    # stable token instead of shell-quoting its angle brackets.
    display = display.replace("'constructor-artifacts=<prospective:not-materialized>'", "constructor-artifacts=<prospective:not-materialized>")
    return "Planned build (not executable)\n" + display


def render_command_display(args: tuple[str, ...]) -> str:
    """Render a command vector as a shell-escaped string for display.

    Returns a ``str`` suitable for terminal output (logs, dry-run
    messages).  The returned string MUST NOT be fed back into
    ``subprocess`` — it may contain shell metacharacters that would
    alter the executed command.
    """
    import shlex

    return shlex.join(list(args))


def render_build_environment(
    effective: EffectiveConfiguration,
    *,
    platform: str = "linux-amd64",
) -> Mapping[str, str]:
    """Return a deterministic mapping of build-argument names to string values.

    Each key is a Docker ``--build-arg`` name; each value is a plain string
    (never a list or mapping).  The mapping is derived from the immutable
    *effective* configuration and never consults the network or filesystem.
    """
    inv = effective.inventory

    artifacts_uv = inv.stages.toolchain.uv.artifacts.get(platform)
    if artifacts_uv is None:
        raise EffectiveConfigError(
            f"No artifact for platform {platform!r} in uv entry"
        )
    artifacts_rtk = inv.stages.rtk_prebuilt.rtk.artifacts.get(platform)
    if artifacts_rtk is None:
        raise EffectiveConfigError(
            f"No artifact for platform {platform!r} in rtk entry"
        )
    artifacts_fd = inv.stages.fd_prebuilt.fd.artifacts.get(platform)
    if artifacts_fd is None:
        raise EffectiveConfigError(
            f"No artifact for platform {platform!r} in fd entry"
        )

    node = inv.stages.base.node
    registry = node.source.registry.rstrip("/")
    repository = node.source.repository
    node_image = f"{registry}/{repository}:{node.tag}@{node.digest}"

    result: dict[str, str] = {}

    # Base
    result["NODE_BASE_IMAGE"] = node_image

    # Toolchain
    result["RUST_VERSION"] = inv.stages.toolchain.rust.version
    result["RUST_PROFILE"] = inv.stages.toolchain.rust.profile
    result["RUST_COMPONENTS"] = " ".join(inv.stages.toolchain.rust.components)
    # Mandatory rustup bootstrap artifact
    rustup_artifact = inv.stages.toolchain.rust.rustup.get(platform)
    if rustup_artifact is None:
        raise EffectiveConfigError(
            f"No artifact for platform {platform!r} in rustup entry"
        )
    result["RUSTUP_SHA256"] = rustup_artifact.sha256
    result["UV_VERSION"] = inv.stages.toolchain.uv.version
    result["UV_SHA256"] = artifacts_uv.sha256
    result["PYTHON_VERSION"] = inv.stages.toolchain.python.version
    result["TY_VERSION"] = inv.stages.toolchain.ty.version

    # Prebuilt
    result["RTK_VERSION"] = inv.stages.rtk_prebuilt.rtk.version
    result["RTK_SHA256"] = artifacts_rtk.sha256
    result["FD_VERSION"] = inv.stages.fd_prebuilt.fd.version
    result["FD_SHA256"] = artifacts_fd.sha256

    # Node tools
    result["PI_VERSION"] = inv.stages.pi_tools.pi.version
    result["OPENSPEC_VERSION"] = inv.stages.openspec_tools.openspec.version

    # Runtime
    result["OH_MY_ZSH_VERSION"] = inv.stages.runtime.oh_my_zsh.revision

    # Pi extensions (deterministic sorted by name)
    for name in sorted(inv.runtime_pi_extensions.keys()):
        ext = inv.runtime_pi_extensions[name]
        prefix = name.upper().replace("-", "_").replace("@", "")
        result[f"{prefix}_VERSION"] = ext.version

    return MappingProxyType(result)


def write_effective_inventory(
    effective: EffectiveConfiguration,
    destination: Path,
    *,
    repo_root: Path | None = None,
    output_path: str | None = None,
) -> None:
    """Write the effective inventory as TOML to *destination*.

    The file is written atomically via a sibling temporary file followed
    by ``os.replace()``.  The parent directory is created if needed.

    When *repo_root* and *output_path* are provided, the path is validated:

    * Must be a relative path inside *repo_root*
    * Must not be the authoritative ``docker-constructor.toml``
    * Must not contain ``..`` traversal or absolute paths
    * Must not resolve to a symlink pointing outside *repo_root*

    The validated *destination* is then ``repo_root / output_path``
    resolved.
    """
    if repo_root is not None and output_path is not None:
        destination = _validate_inventory_output(repo_root, output_path)

    data = to_plain_data(effective.inventory)

    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        suffix=".toml",
        prefix=".versions-",
        dir=str(destination.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            _write_toml(fh, data)
    except Exception:
        os.unlink(tmp_path)
        raise
    os.replace(tmp_path, destination)


def _validate_inventory_output(
    repo_root: Path, relative_path: str
) -> Path:
    """Validate *relative_path* is safe and return the resolved ``Path``.

    Rejects:
    * Absolute paths
    * ``..`` traversal
    * The authoritative ``docker-constructor.toml``
    * Symlink escapes (resolved real path outside repo_root)
    """
    repo_root = repo_root.resolve()

    # Reject absolute paths
    if relative_path.startswith("/") or Path(relative_path).is_absolute():
        raise EffectiveInventoryOutputError(
            f"Effective inventory output must be a relative path, "
            f"not {relative_path!r}"
        )

    # Reject traversal
    if ".." in Path(relative_path).parts:
        raise EffectiveInventoryOutputError(
            f"Effective inventory output must be inside the repo root; "
            f"{relative_path!r} contains '..'"
        )

    # Reject docker-constructor.toml (exact match only, not prefixed)
    norm = Path(relative_path).as_posix()
    if norm in ("docker-constructor.toml", "./docker-constructor.toml"):
        raise EffectiveInventoryOutputError(
            "Effective inventory output cannot be docker-constructor.toml "
            "(the authoritative source)"
        )

    # Resolve and check boundaries + symlinks.
    # Use strict=False for the intermediate resolve so we can detect
    # symlinks at the leaf component separately.
    raw = repo_root / relative_path  # may not exist yet
    # Resolve parent without following symlinks at the leaf
    raw_parent = raw.parent.resolve() if raw.parent != raw else raw.parent

    # Check parent is inside repo_root
    try:
        raw_parent.relative_to(repo_root)
    except ValueError:
        raise EffectiveInventoryOutputError(
            f"Effective inventory output must be inside the repo root; "
            f"{relative_path!r} resolves to {str(raw_parent)!r}"
        )

    # Walk intermediate components for symlink escapes
    for parent in raw_parent.parents:
        if parent == repo_root:
            break
        if parent.is_symlink():
            real_parent = parent.resolve()
            try:
                real_parent.relative_to(repo_root)
            except ValueError:
                raise EffectiveInventoryOutputError(
                    f"Effective inventory output traverses a symlink "
                    f"pointing outside the repo root: {str(parent)!r} "
                    f"-> {str(real_parent)!r}"
                )

    # Reject if the leaf exists and is a symlink outside
    if raw.is_symlink():
        real = raw.resolve()
        try:
            real.relative_to(repo_root)
        except ValueError:
            raise EffectiveInventoryOutputError(
                f"Effective inventory output is a symlink pointing "
                f"outside the repo root: {relative_path!r} "
                f"-> {str(real)!r}"
            )

    resolved = raw.resolve()

    return resolved


class EffectiveInventoryOutputError(ValueError):
    """Raised when the effective inventory output path is invalid."""


def compose_command(args: Sequence[str]) -> tuple[str, ...]:
    """Return the canonical ``docker compose`` argument tuple.

    ``args`` are the user-supplied arguments after ``--``, e.g.
    ``("build", "pi")``.
    """
    return ("docker", "compose", *args)


# ---------------------------------------------------------------------------
# Deterministic TOML serialization
# ---------------------------------------------------------------------------

_TOML_BARE_KEY_RE = __import__("re").compile(r'^[A-Za-z0-9_-]+$')


def _toml_key(k: str) -> str:
    """Quote *k* if it contains characters not allowed in TOML bare keys.

    Uses TOML basic-string quoting with proper escape of backslash and
    double-quote characters.
    """
    if _TOML_BARE_KEY_RE.match(k):
        return k
    escaped = k.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_table(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("TOML table must be a mapping")
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise TypeError("TOML table keys must be strings")
        result[key] = item
    return result


def _toml_array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise TypeError("TOML array must be a list")
    return list(value)


def _write_toml(fh: TextIO, data: object, *, _prefix: str = "") -> None:
    """Write plain-data *data* as deterministically-ordered TOML.

    Uses dotted-key table headers (``[build.stages.toolchain.rust]``) for
    sections and inline ``key = value`` for leaves and small tables.
    """
    if isinstance(data, dict):
        _write_dict(fh, _toml_table(data), _prefix)
    elif isinstance(data, list):
        _write_array(fh, _toml_array(data), _prefix)
    elif isinstance(data, str):
        fh.write(f"{_toml_str(data)}\n")
    elif isinstance(data, bool):
        fh.write(f"{'true' if data else 'false'}\n")
    elif isinstance(data, (int, float)):
        fh.write(f"{data}\n")
    elif data is None:
        fh.write("# <absent>\n")
    else:
        fh.write(f"{_toml_str(str(data))}\n")


def _write_dict(fh: TextIO, data: dict[str, object], prefix: str) -> None:
    keys = sorted(data.keys(), key=str)
    # Scalars first, then arrays, then nested dicts.
    # TOML requires that bare ``key = value`` lines appear before
    # any ``[header]`` — otherwise they get absorbed into the last table.
    # Arrays (e.g. components = [...]) must also precede nested tables
    # because a plain ``key = [...]`` after a ``[subsection]`` header
    # would be captured into that subsection.
    scalars: list[tuple[str, object]] = []
    nested: list[tuple[str, dict[str, object]]] = []
    arrays: list[tuple[str, list[object]]] = []

    for k in keys:
        v = data[k]
        if isinstance(v, dict):
            table = _toml_table(v)
            if _is_leaf_dict(table):
                scalars.append((k, table))  # inline table counts as scalar
            else:
                nested.append((k, table))
        elif isinstance(v, list):
            arrays.append((k, _toml_array(v)))
        else:
            scalars.append((k, v))

    # --- scalars ---
    for k, v in scalars:
        qk = _toml_key(k)
        if isinstance(v, dict):
            # Inline table — omit None values (TOML has no null literal)
            parts = []
            for sk in sorted(v.keys(), key=str):
                sv = v[sk]
                if sv is None:
                    continue
                if not isinstance(sk, str):
                    raise TypeError("TOML table keys must be strings")
                parts.append(f"{_toml_key(sk)} = {_toml_value(sv)}")
            if parts:
                fh.write(f"{qk} = {{ ")
                fh.write(", ".join(parts))
                fh.write(" }\n")
            # else: empty inline table → omit entirely
        elif isinstance(v, str):
            fh.write(f"{qk} = {_toml_str(v)}\n")
        elif isinstance(v, bool):
            fh.write(f"{qk} = {'true' if v else 'false'}\n")
        elif isinstance(v, (int, float)):
            fh.write(f"{qk} = {v}\n")
        elif v is None:
            fh.write(f"# {qk} = <absent>\n")
        else:
            fh.write(f"{qk} = {_toml_str(str(v))}\n")

    # --- nested dicts (table headers) ---
    # Emit arrays BEFORE nested tables so that ``components = [...]``
    # lines are not captured into a preceding ``[subsection]``.
    for k, v in arrays:
        qk = _toml_key(k)
        full = f"{prefix}.{qk}" if prefix else qk
        if all(isinstance(i, dict) for i in v):
            for item in v:
                fh.write(f"\n[[{full}]]\n")
                _write_inline_dict(fh, _toml_table(item))
        else:
            fh.write(f"{qk} = [")
            fh.write(", ".join(_toml_value(i) for i in v))
            fh.write("]\n")

    # --- nested dicts (table headers) ---
    for k, v in nested:
        qk = _toml_key(k)
        full = f"{prefix}.{qk}" if prefix else qk
        fh.write(f"\n[{full}]\n")
        _write_dict(fh, v, full)


def _is_leaf_dict(d: dict[str, object]) -> bool:
    """Return True if *d* contains only scalar/string values (no nested dicts/lists)."""
    for v in d.values():
        if isinstance(v, (dict, list)):
            return False
    return True


def _write_inline_dict(fh: TextIO, data: dict[str, object]) -> None:
    """Write an inline key=value dict for array-of-tables entries."""
    for k in sorted(data.keys(), key=str):
        v = data[k]
        fh.write(f"{k} = {_toml_value(v)}\n")


def _write_array(fh: TextIO, data: list[object], prefix: str) -> None:
    if not prefix:
        fh.write("[]\n")
        return
    if all(isinstance(i, dict) for i in data):
        for item in data:
            fh.write(f"\n[[{prefix}]]\n")
            _write_inline_dict(fh, _toml_table(item))
    else:
        fh.write(f"{prefix} = [")
        fh.write(", ".join(_toml_value(i) for i in data))
        fh.write("]\n")


def _toml_value(v: object) -> str:
    """Format a scalar value for TOML."""
    if isinstance(v, str):
        return _toml_str(v)
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if v is None:
        return '""'
    return _toml_str(str(v))


def _toml_str(s: str) -> str:
    """Quote *s* as a TOML basic string, escaping backslashes and quotes."""
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


# ---------------------------------------------------------------------------
# Effective Build Projection serialization (Stage 4)
# ---------------------------------------------------------------------------

import dataclasses as _dc


def serialize_effective_build(projection) -> dict[str, object]:
    """Serialize an EffectiveBuildProjection to a deterministic plain dict."""
    p = projection
    return {
        "platform": p.platform,
        "node": {
            "image": p.node.image,
            "node_version": p.node.node_version,
            "npm_version": p.node.npm_version,
        },
        "rust": _serialize_rust(p.rust),
        "uv": _serialize_tool(p.uv),
        "python": {
            "version": p.python_version,
        },
        "ty": {
            "version": p.ty_version,
        },
        "rtk": _serialize_tool(p.rtk),
        "fd": _serialize_tool(p.fd),
        "pi": {
            "version": p.pi_version,
            "package": p.pi_release.package,
            "release_repository": p.pi_release.release_repository,
            "release_tag_prefix": p.pi_release.release_tag_prefix,
        },
        "openspec": {
            "version": p.openspec_version,
        },
        "oh-my-zsh": {
            "revision": p.oh_my_zsh_revision,
        },
    }


def _serialize_rust(rust) -> dict[str, object]:
    result: dict[str, object] = {
        "version": rust.version,
        "profile": rust.profile,
        "components": list(rust.components),
        "rustup": {
            "url": rust.rustup.url,
            "sha256": rust.rustup.sha256,
        },
    }
    return result


def _serialize_tool(tool) -> dict[str, object]:
    return {
        "version": tool.version,
        "artifact": {
            "url": tool.artifact.url,
            "sha256": tool.artifact.sha256,
        },
    }


def validate_effective_build(data: dict[str, object]):
    """Validate a plain dict against the EffectiveBuildProjection schema.

    Raises ValueError or EffectiveConfigError on invalid data.
    """
    from .model import EffectiveBuildProjection, EffectiveNode, EffectiveRust, \
        EffectiveTool, EffectiveArtifact

    if not isinstance(data, dict):
        raise ValueError("effective build projection must be a dict")

    required = {"platform", "node", "rust", "uv", "python", "ty", "rtk", "fd", "pi", "openspec", "oh-my-zsh"}
    missing = required - set(data.keys())
    if missing:
        raise ValueError(f"missing required sections: {sorted(missing)}")

    platform_val = data.get("platform")
    if platform_val not in ("linux-amd64", "linux-arm64"):
        raise ValueError(f"platform must be 'linux-amd64' or 'linux-arm64', got {platform_val!r}")

    node = data["node"]
    if not isinstance(node, dict) or "image" not in node:
        raise ValueError("node section missing 'image'")
    for nkey in ("node_version", "npm_version"):
        nval = node.get(nkey)
        if not isinstance(nval, str) or not nval:
            raise ValueError(f"node section missing {nkey!r}")

    rust = data["rust"]
    if not isinstance(rust, dict):
        raise ValueError("rust section must be a dict")
    for key in ("version", "profile", "components", "rustup"):
        if key not in rust:
            raise ValueError(f"rust section missing {key!r}")
    rustup = rust["rustup"]
    if not isinstance(rustup, dict):
        raise ValueError("rust.rustup must be a dict")
    for akey in ("url", "sha256"):
        if akey not in rustup:
            raise ValueError(f"rust.rustup missing {akey!r}")

    for section_name in ("uv", "rtk", "fd"):
        sec = data[section_name]
        if not isinstance(sec, dict):
            raise ValueError(f"{section_name} must be a dict")
        for key in ("version", "artifact"):
            if key not in sec:
                raise ValueError(f"{section_name} section missing {key!r}")
        art = sec["artifact"]
        if not isinstance(art, dict):
            raise ValueError(f"{section_name}.artifact must be a dict")
        for akey in ("url", "sha256"):
            if akey not in art:
                raise ValueError(f"{section_name}.artifact missing {akey!r}")

    for section_name in ("python", "ty", "openspec"):
        sec = data[section_name]
        if not isinstance(sec, dict) or "version" not in sec:
            raise ValueError(f"{section_name} section missing 'version'")

    pi = data["pi"]
    if not isinstance(pi, dict) or "version" not in pi:
        raise ValueError("pi section missing 'version'")
    for pkey in ("package", "release_repository", "release_tag_prefix"):
        pval = pi.get(pkey)
        if not isinstance(pval, str) or not pval:
            raise ValueError(f"pi section missing {pkey!r}")

    oh = data["oh-my-zsh"]
    if not isinstance(oh, dict) or "revision" not in oh:
        raise ValueError("oh-my-zsh section missing 'revision'")

    # Reject runtime / extension keys (platform is allowed as a top-level scalar)
    forbidden = {"runtime", "pi-extensions", "extensions", "cache", "update", "override"}
    for key in data:
        if key in forbidden:
            raise ValueError(f"forbidden key in build projection: {key!r}")


def _serialize_toml_bytes(data: object) -> bytes:
    """Render deterministically-ordered TOML *data* to UTF-8 bytes."""
    buffer = io.StringIO()
    _write_toml(buffer, data)
    return buffer.getvalue().encode("utf-8")


def _validate_effective_destination(
    ops: PosixFileOps, directory: DirectoryCapability, name: str
) -> None:
    """Validate an existing destination leaf without following it.

    Absence is allowed.  A symlink, non-regular entry, foreign-owned entry,
    multiply linked entry, or permissive mode is rejected with the existing
    rendering-domain diagnostic before the L2 replacement commits.  The
    validation uses a retained no-follow descriptor relative to the verified
    ``generated`` capability; no pathname is reconstructed.
    """
    try:
        fd = ops.openat(directory.fd, name, _VALIDATE_FLAGS, 0)
    except FileNotFoundError:
        return
    except OSError as exc:
        # Only a known unsafe-leaf rejection (a no-follow symlink) is a
        # rendering-domain diagnostic.  Every other operational failure
        # (EIO, EMFILE, ...) stays observable as its raw ``OSError``.
        if exc.errno == errno.ELOOP:
            raise EffectiveInventoryOutputError(
                f"{name} is not a regular file"
            ) from exc
        raise
    primary: BaseException | None = None
    try:
        info = ops.fstat(fd)
        # The destination type/ownership/link/mode checks run inside the
        # protected region so an unsafe-destination diagnostic is the primary
        # failure and an ordinary descriptor-close failure is only attached as
        # a secondary diagnostic.
        if not stat.S_ISREG(info.st_mode):
            raise EffectiveInventoryOutputError(f"{name} is not a regular file")
        if info.st_uid != os.geteuid():
            raise EffectiveInventoryOutputError(
                f"{name} is not owned by the invoking user"
            )
        if info.st_nlink != 1:
            raise EffectiveInventoryOutputError(
                f"{name} must have exactly one hard link"
            )
        mode = stat.S_IMODE(info.st_mode)
        if mode != 0o600:
            raise EffectiveInventoryOutputError(
                f"{name} has mode {oct(mode)}, expected 0o600"
            )
    except BaseException as exc:
        primary = exc
        raise
    finally:
        # A descriptor-close failure must never mask a primary validation
        # failure: it is attached as secondary diagnostics and the primary
        # exception keeps propagating.  Only I/O close failures are caught;
        # a process-control interruption during close propagates unchanged.
        try:
            ops.close(fd)
        except OSError as close_exc:
            if primary is not None:
                attach_secondary(primary, [close_exc])
            else:
                raise


def _raise_effective_failure(name: str, exc: TransactionError) -> None:
    """Map an L2 replacement failure while preserving the legacy contract.

    Unsafe-destination validation retains an :class:`EffectiveInventoryOutputError`
    diagnostic; an unexpected operational failure re-raises the original
    ``OSError`` object with any shared cleanup failures attached, so the raw
    ``errno`` and chaining stay observable.  Nothing here translates a
    process-control interruption: those are not :class:`TransactionError`
    instances and propagate unchanged.
    """
    if isinstance(exc, UnsafeFileError) and exc.stage in (
        STAGE_VALIDATE,
        STAGE_VALIDATE_DESTINATION,
    ):
        raise EffectiveInventoryOutputError(
            f"{name} is not a safe owned regular file"
        ) from exc
    cause = exc.cause
    if isinstance(cause, OSError):
        attach_secondary(cause, list(exc.secondary))
        raise cause
    raise EffectiveInventoryOutputError(str(exc)) from exc


def write_effective_build(
    projection,
    *,
    repo_root,
    project_state: ProjectState | None = None,
) -> Path:
    """Write the effective build projection durably into verified state.

    The destination is always the selected constructor project's external
    ``generated/docker-constructor.build.effective.toml``.  The supplied (or
    resolved) ``ProjectState`` is re-verified with :func:`validate_project_state`,
    which recomputes the canonical project identity and namespace name and
    re-reads ``project.json`` through a retained no-follow namespace
    descriptor.  ``generated`` is then opened relative to that descriptor and
    the projection is published through the shared L2 durable-replacement
    contract: a complete private sibling is written and flushed, any existing
    destination is validated, and the destination is atomically replaced and
    its parent directory flushed.  No pathname-based
    ``resolve``/``mkdir``/``mkstemp``/``replace`` is used, so a symlink
    swapped in during publication cannot redirect the write outside the
    verified namespace.

    A *project_state* supplied by the caller must already belong to the
    canonical *repo_root*; otherwise it is resolved from *repo_root*.
    """
    from docker.versioning.project_state import (
        ProjectState,
        ProjectStateError,
        resolve_project_state,
        validate_project_state,
    )

    repo_root = Path(repo_root).resolve()
    if project_state is None:
        project_state = resolve_project_state(repo_root)
    elif project_state.project_path != repo_root:
        raise EffectiveInventoryOutputError(
            f"project state belongs to {project_state.project_path!r}, "
            f"not the constructor project {repo_root!r}"
        )

    # Validate the projection in memory before touching any existing file.
    data = serialize_effective_build(projection)
    validate_effective_build(data)
    payload = _serialize_toml_bytes(data)

    destination_name = "docker-constructor.build.effective.toml"
    ops = PosixFileOps()
    generated_fd = None
    primary: BaseException | None = None
    try:
        try:
            # Re-verify the supplied state and retain its namespace descriptor.
            with validate_project_state(project_state) as validated:
                namespace_fd = validated.namespace_fd
                try:
                    generated_fd = ops.openat(
                        namespace_fd, "generated", _DIR_FLAGS, 0
                    )
                except OSError as exc:
                    raise EffectiveInventoryOutputError(
                        f"generated project state child is unsafe or missing: {exc}"
                    ) from exc
                generated_stat = ops.fstat(generated_fd)
                if (not stat.S_ISDIR(generated_stat.st_mode)
                        or generated_stat.st_uid != os.geteuid()
                        or stat.S_IMODE(generated_stat.st_mode) != 0o700):
                    raise EffectiveInventoryOutputError(
                        "generated project state child must be invoking-user-owned "
                        "and mode 0700"
                    )
                generated = DirectoryCapability.from_fd(
                    ops, generated_fd, "generated"
                )
                _validate_effective_destination(ops, generated, destination_name)
                try:
                    RegularFileContracts(ops).durable_replace(
                        generated, destination_name, payload, 0o600
                    )
                except TransactionError as exc:
                    _raise_effective_failure(destination_name, exc)
        except ProjectStateError as exc:
            raise EffectiveInventoryOutputError(str(exc)) from exc
    except BaseException as exc:
        primary = exc
        raise
    finally:
        if generated_fd is not None:
            # A generated-directory close failure must never mask a primary
            # validation/publication failure: it is attached as secondary
            # diagnostics and the primary exception keeps propagating.  Only
            # I/O close failures are caught; a process-control interruption
            # during close propagates unchanged.
            try:
                ops.close(generated_fd)
            except OSError as close_exc:
                if primary is not None:
                    attach_secondary(primary, [close_exc])
                else:
                    raise

    return project_state.generated_root / destination_name


def _require_not_symlink_escape(path: Path, repo_root: Path, *, label: str) -> None:
    """Raise if *path* is a symlink that resolves outside *repo_root*.

    Used only for directory symlinks.  Leaf-file symlinks are unconditionally
    rejected by the caller.
    """
    if not path.is_symlink():
        return
    resolved = path.resolve()
    try:
        resolved.relative_to(repo_root)
    except ValueError:
        raise EffectiveInventoryOutputError(
            f"{label} is a symlink escaping repo root: {path} -> {resolved}"
        ) from None

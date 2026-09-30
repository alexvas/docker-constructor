"""Deterministic Docker run-vector rendering for the npm assembler.

``render_run_vector`` rechecks the validated-input and assembler bindings
before returning an immutable :class:`DockerRunVector`.  ``render_docker_argv``
turns that vector into the exact ``docker run`` argument list handed to the
execution boundary.  The vector carries a pinned image digest, numeric
UID/GID, a private HOME, read-only inputs, an opaque writable npm cache, one
writable staging output, and no consumer mounts or ambient environment.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .assembler import (
    ASSEMBLER_SCRIPT,
    assembler_script_digest,
    npm_policy_digest,
    npm_policy_env,
)
from .errors import LockedNpmError
from .identity import (
    AssemblerIdentity,
    compute_assembler_input_identity,
)
from .image_ref import validate_image_reference
from .model import ValidatedAssemblyInput
from .network import CorporateNetworkPolicy
from docker.versioning.corporate_network import (
    CLIENT_CA_ENVIRONMENT,
    SYSTEM_CA_BUNDLE,
)

#: Fixed container paths — private HOME and the opaque npm cache both live
#: under the disposable cache mount; the staging workspace is the workdir.
ASSEMBLER_HOME = "/cache/home"
ASSEMBLER_CACHE = "/cache"
ASSEMBLER_WORKDIR = "/work"
LOCKFILE_CONTAINER_PATH = "/work/package-lock.json"

#: Fixed container-side system CA bundle that receives the corporate trust
#: override when corporate trust is enabled.  Imported from the shared
#: corporate-network owner and re-exported so build and runtime point at one
#: destination.

#: npm does not necessarily ask Node to use the operating-system CA store.
#: Point npm explicitly at the fixed container path whenever a reviewed trust
#: bundle is mounted; the machine-local host path remains absent from env.
NPM_CONFIG_CAFILE = "npm_config_cafile"

#: Standard proxy variable names emitted at runtime.  The endpoint is copied
#: verbatim across every uppercase/lowercase HTTP, HTTPS, and ALL variable.
PROXY_URL_ENV_NAMES: tuple[str, ...] = (
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
)

#: Standard proxy bypass variable names, emitted only for an explicit list.
PROXY_BYPASS_ENV_NAMES: tuple[str, ...] = ("NO_PROXY", "no_proxy")


@dataclass(frozen=True, order=True)
class Mount:
    """One narrow bind mount for the assembler container."""

    host: str
    """Host path (must already be prepared by the caller)."""

    container: str
    """Container path."""

    mode: str
    """``"ro"`` or ``"rw"``."""


@dataclass(frozen=True)
class DockerRunVector:
    """Immutable, deterministic ``docker run`` vector for one assembly."""

    image: str
    """Pinned immutable image reference (bare digest or
    repository-qualified digest)."""

    user: str
    """Numeric ``"uid:gid"`` host identity."""

    name: str
    """Container name used for cancellation cleanup."""

    home: str
    """Private container HOME."""

    workdir: str
    """Container working directory (writable staging)."""

    env: tuple[tuple[str, str], ...]
    """Explicit, deterministic environment (no ambient input)."""

    mounts: tuple[Mount, ...]
    """Read-only inputs, opaque cache, and writable staging — nothing else."""

    command: tuple[str, ...]
    """``/bin/sh -c <canonical assembler script>``."""


def recheck_assembler_bindings(assembler: AssemblerIdentity) -> None:
    """Reject an assembler whose image reference or script/policy digests
    do not match the canonical script and policy bytes actually executed.

    The image reference is validated first so an invalid or mutable
    reference fails before any cache, staging, executor, or Docker effect.
    The assembler digest itself is re-verified by
    :func:`compute_assembler_input_identity`; this check ties the image
    reference and the two content digests to the fixed assembler script and
    npm policy.
    """
    validate_image_reference(assembler.image_digest)
    if assembler.script_digest != assembler_script_digest():
        raise LockedNpmError(
            "script_digest_mismatch",
            "the assembler script digest does not match the canonical "
            "assembler script bytes",
        )
    if assembler.policy_digest != npm_policy_digest():
        raise LockedNpmError(
            "policy_digest_mismatch",
            "the assembler policy digest does not match the fixed npm policy",
        )


def render_run_vector(
    *,
    validated: ValidatedAssemblyInput,
    assembler: AssemblerIdentity,
    staging: Path,
    npm_cache: Path,
    uid: int,
    gid: int,
    name: str,
    corporate_network: CorporateNetworkPolicy | None = None,
) -> DockerRunVector:
    """Render a deterministic run vector after rechecking all bindings.

    Re-verifies the supplied validated input (re-running preflight and
    comparing field-for-field) and the assembler identity, rejects any
    script/policy digest drift, and then renders the vector with a pinned
    image digest, numeric UID/GID, private HOME, explicit environment,
    read-only lockfile input, opaque cache, and writable staging.

    When *corporate_network* carries a credential-free proxy URL, the exact
    URL is emitted under every standard proxy variable (with both bypass
    variables only for an explicit bypass list).  When it carries an
    enabled corporate trust bundle, that host path is mounted read-only at
    the fixed system trust path before npm network access and the closed
    five-variable client CA environment mapping is applied on top of the
    existing ``npm_config_cafile`` behavior.  A disabled policy emits no
    proxy environment, no trust override, and no client CA assignment.
    """
    recheck_assembler_bindings(assembler)
    compute_assembler_input_identity(validated, assembler)

    env: list[tuple[str, str]] = [
        ("HOME", ASSEMBLER_HOME),
        ("npm_config_cache", ASSEMBLER_CACHE),
        ("REVIEWED_NODE_VERSION", validated.node_version),
        ("REVIEWED_NPM_VERSION", validated.npm_version),
        *npm_policy_env(),
    ]
    mounts: list[Mount] = [
        Mount(str(staging), ASSEMBLER_WORKDIR, "rw"),
        Mount(
            str(staging / "package-lock.json"),
            LOCKFILE_CONTAINER_PATH,
            "ro",
        ),
        Mount(str(npm_cache), ASSEMBLER_CACHE, "rw"),
    ]

    if corporate_network is not None:
        if corporate_network.proxy_url is not None:
            for proxy_name in PROXY_URL_ENV_NAMES:
                env.append((proxy_name, corporate_network.proxy_url))
            if corporate_network.proxy_no_proxy is not None:
                for bypass_name in PROXY_BYPASS_ENV_NAMES:
                    env.append((bypass_name, corporate_network.proxy_no_proxy))
        if corporate_network.corporate_trust_bundle is not None:
            for ca_name, ca_value in CLIENT_CA_ENVIRONMENT:
                env.append((ca_name, ca_value))
            env.append((NPM_CONFIG_CAFILE, SYSTEM_CA_BUNDLE))
            mounts.append(
                Mount(
                    corporate_network.corporate_trust_bundle,
                    SYSTEM_CA_BUNDLE,
                    "ro",
                )
            )

    return DockerRunVector(
        image=assembler.image_digest,
        user=f"{uid}:{gid}",
        name=name,
        home=ASSEMBLER_HOME,
        workdir=ASSEMBLER_WORKDIR,
        env=tuple(env),
        mounts=tuple(mounts),
        command=("/bin/sh", "-c", ASSEMBLER_SCRIPT),
    )


def render_docker_argv(
    vector: DockerRunVector, *, docker_bin: str = "docker"
) -> tuple[str, ...]:
    """Render the exact ``docker run`` argument list for *vector*."""
    argv = [docker_bin, "run", "--rm", "--name", vector.name, "--user", vector.user]
    for key, value in vector.env:
        argv.extend(("--env", f"{key}={value}"))
    for mount in vector.mounts:
        argv.extend(("--volume", f"{mount.host}:{mount.container}:{mount.mode}"))
    argv.extend(("--workdir", vector.workdir, vector.image))
    argv.extend(vector.command)
    return tuple(argv)

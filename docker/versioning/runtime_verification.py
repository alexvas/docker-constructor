"""Runtime verification — inspect a running container's state against
host-side expectations without injecting host metadata.

All process boundaries are injectable so tests remain daemon-independent.

Checks
------

+--------------------------+-----------------------------------------------------------+
| Key                      | What is verified                                          |
+==========================+===========================================================+
| ``projection.identity``  | The mount at ``/run/pi-cli/docker-constructor.runtime.toml``   |
|                          | is identical to the host file at ``runtime_projection_path``   |
|                          | (hash computed from the host file, never caller-supplied).     |
+--------------------------+-----------------------------------------------------------+
| ``projection.readonly``  | The runtime projection mount is read-only.  Verified via        |
|                          | ``/proc/mounts`` mount options (``ro``), not merely file       |
|                          | permissions — ``test -w`` is a secondary sanity check only.    |
+--------------------------+-----------------------------------------------------------+
| ``extensions.results``   | Every declared Pi extension has its npm package installed    |
|                          | at the expected version under                                |
|                          | ``/home/dev/.pi/agent/npm/node_modules/<package>/``          |
|                          | (verified by reading ``package.json``).                      |
+--------------------------+-----------------------------------------------------------+
| ``workspaces.present``     | ``WORKSPACE_PATH_1..N`` environment variables each contain        |
|                          | exactly the corresponding request-supplied path, AND each      |
|                          | directory exists and is accessible.  Numbering is consecutive  |
|                          | 1..N with no unexpected next entry.                           |
+--------------------------+-----------------------------------------------------------+
| ``working.directory``    | The container working directory equals ``WORKSPACE_PATH_1``.   |
+--------------------------+-----------------------------------------------------------+
| ``ownership.dev``        | ``/home/dev/.pi`` and every workspace path are owned          |
|                          | by ``dev:dev`` (UID/GID 1000:1000).                          |
+--------------------------+-----------------------------------------------------------+
| ``pi-home.setup``        | ``~/.pi`` exists and is writable by dev.                     |
+--------------------------+-----------------------------------------------------------+
| ``gateway.mapping``      | ``host.docker.internal`` resolves to exactly the            |
|                          | expected address when host access is enabled.  A            |
|                          | different address or a resolution failure are both         |
|                          | check failures — there is no fallback.  This check is      |
|                          | omitted when host access is disabled.                      |
+--------------------------+-----------------------------------------------------------+
| ``host-access.address``  | ``HOST_ACCESS_ADDRESS`` equals the expected address when    |
|                          | host access is enabled, or is unset when disabled.         |
+--------------------------+-----------------------------------------------------------+
| ``host-access.proxy-port`` | ``HOST_PROXY_PORT`` matches the configured port when       |
|                          | present in policy, or is unset when disabled/omitted.      |
+--------------------------+-----------------------------------------------------------+
| ``corporate-trust.mount`` | When corporate trust is enabled, the fixed system CA      |
|                          | bundle is mounted read-only at                            |
|                          | ``/etc/ssl/certs/ca-certificates.crt``; when disabled,    |
|                          | that exact mountpoint field is checked to be absent from |
|                          | ``/proc/mounts``.                                        |
+--------------------------+-----------------------------------------------------------+
| ``corporate-trust.environment`` | When corporate trust is enabled, each of the five   |
|                          | fixed client CA variables (``NODE_EXTRA_CA_CERTS``,       |
|                          | ``SSL_CERT_FILE``, ``REQUESTS_CA_BUNDLE``, ``PIP_CERT``,  |
|                          | ``CURL_CA_BUNDLE``) is checked to equal exactly           |
|                          | ``/etc/ssl/certs/ca-certificates.crt``.  A missing or     |
|                          | mismatched variable is reported on its own check while    |
|                          | the mount result is retained.  When disabled, no          |
|                          | constructor-defined client CA expectation is emitted, so  |
|                          | inherited image values are neither required nor reported  |
|                          | as enabled policy.                                        |
+--------------------------+-----------------------------------------------------------+
| ``proxy.environment``    | When a proxy URL is configured, every standard            |
|                          | uppercase/lowercase HTTP, HTTPS, and ALL variable matches |
|                          | it and both ``NO_PROXY`` forms match the explicit bypass  |
|                          | list (or are unset when no bypass list is configured).    |
|                          | When disabled, every defined variable is checked to be   |
|                          | unset.                                                   |
+--------------------------+-----------------------------------------------------------+
| ``forbidden.paths``      | ``/run/pi-cli/docker-constructor.toml`` (reviewed inventory)    |
|                          | and ``/run/pi-cli/docker-constructor.build.effective.toml``     |
|                          | (effective build projection) are NOT present inside the         |
|                          | container.  ``/.dockerenv`` is NOT checked — it normally        |
|                          | exists inside Docker containers.                                |
+--------------------------+-----------------------------------------------------------+

Every check runs ``docker exec <container> <command>`` through the
injected *runner*.  The effective runtime projection is read from the
host only — it is NEVER mounted or passed into the container as part
of verification.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence, TYPE_CHECKING

from .corporate_network import CLIENT_CA_ENVIRONMENT, SYSTEM_CA_BUNDLE

if TYPE_CHECKING:
    from docker.versioning.model import HostAccessPolicy


# Fixed container-side system CA bundle.  Runtime verification checks this
# exact mount destination — never a substring — so unrelated mounts whose
# source or destination merely contains the filename are ignored.  The path
# and the five-variable mapping are owned by ``corporate_network`` so the
# build helper, the runtime renderers, and verification share one source.
_SYSTEM_CA_BUNDLE = SYSTEM_CA_BUNDLE

# Targeted ``/proc/mounts`` probe: print the mount options (fourth field)
# only for the mount whose second field (mountpoint) is exactly the system CA
# bundle path.  ``awk`` exits 0 with empty output when no such mount exists.
_MOUNTS_PROBE = (
    "awk",
    f'$2 == "{_SYSTEM_CA_BUNDLE}" {{print $4}}',
    "/proc/mounts",
)


# ── Process boundary (shared contract) ───────────────────────────────


@dataclass(frozen=True)
class ProcessResult:
    argv: tuple[str, ...]
    return_code: int
    stdout: str
    stderr: str


class ProcessOutcome(Protocol):
    @property
    def argv(self) -> tuple[str, ...]: ...

    @property
    def return_code(self) -> int: ...

    @property
    def stdout(self) -> str: ...

    @property
    def stderr(self) -> str: ...


class ProcessRunner(Protocol):
    def run(self, argv: Sequence[str]) -> ProcessOutcome:
        """Execute *argv* and return the outcome."""
        ...


# ── Runtime verification models ──────────────────────────────────────


@dataclass(frozen=True)
class RuntimeCheck:
    """A single runtime-property observation."""
    key: str
    """Unique check key (e.g. ``"projection.identity"``)."""
    ok: bool
    """``True`` when the check passes."""
    detail: str
    """Human-readable description or failure reason."""
    command: tuple[str, ...] | None = None
    """The exact command vector that produced the primary diagnostic
    output (e.g. ``("sha256sum", "/run/pi-cli/...")``).  When the
    check involves a single ``_exec()`` call this matches the full
    ``docker exec`` argv; when the check runs multiple commands it
    holds the deciding command's argv.  ``None`` for checks that did
    not execute any command."""
    exit_code: int | None = None
    """Exit code of the command in *command*."""
    raw_stdout: str | None = None
    """Raw stdout captured from the command in *command*."""
    raw_stderr: str | None = None
    """Raw stderr captured from the command in *command*."""


@dataclass(frozen=True)
class RuntimeVerificationResult:
    """Complete result of a runtime verification pass."""
    container: str
    checks: tuple[RuntimeCheck, ...]
    all_ok: bool
    """``True`` when every check is ``ok``."""
    errors: tuple[str, ...]
    """Non-check errors (container not found, exec failure, etc.)."""


# ── Request ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class VerifyRuntimeRequest:
    """Request to verify a running container against host-side runtime
expectations."""
    container: str
    """Container name or ID to inspect via ``docker exec``."""
    runtime_projection_path: Path
    """Path to the host-side effective runtime projection TOML file.
    The ``projection.identity`` check hashes this file and compares
    the container-side copy — the hash is derived from this path,
    not caller-supplied."""
    workspace_paths: tuple[Path, ...]
    """Workspace paths **inside the container** that should be present
    as ``WORKSPACE_PATH_1..N``."""
    container_pi_home: Path
    """Pi home path **inside the container** (e.g. ``"/home/dev/.pi"``).
    All runtime checks use this as the container-side directory — the
    host-side mapping is the launcher's responsibility and is not
    needed for verification."""
    runner: ProcessRunner
    """Injected process boundary for ``docker exec ...`` invocations."""
    host_access: HostAccessPolicy | None = None
    """Reviewed host-access policy.  When ``None``, host access is
    disabled and the verifier checks that no constructor-set
    host-access variables are present in the container.
    When enabled, the verifier checks that ``host.docker.internal``
    resolves to the expected address and that ``HOST_ACCESS_ADDRESS``
    equals that address."""
    host_access_address: str | None = None
    """Expected ``HOST_ACCESS_ADDRESS`` value.  Required when
    *host_access* is enabled, ``None`` when disabled."""

    corporate_trust_enabled: bool = False
    """Whether corporate trust is enabled.  When enabled, the verifier
    checks the read-only system CA bundle mount; when disabled, it
    reports no corporate trust contract."""

    proxy_url: str | None = None
    """Configured credential-free proxy URL, or ``None`` when disabled.
    When set, every standard proxy variable is checked."""

    proxy_no_proxy: str | None = None
    """Configured bypass list, or ``None`` when omitted.  When set, both
    ``NO_PROXY``/``no_proxy`` are checked; when omitted, both are checked
    to be unset."""


# ── Public API ───────────────────────────────────────────────────────


def verify_runtime(request: VerifyRuntimeRequest) -> RuntimeVerificationResult:
    """Run property checks against *container* and compare the
    observed state against host-side expectations.

    Every check uses ``docker exec`` through the injected *runner*.
    The effective runtime projection is read from the host only — it is
    NEVER mounted or passed into the container as part of verification.
    """
    import hashlib
    import tomllib

    errors: list[str] = []
    checks: list[RuntimeCheck] = []
    runner = request.runner
    container = request.container
    pi_home = str(request.container_pi_home)
    pp = request.workspace_paths

    # ── Load projection + compute host hash ──────────────────────────
    try:
        proj_path = Path(request.runtime_projection_path)
        proj_bytes = proj_path.read_bytes()
        host_hash = hashlib.sha256(proj_bytes).hexdigest()
        proj_data = tomllib.loads(proj_bytes.decode())
    except (OSError, ValueError) as exc:
        return RuntimeVerificationResult(
            container=container,
            checks=(),
            all_ok=False,
            errors=(f"cannot read runtime projection: {exc}",),
        )

    def _exec(cmd: tuple[str, ...]) -> ProcessOutcome:
        """Run ``docker exec <container> ...``."""
        return runner.run(("docker", "exec", container, *cmd))

    def _add(key: str, ok: bool, detail: str,
             result: ProcessOutcome | None = None,
             *, redact_output: bool = False) -> None:
        checks.append(RuntimeCheck(
            key=key, ok=ok, detail=detail,
            command=result.argv if result is not None else None,
            exit_code=result.return_code if result is not None else None,
            raw_stdout=(
                None if result is None or redact_output else result.stdout
            ),
            raw_stderr=(
                None if result is None or redact_output else result.stderr
            ),
        ))

    # ── projection.identity ──────────────────────────────────────────
    r = _exec(("sha256sum", "/run/pi-cli/docker-constructor.runtime.toml"))
    if r.return_code != 0:
        _add("projection.identity", False,
             f"sha256sum failed (exit {r.return_code}): {r.stderr.strip()}", r)
    else:
        parts = r.stdout.strip().split()
        container_hash = parts[0] if parts else ""
        if container_hash == host_hash:
            _add("projection.identity", True,
                 f"projection hash {host_hash} matches host", r)
        else:
            _add("projection.identity", False,
                 f"hash mismatch: container={container_hash[:12]}…"
                 f" host={host_hash[:12]}…", r)

    # ── projection.readonly ──────────────────────────────────────────
    r = _exec(("grep", "docker-constructor.runtime.toml", "/proc/mounts"))
    if r.return_code != 0:
        _add("projection.readonly", False,
             "runtime projection not found in /proc/mounts", r)
    else:
        mount_line = r.stdout.strip()
        # Mount options are comma-separated in the 4th field.
        # Check if ``ro`` is present as a mount option.
        if mount_line:
            tokens = mount_line.split()
            if len(tokens) >= 4:
                opts = tokens[3].split(",")  # e.g. ro,nosuid,nodev,relatime
                if "ro" in opts:
                    rw = _exec(("test", "-w",
                                "/run/pi-cli/docker-constructor.runtime.toml"))
                    if rw.return_code != 0:
                        _add("projection.readonly", True,
                             "runtime projection is read-only"
                             " (ro mount + not writable)", rw)
                    else:
                        _add("projection.readonly", False,
                             "mount claims ro but file is writable", rw)
                else:
                    _add("projection.readonly", False,
                         f"runtime projection mount is not ro"
                         f" (options: {','.join(opts)})", r)
            else:
                _add("projection.readonly", False,
                     f"unexpected /proc/mounts format: {mount_line[:120]}", r)
        else:
            _add("projection.readonly", False,
                 "empty /proc/mounts line for projection", r)

    # ── extensions.results ───────────────────────────────────────────
    extensions = proj_data.get("extensions", {})
    for _key in sorted(extensions):
        ext = extensions[_key]
        pkg_name = ext["package"]
        expected_ver = ext["version"]
        pkg_json_path = f"/home/dev/.pi/agent/npm/node_modules/{pkg_name}/package.json"
        r = _exec(("cat", pkg_json_path))
        if r.return_code != 0:
            _add("extensions.results", False,
                 f"{pkg_name}: package.json not found", r)
        else:
            try:
                import json as _json
                pkg = _json.loads(r.stdout)
                actual_ver = pkg.get("version", "")
            except Exception:
                actual_ver = ""
            if actual_ver == expected_ver:
                _add("extensions.results", True,
                     f"{pkg_name} v{expected_ver} installed", r)
            else:
                _add("extensions.results", False,
                     f"{pkg_name}: expected v{expected_ver}, got v{actual_ver}", r)

    # ── workspaces.present ─────────────────────────────────────────────
    for i, p in enumerate(pp, start=1):
        sp = str(p)
        # Directory accessible
        r = _exec(("test", "-d", sp))
        if r.return_code != 0:
            _add("workspaces.present", False,
                 f"WORKSPACE_PATH_{i} ({sp}) is not an accessible directory", r)
        else:
            # Env var exact value
            r_env = _exec(("printenv", f"WORKSPACE_PATH_{i}"))
            actual = r_env.stdout.strip() if r_env.return_code == 0 else ""
            if actual == sp:
                _add("workspaces.present", True,
                     f"WORKSPACE_PATH_{i}={sp} (dir present)", r_env)
            else:
                _add("workspaces.present", False,
                     f"WORKSPACE_PATH_{i}: expected {sp!r}, got {actual!r}", r_env)
    # Guard: no unexpected next entry
    guard_key = f"WORKSPACE_PATH_{len(pp) + 1}"
    r_guard = _exec(("printenv", guard_key))
    if r_guard.return_code == 0:
        _add("workspaces.present", False,
             f"unexpected {guard_key}={r_guard.stdout.strip()!r}", r_guard)

    # ── working.directory ────────────────────────────────────────────
    if pp:
        r = _exec(("pwd",))
        wd = r.stdout.strip() if r.return_code == 0 else ""
        expected_wd = str(pp[0])
        if wd == expected_wd:
            _add("working.directory", True,
                 f"working directory is WORKSPACE_PATH_1 ({wd})", r)
        else:
            _add("working.directory", False,
                 f"working directory: expected {expected_wd!r}, got {wd!r}", r)
    else:
        _add("working.directory", True,
             "no workspace paths — nothing to verify")

    # ── ownership.dev ────────────────────────────────────────────────
    # Pi home
    r = _exec(("stat", "-c", "%U:%G", pi_home))
    owner = r.stdout.strip() if r.return_code == 0 else ""
    if owner == "dev:dev":
        _add("ownership.dev", True, f"{pi_home} owned by dev:dev", r)
    else:
        _add("ownership.dev", False,
             f"{pi_home} owned by {owner!r}, expected dev:dev", r)
    # Workspace paths
    for i, p in enumerate(pp, start=1):
        sp = str(p)
        r = _exec(("stat", "-c", "%U:%G", sp))
        p_owner = r.stdout.strip() if r.return_code == 0 else ""
        if p_owner == "dev:dev":
            _add("ownership.dev", True,
                 f"WORKSPACE_PATH_{i} ({sp}) owned by dev:dev", r)
        else:
            _add("ownership.dev", False,
                 f"WORKSPACE_PATH_{i} ({sp}) owned by {p_owner!r}, expected dev:dev", r)

    # ── pi-home.setup ─────────────────────────────────────────────────
    r = _exec(("test", "-d", pi_home))
    if r.return_code != 0:
        _add("pi-home.setup", False, f"{pi_home} does not exist", r)
    else:
        rw = _exec(("test", "-w", pi_home))
        if rw.return_code == 0:
            _add("pi-home.setup", True,
                 f"{pi_home} exists and is writable", rw)
        else:
            _add("pi-home.setup", False,
                 f"{pi_home} exists but is not writable", rw)

    # ── host-access checks ───────────────────────────────────────────
    ha = request.host_access
    if ha is not None and ha.enabled:
        # 4.6: Enabled hostname + address-variable checks
        expected_addr = request.host_access_address or ""
        # gateway.mapping
        r = _exec(("getent", "hosts", "host.docker.internal"))
        if r.return_code != 0:
            _add("gateway.mapping", False,
                 f"host.docker.internal resolution failed", r)
        else:
            resolved = r.stdout.strip().split()[0] if r.stdout.strip() else ""
            if resolved == expected_addr:
                _add("gateway.mapping", True,
                     f"host.docker.internal → {resolved}", r)
            else:
                _add("gateway.mapping", False,
                     f"host.docker.internal → {resolved!r}, expected"
                     f" {expected_addr!r}", r)
        # host-access.address
        r = _exec(("printenv", "HOST_ACCESS_ADDRESS"))
        if r.return_code != 0:
            _add("host-access.address", False,
                 f"HOST_ACCESS_ADDRESS is not set", r)
        else:
            actual = r.stdout.strip()
            if actual == expected_addr:
                _add("host-access.address", True,
                     f"HOST_ACCESS_ADDRESS={actual}", r)
            else:
                _add("host-access.address", False,
                     f"HOST_ACCESS_ADDRESS={actual!r}, expected {expected_addr!r}", r)
        # host-access.proxy-port (only when policy declares a port)
        if ha.proxy_port is not None:
            expected_port = str(ha.proxy_port)
            r = _exec(("printenv", "HOST_PROXY_PORT"))
            if r.return_code != 0:
                _add("host-access.proxy-port", False,
                     f"HOST_PROXY_PORT is not set (expected {expected_port})", r)
            else:
                actual_port = r.stdout.strip()
                if actual_port == expected_port:
                    _add("host-access.proxy-port", True,
                         f"HOST_PROXY_PORT={actual_port}", r)
                else:
                    _add("host-access.proxy-port", False,
                         f"HOST_PROXY_PORT={actual_port!r}, expected {expected_port!r}", r)
    elif ha is None:
        # 4.7: Disabled — variables must not be present
        r = _exec(("printenv", "HOST_ACCESS_ADDRESS"))
        if r.return_code == 0 and r.stdout.strip():
            _add("host-access.address", False,
                 f"HOST_ACCESS_ADDRESS is set ({r.stdout.strip()!r})"
                 f" but host access is disabled", r)
        else:
            _add("host-access.address", True,
                 "HOST_ACCESS_ADDRESS is not set (disabled)", r)
        r = _exec(("printenv", "HOST_PROXY_PORT"))
        if r.return_code == 0 and r.stdout.strip():
            _add("host-access.proxy-port", False,
                 f"HOST_PROXY_PORT is set ({r.stdout.strip()!r})"
                 f" but host access is disabled", r)
        else:
            _add("host-access.proxy-port", True,
                 "HOST_PROXY_PORT is not set (disabled)", r)

    # ── corporate-trust.mount ──────────────────────────────────────
    if request.corporate_trust_enabled:
        r = _exec(_MOUNTS_PROBE)
        opts_raw = ""
        for line in r.stdout.splitlines():
            if line.strip():
                opts_raw = line.strip()
        if r.return_code != 0 or not opts_raw:
            _add("corporate-trust.mount", False,
                 f"system CA bundle not mounted at {_SYSTEM_CA_BUNDLE}", r)
        else:
            opts = opts_raw.split(",")
            if "ro" in opts:
                rw = _exec(("test", "-w", _SYSTEM_CA_BUNDLE))
                if rw.return_code != 0:
                    _add("corporate-trust.mount", True,
                         "system CA bundle mounted read-only"
                         " (ro mount + not writable)", rw)
                else:
                    _add("corporate-trust.mount", False,
                         "mount claims ro but file is writable", rw)
            else:
                _add("corporate-trust.mount", False,
                     f"system CA bundle mount is not ro"
                     f" (options: {opts_raw})", r)
    else:
        r = _exec(_MOUNTS_PROBE)
        if r.return_code == 0 and r.stdout.strip():
            _add("corporate-trust.mount", False,
                 "system CA bundle mount present but corporate trust "
                 "disabled", r)
        else:
            _add("corporate-trust.mount", True,
                 "corporate trust disabled; no system CA bundle mount", r)

    # ── corporate-trust.environment ───────────────────────────────
    # When corporate trust is enabled, every in-scope launched container
    # must carry the closed five-variable client CA mapping.  Each
    # variable is reported on its own check so a single missing or
    # mismatched name is diagnosed independently of the mount result.
    # Disabled trust intentionally emits no constructor-defined client CA
    # expectations: values inherited from the base image are preserved
    # and are not reported as enabled policy.  Verification inspects only
    # these fixed in-container names and the fixed system path; it never
    # reads the host bundle source or certificate contents and makes no
    # TLS request or validity/connectivity claim.
    if request.corporate_trust_enabled:
        for name, expected in CLIENT_CA_ENVIRONMENT:
            r = _exec(("printenv", name))
            actual = r.stdout if r.return_code == 0 else ""
            # ``printenv`` terminates its value with exactly one newline.
            # Remove only that byte so leading/trailing whitespace or extra
            # embedded content is observed verbatim and fails the exact
            # fixed-value comparison instead of being silently normalized.
            if actual.endswith("\n"):
                actual = actual[:-1]
            if actual == expected:
                _add("corporate-trust.environment", True,
                     f"{name}={actual}", r)
            elif not actual:
                # Fail-closed diagnostic: name only the fixed variable and
                # the fixed expected path, never the observed value.
                _add("corporate-trust.environment", False,
                     f"{name} is not set (expected {expected})", r,
                     redact_output=True)
            else:
                _add("corporate-trust.environment", False,
                     f"{name} does not equal expected {expected}", r,
                     redact_output=True)

    # ── proxy.environment ──────────────────────────────────────────
    if request.proxy_url is not None:
        for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY",
                     "https_proxy", "ALL_PROXY", "all_proxy"):
            r = _exec(("printenv", name))
            actual = r.stdout.strip() if r.return_code == 0 else ""
            if actual == request.proxy_url:
                _add("proxy.environment", True, f"{name}={actual}", r)
            else:
                _add("proxy.environment", False,
                     f"{name}: expected {request.proxy_url!r}, got {actual!r}", r)
        for name in ("NO_PROXY", "no_proxy"):
            r = _exec(("printenv", name))
            actual = r.stdout.strip() if r.return_code == 0 else ""
            if request.proxy_no_proxy is not None:
                if actual == request.proxy_no_proxy:
                    _add("proxy.environment", True, f"{name}={actual}", r)
                else:
                    _add("proxy.environment", False,
                         f"{name}: expected {request.proxy_no_proxy!r}, "
                         f"got {actual!r}", r)
            elif actual:
                _add("proxy.environment", False,
                     f"{name} is set ({actual!r}) but no bypass list "
                     f"configured", r)
            else:
                _add("proxy.environment", True,
                     f"{name} unset (no bypass list)", r)
    else:
        for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY",
                     "https_proxy", "ALL_PROXY", "all_proxy",
                     "NO_PROXY", "no_proxy"):
            r = _exec(("printenv", name))
            if r.return_code == 0 and r.stdout.strip():
                _add("proxy.environment", False,
                     f"{name} is set ({r.stdout.strip()!r}) but proxy is "
                     f"disabled", r)
            else:
                _add("proxy.environment", True,
                     f"{name} is not set (disabled)", r)

    # ── forbidden.paths ──────────────────────────────────────────────
    forbidden = (
        "/run/pi-cli/docker-constructor.toml",
        "/run/pi-cli/docker-constructor.build.effective.toml",
    )
    for fp in forbidden:
        r = _exec(("test", "-f", fp))
        if r.return_code == 0:
            _add("forbidden.paths", False,
                 f"forbidden path present: {fp}", r)
        else:
            _add("forbidden.paths", True,
                 f"forbidden path absent: {fp}", r)

    # ── Assemble ─────────────────────────────────────────────────────
    all_ok = all(c.ok for c in checks) and len(errors) == 0
    return RuntimeVerificationResult(
        container=container,
        checks=tuple(checks),
        all_ok=all_ok,
        errors=tuple(errors),
    )

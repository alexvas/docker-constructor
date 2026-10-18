"""Docker host-gateway diagnosis and rootless-override management.

Detects Docker rootful/rootless mode, discovers LAN IPs, probes gateway
candidates through an ephemeral HTTP server, and manages rootless
port-forward overrides.  Every side-effecting operation accepts an
injectable ``_run``, ``_filesystem``, or factory argument so tests can
substitute fakes without Docker or systemd.

This module does **not** own argument parsing, user prompts, terminal
output, exit-code selection, inventory resolution, or build/run
orchestration.
"""

from __future__ import annotations

import enum
import http.server
import ipaddress
import os
import re
import shutil
import subprocess
from enum import Enum
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

from docker.filesystem.cleanup import CleanupFailures
from docker.filesystem.streams import OwnedStream

# ---------------------------------------------------------------------------
# Default filesystem locations
# ---------------------------------------------------------------------------

_DEFAULT_PROBE_IMAGE = "alpine:3.20"
_DEFAULT_OVERRIDE_SRC = (
    Path(__file__).resolve().parent / "rootless-docker.override.conf"
)
# Dest is resolved through Filesystem.home at call time so tests can
# inject a fake home directory without touching the real filesystem.
_OVERRIDE_DEST_REL = Path(".config/systemd/user/docker.service.d/override.conf")


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------


class DockerMode(enum.Enum):
    """Docker daemon operational mode."""

    ROOTFUL = "rootful"
    ROOTLESS = "rootless"


class OverrideState(enum.Enum):
    """Current state of the rootless-override destination file."""

    ABSENT = "absent"
    MATCHING = "matching"
    DIFFERENT = "different"


@dataclass(frozen=True)
class ProbeResult:
    """Result of probing a single gateway candidate."""

    candidate: str
    ok: bool
    resolved_ip: Optional[str]
    detail: str

    def __post_init__(self) -> None:
        if not self.candidate:
            raise ValueError("candidate must be non-empty")
        # detail may be empty for a clean success; truncate is caller's job


@dataclass(frozen=True)
class GatewayDiagnosis:
    """Structured result of a full gateway diagnosis run.

    Immutable: callers cannot mutate fields after construction.
    ``resolved_address`` resolves to the successful probe's resolved IP,
    falling back to the candidate string when the resolved IP is
    unavailable.  Returns ``None`` when no candidate succeeded.
    """

    mode: DockerMode
    probe_port: int
    probe_token: str
    lan_ip: Optional[str]
    probes: tuple[ProbeResult, ...]
    chosen_gateway: Optional[str]
    override_installed: bool
    override_needed: bool

    @property
    def rootless(self) -> bool:
        """Convenience accessor for callers migrating from ``bool``."""
        return self.mode is DockerMode.ROOTLESS

    @property
    def resolved_address(self) -> Optional[str]:
        """Concrete IP for the chosen gateway (if one succeeded)."""
        if self.chosen_gateway is None:
            return None
        for p in self.probes:
            if p.candidate == self.chosen_gateway and p.ok:
                return p.resolved_ip or p.candidate
        return None

    def chosen_probe(self) -> Optional[ProbeResult]:
        """Return the ``ProbeResult`` for the chosen gateway, if any."""
        if self.chosen_gateway is None:
            return None
        for p in self.probes:
            if p.candidate == self.chosen_gateway and p.ok:
                return p
        return None


@dataclass(frozen=True)
class RootlessOverridePlan:
    """Description of the current rootless-override state.

    ``installed`` is ``True`` when the destination file exists and its
    content matches the source template.
    """

    installed: bool = False
    needed: bool = True
    src: Path = Path("/")
    dest: Path = Path("/")
    rootless: bool = True
    state: OverrideState = OverrideState.ABSENT
    filesystem_ops: tuple["FilesystemOperation", ...] = ()
    service_ops: tuple["ServiceOperation", ...] = ()


@dataclass(frozen=True)
class FilesystemOperation:
    """A single filesystem step in an override plan."""

    kind: str
    path: Path
    source_path: Optional[Path] = None


@dataclass(frozen=True)
class ServiceOperation:
    """A single service-control step in an override plan."""

    kind: str
    unit: Optional[str] = None


# ---------------------------------------------------------------------------
# Domain errors
# ---------------------------------------------------------------------------


class DockerDetectionError(RuntimeError):
    """Docker daemon could not be reached or its mode could not be
    determined."""


@dataclass(frozen=True)
class OverrideFailure:
    """Structured result when ``apply_rootless_override`` cannot complete.

    ``persistence_applied`` is ``True`` when the override file was
    successfully written before a subsequent service operation failed.
    """

    operation: str
    path_or_command: str
    detail: str
    persistence_applied: bool


@dataclass(frozen=True)
class PersistenceResult:
    """Result of persisting a host-access address to the local companion."""

    path: Path
    address: str
    written: bool
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Abstract side-effect boundaries (injectable for testing)
# ---------------------------------------------------------------------------


class Filesystem:
    """Filesystem operations needed by the networking module.

    Subclass and override methods to create an in-memory fake for
    tests that must not touch the real disk.
    """

    def is_file(self, path: Path) -> bool:
        return path.is_file()

    def is_symlink(self, path: Path) -> bool:
        return path.is_symlink()

    def read_text(self, path: Path, encoding: str = "utf-8") -> str:
        return path.read_text(encoding=encoding)

    def write_text(self, path: Path, content: str, encoding: str = "utf-8") -> None:
        path.write_text(content, encoding=encoding)

    def mkdir(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)

    def copy(self, src: Path, dest: Path) -> None:
        shutil.copy2(src, dest)

    def rename(self, src: Path, dest: Path) -> None:
        src.rename(dest)

    def delete(self, path: Path) -> None:
        path.unlink(missing_ok=True)

    @property
    def home(self) -> Path:
        return Path.home()


class ServiceController:
    """systemd user-service control.

    Subclass and override methods to create an in-memory fake for
    tests that must not invoke real systemctl.
    """

    def __init__(self, *, _runner: Optional[ProcessRunner] = None) -> None:
        self._runner = _runner if _runner is not None else ProcessRunner()

    def daemon_reload(self) -> None:
        proc = self._runner.run(["systemctl", "--user", "daemon-reload"])
        if proc.return_code != 0:
            raise RuntimeError(
                f"daemon-reload failed (rc={proc.return_code}): {proc.stderr[:200]}"
            )

    def restart(self, unit: str) -> None:
        proc = self._runner.run(["systemctl", "--user", "restart", unit])
        if proc.return_code != 0:
            raise RuntimeError(
                f"restart {unit} failed (rc={proc.return_code}): {proc.stderr[:200]}"
            )


class SystemClock:
    """Clock / sleep operations.

    Subclass and override for tests that must control time.
    """

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def timestamp(self) -> float:
        return time.time()


# ---------------------------------------------------------------------------
# Process-runner boundary (requirement 24)
# ---------------------------------------------------------------------------


class BuildOutputPolicy(str, Enum):
    """How a Docker build exposes its output.

    ``CAPTURED`` is the backwards-compatible default; ``STREAMED`` lets
    Docker inherit the constructor's stdout and stderr.
    """

    STREAMED = "streamed"
    CAPTURED = "captured"


@dataclass(frozen=True)
class ProcessResult:
    """Explicit fake-process outcome — never wraps a live ``subprocess``.

    Tests construct these directly; the production ``ProcessRunner``
    returns them so every caller (detection, probing, LAN-IP) can be
    tested with determistic outcomes.
    """

    argv: tuple[str, ...]
    return_code: int
    stdout: str
    stderr: str
    output_policy: BuildOutputPolicy = BuildOutputPolicy.CAPTURED


class ProcessRunner:
    """Injectable process-execution boundary.

    Subclass and override ``run`` for in-memory fakes that return
    ``ProcessResult`` instead of invoking a real subprocess.
    """

    def run(self, argv: list[str]) -> ProcessResult:
        """Execute *argv* and return a structured result."""
        proc = subprocess.run(
            argv,
            text=True,
            capture_output=True,
            check=False,
        )
        return ProcessResult(
            argv=tuple(argv),
            return_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )


# ---------------------------------------------------------------------------
# Host probe server
# ---------------------------------------------------------------------------


class _ProbeListener:
    """Translate server_close for the sole filesystem stream owner."""

    def __init__(self) -> None:
        self.server: Optional[http.server.ThreadingHTTPServer] = None

    def close(self) -> None:
        if self.server is not None:
            self.server.server_close()


class HostProbeServer:
    """Ephemeral HTTP server on ``0.0.0.0:RAND`` returning a fixed token.

    The server is created once per diagnosis run and torn down in a
    ``finally`` block so the port is released regardless of probe
    outcome.
    """

    def __init__(self, *, _clock: Optional[SystemClock] = None) -> None:
        clock = _clock or SystemClock()
        self.token = f"OK_{int(clock.timestamp())}"
        self.port = 0
        self._httpd: Optional[http.server.ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._thread_started = False
        self._listener_owner: Optional[OwnedStream] = None

    def start(self) -> int:
        if self._listener_owner is not None:
            raise RuntimeError("host probe server is already started")
        token = self.token

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(token.encode())

            def log_message(self, format: str, *args: object) -> None:
                pass  # suppress access-log noise

        listener = _ProbeListener()
        self._listener_owner = OwnedStream(listener, label="host probe listener")
        try:
            listener.server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), _Handler)
            self._httpd = listener.server
            self.port = int(self._httpd.server_address[1])
            self._thread = threading.Thread(
                target=self._httpd.serve_forever, daemon=True
            )
            self._thread.start()
            self._thread_started = True
            return self.port
        except BaseException as primary:
            failures = CleanupFailures(primary)
            failures.run(self.stop, ordinary=(OSError,))
            result = failures.complete()
            if result is not None:
                raise result
            raise

    def stop(self) -> None:
        server, thread, started = self._httpd, self._thread, self._thread_started
        owner = self._listener_owner
        # Detach before any fallible action: repeated cleanup cannot retry close.
        self._httpd = None
        self._thread = None
        self._thread_started = False
        self._listener_owner = None
        failures = CleanupFailures(None)
        if server is not None and started:
            failures.run(server.shutdown, ordinary=(OSError,))
        if owner is not None:
            failures.run(owner.close, ordinary=(OSError,))
        if thread is not None and started:
            failures.run(lambda: thread.join(timeout=5), ordinary=())
        result = failures.complete()
        if result is not None:
            raise result


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def detect_docker_mode(
    *,
    _runner: Optional[ProcessRunner] = None,
) -> DockerMode:
    """Return the Docker daemon operational mode.

    Raises ``DockerDetectionError`` when ``docker info`` fails or the
    ``docker`` binary is not found.
    """
    runner = _runner if _runner is not None else ProcessRunner()
    try:
        proc = runner.run(["docker", "info"])
    except FileNotFoundError:
        raise DockerDetectionError("docker not found on PATH")
    if proc.return_code != 0:
        detail = (
            proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.return_code}"
        )
        raise DockerDetectionError(detail)
    if "rootless" in proc.stdout.lower():
        return DockerMode.ROOTLESS
    return DockerMode.ROOTFUL


def detect_lan_ip(
    *,
    _runner: Optional[ProcessRunner] = None,
) -> Optional[str]:
    """Discover a plausible LAN IP via ``hostname -I`` or ``ip route``.

    Returns ``None`` when no address can be determined.  Process
    failures are handled gracefully — this function never raises.
    """
    runner = _runner if _runner is not None else ProcessRunner()
    try:
        proc = runner.run(["hostname", "-I"])
        out = proc.stdout.strip()
        if out:
            return out.split()[0]
    except subprocess.CalledProcessError, FileNotFoundError, OSError:
        pass
    try:
        proc = runner.run(["ip", "-4", "route", "show", "default"])
        m = re.search(r"\bsrc\s+(\d+\.\d+\.\d+\.\d+)\b", proc.stdout)
        if m:
            return m.group(1)
    except FileNotFoundError, OSError:
        pass
    return None


# ---------------------------------------------------------------------------
# Rootless override
# ---------------------------------------------------------------------------


def plan_rootless_override(
    *,
    _mode: Optional[DockerMode] = None,
    _fs: Optional[Filesystem] = None,
    _override_src: Optional[Path] = None,
    _override_dest: Optional[Path] = None,
) -> RootlessOverridePlan:
    """Return a plan describing the current rootless-override state.

    The plan does **not** apply or prompt; the caller decides whether to
    act on it.  When ``_mode`` is ``None`` the plan assumes rootless
    Docker; pass ``DockerMode.ROOTFUL`` for rootful daemons.
    """
    mode = _mode or DockerMode.ROOTLESS
    fs = _fs or Filesystem()
    src = _override_src if _override_src is not None else _DEFAULT_OVERRIDE_SRC
    dest = (
        _override_dest if _override_dest is not None else fs.home / _OVERRIDE_DEST_REL
    )

    state: OverrideState
    filesystem_ops: tuple[FilesystemOperation, ...]
    service_ops: tuple[ServiceOperation, ...]

    if mode is DockerMode.ROOTFUL:
        state = OverrideState.MATCHING
        filesystem_ops = ()
        service_ops = ()
        installed = True
    elif not fs.is_file(dest):
        state = OverrideState.ABSENT
        filesystem_ops = (
            FilesystemOperation(kind="mkdir", path=dest.parent),
            FilesystemOperation(kind="copy", path=dest, source_path=src),
        )
        service_ops = (
            ServiceOperation(kind="daemon_reload", unit=None),
            ServiceOperation(kind="restart", unit="docker.service"),
        )
        installed = False
    else:
        try:
            content_match = fs.read_text(dest) == fs.read_text(src)
        except OSError:
            content_match = False
        if content_match:
            state = OverrideState.MATCHING
            filesystem_ops = ()
            service_ops = ()
            installed = True
        else:
            state = OverrideState.DIFFERENT
            filesystem_ops = (
                FilesystemOperation(kind="copy", path=dest, source_path=src),
            )
            service_ops = (
                ServiceOperation(kind="daemon_reload", unit=None),
                ServiceOperation(kind="restart", unit="docker.service"),
            )
            installed = False

    return RootlessOverridePlan(
        installed=installed,
        needed=(state is not OverrideState.MATCHING),
        src=src,
        dest=dest,
        rootless=(mode is DockerMode.ROOTLESS),
        state=state,
        filesystem_ops=filesystem_ops,
        service_ops=service_ops,
    )


def apply_rootless_override(
    plan: RootlessOverridePlan,
    *,
    consent: bool,
    _fs: Optional[Filesystem] = None,
    _svc: Optional[ServiceController] = None,
    _clock: Optional[SystemClock] = None,
) -> Optional[OverrideFailure]:
    """Install the rootless port-forward override and restart Docker.

    ``consent`` must be ``True`` for the function to perform any
    filesystem or service operation.  When ``consent`` is ``False``
    this function returns ``None`` immediately — no override is
    installed, no systemctl commands are issued.

    Returns ``None`` on success or an ``OverrideFailure`` describing
    what went wrong.  This function never raises; errors in each
    individual operation are caught and returned as structured
    failures.
    """
    if not consent:
        return None
    if not plan.needed:
        return None
    fs = _fs or Filesystem()
    svc = _svc or ServiceController()
    clock = _clock or SystemClock()

    # --- Source check ---
    if not fs.is_file(plan.src):
        return OverrideFailure(
            operation="copy override",
            path_or_command=str(plan.src),
            detail="source file not found",
            persistence_applied=False,
        )

    # --- mkdir ---
    try:
        fs.mkdir(plan.dest.parent)
    except OSError as exc:
        return OverrideFailure(
            operation="mkdir",
            path_or_command=str(plan.dest.parent),
            detail=str(exc),
            persistence_applied=False,
        )

    # --- copy ---
    try:
        fs.copy(plan.src, plan.dest)
    except OSError as exc:
        return OverrideFailure(
            operation="copy",
            path_or_command=str(plan.dest),
            detail=str(exc),
            persistence_applied=False,  # mkdir may have succeeded but
            # no override content was written
        )

    persistence_applied = True

    # --- daemon-reload ---
    try:
        svc.daemon_reload()
    except (OSError, RuntimeError) as exc:
        return OverrideFailure(
            operation="daemon-reload",
            path_or_command="systemctl --user daemon-reload",
            detail=str(exc),
            persistence_applied=True,
        )

    # --- restart ---
    try:
        svc.restart("docker.service")
    except (OSError, RuntimeError) as exc:
        return OverrideFailure(
            operation="restart",
            path_or_command="systemctl --user restart docker.service",
            detail=str(exc),
            persistence_applied=True,
        )

    clock.sleep(3)
    return None


# ---------------------------------------------------------------------------
# Gateway selection and probing
# ---------------------------------------------------------------------------


def candidate_gateways(
    mode: DockerMode,
    lan_ip: Optional[str],
) -> tuple[str, ...]:
    """Return the ordered list of gateway candidates to probe.

    Rootless: ``10.0.2.2`` first (VirtualBox default), then LAN IP,
    then ``host-gateway``.
    Rootful: ``host-gateway`` first, then LAN IP.
    """
    if mode is DockerMode.ROOTLESS:
        order: list[str] = ["10.0.2.2"]
        if lan_ip and lan_ip not in order:
            order.append(lan_ip)
        if "host-gateway" not in order:
            order.append("host-gateway")
        return tuple(order)
    order = ["host-gateway"]
    if lan_ip and lan_ip not in order:
        order.append(lan_ip)
    return tuple(order)


_PROBE_SCRIPT = (
    "getent hosts host.docker.internal | awk '{print $1}' | head -1 | "
    "while read ip; do echo RESOLVED_IP=$ip; done; "
    "body=$(wget -qO- --timeout=_TIMEOUT_ http://host.docker.internal:_PORT_ 2>/dev/null) && "
    'echo "$body" | grep -qx "_TOKEN_" && echo PROBE_OK'
)

# Tokens must be alphanumeric + dots/hyphens/underscores only — no quotes,
# semicolons, newlines, or shell metacharacters that could escape the
# double-quoted grep expression in _PROBE_SCRIPT.
_UNSAFE_TOKEN_CHARS = re.compile(r"[^A-Za-z0-9._-]")

# PROBE_OK must appear as a standalone line (not embedded in other text).
_PROBE_OK_LINE = re.compile(r"(?m)^PROBE_OK$")

# Gateway values must be "host-gateway" or a valid IPv4/IPv6 address.


def probe_gateway(
    candidate: str,
    probe_port: int,
    token: str,
    *,
    probe_image: str = _DEFAULT_PROBE_IMAGE,
    probe_timeout: int = 3,
    _runner: Optional[ProcessRunner] = None,
) -> ProbeResult:
    """Probe a single gateway candidate by running a throwaway container.

    The container resolves ``host.docker.internal`` via ``--add-host``,
    fetches the probe token from the host ephemeral server, and reports
    the resolved IP.
    """
    # Validate inputs before any interpolation (requirement 33)
    if not candidate:
        return ProbeResult(candidate, False, None, "empty candidate")
    if not isinstance(probe_port, int) or probe_port < 1 or probe_port > 65535:
        return ProbeResult(candidate, False, None, f"invalid probe port: {probe_port}")
    if not token or not token.strip():
        return ProbeResult(candidate, False, None, "empty probe token")
    if _UNSAFE_TOKEN_CHARS.search(token):
        return ProbeResult(
            candidate, False, None, f"token contains unsafe characters: {token!r}"
        )
    # probe_timeout must be a plain int (not bool), 1–300 seconds.
    if isinstance(probe_timeout, bool) or not isinstance(probe_timeout, int):
        return ProbeResult(
            candidate,
            False,
            None,
            f"probe_timeout must be int, got {type(probe_timeout).__name__}",
        )
    if probe_timeout < 1 or probe_timeout > 300:
        return ProbeResult(
            candidate, False, None, f"probe_timeout out of range: {probe_timeout}"
        )

    runner = _runner if _runner is not None else ProcessRunner()
    add_host = f"host.docker.internal:{candidate}"
    script = (
        _PROBE_SCRIPT.replace("_PORT_", str(probe_port))
        .replace("_TOKEN_", token)
        .replace("_TIMEOUT_", str(probe_timeout))
    )
    cmd = [
        "docker",
        "run",
        "--rm",
        "--add-host",
        add_host,
        probe_image,
        "sh",
        "-c",
        script,
    ]
    try:
        proc = runner.run(cmd)
        out = (proc.stdout or "") + (proc.stderr or "")
        # Requirement 31: Docker must exit successfully *and* output must
        # contain PROBE_OK as a standalone line — not embedded in other
        # text (e.g. NOT_PROBE_OK) or quoted in an error message.
        ok = proc.return_code == 0 and bool(_PROBE_OK_LINE.search(out))
        resolved: Optional[str] = None
        m = re.search(r"RESOLVED_IP=(\S+)", out)
        if m:
            resolved = m.group(1)
        # Build detail: bounded output, prefixed with exit code on failure.
        body = out.strip()
        if body:
            base = body[-200:]
            detail = (
                f"[exit {proc.return_code}] {base}" if proc.return_code != 0 else base
            )
        else:
            detail = f"exit {proc.return_code}"
        return ProbeResult(candidate, ok, resolved, detail)
    except Exception as exc:
        return ProbeResult(candidate, False, None, str(exc)[:200])


def _choose_gateway(probes: tuple[ProbeResult, ...]) -> Optional[str]:
    """Pick the first successful candidate."""
    for probe in probes:
        if probe.ok:
            return probe.candidate
    return None


# ---------------------------------------------------------------------------
# Full diagnosis orchestration
# ---------------------------------------------------------------------------


def diagnose_gateway(
    *,
    probe_image: str = _DEFAULT_PROBE_IMAGE,
    probe_timeout: int = 3,
    _runner: Optional[ProcessRunner] = None,
    _host_probe_factory: Optional[Callable[[], HostProbeServer]] = None,
    _fs: Optional[Filesystem] = None,
    _override_src: Optional[Path] = None,
    _override_dest: Optional[Path] = None,
) -> GatewayDiagnosis:
    """Run full gateway diagnosis: detection, probing, override inspection.

    Returns a structured ``GatewayDiagnosis``.  Callers present the
    result to the user and decide whether to apply overrides.  This
    function never installs an override — it only reports whether one
    is needed.
    """
    runner = _runner if _runner is not None else ProcessRunner()
    host_factory = _host_probe_factory or HostProbeServer

    mode = detect_docker_mode(_runner=runner)
    lan_ip = detect_lan_ip(_runner=runner)
    plan = plan_rootless_override(
        _mode=mode,
        _fs=_fs,
        _override_src=_override_src,
        _override_dest=_override_dest,
    )

    server = host_factory()
    try:
        probe_port = server.start()
        probes: list[ProbeResult] = []
        for cand in candidate_gateways(mode, lan_ip):
            probes.append(
                probe_gateway(
                    cand,
                    probe_port,
                    server.token,
                    probe_image=probe_image,
                    probe_timeout=probe_timeout,
                    _runner=runner,
                )
            )

        chosen = _choose_gateway(tuple(probes))
        return GatewayDiagnosis(
            mode=mode,
            probe_port=probe_port,
            probe_token=server.token,
            lan_ip=lan_ip,
            probes=tuple(probes),
            chosen_gateway=chosen,
            override_installed=plan.installed,
            override_needed=(mode is DockerMode.ROOTLESS) and plan.needed,
        )
    finally:
        server.stop()


# ════════════════════════════════════════════════════════════════════
# Operational persistence (deprecated — preserved for
# _persist_host_access_address in build_orchestration.py)
# ════════════════════════════════════════════════════════════════════


# Backward-compatible alias — prefer ``plan_rootless_override``.
inspect_rootless_override = plan_rootless_override

"""Reproducible Phase 6 npm ``loglevel=http`` research harness.

The harness deliberately runs npm through stdout/stderr pipes in the current
reviewed Node container.  Its registry is a local TLS CONNECT fixture: npm
still sees the canonical registry host, but the fixture never reaches the
public network.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from docker.versioning.diagnostic_projection import sanitize_diagnostic_text
from tests.phase6_acceptance_harness import LocalRegistryProxy, make_lock, make_package

NPM_VERSION = "11.16.0"
NODE_VERSION = "24.18.0"
NPM_COMMAND = (
    "npm", "ci", "--ignore-scripts", "--no-bin-links", "--no-audit", "--no-fund",
)
Scenario = Literal["cache-miss", "cache-hit", "retry", "timeout", "high-volume"]


@dataclass(frozen=True)
class Observation:
    scenario: Scenario
    loglevel: str
    returncode: int
    elapsed_seconds: float
    first_observation_seconds: float | None
    exit_observation_seconds: float
    confirmed_live_output: bool
    cache_initially_empty: bool
    cache_seeded: bool
    cache_id: str
    stdout: str
    stderr: str
    projected_stdout: str
    projected_stderr: str
    hostnames: tuple[str, ...]
    line_count: int
    projected_line_count: int
    aggregated_lines: tuple[str, ...]


def _aggregate_projected_lines(text: str) -> tuple[str, ...]:
    """Apply Phase 7's consecutive-identical coalescing with O(1) state."""
    rendered: list[str] = []
    pending: str | None = None
    occurrences = 0

    def flush() -> None:
        nonlocal pending, occurrences
        if pending is not None:
            rendered.append(
                pending if occurrences == 1 else f"{pending} (repeated {occurrences} times)"
            )
        pending = None; occurrences = 0

    for line in text.splitlines():
        if line == pending:
            occurrences += 1
        else:
            flush()
            pending = line; occurrences = 1
    flush()
    return tuple(rendered)


def _read_fd(
    fd: int, chunks: list[bytes], first: list[float], live: list[bool], lock: threading.Lock,
    process: subprocess.Popen[bytes], gate: threading.Event | None = None,
) -> None:
    """Read unbuffered bytes and confirm liveness at the read boundary."""
    if gate is not None:
        gate.wait()
    while data := os.read(fd, 4096):
        with lock:
            if not first:
                first.append(time.monotonic())
                live.append(process.poll() is None)
        chunks.append(data)


def _run(
    command: tuple[str, ...], *, cwd: Path, env: dict[str, str], scenario: Scenario,
    cache_initially_empty: bool, cache_seeded: bool, cache_id: str,
    timeout_seconds: float = 15, reader_gate: threading.Event | None = None,
    loglevel: str | None = None,
) -> Observation:
    started = time.monotonic()
    process = subprocess.Popen(command, cwd=cwd, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdout is not None and process.stderr is not None
    out: list[bytes] = []; err: list[bytes] = []; first: list[float] = []; live: list[bool] = []
    lock = threading.Lock()
    readers = [threading.Thread(
        target=_read_fd, args=(pipe.fileno(), target, first, live, lock, process, reader_gate),
    ) for pipe, target in ((process.stdout, out), (process.stderr, err))]
    for reader in readers: reader.start()
    try:
        returncode = process.wait(timeout=timeout_seconds)
        exit_observed = time.monotonic()
    except BaseException:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait(timeout=2)
        raise
    finally:
        # Reaping/closing guarantees readers cannot survive a timeout or error.
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=2)
        for reader in readers:
            reader.join(2)
            if reader.is_alive():
                process.stdout.close(); process.stderr.close()
                raise RuntimeError("research pipe reader did not terminate")
        process.stdout.close()
        process.stderr.close()
    stdout = b"".join(out).decode("utf-8", "replace")
    stderr = b"".join(err).decode("utf-8", "replace")
    projected_stdout, stdout_hosts = sanitize_diagnostic_text(stdout, ())
    projected_stderr, stderr_hosts = sanitize_diagnostic_text(stderr, ())
    projected = projected_stdout + projected_stderr
    aggregated = _aggregate_projected_lines(projected)
    return Observation(
        scenario=scenario, loglevel=loglevel or command[-1].split("=", 1)[1], returncode=returncode,
        elapsed_seconds=round(time.monotonic() - started, 3),
        first_observation_seconds=round(first[0] - started, 3) if first else None,
        exit_observation_seconds=round(exit_observed - started, 3),
        confirmed_live_output=bool(first and live and live[0]),
        cache_initially_empty=cache_initially_empty,
        cache_seeded=cache_seeded,
        cache_id=cache_id,
        stdout=stdout, stderr=stderr, projected_stdout=projected_stdout,
        projected_stderr=projected_stderr,
        hostnames=tuple(sorted(set(stdout_hosts + stderr_hosts))),
        line_count=len((stdout + stderr).splitlines()),
        projected_line_count=len(projected.splitlines()),
        aggregated_lines=aggregated,
    )


def _research_env(root: Path, registry: LocalRegistryProxy, cache: Path, scenario: Scenario) -> dict[str, str]:
    """Build an npm environment with no inherited proxy or npm configuration."""
    home = root / "home"; home.mkdir(exist_ok=True)
    userconfig = root / "npmrc"; globalconfig = root / "global-npmrc"
    userconfig.write_text(""); globalconfig.write_text("")
    return {
        "PATH": os.environ["PATH"], "HOME": str(home), "LANG": "C.UTF-8",
        "HTTPS_PROXY": f"http://127.0.0.1:{registry.port}",
        "https_proxy": f"http://127.0.0.1:{registry.port}",
        "npm_config_registry": "https://registry.npmjs.org/",
        "npm_config_cafile": str(registry.ca), "npm_config_cache": str(cache),
        "npm_config_userconfig": str(userconfig), "npm_config_globalconfig": str(globalconfig),
        "npm_config_fetch_retries": "24" if scenario == "high-volume" else "1",
        "npm_config_fetch_retry_factor": "1", "npm_config_fetch_retry_mintimeout": "10",
        "npm_config_fetch_retry_maxtimeout": "10", "npm_config_fetch_timeout": "200",
    }


def run_research() -> tuple[Observation, ...]:
    """Run baseline and HTTP logging experiments for all controlled cases."""
    if subprocess.run(("node", "--version"), capture_output=True, text=True).stdout.strip() != f"v{NODE_VERSION}":
        raise RuntimeError("research must run in the reviewed Node image")
    if subprocess.run(("npm", "--version"), capture_output=True, text=True).stdout.strip() != NPM_VERSION:
        raise RuntimeError("research must run with npm 11.16.0")

    observations: list[Observation] = []
    with tempfile.TemporaryDirectory(prefix="npm-http-research-") as temporary:
        root = Path(temporary)
        package = make_package("npm-http-research-fixture")
        (root / "registry").mkdir()
        with LocalRegistryProxy(package, root / "registry") as registry:
            for scenario in ("cache-miss", "cache-hit", "retry", "timeout", "high-volume"):
                for loglevel in ("notice", "http"):
                    # No measured run shares a project or cache with another run.
                    cache = root / f"cache-{scenario}-{loglevel}"
                    project = root / f"project-{scenario}-{loglevel}"
                    project.mkdir()
                    (project / "package-lock.json").write_bytes(make_lock(package, f"research-{scenario}"))
                    (project / "package.json").write_text(json.dumps({
                        "name": f"research-{scenario}", "version": "1.0.0",
                        "dependencies": {package.name: package.version},
                    }))
                    env = _research_env(root, registry, cache, scenario)
                    cache_initially_empty = not cache.exists()
                    cache_seeded = scenario == "cache-hit"
                    if cache_seeded:
                        registry.controller.select("normal")
                        seeded = _run(NPM_COMMAND + ("--loglevel=notice",), cwd=project, env=env,
                                      scenario=scenario, cache_initially_empty=True, cache_seeded=False,
                                      cache_id=cache.name)
                        if seeded.returncode:
                            raise RuntimeError("could not seed controlled npm cache")
                        shutil.rmtree(project / "node_modules")
                    registry.controller.select({"cache-miss": "normal", "cache-hit": "normal", "retry": "large-error", "timeout": "pause", "high-volume": "large-error"}[scenario])
                    observations.append(_run(NPM_COMMAND + (f"--loglevel={loglevel}",), cwd=project,
                                             env=env, scenario=scenario,
                                             cache_initially_empty=cache_initially_empty,
                                             cache_seeded=cache_seeded, cache_id=cache.name))
            if registry.controller.requests == 0:
                raise RuntimeError("research registry received no requests")
    return tuple(observations)


def markdown_measurement_rows(observations: tuple[Observation, ...]) -> str:
    """Format measured observations without executing the harness."""
    return "\n".join(
        f"| {item.scenario} | {item.loglevel} | {item.first_observation_seconds:.3f} | "
        f"{item.exit_observation_seconds:.3f} | {item.line_count} | "
        f"{item.projected_line_count} | {item.returncode} |"
        for item in observations
    )


def report_json(observations: tuple[Observation, ...]) -> str:
    return json.dumps([asdict(item) for item in observations], indent=2, sort_keys=True) + "\n"

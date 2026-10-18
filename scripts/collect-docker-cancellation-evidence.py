#!/usr/bin/env python3
"""Collect real Docker protocol evidence; this is NOT the planned wrapper.

Example (pre-pull the image separately):
  docker pull busybox:1.36
  python3 scripts/collect-docker-cancellation-evidence.py \
      --output /tmp/docker-cancellation-evidence --image busybox:1.36

Uses the current Docker context. Creates/removes only uniquely labeled test
containers. The output directory must not exist. Full command output is local
host evidence: review it before sharing. No daemon access means failure, not a
simulated pass. Cleanup failure is recorded and makes the exit status nonzero.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Direct execution puts scripts/, not the repository root, on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from docker.filesystem.cleanup import CleanupFailures
from docker.filesystem.descriptors import OwnedDescriptor
from docker.filesystem.operations import PosixDescriptorOps


def _finish(failures, primary):
    ordinary = failures.complete()
    if primary is not None:
        raise primary
    if ordinary is not None:
        raise ordinary


def _with_owned(paths, action, *, ops=None):
    ops = PosixDescriptorOps() if ops is None else ops
    # Prepare all labels/arguments before any acquisition.
    prepared = [
        (str(path), flags | os.O_CLOEXEC | os.O_NOFOLLOW, f"evidence file {path}")
        for path, flags in paths
    ]
    owners = [None] * len(prepared)
    primary = None
    result = None
    try:
        for index, (path, flags, label) in enumerate(prepared):
            owners[index] = OwnedDescriptor(
                ops, ops.openat(None, path, flags, 0o600), label=label
            )
        result = action(owners)
    except BaseException as error:
        primary = error
    failures = CleanupFailures(primary)
    for owner in owners:
        if owner is not None:
            failures.run(owner.close, ordinary=(OSError,))
    _finish(failures, primary)
    return result


def _read_text(path, *, encoding="utf-8", errors="strict", ops=None):
    def read(owners):
        chunks = []
        while chunk := os.read(owners[0].fd, 65536):
            chunks.append(chunk)
        return b"".join(chunks).decode(encoding, errors)

    return _with_owned([(path, os.O_RDONLY)], read, ops=ops)


def _write_text(path, text, *, ops=None):
    payload = text.encode("utf-8")

    def write(owners):
        remaining = memoryview(payload)
        while remaining:
            count = os.write(owners[0].fd, remaining)
            if count <= 0:
                raise OSError("evidence write made no progress")
            remaining = remaining[count:]

    _with_owned([(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)], write, ops=ops)


MANAGED = "io.docker-constructor.managed"
PID = "io.docker-constructor.controller-pid"
OPERATION = "io.docker-constructor.operation-id"


def valid_id(value: str) -> bool:
    return re.fullmatch(r"[0-9a-f]{64}", value) is not None


def next_delay(base: float, remaining: float, jitter: float) -> float:
    if not 0.9 <= jitter <= 1.1:
        raise ValueError("jitter must be in [0.9, 1.1]")
    return max(0.0, min(base * jitter, remaining))


def filters(pid: int, operation: str) -> list[str]:
    return [
        "--filter",
        f"label={MANAGED}=true",
        "--filter",
        f"label={PID}={pid}",
        "--filter",
        f"label={OPERATION}={operation}",
    ]


class EvidenceFailure(Exception):
    """A failed protocol assertion, distinct from an unexpected cleanup defect."""


class Evidence:
    def __init__(self, root: Path, image: str, timeout: float, docker: str):
        self.root = root
        self.image = image
        self.timeout = timeout
        self.docker = docker
        self.pid = os.getpid()
        self.token = uuid.uuid4().hex
        self.operations: list[str] = []
        self.commands: list[dict] = []
        self.checks: list[dict] = []
        self.lock = threading.Lock()
        self.threads = ThreadPoolExecutor(max_workers=2)
        self.cleanup_failures = []
        self._cleanup_started = False

    def operation(self, name: str) -> str:
        value = f"{self.token}-{name}"
        self.operations.append(value)
        return value

    def labels(self, operation: str) -> list[str]:
        return [
            "--label",
            f"{MANAGED}=true",
            "--label",
            f"{PID}={self.pid}",
            "--label",
            f"{OPERATION}={operation}",
        ]

    def run(self, *args: str) -> dict:
        with self.lock:
            index = len(self.commands)
            record = {"index": index, "argv": [self.docker, *args]}
            self.commands.append(record)
        out = self.root / f"{index:03d}.stdout"
        err = self.root / f"{index:03d}.stderr"
        start = time.monotonic()
        record.update(start_monotonic=start, stdout=out.name, stderr=err.name)
        primary = None
        try:
            completed = _with_owned(
                [
                    (out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC),
                    (err, os.O_WRONLY | os.O_CREAT | os.O_TRUNC),
                ],
                lambda owners: subprocess.run(
                    record["argv"],
                    stdout=owners[0].fd,
                    stderr=owners[1].fd,
                    timeout=self.timeout,
                    check=False,
                ),
            )
            record["returncode"] = completed.returncode
        except BaseException as error:
            primary = error
            record.update(returncode=None, error=repr(error))
        record["duration_seconds"] = time.monotonic() - start
        if primary is not None:
            record["secondary"] = [
                repr(error) for error in getattr(primary, "_transaction_secondary", [])
            ]
        failures = CleanupFailures(primary)
        failures.run(
            lambda: _write_text(
                self.root / f"{index:03d}.json", json.dumps(record, indent=2) + "\n"
            ),
            ordinary=(OSError,),
        )
        try:
            _finish(failures, primary)
        except BaseException as authoritative:
            # Expected command failures retain their identity and close diagnostics;
            # a defect/interruption from independent evidence publication still wins.
            if authoritative is not primary or not isinstance(
                primary, (OSError, subprocess.TimeoutExpired)
            ):
                raise
            record["secondary"] = [
                repr(error) for error in getattr(primary, "_transaction_secondary", [])
            ]
        return record

    def text(self, record: dict, channel: str = "stdout") -> str:
        return _read_text(self.root / record[channel], errors="replace").strip()

    def require(self, condition: bool, name: str) -> None:
        self.checks.append({"name": name, "passed": condition})
        if not condition:
            raise EvidenceFailure(name)

    def success(self, record: dict) -> None:
        self.require(record["returncode"] == 0, f"command {record['index']} succeeded")

    def create(self, operation: str) -> str:
        record = self.run(
            "create",
            "--pull=never",
            *self.labels(operation),
            self.image,
            "sleep",
            "30",
        )
        self.success(record)
        identity = self.text(record)
        self.require(valid_id(identity), "create returned a full container ID")
        return identity

    def absent(self, identity: str) -> None:
        record = self.run("inspect", identity)
        self.require(
            record["returncode"] not in (None, 0)
            and "no such" in self.text(record, "stderr").lower(),
            "exact container ID is absent (not a communication failure)",
        )

    def discover(self, operation: str) -> list[str]:
        record = self.run(
            "ps", "-a", "--no-trunc", "--quiet", *filters(self.pid, operation)
        )
        self.success(record)
        values = self.text(record).splitlines()
        self.require(
            all(valid_id(value) for value in values), "discovery returned only full IDs"
        )
        return values

    def probe(self, races: int) -> None:
        self.success(self.run("version"))
        self.success(self.run("context", "show"))
        self.success(self.run("image", "inspect", self.image))

        # A successful stop of a created container is not a start prohibition.
        identity = self.create(self.operation("stop-before-start"))
        self.success(self.run("stop", "--time", "1", identity))
        self.success(self.run("start", identity))
        running = self.run("inspect", "--format", "{{.State.Running}}", identity)
        self.success(running)
        self.require(
            self.text(running) == "true", "stop-before-start permits later start"
        )
        self.success(self.run("rm", "-f", identity))
        self.absent(identity)

        identity = self.create(self.operation("remove-before-start"))
        self.success(self.run("rm", "-f", identity))
        started = self.run("start", identity)
        self.require(
            started["returncode"] not in (None, 0)
            and "no such" in self.text(started, "stderr").lower(),
            "removed ID cannot subsequently start",
        )
        self.absent(identity)

        for number in range(races):
            identity = self.create(self.operation(f"race-{number}"))
            barrier = threading.Barrier(2)

            def action(command: str, target: str) -> dict:
                barrier.wait(timeout=self.timeout)
                return self.run(command, *(["-f"] if command == "rm" else []), target)

            start = self.threads.submit(action, "start", identity)
            remove = self.threads.submit(action, "rm", identity)
            start_result = start.result()
            self.success(remove.result())
            self.require(
                start_result["returncode"] == 0
                or (
                    start_result["returncode"] not in (None, 0)
                    and any(
                        message in self.text(start_result, "stderr").lower()
                        for message in ("no such", "marked for removal")
                    )
                ),
                "racing start succeeded or was rejected by removal",
            )
            self.absent(identity)
        # Races are sampled; neither outcome nor zero transient execution is promised.

        operation = self.operation("cidfile")
        cidfile = self.root / "container.cid"
        deadline = time.monotonic() + 10.0
        attached = self.threads.submit(
            self.run,
            "run",
            "--pull=never",
            "--rm",
            "--cidfile",
            str(cidfile),
            *self.labels(operation),
            self.image,
            "sleep",
            "30",
        )
        polls = []
        base = 0.010
        identity = ""
        while time.monotonic() < deadline:
            try:
                value = _read_text(cidfile, encoding="ascii").strip()
            except (FileNotFoundError, UnicodeError) as error:
                if getattr(error, "_transaction_secondary", []):
                    raise
                value = ""
            polls.append(
                {"monotonic": time.monotonic(), "complete_id": valid_id(value)}
            )
            if valid_id(value):
                identity = value
                break
            delay = next_delay(
                base, deadline - time.monotonic(), random.uniform(0.9, 1.1)
            )
            time.sleep(delay)
            base *= 2
        _write_text(self.root / "cid-polls.json", json.dumps(polls, indent=2) + "\n")
        primary = None
        try:
            self.require(
                valid_id(identity), "attached run published CID within 10 seconds"
            )
            self.success(self.run("stop", "--time", "1", identity))
            self.success(self.run("rm", "-f", identity))
            # Cancellation may cause nonzero run exit; preserve, do not reinterpret it.
        except BaseException as error:
            primary = error
        failures = CleanupFailures(primary)

        def finish_attached():
            attached_result = attached.result()
            self.require(
                attached_result["returncode"] is not None, "attached CLI finalized"
            )

        failures.run(finish_attached, ordinary=(OSError, EvidenceFailure))
        _finish(failures, primary)
        self.absent(identity)

        # Recovery with deliberately discarded create result; not a lost-RPC simulation.
        wanted = self.operation("recovery")
        other = self.operation("unrelated")
        target = self.create(wanted)
        unrelated = self.create(other)
        found = self.discover(wanted)
        self.require(
            target in found and unrelated not in found, "exact labels isolate recovery"
        )
        for identity in found:
            self.success(self.run("rm", "-f", identity))
        self.require(
            not self.discover(wanted), "scoped recovery is empty after removal"
        )
        self.success(self.run("inspect", unrelated))

    def cleanup(self) -> list[str]:
        if self._cleanup_started:
            return self.cleanup_failures
        self._cleanup_started = True
        failures = CleanupFailures(None)
        # Join command jobs before the final sweep so they cannot create later objects.
        failures.run(lambda: self.threads.shutdown(wait=True), ordinary=(OSError,))

        def clean_operation(operation):
            targets = self.discover(operation)
            releases = CleanupFailures(None)
            for identity in targets:
                releases.run(
                    lambda identity=identity: self.success(
                        self.run("rm", "-f", identity)
                    ),
                    ordinary=(OSError, EvidenceFailure),
                )
            releases.run(
                lambda: self.require(
                    not self.discover(operation), "final operation sweep is empty"
                ),
                ordinary=(OSError, EvidenceFailure),
            )
            _finish(releases, None)

        for operation in self.operations:
            failures.run(
                lambda operation=operation: clean_operation(operation),
                ordinary=(OSError, EvidenceFailure),
            )
        self.cleanup_failures = [
            repr(error)
            for error in (
                *failures.ordinary_failures,
                *failures.unexpected,
                *failures.interruptions,
            )
        ]
        _finish(failures, None)
        return self.cleanup_failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--image", default="busybox:1.36", help="preloaded image with sleep"
    )
    parser.add_argument("--docker", default="docker")
    parser.add_argument("--command-timeout", type=float, default=20.0)
    parser.add_argument("--races", type=int, default=5)
    args = parser.parse_args()
    if (
        not math.isfinite(args.command_timeout)
        or args.command_timeout <= 0
        or not 1 <= args.races <= 100
    ):
        parser.error("finite positive command timeout and 1..100 races required")
    root = args.output.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    evidence = Evidence(root, args.image, args.command_timeout, args.docker)
    primary = None
    try:
        evidence.probe(args.races)
    except BaseException as failure:
        primary = failure
    failures = CleanupFailures(primary)
    failures.run(evidence.cleanup, ordinary=(OSError, EvidenceFailure))
    try:
        _finish(failures, primary)
    except BaseException as failure:
        primary = failure
    status = (
        0 if primary is None else (130 if isinstance(primary, KeyboardInterrupt) else 1)
    )
    summary = {
        "exit_status": status,
        "error": repr(primary) if primary is not None else None,
        "cleanup_failures": evidence.cleanup_failures,
        "secondary": [
            repr(error) for error in getattr(primary, "_transaction_secondary", [])
        ],
        "controller_pid": evidence.pid,
        "token": evidence.token,
        "operations": evidence.operations,
        "checks": evidence.checks,
        "commands": sorted(evidence.commands, key=lambda command: command["index"]),
        "scope": "Docker protocol probes, not wrapper acceptance or daemon-create latency injection",
        "limitations": "A lost/ongoing daemon create may outlive the client; empty discovery alone cannot prove settlement. Race outcomes are sampled.",
    }
    publication = CleanupFailures(primary)
    publication.run(
        lambda: _write_text(
            root / "summary.json", json.dumps(summary, indent=2) + "\n"
        ),
        ordinary=(OSError,),
    )
    try:
        _finish(publication, primary)
    except BaseException as failure:
        status = 130 if isinstance(failure, KeyboardInterrupt) else 1
        if failure is not primary:
            print(f"Evidence publication failed: {failure!r}", file=sys.stderr)
    print(f"Evidence: {root / 'summary.json'}; exit status: {status}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())

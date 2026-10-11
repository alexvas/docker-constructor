"""Hermetic process and npm-registry fixtures for Phase 6 acceptance tests.

The registry fixture is an HTTPS CONNECT proxy.  Lockfiles keep their reviewed
``https://registry.npmjs.org/...`` URLs while the assembler receives a local,
credential-free proxy and a fixture CA.  The proxy never opens an upstream
socket, so a passing test cannot depend on the public npm registry.
"""
from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import io
import json
import os
import signal
import ssl
import subprocess
import tarfile
import threading
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FixturePackage:
    name: str
    version: str
    tarball: bytes

    @property
    def path(self) -> str:
        stem = self.name.rsplit("/", 1)[-1]
        return f"/{self.name}/-/{stem}-{self.version}.tgz"

    @property
    def integrity(self) -> str:
        digest = hashlib.sha512(self.tarball).digest()
        return "sha512-" + base64.b64encode(digest).decode("ascii")


def make_package(name: str = "phase6-fixture", version: str = "1.0.0") -> FixturePackage:
    """Create deterministic npm package tarball bytes without npm or network."""
    package_json = json.dumps(
        {"name": name, "version": version, "bin": {"pi": "index.js"}},
        sort_keys=True,
        separators=(",", ":"),
    ).encode() + b"\n"
    raw = io.BytesIO()
    with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as zipped:
        with tarfile.open(fileobj=zipped, mode="w") as archive:
            info = tarfile.TarInfo("package/package.json")
            info.size = len(package_json)
            info.mode = 0o644
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            archive.addfile(info, io.BytesIO(package_json))
            executable = b"#!/usr/bin/env node\nprocess.stdout.write('phase6-pi\\n')\n"
            info = tarfile.TarInfo("package/index.js")
            info.size = len(executable)
            info.mode = 0o755
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            archive.addfile(info, io.BytesIO(executable))
    return FixturePackage(name, version, raw.getvalue())


def make_lock(package: FixturePackage, consumer: str = "phase6-consumer") -> bytes:
    return json.dumps({
        "name": consumer, "version": "1.0.0", "lockfileVersion": 3,
        "requires": True,
        "packages": {
            "": {"name": consumer, "version": "1.0.0",
                 "dependencies": {package.name: package.version}},
            f"node_modules/{package.name}": {
                "version": package.version,
                "resolved": "https://registry.npmjs.org" + package.path,
                "integrity": package.integrity,
                "bin": {"pi": "index.js"},
            },
        },
    }, sort_keys=True).encode()


class FaultController:
    """Thread-safe deterministic response mode selected by an acceptance test."""
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self.mode = "normal"
        self.request_started = threading.Event()
        self.release = threading.Event()
        self.requests = 0

    def select(self, mode: str) -> None:
        if mode not in {"normal", "pause", "partial", "large-error"}:
            raise ValueError(mode)
        with self._condition:
            self.mode = mode
            self.request_started.clear()
            self.release.clear()


class LocalRegistryProxy:
    """Local-only CONNECT proxy serving one npm tarball and controllable faults."""
    def __init__(
        self, package: FixturePackage | tuple[FixturePackage, ...], root: Path,
    ):
        self.packages = package if isinstance(package, tuple) else (package,)
        if not self.packages:
            raise ValueError("at least one fixture package is required")
        self.package = self.packages[0]
        self.root = root
        self.controller = FaultController()
        self.ca = root / "phase6-ca.crt"
        self._ca_key = root / "phase6-ca.key"
        self._key = root / "phase6-registry.key"
        self._cert = root / "phase6-registry.crt"
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.Server | None = None
        self.port = 0

    def __enter__(self) -> "LocalRegistryProxy":
        self._make_certificate()
        ready = threading.Event()
        failure: list[BaseException] = []

        def run() -> None:
            try:
                asyncio.run(self._serve(ready))
            except BaseException as exc:  # surfaced to the owner thread
                failure.append(exc); ready.set()

        self._thread = threading.Thread(target=run, name="phase6-registry", daemon=True)
        self._thread.start(); ready.wait(10)
        if failure:
            raise RuntimeError("fixture registry failed to start") from failure[0]
        if not self.port:
            raise RuntimeError("fixture registry did not become ready")
        return self

    def proxy_url(self, host_access_address: str) -> str:
        """Return the credential-free URL visible from Docker containers."""
        if not host_access_address or host_access_address == "host-gateway":
            raise ValueError("a concrete diagnosed host-access address is required")
        return f"http://{host_access_address}:{self.port}"

    def __exit__(self, *_: object) -> None:
        # This is a threading.Event, not an asyncio.Event. Release paused
        # response workers directly before requesting server shutdown: closing
        # the server can finish asyncio.run() and close its loop immediately.
        self.controller.release.set()
        if self._loop and self._server:
            self._loop.call_soon_threadsafe(self._server.close)
        if self._thread:
            self._thread.join(10)
            if self._thread.is_alive():
                raise RuntimeError("fixture registry thread did not stop")

    def _make_certificate(self) -> None:
        command = (
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", os.fspath(self._ca_key), "-out", os.fspath(self.ca),
            "-days", "1", "-subj", "/CN=phase6 fixture CA",
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign",
        )
        subprocess.run(command, check=True, capture_output=True)
        subprocess.run((
            "openssl", "req", "-newkey", "rsa:2048", "-nodes",
            "-keyout", os.fspath(self._key), "-out", os.fspath(self.root / "server.csr"),
            "-subj", "/CN=registry.npmjs.org",
        ), check=True, capture_output=True)
        extensions = self.root / "extensions"
        extensions.write_text(
            "basicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature,keyEncipherment\n"
            "extendedKeyUsage=serverAuth\n"
            "subjectAltName=DNS:registry.npmjs.org\n"
        )
        subprocess.run((
            "openssl", "x509", "-req", "-in", os.fspath(self.root / "server.csr"),
            "-CA", os.fspath(self.ca), "-CAkey", os.fspath(self._ca_key),
            "-CAcreateserial", "-out", os.fspath(self._cert), "-days", "1",
            "-extfile", os.fspath(extensions),
        ), check=True, capture_output=True)

    async def _serve(self, ready: threading.Event) -> None:
        self._loop = asyncio.get_running_loop()
        self._server = await asyncio.start_server(self._client, "0.0.0.0", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        ready.set()
        async with self._server:
            try:
                await self._server.serve_forever()
            except asyncio.CancelledError:
                pass

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            first = request.split(b"\r\n", 1)[0]
            if first != b"CONNECT registry.npmjs.org:443 HTTP/1.1":
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                await writer.drain(); return
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(self._cert, self._key)
            await writer.start_tls(context)
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            target = request.split(b" ", 2)[1].decode("ascii")
            self.controller.requests += 1
            self.controller.request_started.set()
            mode = self.controller.mode
            if mode == "pause":
                await asyncio.to_thread(self.controller.release.wait)
            package = next((item for item in self.packages if item.path == target), None)
            if package is None:
                await self._response(writer, 404, b"not found\n"); return
            if mode == "large-error":
                await self._response(writer, 503, b"fixture-error " * 100_000); return
            if mode == "partial":
                body = package.tarball
                writer.write(f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n\r\n".encode())
                writer.write(body[: max(1, len(body) // 2)]); await writer.drain(); return
            await self._response(writer, 200, package.tarball)
        except (asyncio.IncompleteReadError, ConnectionError, TimeoutError, ssl.SSLError):
            pass
        finally:
            writer.close()
            try: await writer.wait_closed()
            except Exception: pass

    @staticmethod
    async def _response(writer: asyncio.StreamWriter, status: int, body: bytes) -> None:
        reason = {200: "OK", 404: "Not Found", 503: "Unavailable"}[status]
        writer.write(f"HTTP/1.1 {status} {reason}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
        await writer.drain()


class ManagedChild:
    """A process-group-owned command for deadline, output, and build interruption."""
    def __init__(self, argv: tuple[str, ...]):
        self.process = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )

    @classmethod
    def python(cls, code: str) -> "ManagedChild":
        return cls((os.environ.get("PYTHON", "python3"), "-c", code))

    def interrupt(self) -> None:
        os.killpg(self.process.pid, signal.SIGINT)

    def stop(self) -> None:
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGKILL)
        self.process.wait(timeout=5)
        for stream in (self.process.stdout, self.process.stderr):
            if stream is not None and not stream.closed:
                stream.close()

    def __enter__(self) -> "ManagedChild": return self
    def __exit__(self, *_: object) -> None: self.stop()

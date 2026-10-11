"""Task 6.1 hermetic local-registry harness capability checks."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import signal
import ssl
import tempfile
import threading
import time
import tracemalloc
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from docker.npm_environment import (
    DockerRunExecutor, LockedNpmError, RootSpec, assemble_environment, assembler_script_digest,
    compute_assembler_identity, npm_policy_digest, preflight, publication,
    verify_output,
)
from docker.npm_environment.network import CorporateNetworkPolicy
from docker.versioning.build_orchestration import BuildRequest, ProcessResult, orchestrate_build
from docker.versioning.dispatch_types import ExitKind
from docker.versioning.effective import resolve_build_projection
from docker.versioning.host_progress import HostPhase, HostPhaseEvent, HostPhaseState
from docker.versioning.inventory import load_inventory
from tests.phase6_acceptance_harness import LocalRegistryProxy, make_lock, make_package


class TestRegistryProxyCleanup(unittest.TestCase):
    def test_cleanup_releases_waiters_before_loop_can_close(self):
        # Reproduce the race deterministically: the registry loop completes
        # shutdown before the owner can schedule another callback.
        proxy = LocalRegistryProxy(make_package(), Path("unused-registry-root"))
        proxy._server = mock.Mock()
        proxy._loop = mock.Mock()
        proxy._thread = mock.Mock()
        proxy._thread.is_alive.return_value = False
        closed = False

        def schedule(callback):
            nonlocal closed
            if closed:
                raise RuntimeError("Event loop is closed")
            callback()
            closed = True

        released_at_close = []
        proxy._loop.call_soon_threadsafe.side_effect = schedule
        proxy._server.close.side_effect = lambda: released_at_close.append(
            proxy.controller.release.is_set())
        proxy.__exit__(None, None, None)
        self.assertTrue(closed)
        self.assertEqual([True], released_at_close)
        proxy._loop.call_soon_threadsafe.assert_called_once_with(proxy._server.close)
        proxy._thread.join.assert_called_once_with(10)
        proxy._thread.is_alive.assert_called_once_with()


class TestPhase6RegistryHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="phase6-registry-")
        self.addCleanup(self.temporary.cleanup)
        self.package = make_package()
        self.proxy = LocalRegistryProxy(self.package, Path(self.temporary.name))
        self.proxy.__enter__()
        self.addCleanup(self.proxy.__exit__, None, None, None)

    def _open(self, timeout: float = 5) -> bytes:
        proxy = f"http://127.0.0.1:{self.proxy.port}"
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"https": proxy}),
            urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=self.proxy.ca)),
        )
        return opener.open("https://registry.npmjs.org" + self.package.path,
                           timeout=timeout).read()

    def test_fixture_tarball_and_lock_are_deterministic_and_validly_bound(self):
        again = make_package()
        self.assertEqual(self.package.tarball, again.tarball)
        self.assertEqual(self.package.integrity, again.integrity)
        lock = make_lock(self.package)
        self.assertIn(self.package.integrity.encode(), lock)
        validated = preflight(
            lock, roots=(RootSpec(self.package.name, self.package.version),),
            platform="linux-x64", node_version="24.18.0", npm_version="11.16.0",
        )
        self.assertEqual(validated.roots[0].name, self.package.name)
        self.assertEqual(self._open(), self.package.tarball)

    def test_registry_response_can_be_paused_and_released(self):
        self.proxy.controller.select("pause")
        result: list[bytes] = []
        worker = threading.Thread(target=lambda: result.append(self._open(10)))
        worker.start()
        self.assertTrue(self.proxy.controller.request_started.wait(5))
        time.sleep(0.05)
        self.assertTrue(worker.is_alive())
        self.proxy.controller.release.set()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [self.package.tarball])

    def test_registry_can_abort_after_partial_download(self):
        self.proxy.controller.select("partial")
        with self.assertRaises(Exception) as raised:  # transport error type is implementation-specific
            self._open()
        self.assertNotIsInstance(raised.exception, AssertionError)

    def test_registry_can_emit_large_failure_response(self):
        self.proxy.controller.select("large-error")
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self._open()
        error = raised.exception
        try:
            self.assertEqual(error.code, 503)
            self.assertGreater(len(error.read()), 1_000_000)
        finally:
            error.close()



@unittest.skipUnless(
    os.environ.get("BOUND_PI_PHASE6_HERMETIC") == "1",
    "run through validate-bound-pi-assembly-execution-phase6-registry",
)
class TestDockerBackedRegistryAcceptance(unittest.TestCase):
    """Real production assembly against the local proxy, never public npm."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="phase6-docker-registry-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.address = os.environ["BOUND_PI_PHASE6_HOST_ACCESS_ADDRESS"]
        projection = resolve_build_projection(
            load_inventory(Path(__file__).resolve().parents[1] / "docker-constructor.toml").build,
            {}, platform="linux-amd64",
        )
        self.projection = projection
        self.package = make_package(
            projection.pi_release.package, projection.pi_version,
        )
        self.seed_package = make_package("phase6-cache-seed", "1.0.0")
        self.lock = make_lock(self.package)
        self.proxy = LocalRegistryProxy((self.package, self.seed_package), self.root)
        self.proxy.__enter__()
        self.addCleanup(self.proxy.__exit__, None, None, None)
        self.validated = preflight(
            self.lock, roots=(RootSpec(self.package.name, self.package.version),),
            platform="linux-x64", node_version=projection.node.node_version,
            npm_version=projection.node.npm_version,
        )
        self.assembler = compute_assembler_identity(
            image_digest=projection.node.image,
            node_version=projection.node.node_version,
            npm_version=projection.node.npm_version,
            script_digest=assembler_script_digest(),
            policy_digest=npm_policy_digest(), platform="linux-x64",
        )
        self.cache = self.root / "cache"
        self.cache.mkdir(mode=0o700)
        self.network = CorporateNetworkPolicy(
            proxy_url=self.proxy.proxy_url(self.address),
            corporate_trust_bundle=str(self.proxy.ca.resolve()),
        )

    def _assemble(self, *, validated=None, assembler=None, executor=None):
        effective_validated = validated or self.validated
        effective_assembler = assembler or self.assembler
        # Register before Docker can start so validator cleanup remains useful
        # if the test process itself is interrupted or aborts mid-assembly.
        self._container_name(
            validated=effective_validated, assembler=effective_assembler,
        )
        return assemble_environment(
            validated=effective_validated,
            assembler=effective_assembler,
            cache_root=self.cache, executor=executor or DockerRunExecutor(),
            corporate_network=self.network,
        )

    def _validated(self, consumer: str):
        return preflight(
            make_lock(self.package, consumer),
            roots=(RootSpec(self.package.name, self.package.version),),
            platform="linux-x64", node_version=self.validated.node_version,
            npm_version=self.validated.npm_version,
        )

    def _container_name(self, *, validated=None, assembler=None) -> str:
        from docker.npm_environment import compute_assembler_input_identity
        identity = compute_assembler_input_identity(
            validated or self.validated, assembler or self.assembler,
        )
        name = "npm-assembler-" + identity.digest[:16]
        resource_file = os.environ.get("BOUND_PI_PHASE6_RESOURCE_FILE")
        if resource_file:
            with open(resource_file, "a", encoding="utf-8") as resources:
                resources.write(name + "\n")
        return name

    def _assert_container_absent(self, *, validated=None, assembler=None) -> None:
        import subprocess
        name = self._container_name(validated=validated, assembler=assembler)
        inspected = subprocess.run(
            ("docker", "inspect", name), capture_output=True, text=True,
        )
        self.assertNotEqual(inspected.returncode, 0, f"orphan assembler container: {name}")

    def _constructor_repo(self) -> Path:
        repo = self.root / "constructor"
        repo.mkdir(exist_ok=True)
        source = Path(__file__).resolve().parents[1] / "docker-constructor.toml"
        inventory_text = source.read_text()
        fixture_digests = iter(
            hashlib.sha256(name.encode()).hexdigest()
            for name in ("rustup", "uv", "rtk", "fd")
        )
        inventory_text, replacements = re.subn(
            r'(?m)^sha256 = "[0-9a-f]{64}"$',
            lambda match: f'sha256 = "{next(fixture_digests)}"',
            inventory_text,
            count=4,
        )
        if replacements != 4:
            raise AssertionError("expected four selected artifact SHA-256 fields")
        (repo / "docker-constructor.toml").write_text(inventory_text)
        bundle = repo / ".docker-local" / "corporate-ca-bundle.crt"
        bundle.parent.mkdir()
        shutil.copyfile(self.proxy.ca, bundle)
        (repo / "docker-constructor.local.toml").write_text(
            f'[cache]\ndir = "{self.root / "constructor-cache"}"\n'
            f'[network.proxy]\nurl = "{self.proxy.proxy_url(self.address)}"\n'
            '[corporate-trust]\nenabled = true\n'
        )
        return repo

    def _constructor_build(
        self, repo: Path, events: list[object], runner, *, tag: str | None = None,
        dockerfile: str | None = None, assembler_executor=None,
    ):
        package_json = json.dumps(
            {
                "name": "phase6-consumer",
                "version": "1.0.0",
                "dependencies": {self.package.name: self.package.version},
            },
            sort_keys=True, separators=(",", ":"),
        ).encode() + b"\n"

        checksums = (
            f"{hashlib.sha256(package_json).hexdigest()}  "
            "pi-coding-agent-install-package.json\n"
            f"{hashlib.sha256(self.lock).hexdigest()}  "
            "pi-coding-agent-install-package-lock.json\n"
        ).encode()

        class ReleaseTransport:
            def stream(inner_self, url):
                if url.endswith("/SHA256SUMS"):
                    yield checksums
                elif url.endswith("/pi-coding-agent-install-package-lock.json"):
                    yield self.lock
                elif url.endswith("/pi-coding-agent-install-package.json"):
                    yield package_json
                else:
                    raise AssertionError(f"unexpected Pi release URL: {url}")

        artifacts = self.root / "artifacts"
        artifacts.mkdir(exist_ok=True)
        paths = []
        for name in ("rustup", "uv", "rtk", "fd"):
            path = artifacts / name
            if path.exists():
                path.chmod(0o644)
            path.write_bytes(name.encode())
            path.chmod(0o444)
            paths.append(path)

        current_assembler = compute_assembler_identity(
            image_digest=self.assembler.image_digest,
            node_version=self.assembler.node_version,
            npm_version=self.assembler.npm_version,
            script_digest=assembler_script_digest(),
            policy_digest=npm_policy_digest(), platform="linux-x64",
        )
        self._container_name(assembler=current_assembler)
        return orchestrate_build(BuildRequest(
            inventory_path=str(repo / "docker-constructor.toml"),
            repo_root=str(repo), context=str(repo), tag=tag, dockerfile=dockerfile,
            confirmed=True, runner=runner, event_sink=events.append,
            _assembler_executor=assembler_executor,
            _transport_factory=lambda _policy: ReleaseTransport(),
            _materialize_artifacts=lambda *_args, **_kwargs: tuple(paths),
            _named_context_supported=lambda: True,
        project_root=Path(str(repo / "docker-constructor.toml")).resolve().parent))

    def test_constructor_cold_and_warm_build_preserve_progress_and_final_image(self):
        import subprocess

        class MainBuild:
            def __init__(inner_self):
                inner_self.calls = []
            def run(inner_self, argv):
                inner_self.calls.append(argv)
                completed = subprocess.run(argv, capture_output=True, text=True)
                return ProcessResult(argv, completed.returncode,
                                     completed.stdout, completed.stderr)

        repo = self._constructor_repo()
        (repo / "Dockerfile.phase6").write_text(
            "ARG NODE_BASE_IMAGE\n"
            "FROM ${NODE_BASE_IMAGE} AS runtime\n"
            "COPY --from=constructor-artifacts derived-environments/pi/opt/pi /opt/pi\n"
            'CMD ["/opt/pi/bin/pi"]\n'
        )
        tag = "dc-phase6-final-" + os.environ.get("BOUND_PI_PHASE6_RUN_ID", "local")
        self.addCleanup(subprocess.run, ("docker", "image", "rm", "-f", tag),
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cold_events: list[object] = []
        cold_runner = MainBuild()
        tracemalloc.start()
        before, _peak = tracemalloc.get_traced_memory()
        phase6_dockerfile = str((repo / "Dockerfile.phase6").resolve())
        cold = self._constructor_build(repo, cold_events, cold_runner, tag=tag,
                                       dockerfile=phase6_dockerfile)
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.assertEqual(cold.exit_kind, ExitKind.SUCCESS, cold.message)
        self.assertEqual(len(cold_runner.calls), 1)
        self.assertLess(peak - before, 16 * 1024 * 1024)
        self.assertIn(
            HostPhaseEvent(HostPhase.DOCKER_TRANSITION, HostPhaseState.SUCCEEDED),
            cold_events,
        )
        requests_after_cold = self.proxy.controller.requests
        self.assertGreater(requests_after_cold, 0)

        self.proxy.controller.select("pause")
        warm_events: list[object] = []
        warm_runner = MainBuild()
        warm = self._constructor_build(repo, warm_events, warm_runner, tag=tag,
                                       dockerfile=phase6_dockerfile)
        self.assertEqual(warm.exit_kind, ExitKind.SUCCESS, warm.message)
        self.assertEqual(self.proxy.controller.requests, requests_after_cold)
        self.assertFalse(self.proxy.controller.request_started.is_set())

        def normalized_build_call(call):
            return tuple(
                "constructor-artifacts=<ephemeral-snapshot>"
                if argument.startswith("constructor-artifacts=") else argument
                for argument in call
            )

        self.assertEqual(
            [normalized_build_call(call) for call in warm_runner.calls],
            [normalized_build_call(call) for call in cold_runner.calls],
        )
        runtime = subprocess.run(
            ("docker", "run", "--rm", tag), capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(runtime.returncode, 0, runtime.stderr)
        self.assertEqual(runtime.stdout, "phase6-pi\n")

        # Feed the real constructor result through the production facade's JSON
        # renderer. No host event sink is created and stdout is exactly one
        # parseable document with no lifecycle/diagnostic contamination.
        from docker import constructor_cli
        import docker.versioning.build_orchestration as build_orchestration
        stdout = io.StringIO()
        with (
            mock.patch.object(build_orchestration, "orchestrate_build", return_value=warm),
            contextlib.redirect_stdout(stdout),
        ):
            rc = constructor_cli.main(
                ("--output", "json", "--project-directory", str(repo),
                 "build", "--yes"),
                stdout_isatty=lambda: False, stderr_isatty=lambda: False,
            )
        self.assertEqual(rc, 0)
        document = json.loads(stdout.getvalue())
        self.assertEqual(document["data"]["return_code"], 0)
        self.assertNotIn("locked dependency assembly", stdout.getvalue().lower())
        self.assertIn(
            HostPhaseEvent(HostPhase.LOCKED_ASSEMBLY, HostPhaseState.SUCCEEDED),
            warm_events,
        )
        self._assert_container_absent()

    def test_cold_then_network_blocked_warm_reuses_verified_publication(self):
        cold = self._assemble()
        requests_after_cold = self.proxy.controller.requests
        self.assertGreater(requests_after_cold, 0)
        namespace = publication.prepare_assembler_namespace(
            self.cache, self.assembler.digest,
        )
        self.assertIsNotNone(verify_output(
            namespace, cold.output_identity, input_identity=cold.input_identity,
        ))

        self.proxy.controller.select("pause")
        warm = self._assemble()
        self.assertEqual(warm.output_identity, cold.output_identity)
        self.assertEqual(self.proxy.controller.requests, requests_after_cold)
        self.assertFalse(self.proxy.controller.request_started.is_set())

    def test_stalled_request_fails_finitely_without_network_classification(self):
        import docker.npm_environment.assembler as policy
        import docker.npm_environment.execution as execution

        self.proxy.controller.select("pause")
        with (
            mock.patch.multiple(
                policy, NPM_REQUEST_TIMEOUT_MS=100, NPM_RETRY_COUNT=0,
                NPM_RETRY_MIN_TIMEOUT_MS=10, NPM_RETRY_MAX_TIMEOUT_MS=10,
            ),
            mock.patch.object(execution, "ASSEMBLY_TOTAL_TIMEOUT_SECONDS", 10),
        ):
            assembler = compute_assembler_identity(
                image_digest=self.assembler.image_digest,
                node_version=self.assembler.node_version,
                npm_version=self.assembler.npm_version,
                script_digest=assembler_script_digest(),
                policy_digest=npm_policy_digest(), platform="linux-x64",
            )
            started = time.monotonic()
            with self.assertRaises(LockedNpmError) as raised:
                self._assemble(assembler=assembler)
            elapsed = time.monotonic() - started

            class NoMainBuild:
                def run(inner_self, _argv):
                    raise AssertionError("main Docker build must not run")

            repo = self._constructor_repo()
            constructor_events: list[object] = []
            constructor_result = self._constructor_build(
                repo, constructor_events, NoMainBuild(),
            )
        self.proxy.controller.release.set()
        self.assertLess(elapsed, 10)
        # npm request/retry exhaustion is intentionally mapped through the
        # existing generic nonzero-exit reason; only the constructor-owned
        # outer deadline receives timeout-specific classification.
        self.assertEqual(raised.exception.reason, "npm_exit_nonzero")
        self.assertNotIn("network", raised.exception.reason)
        self.assertEqual(constructor_result.exit_kind, ExitKind.OPERATIONAL)
        self.assertNotIn(
            HostPhaseEvent(HostPhase.DOCKER_TRANSITION, HostPhaseState.STARTED),
            constructor_events,
        )
        self.assertLessEqual(len((constructor_result.message or "").encode()),
                             2 * 64 * 1024 + 8192)
        self.assertLessEqual(len(raised.exception.detail.encode()), 2 * 64 * 1024 + 4096)
        namespace = publication.prepare_assembler_namespace(self.cache, assembler.digest)
        self.assertEqual([], list(namespace.outputs.iterdir()))
        self.assertEqual([], list(namespace.staging.iterdir()))
        self._assert_container_absent(assembler=assembler)

    def test_large_registry_failure_uses_bounded_redacted_production_diagnostics(self):
        import docker.npm_environment.assembler as policy

        self.proxy.controller.select("large-error")
        with mock.patch.multiple(
            policy, NPM_REQUEST_TIMEOUT_MS=500, NPM_RETRY_COUNT=0,
            NPM_RETRY_MIN_TIMEOUT_MS=10, NPM_RETRY_MAX_TIMEOUT_MS=10,
        ):
            assembler = compute_assembler_identity(
                image_digest=self.assembler.image_digest,
                node_version=self.assembler.node_version,
                npm_version=self.assembler.npm_version,
                script_digest=assembler_script_digest(),
                policy_digest=npm_policy_digest(), platform="linux-x64",
            )
            with self.assertRaises(LockedNpmError) as raised:
                self._assemble(assembler=assembler)
        self.assertEqual(raised.exception.reason, "npm_exit_nonzero")
        self.assertLessEqual(len(raised.exception.detail.encode()), 2 * 64 * 1024 + 4096)
        self.assertNotIn(self.network.proxy_url, raised.exception.detail)
        self.assertNotIn(self.network.corporate_trust_bundle, raised.exception.detail)
        namespace = publication.prepare_assembler_namespace(self.cache, assembler.digest)
        self.assertEqual([], list(namespace.outputs.iterdir()))
        self.assertEqual([], list(namespace.staging.iterdir()))
        self._assert_container_absent(assembler=assembler)

    def test_partial_download_preserves_opaque_cache_and_retry_succeeds(self):
        import hashlib
        import docker.npm_environment.assembler as policy

        seed_validated = preflight(
            make_lock(self.seed_package, "phase6-cache-seed-consumer"),
            roots=(RootSpec(self.seed_package.name, self.seed_package.version),),
            platform="linux-x64", node_version=self.validated.node_version,
            npm_version=self.validated.npm_version,
        )
        with mock.patch.multiple(
            policy, NPM_REQUEST_TIMEOUT_MS=500, NPM_RETRY_COUNT=0,
            NPM_RETRY_MIN_TIMEOUT_MS=10, NPM_RETRY_MAX_TIMEOUT_MS=10,
        ):
            assembler = compute_assembler_identity(
                image_digest=self.assembler.image_digest,
                node_version=self.assembler.node_version,
                npm_version=self.assembler.npm_version,
                script_digest=assembler_script_digest(),
                policy_digest=npm_policy_digest(), platform="linux-x64",
            )
            # Establish a verified package download and publication in exactly
            # the policy namespace that will experience the partial response.
            seed = self._assemble(validated=seed_validated, assembler=assembler)
            namespace = publication.prepare_assembler_namespace(self.cache, assembler.digest)
            content_root = namespace.npm_cache / "_cacache" / "content-v2"

            def content_hashes():
                return {
                    path.relative_to(content_root): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in content_root.rglob("*") if path.is_file()
                }

            safe_content_before = content_hashes()
            self.assertTrue(safe_content_before, "seed tarball must create cacache content-v2 data")
            self.assertIn(
                hashlib.sha512(self.seed_package.tarball).hexdigest(),
                {
                    hashlib.sha512(path.read_bytes()).hexdigest()
                    for path in content_root.rglob("*") if path.is_file()
                },
                "content-v2 must contain the exact safely downloaded seed tarball",
            )

            self.proxy.controller.select("partial")
            with self.assertRaises(LockedNpmError):
                self._assemble(assembler=assembler)
            self.assertEqual(content_hashes(), safe_content_before)
            self.assertIsNotNone(verify_output(
                namespace, seed.output_identity, input_identity=seed.input_identity,
            ))
            self.assertEqual([], list(namespace.staging.iterdir()))

            self.proxy.controller.select("normal")
            retry = self._assemble(assembler=assembler)
        self.assertIsNotNone(verify_output(
            namespace, retry.output_identity, input_identity=retry.input_identity,
        ))
        for relative, digest in safe_content_before.items():
            self.assertEqual(
                hashlib.sha256((content_root / relative).read_bytes()).hexdigest(), digest,
                f"retry mutated safely cached opaque content: {relative}",
            )
        self._assert_container_absent(assembler=assembler)

    def test_constructor_build_interruption_cleans_up_and_retry_succeeds(self):
        class DownloadThenStall(DockerRunExecutor):
            def run_streaming(inner_self, argv, **kwargs):
                script = argv[-1].replace("exec npm ci", "npm ci") + "\nsleep 60\n"
                return super(DownloadThenStall, inner_self).run_streaming(
                    (*argv[:-1], script), **kwargs,
                )

        class NoMainBuild:
            def __init__(inner_self):
                inner_self.calls = 0
            def run(inner_self, _argv):
                inner_self.calls += 1
                raise AssertionError("main Docker build must not run after interruption")

        class SuccessfulMainBuild:
            def __init__(inner_self):
                inner_self.calls = 0
            def run(inner_self, argv):
                inner_self.calls += 1
                return ProcessResult(argv, 0, "retry-image-ok", "")

        repo = self._constructor_repo()
        events: list[object] = []
        main = NoMainBuild()
        timer = threading.Timer(1.0, os.kill, args=(os.getpid(), signal.SIGINT))
        timer.start()
        try:
            with self.assertRaises(KeyboardInterrupt):
                self._constructor_build(
                    repo, events, main, assembler_executor=DownloadThenStall(),
                )
        finally:
            timer.cancel()
        self.assertEqual(main.calls, 0)
        self.assertIn(
            HostPhaseEvent(HostPhase.LOCKED_ASSEMBLY, HostPhaseState.FAILED), events,
        )
        self.assertNotIn(
            HostPhaseEvent(HostPhase.DOCKER_TRANSITION, HostPhaseState.STARTED), events,
        )
        self.assertEqual([], list((self.root / "constructor-cache").rglob("staging/*")))
        self._assert_container_absent()

        retry_main = SuccessfulMainBuild()
        retry_events: list[object] = []
        retry = self._constructor_build(repo, retry_events, retry_main)
        self.assertEqual(retry.exit_kind, ExitKind.SUCCESS, retry.message)
        self.assertEqual(retry_main.calls, 1)
        self.assertIn(
            HostPhaseEvent(HostPhase.DOCKER_TRANSITION, HostPhaseState.SUCCEEDED),
            retry_events,
        )
        self._assert_container_absent()

    def test_interruption_preserves_prior_output_and_cache_then_retry_succeeds(self):
        class DownloadThenStall(DockerRunExecutor):
            def run_streaming(inner_self, argv, **kwargs):
                script = argv[-1].replace("exec npm ci", "npm ci") + "\nsleep 60\n"
                return super(DownloadThenStall, inner_self).run_streaming(
                    (*argv[:-1], script), **kwargs,
                )

        prior_validated = self._validated("phase6-prior")
        prior = self._assemble(validated=prior_validated)
        namespace = publication.prepare_assembler_namespace(
            self.cache, self.assembler.digest,
        )
        cache_before = {p.relative_to(namespace.npm_cache) for p in namespace.npm_cache.rglob("*")}
        interrupted_validated = self._validated("phase6-interrupted")
        timer = threading.Timer(1.0, os.kill, args=(os.getpid(), signal.SIGINT))
        timer.start()
        try:
            with self.assertRaises(KeyboardInterrupt):
                self._assemble(
                    validated=interrupted_validated, executor=DownloadThenStall(),
                )
        finally:
            timer.cancel()
        self.assertIsNotNone(verify_output(
            namespace, prior.output_identity, input_identity=prior.input_identity,
        ))
        cache_after = {p.relative_to(namespace.npm_cache) for p in namespace.npm_cache.rglob("*")}
        self.assertTrue(cache_before <= cache_after)
        self.assertEqual([], list(namespace.staging.iterdir()))
        self._assert_container_absent(validated=interrupted_validated)
        retry = self._assemble(validated=interrupted_validated)
        self.assertIsNotNone(verify_output(
            namespace, retry.output_identity, input_identity=retry.input_identity,
        ))


if __name__ == "__main__":
    unittest.main()

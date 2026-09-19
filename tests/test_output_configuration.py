"""Phase 4 output-policy configuration contracts."""
from __future__ import annotations

import contextlib
import json
import tempfile
import unittest
from collections.abc import Mapping
from unittest import mock
from pathlib import Path

from docker.versioning.errors import InventoryError
from docker.versioning.configuration_document_validation import ConfigurationDocumentError
from docker.versioning.inventory import load_inventory, load_project_configuration
from docker.versioning.local_project_configuration import (
    LOCAL_COMPANION_BASENAME,
    load_local_project_configuration,
    validate_local_document,
)
from docker.versioning.model import BuildLocalInputs, LocalConfig, LocalOutputPolicy
from docker.versioning.build_orchestration import BuildResult
from docker.versioning.dispatch_types import ExitKind


PROHIBITED_FIELDS = frozenset({"output", "host_heartbeat", "show_network_hosts"})


def assert_no_presentation_policy(case: unittest.TestCase, obj: object, *, path: str = "input") -> None:
    """Fail when any orchestration input carries host-only presentation policy."""
    seen: list[tuple[object, str]] = [(obj, path)]
    visited: set[int] = set()
    while seen:
        value, where = seen.pop()
        if value is None or isinstance(value, (str, bytes, int, float, bool)):
            continue
        if isinstance(value, (LocalConfig, LocalOutputPolicy)):
            case.fail(f"presentation policy crossed the facade boundary at {where}: {value!r}")
        if id(value) in visited:
            continue
        visited.add(id(value))
        if isinstance(value, BuildLocalInputs):
            for name in PROHIBITED_FIELDS:
                case.assertFalse(hasattr(value, name), f"{where} exposed {name}")
            continue
        if isinstance(value, Mapping):
            for key, child in value.items():
                case.assertNotIn(key, PROHIBITED_FIELDS, f"{where}.{key}")
                seen.append((child, f"{where}.{key}"))
        elif isinstance(value, (list, tuple, set, frozenset)):
            for index, child in enumerate(value):
                seen.append((child, f"{where}[{index}]"))
        elif hasattr(value, "__dict__") and not callable(value):
            for name, child in vars(value).items():
                case.assertNotIn(name, PROHIBITED_FIELDS, f"{where}.{name}")
                seen.append((child, f"{where}.{name}"))


class TestOutputAggregateRegistration(unittest.TestCase):
    def test_output_is_accepted_independently_of_every_other_local_table(self) -> None:
        combinations = (
            {},
            {"host-access": {"address": "10.0.2.2"}},
            {"cache": {"dir": "/var/tmp/cache"}},
            {"corporate-trust": {"enabled": True}},
            {"network": {"proxy": {"url": "http://proxy.example:3128"}}},
        )
        for companion in combinations:
            with self.subTest(companion=companion):
                local = validate_local_document({
                    **companion,
                    "output": {
                        "host_heartbeat": "lines",
                        "show_network_hosts": True,
                    },
                })
                self.assertEqual("lines", local.output.host_heartbeat)
                self.assertTrue(local.output.show_network_hosts)

    def test_every_top_level_table_except_registered_output_and_predecessors_is_rejected(self) -> None:
        for table in ("build", "runtime", "observability", "outputx"):
            with self.subTest(table=table):
                with self.assertRaises(InventoryError) as raised:
                    validate_local_document({table: {}})
                self.assertEqual(f"local.{table}", raised.exception.field)


class TestOutputOwner(unittest.TestCase):
    def _companion(self, content: str) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / LOCAL_COMPANION_BASENAME
        path.write_text(content)
        return path

    def test_absent_output_uses_immutable_defaults(self) -> None:
        self.assertEqual(LocalOutputPolicy(), validate_local_document({}).output)
        self.assertEqual("interactive", validate_local_document({}).output.host_heartbeat)
        self.assertFalse(validate_local_document({}).output.show_network_hosts)

    def test_all_closed_output_values_are_accepted_exactly(self) -> None:
        for heartbeat in ("interactive", "lines", "off"):
            for show_hosts in (False, True):
                with self.subTest(heartbeat=heartbeat, show_hosts=show_hosts):
                    local = validate_local_document({"output": {
                        "host_heartbeat": heartbeat,
                        "show_network_hosts": show_hosts,
                    }})
                    self.assertEqual(LocalOutputPolicy(heartbeat, show_hosts), local.output)

    def test_invalid_output_values_have_safe_path_specific_errors(self) -> None:
        cases = (
            ('[output]\nunknown = true\n', "local.output.unknown"),
            ('[output]\nhost_heartbeat = "verbose"\n', "local.output.host_heartbeat"),
            ('[output]\nhost_heartbeat = true\n', "local.output.host_heartbeat"),
            ('[output]\nshow_network_hosts = "true"\n', "local.output.show_network_hosts"),
            ('host_heartbeat = "lines"\n', "local.host_heartbeat"),
            ('show_network_hosts = true\n', "local.show_network_hosts"),
            ('[output]\nhost_heartbeat = "lines"\nhost_heartbeat = "off"\n', None),
        )
        for content, field in cases:
            with self.subTest(content=content):
                with self.assertRaises(ConfigurationDocumentError) as raised:
                    load_local_project_configuration(self._companion(content))
                self.assertEqual(field, raised.exception.field)
                self.assertNotIn("unrelated", str(raised.exception))

    def test_reviewed_inventory_rejects_output_settings(self) -> None:
        source = Path("docker-constructor.toml").read_text()
        for key, value in (("host_heartbeat", '"lines"'), ("show_network_hosts", "true")):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "docker-constructor.toml"
                path.write_text(f"{source}\n[output]\n{key} = {value}\n")
                with self.assertRaises(ConfigurationDocumentError) as raised:
                    load_inventory(path)
                self.assertEqual("output", raised.exception.field)

    def test_no_cli_or_environment_alias_exists_for_output_settings(self) -> None:
        import io
        import os
        from docker import constructor_cli

        # CLI: neither would-be flag belongs to the reviewed surface.
        for flag in ("--host-heartbeat", "--show-network-hosts"):
            with self.subTest(flag=flag), contextlib.redirect_stderr(io.StringIO()):
                self.assertNotEqual(0, constructor_cli.main([flag, "off"]))

        # Environment: both names are ignored by the companion loader.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.joinpath("docker-constructor.toml").write_text(
                Path("docker-constructor.toml").read_text()
            )
            root.joinpath(LOCAL_COMPANION_BASENAME).write_text(
                '[cache]\ndir = "/var/tmp/cache"\n'
            )
            with mock.patch.dict(
                os.environ,
                {"HOST_HEARTBEAT": "off", "SHOW_NETWORK_HOSTS": "true"},
            ):
                _, local = load_project_configuration(
                    root / "docker-constructor.toml"
                )
        self.assertEqual(LocalOutputPolicy(), local.output)


class TestFacadeOutputPolicyDispatch(unittest.TestCase):
    def _project(self, output: str = '') -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        root.joinpath("docker-constructor.toml").write_text(
            Path("docker-constructor.toml").read_text()
        )
        root.joinpath("Dockerfile").write_text("FROM scratch\n")
        root.joinpath(LOCAL_COMPANION_BASENAME).write_text(output)
        return root

    def test_tty_text_build_constructs_renderer_from_the_single_resolved_policy(self) -> None:
        from docker import constructor_cli

        project = self._project(
            '[output]\nhost_heartbeat = "lines"\nshow_network_hosts = true\n'
        )
        rendered: list[object] = []
        original_read_bytes = Path.read_bytes
        observed_policies: list[object] = []
        real_load = load_project_configuration

        def recording_load(path):
            loaded_inventory, loaded_local = real_load(path)
            observed_policies.append(loaded_local.output)
            return loaded_inventory, loaded_local

        with (
            mock.patch.object(
                constructor_cli, "_HostEventRenderer",
                side_effect=lambda *, output_policy: rendered.append(output_policy) or object(),
            ),
            mock.patch(
                "docker.versioning.inventory.load_project_configuration",
                side_effect=recording_load,
            ),
            mock.patch(
                "docker.versioning.build_orchestration.orchestrate_build",
                return_value=BuildResult(exit_kind=ExitKind.SUCCESS),
            ) as orchestrate,
            mock.patch.object(
                Path, "read_bytes", autospec=True, side_effect=original_read_bytes,
            ) as read_bytes,
        ):
            exit_code = constructor_cli.main(
                ["--project-directory", str(project), "build", "--yes"],
                stderr_isatty=lambda: True,
            )
        self.assertEqual(0, exit_code)
        self.assertEqual([LocalOutputPolicy("lines", True)], rendered)
        # The exact instance produced by the single read reaches only the
        # facade renderer factory.
        self.assertEqual(1, len(observed_policies))
        self.assertIs(observed_policies[0], rendered[0])
        self.assertEqual(1, orchestrate.call_count)
        request = orchestrate.call_args.args[0]
        self.assertIsNotNone(request.event_sink)
        # Domain planning receives only audited, output-free slices.
        self.assertEqual(
            BuildLocalInputs.from_local_config(
                validate_local_document({
                    "output": {"host_heartbeat": "lines", "show_network_hosts": True},
                })
            ),
            orchestrate.call_args.kwargs["local_inputs"],
        )
        self.assertIsNotNone(orchestrate.call_args.kwargs["inventory"])
        assert_no_presentation_policy(
            self,
            {
                "args": orchestrate.call_args.args,
                "kwargs": orchestrate.call_args.kwargs,
            },
        )
        companion_reads = [
            call for call in read_bytes.call_args_list
            if call.args[0].resolve() == project.joinpath(LOCAL_COMPANION_BASENAME)
        ]
        self.assertEqual(1, len(companion_reads))

    def test_non_live_modes_skip_the_renderer_but_still_plan_the_build(self) -> None:
        from docker import constructor_cli

        for output, isatty, extra in (
            ("text", False, []), ("json", True, []),
            ("text", True, ["--dry-run"]),
        ):
            with self.subTest(output=output, isatty=isatty, extra=extra):
                project = self._project('[output]\nhost_heartbeat = "off"\n')
                with (
                    mock.patch.object(constructor_cli, "_HostEventRenderer") as renderer,
                    mock.patch(
                        "docker.versioning.build_orchestration.orchestrate_build",
                        return_value=BuildResult(exit_kind=ExitKind.SUCCESS),
                    ) as orchestrate,
                ):
                    exit_code = constructor_cli.main(
                        ["--project-directory", str(project), "--output", output, "build", "--yes", *extra],
                        stderr_isatty=lambda: isatty,
                    )
                self.assertEqual(0, exit_code)
                renderer.assert_not_called()
                self.assertEqual(1, orchestrate.call_count)
                assert_no_presentation_policy(
                    self,
                    {
                        "args": orchestrate.call_args.args,
                        "kwargs": orchestrate.call_args.kwargs,
                    },
                )

    def test_malformed_output_config_is_reported_in_every_mode_without_planning(self) -> None:
        from docker import constructor_cli

        malformed = '[output]\nhost_heartbeat = "invalid"\n'
        for output, isatty, extra in (
            ("text", True, []), ("text", False, []), ("json", True, []),
            ("text", True, ["--dry-run"]),
        ):
            with self.subTest(output=output, isatty=isatty, extra=extra):
                project = self._project(malformed)
                with (
                    mock.patch.object(constructor_cli, "_HostEventRenderer") as renderer,
                    mock.patch("docker.versioning.build_orchestration.orchestrate_build") as orchestrate,
                ):
                    exit_code = constructor_cli.main(
                        ["--project-directory", str(project), "--output", output, "build", "--yes", *extra],
                        stderr_isatty=lambda: isatty,
                    )
                self.assertEqual(3, exit_code)
                renderer.assert_not_called()
                orchestrate.assert_not_called()


class TestOutputPolicyConfinement(unittest.TestCase):
    def test_facade_renderer_accepts_immutable_output_policy_without_domain_request_leakage(self) -> None:
        from docker.constructor_cli import _HostEventRenderer

        policy = LocalOutputPolicy("off", True)
        renderer = _HostEventRenderer(output_policy=policy)
        self.assertIs(policy, renderer.output_policy)

    def test_output_policy_is_absent_from_domain_request_models(self) -> None:
        import dataclasses
        from docker.versioning.build_orchestration import BuildRequest
        from docker.versioning.host_progress import HostDiagnosticEvent
        from docker.versioning.pi_assembly import PiAssemblyRequest

        for request in (
            BuildRequest, PiAssemblyRequest, HostDiagnosticEvent,
            BuildLocalInputs,
        ):
            with self.subTest(request=request.__name__):
                self.assertFalse(
                    PROHIBITED_FIELDS & {field.name for field in dataclasses.fields(request)}
                )

    def test_narrow_build_inputs_projection_drops_output_only(self) -> None:
        local = LocalConfig(output=LocalOutputPolicy("off", True))
        projected = BuildLocalInputs.from_local_config(local)
        self.assertEqual(BuildLocalInputs(), projected)
        self.assertFalse(hasattr(projected, "output"))
        self.assertFalse(hasattr(projected, "host_heartbeat"))
        self.assertFalse(hasattr(projected, "show_network_hosts"))



POLICY_TOKENS = (
    "host_heartbeat", "show_network_hosts",
    "host-heartbeat", "show-network-hosts",
    "[output]",
    LOCAL_COMPANION_BASENAME,
)


def assert_no_policy_strings(
    case: unittest.TestCase, value: object, *, where: str,
    extra: tuple[str, ...] = (),
) -> None:
    """Fail when rendered/serialized text carries output policy or the companion.

    ``assert_no_presentation_policy`` skips strings by design, so argument
    vectors and serialized documents need this explicit textual check.
    """
    if isinstance(value, (list, tuple)):
        text = " ".join(str(item) for item in value)
    else:
        text = str(value)
    lowered = text.lower()
    for token in (*POLICY_TOKENS, *extra):
        case.assertNotIn(
            token.lower(), lowered, f"{token!r} leaked into {where}: {text!r}",
        )


class _IntegrationProject:
    """A real project whose companion declares nondefault output and cache."""

    OUTPUT_OFF = 'host_heartbeat = "off"\nshow_network_hosts = true\n'
    OUTPUT_LINES = 'host_heartbeat = "lines"\nshow_network_hosts = false\n'

    def __init__(self, case: unittest.TestCase, *, output_body: str | None = None) -> None:
        directory = tempfile.TemporaryDirectory()
        case.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.inventory_path = self.root / "docker-constructor.toml"
        self.inventory_path.write_text(Path("docker-constructor.toml").read_text())
        self.dockerfile = self.root / "Dockerfile"
        self.dockerfile.write_text("FROM scratch\n")
        self.cache_dir = self.root / "cache"
        self.cache_dir.mkdir(mode=0o700)
        self.companion = self.root / LOCAL_COMPANION_BASENAME
        self.companion.write_text(
            f'[cache]\ndir = "{self.cache_dir}"\n'
            f"[output]\n{self.OUTPUT_OFF if output_body is None else output_body}"
        )
        # Exactly the production read the facade performs.
        self.inventory, self.local = load_project_configuration(self.inventory_path)
        self.local_inputs = BuildLocalInputs.from_local_config(self.local)
        case.assertEqual(
            LocalOutputPolicy("lines", False)
            if output_body == self.OUTPUT_LINES
            else LocalOutputPolicy("off", True),
            self.local.output,
        )

    def prepare_state(self) -> None:
        from docker.versioning.cache_storage import prepare_resolved_root
        from docker.versioning.project_state import resolve_project_state
        resolve_project_state(
            self.root, cache_root=prepare_resolved_root(self.cache_dir), create=True,
        )


class _RecordingBuildExecutor:
    """Recording ``docker build`` boundary matching the real Protocol."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: tuple[str, ...]) -> object:
        from docker.versioning.build_orchestration import ProcessResult
        self.calls.append(tuple(argv))
        return ProcessResult(
            argv=tuple(argv), return_code=0, stdout="build output", stderr="",
        )


def _fixture_snapshot(*_args: object, **_kwargs: object) -> object:
    from tests.build_test_support import fixture_directory
    from docker.versioning.build_snapshot import MaterializedSnapshot
    path = fixture_directory("fixture-snapshot-")
    return MaterializedSnapshot(path, path / "manifest.json")


class _BuildDispatch:
    """Exercise the real ``orchestrate_build`` path with external effects faked."""

    def __init__(
        self,
        project: _IntegrationProject,
        *,
        render_transform=None,
        confinement_transform=None,
    ) -> None:
        self.project = project
        self._render_transform = render_transform
        self._confinement_transform = confinement_transform
        self.executor = _RecordingBuildExecutor()
        self.events: list[object] = []
        self.materialize_requests: list[dict[str, object]] = []
        self.pi_requests: list[dict[str, object]] = []
        self.publish_projection: object | None = None
        self.transport_policies: list[object] = []
        self.confinement_plans: list[object] = []
        self.generated_ignore_bodies: list[str] = []

    def run(self) -> object:
        from docker.versioning import build_orchestration as module
        from docker.versioning.build_orchestration import (
            BuildRequest, PublishResult,
        )
        from tests.build_test_support import (
            digest_valid_selected_artifacts, fake_pi_materialization,
            no_network_transport_factory, publish_digest_valid_artifacts,
        )

        project = self.project

        def record_materialize(
            projection, *, constructor_project_root, cache_root,
            project_state, transport, lock,
        ):
            self.materialize_requests.append({
                "projection": projection,
                "constructor_project_root": constructor_project_root,
                "cache_root": cache_root,
                "project_state": project_state,
                "transport": transport,
                "lock": lock,
            })
            return publish_digest_valid_artifacts(
                projection, constructor_project_root=constructor_project_root,
                cache_root=cache_root, project_state=project_state,
            )

        def record_pi(
            projection, *, transport, cache_root, executor, uid, gid,
            proxy_url, proxy_no_proxy, corporate_trust_bundle, event_sink=None,
        ):
            self.pi_requests.append({
                "projection": projection,
                "cache_root": cache_root,
                "proxy_url": proxy_url,
                "proxy_no_proxy": proxy_no_proxy,
                "corporate_trust_bundle": corporate_trust_bundle,
                "event_sink": event_sink,
            })
            return fake_pi_materialization()

        def record_publish(projection, *, repo_root):
            self.publish_projection = projection
            return PublishResult(published_path=str(project.root / "effective.toml"))

        def record_transport(policy):
            self.transport_policies.append(policy)
            return no_network_transport_factory(policy)

        real_plan_confinement = module.plan_build_context_confinement

        def record_confinement(*, inventory_path, context, dockerfile):
            plan = real_plan_confinement(
                inventory_path=inventory_path, context=context, dockerfile=dockerfile,
            )
            if self._confinement_transform is not None:
                plan = self._confinement_transform(plan)
            self.confinement_plans.append(plan)
            return plan

        real_materialize_confinement = module.materialize_build_context_confinement

        def record_materialize_confinement(confinement, *, generated_root):
            materialized = real_materialize_confinement(
                confinement, generated_root=generated_root,
            )
            self.generated_ignore_bodies.append(
                materialized.ignorefile.read_text()
            )
            return materialized

        real_render = module.render_build_vector

        def record_render(inputs):
            argv = real_render(inputs)
            if self._render_transform is not None:
                argv = self._render_transform(argv, self.project)
            return argv

        request = BuildRequest(
            inventory_path=str(project.inventory_path),
            project_root=str(project.root),
            confirmed=True,
            runner=self.executor,
            event_sink=self.events.append,
            _materialize_artifacts=record_materialize,
            _materialize_pi=record_pi,
            _transport_factory=record_transport,
            _named_context_supported=lambda: True,
            _publish_projection=record_publish,
        )

        with (
            mock.patch.object(
                module, "select_build_artifacts",
                side_effect=digest_valid_selected_artifacts,
            ),
            mock.patch.object(
                module, "create_artifact_snapshot", side_effect=_fixture_snapshot,
            ),
            mock.patch.object(
                module, "plan_build_context_confinement",
                side_effect=record_confinement,
            ),
            mock.patch.object(
                module, "materialize_build_context_confinement",
                side_effect=record_materialize_confinement,
            ),
            mock.patch.object(module, "render_build_vector", side_effect=record_render),
        ):
            self.result = module.orchestrate_build(
                request,
                inventory=project.inventory,
                local_inputs=project.local_inputs,
            )
        self.request = request
        return self.result

    def serialized_projection(self) -> dict[str, object]:
        from docker.versioning import rendering
        return rendering.serialize_effective_build(self.publish_projection)


class _RunDispatch:
    """Exercise the real ``orchestrate_run`` dry-run path with companions loaded."""

    def __init__(self, project: _IntegrationProject, *, render_transform=None) -> None:
        self.project = project
        self._render_transform = render_transform
        self.effective_projections: list[object] = []

    def run(self) -> object:
        from docker.versioning import effective as effective_module
        from docker.versioning import rendering
        from docker.launcher import (
            RunRequest, WorkspaceSelection, orchestrate_run,
        )

        project = self.project
        project.prepare_state()

        real_resolve = effective_module.resolve_runtime

        def record_resolve(runtime, overrides):
            selected, effective = real_resolve(runtime, overrides)
            self.effective_projections.append(effective)
            return selected, effective

        real_render = rendering.render_run_vector

        def record_render(inputs):
            argv = real_render(inputs)
            if self._render_transform is not None:
                argv = self._render_transform(argv, project)
            return argv

        request = RunRequest(
            inventory_path=str(project.inventory_path),
            project_root=str(project.root),
            image="pi-cli-pi:latest",
            selection=WorkspaceSelection(workspace=str(project.root)),
            pi_home_host=str(project.root / "pi-home"),
            dry_run=True,
            _constructor_cache_root=str(project.cache_dir),
        )
        with (
            mock.patch.object(effective_module, "resolve_runtime", side_effect=record_resolve),
            mock.patch.object(rendering, "render_run_vector", side_effect=record_render),
        ):
            self.result = orchestrate_run(request)
        self.request = request
        return self.result

    def serialized_projection(self) -> dict[str, object]:
        from docker.versioning.effective import to_plain_data
        return to_plain_data(self.effective_projections[0])


class _VerifyDispatch:
    """Exercise the real facade ``verify`` dispatch with Docker calls stubbed."""

    def __init__(self, project: _IntegrationProject, *, network_transform=None) -> None:
        self.project = project
        self._network_transform = network_transform
        self.build_requests: list[object] = []
        self.runtime_requests: list[object] = []

    def run(self) -> int:
        from types import SimpleNamespace
        from docker import constructor_cli
        from docker.versioning import runtime_verification, verification

        project = self.project
        project.prepare_state()
        projection = project.root / "runtime.toml"
        projection.write_text("[project]\n")
        (project.root / "workspace").mkdir(exist_ok=True)

        def record_build(request):
            self.build_requests.append(request)
            return SimpleNamespace(all_ok=True, observations=(), errors=())

        def record_runtime(request):
            self.runtime_requests.append(request)
            return SimpleNamespace(all_ok=True, checks=(), errors=())

        patches = [
            mock.patch.object(verification, "verify_build", side_effect=record_build),
            mock.patch.object(
                runtime_verification, "verify_runtime", side_effect=record_runtime,
            ),
        ]
        if self._network_transform is not None:
            real_resolve = constructor_cli._resolve_verify_corporate_network

            def record_network(*args, **kwargs):
                resolved = real_resolve(*args, **kwargs)
                return self._network_transform(resolved, project)

            patches.append(mock.patch.object(
                constructor_cli, "_resolve_verify_corporate_network",
                side_effect=record_network,
            ))

        with contextlib.ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            self.exit_code = constructor_cli.main([
                "--project-directory", str(project.root),
                "verify", "--scope", "all",
                "--container", "pi-1",
                "--runtime-projection", str(projection),
                "--workspace", "/work",
            ])
        return self.exit_code


def _assert_build_argv_confinement(case, dispatch, project) -> None:
    """Rendered argv excludes policy text and the host-only companion."""
    case.assertIsNotNone(dispatch.result)
    case.assertEqual(ExitKind.SUCCESS, dispatch.result.exit_kind)
    case.assertEqual(1, len(dispatch.executor.calls))
    argv = dispatch.executor.calls[0]
    case.assertEqual(dispatch.result.build_args, argv)
    assert_no_policy_strings(case, argv, where="rendered docker build arguments")
    case.assertNotIn(str(project.companion), argv)
    case.assertNotIn(str(project.dockerfile), argv)
    # The generated Dockerfile copy (with its forced-ignore sibling) is used.
    case.assertIn("--file", argv)
    generated = argv[argv.index("--file") + 1]
    case.assertNotEqual(str(project.dockerfile), generated)
    case.assertIn("--build-context", argv)


def _assert_confinement_excludes_companion(case, dispatch) -> None:
    from docker.versioning.build_context_confinement import confinement_ignore_rules
    case.assertEqual(1, len(dispatch.confinement_plans))
    plan = dispatch.confinement_plans[0]
    case.assertIn(LOCAL_COMPANION_BASENAME, plan.relative_documents)
    case.assertIn(LOCAL_COMPANION_BASENAME, confinement_ignore_rules(plan))
    # The generated Dockerfile-specific ignore file carries the exclusion.
    case.assertEqual(1, len(dispatch.generated_ignore_bodies))
    case.assertIn(LOCAL_COMPANION_BASENAME, dispatch.generated_ignore_bodies[0])


class TestReadOnlyUpdateDiscoveryConfinement(unittest.TestCase):
    def test_companion_read_reaches_update_discovery_only_as_a_cache_slice(self) -> None:
        from docker.versioning import readonly_service
        from docker.versioning.model import LocalCacheConfig
        from docker.versioning.transports import build_transports

        project = _IntegrationProject(self)
        captured_transports: dict[str, object] = {}
        captured_handler_args: dict[str, object] = {}
        real_build_transports = build_transports
        real_handler = readonly_service._HANDLERS["check-updates"]

        def recording_handler(inventory, command_args, *, progress=None):
            captured_handler_args.update(command_args)
            return real_handler(inventory, command_args, progress=progress)

        def recording_transports(**kwargs):
            captured_transports.update(kwargs)
            return real_build_transports(no_cache=True)

        with (
            mock.patch.dict(
                readonly_service._HANDLERS,
                {"check-updates": recording_handler},
            ),
            mock.patch(
                "docker.versioning.transports.build_transports",
                side_effect=recording_transports,
            ),
            mock.patch(
                "docker.versioning.updates.check_updates", return_value=[],
            ),
        ):
            result = readonly_service.dispatch(
                project.inventory_path, "check-updates",
                command_args={"scope": "all"},
            )

        self.assertEqual(ExitKind.SUCCESS, result.exit_kind)
        # The aggregate with [output] never reaches discovery...
        self.assertNotIn("_local_config", captured_handler_args)
        assert_no_presentation_policy(self, captured_handler_args)
        assert_no_presentation_policy(self, captured_transports)
        # ...only the cache-owned slice, preserving cache behaviour exactly.
        self.assertEqual(
            LocalCacheConfig(str(project.cache_dir)),
            captured_transports["local_cache"],
        )
        self.assertEqual(
            LocalCacheConfig(str(project.cache_dir)),
            captured_handler_args["_local_cache"],
        )
        self.assertNotIsInstance(captured_transports["local_cache"], LocalConfig)


class TestOutputPolicyBoundaryConfinement(unittest.TestCase):
    """Exercise real construction paths under nondefault ``[output]``.

    Each candidate is captured from the production dispatch/orchestration
    path for its boundary and audited both structurally and textually, so
    neither field introspection nor object-graph walking alone establishes
    confinement.
    """

    def test_build_dispatch_renders_argv_without_policy_or_companion(self) -> None:
        project = _IntegrationProject(self)
        dispatch = _BuildDispatch(project)
        dispatch.run()
        _assert_build_argv_confinement(self, dispatch, project)
        _assert_confinement_excludes_companion(self, dispatch)

    def test_build_dispatch_forwards_only_domain_slices_downstream(self) -> None:
        project = _IntegrationProject(self)
        dispatch = _BuildDispatch(project)
        dispatch.run()
        assert_no_presentation_policy(self, project.inventory)
        assert_no_presentation_policy(self, project.local_inputs)
        assert_no_presentation_policy(self, dispatch.request)
        assert_no_presentation_policy(self, dispatch.materialize_requests)
        assert_no_presentation_policy(self, dispatch.pi_requests)
        assert_no_presentation_policy(self, dispatch.publish_projection)
        assert_no_presentation_policy(self, dispatch.transport_policies)
        assert_no_presentation_policy(self, dispatch.events)
        serialized = dispatch.serialized_projection()
        assert_no_presentation_policy(self, serialized)
        assert_no_policy_strings(
            self, json.dumps(serialized, sort_keys=True),
            where="published effective build projection",
            extra=(str(project.cache_dir), "[cache]"),
        )

    def test_run_dispatch_renders_argv_without_policy_or_companion(self) -> None:
        project = _IntegrationProject(self)
        dispatch = _RunDispatch(project)
        result = dispatch.run()
        self.assertEqual(ExitKind.SUCCESS, result.exit_kind)
        self.assertTrue(result.run_args)
        assert_no_policy_strings(
            self, result.run_args, where="rendered docker run arguments",
        )
        self.assertNotIn(str(project.companion), result.run_args)
        # No companion mount/copy is rendered and no output setting reaches
        # the effective runtime projection.
        assert_no_presentation_policy(self, dispatch.request)
        assert_no_presentation_policy(self, result.run_args)
        assert_no_presentation_policy(self, dispatch.effective_projections)
        serialized = dispatch.serialized_projection()
        assert_no_presentation_policy(self, serialized)
        assert_no_policy_strings(
            self, json.dumps(serialized, sort_keys=True),
            where="effective runtime projection",
            extra=(str(project.cache_dir), "[cache]"),
        )

    def test_verify_dispatch_builds_requests_without_policy_or_companion(self) -> None:
        project = _IntegrationProject(self)
        dispatch = _VerifyDispatch(project)
        self.assertEqual(0, dispatch.run())
        self.assertEqual(1, len(dispatch.build_requests))
        self.assertEqual(1, len(dispatch.runtime_requests))
        for request in (*dispatch.build_requests, *dispatch.runtime_requests):
            assert_no_presentation_policy(self, request)
            assert_no_policy_strings(
                self, repr(request), where="verification request",
            )
            self.assertNotIn(str(project.companion), repr(request))
        build_request = dispatch.build_requests[0]
        self.assertNotIn(
            str(project.companion), str(build_request.effective_projection_path),
        )

    def test_domain_event_production_is_independent_of_output_policy(self) -> None:
        off = _IntegrationProject(self, output_body=_IntegrationProject.OUTPUT_OFF)
        lines = _IntegrationProject(self, output_body=_IntegrationProject.OUTPUT_LINES)
        off_dispatch = _BuildDispatch(off)
        lines_dispatch = _BuildDispatch(lines)
        off_dispatch.run()
        lines_dispatch.run()
        self.assertEqual(ExitKind.SUCCESS, off_dispatch.result.exit_kind)
        self.assertEqual(ExitKind.SUCCESS, lines_dispatch.result.exit_kind)
        # Nondefault policies differ, yet domain events are identical.
        self.assertNotEqual(off.local.output, lines.local.output)
        self.assertTrue(off_dispatch.events)
        self.assertEqual(off_dispatch.events, lines_dispatch.events)
        # And the renderer is never constructed by orchestration.
        for dispatch in (off_dispatch, lines_dispatch):
            assert_no_presentation_policy(self, dispatch.events)
            assert_no_presentation_policy(self, dispatch.pi_requests)

    def test_reviewed_serialization_and_update_discovery_exclude_output(self) -> None:
        from types import MappingProxyType
        from docker.versioning.effective import (
            EffectiveConfiguration, serialize_effective_inventory,
        )
        from docker.versioning.inventory import load_inventory_raw
        from docker.versioning.updates import build_update_targets

        project = _IntegrationProject(self)
        serialized = serialize_effective_inventory(EffectiveConfiguration(
            inventory=project.inventory, overrides=MappingProxyType({}),
        ))
        raw = load_inventory_raw(project.inventory_path)
        targets = build_update_targets(project.inventory)
        self.assertNotIn("output", serialized)
        self.assertNotIn("output", raw)
        for candidate in (serialized, raw, targets):
            assert_no_presentation_policy(self, candidate)
            assert_no_policy_strings(
                self, json.dumps(candidate, sort_keys=True, default=str),
                where="reviewed serialization/update discovery",
            )


class TestBoundaryAssertionSensitivity(unittest.TestCase):
    """Each boundary assertion fails when a leak is introduced at its source."""

    def test_build_argv_checker_detects_a_companion_mount(self) -> None:
        project = _IntegrationProject(self)
        dispatch = _BuildDispatch(
            project,
            render_transform=lambda argv, proj: argv + (
                "--mount", f"type=bind,src={proj.companion},dst=/host",
            ),
        )
        dispatch.run()
        with self.assertRaises(AssertionError):
            _assert_build_argv_confinement(self, dispatch, project)

    def test_build_argv_checker_detects_policy_settings(self) -> None:
        project = _IntegrationProject(self)
        dispatch = _BuildDispatch(
            project,
            render_transform=lambda argv, proj: argv + (
                "--build-arg", "HOST_HEARTBEAT=off",
            ),
        )
        dispatch.run()
        with self.assertRaises(AssertionError):
            _assert_build_argv_confinement(self, dispatch, project)

    def test_serialized_projection_checker_detects_output_settings(self) -> None:
        from docker.versioning import rendering

        project = _IntegrationProject(self)
        dispatch = _BuildDispatch(project)
        dispatch.run()
        real_serialize = rendering.serialize_effective_build

        def leaky_serialize(projection):
            data = real_serialize(projection)
            data["output"] = {"host_heartbeat": "off"}
            return data

        with mock.patch.object(rendering, "serialize_effective_build", side_effect=leaky_serialize):
            serialized = dispatch.serialized_projection()
        with self.assertRaises(AssertionError):
            assert_no_policy_strings(
                self, json.dumps(serialized, sort_keys=True),
                where="published effective build projection",
            )

    def test_confinement_checker_detects_a_dropped_companion_exclusion(self) -> None:
        import dataclasses

        project = _IntegrationProject(self)
        dispatch = _BuildDispatch(
            project,
            confinement_transform=lambda plan: dataclasses.replace(
                plan, relative_documents=(),
            ),
        )
        dispatch.run()
        with self.assertRaises(AssertionError):
            _assert_confinement_excludes_companion(self, dispatch)

    def test_run_argv_checker_detects_a_companion_mount(self) -> None:
        project = _IntegrationProject(self)
        dispatch = _RunDispatch(
            project,
            render_transform=lambda argv, proj: argv + (
                "--mount", f"type=bind,src={proj.companion},dst=/host",
            ),
        )
        result = dispatch.run()
        with self.assertRaises(AssertionError):
            assert_no_policy_strings(
                self, result.run_args, where="rendered docker run arguments",
            )

    def test_verify_checker_detects_companion_forwarding(self) -> None:
        project = _IntegrationProject(self)
        dispatch = _VerifyDispatch(
            project,
            network_transform=lambda resolved, proj: (
                resolved[0], str(proj.companion), resolved[2], resolved[3],
            ),
        )
        dispatch.run()
        with self.assertRaises(AssertionError):
            assert_no_policy_strings(
                self, repr(dispatch.runtime_requests[0]),
                where="verification request",
            )



if __name__ == "__main__":
    unittest.main()

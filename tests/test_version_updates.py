"""Coordinator tests: target traversal, check_updates, suggestions.

Uses fake providers (not real network) via injected transports.
"""
from __future__ import annotations

import json
import pathlib
import unittest

from docker.versioning.model import (
    Constraint,
    OverridePolicy,
    ArtifactEntry,
    CandidateArtifact,
    DockerRegistrySource,
    DockerRegistryUpdate,
    GitHubReleaseSource,
    GitHubReleaseUpdate,
    GitSource,
    GitRefUpdate,
    Inventory,
    NodeEntry,
    NpmArtifact,
    NpmSource,
    NpmToolEntry,
    NpmUpdate,
    OhMyZshEntry,
    PiExtensionEntry,
    PiReleaseSource,
    PiToolEntry,
    PrebuiltToolEntry,
    PythonEntry,
    PyPiSource,
    PyPiUpdate,
    RustChannelSource,
    RustChannelUpdate,
    RustEntry,
    StaticUrlSource,
    StaticUrlUpdate,
    TyEntry,
    UvEntry,
    UvPythonSource,
    UvPythonUpdate,
    UpdateCandidate,
    UpdateKind,
    UpdateResult,
    UpdateStatus,
    UpdateTarget,
    OverridePolicy,
    BaseStage,
    ToolchainStage,
    RtkPrebuiltStage,
    FdPrebuiltStage,
    PiToolsStage,
    OpenSpecToolsStage,
    RuntimeStage,
    RuntimeValidation,
    Stages,
)
from docker.versioning.constraints import parse_constraint
from docker.versioning.inventory import load_inventory, load_inventory_raw
from docker.versioning.providers.base import (
    ProviderContext,
    ProviderResult,
    UpdateProvider,
)
from docker.versioning.providers.npm import NpmProvider
from docker.versioning.updates import (
    build_update_targets,
    check_updates,
    render_table,
    render_json,
    render_suggestions_json,
    render_replacement_fragments,
    Scope,
)
from tests.versioning.support.fake_http import FakeHttpTransport, FailingHttpTransport
from tests.versioning.support.fake_git import FakeGitTransport, FailingGitTransport


def _minimal_inventory():
    return Inventory(
        schema=1,
        stages=Stages(
            base=BaseStage(node=NodeEntry(
                tag="24-trixie-slim",
                digest="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                node_version="24.18.0", npm_version="11.16.0",
                source=DockerRegistrySource(registry="docker.io", repository="library/node"),
                update=DockerRegistryUpdate(stable_only=True, track="tag-digest"),
            )),
            toolchain=ToolchainStage(
                rust=RustEntry(
                    version="1.88.0", profile="minimal",
                    components=("rustfmt", "clippy"),
                    source=RustChannelSource(manifest="https://example.com/rust.toml"),
                    update=RustChannelUpdate(channel="stable", stable_only=True),
                    rustup={"linux-amd64": ArtifactEntry(url="https://example.com/rustup-init", sha256="a" * 64)},
                    rustup_source=StaticUrlSource(checksum_url="https://example.com/rustup-init.sha256"),
                    rustup_update=StaticUrlUpdate(stable_only=True),
                ),
                uv=UvEntry(
                    version="0.1.0",
                    artifacts={"linux-amd64": ArtifactEntry(url="https://example.com/uv.tar.gz", sha256="a" * 64)},
                    source=GitHubReleaseSource(repository="astral-sh/uv", tag="0.1.0"),
                    update=GitHubReleaseUpdate(stable_only=True, required_platforms=("linux-amd64",)),
                ),
                python=PythonEntry(
                    version="3.14.6",
                    source=UvPythonSource(implementation="cpython"),
                    update=UvPythonUpdate(implementation="cpython", stable_only=True),
                    override=OverridePolicy(constraint=parse_constraint(">=3.14.6"), allow_prerelease=False, scheme="numeric"),
                ),
                ty=TyEntry(
                    version="0.0.61",
                    source=PyPiSource(package="ty"),
                    update=PyPiUpdate(stable_only=True),
                ),
            ),
            rtk_prebuilt=RtkPrebuiltStage(rtk=PrebuiltToolEntry(
                version="v0.1.0",
                artifacts={"linux-amd64": ArtifactEntry(url="https://example.com/rtk.deb", sha256="a" * 64)},
                source=GitHubReleaseSource(repository="rtk-ai/rtk", tag="v0.1.0"),
                update=GitHubReleaseUpdate(stable_only=True, required_platforms=("linux-amd64",)),
            )),
            fd_prebuilt=FdPrebuiltStage(fd=PrebuiltToolEntry(
                version="v1.0.0",
                artifacts={"linux-amd64": ArtifactEntry(url="https://example.com/fd.deb", sha256="a" * 64)},
                source=GitHubReleaseSource(repository="sharkdp/fd", tag="v1.0.0"),
                update=GitHubReleaseUpdate(stable_only=True, required_platforms=("linux-amd64",)),
            )),
            pi_tools=PiToolsStage(pi=PiToolEntry(
                version="1.0.0",
                source=PiReleaseSource(
                    package="@scope/pkg",
                    release_repository="scope/pi",
                    release_tag_prefix="v",
                ),
                update=NpmUpdate(stable_only=True),
            )),
            openspec_tools=OpenSpecToolsStage(openspec=NpmToolEntry(
                version="1.0.0",
                source=NpmSource(package="@scope/openspec"),
                update=NpmUpdate(stable_only=True),
            )),
            runtime=RuntimeStage(oh_my_zsh=OhMyZshEntry(
                revision="a" * 40,
                source=GitSource(repository="https://github.com/ohmyzsh/ohmyzsh.git"),
                update=GitRefUpdate(ref="master"),
            )),
        ),
        runtime_pi_extensions={},
    )


class StubProvider:
    """A fake provider that returns a pre-configured result."""
    name = "stub"

    def __init__(self, result=None, exc=None):
        self._result = result
        self._exc = exc

    def discover(self, target, context):
        if self._exc is not None:
            raise self._exc
        if self._result is not None:
            return self._result
        return ProviderResult(
            candidate=UpdateCandidate(
                value=target.current, kind=UpdateKind.VERSION, artifacts={},
            )
        )


class TestTargetTraversal(unittest.TestCase):
    def test_all_targets_present(self):
        inv = _minimal_inventory()
        targets = build_update_targets(inv)
        paths = {t.path for t in targets}
        expected = {
            "build.stages.base.node",
            "build.stages.toolchain.rust",
            "build.stages.toolchain.rust.rustup",
            "build.stages.toolchain.uv",
            "build.stages.toolchain.python",
            "build.stages.toolchain.ty",
            "build.stages.rtk-prebuilt.rtk",
            "build.stages.fd-prebuilt.fd",
            "build.stages.pi-tools.pi",
            "build.stages.openspec-tools.openspec",
            "build.stages.runtime.oh-my-zsh",
        }
        self.assertEqual(paths, expected)

    def test_deterministic_ordering(self):
        inv = _minimal_inventory()
        t1 = build_update_targets(inv)
        t2 = build_update_targets(inv)
        self.assertEqual(
            [t.path for t in t1],
            [t.path for t in t2],
        )

    def test_pi_extensions_sorted(self):
        from types import MappingProxyType
        inv = Inventory(
            schema=1,
            stages=Stages(
                base=BaseStage(node=NodeEntry(
                    tag="t", digest="sha256:" + "a" * 64,
                    node_version="24.18.0", npm_version="11.16.0",
                    source=DockerRegistrySource(registry="r", repository="p"),
                    update=DockerRegistryUpdate(stable_only=True, track="tag-digest"),
                )),
                toolchain=ToolchainStage(
                    rust=RustEntry(version="1.0.0", profile="minimal", components=(), source=RustChannelSource(manifest="u"), update=RustChannelUpdate(channel="stable", stable_only=True), rustup={"linux-amd64": ArtifactEntry(url="u", sha256="a" * 64)}, rustup_source=StaticUrlSource(checksum_url="u.sha256"), rustup_update=StaticUrlUpdate(stable_only=True)),
                    uv=UvEntry(version="0.1.0", artifacts={}, source=GitHubReleaseSource(repository="r/r", tag="0.1.0"), update=GitHubReleaseUpdate(stable_only=True)),
                    python=PythonEntry(version="3.14.6", source=UvPythonSource(implementation="cpython"), update=UvPythonUpdate(implementation="cpython", stable_only=True)),
                    ty=TyEntry(version="1.0.0", source=PyPiSource(package="p"), update=PyPiUpdate(stable_only=True)),
                ),
                rtk_prebuilt=RtkPrebuiltStage(rtk=PrebuiltToolEntry(version="v1.0.0", artifacts={}, source=GitHubReleaseSource(repository="r", tag="v1.0.0"), update=GitHubReleaseUpdate(stable_only=True))),
                fd_prebuilt=FdPrebuiltStage(fd=PrebuiltToolEntry(version="v1.0.0", artifacts={}, source=GitHubReleaseSource(repository="r", tag="v1.0.0"), update=GitHubReleaseUpdate(stable_only=True))),
                pi_tools=PiToolsStage(pi=PiToolEntry(version="1.0.0", source=PiReleaseSource(package="p", release_repository="r/r", release_tag_prefix="v"), update=NpmUpdate(stable_only=True))),
                openspec_tools=OpenSpecToolsStage(openspec=NpmToolEntry(version="1.0.0", source=NpmSource(package="p"), update=NpmUpdate(stable_only=True))),
                runtime=RuntimeStage(oh_my_zsh=OhMyZshEntry(revision="a" * 40, source=GitSource(repository="r"), update=GitRefUpdate(ref="r"))),
            ),
            runtime_pi_extensions=MappingProxyType({
                "z-ext": PiExtensionEntry(version="1.0.0", source=NpmSource(package="z"), update=NpmUpdate(stable_only=True),
            artifacts={"1.0.0": NpmArtifact(url="https://registry.npmjs.org/z/-/z-1.0.0.tgz", integrity="sha512-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==")},
            validation=RuntimeValidation(metadata_file="package.json"),
            override=OverridePolicy(constraint=parse_constraint(">=1.0.0"), allow_prerelease=False, scheme="numeric")),
                "a-ext": PiExtensionEntry(version="1.0.0", source=NpmSource(package="a"), update=NpmUpdate(stable_only=True),
            artifacts={"1.0.0": NpmArtifact(url="https://registry.npmjs.org/a/-/a-1.0.0.tgz", integrity="sha512-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==")},
            validation=RuntimeValidation(metadata_file="package.json"),
            override=OverridePolicy(constraint=parse_constraint(">=1.0.0"), allow_prerelease=False, scheme="numeric")),
            }),
        )
        targets = build_update_targets(inv)
        ext_paths = [t.path for t in targets if "pi-extensions" in t.path]
        self.assertEqual(ext_paths, [
            "runtime.pi-extensions.a-ext",
            "runtime.pi-extensions.z-ext",
        ])


class TestCheckUpdates(unittest.TestCase):
    def setUp(self):
        self.http = FailingHttpTransport()
        self.git = FailingGitTransport()

    def _ctx(self, include_prerelease=False):
        return ProviderContext(
            http=self.http, git=self.git,
            include_prerelease=include_prerelease, tokens={},
        )

    def test_current(self):
        inv = _minimal_inventory()
        # All providers return current
        providers = {k: StubProvider() for k in [
            "docker-registry", "rust-channel", "github-release",
            "uv-python", "pypi", "npm", "git-ref", "static-url",
        ]}
        results = check_updates(inv, providers=providers, context=self._ctx())
        for r in results:
            self.assertEqual(r.status.value, "current", f"{r.path}: {r.status}")

    def test_outdated_applicable(self):
        inv = _minimal_inventory()
        providers = {
            "docker-registry": StubProvider(),
            "rust-channel": StubProvider(),
            "github-release": StubProvider(),
            "uv-python": StubProvider(),
            "pypi": StubProvider(
                ProviderResult(candidate=UpdateCandidate(
                    value="0.0.62", kind=UpdateKind.VERSION, artifacts={},
                ))
            ),
            "npm": StubProvider(),
            "git-ref": StubProvider(),
            "static-url": StubProvider(),
        }
        results = check_updates(inv, providers=providers, context=self._ctx())
        ty = [r for r in results if r.path == "build.stages.toolchain.ty"][0]
        self.assertEqual(ty.status, UpdateStatus.OUTDATED)
        self.assertTrue(ty.applicable)
        self.assertEqual(ty.candidate, "0.0.62")

    def test_npm_extension_incomplete_candidate(self):
        """A Pi-extension candidate missing dist data is INCOMPLETE, not applicable."""
        from dataclasses import replace
        from types import MappingProxyType
        inv = replace(
            _minimal_inventory(),
            runtime_pi_extensions=MappingProxyType({
                "ext": PiExtensionEntry(
                    version="1.0.0",
                    source=NpmSource(package="pkg"),
                    update=NpmUpdate(stable_only=True),
                    artifacts={"1.0.0": NpmArtifact(
                        url="https://registry.npmjs.org/pkg/-/pkg-1.0.0.tgz",
                        integrity="sha512-" + "A" * 86 + "==",
                    )},
                    validation=RuntimeValidation(metadata_file="package.json"),
                    override=OverridePolicy(
                        constraint=parse_constraint(">=1.0.0"),
                        allow_prerelease=False, scheme="numeric",
                    ),
                ),
            }),
        )
        providers = {
            "npm": StubProvider(ProviderResult(candidate=UpdateCandidate(
                value="2.0.0", kind=UpdateKind.VERSION, artifacts={},
                incomplete_reason="missing npm tarball URL",
            ))),
        }
        results = check_updates(
            inv, providers=providers, context=self._ctx(), only=("npm",),
        )
        ext = [r for r in results if r.path == "runtime.pi-extensions.ext"][0]
        self.assertEqual(ext.status, UpdateStatus.INCOMPLETE)
        self.assertFalse(ext.applicable)
        self.assertEqual(ext.reason, "missing npm tarball URL")

        # Build-stage npm tools ignore dist metadata: their version-only
        # update remains OUTDATED + applicable.
        build = [r for r in results if r.path == "build.stages.pi-tools.pi"][0]
        self.assertEqual(build.status, UpdateStatus.OUTDATED)
        self.assertTrue(build.applicable)

    def test_npm_extension_incomplete_is_not_suggested(self):
        """INCOMPLETE extension results never enter replacement blocks."""
        from dataclasses import replace
        from types import MappingProxyType
        inv = replace(
            _minimal_inventory(),
            runtime_pi_extensions=MappingProxyType({
                "ext": PiExtensionEntry(
                    version="1.0.0",
                    source=NpmSource(package="pkg"),
                    update=NpmUpdate(stable_only=True),
                    artifacts={"1.0.0": NpmArtifact(
                        url="https://registry.npmjs.org/pkg/-/pkg-1.0.0.tgz",
                        integrity="sha512-" + "A" * 86 + "==",
                    )},
                    validation=RuntimeValidation(metadata_file="package.json"),
                    override=OverridePolicy(
                        constraint=parse_constraint(">=1.0.0"),
                        allow_prerelease=False, scheme="numeric",
                    ),
                ),
            }),
        )
        providers = {
            "npm": StubProvider(ProviderResult(candidate=UpdateCandidate(
                value="2.0.0", kind=UpdateKind.VERSION, artifacts={},
                incomplete_reason="missing npm integrity",
            ))),
        }
        results = check_updates(
            inv, providers=providers, context=self._ctx(), only=("npm",),
        )
        from docker.versioning.updates import build_replacement_blocks, serialize_suggestions
        targets = build_update_targets(inv)
        ext_result = [r for r in results if r.path == "runtime.pi-extensions.ext"][0]
        # The INCOMPLETE result is filtered out before any raw block is
        # extracted or any fragment is emitted.
        blocks = build_replacement_blocks({}, targets, [ext_result])
        self.assertEqual(blocks, ())
        suggestions = serialize_suggestions([ext_result])
        self.assertEqual(suggestions, [])

    def test_skipped_unknown_provider(self):
        inv = _minimal_inventory()
        results = check_updates(inv, providers={}, context=self._ctx())
        self.assertTrue(any(r.status == UpdateStatus.SKIPPED for r in results))

    def test_unavailable_provider_error(self):
        inv = _minimal_inventory()
        providers = {
            "docker-registry": StubProvider(),
            "rust-channel": StubProvider(),
            "github-release": StubProvider(),
            "uv-python": StubProvider(),
            "pypi": StubProvider(exc=RuntimeError("network error")),
            "npm": StubProvider(),
            "git-ref": StubProvider(),
            "static-url": StubProvider(),
        }
        results = check_updates(inv, providers=providers, context=self._ctx())
        ty = [r for r in results if r.path == "build.stages.toolchain.ty"][0]
        self.assertEqual(ty.status, UpdateStatus.UNAVAILABLE)
        self.assertIn("network error", ty.reason)

    def test_provider_exception_isolation(self):
        """One provider crashing should not stop others."""
        inv = _minimal_inventory()
        providers = {
            "docker-registry": StubProvider(exc=RuntimeError("dead!")),
            "rust-channel": StubProvider(exc=RuntimeError("dead!")),
            "github-release": StubProvider(exc=RuntimeError("dead!")),
            "uv-python": StubProvider(exc=RuntimeError("dead!")),
            "pypi": StubProvider(exc=RuntimeError("dead!")),
            "npm": StubProvider(),
            "git-ref": StubProvider(exc=RuntimeError("dead!")),
            "static-url": StubProvider(exc=RuntimeError("dead!")),
        }
        results = check_updates(inv, providers=providers, context=self._ctx())
        self.assertEqual(len(results), 11)  # All targets present
        available = [r for r in results if r.status != UpdateStatus.UNAVAILABLE]
        self.assertTrue(len(available) > 0)

    def test_only_filter_provider(self):
        inv = _minimal_inventory()
        providers = {k: StubProvider() for k in [
            "docker-registry", "rust-channel", "github-release",
            "uv-python", "pypi", "npm", "git-ref", "static-url",
        ]}
        results = check_updates(inv, providers=providers, context=self._ctx(), only=("npm",))
        self.assertTrue(all(r.provider == "npm" for r in results))
        self.assertEqual(len(results), 2)  # pi-tools.pi + openspec-tools.openspec

    def test_only_filter_path(self):
        inv = _minimal_inventory()
        providers = {k: StubProvider() for k in [
            "docker-registry", "rust-channel", "github-release",
            "uv-python", "pypi", "npm", "git-ref", "static-url",
        ]}
        results = check_updates(inv, providers=providers, context=self._ctx(),
                                 only=("build.stages.toolchain.python",))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].path, "build.stages.toolchain.python")

    def test_deterministic_json(self):
        inv = _minimal_inventory()
        providers = {k: StubProvider() for k in [
            "docker-registry", "rust-channel", "github-release",
            "uv-python", "pypi", "npm", "git-ref", "static-url",
        ]}
        results = check_updates(inv, providers=providers, context=self._ctx())
        j1 = render_json(results)
        j2 = render_json(results)
        self.assertEqual(j1, j2)
        data = json.loads(j1)
        self.assertIn("results", data)
        self.assertEqual(len(data["results"]), 11)

    def test_invalid_scope_value_error(self) -> None:
        """Passing a string or other non-Scope value to check_updates()
        must raise TypeError — the API boundary validates its inputs."""
        inv = _minimal_inventory()
        with self.assertRaises(TypeError) as ctx:
            check_updates(
                inv,
                context=self._ctx(),
                scope="invalid",
            )
        self.assertIn("Scope", str(ctx.exception))

    def test_scope_none_rejected(self) -> None:
        """None is not a valid scope."""
        inv = _minimal_inventory()
        with self.assertRaises(TypeError):
            check_updates(inv, context=self._ctx(), scope=None)


class TestCheckUpdatesProgress(unittest.TestCase):
    """The coordinator emits one presentation-neutral start event per
    selected target before resolving it."""

    def setUp(self) -> None:
        self.http = FailingHttpTransport()
        self.git = FailingGitTransport()

    def _ctx(self, include_prerelease=False):
        return ProviderContext(
            http=self.http, git=self.git,
            include_prerelease=include_prerelease, tokens={},
        )

    def _providers(self):
        return {k: StubProvider() for k in [
            "docker-registry", "rust-channel", "github-release",
            "uv-python", "pypi", "npm", "git-ref", "static-url",
        ]}

    def test_event_fields_and_one_based_index(self) -> None:
        inv = _minimal_inventory()
        events: list[object] = []
        results = check_updates(
            inv, providers=self._providers(), context=self._ctx(),
            progress=events.append,
        )
        self.assertEqual(len(events), 11)
        self.assertEqual(len(results), 11)
        first = events[0]
        self.assertEqual(first.index, 1)  # type: ignore[attr-defined]
        self.assertEqual(first.total, 11)  # type: ignore[attr-defined]
        self.assertEqual(first.path, "build.stages.base.node")  # type: ignore[attr-defined]
        self.assertEqual(first.provider, "docker-registry")  # type: ignore[attr-defined]
        last = events[-1]
        self.assertEqual(last.index, 11)  # type: ignore[attr-defined]
        self.assertEqual(last.total, 11)  # type: ignore[attr-defined]
        self.assertEqual(last.path, "build.stages.runtime.oh-my-zsh")  # type: ignore[attr-defined]
        self.assertEqual(last.provider, "git-ref")  # type: ignore[attr-defined]

    def test_scope_filter_determines_event_total_and_order(self) -> None:
        inv = _minimal_inventory()
        events: list[object] = []
        check_updates(
            inv, providers=self._providers(), context=self._ctx(),
            scope=Scope.BUILD, progress=events.append,
        )
        self.assertTrue(events)
        self.assertTrue(all(e.path.startswith("build.") for e in events))
        self.assertEqual(events[0].total, len(events))  # type: ignore[attr-defined]
        self.assertEqual(
            [e.index for e in events],
            list(range(1, len(events) + 1)),
        )

    def test_only_filter_determines_event_total_and_order(self) -> None:
        inv = _minimal_inventory()
        events: list[object] = []
        check_updates(
            inv, providers=self._providers(), context=self._ctx(),
            only=("npm",), progress=events.append,
        )
        self.assertEqual(
            [e.path for e in events],
            ["build.stages.pi-tools.pi", "build.stages.openspec-tools.openspec"],
        )
        self.assertTrue(all(e.provider == "npm" for e in events))
        self.assertEqual(events[0].total, 2)  # type: ignore[attr-defined]
        self.assertEqual(events[0].index, 1)  # type: ignore[attr-defined]
        self.assertEqual(events[1].index, 2)  # type: ignore[attr-defined]

    def test_provider_failure_yields_one_event_and_one_result(self) -> None:
        inv = _minimal_inventory()
        events: list[object] = []
        providers = self._providers()
        providers["pypi"] = StubProvider(exc=RuntimeError("network error"))
        results = check_updates(
            inv, providers=providers, context=self._ctx(),
            progress=events.append,
        )
        ty_events = [e for e in events if e.path == "build.stages.toolchain.ty"]
        ty_results = [r for r in results if r.path == "build.stages.toolchain.ty"]
        self.assertEqual(len(ty_events), 1)
        self.assertEqual(len(ty_results), 1)
        self.assertEqual(ty_results[0].status, UpdateStatus.UNAVAILABLE)

    def test_missing_callback_preserves_behavior(self) -> None:
        inv = _minimal_inventory()
        providers = self._providers()
        with_cb = check_updates(
            inv, providers=providers, context=self._ctx(),
            progress=lambda e: None,
        )
        without_cb = check_updates(
            inv, providers=providers, context=self._ctx(),
        )
        self.assertEqual(
            [(r.path, r.status, r.candidate) for r in with_cb],
            [(r.path, r.status, r.candidate) for r in without_cb],
        )

    def test_sequential_ordering_and_event_before_resolution(self) -> None:
        inv = _minimal_inventory()
        log: list[str] = []

        class RecordingProvider:
            name = "npm"

            def discover(self, target, context):
                log.append(f"start:{target.path}")
                log.append(f"end:{target.path}")
                return ProviderResult(
                    candidate=UpdateCandidate(
                        value=target.current, kind=UpdateKind.VERSION,
                        artifacts={},
                    ),
                )

        results = check_updates(
            inv,
            providers={"npm": RecordingProvider()},
            context=self._ctx(),
            only=("npm",),
            progress=lambda e: log.append(
                f"event:{e.path}:{e.index}/{e.total}"
            ),
        )
        self.assertEqual(log, [
            "event:build.stages.pi-tools.pi:1/2",
            "start:build.stages.pi-tools.pi",
            "end:build.stages.pi-tools.pi",
            "event:build.stages.openspec-tools.openspec:2/2",
            "start:build.stages.openspec-tools.openspec",
            "end:build.stages.openspec-tools.openspec",
        ])
        self.assertEqual(
            [r.path for r in results],
            ["build.stages.pi-tools.pi", "build.stages.openspec-tools.openspec"],
        )


class TestSuggestions(unittest.TestCase):
    def test_render_suggestions_json(self):
        results = [
            UpdateResult(
                path="build.stages.toolchain.ty",
                provider="pypi",
                current="0.0.61",
                candidate="0.0.62",
                status=UpdateStatus.OUTDATED,
                kind=UpdateKind.VERSION,
                applicable=True,
                reason=None,
                artifacts={},
            ),
        ]
        output = render_suggestions_json(results)
        data = json.loads(output)
        self.assertIn("suggestions", data)
        self.assertEqual(len(data["suggestions"]), 1)


class TestTableRendering(unittest.TestCase):
    def test_table_header(self):
        results: list[UpdateResult] = []
        table = render_table(results)
        self.assertIn("PATH", table)
        self.assertIn("STATUS", table)

    def test_table_contains_results(self):
        results = [
            UpdateResult(
                path="test.path",
                provider="test",
                current="1.0.0",
                candidate="2.0.0",
                status=UpdateStatus.OUTDATED,
                kind=UpdateKind.VERSION,
                applicable=True,
                reason=None,
                artifacts={},
            ),
        ]
        table = render_table(results)
        self.assertIn("test.path", table)
        self.assertIn("2.0.0", table)


class TestPiReleaseNpmDiscovery(unittest.TestCase):
    """The canonical Pi target discovers versions through its npm package
    identity while retaining its dedicated ``pi-release`` installation
    metadata in generated replacement fragments."""

    def setUp(self) -> None:
        self.http = FakeHttpTransport()
        self.git = FakeGitTransport()
        self.root = pathlib.Path(__file__).resolve().parents[1]
        self.inventory_path = self.root / "docker-constructor.toml"

    def _ctx(self) -> ProviderContext:
        return ProviderContext(
            http=self.http, git=self.git,
            include_prerelease=False, tokens={},
        )

    def test_pi_target_is_outdated_and_fragment_retains_release_metadata(
        self,
    ) -> None:
        inventory = load_inventory(self.inventory_path)
        pi = inventory.stages.pi_tools.pi
        self.assertEqual("pi-release", pi.source.type)
        current = pi.version
        package = pi.source.package

        # Candidate strictly newer than the canonical current version.
        core = current.split("-", 1)[0].split("+", 1)[0].split(".")
        core[-1] = str(int(core[-1]) + 1)
        candidate = ".".join(core)

        from urllib.parse import quote
        encoded = quote(package, safe="")
        self.http.set(
            "GET",
            f"https://registry.npmjs.org/{encoded}",
            status=200,
            body=json.dumps({
                "versions": {
                    current: {"version": current},
                    candidate: {"version": candidate},
                },
                "time": {candidate: "2026-01-02T03:04:05Z"},
            }).encode(),
        )

        targets = build_update_targets(inventory)
        results = check_updates(
            inventory,
            providers={"npm": NpmProvider()},
            context=self._ctx(),
            only=("build.stages.pi-tools.pi",),
        )
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual("build.stages.pi-tools.pi", result.path)
        self.assertEqual(UpdateStatus.OUTDATED, result.status)
        self.assertTrue(result.applicable)
        self.assertEqual(candidate, result.candidate)
        self.assertEqual("2026-01-02T03:04:05Z", result.published_at)

        raw = load_inventory_raw(self.inventory_path)
        fragments = render_replacement_fragments(raw, targets, results)
        self.assertIn('type = "pi-release"', fragments)
        self.assertIn('release_repository = "earendil-works/pi"', fragments)
        self.assertIn('release_tag_prefix = "v"', fragments)
        self.assertIn(f'version = "{candidate}"', fragments)
        self.assertIn(f'package = "{package}"', fragments)

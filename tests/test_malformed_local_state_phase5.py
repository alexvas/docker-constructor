"""Malformed local-companion branches: diagnostics and effect ordering.

The removed ``runtime-host-access`` scenario "Rejecting malformed local state"
is broader than malformed TOML syntax. This module covers every local-document
failure branch -- unknown top-level or nested key, malformed syntax, invalid
host address, invalid cache value, invalid corporate-trust value, and invalid
network proxy -- and proves each:

* is rejected with the exact structured field path (``local.<key>``), or the
  fixed ``malformed_toml`` classification for a syntax failure;
* never leaks a rejected value;
* fails *before* network access, cache mutation, artifact materialization or
  publication, and Docker/container execution.

Effect boundaries are probed with fail-on-call doubles (an exploding
``urllib.request.urlopen``, a recording executor, a patched artifact
materializer, a patched ``execute_build``) plus filesystem assertions that the
cache root was never created.
"""
from __future__ import annotations

import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from docker.versioning.configuration_document_validation import (
    ConfigurationDocumentError,
    DocumentErrorClassification,
)
from docker.versioning.errors import InventoryError
from docker.versioning.local_project_configuration import (
    load_local_project_configuration,
    validate_local_document,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CANONICAL = (_REPO_ROOT / "docker-constructor.toml").read_text(encoding="utf-8")

# The rejected secret in the proxy branch: it must never reach any diagnostic.
_PROXY_SECRET = "rejected-proxy-secret-9c3f"

# branch_id -> (companion body, expected field path; ``None`` means a syntax
# failure whose only identity is the ``malformed_toml`` classification)
BRANCHES: dict[str, tuple[str, str | None]] = {
    "unknown_top_level_key": (
        '[observability]\ndir = "/tmp/local-output"\n',
        "local.observability",
    ),
    "unknown_nested_key": (
        '[host-access]\nfoo = "bar"\n',
        "local.host-access.foo",
    ),
    "malformed_syntax": (
        "not valid toml [[[\n",
        None,
    ),
    "invalid_host_address": (
        '[host-access]\naddress = "not-an-ip"\n',
        "local.host-access.address",
    ),
    "invalid_cache_dir": (
        "[cache]\ndir = 5\n",
        "local.cache.dir",
    ),
    "invalid_corporate_trust": (
        '[corporate-trust]\nenabled = "yes"\n',
        "local.corporate-trust.enabled",
    ),
    "invalid_network_proxy": (
        f'[network.proxy]\nurl = "ftp://{_PROXY_SECRET}:3128"\n',
        "local.network.proxy.url",
    ),
}

# Actionable recovery guidance the owning parser must carry for branches whose
# spec scenario requires the user to be told how to fix the value.
_RECOVERY_GUIDANCE: dict[str, str] = {
    "invalid_host_address": "correct [host-access].address",
    "invalid_cache_dir": "set [cache].dir",
    "invalid_corporate_trust": "set enabled = true",
    "invalid_network_proxy": "use http, socks5, or socks5h",
}


class _RecordingExecutor:
    """Fail-closed Docker/container execution probe."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: tuple[str, ...], *, interactive: bool = False):
        from docker.launcher import ProcessResult

        self.calls.append(tuple(argv))
        return ProcessResult(argv=tuple(argv), return_code=0, stdout="", stderr="")


class _NoContainersInspector:
    def list_names(self) -> set[str]:
        return set()


class _MalformedStateHarness(unittest.TestCase):
    """A canonical project with an isolated HOME/XDG and a writable companion."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="malformed-local-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        self.inventory = self.checkout / "docker-constructor.toml"
        self.inventory.write_text(_CANONICAL, encoding="utf-8")
        (self.checkout / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        self.companion = self.checkout / "docker-constructor.local.toml"
        self.home = self.root / "home"
        self.home.mkdir()
        self.xdg = self.root / "xdg"
        self._saved_env = {
            key: os.environ.get(key) for key in ("HOME", "XDG_CACHE_HOME")
        }
        os.environ["HOME"] = str(self.home)
        os.environ["XDG_CACHE_HOME"] = str(self.xdg)
        self.addCleanup(self._restore_env)

    def _restore_env(self) -> None:
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _write_branch(self, branch_id: str) -> None:
        body, _ = BRANCHES[branch_id]
        self.companion.write_text(body, encoding="utf-8")

    # ── assertions ───────────────────────────────────────────────────

    def _assert_diagnostic(self, branch_id: str) -> None:
        body, expected_field = BRANCHES[branch_id]
        self.companion.write_text(body, encoding="utf-8")
        with self.assertRaises(InventoryError) as raised:
            load_local_project_configuration(self.companion)
        error = raised.exception

        self.assertNotIn(
            _PROXY_SECRET, str(error), "a rejected value must never be published"
        )

        if expected_field is None:
            self.assertIsInstance(error, ConfigurationDocumentError)
            assert isinstance(error, ConfigurationDocumentError)
            self.assertIs(
                DocumentErrorClassification.MALFORMED_TOML, error.classification
            )
            self.assertIn("malformed TOML", str(error))
        else:
            self.assertEqual(expected_field, error.field)
            self.assertIn(
                expected_field,
                str(error),
                "the published diagnostic must name the offending field path",
            )

        if branch_id in _RECOVERY_GUIDANCE:
            # The owner message (pre-projection) is the recovery guidance.
            with self.assertRaises(InventoryError) as owner_raised:
                validate_local_document(tomllib.loads(body))
            self.assertIn(
                _RECOVERY_GUIDANCE[branch_id],
                str(owner_raised.exception),
                "the owner failure must carry actionable recovery guidance",
            )

    def _assert_run_fails_before_effects(self, branch_id: str) -> None:
        from docker.launcher import RunRequest, WorkspaceSelection, orchestrate_run

        self._write_branch(branch_id)
        executor = _RecordingExecutor()
        fetchers: list[str] = []

        def fetch(url: str) -> bytes:
            fetchers.append(url)
            return b"should-not-be-fetched"

        with mock.patch(
            "docker.versioning.artifact_cache.materialize_selected_artifacts",
        ) as materialize, mock.patch(
            "urllib.request.urlopen",
            side_effect=AssertionError("network access reached"),
        ):
            result = orchestrate_run(
                RunRequest(
                    inventory_path=str(self.inventory),
                    image="test-image",
                    selection=WorkspaceSelection(workspace="/work/project"),
                    pi_home_host=str(self.root / "pi-home"),
                    repo_root=str(self.checkout),
                    project_root=self.checkout,
                    _artifact_fetcher=fetch,
                    executor=executor,
                    inspector=_NoContainersInspector(),
                )
            )

        self.assertEqual("config", result.exit_kind.value, result.message)
        materialize.assert_not_called()
        self.assertEqual([], fetchers, "network access must not be reached")
        self.assertEqual([], executor.calls, "container execution must not run")
        self.assertFalse(self.xdg.exists(), "default cache root must not be created")
        self.assertFalse(
            (self.checkout / "runtime-artifacts").exists(),
            "no cache child may be created",
        )

    def _assert_build_fails_before_docker(self, branch_id: str) -> None:
        from docker.versioning.build_orchestration import (
            BuildRequest,
            orchestrate_build,
        )

        self._write_branch(branch_id)
        with mock.patch(
            "docker.versioning.build_orchestration.execute_build",
        ) as execute:
            result = orchestrate_build(
                BuildRequest(
                    inventory_path=str(self.inventory),
                    repo_root=str(self.checkout),
                    project_root=str(self.checkout),
                    context=str(self.checkout),
                    dockerfile=str(self.checkout / "Dockerfile"),
                    platform="linux-amd64",
                    tag=None,
                    overrides={},
                    cache=True,
                    pull=False,
                    progress="auto",
                    uid=None,
                    gid=None,
                    confirmed=True,
                    dry_run=False,
                )
            )

        self.assertEqual("config", result.exit_kind.value, result.message)
        execute.assert_not_called()
        self.assertFalse(self.xdg.exists(), "default cache root must not be created")

    def _assert_branch(self, branch_id: str) -> None:
        self._assert_diagnostic(branch_id)
        self._assert_run_fails_before_effects(branch_id)
        self._assert_build_fails_before_docker(branch_id)


class TestMalformedLocalStateBranches(_MalformedStateHarness):
    """Each malformed-state input branch reports its path and blocks effects."""

    def test_unknown_top_level_key_reports_field_and_blocks_effects(self) -> None:
        self._assert_branch("unknown_top_level_key")

    def test_unknown_nested_key_reports_field_and_blocks_effects(self) -> None:
        self._assert_branch("unknown_nested_key")

    def test_malformed_syntax_reports_classification_and_blocks_effects(self) -> None:
        self._assert_branch("malformed_syntax")

    def test_invalid_host_address_reports_field_and_blocks_effects(self) -> None:
        self._assert_branch("invalid_host_address")

    def test_invalid_cache_dir_reports_field_and_blocks_effects(self) -> None:
        self._assert_branch("invalid_cache_dir")

    def test_invalid_corporate_trust_reports_field_and_blocks_effects(self) -> None:
        self._assert_branch("invalid_corporate_trust")

    def test_invalid_network_proxy_reports_field_and_blocks_effects(self) -> None:
        self._assert_branch("invalid_network_proxy")


if __name__ == "__main__":
    unittest.main()

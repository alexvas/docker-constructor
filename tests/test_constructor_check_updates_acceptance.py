"""End-to-end check-updates acceptance tests (no network, no Docker).

Each case drives the real CLI facade (``constructor_cli.main``) through
the real dispatcher, the real inventory, and a patched no-network
provider registry, then asserts the final stdout/stderr/exit-code
contract for compact output, detailed output with suggestions,
interactive progress, and redirected JSON.
"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from typing import Any
from unittest.mock import patch


def _load_mod() -> Any:
    from docker import constructor_cli
    return constructor_cli


def _run(
    mod: Any,
    argv: list[str],
    *,
    stdout_isatty: bool = False,
    stderr_isatty: bool = False,
) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = mod.main(
            argv,
            stdout_isatty=lambda: stdout_isatty,
            stderr_isatty=lambda: stderr_isatty,
        )
    return rc, out.getvalue(), err.getvalue()


_PROVIDER_NAMES = (
    "docker-registry", "rust-channel", "static-url",
    "github-release", "uv-python", "pypi", "npm", "git-ref",
)


def _stub_providers(
    *,
    failing: frozenset[str] = frozenset(),
    candidates: dict[str, str] | None = None,
) -> Any:
    """Return a frozen no-network provider registry.

    Providers named in *failing* raise ``RuntimeError("network
    unreachable")`` during discovery.  Providers named in *candidates*
    return that distinct candidate value (making them OUTDATED and
    applicable).  Every other provider returns the target's current
    value, producing a CURRENT result.
    """
    from types import MappingProxyType
    from docker.versioning.model import UpdateCandidate, UpdateKind
    from docker.versioning.providers.base import ProviderResult

    cands = candidates or {}

    class _Stub:
        def __init__(self, name: str, fail: bool, candidate_value: str | None):
            self.name = name
            self._fail = fail
            self._candidate_value = candidate_value

        def discover(self, target: Any, context: Any) -> ProviderResult:
            if self._fail:
                raise RuntimeError("network unreachable")
            value = self._candidate_value
            if value is None:
                value = getattr(target, "current", "")
            return ProviderResult(
                candidate=UpdateCandidate(
                    value=value,
                    kind=UpdateKind.VERSION,
                    artifacts={},
                ),
            )

    return MappingProxyType({
        name: _Stub(name, name in failing, cands.get(name))
        for name in _PROVIDER_NAMES
    })


class TestCheckUpdatesAcceptance(unittest.TestCase):
    """5.1 — end-to-end CLI acceptance for check-updates presentation."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = _load_mod()

    def _run_stub(
        self,
        argv: list[str],
        *,
        stdout_isatty: bool = False,
        stderr_isatty: bool = False,
        failing: frozenset[str] = frozenset(),
        candidates: dict[str, str] | None = None,
    ) -> tuple[int, str, str]:
        import docker.versioning.updates
        with patch.object(
            docker.versioning.updates, "_DEFAULT_PROVIDERS",
            _stub_providers(failing=failing, candidates=candidates),
        ):
            return _run(
                self.m, argv,
                stdout_isatty=stdout_isatty,
                stderr_isatty=stderr_isatty,
            )

    def test_default_compact_output_with_provider_failure(self) -> None:
        rc, out, err = self._run_stub(
            ["check-updates"], failing=frozenset({"pypi"}),
        )
        self.assertEqual(0, rc)
        self.assertEqual("", err)
        for header in ("TARGET", "PROVIDER", "CURR -> NEXT", "STATUS",
                       "PUBLISHED"):
            self.assertIn(header, out)
        self.assertIn("unavailable", out)
        self.assertIn("Details:", out)
        self.assertIn("toolchain.ty: network unreachable", out)
        # The failure reason stays out of the table body.
        self.assertNotIn("network unreachable", out.split("Details:")[0])

    def test_details_with_suggest(self) -> None:
        rc, out, err = self._run_stub(
            ["check-updates", "--details", "--suggest"],
            candidates={"pypi": "1.2.3"},
        )
        self.assertEqual(0, rc)
        self.assertEqual("", err)
        for header in ("PATH", "PROVIDER", "CURRENT", "CANDIDATE", "STATUS",
                       "KIND", "APPLICABLE", "PUBLISHED", "DETAIL"):
            self.assertIn(header, out)
        # Detailed mode retains full paths and the new candidate value.
        self.assertIn("build.stages.toolchain.ty", out)
        self.assertIn("1.2.3", out)
        # Review-only suggestion section with its replacement fragment.
        self.assertIn("─── manual replacement blocks", out)
        self.assertIn("[build.stages.toolchain.ty]", out)
        self.assertIn('version = "1.2.3"', out)

    def test_interactive_progress_then_clean_report(self) -> None:
        rc, out, err = self._run_stub(
            ["check-updates"], stderr_isatty=True,
        )
        self.assertEqual(0, rc)
        self.assertIn("TARGET", out)
        self.assertIn(
            "Checking updates [1/14] base.node (docker-registry)…", err,
        )
        self.assertTrue(err.endswith("\r\x1b[K"))
        self.assertNotIn("\n", err)

    def test_redirected_json_has_no_progress(self) -> None:
        rc, out, err = self._run_stub(
            ["--output", "json", "check-updates"], stderr_isatty=False,
        )
        self.assertEqual(0, rc)
        self.assertEqual("", err)
        payload = json.loads(out)
        self.assertEqual("check-updates", payload["command"])
        self.assertEqual("success", payload["status"])
        self.assertIn("results", payload["data"])

"""RED tests for Stage 8.2 — scoped read-only commands.

validate / show / check-updates with ``build`` | ``runtime`` | ``all``
scope selection, effective display, provider/path filters, suggestions,
prerelease, cache controls, policy exits, and JSON output.

All tests inject fake dispatchers — no network, no Docker, no real
inventory parsing.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from typing import Any, Sequence

# ── module loader ──────────────────────────────────────────────────────


def _load_mod() -> Any:
    """Import the importable facade module."""
    from docker import constructor_cli

    return constructor_cli


# ── helpers ────────────────────────────────────────────────────────────


def _run(
    mod: Any,
    argv: Sequence[str],
    *,
    dispatcher: Any = None,
    stdout_isatty: bool = False,
    stderr_isatty: bool = False,
) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = mod.main(
            list(argv),
            dispatcher=dispatcher,
            stdout_isatty=lambda: stdout_isatty,
            stderr_isatty=lambda: stderr_isatty,
        )
    return rc, out.getvalue(), err.getvalue()


def _make_fake(
    mod: Any,
    *,
    exit_kind: str = "success",
    data: object = None,
    message: str | None = None,
    debug: str | None = None,
) -> Any:
    class Fake:
        def execute(self, command: str, request: Any) -> Any:
            Fake.command = command
            Fake.request = request
            return mod.CommandResult(
                exit_kind=getattr(mod.ExitKind, exit_kind.upper()),
                data=data,
                message=message,
                debug=debug,
            )

    return Fake()


def _make_recording_fake(mod: Any) -> Any:
    class Recording:
        def __init__(self) -> None:
            self.calls: list[tuple[str, Any]] = []

        def execute(self, command: str, request: Any) -> Any:
            self.calls.append((command, request))
            return mod.CommandResult(
                exit_kind=mod.ExitKind.SUCCESS,
                data={"ok": True},
            )

    return Recording()


# ════════════════════════════════════════════════════════════════════════
# Scope enum / DTO
# ════════════════════════════════════════════════════════════════════════


class TestScopeParsing(unittest.TestCase):
    """The facade parses and validates scope strings; internal services own the enum."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = _load_mod()

    def test_scope_is_passed_as_validated_string(self) -> None:
        """Scope crosses the dispatcher boundary as a plain string, not an enum."""
        fake = _make_recording_fake(self.m)
        _run(self.m, ["validate", "--scope", "build"], dispatcher=fake)
        scope = fake.calls[0][1].command_args["scope"]
        self.assertIsInstance(scope, str)
        self.assertEqual("build", scope)

    def test_default_scope_is_all_string(self) -> None:
        fake = _make_recording_fake(self.m)
        _run(self.m, ["validate"], dispatcher=fake)
        self.assertEqual("all", fake.calls[0][1].command_args["scope"])


# ════════════════════════════════════════════════════════════════════════
# validate — scope selection and structured results
# ════════════════════════════════════════════════════════════════════════


class TestValidateScope(unittest.TestCase):
    """``validate`` command accepts --scope and passes it through."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = _load_mod()

    def test_validate_default_scope_is_all(self) -> None:
        fake = _make_recording_fake(self.m)
        _run(self.m, ["validate"], dispatcher=fake)
        self.assertEqual(1, len(fake.calls))
        self.assertEqual("all", fake.calls[0][1].command_args["scope"])

    def test_validate_scope_build(self) -> None:
        fake = _make_recording_fake(self.m)
        _run(self.m, ["validate", "--scope", "build"], dispatcher=fake)
        self.assertEqual("build", fake.calls[0][1].command_args["scope"])

    def test_validate_scope_runtime(self) -> None:
        fake = _make_recording_fake(self.m)
        _run(self.m, ["validate", "--scope", "runtime"], dispatcher=fake)
        self.assertEqual("runtime", fake.calls[0][1].command_args["scope"])

    def test_validate_scope_all(self) -> None:
        fake = _make_recording_fake(self.m)
        _run(self.m, ["validate", "--scope", "all"], dispatcher=fake)
        self.assertEqual("all", fake.calls[0][1].command_args["scope"])

    def test_validate_invalid_scope_exits_cli(self) -> None:
        rc, _, err = _run(self.m, ["validate", "--scope", "frob"])
        self.assertEqual(2, rc)  # ExitKind.CLI
        self.assertIn("invalid choice", err.lower())

    def test_validate_no_network_no_docker(self) -> None:
        """validate must never invoke network or Docker."""
        import socket, subprocess
        from unittest.mock import patch

        mod = _load_mod()
        with patch.object(socket, "socket", side_effect=RuntimeError("no network")):
            with patch.object(
                subprocess, "run", side_effect=RuntimeError("no subprocess")
            ):
                fake = _make_fake(mod, exit_kind="success")
                rc, out, _ = _run(mod, ["validate"], dispatcher=fake)
                self.assertEqual(0, rc)
                self.assertIn("[SUCCESS]", out)

    def test_validate_source_inventory_byte_identical(self) -> None:
        """Full inventory validation does not mutate the source TOML."""
        pass  # structural — exercised after GREEN wiring


class TestValidateStructuredResult(unittest.TestCase):
    """validate produces structured results with scope & diagnostics."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = _load_mod()

    def test_validate_success_result(self) -> None:
        data = {
            "scope": "all",
            "inventory": "/tmp/t.toml",
            "valid": True,
            "diagnostics": [],
        }
        fake = _make_fake(
            self.m, exit_kind="success", data=data, message="validation passed"
        )
        _, out, _ = _run(self.m, ["validate"], dispatcher=fake)
        self.assertIn("validation passed", out)

    def test_validate_json_result_contains_scope_and_path(self) -> None:
        data = {
            "scope": "build",
            "inventory": "/tmp/t.toml",
            "valid": True,
            "diagnostics": [],
        }
        fake = _make_fake(self.m, exit_kind="success", data=data, message="ok")
        _, out, _ = _run(self.m, ["--output", "json", "validate"], dispatcher=fake)
        payload = json.loads(out)
        self.assertEqual(
            {
                "scope": "build",
                "inventory": "/tmp/t.toml",
                "valid": True,
                "diagnostics": [],
            },
            payload["data"],
        )

    def test_validate_failure_with_diagnostics_on_stderr(self) -> None:
        data = {
            "scope": "runtime",
            "inventory": "/tmp/t.toml",
            "valid": False,
            "diagnostics": [
                {
                    "path": "runtime.pi-extensions.myext.version",
                    "problem": "missing version",
                },
                {
                    "path": "runtime.pi-extensions.other.version",
                    "problem": "invalid semver",
                },
            ],
        }
        fake = _make_fake(
            self.m, exit_kind="config", data=data, message="validation failed"
        )
        _, out, err = _run(self.m, ["validate", "--scope", "runtime"], dispatcher=fake)
        self.assertEqual("", out)
        self.assertIn("validation failed", err)

    def test_validate_json_failure_includes_diagnostics(self) -> None:
        data = {
            "scope": "runtime",
            "inventory": "/tmp/t.toml",
            "valid": False,
            "diagnostics": [{"path": "x", "problem": "y"}],
        }
        fake = _make_fake(self.m, exit_kind="config", data=data, message="fail")
        _, out, _ = _run(self.m, ["--output", "json", "validate"], dispatcher=fake)
        payload = json.loads(out)
        self.assertEqual("config", payload["status"])
        self.assertIn("diagnostics", payload["data"])
        self.assertEqual(1, len(payload["data"]["diagnostics"]))

    def test_validate_does_not_delete_unselected_section(self) -> None:
        """Validate build only — runtime data must not be discarded."""
        data = {
            "scope": "build",
            "inventory": "/tmp/t.toml",
            "valid": True,
            "diagnostics": [],
        }
        fake = _make_fake(self.m, exit_kind="success", data=data, message="build ok")
        _, out, _ = _run(self.m, ["validate", "--scope", "build"], dispatcher=fake)
        self.assertIn("build ok", out)
        # No runtime info in output (scope=build), but runtime is NOT
        # stripped from the underlying inventory — that's verified by
        # the dispatcher itself in GREEN.

    def test_validate_path_aware_diagnostics(self) -> None:
        """Diagnostics reference the full TOML dotted path."""
        data = {
            "scope": "all",
            "inventory": "/tmp/t.toml",
            "valid": False,
            "diagnostics": [
                {
                    "path": "build.stages.toolchain.rust.version",
                    "problem": "unknown version",
                },
            ],
        }
        fake = _make_fake(self.m, exit_kind="config", data=data, message="fail")
        _, out, err = _run(self.m, ["validate"], dispatcher=fake)
        self.assertEqual("", out)
        self.assertIn("fail", err)


# ════════════════════════════════════════════════════════════════════════
# show — source and effective display
# ════════════════════════════════════════════════════════════════════════


class TestShowSource(unittest.TestCase):
    """``show`` without ``--effective`` renders source inventory."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = _load_mod()

    def _build_source(self) -> dict[str, object]:
        return {
            "node": {"tag": "24-trixie-slim", "digest": "sha256:abc..."},
            "toolchain": {"rust": "1.0.0", "uv": "0.1.0", "python": "3.14.6"},
        }

    def _runtime_source(self) -> dict[str, object]:
        return {
            "pi-extensions": {
                "myext": {"version": "1.2.3"},
            },
        }

    def test_show_build_source_only(self) -> None:
        data = self._build_source()
        fake = _make_fake(self.m, exit_kind="success", data=data)
        _, out, _ = _run(self.m, ["show", "--scope", "build"], dispatcher=fake)
        self.assertIn("node", out)
        self.assertNotIn("pi-extensions", out)

    def test_show_runtime_source_only(self) -> None:
        data = self._runtime_source()
        fake = _make_fake(self.m, exit_kind="success", data=data)
        _, out, _ = _run(self.m, ["show", "--scope", "runtime"], dispatcher=fake)
        self.assertIn("pi-extensions", out)
        self.assertNotIn("node", out)

    def test_show_all_source(self) -> None:
        data = {"build": self._build_source(), "runtime": self._runtime_source()}
        fake = _make_fake(self.m, exit_kind="success", data=data)
        _, out, _ = _run(self.m, ["show", "--scope", "all"], dispatcher=fake)
        self.assertIn("node", out)
        self.assertIn("pi-extensions", out)

    def test_show_source_deterministic_text(self) -> None:
        data = self._build_source()
        fake = _make_fake(self.m, exit_kind="success", data=data)
        _, out1, _ = _run(self.m, ["show", "--scope", "build"], dispatcher=fake)
        _, out2, _ = _run(self.m, ["show", "--scope", "build"], dispatcher=fake)
        self.assertEqual(out1, out2)

    def test_show_source_deterministic_json(self) -> None:
        data = self._runtime_source()
        fake = _make_fake(self.m, exit_kind="success", data=data)
        _, out1, _ = _run(
            self.m, ["--output", "json", "show", "--scope", "runtime"], dispatcher=fake
        )
        _, out2, _ = _run(
            self.m, ["--output", "json", "show", "--scope", "runtime"], dispatcher=fake
        )
        self.assertEqual(out1, out2)

    def test_show_source_json_is_parseable(self) -> None:
        data = self._build_source()
        fake = _make_fake(self.m, exit_kind="success", data=data)
        _, out, _ = _run(
            self.m, ["--output", "json", "show", "--scope", "build"], dispatcher=fake
        )
        payload = json.loads(out)
        self.assertEqual("node", list(payload["data"].keys())[0])


class TestShowEffective(unittest.TestCase):
    """``show --effective`` projects resolved versions."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = _load_mod()

    def _eff_build(self) -> dict[str, object]:
        return {
            "node": {"tag": "24-trixie-slim", "digest": "sha256:def..."},
            "toolchain": {"rust": "1.85.0", "uv": "0.6.0", "python": "3.14.6"},
        }

    def _eff_runtime(self) -> dict[str, object]:
        return {
            "pi-extensions": {
                "myext": {"version": "1.2.3"},
            },
        }

    def test_show_effective_build_projection(self) -> None:
        data = self._eff_build()
        fake = _make_fake(self.m, exit_kind="success", data=data)
        _, out, _ = _run(
            self.m, ["show", "--scope", "build", "--effective"], dispatcher=fake
        )
        self.assertIn("node", out)

    def test_show_effective_runtime_projection(self) -> None:
        data = self._eff_runtime()
        fake = _make_fake(self.m, exit_kind="success", data=data)
        _, out, _ = _run(
            self.m, ["show", "--scope", "runtime", "--effective"], dispatcher=fake
        )
        self.assertIn("pi-extensions", out)

    def test_show_effective_all_separate_objects(self) -> None:
        data = {"build": self._eff_build(), "runtime": self._eff_runtime()}
        fake = _make_fake(self.m, exit_kind="success", data=data)
        _, out, _ = _run(
            self.m,
            ["--output", "json", "show", "--scope", "all", "--effective"],
            dispatcher=fake,
        )
        payload = json.loads(out)
        self.assertIn("build", payload["data"])
        self.assertIn("runtime", payload["data"])

    def test_effective_runtime_excludes_provider_metadata(self) -> None:
        """Runtime effective output must not leak source/update/override metadata."""
        data = self._eff_runtime()  # provider metadata absent by construction
        fake = _make_fake(self.m, exit_kind="success", data=data)
        _, out, _ = _run(
            self.m,
            ["--output", "json", "show", "--scope", "runtime", "--effective"],
            dispatcher=fake,
        )
        payload = json.loads(out)
        flat = json.dumps(payload["data"])
        for forbidden in ("source", "provider", "update", "override", "cache"):
            self.assertNotIn(
                f'"{forbidden}"',
                flat,
                f"effective runtime must not expose {forbidden!r}",
            )

    def test_effective_build_excludes_runtime_extensions(self) -> None:
        """Build effective output must not include runtime extensions."""
        data = self._eff_build()
        fake = _make_fake(self.m, exit_kind="success", data=data)
        _, out, _ = _run(
            self.m,
            ["--output", "json", "show", "--scope", "build", "--effective"],
            dispatcher=fake,
        )
        payload = json.loads(out)
        flat = json.dumps(payload["data"])
        self.assertNotIn("pi-extensions", flat)

    def test_effective_never_publishes_projection_file(self) -> None:
        """show --effective must never write a projection file to disk."""
        import tempfile
        import pathlib

        with tempfile.TemporaryDirectory() as tmp:
            # Redirect CWD so any accidental file creation lands here
            orig = os.getcwd()
            try:
                os.chdir(tmp)
                data = self._eff_build()
                fake = _make_fake(self.m, exit_kind="success", data=data)
                _run(
                    self.m, ["show", "--scope", "build", "--effective"], dispatcher=fake
                )
                # No .docker-generated or toml files created
                created = list(pathlib.Path(tmp).rglob("*"))
                self.assertEqual(
                    [],
                    [str(p) for p in created],
                    "show --effective must not write files",
                )
            finally:
                os.chdir(orig)


class TestShowOverrides(unittest.TestCase):
    """``show`` accepts ``--override PATH=VALUE`` for effective display."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = _load_mod()

    def test_override_passed_to_dispatcher(self) -> None:
        fake = _make_recording_fake(self.m)
        _run(
            self.m,
            [
                "show",
                "--scope",
                "build",
                "--effective",
                "--override",
                "build.stages.toolchain.rust.version=1.86.0",
            ],
            dispatcher=fake,
        )
        overrides = fake.calls[0][1].command_args.get("overrides")
        self.assertIsNotNone(overrides)
        self.assertIn("build.stages.toolchain.rust.version", overrides)

    def test_override_rejected_for_wrong_scope(self) -> None:
        """A runtime override under --scope build must be rejected."""
        # This is a domain rule — the fake simulates a config error
        fake = _make_fake(
            self.m,
            exit_kind="config",
            message="override 'runtime.pi-extensions.x.version'"
            " is not valid for scope build",
        )
        _, out, err = _run(
            self.m,
            [
                "show",
                "--scope",
                "build",
                "--effective",
                "--override",
                "runtime.pi-extensions.x.version=2.0",
            ],
            dispatcher=fake,
        )
        self.assertEqual("", out)
        self.assertIn("not valid for scope build", err)

    def test_override_never_mutates_source_toml(self) -> None:
        """--override must never write back to docker-constructor.toml."""
        import tempfile, pathlib

        with tempfile.TemporaryDirectory() as tmp:
            orig = os.getcwd()
            try:
                os.chdir(tmp)
                data = {"toolchain": {"rust": "1.86.0"}}
                fake = _make_fake(self.m, exit_kind="success", data=data)
                _run(
                    self.m,
                    [
                        "show",
                        "--scope",
                        "build",
                        "--effective",
                        "--override",
                        "build.stages.toolchain.rust.version=1.86.0",
                    ],
                    dispatcher=fake,
                )
                created = list(pathlib.Path(tmp).rglob("*"))
                self.assertEqual([], [str(p) for p in created])
            finally:
                os.chdir(orig)

    def test_duplicate_override_rejected_at_cli(self) -> None:
        """Repeated ``--override`` with the same path must be rejected
        by the facade's ``_parse_overrides``, producing a CLI error."""
        _, out, err = _run(
            self.m,
            [
                "show",
                "--scope",
                "build",
                "--effective",
                "--override",
                "build.stages.toolchain.python.version=3.14.0",
                "--override",
                "build.stages.toolchain.python.version=3.15.0",
            ],
            dispatcher=None,  # dispatcher never reached — parse fails first
        )
        self.assertEqual("", out, "must not produce stdout on parse error")
        self.assertIn(
            "duplicate", err.lower(), f"expected 'duplicate' in stderr, got: {err}"
        )


# ════════════════════════════════════════════════════════════════════════
# check-updates — scope, filters, policy exits
# ════════════════════════════════════════════════════════════════════════


class TestCheckUpdatesScope(unittest.TestCase):
    """``check-updates`` scopes update discovery to build | runtime | all."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = _load_mod()

    def test_check_updates_default_scope_all(self) -> None:
        fake = _make_recording_fake(self.m)
        _run(self.m, ["check-updates"], dispatcher=fake)
        scope = fake.calls[0][1].command_args["scope"]
        self.assertEqual("all", scope)

    def test_check_updates_scope_build(self) -> None:
        fake = _make_recording_fake(self.m)
        _run(self.m, ["check-updates", "--scope", "build"], dispatcher=fake)
        self.assertEqual("build", fake.calls[0][1].command_args["scope"])

    def test_check_updates_scope_runtime(self) -> None:
        fake = _make_recording_fake(self.m)
        _run(self.m, ["check-updates", "--scope", "runtime"], dispatcher=fake)
        self.assertEqual("runtime", fake.calls[0][1].command_args["scope"])

    def test_check_updates_deterministic_target_ordering(self) -> None:
        """Repeated runs with same scope produce identical output."""
        data = {
            "results": [
                {
                    "path": "build.stages.base.node",
                    "current": "24",
                    "candidate": "25",
                    "status": "outdated",
                    "applicable": True,
                },
                {
                    "path": "build.stages.toolchain.rust.version",
                    "current": "1.0.0",
                    "candidate": "1.85.0",
                    "status": "outdated",
                    "applicable": True,
                },
            ],
        }
        fake = _make_fake(self.m, exit_kind="policy", data=data, message="2 outdated")
        _, out1, _ = _run(
            self.m, ["check-updates", "--scope", "build"], dispatcher=fake
        )
        _, out2, _ = _run(
            self.m, ["check-updates", "--scope", "build"], dispatcher=fake
        )
        self.assertEqual(out1, out2)


class TestCheckUpdatesFilters(unittest.TestCase):
    """``check-updates`` preserves ``--only`` and ``--provider`` filters."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = _load_mod()

    def test_only_filter_passed_to_dispatcher(self) -> None:
        fake = _make_recording_fake(self.m)
        _run(
            self.m,
            ["check-updates", "--only", "gh", "--only", "docker"],
            dispatcher=fake,
        )
        only_filter = fake.calls[0][1].command_args.get("only_filter")
        self.assertIsNotNone(only_filter)
        self.assertEqual(("gh", "docker"), only_filter)
        self.assertIsInstance(only_filter, tuple)  # frozen

    def test_unknown_filter_receives_structured_error(self) -> None:
        fake = _make_fake(
            self.m,
            exit_kind="cli",
            message="unknown filter 'nonexistent': not a provider or path",
        )
        _, out, err = _run(
            self.m,
            ["check-updates", "--only", "nonexistent"],
            dispatcher=fake,
        )
        self.assertEqual("", out)
        self.assertIn("unknown filter", err)

    def test_unknown_filter_suggestion(self) -> None:
        fake = _make_fake(
            self.m,
            exit_kind="cli",
            data={"suggestion": "did you mean 'github-release'?"},
            message="unknown provider 'github'",
        )
        _, out, _ = _run(
            self.m,
            ["--output", "json", "check-updates", "--only", "github"],
            dispatcher=fake,
        )
        payload = json.loads(out)
        self.assertIn("suggestion", payload["data"])

    def test_only_filter_preserves_canonical_inventory_path(self) -> None:
        """``--only build.stages.base.node`` must be passed as-is."""
        fake = _make_recording_fake(self.m)
        _run(
            self.m,
            ["check-updates", "--only", "build.stages.base.node"],
            dispatcher=fake,
        )
        only_filter = fake.calls[0][1].command_args["only_filter"]
        self.assertIn("build.stages.base.node", only_filter)

    def test_only_filter_preserves_provider_name(self) -> None:
        """``--only docker-registry`` must be passed as-is."""
        fake = _make_recording_fake(self.m)
        _run(self.m, ["check-updates", "--only", "docker-registry"], dispatcher=fake)
        only_filter = fake.calls[0][1].command_args["only_filter"]
        self.assertIn("docker-registry", only_filter)


class TestCheckUpdatesControls(unittest.TestCase):
    """``check-updates`` preserves supported discovery controls."""

    def test_help_omits_retired_cache_ttl(self) -> None:
        # Render the command-specific help directly and assert the retired
        # option is absent.
        out = io.StringIO()
        with redirect_stdout(out):
            try:
                self.m._build_parser().parse_args(["check-updates", "--help"])
            except SystemExit as exc:
                self.assertEqual(0, exc.code)
        self.assertNotIn("--cache-ttl", out.getvalue())

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = _load_mod()

    def test_reviewed_ttl_modes_preserve_suggest_json_and_policy_flags(self) -> None:
        for argv in (
            ["check-updates", "--suggest"],
            ["--output", "json", "check-updates"],
            ["check-updates", "--fail-on-outdated"],
        ):
            with self.subTest(argv=argv):
                fake = _make_recording_fake(self.m)
                _run(self.m, argv, dispatcher=fake)
                args = fake.calls[0][1].command_args
                self.assertNotIn("cache_ttl", args)
                self.assertFalse(args["no_cache"])

    def test_no_cache_modes_do_not_add_a_ttl_override(self) -> None:
        for argv in (
            ["check-updates", "--suggest", "--no-cache"],
            ["--output", "json", "check-updates", "--no-cache"],
            ["check-updates", "--fail-on-outdated", "--no-cache"],
        ):
            with self.subTest(argv=argv):
                fake = _make_recording_fake(self.m)
                _run(self.m, argv, dispatcher=fake)
                args = fake.calls[0][1].command_args
                self.assertTrue(args["no_cache"])
                self.assertNotIn("cache_ttl", args)

    def test_suggest_flag_passed(self) -> None:
        fake = _make_recording_fake(self.m)
        _run(self.m, ["check-updates", "--suggest"], dispatcher=fake)
        self.assertTrue(fake.calls[0][1].command_args["suggest"])

    def test_include_prerelease_passed(self) -> None:
        fake = _make_recording_fake(self.m)
        _run(self.m, ["check-updates", "--include-prerelease"], dispatcher=fake)
        self.assertTrue(fake.calls[0][1].command_args["include_prerelease"])

    def test_cache_ttl_is_rejected(self) -> None:
        # Reviewed inventory policy is the sole TTL source; the retired CLI
        # override must fail in argparse before dispatch.
        with self.assertRaises(SystemExit) as caught:
            self.m._build_parser().parse_args(["check-updates", "--cache-ttl", "300"])
        self.assertEqual(2, caught.exception.code)

    def test_cache_dir_is_rejected(self) -> None:
        # Retired HTTP-only cache location must fail in argparse before
        # dispatch, so no cache path can be selected or migrated.
        with self.assertRaises(SystemExit) as caught:
            self.m._build_parser().parse_args(
                ["check-updates", "--cache-dir", "/tmp/pi-cache"]
            )
        self.assertEqual(2, caught.exception.code)

    def test_no_cache_passed(self) -> None:
        fake = _make_recording_fake(self.m)
        _run(self.m, ["check-updates", "--no-cache"], dispatcher=fake)
        self.assertTrue(fake.calls[0][1].command_args["no_cache"])

    def test_strict_passed(self) -> None:
        fake = _make_recording_fake(self.m)
        _run(self.m, ["check-updates", "--strict"], dispatcher=fake)
        self.assertTrue(fake.calls[0][1].command_args["strict"])

    def test_fail_on_outdated_passed(self) -> None:
        fake = _make_recording_fake(self.m)
        _run(self.m, ["check-updates", "--fail-on-outdated"], dispatcher=fake)
        self.assertTrue(fake.calls[0][1].command_args["fail_on_outdated"])

    def test_json_output_flag(self) -> None:
        fake = _make_recording_fake(self.m)
        _run(self.m, ["--output", "json", "check-updates"], dispatcher=fake)
        self.assertEqual("json", fake.calls[0][1].output)

    def test_provider_receives_only_selected_scope_targets(self) -> None:
        """Provider calls must receive targets from the selected scope only."""
        fake = _make_recording_fake(self.m)
        _run(self.m, ["check-updates", "--scope", "build"], dispatcher=fake)
        req = fake.calls[0][1]
        self.assertEqual("build", req.command_args["scope"])
        # The dispatcher handles filtering — we only verify scope is
        # passed correctly.  Actual target filtering is tested in GREEN
        # with real Inventory objects.

    def test_runtime_artifact_catalogs_not_queried_as_update_targets(self) -> None:
        """Runtime extension artifact catalogs are NOT independent targets."""
        # Structural: the dispatcher receives scope=runtime and is
        # expected to only enumerate extensions as update targets,
        # not their individual artifact sub-entries.
        pass  # exercised after GREEN wiring

    def test_details_flag_is_command_local_boolean(self) -> None:
        """``--details`` is a command-local ``check-updates`` boolean."""
        ns = self.m._build_parser().parse_args(["check-updates", "--details"])
        self.assertIs(ns.details, True)
        ns_default = self.m._build_parser().parse_args(["check-updates"])
        self.assertIs(ns_default.details, False)

    def test_details_help_is_discoverable(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            try:
                self.m._build_parser().parse_args(["check-updates", "--help"])
            except SystemExit as exc:
                self.assertEqual(0, exc.code)
        self.assertIn("--details", out.getvalue())

    def test_details_flag_not_passed_to_dispatcher(self) -> None:
        """Presentation-only flag must not leak into provider inputs."""
        fake = _make_recording_fake(self.m)
        _run(self.m, ["check-updates", "--details"], dispatcher=fake)
        args = fake.calls[0][1].command_args
        self.assertNotIn("details", args)


class TestCheckUpdatesSuggestions(unittest.TestCase):
    """``--suggest`` produces non-mutating output."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = _load_mod()

    def test_suggestions_non_mutating_text(self) -> None:
        data = {
            "results": [
                {
                    "path": "build.stages.base.node",
                    "status": "outdated",
                    "current": "24",
                    "candidate": "25",
                }
            ],
            "suggestions": [
                {"path": "build.stages.base.node", "change": {"tag": "25-trixie-slim"}},
            ],
        }
        fake = _make_fake(self.m, exit_kind="policy", data=data, message="1 outdated")
        import tempfile, pathlib

        with tempfile.TemporaryDirectory() as tmp:
            orig = os.getcwd()
            try:
                os.chdir(tmp)
                _, out, _ = _run(
                    self.m,
                    ["check-updates", "--scope", "build", "--suggest"],
                    dispatcher=fake,
                )
                created = list(pathlib.Path(tmp).rglob("*"))
                self.assertEqual([], [str(p) for p in created])
                self.assertIn("1 outdated", out)
            finally:
                os.chdir(orig)

    def test_suggestions_non_mutating_json(self) -> None:
        data = {"suggestions": [{"path": "x", "value": "y"}]}
        fake = _make_fake(
            self.m, exit_kind="policy", data=data, message="suggestions follow"
        )
        import tempfile, pathlib

        with tempfile.TemporaryDirectory() as tmp:
            orig = os.getcwd()
            try:
                os.chdir(tmp)
                _, out, _ = _run(
                    self.m,
                    ["--output", "json", "check-updates", "--suggest"],
                    dispatcher=fake,
                )
                payload = json.loads(out)
                self.assertIn("suggestions", payload["data"])
                self.assertEqual(
                    [],
                    [str(p) for p in pathlib.Path(tmp).rglob("*")],
                )
            finally:
                os.chdir(orig)

    def test_suggestions_never_written_to_toml(self) -> None:
        """Suggestions must never modify docker-constructor.toml."""
        pass  # exercised via GREEN wiring


class TestCheckUpdatesTextRendering(unittest.TestCase):
    """Text-mode ``check-updates`` produces human-readable reports."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = _load_mod()

    # ── status summary ────────────────────────────────────────────

    def test_summary_counts_all_statuses(self) -> None:
        data = {
            "results": [
                {
                    "path": "a",
                    "provider": "pypi",
                    "current": "1.0",
                    "candidate": None,
                    "status": "current",
                    "kind": "version",
                    "applicable": False,
                    "reason": None,
                },
                {
                    "path": "b",
                    "provider": "npm",
                    "current": "2.0",
                    "candidate": "3.0",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": True,
                    "reason": None,
                },
                {
                    "path": "c",
                    "provider": "gh",
                    "current": "4.0",
                    "candidate": "5.0",
                    "status": "skipped",
                    "kind": "version",
                    "applicable": False,
                    "reason": "prerelease excluded",
                },
                {
                    "path": "d",
                    "provider": "docker",
                    "current": "6.0",
                    "candidate": None,
                    "status": "unavailable",
                    "kind": "digest-refresh",
                    "applicable": False,
                    "reason": "timeout",
                },
                {
                    "path": "e",
                    "provider": "pypi",
                    "current": "7.0",
                    "candidate": "8.0",
                    "status": "incomplete",
                    "kind": "version",
                    "applicable": False,
                    "reason": "missing sha256",
                },
            ],
        }
        fake = _make_fake(self.m, exit_kind="success", data=data)
        _, out, _ = _run(self.m, ["check-updates"], dispatcher=fake)
        self.assertIn(
            "1 current, 1 outdated (1 applicable), 1 skipped, 1 unavailable, 1 incomplete",
            out,
        )

    # ── table columns ─────────────────────────────────────────────

    def test_table_headers_present(self) -> None:
        data = {
            "results": [
                {
                    "path": "x",
                    "provider": "npm",
                    "current": "1",
                    "candidate": "2",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": True,
                    "reason": None,
                },
            ],
        }
        fake = _make_fake(self.m, exit_kind="success", data=data)
        _, out, _ = _run(self.m, ["check-updates"], dispatcher=fake)
        self.assertIn("TARGET", out)
        self.assertIn("PROVIDER", out)
        self.assertIn("CURR -> NEXT", out)
        self.assertIn("STATUS", out)
        self.assertIn("PUBLISHED", out)

    def test_table_rows_contain_values(self) -> None:
        data = {
            "results": [
                {
                    "path": "pkg",
                    "provider": "pypi",
                    "current": "1.2",
                    "candidate": "1.3",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": True,
                    "reason": "available",
                },
            ],
        }
        fake = _make_fake(self.m, exit_kind="success", data=data)
        _, out, _ = _run(self.m, ["check-updates"], dispatcher=fake)
        self.assertIn("pkg", out)
        self.assertIn("pypi", out)
        self.assertIn("1.2 -> 1.3", out)
        self.assertIn("outdated", out)
        self.assertIn("available", out)

    def test_applicable_no_in_output(self) -> None:
        data = {
            "results": [
                {
                    "path": "pkg",
                    "provider": "npm",
                    "current": "1.0",
                    "candidate": "2.0",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": False,
                    "reason": "incompatible",
                },
            ],
        }
        fake = _make_fake(self.m, exit_kind="success", data=data)
        _, out, _ = _run(self.m, ["check-updates"], dispatcher=fake)
        # Applicability is reported via the summary, not a table column.
        self.assertIn("1 outdated (0 applicable)", out)
        self.assertNotIn("APPLICABLE", out)

    def test_null_candidate_renders_dash(self) -> None:
        data = {
            "results": [
                {
                    "path": "x",
                    "provider": "gh",
                    "current": "1.0",
                    "candidate": None,
                    "status": "unavailable",
                    "kind": "version",
                    "applicable": False,
                    "reason": "timeout",
                },
            ],
        }
        fake = _make_fake(self.m, exit_kind="success", data=data)
        _, out, _ = _run(self.m, ["check-updates"], dispatcher=fake)
        # The dash for null candidate should appear between CURRENT and STATUS
        self.assertIn("1.0", out)
        self.assertIn("unavailable", out)

    def test_null_reason_renders_dash(self) -> None:
        data = {
            "results": [
                {
                    "path": "x",
                    "provider": "gh",
                    "current": "1.0",
                    "candidate": "2.0",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": True,
                    "reason": None,
                },
            ],
        }
        fake = _make_fake(self.m, exit_kind="success", data=data)
        _, out, _ = _run(self.m, ["check-updates"], dispatcher=fake)
        # A null reason contributes no Details: entry.
        self.assertIn("outdated", out)
        self.assertNotIn("Details:", out)

    # ── summary: applicable count visible ─────────────────────

    def test_summary_shows_applicable_outdated_separately(self) -> None:
        data = {
            "results": [
                {
                    "path": "a",
                    "provider": "pypi",
                    "current": "1",
                    "candidate": "2",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": True,
                    "reason": None,
                },
                {
                    "path": "b",
                    "provider": "npm",
                    "current": "3",
                    "candidate": "4",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": False,
                    "reason": "incompatible",
                },
                {
                    "path": "c",
                    "provider": "gh",
                    "current": "5",
                    "candidate": None,
                    "status": "current",
                    "kind": "version",
                    "applicable": False,
                    "reason": None,
                },
            ],
        }
        fake = _make_fake(self.m, exit_kind="success", data=data)
        _, out, _ = _run(self.m, ["check-updates"], dispatcher=fake)
        self.assertIn("1 current, 2 outdated (1 applicable)", out)

    def test_summary_no_applicable_outdated_shows_zero(self) -> None:
        data = {
            "results": [
                {
                    "path": "a",
                    "provider": "pypi",
                    "current": "1",
                    "candidate": "2",
                    "status": "skipped",
                    "kind": "version",
                    "applicable": False,
                    "reason": "prerelease",
                },
                {
                    "path": "b",
                    "provider": "npm",
                    "current": "3",
                    "candidate": None,
                    "status": "current",
                    "kind": "version",
                    "applicable": False,
                    "reason": None,
                },
            ],
        }
        fake = _make_fake(self.m, exit_kind="success", data=data)
        _, out, _ = _run(self.m, ["check-updates"], dispatcher=fake)
        # Zero outdated → no parenthetical needed
        self.assertIn("1 current, 1 skipped", out)

    def test_summary_zero_applicable_outdated_visible(self) -> None:
        """Even when zero outdated entries are applicable, the count
        must appear so applicability is immediately visible."""
        data = {
            "results": [
                {
                    "path": "a",
                    "provider": "pypi",
                    "current": "1",
                    "candidate": "2",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": False,
                    "reason": "incompatible major",
                },
                {
                    "path": "b",
                    "provider": "npm",
                    "current": "3",
                    "candidate": "4",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": False,
                    "reason": "missing artifact",
                },
                {
                    "path": "c",
                    "provider": "gh",
                    "current": "5",
                    "candidate": None,
                    "status": "current",
                    "kind": "version",
                    "applicable": False,
                    "reason": None,
                },
            ],
        }
        fake = _make_fake(self.m, exit_kind="success", data=data)
        _, out, _ = _run(self.m, ["check-updates"], dispatcher=fake)
        self.assertIn("1 current, 2 outdated (0 applicable)", out)

    # ── None values render as dash ─────────────────────────────

    def test_none_current_renders_as_dash(self) -> None:
        data = {
            "results": [
                {
                    "path": "x",
                    "provider": "pypi",
                    "current": None,
                    "candidate": None,
                    "status": "unavailable",
                    "kind": "version",
                    "applicable": False,
                    "reason": "timeout",
                },
            ],
        }
        fake = _make_fake(self.m, exit_kind="success", data=data)
        _, out, _ = _run(self.m, ["check-updates"], dispatcher=fake)
        # current=None must not appear as "None" in output
        self.assertNotIn("None", out)
        # Dash appears in the table row
        self.assertIn("pypi", out)
        self.assertIn("unavailable", out)

    def test_none_provider_renders_as_dash(self) -> None:
        data = {
            "results": [
                {
                    "path": "x",
                    "provider": None,
                    "current": "1",
                    "candidate": None,
                    "status": "current",
                    "kind": "version",
                    "applicable": False,
                    "reason": None,
                },
            ],
        }
        fake = _make_fake(self.m, exit_kind="success", data=data)
        _, out, _ = _run(self.m, ["check-updates"], dispatcher=fake)
        self.assertNotIn("None", out)

    # ── table robustness: long values, column separation ───────

    def test_long_path_does_not_overflow_into_provider(self) -> None:
        long_path = "build.stages.toolchain.rust.version.artifact.linux-amd64"
        compact_path = "toolchain.rust.version.artifact.linux-amd64"
        data = {
            "results": [
                {
                    "path": long_path,
                    "provider": "gh",
                    "current": "1.0",
                    "candidate": "2.0",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": True,
                    "reason": None,
                },
            ],
        }
        fake = _make_fake(self.m, exit_kind="success", data=data)
        _, out, _ = _run(self.m, ["check-updates"], dispatcher=fake)
        # The compact target and provider must both appear, separated
        self.assertIn(compact_path, out)
        self.assertNotIn("build.stages.", out)
        self.assertIn("gh", out)
        # Provider appears after the target, not glued to it
        self.assertIn(compact_path + "  ", out)

    def test_columns_growth_expands_separator(self) -> None:
        data_small = {
            "results": [
                {
                    "path": "a",
                    "provider": "p",
                    "current": "1",
                    "candidate": "2",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": True,
                    "reason": None,
                },
            ],
        }
        data_large = {
            "results": [
                {
                    "path": "very.long.path.name",
                    "provider": "long-provider",
                    "current": "99.99.99",
                    "candidate": "100.0.0",
                    "status": "outdated",
                    "kind": "digest-refresh",
                    "applicable": True,
                    "reason": None,
                },
            ],
        }
        fake_small = _make_fake(self.m, exit_kind="success", data=data_small)
        fake_large = _make_fake(self.m, exit_kind="success", data=data_large)
        _, out_s, _ = _run(self.m, ["check-updates"], dispatcher=fake_small)
        _, out_l, _ = _run(self.m, ["check-updates"], dispatcher=fake_large)
        # Separator line must be wider for the larger data
        sep_s = [ln for ln in out_s.split("\n") if ln.startswith("---")][0]
        sep_l = [ln for ln in out_l.split("\n") if ln.startswith("---")][0]
        self.assertGreater(len(sep_l), len(sep_s))

    # ── deterministic ordering ───────────────────────────────────

    def test_identical_inputs_produce_identical_output(self) -> None:
        data = {
            "results": [
                {
                    "path": "a",
                    "provider": "pypi",
                    "current": "1",
                    "candidate": "2",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": True,
                    "reason": None,
                },
                {
                    "path": "b",
                    "provider": "npm",
                    "current": "3",
                    "candidate": "4",
                    "status": "current",
                    "kind": "version",
                    "applicable": False,
                    "reason": None,
                },
            ],
        }
        fake = _make_fake(self.m, exit_kind="success", data=data)
        _, out1, _ = _run(self.m, ["check-updates"], dispatcher=fake)
        _, out2, _ = _run(self.m, ["check-updates"], dispatcher=fake)
        self.assertEqual(out1, out2)

    # ── JSON compatibility ───────────────────────────────────────

    def test_json_output_unchanged(self) -> None:
        data = {
            "results": [
                {
                    "path": "x",
                    "provider": "npm",
                    "current": "1",
                    "candidate": "2",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": True,
                    "reason": None,
                },
            ],
            "suggestions": [
                {"path": "x", "changes": {"version": "2"}},
            ],
        }
        fake = _make_fake(self.m, exit_kind="policy", data=data, message="1 outdated")
        _, out, _ = _run(
            self.m,
            ["--output", "json", "check-updates", "--suggest"],
            dispatcher=fake,
        )
        payload = json.loads(out)
        self.assertEqual("policy", payload["status"])
        self.assertIn("results", payload["data"])
        self.assertIn("suggestions", payload["data"])

    def test_json_no_suggestions_key_when_absent(self) -> None:
        data = {
            "results": [
                {
                    "path": "x",
                    "provider": "npm",
                    "current": "1",
                    "candidate": None,
                    "status": "current",
                    "kind": "version",
                    "applicable": False,
                    "reason": None,
                },
            ],
        }
        fake = _make_fake(self.m, exit_kind="success", data=data)
        _, out, _ = _run(
            self.m,
            ["--output", "json", "check-updates"],
            dispatcher=fake,
        )
        payload = json.loads(out)
        # suggest flag absent → suggestions not emitted
        self.assertNotIn("suggestions", payload.get("data", {}))


class TestCheckUpdatesDetailsRendering(unittest.TestCase):
    """``check-updates --details`` renders the full diagnostic table."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = _load_mod()

    def _detail_data(self) -> dict[str, object]:
        return {
            "results": [
                {
                    "path": "build.stages.toolchain.ty",
                    "provider": "pypi",
                    "current": "0.0.61",
                    "candidate": "0.0.62",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": True,
                    "reason": None,
                    "published_at": "2026-08-14T07:05:02Z",
                },
            ],
        }

    def test_details_headers_present(self) -> None:
        fake = _make_fake(self.m, exit_kind="success", data=self._detail_data())
        _, out, _ = _run(self.m, ["check-updates", "--details"], dispatcher=fake)
        for header in (
            "PATH",
            "PROVIDER",
            "CURRENT",
            "CANDIDATE",
            "STATUS",
            "KIND",
            "APPLICABLE",
            "PUBLISHED",
            "DETAIL",
        ):
            self.assertIn(header, out)
        self.assertNotIn("CURR -> NEXT", out)
        self.assertNotIn("TARGET", out)

    def test_details_retains_full_paths(self) -> None:
        fake = _make_fake(self.m, exit_kind="success", data=self._detail_data())
        _, out, _ = _run(self.m, ["check-updates", "--details"], dispatcher=fake)
        self.assertIn("build.stages.toolchain.ty", out)

    def test_details_publication_timestamp_format(self) -> None:
        fake = _make_fake(self.m, exit_kind="success", data=self._detail_data())
        _, out, _ = _run(self.m, ["check-updates", "--details"], dispatcher=fake)
        self.assertIn("2026-08-14 07:05:02 GMT", out)

    def test_details_preserves_suggestions(self) -> None:
        data = {
            "results": self._detail_data()["results"],
            "suggest": True,
            "suggestions": [
                {"path": "build.stages.toolchain.ty", "changes": {"version": "0.0.62"}},
            ],
            "replacement_fragments": (
                '[build.stages.toolchain.ty]\nversion = "0.0.62"\n'
            ),
        }
        fake = _make_fake(self.m, exit_kind="policy", data=data, message="1 outdated")
        _, out, _ = _run(
            self.m,
            ["check-updates", "--details", "--suggest"],
            dispatcher=fake,
        )
        self.assertIn("APPLICABLE", out)
        self.assertIn("─── manual replacement blocks", out)
        self.assertIn("review-only", out)
        self.assertIn("[build.stages.toolchain.ty]", out)
        self.assertIn('version = "0.0.62"', out)

    def test_details_json_matches_without_details(self) -> None:
        data = self._detail_data()
        fake = _make_fake(self.m, exit_kind="policy", data=data, message="1 outdated")
        _, out_details, _ = _run(
            self.m,
            ["--output", "json", "check-updates", "--details"],
            dispatcher=fake,
        )
        _, out_plain, _ = _run(
            self.m,
            ["--output", "json", "check-updates"],
            dispatcher=fake,
        )
        payload_details = json.loads(out_details)
        payload_plain = json.loads(out_plain)
        self.assertEqual(payload_details, payload_plain)
        self.assertNotIn("details", payload_details.get("data", {}))

    def test_details_preserves_policy_exit_code(self) -> None:
        data = self._detail_data()
        fake = _make_fake(self.m, exit_kind="policy", data=data, message="1 outdated")
        rc_details, _, _ = _run(
            self.m,
            ["check-updates", "--details", "--fail-on-outdated"],
            dispatcher=fake,
        )
        rc_plain, _, _ = _run(
            self.m,
            ["check-updates", "--fail-on-outdated"],
            dispatcher=fake,
        )
        self.assertEqual(rc_details, rc_plain)
        self.assertEqual(1, rc_details)


class TestCheckUpdatesSuggestRendering(unittest.TestCase):
    """``--suggest`` renders a labelled TOML fragment or a clear no-candidates message."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = _load_mod()

    def test_suggestions_render_toml_fragment(self) -> None:
        data = {
            "results": [
                {
                    "path": "pkg",
                    "provider": "pypi",
                    "current": "1.0",
                    "candidate": "2.0",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": True,
                    "reason": None,
                },
            ],
            "suggest": True,
            "suggestions": [
                {"path": "pkg", "changes": {"version": "2.0"}},
            ],
            "replacement_fragments": '[pkg]\nversion = "2.0"\n',
        }
        fake = _make_fake(self.m, exit_kind="policy", data=data, message="1 outdated")
        _, out, _ = _run(
            self.m,
            ["check-updates", "--suggest"],
            dispatcher=fake,
        )
        self.assertIn("─── manual replacement blocks", out)
        self.assertIn("review-only", out)
        self.assertIn("[pkg]", out)
        self.assertIn('version = "2.0"', out)

    def test_suggestions_label_explicitly_review_only(self) -> None:
        data = {
            "results": [
                {
                    "path": "x",
                    "provider": "npm",
                    "current": "1",
                    "candidate": "2",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": True,
                    "reason": None,
                },
            ],
            "suggest": True,
            "suggestions": [
                {"path": "x", "changes": {"version": "2"}},
            ],
            "replacement_fragments": '[x]\nversion = "2"\n',
        }
        fake = _make_fake(self.m, exit_kind="policy", data=data, message="1 outdated")
        _, out, _ = _run(
            self.m,
            ["check-updates", "--suggest"],
            dispatcher=fake,
        )
        self.assertIn("review-only", out)
        self.assertIn("not applied automatically", out)

    def test_no_suggestions_shows_message(self) -> None:
        data = {
            "results": [
                {
                    "path": "x",
                    "provider": "npm",
                    "current": "1",
                    "candidate": None,
                    "status": "current",
                    "kind": "version",
                    "applicable": False,
                    "reason": None,
                },
            ],
            "suggest": True,
            "suggestions": [],
        }
        fake = _make_fake(self.m, exit_kind="success", data=data, message="all current")
        _, out, _ = _run(
            self.m,
            ["check-updates", "--suggest"],
            dispatcher=fake,
        )
        self.assertIn("No reviewable replacement blocks", out)
        self.assertNotIn("[x]", out)  # no TOML block

    def test_no_candidate_emits_labelled_message_not_empty_fragment(self) -> None:
        """No candidate → explicit no-replacement-block message, no fragment."""
        data = {
            "results": [
                {
                    "path": "x",
                    "provider": "npm",
                    "current": "1",
                    "candidate": None,
                    "status": "current",
                    "kind": "version",
                    "applicable": False,
                    "reason": None,
                },
            ],
            "suggest": True,
            "suggestions": [],
            "replacement_fragments": "",
        }
        fake = _make_fake(self.m, exit_kind="success", data=data, message="all current")
        _, out, _ = _run(
            self.m,
            ["check-updates", "--suggest"],
            dispatcher=fake,
        )
        # Labelled empty-case header + explicit message.
        self.assertIn("─── replacement blocks", out)
        self.assertIn("No reviewable replacement blocks available.", out)
        # No fragment label and no stray TOML table.
        self.assertNotIn("─── manual replacement blocks", out)
        self.assertNotIn("[x]", out)

    def test_incomplete_candidate_shows_reason_and_no_fragment(self) -> None:
        """Incomplete candidate → missing-data reason, no fragment block."""
        data = {
            "results": [
                {
                    "path": "x",
                    "provider": "npm",
                    "current": "1",
                    "candidate": "2",
                    "status": "incomplete",
                    "kind": "version",
                    "applicable": False,
                    "reason": "missing npm integrity",
                },
            ],
            "suggest": True,
            "suggestions": [],
            "replacement_fragments": "",
        }
        fake = _make_fake(
            self.m, exit_kind="success", data=data, message="1 incomplete"
        )
        _, out, _ = _run(
            self.m,
            ["check-updates", "--suggest"],
            dispatcher=fake,
        )
        # The missing-data reason is surfaced in Details.
        self.assertIn("missing npm integrity", out)
        # No empty/unlabelled fragment; explicit no-replacement message.
        self.assertIn("No reviewable replacement blocks available.", out)
        self.assertNotIn("─── manual replacement blocks", out)
        self.assertNotIn("[x]", out)

    def test_suggest_off_no_suggestion_block(self) -> None:
        data = {
            "results": [
                {
                    "path": "x",
                    "provider": "npm",
                    "current": "1",
                    "candidate": "2",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": True,
                    "reason": None,
                },
            ],
            "suggest": False,
            "suggestions": [
                {"path": "x", "changes": {"version": "2"}},
            ],
        }
        fake = _make_fake(self.m, exit_kind="policy", data=data, message="1 outdated")
        _, out, _ = _run(
            self.m,
            ["check-updates"],
            dispatcher=fake,
        )
        self.assertNotIn("suggestions", out)
        self.assertNotIn("review-only", out)

    def test_suggestions_non_mutating_no_files_created(self) -> None:
        data = {
            "results": [
                {
                    "path": "x",
                    "provider": "npm",
                    "current": "1",
                    "candidate": "2",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": True,
                    "reason": None,
                },
            ],
            "suggest": True,
            "suggestions": [
                {"path": "x", "changes": {"version": "2"}},
            ],
            "replacement_fragments": '[x]\nversion = "2"\n',
        }
        fake = _make_fake(self.m, exit_kind="policy", data=data, message="1 outdated")
        import tempfile
        import pathlib

        with tempfile.TemporaryDirectory() as tmp:
            orig = os.getcwd()
            try:
                os.chdir(tmp)
                _, out, _ = _run(
                    self.m,
                    ["check-updates", "--suggest"],
                    dispatcher=fake,
                )
                created = list(pathlib.Path(tmp).rglob("*"))
                self.assertEqual([], [str(p) for p in created])
                self.assertIn("[x]", out)
            finally:
                os.chdir(orig)


class TestCheckUpdatesCompactIdentifiers(unittest.TestCase):
    """Text-mode CURRENT/CANDIDATE cells abbreviate hex identifiers."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = _load_mod()

    def test_plain_hex_abbreviated(self) -> None:
        data = {
            "results": [
                {
                    "path": "x",
                    "provider": "gh",
                    "current": "abc123def4567890abcdef0123456789abcdef01",
                    "candidate": "fedcba0987654321fedcba0987654321fedcba09",
                    "status": "outdated",
                    "kind": "revision",
                    "applicable": True,
                    "reason": None,
                },
            ],
        }
        fake = _make_fake(self.m, exit_kind="policy", data=data, message="1 outdated")
        _, out, _ = _run(self.m, ["check-updates"], dispatcher=fake)
        self.assertIn("abc12...", out)
        self.assertIn("fedcb...", out)
        self.assertNotIn("abc123def4567890abcdef", out)
        self.assertNotIn("fedcba0987654321fedcba", out)

    def test_sha256_prefix_retained(self) -> None:
        data = {
            "results": [
                {
                    "path": "x",
                    "provider": "gh",
                    "current": "sha256:deadbeefcafebabe0123456789",
                    "candidate": "sha256:abcdef0123456789abcdef0123",
                    "status": "outdated",
                    "kind": "digest-refresh",
                    "applicable": True,
                    "reason": None,
                },
            ],
        }
        fake = _make_fake(self.m, exit_kind="policy", data=data, message="1 outdated")
        _, out, _ = _run(self.m, ["check-updates"], dispatcher=fake)
        self.assertIn("sha256:deadb...", out)
        self.assertIn("sha256:abcde...", out)
        self.assertNotIn("deadbeefcafe", out)

    def test_short_hex_not_abbreviated(self) -> None:
        data = {
            "results": [
                {
                    "path": "x",
                    "provider": "gh",
                    "current": "abcd",
                    "candidate": "ef01",
                    "status": "outdated",
                    "kind": "revision",
                    "applicable": True,
                    "reason": None,
                },
            ],
        }
        fake = _make_fake(self.m, exit_kind="policy", data=data, message="1 outdated")
        _, out, _ = _run(self.m, ["check-updates"], dispatcher=fake)
        self.assertIn("abcd", out)
        self.assertIn("ef01", out)

    def test_version_like_hex_not_abbreviated(self) -> None:
        """Short all-hex values such as '12345' must NOT be truncated."""
        data = {
            "results": [
                {
                    "path": "x",
                    "provider": "gh",
                    "current": "12345",
                    "candidate": "67890",
                    "status": "outdated",
                    "kind": "revision",
                    "applicable": True,
                    "reason": None,
                },
            ],
        }
        fake = _make_fake(self.m, exit_kind="policy", data=data, message="1 outdated")
        _, out, _ = _run(self.m, ["check-updates"], dispatcher=fake)
        self.assertIn("12345", out)
        self.assertIn("67890", out)
        self.assertNotIn("12345...", out)

    def test_sha256_prefix_partial_hex_not_abbreviated(self) -> None:
        """sha256: with non-hex suffix not treated as a digest."""
        data = {
            "results": [
                {
                    "path": "x",
                    "provider": "gh",
                    "current": "sha256:abcdef123-filesystem",
                    "candidate": "sha256:abcdef999-filesystem",
                    "status": "outdated",
                    "kind": "digest-refresh",
                    "applicable": True,
                    "reason": None,
                },
            ],
        }
        fake = _make_fake(self.m, exit_kind="policy", data=data, message="1 outdated")
        _, out, _ = _run(self.m, ["check-updates"], dispatcher=fake)
        # Full values preserved — not truncated
        self.assertIn("sha256:abcdef123-filesystem", out)
        self.assertIn("sha256:abcdef999-filesystem", out)

    def test_prefix_collision_does_not_hide_candidate(self) -> None:
        """Distinct hashes sharing a five-char prefix are not collapsed
        to '-' when they differ after the abbreviated prefix."""
        data = {
            "results": [
                {
                    "path": "x",
                    "provider": "gh",
                    # Same prefix, different tails
                    "current": "4acc900000000000000000000000000000000001",
                    "candidate": "4acc9fffffffffffffffffffffffffffffffffffff",
                    "status": "outdated",
                    "kind": "digest-refresh",
                    "applicable": True,
                    "reason": None,
                },
            ],
        }
        fake = _make_fake(self.m, exit_kind="policy", data=data, message="1 outdated")
        _, out, _ = _run(self.m, ["check-updates"], dispatcher=fake)
        # The CURR -> NEXT cell keeps the candidate portion ("4acc9..."),
        # it is not collapsed to "-".
        self.assertIn("4acc9... -> 4acc9...", out)
        self.assertNotIn("4acc9... -> -", out)

    def test_non_hex_unchanged(self) -> None:
        data = {
            "results": [
                {
                    "path": "x",
                    "provider": "npm",
                    "current": "20.10.0",
                    "candidate": "21.0.0",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": True,
                    "reason": None,
                },
            ],
        }
        fake = _make_fake(self.m, exit_kind="policy", data=data, message="1 outdated")
        _, out, _ = _run(self.m, ["check-updates"], dispatcher=fake)
        self.assertIn("20.10.0", out)
        self.assertIn("21.0.0", out)

    def test_candidate_equal_to_current_renders_dash(self) -> None:
        data = {
            "results": [
                {
                    "path": "x",
                    "provider": "gh",
                    "current": "1.2.3",
                    "candidate": "1.2.3",
                    "status": "current",
                    "kind": "version",
                    "applicable": False,
                    "reason": None,
                },
            ],
        }
        fake = _make_fake(self.m, exit_kind="success", data=data)
        _, out, _ = _run(self.m, ["check-updates"], dispatcher=fake)
        # The CURR -> NEXT cell shows "<current> -> -" when the
        # candidate equals current.
        self.assertIn("1.2.3 -> -", out)
        self.assertRegex(out, r"x\s+gh\s+1\.2\.3\s+->\s+-\s+current")

    # ── full values in JSON ─────────────────────────────────────

    def test_json_retains_full_hex(self) -> None:
        data = {
            "results": [
                {
                    "path": "x",
                    "provider": "gh",
                    "current": "abc123def4567890abcdef0123456789abcdef01",
                    "candidate": "fedcba0987654321fedcba0987654321fedcba09",
                    "status": "outdated",
                    "kind": "revision",
                    "applicable": True,
                    "reason": None,
                },
            ],
        }
        fake = _make_fake(self.m, exit_kind="policy", data=data, message="1 outdated")
        _, out, _ = _run(
            self.m,
            ["--output", "json", "check-updates"],
            dispatcher=fake,
        )
        payload = json.loads(out)
        results = payload["data"]["results"]
        self.assertEqual(
            "abc123def4567890abcdef0123456789abcdef01", results[0]["current"]
        )
        self.assertEqual(
            "fedcba0987654321fedcba0987654321fedcba09", results[0]["candidate"]
        )


class TestCheckUpdatesPublishedAt(unittest.TestCase):
    """Publication dates in compact text, unknown as dash, full values in JSON."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = _load_mod()

    def _published_cell(self, out: str, target: str) -> str:
        """Return the trailing PUBLISHED cell value for *target*'s row."""
        lines = out.splitlines()
        header_line = next(line for line in lines if "PUBLISHED" in line)
        row_line = next(line for line in lines if target in line)
        start = header_line.index("PUBLISHED")
        return row_line[start:].rstrip()

    def test_known_published_at_rendered_as_date(self) -> None:
        data = {
            "results": [
                {
                    "path": "x",
                    "provider": "gh",
                    "current": "1.0",
                    "candidate": "2.0",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": True,
                    "reason": None,
                    "published_at": "2025-03-15T10:30:00Z",
                },
            ],
        }
        fake = _make_fake(self.m, exit_kind="policy", data=data, message="1 outdated")
        _, out, _ = _run(self.m, ["check-updates"], dispatcher=fake)
        self.assertEqual("2025-03-15", self._published_cell(out, "x"))
        self.assertNotIn("10:30:00", out)
        self.assertNotIn("GMT", out)

    def test_null_published_at_renders_dash(self) -> None:
        data = {
            "results": [
                {
                    "path": "x",
                    "provider": "gh",
                    "current": "1.0",
                    "candidate": "2.0",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": True,
                    "reason": None,
                    "published_at": None,
                },
            ],
        }
        fake = _make_fake(self.m, exit_kind="policy", data=data, message="1 outdated")
        _, out, _ = _run(self.m, ["check-updates"], dispatcher=fake)
        self.assertEqual("-", self._published_cell(out, "x"))
        self.assertNotIn("GMT", out)

    def test_malformed_published_at_renders_dash(self) -> None:
        data = {
            "results": [
                {
                    "path": "x",
                    "provider": "gh",
                    "current": "1.0",
                    "candidate": "2.0",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": True,
                    "reason": None,
                    "published_at": "not-a-timestamp",
                },
            ],
        }
        fake = _make_fake(self.m, exit_kind="policy", data=data, message="1 outdated")
        _, out, _ = _run(self.m, ["check-updates"], dispatcher=fake)
        self.assertNotIn("not-a-timestamp", out)
        self.assertEqual("-", self._published_cell(out, "x"))

    def test_timezone_less_published_at_renders_dash(self) -> None:
        """Timezone-less timestamps are not UTC — rendered as '-'."""
        data = {
            "results": [
                {
                    "path": "x",
                    "provider": "gh",
                    "current": "1.0",
                    "candidate": "2.0",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": True,
                    "reason": None,
                    "published_at": "2025-01-01T00:00:00",
                },
            ],
        }
        fake = _make_fake(self.m, exit_kind="policy", data=data, message="1 outdated")
        _, out, _ = _run(self.m, ["check-updates"], dispatcher=fake)
        self.assertNotIn("2025-01-01", out)
        self.assertEqual("-", self._published_cell(out, "x"))

    def test_non_utc_offset_published_at_renders_dash(self) -> None:
        """Non-UTC offsets (e.g. +05:00) are rejected — rendered as '-'."""
        data = {
            "results": [
                {
                    "path": "x",
                    "provider": "gh",
                    "current": "1.0",
                    "candidate": "2.0",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": True,
                    "reason": None,
                    "published_at": "2025-01-01T00:00:00+05:00",
                },
            ],
        }
        fake = _make_fake(self.m, exit_kind="policy", data=data, message="1 outdated")
        _, out, _ = _run(self.m, ["check-updates"], dispatcher=fake)
        self.assertNotIn("2025-01-01", out)
        self.assertNotIn("+05:00", out)
        self.assertEqual("-", self._published_cell(out, "x"))

    def test_minus_00_00_renders_dash(self) -> None:
        """-00:00 is unknown local offset — rendered as '-'."""
        data = {
            "results": [
                {
                    "path": "x",
                    "provider": "gh",
                    "current": "1.0",
                    "candidate": "2.0",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": True,
                    "reason": None,
                    "published_at": "2025-01-01T00:00:00-00:00",
                },
            ],
        }
        fake = _make_fake(self.m, exit_kind="policy", data=data, message="1 outdated")
        _, out, _ = _run(self.m, ["check-updates"], dispatcher=fake)
        self.assertNotIn("2025-01-01", out)
        self.assertNotIn("-00:00", out)
        self.assertEqual("-", self._published_cell(out, "x"))

    def test_over_six_fraction_digits_renders_dash(self) -> None:
        """>6 fractional digits exceed resolution — rendered as '-'."""
        data = {
            "results": [
                {
                    "path": "x",
                    "provider": "gh",
                    "current": "1.0",
                    "candidate": "2.0",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": True,
                    "reason": None,
                    "published_at": "2025-01-01T00:00:00.1234567Z",
                },
            ],
        }
        fake = _make_fake(self.m, exit_kind="policy", data=data, message="1 outdated")
        _, out, _ = _run(self.m, ["check-updates"], dispatcher=fake)
        self.assertNotIn("2025-01-01", out)
        self.assertNotIn("1234567", out)
        self.assertEqual("-", self._published_cell(out, "x"))

    def test_plus_00_00_accepted_and_normalised_in_text(self) -> None:
        """+00:00 offset is valid UTC RFC 3339 — normalised to Z in text."""
        data = {
            "results": [
                {
                    "path": "x",
                    "provider": "gh",
                    "current": "1.0",
                    "candidate": "2.0",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": True,
                    "reason": None,
                    # already normalised by UpdateResult.__post_init__
                    "published_at": "2025-06-01T12:30:00Z",
                },
            ],
        }
        fake = _make_fake(self.m, exit_kind="policy", data=data, message="1 outdated")
        _, out, _ = _run(self.m, ["check-updates"], dispatcher=fake)
        self.assertEqual("2025-06-01", self._published_cell(out, "x"))
        self.assertNotIn("12:30:00", out)
        self.assertNotIn("GMT", out)

    def test_fractional_seconds_preserved_in_json(self) -> None:
        """Fractional seconds like .000 and .123 are preserved in JSON."""
        # .123 — presented as-is
        data = {
            "results": [
                {
                    "path": "x",
                    "provider": "gh",
                    "current": "1.0",
                    "candidate": "2.0",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": True,
                    "reason": None,
                    "published_at": "2025-03-15T10:30:00.123Z",
                },
            ],
        }
        fake = _make_fake(self.m, exit_kind="policy", data=data, message="1 outdated")
        _, out, _ = _run(self.m, ["--output", "json", "check-updates"], dispatcher=fake)
        payload = json.loads(out)
        results = payload["data"]["results"]
        self.assertEqual("2025-03-15T10:30:00.123Z", results[0]["published_at"])

        # .000 — same precision preserved (only offset normalised)
        data2 = {
            "results": [
                {
                    "path": "x",
                    "provider": "gh",
                    "current": "1.0",
                    "candidate": "2.0",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": True,
                    "reason": None,
                    "published_at": "2025-03-15T10:30:00.000Z",
                },
            ],
        }
        fake2 = _make_fake(self.m, exit_kind="policy", data=data2, message="1 outdated")
        _, out2, _ = _run(
            self.m, ["--output", "json", "check-updates"], dispatcher=fake2
        )
        payload2 = json.loads(out2)
        results2 = payload2["data"]["results"]
        self.assertEqual("2025-03-15T10:30:00.000Z", results2[0]["published_at"])

    def test_json_retains_full_published_at(self) -> None:
        data = {
            "results": [
                {
                    "path": "x",
                    "provider": "gh",
                    "current": "1.0",
                    "candidate": "2.0",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": True,
                    "reason": None,
                    "published_at": "2025-03-15T10:30:00Z",
                },
            ],
        }
        fake = _make_fake(self.m, exit_kind="policy", data=data, message="1 outdated")
        _, out, _ = _run(
            self.m,
            ["--output", "json", "check-updates"],
            dispatcher=fake,
        )
        payload = json.loads(out)
        results = payload["data"]["results"]
        self.assertEqual("2025-03-15T10:30:00Z", results[0]["published_at"])

    def test_json_omits_published_at_when_absent(self) -> None:
        data = {
            "results": [
                {
                    "path": "x",
                    "provider": "gh",
                    "current": "1.0",
                    "candidate": "2.0",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": True,
                    "reason": None,
                },
            ],
        }
        fake = _make_fake(self.m, exit_kind="policy", data=data, message="1 outdated")
        _, out, _ = _run(
            self.m,
            ["--output", "json", "check-updates"],
            dispatcher=fake,
        )
        payload = json.loads(out)
        results = payload["data"]["results"]
        self.assertNotIn("published_at", results[0])

    def test_published_at_preserved_in_collective_json(self) -> None:
        """When present, published_at is included in the JSON results array."""
        data = {
            "results": [
                {
                    "path": "x",
                    "provider": "gh",
                    "current": "1.0",
                    "candidate": "2.0",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": True,
                    "reason": None,
                    "published_at": "2025-06-01T12:00:00+00:00",
                },
            ],
        }
        fake = _make_fake(self.m, exit_kind="policy", data=data, message="1 outdated")
        _, out, _ = _run(
            self.m,
            ["--output", "json", "check-updates"],
            dispatcher=fake,
        )
        payload = json.loads(out)
        self.assertIn("published_at", payload["data"]["results"][0])

    def test_suggestions_toml_retains_full_values(self) -> None:
        """TOML suggestions fragment retains unabridged identifiers."""
        full_revision = "fedcba0987654321fedcba0987654321fedcba09"
        data = {
            "results": [
                {
                    "path": "x",
                    "provider": "gh",
                    "current": "abc123def4567890abcdef0123456789abcdef01",
                    "candidate": full_revision,
                    "status": "outdated",
                    "kind": "revision",
                    "applicable": True,
                    "reason": None,
                    "published_at": "2025-01-01T00:00:00Z",
                },
            ],
            "suggest": True,
            "suggestions": [
                {"path": "x", "changes": {"revision": full_revision}},
            ],
            "replacement_fragments": ('[x]\nrevision = "' + full_revision + '"\n'),
        }
        fake = _make_fake(self.m, exit_kind="policy", data=data, message="1 outdated")
        _, out, _ = _run(
            self.m,
            ["check-updates", "--suggest"],
            dispatcher=fake,
        )
        # Table abbreviates hex; TOML fragment below retains full value
        self.assertIn('revision = "' + full_revision + '"', out)
        self.assertIn("fedcb...", out)  # abbreviated in table


class TestCheckUpdatesCompactRenderer(unittest.TestCase):
    """Compact cell formatting: target, date, identifier, value transitions."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = _load_mod()

    def _render(self, data: object) -> str:
        return self.m._render_check_updates_text(data)

    def _published_cell(self, out: str, target: str) -> str:
        lines = out.splitlines()
        header_line = next(line for line in lines if "PUBLISHED" in line)
        row_line = next(line for line in lines if target in line)
        start = header_line.index("PUBLISHED")
        return row_line[start:].rstrip()

    def test_build_stages_prefix_removed(self) -> None:
        data = {
            "results": [
                {
                    "path": "build.stages.toolchain.ty",
                    "provider": "pypi",
                    "current": "0.0.61",
                    "candidate": "0.0.62",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": True,
                    "reason": None,
                    "published_at": "2025-03-15T10:30:00Z",
                },
            ],
        }
        out = self._render(data)
        self.assertIn("toolchain.ty", out)
        self.assertNotIn("build.stages.", out)

    def test_non_matching_path_unchanged(self) -> None:
        data = {
            "results": [
                {
                    "path": "runtime.pi-extensions.foo",
                    "provider": "gh",
                    "current": "1.0.0",
                    "candidate": "2.0.0",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": True,
                    "reason": None,
                    "published_at": None,
                },
            ],
        }
        out = self._render(data)
        # A path without the leading build.stages. prefix is kept intact.
        self.assertIn("runtime.pi-extensions.foo", out)

    def test_publication_date_rendered_as_date_only(self) -> None:
        data = {
            "results": [
                {
                    "path": "build.stages.toolchain.ty",
                    "provider": "pypi",
                    "current": "0.0.61",
                    "candidate": "0.0.62",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": True,
                    "reason": None,
                    "published_at": "2025-03-15T10:30:00Z",
                },
            ],
        }
        out = self._render(data)
        self.assertIn("2025-03-15", out)
        # Compact publication values are calendar dates, not GMT timestamps.
        self.assertNotIn("10:30:00", out)
        self.assertNotIn("GMT", out)

    def test_invalid_publication_date_renders_dash(self) -> None:
        data = {
            "results": [
                {
                    "path": "pkg",
                    "provider": "pypi",
                    "current": "1.0",
                    "candidate": "2.0",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": True,
                    "reason": None,
                    "published_at": "not-a-timestamp",
                },
            ],
        }
        out = self._render(data)
        self.assertNotIn("not-a-timestamp", out)
        self.assertEqual("-", self._published_cell(out, "pkg"))

    def test_missing_publication_date_renders_dash(self) -> None:
        data = {
            "results": [
                {
                    "path": "pkg",
                    "provider": "pypi",
                    "current": "1.0",
                    "candidate": "2.0",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": True,
                    "reason": None,
                    "published_at": None,
                },
            ],
        }
        out = self._render(data)
        self.assertEqual("-", self._published_cell(out, "pkg"))

    def test_identifier_abbreviation_in_combined_cell(self) -> None:
        data = {
            "results": [
                {
                    "path": "x",
                    "provider": "gh",
                    "current": "abc123def4567890abcdef0123456789abcdef01",
                    "candidate": "fedcba0987654321fedcba0987654321fedcba09",
                    "status": "outdated",
                    "kind": "revision",
                    "applicable": True,
                    "reason": None,
                },
            ],
        }
        out = self._render(data)
        self.assertIn("abc12... -> fedcb...", out)

    def test_sha256_identifier_abbreviation_in_combined_cell(self) -> None:
        data = {
            "results": [
                {
                    "path": "x",
                    "provider": "gh",
                    "current": "sha256:deadbeefcafebabe0123456789abcdef01",
                    "candidate": "sha256:abcdef0123456789abcdef0123456789ab",
                    "status": "outdated",
                    "kind": "digest-refresh",
                    "applicable": True,
                    "reason": None,
                },
            ],
        }
        out = self._render(data)
        self.assertIn("sha256:deadb... -> sha256:abcde...", out)
        self.assertNotIn("deadbeefcafebabe", out)
        self.assertNotIn("abcdef0123456789abcdef", out)

    def test_equal_candidate_suppressed(self) -> None:
        data = {
            "results": [
                {
                    "path": "x",
                    "provider": "gh",
                    "current": "1.2.3",
                    "candidate": "1.2.3",
                    "status": "current",
                    "kind": "version",
                    "applicable": False,
                    "reason": None,
                },
            ],
        }
        out = self._render(data)
        self.assertIn("1.2.3 -> -", out)

    def test_absent_candidate_renders_dash(self) -> None:
        data = {
            "results": [
                {
                    "path": "x",
                    "provider": "npm",
                    "current": "1.0.0",
                    "candidate": None,
                    "status": "unavailable",
                    "kind": "version",
                    "applicable": False,
                    "reason": None,
                },
            ],
        }
        out = self._render(data)
        self.assertIn("1.0.0 -> -", out)

    def test_current_to_next_transition(self) -> None:
        data = {
            "results": [
                {
                    "path": "x",
                    "provider": "npm",
                    "current": "1.0.0",
                    "candidate": "2.0.0",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": True,
                    "reason": None,
                },
            ],
        }
        out = self._render(data)
        self.assertIn("1.0.0 -> 2.0.0", out)


class TestCheckUpdatesCompactReport(unittest.TestCase):
    """Default report structure: headers, alignment, status, reasons, width."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = _load_mod()

    def _render(self, data: object) -> str:
        return self.m._render_check_updates_text(data)

    def _assert_compact(self, out: str) -> None:
        self.assertIn("TARGET", out)
        self.assertIn("CURR -> NEXT", out)
        self.assertNotIn("PATH", out)
        self.assertNotIn("CANDIDATE", out)
        self.assertNotIn("APPLICABLE", out)
        self.assertNotIn("DETAIL", out)

    def test_exact_compact_headers(self) -> None:
        out = self._render({"results": []})
        header_line = next(
            line for line in out.splitlines() if "TARGET" in line or "PATH" in line
        )
        self.assertEqual(
            "TARGET  PROVIDER  CURR -> NEXT  STATUS  PUBLISHED",
            header_line,
        )

    def test_deterministic_row_ordering(self) -> None:
        data = {
            "results": [
                {
                    "path": "build.stages.toolchain.ty",
                    "provider": "pypi",
                    "current": "0.0.61",
                    "candidate": "0.0.62",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": True,
                    "reason": None,
                },
                {
                    "path": "runtime.pi-extensions.foo",
                    "provider": "gh",
                    "current": "1.0.0",
                    "candidate": "2.0.0",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": True,
                    "reason": None,
                },
            ],
        }
        out = self._render(data)
        self._assert_compact(out)
        self.assertLess(
            out.index("toolchain.ty"), out.index("runtime.pi-extensions.foo")
        )

    def test_column_alignment_is_deterministic(self) -> None:
        data = {
            "results": [
                {
                    "path": "build.stages.toolchain.ty",
                    "provider": "pypi",
                    "current": "0.0.61",
                    "candidate": "0.0.62",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": True,
                    "reason": None,
                    "published_at": "2025-03-15T10:30:00Z",
                },
            ],
        }
        out = self._render(data)
        self._assert_compact(out)
        lines = out.splitlines()
        header_line = next(line for line in lines if "TARGET" in line)
        row_line = next(line for line in lines if "toolchain.ty" in line)
        self.assertEqual(header_line.index("TARGET"), row_line.index("toolchain.ty"))
        self.assertEqual(header_line.index("PROVIDER"), row_line.index("pypi"))
        self.assertEqual(
            header_line.index("CURR -> NEXT"), row_line.index("0.0.61 -> 0.0.62")
        )
        self.assertEqual(header_line.index("STATUS"), row_line.index("outdated"))
        self.assertEqual(header_line.index("PUBLISHED"), row_line.index("2025-03-15"))
        self.assertEqual(len(header_line), len(row_line))

    def test_original_status_value_preserved(self) -> None:
        data = {
            "results": [
                {
                    "path": "build.stages.toolchain.ty",
                    "provider": "pypi",
                    "current": "0.0.61",
                    "candidate": None,
                    "status": "unavailable",
                    "kind": "version",
                    "applicable": False,
                    "reason": "provider down",
                },
            ],
        }
        out = self._render(data)
        self._assert_compact(out)
        # The status cell uses the original serialized status value.
        self.assertIn("unavailable", out)

    def test_reason_excluded_from_row(self) -> None:
        reason = "connection timed out after 30s"
        data = {
            "results": [
                {
                    "path": "build.stages.toolchain.ty",
                    "provider": "pypi",
                    "current": "0.0.61",
                    "candidate": None,
                    "status": "unavailable",
                    "kind": "version",
                    "applicable": False,
                    "reason": reason,
                },
            ],
        }
        out = self._render(data)
        self._assert_compact(out)
        row_line = next(line for line in out.splitlines() if "toolchain.ty" in line)
        self.assertNotIn(reason, row_line)

    def test_complete_reason_in_details_section(self) -> None:
        reason = "connection timed out after 30s: connection refused"
        data = {
            "results": [
                {
                    "path": "build.stages.toolchain.ty",
                    "provider": "pypi",
                    "current": "0.0.61",
                    "candidate": None,
                    "status": "unavailable",
                    "kind": "version",
                    "applicable": False,
                    "reason": reason,
                },
            ],
        }
        out = self._render(data)
        self._assert_compact(out)
        self.assertIn("Details:", out)
        self.assertIn(reason, out)
        self.assertGreater(out.index(reason), out.index("Details:"))
        details = out[out.index("Details:") :]
        self.assertIn("toolchain.ty", details)
        self.assertLess(details.index("toolchain.ty"), details.index(reason))
        self.assertNotIn("build.stages.toolchain.ty", details)

    def test_details_section_omitted_without_reasons(self) -> None:
        data = {
            "results": [
                {
                    "path": "build.stages.toolchain.ty",
                    "provider": "pypi",
                    "current": "0.0.61",
                    "candidate": "0.0.62",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": True,
                    "reason": None,
                },
            ],
        }
        out = self._render(data)
        self._assert_compact(out)
        self.assertNotIn("Details:", out)

    def test_empty_string_reason_omits_details(self) -> None:
        data = {
            "results": [
                {
                    "path": "build.stages.toolchain.ty",
                    "provider": "pypi",
                    "current": "0.0.61",
                    "candidate": "0.0.62",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": True,
                    "reason": "",
                },
            ],
        }
        out = self._render(data)
        self._assert_compact(out)
        self.assertNotIn("Details:", out)

    def test_output_independent_of_terminal_width(self) -> None:
        data = {
            "results": [
                {
                    "path": "build.stages.toolchain.ty",
                    "provider": "pypi",
                    "current": "0.0.61",
                    "candidate": "0.0.62",
                    "status": "outdated",
                    "kind": "version",
                    "applicable": True,
                    "reason": None,
                    "published_at": "2025-03-15T10:30:00Z",
                },
            ],
        }
        _, out_plain, _ = _run(
            self.m,
            ["check-updates"],
            dispatcher=_make_fake(self.m, exit_kind="success", data=data),
            stdout_isatty=False,
        )
        _, out_tty, _ = _run(
            self.m,
            ["check-updates"],
            dispatcher=_make_fake(self.m, exit_kind="success", data=data),
            stdout_isatty=True,
        )
        body_plain = re.sub(r"\x1b\[[0-9;]*m", "", out_plain)
        body_tty = re.sub(r"\x1b\[[0-9;]*m", "", out_tty)
        self.assertEqual(body_plain, body_tty)
        self.assertIn("CURR -> NEXT", body_plain)


class TestCheckUpdatesPolicyExits(unittest.TestCase):
    """``check-updates`` maps domain outcomes to correct exit codes."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = _load_mod()

    def test_all_current_exits_zero(self) -> None:
        data = {
            "results": [
                {"path": "build.stages.base.node", "status": "current"},
                {"path": "build.stages.toolchain.rust.version", "status": "current"},
            ]
        }
        fake = _make_fake(self.m, exit_kind="success", data=data)
        rc, _, _ = _run(self.m, ["check-updates"], dispatcher=fake)
        self.assertEqual(0, rc)

    def test_outdated_without_fail_policy_exits_zero(self) -> None:
        data = {
            "results": [
                {
                    "path": "build.stages.base.node",
                    "status": "outdated",
                    "applicable": True,
                },
            ]
        }
        fake = _make_fake(self.m, exit_kind="success", data=data)
        rc, _, _ = _run(self.m, ["check-updates"], dispatcher=fake)
        self.assertEqual(0, rc)

    def test_outdated_with_fail_on_outdated_exits_policy(self) -> None:
        data = {
            "results": [
                {
                    "path": "build.stages.base.node",
                    "status": "outdated",
                    "applicable": True,
                },
            ]
        }
        fake = _make_fake(self.m, exit_kind="policy", data=data)
        rc, _, _ = _run(
            self.m, ["check-updates", "--fail-on-outdated"], dispatcher=fake
        )
        self.assertEqual(1, rc)

    def test_provider_failure_strict_exits_operational(self) -> None:
        data = {
            "results": [
                {
                    "path": "build.stages.base.node",
                    "status": "unavailable",
                    "reason": "network timeout",
                },
            ]
        }
        fake = _make_fake(
            self.m, exit_kind="operational", data=data, message="provider failure"
        )
        rc, _, _ = _run(self.m, ["check-updates", "--strict"], dispatcher=fake)
        self.assertEqual(4, rc)

    def test_provider_failure_non_strict_exits_success(self) -> None:
        data = {
            "results": [
                {
                    "path": "build.stages.base.node",
                    "status": "unavailable",
                    "reason": "network timeout",
                },
            ]
        }
        fake = _make_fake(
            self.m, exit_kind="success", data=data, message="1 provider unavailable"
        )
        rc, _, _ = _run(self.m, ["check-updates"], dispatcher=fake)
        self.assertEqual(0, rc)


# ════════════════════════════════════════════════════════════════════════
# Cross-cutting — no Docker, no network, no mutation
# ════════════════════════════════════════════════════════════════════════


class TestReadOnlySafety(unittest.TestCase):
    """Read-only commands never invoke Docker or mutate inventory."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = _load_mod()

    def test_validate_does_not_invoke_docker(self) -> None:
        import subprocess
        from unittest.mock import patch

        fake = _make_fake(self.m, exit_kind="success", data={"valid": True})
        with patch.object(subprocess, "run", side_effect=RuntimeError("no subprocess")):
            rc, _, _ = _run(self.m, ["validate"], dispatcher=fake)
            self.assertEqual(0, rc)

    def test_show_does_not_invoke_docker(self) -> None:
        import subprocess
        from unittest.mock import patch

        fake = _make_fake(self.m, exit_kind="success", data={"node": {"tag": "24"}})
        with patch.object(subprocess, "run", side_effect=RuntimeError("no subprocess")):
            rc, _, _ = _run(self.m, ["show", "--scope", "build"], dispatcher=fake)
            self.assertEqual(0, rc)

    def test_check_updates_does_not_invoke_docker(self) -> None:
        import subprocess
        from unittest.mock import patch

        fake = _make_fake(self.m, exit_kind="success", data={"results": []})
        with patch.object(subprocess, "run", side_effect=RuntimeError("no subprocess")):
            rc, _, _ = _run(self.m, ["check-updates"], dispatcher=fake)
            self.assertEqual(0, rc)


# ════════════════════════════════════════════════════════════════════════
# 8.3 — Command surface and execution-boundary safety
# ════════════════════════════════════════════════════════════════════════

_LEGACY_COMMANDS = {"compose", "env", "get", "extensions"}

_READ_ONLY_COMMANDS = [
    ["validate"],
    ["validate", "--scope", "build"],
    ["show", "--scope", "all"],
    ["show", "--scope", "build", "--effective"],
    ["check-updates"],
    ["check-updates", "--suggest"],
]


class TestUnknownCommands(unittest.TestCase):
    """Unrecognised commands (including legacy names) cause input errors."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = _load_mod()

    def test_schema_rejected_as_unknown_command(self) -> None:
        """``schema`` is not a command — it is a top-level TOML key."""
        rc, _, err = _run(self.m, ["schema"])
        self.assertEqual(2, rc)
        self.assertIn("invalid choice", err.lower())

    def test_legacy_compose_absent(self) -> None:
        _, out, _ = _run(self.m, ["--help"])
        self.assertNotIn("compose", out)

    def test_legacy_env_absent(self) -> None:
        _, out, _ = _run(self.m, ["--help"])
        self.assertNotIn("env", out)

    def test_legacy_get_absent(self) -> None:
        _, out, _ = _run(self.m, ["--help"])
        self.assertNotIn("get", out)

    def test_legacy_extensions_absent(self) -> None:
        _, out, _ = _run(self.m, ["--help"])
        self.assertNotIn("extensions", out)

    def test_legacy_commands_all_rejected(self) -> None:
        for cmd in sorted(_LEGACY_COMMANDS):
            rc, _, err = _run(self.m, [cmd])
            self.assertEqual(2, rc, f"{cmd!r} must be rejected")
            self.assertIn(
                "invalid choice",
                err.lower(),
                f"{cmd!r} error must mention invalid choice",
            )


# ════════════════════════════════════════════════════════════════════════
# 8.3 — Fake provider / transport boundary
# ════════════════════════════════════════════════════════════════════════

_RECORDING_PROVIDER_CALLS: list[dict] = []


class _RecordingStubProvider:
    """Fake provider that records every discover() call.

    Returns the current value as candidate ("current" status) so
    check-updates exits 0 without fabricating outdated results.
    """

    def __init__(self, name: str) -> None:
        self.name = name

    def discover(self, target: object, context: object) -> object:
        from docker.versioning.model import UpdateCandidate, UpdateKind
        from docker.versioning.providers.base import ProviderResult

        _RECORDING_PROVIDER_CALLS.append(
            {
                "provider": self.name,
                "path": getattr(target, "path", "?"),
                "current": getattr(target, "current", "?"),
            }
        )
        return ProviderResult(
            candidate=UpdateCandidate(
                value=getattr(target, "current", ""),
                kind=UpdateKind.VERSION,
                artifacts={},
            ),
        )


def _make_recording_providers():
    """Return a MappingProxyType with recording stubs for every known provider."""
    from types import MappingProxyType

    _RECORDING_PROVIDER_CALLS.clear()
    return MappingProxyType(
        {
            k: _RecordingStubProvider(k)
            for k in (
                "docker-registry",
                "rust-channel",
                "static-url",
                "github-release",
                "uv-python",
                "pypi",
                "npm",
                "git-ref",
            )
        }
    )


class TestAllExecutionBoundariesGuarded(unittest.TestCase):
    """Every execution boundary must refuse to run during read-only commands.

    subprocess.run, networking diagnosis, and gateway persistence are
    blocked so any accidental invocation fails hard.

    Pure rendering and in-memory serialization (e.g. effective projection
    resolution) are NOT bombed — they are legitimate read-only operations.

    Uses the real (default) dispatcher — no injected success fakes.
    Commands exercise actual inventory parsing, which means these tests
    are RED until Stage 8.4 wires the real dispatcher.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = _load_mod()

    def _patch_all_boundaries(self):
        """Return a context manager that bombs every execution boundary."""
        import contextlib
        import subprocess
        from unittest.mock import patch

        def _bomb(*a: object, **kw: object) -> None:
            raise RuntimeError("execution boundary must not be called")

        patches = [
            patch.object(subprocess, "run", side_effect=_bomb),
            patch.object(subprocess, "Popen", side_effect=_bomb),
        ]
        try:
            import docker.networking

            for fn in ("diagnose_gateway", "probe_gateway", "apply_rootless_override"):
                patches.append(patch.object(docker.networking, fn, side_effect=_bomb))
        except ImportError:
            pass

        @contextlib.contextmanager
        def _manager():
            entered: list = []
            try:
                for p in patches:
                    entered.append(p.__enter__())
                yield
            finally:
                for p in reversed(patches):
                    p.__exit__(None, None, None)

        return _manager()

    # ── no fake — expects real dispatcher (RED until 8.4) ──────────

    def test_validate_under_boundary_guard(self) -> None:
        with self._patch_all_boundaries():
            rc, out, _ = _run(self.m, ["validate"])
        # RED: default dispatcher returns "unavailable" (exit 2);
        # will pass with exit 0 when Stage 8.4 wires the real dispatcher
        self.assertEqual(0, rc, "expected SUCCESS when real dispatcher is wired")
        self.assertIn("valid", out.lower())

    def test_show_under_boundary_guard(self) -> None:
        with self._patch_all_boundaries():
            rc, out, _ = _run(
                self.m,
                ["show", "--scope", "build"],
            )
        self.assertEqual(0, rc)
        self.assertIn("node", out.lower())

    def test_show_effective_under_boundary_guard(self) -> None:
        with self._patch_all_boundaries():
            rc, out, _ = _run(
                self.m,
                ["show", "--scope", "build", "--effective"],
            )
        self.assertEqual(0, rc)
        self.assertIn("rust", out.lower())

    def test_check_updates_under_boundary_guard(self) -> None:
        import docker.versioning.updates
        from unittest.mock import patch

        fake_providers = _make_recording_providers()
        with patch.object(
            docker.versioning.updates, "_DEFAULT_PROVIDERS", fake_providers
        ):
            with self._patch_all_boundaries():
                rc, out, _ = _run(self.m, ["check-updates"])
        self.assertEqual(0, rc, "expected SUCCESS when real dispatcher is wired")
        provider_names = {c["provider"] for c in _RECORDING_PROVIDER_CALLS}
        self.assertGreater(
            len(_RECORDING_PROVIDER_CALLS), 0, "providers must be called"
        )
        self.assertIn("docker-registry", provider_names)

    def test_check_updates_suggest_under_boundary_guard(self) -> None:
        import docker.versioning.updates
        from unittest.mock import patch

        fake_providers = _make_recording_providers()
        with patch.object(
            docker.versioning.updates, "_DEFAULT_PROVIDERS", fake_providers
        ):
            with self._patch_all_boundaries():
                rc, out, _ = _run(
                    self.m,
                    ["check-updates", "--suggest"],
                )
        self.assertEqual(0, rc)
        provider_names = {c["provider"] for c in _RECORDING_PROVIDER_CALLS}
        self.assertIn("docker-registry", provider_names)


class TestInventoryImmutability(unittest.TestCase):
    """Read-only commands never mutate the on-disk inventory.

    Uses the real (default) dispatcher — no injected success fakes.
    Commands exercise actual inventory parsing against a real TOML file.
    These tests are RED until Stage 8.4 wires the real dispatcher;
    currently every command returns "unavailable".
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = _load_mod()

    def _temp_inventory(self) -> str:
        """Return a temporary byte-for-byte copy of the canonical inventory."""
        import pathlib
        import tempfile

        repo_root = pathlib.Path(__file__).resolve().parent.parent
        canonical = repo_root / "docker-constructor.toml"
        project = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, project, True)
        inventory = project / "docker-constructor.toml"
        inventory.write_bytes(canonical.read_bytes())
        return str(inventory)

    # ── no fake — expects real dispatcher (RED until 8.4) ──────────

    def test_validate_preserves_inventory_bytes(self) -> None:
        import pathlib

        path = pathlib.Path(self._temp_inventory())
        try:
            before = path.read_bytes()
            rc, out, _ = _run(
                self.m,
                ["--project-directory", str(pathlib.Path(path).parent), "validate"],
            )
            after = path.read_bytes()
            # RED: default dispatcher returns "unavailable" (exit 2);
            # will pass with exit 0 when Stage 8.4 wires the real dispatcher
            self.assertEqual(0, rc, "validate must succeed with real dispatcher")
            self.assertIn("valid", out.lower())
            self.assertEqual(before, after, "validate must not mutate inventory")
        finally:
            path.unlink(missing_ok=True)

    def test_show_preserves_inventory_bytes(self) -> None:
        import pathlib

        path = pathlib.Path(self._temp_inventory())
        try:
            before = path.read_bytes()
            rc, out, _ = _run(
                self.m,
                [
                    "--project-directory",
                    str(pathlib.Path(path).parent),
                    "show",
                    "--scope",
                    "build",
                ],
            )
            after = path.read_bytes()
            self.assertEqual(0, rc, "show must succeed with real dispatcher")
            self.assertIn("node", out.lower())
            self.assertEqual(before, after, "show must not mutate inventory")
        finally:
            path.unlink(missing_ok=True)

    def test_show_effective_preserves_inventory_bytes(self) -> None:
        import pathlib

        path = pathlib.Path(self._temp_inventory())
        try:
            before = path.read_bytes()
            rc, out, _ = _run(
                self.m,
                [
                    "--project-directory",
                    str(pathlib.Path(path).parent),
                    "show",
                    "--scope",
                    "build",
                    "--effective",
                ],
            )
            after = path.read_bytes()
            self.assertEqual(0, rc, "show --effective must succeed")
            self.assertIn("rust", out.lower())
            self.assertEqual(
                before, after, "show --effective must not mutate inventory"
            )
        finally:
            path.unlink(missing_ok=True)

    def test_check_updates_preserves_inventory_bytes(self) -> None:
        import pathlib
        import docker.versioning.updates
        from unittest.mock import patch

        path = pathlib.Path(self._temp_inventory())
        try:
            before = path.read_bytes()
            fake_providers = _make_recording_providers()
            with patch.object(
                docker.versioning.updates, "_DEFAULT_PROVIDERS", fake_providers
            ):
                rc, out, _ = _run(
                    self.m,
                    [
                        "--project-directory",
                        str(pathlib.Path(path).parent),
                        "check-updates",
                    ],
                )
            after = path.read_bytes()
            self.assertEqual(0, rc, "check-updates must succeed with real dispatcher")
            self.assertEqual(before, after, "check-updates must not mutate inventory")
            self.assertGreater(len(_RECORDING_PROVIDER_CALLS), 0)
        finally:
            path.unlink(missing_ok=True)

    def test_check_updates_suggest_preserves_inventory_bytes(self) -> None:
        import pathlib
        import docker.versioning.updates
        from unittest.mock import patch

        path = pathlib.Path(self._temp_inventory())
        try:
            before = path.read_bytes()
            fake_providers = _make_recording_providers()
            with patch.object(
                docker.versioning.updates, "_DEFAULT_PROVIDERS", fake_providers
            ):
                rc, out, _ = _run(
                    self.m,
                    [
                        "--project-directory",
                        str(pathlib.Path(path).parent),
                        "check-updates",
                        "--suggest",
                    ],
                )
            after = path.read_bytes()
            self.assertEqual(0, rc, "check-updates --suggest must succeed")
            self.assertEqual(
                before, after, "check-updates --suggest must not mutate inventory"
            )
            self.assertGreater(len(_RECORDING_PROVIDER_CALLS), 0)
        finally:
            path.unlink(missing_ok=True)

    def test_no_dot_docker_generated_created(self) -> None:
        """Read-only commands must never create .docker-generated."""
        import contextlib
        import pathlib
        import tempfile
        import docker.versioning.updates
        from unittest.mock import patch

        path = pathlib.Path(self._temp_inventory())
        with tempfile.TemporaryDirectory() as tmp:
            orig = os.getcwd()
            try:
                os.chdir(tmp)
                for argv in _READ_ONLY_COMMANDS:
                    ctx = contextlib.nullcontext()
                    if "check-updates" in argv:
                        fake_providers = _make_recording_providers()
                        ctx = patch.object(
                            docker.versioning.updates,
                            "_DEFAULT_PROVIDERS",
                            fake_providers,
                        )
                    with ctx:
                        rc, out, _ = _run(
                            self.m,
                            ["--project-directory", str(pathlib.Path(path).parent)]
                            + argv,
                        )
                    # RED until 8.4: default dispatcher returns "unavailable"
                    self.assertEqual(
                        0, rc, f"{argv}: must succeed with real dispatcher"
                    )
                    gen_dir = pathlib.Path(tmp) / ".docker-generated"
                    self.assertFalse(
                        gen_dir.exists(),
                        f"{argv} created .docker-generated",
                    )
            finally:
                os.chdir(orig)
                path.unlink(missing_ok=True)

    def test_selected_project_inventory_also_unchanged(self) -> None:
        """A selected project's fixed inventory remains byte-for-byte unchanged."""
        import contextlib
        import pathlib
        import docker.versioning.updates
        from unittest.mock import patch

        path = pathlib.Path(self._temp_inventory())
        try:
            before = path.read_bytes()
            for argv in _READ_ONLY_COMMANDS:
                ctx = contextlib.nullcontext()
                if "check-updates" in argv:
                    fake_providers = _make_recording_providers()
                    ctx = patch.object(
                        docker.versioning.updates,
                        "_DEFAULT_PROVIDERS",
                        fake_providers,
                    )
                with ctx:
                    rc, out, _ = _run(
                        self.m,
                        ["--project-directory", str(pathlib.Path(path).parent)] + argv,
                    )
                # RED until 8.4
                self.assertEqual(0, rc, f"{argv}: must succeed with real dispatcher")
                after = path.read_bytes()
                self.assertEqual(
                    before,
                    after,
                    f"{argv} mutated project inventory {path}",
                )
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()

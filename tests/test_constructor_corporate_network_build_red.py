"""RED contracts for the Phase 2 build-time trust and proxy boundary.

These tests precede the GREEN Phase 2 implementation and define the
build-vector and Dockerfile/build-context contract for ``[corporate-trust]``
and ``[network.proxy]``:

* disabled corporate settings preserve the existing Docker build vector —
  no bundle is required and no corporate proxy argument is emitted;
* configured proxy URLs and bypass lists are carried through
  constructor-specific build arguments and exported into the standard proxy
  variables only when configured, never as same-named build arguments;
* the fixed bundle has an optional build-context convention: the actual
  bundle stays untracked while a tracked placeholder keeps the
  ``.docker-local`` directory present so a missing disabled bundle never
  makes ``COPY`` fail;
* the Dockerfile validates the fixed bundle against the complete PEM
  framing/Base64 contract (standalone delimiters, no outside text, strictly
  decodable nonempty payloads) before the first base-stage network operation,
  re-applies it after package installs that regenerate the system bundle, and
  points applicable clients at the final system-bundle path only on the
  enabled-trust path;
* proxy build arguments are available to build stages but never converted
  into persistent image ``ENV`` values.

Run this module before tasks 2.6-2.11 and expect failures until that
boundary exists.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

from docker.versioning.build_orchestration import (
    BuildRequest,
    BuildResult,
    ProcessResult,
    orchestrate_build,
)
from docker.versioning.dispatch_types import ExitKind

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CANONICAL = (_REPO_ROOT / "docker-constructor.toml").read_text()

_PROXY_URL_NAMES = (
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
)

_PROXY_ARG_NAMES = _PROXY_URL_NAMES + ("NO_PROXY", "no_proxy")

_CONSTRUCTOR_PROXY_ARGS = ("PI_CORPORATE_PROXY_URL", "PI_CORPORATE_NO_PROXY")

_CLIENT_CA_ARG_NAMES = (
    "NODE_EXTRA_CA_CERTS",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "PIP_CERT",
    "CURL_CA_BUNDLE",
)

_SYSTEM_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"

_VALID_BUNDLE = (
    "-----BEGIN CERTIFICATE-----\n"
    "AQIDBAU=\n"
    "-----END CERTIFICATE-----\n"
)

_DOCKERFILE_INSTRUCTIONS = {
    "ADD", "ARG", "CMD", "COPY", "ENTRYPOINT", "ENV", "EXPOSE", "FROM",
    "HEALTHCHECK", "LABEL", "ONBUILD", "RUN", "SHELL", "STOPSIGNAL", "USER",
    "VOLUME", "WORKDIR",
}

_NETWORK_MARKERS = (
    "apt-get update",
    "curl -fsSL",
    "npm install",
    "/tmp/rustup-init -y",
    "setup-python.sh",
    "uv tool install",
    "setup-zsh.sh",
)

_EXPECTED_NETWORKED_RUNS = {
    ("base", "apt-get update"),
    ("toolchain", "apt-get update"),
    ("toolchain", "/tmp/rustup-init -y"),
    ("toolchain", "setup-python.sh"),
    ("toolchain", "uv tool install"),
    ("openspec-tools", "npm install"),
    ("runtime", "setup-zsh.sh"),
}


def _starts_instruction(line: str) -> bool:
    """True when *line* begins a top-level Dockerfile instruction."""
    if not line or line[0].isspace() or line.startswith("#"):
        return False
    return line.split()[0] in _DOCKERFILE_INSTRUCTIONS


def _recording_build_executor(effects: list[str]):
    class _Exec:
        def run(self, argv: tuple[str, ...]) -> ProcessResult:
            effects.append("docker-build")
            return ProcessResult(argv=argv, return_code=0, stdout="", stderr="")

    return _Exec()


def _build_arg_pairs(args: tuple[str, ...] | str) -> dict[str, str]:
    """Extract dry-run display or executable ``--build-arg`` pairs."""
    if isinstance(args, str):
        args = tuple(shlex.split(args.split("\n", 1)[-1]))
    it = iter(args)
    pairs: dict[str, str] = {}
    for token in it:
        if token == "--build-arg":
            raw = next(it)
            name, _, value = raw.partition("=")
            pairs[name] = value
    return pairs


class _BuildOrchestrationRed(unittest.TestCase):
    """Runs ``orchestrate_build`` against a real inventory + local companion."""

    def build_with_local(
        self,
        companion: str | None,
        bundle: str | None = None,
    ) -> BuildResult:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            inventory = root_path / "inventory.toml"
            inventory.write_text(_CANONICAL)
            if companion is not None:
                (root_path / "docker-constructor.local.toml").write_text(companion)
            if bundle is not None:
                bundle_dir = root_path / ".docker-local"
                bundle_dir.mkdir()
                (bundle_dir / "corporate-ca-bundle.crt").write_text(bundle)
            effects: list[str] = []
            result = orchestrate_build(BuildRequest(
                inventory_path=str(inventory),
                repo_root=str(root_path), project_root=str(root_path),
                confirmed=False,
                dry_run=True,
                runner=_recording_build_executor(effects),
            ))
            self.assertEqual([], effects)
            return result


class TestDisabledBuildVectorRed(_BuildOrchestrationRed):
    """Task 2.1: disabled corporate settings preserve the default vector."""

    def test_disabled_build_vector_emits_no_proxy_args(self) -> None:
        result = self.build_with_local(None)
        self.assertEqual(ExitKind.SUCCESS, result.exit_kind, result.message)
        pairs = _build_arg_pairs(result.display_string or "")
        for name in _CONSTRUCTOR_PROXY_ARGS:
            self.assertNotIn(name, pairs)
        for name in _PROXY_ARG_NAMES:
            self.assertNotIn(name, pairs)

    def test_disabled_trust_requires_no_bundle(self) -> None:
        # No .docker-local directory is created; an explicitly disabled
        # [corporate-trust] must not require the fixed bundle to exist.
        result = self.build_with_local("[corporate-trust]\nenabled = false\n")
        self.assertEqual(ExitKind.SUCCESS, result.exit_kind, result.message)
        pairs = _build_arg_pairs(result.display_string or "")
        for name in _CONSTRUCTOR_PROXY_ARGS:
            self.assertNotIn(name, pairs)
        for name in _PROXY_ARG_NAMES:
            self.assertNotIn(name, pairs)

    def test_disabled_build_injects_no_client_ca_args(self) -> None:
        # A disabled build must not inject constructor-specific trust or client
        # CA-path arguments.
        for companion in (None, "[corporate-trust]\nenabled = false\n"):
            with self.subTest(companion=companion):
                result = self.build_with_local(companion)
                self.assertEqual(ExitKind.SUCCESS, result.exit_kind, result.message)
                pairs = _build_arg_pairs(result.display_string or "")
                self.assertNotIn("CORPORATE_TRUST_ENABLED", pairs)
                self.assertNotIn("PI_CORPORATE_CA_PATH", pairs)
                for name in _CLIENT_CA_ARG_NAMES:
                    self.assertNotIn(name, pairs)

    def test_disabled_with_existing_bundle_emits_no_trust_args(self) -> None:
        # A stale .docker-local/corporate-ca-bundle.crt must not enable trust
        # replacement when the section is absent or disabled.
        for companion in (None, "[corporate-trust]\nenabled = false\n"):
            with self.subTest(companion=companion):
                result = self.build_with_local(companion, bundle=_VALID_BUNDLE)
                self.assertEqual(ExitKind.SUCCESS, result.exit_kind, result.message)
                pairs = _build_arg_pairs(result.display_string or "")
                self.assertNotIn("CORPORATE_TRUST_ENABLED", pairs)
                self.assertNotIn("PI_CORPORATE_CA_PATH", pairs)
                for name in _CLIENT_CA_ARG_NAMES:
                    self.assertNotIn(name, pairs)


class TestEnabledTrustBuildVectorRed(_BuildOrchestrationRed):
    """Enabled trust injects constructor-specific trust arguments."""

    def test_enabled_trust_injects_constructor_specific_args(self) -> None:
        result = self.build_with_local(
            "[corporate-trust]\nenabled = true\n",
            bundle=_VALID_BUNDLE,
        )
        self.assertEqual(ExitKind.SUCCESS, result.exit_kind, result.message)
        pairs = _build_arg_pairs(result.display_string or "")
        self.assertEqual("true", pairs.get("CORPORATE_TRUST_ENABLED"))
        self.assertEqual(
            "/etc/ssl/certs/ca-certificates.crt",
            pairs.get("PI_CORPORATE_CA_PATH"),
        )
        # The client variables are set inside the Dockerfile (via the helper),
        # never passed as same-named build arguments.
        for name in _CLIENT_CA_ARG_NAMES:
            self.assertNotIn(name, pairs)


class TestConfiguredProxyBuildVectorRed(_BuildOrchestrationRed):
    """Task 2.4: configured proxy under constructor-specific build args."""

    def test_configured_proxy_emitted_under_constructor_specific_arg(self) -> None:
        # The endpoint is carried verbatim via a constructor-specific argument —
        # including SOCKS schemes, which are best-effort, never reinterpreted.
        for url in (
            "http://proxy.corp.example:3128",
            "socks5h://proxy.corp.example:1080",
        ):
            with self.subTest(url=url):
                result = self.build_with_local(
                    f'[network.proxy]\nurl = "{url}"\n'
                )
                self.assertEqual(ExitKind.SUCCESS, result.exit_kind, result.message)
                pairs = _build_arg_pairs(result.display_string or "")
                self.assertEqual(url, pairs.get("PI_CORPORATE_PROXY_URL"))
                self.assertNotIn("PI_CORPORATE_NO_PROXY", pairs)
                # Same-named proxy build args would be overridden by inherited ENV.
                for name in _PROXY_ARG_NAMES:
                    self.assertNotIn(name, pairs)

    def test_no_proxy_emitted_only_when_explicitly_configured(self) -> None:
        configured = self.build_with_local(
            '[network.proxy]\n'
            'url = "http://proxy.corp.example:3128"\n'
            'no_proxy = "localhost,.corp.example"\n'
        )
        self.assertEqual(ExitKind.SUCCESS, configured.exit_kind, configured.message)
        pairs = _build_arg_pairs(configured.display_string or "")
        self.assertEqual("localhost,.corp.example", pairs.get("PI_CORPORATE_NO_PROXY"))

        unconfigured = self.build_with_local(
            '[network.proxy]\nurl = "http://proxy.corp.example:3128"\n'
        )
        self.assertEqual(ExitKind.SUCCESS, unconfigured.exit_kind, unconfigured.message)
        pairs2 = _build_arg_pairs(unconfigured.display_string or "")
        self.assertNotIn("PI_CORPORATE_NO_PROXY", pairs2)


class TestBundleBuildContextConventionRed(unittest.TestCase):
    """Task 2.2: optional fixed-bundle convention without a missing-COPY."""

    def test_bundle_ignored_and_directory_has_tracked_placeholder(self) -> None:
        gitignore = (_REPO_ROOT / ".gitignore").read_text()
        self.assertIn(
            "corporate-ca-bundle.crt",
            gitignore,
            ".gitignore must keep the actual bundle untracked",
        )
        dockerignore = (_REPO_ROOT / ".dockerignore").read_text()
        self.assertNotIn(
            ".docker-local",
            dockerignore,
            ".dockerignore must not exclude the fixed bundle from the build context",
        )
        bundle_dir = _REPO_ROOT / ".docker-local"
        self.assertTrue(
            bundle_dir.is_dir(),
            ".docker-local placeholder directory must exist in the repository",
        )
        self.assertTrue(
            any(bundle_dir.iterdir()),
            ".docker-local needs a tracked placeholder so the directory is always present",
        )

    def test_dockerfile_uses_optional_directory_copy_not_bare_file_copy(self) -> None:
        text = (_REPO_ROOT / "Dockerfile").read_text()
        self.assertNotIn(
            "COPY .docker-local/corporate-ca-bundle.crt",
            text,
            "a bare file COPY fails when the disabled bundle is missing",
        )
        self.assertIn(
            "COPY .docker-local/",
            text,
            "the bundle must be exposed via the always-present directory convention",
        )


class TestCorporateNetworkEnvironmentHelper(unittest.TestCase):
    """Focused contracts for the conditional build-command environment."""

    _HELPER = _REPO_ROOT / "docker" / "corp-network-env.sh"

    def _source_helper(self, overrides: dict[str, str]) -> dict[str, str]:
        environment = os.environ.copy()
        for name in (
            "CORPORATE_TRUST_ENABLED",
            "PI_CORPORATE_CA_PATH",
            *_CLIENT_CA_ARG_NAMES,
        ):
            environment.pop(name, None)
        environment.update(overrides)
        result = subprocess.run(
            ["sh", "-c", 'set -eu; . "$1"; env -0', "sh", str(self._HELPER)],
            env=environment,
            check=True,
            capture_output=True,
        )
        return dict(
            entry.decode().split("=", 1)
            for entry in result.stdout.split(b"\0")
            if entry
        )

    def test_enabled_trust_exports_complete_fixed_client_ca_mapping(self) -> None:
        inherited = {
            name: f"/inherited/{name.lower()}" for name in _CLIENT_CA_ARG_NAMES
        }
        environment = self._source_helper({
            **inherited,
            "CORPORATE_TRUST_ENABLED": "true",
            "PI_CORPORATE_CA_PATH": _SYSTEM_CA_BUNDLE,
        })
        self.assertEqual(
            {name: _SYSTEM_CA_BUNDLE for name in _CLIENT_CA_ARG_NAMES},
            {name: environment.get(name) for name in _CLIENT_CA_ARG_NAMES},
        )

    def test_absent_or_disabled_trust_introduces_no_client_ca_values(self) -> None:
        for trust_value in (None, "false"):
            with self.subTest(trust_value=trust_value):
                overrides = {"PI_CORPORATE_CA_PATH": _SYSTEM_CA_BUNDLE}
                if trust_value is not None:
                    overrides["CORPORATE_TRUST_ENABLED"] = trust_value
                environment = self._source_helper(overrides)
                for name in _CLIENT_CA_ARG_NAMES:
                    self.assertNotIn(name, environment)

    def test_absent_or_disabled_trust_preserves_inherited_client_ca_values(self) -> None:
        inherited = {
            name: f"/inherited/{name.lower()}" for name in _CLIENT_CA_ARG_NAMES
        }
        for trust_value in (None, "false"):
            with self.subTest(trust_value=trust_value):
                overrides = dict(inherited)
                overrides["PI_CORPORATE_CA_PATH"] = _SYSTEM_CA_BUNDLE
                if trust_value is not None:
                    overrides["CORPORATE_TRUST_ENABLED"] = trust_value
                environment = self._source_helper(overrides)
                self.assertEqual(
                    inherited,
                    {name: environment.get(name) for name in _CLIENT_CA_ARG_NAMES},
                )


class TestDockerfileTrustReplacementRed(unittest.TestCase):
    """Task 2.3: enabled bundle validation and replacement before network."""

    def test_base_stage_replaces_system_trust_before_network_operations(self) -> None:
        text = (_REPO_ROOT / "Dockerfile").read_text()
        self.assertIn("/etc/ssl/certs/ca-certificates.crt", text)
        # The bundle is validated by the dedicated strict validator — never by
        # substring greps that accept arbitrary surrounding text.
        self.assertIn("COPY docker/validate-corporate-bundle.sh", text)
        self.assertIn("sh /tmp/validate-corporate-bundle.sh", text)
        self.assertNotIn('grep -q "BEGIN CERTIFICATE"', text)
        self.assertNotIn('grep -q "END CERTIFICATE"', text)
        lines = text.splitlines()
        apt_indices = [i for i, line in enumerate(lines) if "apt-get update" in line]
        trust_indices = [
            i for i, line in enumerate(lines)
            if "/etc/ssl/certs/ca-certificates.crt" in line
        ]
        self.assertTrue(apt_indices, "expected a base-stage apt-get network operation")
        self.assertTrue(
            trust_indices,
            "expected trust replacement referencing /etc/ssl/certs/ca-certificates.crt",
        )
        self.assertLess(
            trust_indices[0],
            apt_indices[0],
            "trust replacement must precede the first network operation",
        )

    def test_client_ca_not_persisted_and_not_same_named_args(self) -> None:
        # Client CA-path variables must not be persisted as image ENV (which
        # would alter default client behavior) and must not be declared as
        # same-named ARGs: an ENV inherited from the base image overrides an
        # ARG of the same name, so same-named ARGs could not force the path.
        text = (_REPO_ROOT / "Dockerfile").read_text()
        instructions: list[str] = []
        lines = text.splitlines()
        i = 0
        while i < len(lines):
            if _starts_instruction(lines[i]):
                block = [lines[i]]
                i += 1
                while i < len(lines) and not _starts_instruction(lines[i]):
                    block.append(lines[i])
                    i += 1
                instructions.append("\n".join(block))
            else:
                i += 1
        for var in _CLIENT_CA_ARG_NAMES:
            prohibited = [
                instruction for instruction in instructions
                if instruction.startswith(("ENV ", "ARG "))
                and var in instruction.replace("=", " ").split()
            ]
            self.assertEqual(
                [], prohibited,
                f"{var} must not be a persistent ENV or same-named ARG",
            )
        self.assertIn("ARG PI_CORPORATE_CA_PATH", text)

    def test_client_ca_conditionally_exported_only_when_enabled(self) -> None:
        # A helper sourced by networked RUNs exports the client variables from
        # the constructor-specific path only when trust is enabled, preserving
        # inherited values otherwise.
        helper = (_REPO_ROOT / "docker" / "corp-network-env.sh").read_text()
        self.assertIn('"${CORPORATE_TRUST_ENABLED:-}" = "true"', helper)
        for var in _CLIENT_CA_ARG_NAMES:
            self.assertIn(
                f'export {var}="${{PI_CORPORATE_CA_PATH}}"',
                helper,
            )

    def test_bundle_reapplied_after_ca_certificates_install(self) -> None:
        # Installing ca-certificates regenerates /etc/ssl/certs/ca-certificates.crt,
        # so the initial pre-network replacement is not enough; the final image
        # must re-apply the configured bundle after the install.
        text = (_REPO_ROOT / "Dockerfile").read_text()
        lines = text.splitlines()
        ca_install = [
            i for i, line in enumerate(lines)
            if "ca-certificates" in line and "ca-certificates.crt" not in line
        ]
        self.assertTrue(ca_install, "expected the ca-certificates package install")
        reapply = [
            i for i, line in enumerate(lines)
            if i > ca_install[0]
            and "cp " in line
            and "/etc/ssl/certs/ca-certificates.crt" in line
        ]
        self.assertTrue(
            reapply,
            "the bundle must be re-applied after the ca-certificates install",
        )

    def test_trust_replacement_gated_on_explicit_enabled_arg(self) -> None:
        # Replacement must be gated on the explicit enabled build argument, not
        # on bundle-file presence alone, so a stale bundle cannot change trust.
        text = (_REPO_ROOT / "Dockerfile").read_text()
        self.assertIn("ARG CORPORATE_TRUST_ENABLED", text)
        self.assertEqual(
            2,
            text.count('"${CORPORATE_TRUST_ENABLED:-}" = "true"'),
        )

    def test_every_networked_run_sources_ca_helper(self) -> None:
        # Every networked RUN in every build stage — including the base and
        # toolchain apt-get commands — must source the conditional CA helper,
        # so inherited client CA variables cannot override the replaced bundle.
        text = (_REPO_ROOT / "Dockerfile").read_text()
        lines = text.splitlines()
        stage: str | None = None
        networked_runs: set[tuple[str, str]] = set()
        missing: list[str] = []
        i = 0
        while i < len(lines):
            line = lines[i]
            if line.startswith("FROM ") and " AS " in line:
                stage = line.split(" AS ", 1)[1].strip()
                i += 1
            elif line.startswith("RUN "):
                block = [line]
                i += 1
                while i < len(lines) and not _starts_instruction(lines[i]):
                    block.append(lines[i])
                    i += 1
                joined = "\n".join(block)
                matched_markers = [
                    marker for marker in _NETWORK_MARKERS if marker in joined
                ]
                if matched_markers:
                    run_stage = stage or "<unknown>"
                    first_marker = min(matched_markers, key=joined.index)
                    networked_runs.add((run_stage, first_marker))
                    helper_index = joined.find(". /tmp/corp-network-env.sh")
                    if helper_index < 0 or helper_index > joined.index(first_marker):
                        missing.append(f"{run_stage}: {first_marker}")
            else:
                i += 1
        self.assertEqual(
            _EXPECTED_NETWORKED_RUNS,
            networked_runs,
            "the complete set of networked RUNs changed; update the enumeration",
        )
        self.assertEqual(
            [],
            missing,
            "networked RUNs must source the CA helper before their first network operation",
        )


class TestDockerfileBundleValidationRed(unittest.TestCase):
    """Direct-build coverage: the exact validator the Dockerfile runs
    rejects malformed framing/payload and accepts complete bundles.

    These exercise ``docker/validate-corporate-bundle.sh`` directly (via
    ``sh``, as the Dockerfile does) so the build-time rejection contract is
    proven without a Docker daemon.
    """

    _SCRIPT = _REPO_ROOT / "docker" / "validate-corporate-bundle.sh"
    _BEGIN = "-----BEGIN CERTIFICATE-----"
    _END = "-----END CERTIFICATE-----"

    @staticmethod
    def _validator_exit(content: str | bytes) -> int:
        import os
        import subprocess

        raw = content.encode("latin-1") if isinstance(content, str) else content
        fd, path = tempfile.mkstemp(suffix=".crt")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
            result = subprocess.run(
                ["sh", str(TestDockerfileBundleValidationRed._SCRIPT), path],
                capture_output=True,
            )
            return result.returncode
        finally:
            os.unlink(path)

    def test_validator_accepts_complete_bundles(self) -> None:
        for label, content in (
            ("single block",
             f"{self._BEGIN}\nAQIDBAU=\n{self._END}\n"),
            ("multi-block",
             f"{self._BEGIN}\nAQIDBAU=\n{self._END}\n"
             f"{self._BEGIN}\nCQoLDA0=\n{self._END}\n"),
            ("multi-line payload",
             f"{self._BEGIN}\nAQID\nBAUG\n{self._END}\n"),
            ("CRLF line endings",
             f"{self._BEGIN}\r\nAQIDBAU=\r\n{self._END}\r\n"),
        ):
            with self.subTest(case=label):
                self.assertEqual(0, self._validator_exit(content))

    def test_validator_rejects_substring_only_pem(self) -> None:
        # The prior substring greps accepted this; the strict validator must not.
        content = f"junk {self._BEGIN} and {self._END} trailing junk\n"
        self.assertNotEqual(0, self._validator_exit(content))

    def test_validator_rejects_malformed_framing(self) -> None:
        cases = {
            "empty file": "",
            "unterminated block": f"{self._BEGIN}\nAQIDBAU=\n",
            "unmatched END": f"{self._END}\n",
            "text before block": f"hello\n{self._BEGIN}\nAQIDBAU=\n{self._END}\n",
            "text after block": f"{self._BEGIN}\nAQIDBAU=\n{self._END}\nworld\n",
            "inline delimiter in payload":
                f"{self._BEGIN}\nAQID{self._BEGIN}BAU=\n{self._END}\n",
            "empty payload": f"{self._BEGIN}\n{self._END}\n",
        }
        for label, content in cases.items():
            with self.subTest(case=label):
                self.assertNotEqual(0, self._validator_exit(content))

    def test_validator_rejects_invalid_base64_payload(self) -> None:
        cases = {
            "punctuation": f"{self._BEGIN}\nAQIDBAU!\n{self._END}\n",
            "embedded space": f"{self._BEGIN}\nAQID BAU=\n{self._END}\n",
            "embedded tab": f"{self._BEGIN}\nAQID\tBAU=\n{self._END}\n",
            "embedded carriage return":
                f"{self._BEGIN}\nAQID\rBAU=\n{self._END}\n",
            "missing padding": f"{self._BEGIN}\nAQIDBAU\n{self._END}\n",
            "non-ASCII": f"{self._BEGIN}\nAQIDBAU=\u00e4\n{self._END}\n",
            "control byte": f"{self._BEGIN}\nAQIDBAU=\x7f\n{self._END}\n",
        }
        for label, content in cases.items():
            with self.subTest(case=label):
                self.assertNotEqual(0, self._validator_exit(content))


class TestDockerfileProxyArgsRed(unittest.TestCase):
    """Task 2.5: proxy via constructor-specific args, never same-named ARG/ENV."""

    def test_proxy_uses_constructor_specific_args_not_same_named(self) -> None:
        text = (_REPO_ROOT / "Dockerfile").read_text()
        for name in _PROXY_ARG_NAMES:
            self.assertNotIn(
                f"ARG {name}", text,
                f"{name} must not be a same-named ARG (an inherited ENV overrides it)",
            )
        for name in _CONSTRUCTOR_PROXY_ARGS:
            self.assertIn(f"ARG {name}", text)

    def test_proxy_values_never_converted_to_image_env(self) -> None:
        text = (_REPO_ROOT / "Dockerfile").read_text()
        for name in _PROXY_ARG_NAMES:
            self.assertNotIn(f"ENV {name}", text)

    def test_proxy_conditionally_exported_only_when_configured(self) -> None:
        # The helper overrides inherited proxy ENV values only when a proxy is
        # configured; otherwise inherited values are preserved.
        helper = (_REPO_ROOT / "docker" / "corp-network-env.sh").read_text()
        self.assertIn('if [ -n "${PI_CORPORATE_PROXY_URL:-}" ]; then', helper)
        self.assertIn('export HTTP_PROXY="${PI_CORPORATE_PROXY_URL}"', helper)
        self.assertIn('export https_proxy="${PI_CORPORATE_PROXY_URL}"', helper)
        self.assertIn('export no_proxy="${PI_CORPORATE_NO_PROXY}"', helper)

"""Closed configuration, identity, metadata, and disclosure boundaries."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from docker.versioning.corporate_network import (
    CLIENT_CA_ENVIRONMENT,
    SYSTEM_CA_BUNDLE,
    parse_local_corporate_trust,
)
from docker.npm_environment import (
    DockerRunVector,
    Mount,
    redact_docker_argv,
    redact_run_vector,
    render_docker_argv,
)
from docker.versioning.evidence import collect_evidence
from docker.versioning.errors import InventoryError
from tests.test_constructor_corporate_network_verification_red import _ClientCaRunner, _verify

_REPO = Path(__file__).resolve().parents[1]
_NAMES = tuple(name for name, _ in CLIENT_CA_ENVIRONMENT)
_HOST_PATH = "/private/operator/acme-secret-corporate-ca.pem"
_CERTIFICATE = "-----BEGIN CERTIFICATE-----\nSECRET-CONTENT\n-----END CERTIFICATE-----"


class TestClosedClientCaConfiguration(unittest.TestCase):
    def test_mapping_is_exactly_five_fixed_names_and_one_fixed_value(self) -> None:
        self.assertEqual(_NAMES, (
            "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
            "PIP_CERT", "CURL_CA_BUNDLE",
        ))
        self.assertEqual({value for _, value in CLIENT_CA_ENVIRONMENT}, {SYSTEM_CA_BUNDLE})

    def test_local_configuration_rejects_arbitrary_name_value_and_path_fields(self) -> None:
        for key, value in (
            ("environment", {"OTHER_CA": "/tmp/ca.pem"}),
            ("path", "/tmp/ca.pem"),
            ("value", "/tmp/ca.pem"),
            ("NODE_EXTRA_CA_CERTS", "/tmp/ca.pem"),
        ):
            with self.subTest(key=key), self.assertRaises(InventoryError):
                parse_local_corporate_trust({"enabled": True, key: value}, None)

    def test_reviewed_inventory_and_local_example_expose_no_client_ca_fields(self) -> None:
        for path in (_REPO / "docker-constructor.toml", _REPO / "docker-constructor.local.example.toml"):
            text = path.read_text(encoding="utf-8")
            for name in _NAMES:
                self.assertNotIn(name, text)
            self.assertNotIn("client_ca", text.lower())


class TestClientCaDisclosureAndIdentityBoundaries(unittest.TestCase):
    def _assert_sentinels_absent(self, rendered: str) -> None:
        self.assertNotIn(_HOST_PATH, rendered)
        self.assertNotIn(_CERTIFICATE, rendered)

    def test_rendered_run_summaries_redact_exercised_host_material(self) -> None:
        vector = DockerRunVector(
            image="sha256:" + "a" * 64,
            user="1000:1000",
            name="sentinel-assembler",
            home="/home/npm",
            workdir="/work",
            env=(("SENTINEL_PEM", _CERTIFICATE),),
            mounts=(Mount(_HOST_PATH, SYSTEM_CA_BUNDLE, "ro"),),
            command=("true",),
        )
        argv = render_docker_argv(vector)
        self.assertTrue(any(_HOST_PATH in token for token in argv))
        self.assertTrue(any(_CERTIFICATE in token for token in argv))

        secrets = (_HOST_PATH, _CERTIFICATE)
        safe_vector = redact_run_vector(vector, secrets)
        safe_argv = redact_docker_argv(argv, secrets)
        rendered = json.dumps({"vector": repr(safe_vector), "argv": safe_argv})
        self._assert_sentinels_absent(rendered)
        self.assertIn("<redacted>", rendered)

    def test_runtime_verification_redacts_exercised_host_material(self) -> None:
        runner = _ClientCaRunner(enabled=True, overrides={
            ("printenv", "NODE_EXTRA_CA_CERTS"): (0, _HOST_PATH + "\n", ""),
            ("printenv", "SSL_CERT_FILE"): (0, _CERTIFICATE + "\n", ""),
        })
        result = _verify(runner, enabled=True)
        rendered = json.dumps([
            {"key": check.key, "detail": check.detail}
            for check in result.checks
        ])
        self._assert_sentinels_absent(rendered)
        self.assertIn("corporate-trust.environment", rendered)

    def test_configuration_projection_and_evidence_owners_do_not_import_mapping(self) -> None:
        for relative in (
            "docker/versioning/effective.py",
            "docker/versioning/evidence.py",
            "docker/versioning/fetch_identity.py",
            "docker/versioning/digest_identity.py",
            "docker/versioning/cache.py",
            "docker/versioning/build_cache.py",
            "docker/versioning/artifact_cache.py",
            "docker/npm_environment/identity.py",
            "docker/npm_environment/model.py",
            "docker/npm_environment/lockfile.py",
            "docker/npm_environment/evidence.py",
        ):
            text = (_REPO / relative).read_text(encoding="utf-8")
            self.assertNotIn("CLIENT_CA_ENVIRONMENT", text, relative)
            for name in _NAMES:
                self.assertNotIn(name, text, relative)

    def test_evidence_and_image_inspection_redact_exercised_host_material(self) -> None:
        inspect_payload = json.dumps([{
            "Id": "sha256:sentinel-image",
            "Config": {"Env": ["PATH=/usr/bin"]},
            "Mounts": [{"Source": _HOST_PATH, "Destination": SYSTEM_CA_BUNDLE}],
            "Comment": _CERTIFICATE,
        }])

        class Runner:
            def run(self, argv):
                del argv
                return type("Result", (), {
                    "return_code": 0,
                    "stdout": inspect_payload,
                    "stderr": f"inspected {_HOST_PATH} {_CERTIFICATE}",
                })()

        class Clock:
            def __init__(self) -> None:
                self.value = 0.0

            def now(self) -> float:
                self.value += 0.1
                return self.value

        escaped_certificate = json.dumps(_CERTIFICATE)[1:-1]
        self.assertIn(escaped_certificate, inspect_payload)
        with tempfile.TemporaryDirectory() as root:
            bundle = collect_evidence(
                output_dir=Path(root),
                runner=Runner(),
                clock=Clock(),
                image="sentinel-image:latest",
                secrets=(_HOST_PATH, _CERTIFICATE),
            )
            files = {
                path.name: path.read_text(encoding="utf-8")
                for path in Path(root).iterdir() if path.is_file()
            }
            self.assertIn("index.txt", files)
            for filename, raw_content in files.items():
                with self.subTest(filename=filename):
                    self._assert_sentinels_absent(raw_content)
                    self.assertNotIn(escaped_certificate, raw_content)
            rendered = json.dumps({
                "commands": [command.argv for command in bundle.commands],
                "notes": [(note.key, note.detail) for note in bundle.notes],
                "files": files,
            })

        self._assert_sentinels_absent(rendered)
        self.assertNotIn(escaped_certificate, rendered)
        self.assertIn("REDACTED", rendered)
        for name in _NAMES:
            self.assertNotIn(f"{name}=", rendered)


if __name__ == "__main__":
    unittest.main()

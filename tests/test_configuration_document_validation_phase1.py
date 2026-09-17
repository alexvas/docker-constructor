"""Phase 1 contract tests for shared project TOML document validation."""
from __future__ import annotations

import ast
import dataclasses
import io
import tempfile
import tomllib
import unittest
from collections.abc import Mapping
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from docker import constructor_cli
from docker.versioning import inventory
from docker.versioning.configuration_document_validation import (
    ConfigurationDocumentError,
    DocumentErrorClassification,
    DocumentIdentity,
    DocumentRole,
    parse_configuration_document,
    project_schema_error,
    validate_configuration_documents,
)

_MALFORMED_SECRET = "sentinel-malformed-8f3a1c"
_SCHEMA_SECRET = "sentinel-schema-9d2b7e"
_CLASSIFICATION_SECRET = "classification-secret-7c1f4d"
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _reaches_secret(value: object, secret: str, seen: set[int], depth: int = 0) -> bool:
    """Return True if *secret* is reachable from *value* via data or closures."""
    if depth > 8:
        return False
    if isinstance(value, str):
        return secret in value
    if isinstance(value, (bytes, bytearray)):
        return secret.encode("utf-8") in value
    marker = id(value)
    if marker in seen:
        return False
    seen.add(marker)
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _reaches_secret(key, secret, seen, depth + 1) or _reaches_secret(
                item, secret, seen, depth + 1
            ):
                return True
        return False
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_reaches_secret(item, secret, seen, depth + 1) for item in value)
    if callable(value):
        for cell in getattr(value, "__closure__", None) or ():
            try:
                contents = cell.cell_contents
            except ValueError:
                continue
            if _reaches_secret(contents, secret, seen, depth + 1):
                return True
        defaults = getattr(value, "__defaults__", None)
        if defaults and _reaches_secret(tuple(defaults), secret, seen, depth + 1):
            return True
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        for field_info in dataclasses.fields(value):
            if _reaches_secret(
                getattr(value, field_info.name, None), secret, seen, depth + 1
            ):
                return True
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict):
        for item in attributes.values():
            if _reaches_secret(item, secret, seen, depth + 1):
                return True
    return False


def _error_reaches_secret(error: BaseException, secret: str) -> bool:
    """Return True if any retained traceback frame or closure reaches *secret*."""
    traceback = error.__traceback__
    while traceback is not None:
        for local in traceback.tb_frame.f_locals.values():
            if _reaches_secret(local, secret, set()):
                return True
        traceback = traceback.tb_next
    return False


class TestConfigurationDocumentValidation(unittest.TestCase):
    def _write(self, name: str, content: str) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / name
        path.write_text(content)
        return path

    def test_both_fixed_document_roles_route_through_one_boundary(self) -> None:
        reviewed = self._write("docker-constructor.toml", "schema = 1\n")
        local = reviewed.with_name("docker-constructor.local.toml")
        local.write_text("[cache]\n")

        reviewed_document = parse_configuration_document(
            DocumentIdentity(DocumentRole.REVIEWED, reviewed.resolve())
        )
        local_document = parse_configuration_document(
            DocumentIdentity(DocumentRole.LOCAL, local.resolve())
        )

        self.assertEqual(DocumentRole.REVIEWED, reviewed_document.identity.role)
        self.assertEqual(reviewed.resolve(), reviewed_document.identity.path)
        self.assertEqual(DocumentRole.LOCAL, local_document.identity.role)
        self.assertEqual(local.resolve(), local_document.identity.path)

    def test_valid_role_string_is_normalized_to_the_closed_enum(self) -> None:
        path = self._write("docker-constructor.toml", "schema = 1\n")
        for raw, expected in (
            ("reviewed", DocumentRole.REVIEWED),
            ("local", DocumentRole.LOCAL),
        ):
            with self.subTest(raw=raw):
                identity = DocumentIdentity(raw, path)
                self.assertIs(expected, identity.role)
                self.assertIsInstance(identity.role, DocumentRole)

    def test_invalid_role_is_rejected_before_any_parsing(self) -> None:
        path = self._write("docker-constructor.toml", "schema = 1\n")
        for raw in ("other", "local-file", "reviewed-config"):
            with self.subTest(raw=raw), patch(
                "docker.versioning.configuration_document_validation.tomllib.loads",
                wraps=tomllib.loads,
            ) as loads:
                with self.assertRaises(ValueError):
                    DocumentIdentity(raw, path)
                with self.assertRaises(ValueError):
                    parse_configuration_document(DocumentIdentity(raw, path))
                self.assertEqual(0, loads.call_count)

    def test_parse_is_called_once_and_malformed_document_returns_no_result(self) -> None:
        path = self._write("docker-constructor.toml", "not valid = [\n")
        identity = DocumentIdentity(DocumentRole.REVIEWED, path)
        with patch(
            "docker.versioning.configuration_document_validation.tomllib.loads",
            wraps=tomllib.loads,
        ) as loads:
            with self.assertRaises(ConfigurationDocumentError):
                parse_configuration_document(identity)
        self.assertEqual(1, loads.call_count)

    def test_duplicate_definition_is_rejected_without_lossy_mapping(self) -> None:
        path = self._write("docker-constructor.local.toml", "value = 1\nvalue = 2\n")
        with self.assertRaises(ConfigurationDocumentError) as raised:
            parse_configuration_document(DocumentIdentity(DocumentRole.LOCAL, path))
        self.assertEqual("malformed_toml", raised.exception.classification)

    def test_syntax_error_exposes_only_typed_safe_fields(self) -> None:
        secret = "super-secret-token"
        path = self._write("docker-constructor.toml", f"value = {secret}\n")
        with self.assertRaises(ConfigurationDocumentError) as raised:
            parse_configuration_document(DocumentIdentity(DocumentRole.REVIEWED, path))
        error = raised.exception
        self.assertEqual(DocumentRole.REVIEWED, error.role)
        self.assertEqual(path, error.path)
        self.assertEqual("malformed_toml", error.classification)
        self.assertIsNone(error.field)
        self.assertFalse(hasattr(error, "original_exception"))
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        self.assertIsInstance(error.line, int)
        self.assertIsInstance(error.column, int)
        self.assertNotIn(secret, str(error))
        self.assertNotIn("Invalid value", str(error))

    def test_invalid_encoding_has_no_fabricated_coordinates_or_exception_chain(self) -> None:
        path = self._write("docker-constructor.toml", "placeholder")
        path.write_bytes(b"\xff")
        with self.assertRaises(ConfigurationDocumentError) as raised:
            parse_configuration_document(DocumentIdentity(DocumentRole.REVIEWED, path))
        error = raised.exception
        self.assertIsNone(error.line)
        self.assertIsNone(error.column)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

    def test_schema_error_has_only_document_identity_classification_and_field(self) -> None:
        path = self._write("docker-constructor.local.toml", "[cache]\ndir = 42\n")
        error = project_schema_error(
            DocumentIdentity(DocumentRole.LOCAL, path), "cache.dir"
        )
        self.assertEqual("schema_error", error.classification)
        self.assertIs(DocumentErrorClassification.SCHEMA_ERROR, error.classification)
        self.assertEqual("cache.dir", error.field)
        self.assertIsNone(error.line)
        self.assertIsNone(error.column)
        self.assertIsNone(error.__context__)
        self.assertIsNone(error.__cause__)
        self.assertNotIn("42", str(error))

    def test_unknown_classification_is_rejected_before_publication(self) -> None:
        identity = DocumentIdentity(
            DocumentRole.REVIEWED,
            self._write("docker-constructor.toml", "schema = 1\n"),
        )
        with self.assertRaises(ValueError):
            ConfigurationDocumentError(
                identity, classification="not_a_real_classification"
            )

    def test_secret_like_classification_cannot_be_published_or_rendered(self) -> None:
        identity = DocumentIdentity(
            DocumentRole.REVIEWED,
            self._write("docker-constructor.toml", "schema = 1\n"),
        )
        published: ConfigurationDocumentError | None = None
        with self.assertRaises(ValueError):
            published = ConfigurationDocumentError(
                identity, classification=_CLASSIFICATION_SECRET
            )
        self.assertIsNone(published)
        if published is not None:  # pragma: no cover - guard against a removed check
            self.assertNotIn(_CLASSIFICATION_SECRET, str(published))

    def test_actual_reviewed_and_local_loaders_route_once_with_closed_identity(self) -> None:
        reviewed = self._write("docker-constructor.toml", "schema = 99\n")
        local = reviewed.with_name("docker-constructor.local.toml")
        local.write_text("[cache]\n")
        with patch(
            "docker.versioning.inventory.parse_configuration_document",
            wraps=parse_configuration_document,
        ) as boundary:
            with self.assertRaises(ConfigurationDocumentError):
                inventory.load_inventory(reviewed)
            inventory.load_local_config(local)
        identities = [call.args[0] for call in boundary.call_args_list]
        self.assertEqual(
            [(DocumentRole.REVIEWED, reviewed.resolve()), (DocumentRole.LOCAL, local.resolve())],
            [(item.role, item.path) for item in identities],
        )

    def test_every_inventory_error_site_declares_structured_field_metadata(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "docker/versioning/inventory.py").read_text()
        tree = ast.parse(source)
        missing = [
            node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "InventoryError"
            and not any(keyword.arg == "field" for keyword in node.keywords)
        ]
        self.assertEqual([], missing)

    def test_representative_owner_failures_publish_paths_not_values(self) -> None:
        valid = (Path(__file__).resolve().parents[1] / "docker-constructor.toml").read_text()
        reviewed_cases = (
            (valid.replace("schema = 1", "schema = 77", 1), "schema", "77"),
            (valid + "\n[stages.legacy]\nvalue = 1\n", "stages", "legacy"),
            (valid.replace("# ttl = 3600", "ttl = -77", 1), "cache.ttl", "-77"),
        )
        for content, field, rejected in reviewed_cases:
            with self.subTest(field=field):
                path = self._write("docker-constructor.toml", content)
                with self.assertRaises(ConfigurationDocumentError) as raised:
                    inventory.load_inventory(path)
                self.assertEqual(field, raised.exception.field)
                self.assertNotIn(rejected, str(raised.exception))

    def test_actual_owner_schema_errors_are_safe_and_typed(self) -> None:
        reviewed = self._write("docker-constructor.toml", "schema = 99\n")
        local = reviewed.with_name("docker-constructor.local.toml")
        local.write_text("[cache]\ndir = 42\n")
        cases = (
            (
                inventory.load_inventory, reviewed, DocumentRole.REVIEWED,
                "schema",
                f"reviewed configuration {reviewed.resolve()}: "
                "schema_error (schema_error) (field schema)",
                "unsupported version",
            ),
            (
                inventory.load_local_config, local, DocumentRole.LOCAL,
                "local.cache.dir",
                f"local companion configuration {local.resolve()}: "
                "schema_error (schema_error) (field local.cache.dir)",
                "expected string",
            ),
        )
        for loader, path, role, field, rendered, owner_message in cases:
            with self.subTest(role=role), self.assertRaises(ConfigurationDocumentError) as raised:
                loader(path)
            error = raised.exception
            self.assertEqual(role, error.role)
            self.assertEqual(path.resolve(), error.path)
            self.assertEqual("schema_error", error.classification)
            self.assertEqual(field, error.field)
            # The owner exception must not survive as a recoverable chain.
            self.assertIsNone(error.__context__)
            self.assertIsNone(error.__cause__)
            # The exact typed rendering proves neither the owner message nor
            # the rejected value can be recovered from the projected error.
            self.assertEqual(rendered, str(error))
            self.assertNotIn(owner_message, str(error))

    def test_invalid_override_constraint_is_projected_without_the_secret(self) -> None:
        sentinel = "sentinel-secret-constraint-value"
        source = (
            Path(__file__).resolve().parents[1] / "docker-constructor.toml"
        ).read_text()
        poisoned = source.replace(
            'constraint = ">=3.14.6"', f'constraint = "{sentinel}"', 1
        )
        self.assertNotEqual(source, poisoned)
        path = self._write("docker-constructor.toml", poisoned)
        with self.assertRaises(ConfigurationDocumentError) as raised:
            inventory.load_inventory(path)
        error = raised.exception
        self.assertEqual(DocumentRole.REVIEWED, error.role)
        self.assertEqual(path.resolve(), error.path)
        self.assertEqual("schema_error", error.classification)
        self.assertEqual(
            "build.stages.toolchain.python.override.constraint", error.field
        )
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        rendered = str(error)
        self.assertNotIn(sentinel, rendered)
        self.assertNotIn("Invalid constraint", rendered)
        self.assertNotIn("Constraint must be a non-empty string", rendered)

    def test_contradictory_override_constraint_is_projected_to_typed_field(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "docker-constructor.toml"
        ).read_text()
        contradictory = ">=2.0.0,<1.0.0"
        poisoned = source.replace(
            'constraint = ">=3.14.6"', f'constraint = "{contradictory}"', 1
        )
        self.assertNotEqual(source, poisoned)
        path = self._write("docker-constructor.toml", poisoned)
        with self.assertRaises(ConfigurationDocumentError) as raised:
            inventory.load_inventory(path)
        error = raised.exception
        self.assertEqual(DocumentRole.REVIEWED, error.role)
        self.assertEqual(path.resolve(), error.path)
        self.assertEqual("schema_error", error.classification)
        self.assertEqual(
            "build.stages.toolchain.python.override.constraint", error.field
        )
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        self.assertNotIn(contradictory, str(error))

    def test_raw_version_owner_error_is_projected_to_typed_field(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "docker-constructor.toml"
        ).read_text()
        rejected = "3.14.0"
        poisoned = source.replace(
            'version = "3.14.7"', f'version = "{rejected}"', 1
        )
        self.assertNotEqual(source, poisoned)
        path = self._write("docker-constructor.toml", poisoned)
        with self.assertRaises(ConfigurationDocumentError) as raised:
            inventory.load_inventory(path)
        error = raised.exception
        self.assertEqual(DocumentRole.REVIEWED, error.role)
        self.assertEqual(path.resolve(), error.path)
        self.assertEqual("schema_error", error.classification)
        self.assertEqual("build.stages.toolchain.python.version", error.field)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        self.assertNotIn(rejected, str(error))

    def test_malformed_toml_secret_is_not_reachable_from_traceback_locals(self) -> None:
        path = self._write(
            "docker-constructor.toml",
            'secret_value = "' + _MALFORMED_SECRET + '"\nbroken = [\n',
        )
        with self.assertRaises(ConfigurationDocumentError) as raised:
            parse_configuration_document(
                DocumentIdentity(DocumentRole.REVIEWED, path)
            )
        error = raised.exception
        self.assertEqual("malformed_toml", error.classification)
        self.assertFalse(_error_reaches_secret(error, _MALFORMED_SECRET))

    def test_schema_error_hides_parsed_data_and_closures_from_its_traceback(self) -> None:
        path = self._write(
            "docker-constructor.toml",
            (_REPO_ROOT / "docker-constructor.toml")
            .read_text()
            .replace(
                'version = "3.14.7"',
                'version = "' + _SCHEMA_SECRET + '"',
                1,
            ),
        )
        with self.assertRaises(ConfigurationDocumentError) as raised:
            inventory.load_inventory(path)
        error = raised.exception
        self.assertEqual("schema_error", error.classification)
        self.assertEqual("build.stages.toolchain.python.version", error.field)
        self.assertFalse(_error_reaches_secret(error, _SCHEMA_SECRET))

    def test_cli_presentation_cannot_render_parser_source(self) -> None:
        secret = "do-not-publish-this-token"
        reviewed = self._write("docker-constructor.toml", f"secret = {secret}\n")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = constructor_cli.main(["--project-directory", str(reviewed.parent), "validate"])
        rendered = out.getvalue() + err.getvalue()
        self.assertNotEqual(0, status)
        self.assertIn("malformed_toml", rendered)
        self.assertIn(str(reviewed.resolve()), rendered)
        self.assertNotIn(secret, rendered)
        self.assertNotIn("Invalid value", rendered)

    def test_integrated_project_gate_releases_no_partial_configuration(self) -> None:
        valid_reviewed = (
            Path(__file__).resolve().parents[1] / "docker-constructor.toml"
        ).read_text()
        cases = (
            (valid_reviewed, "value = [\n", DocumentRole.LOCAL),
            (valid_reviewed.replace("schema = 1", "schema = 99", 1), "[cache]\n", DocumentRole.REVIEWED),
            (valid_reviewed, "[cache]\ndir = 42\n", DocumentRole.LOCAL),
        )
        for reviewed_text, local_text, role in cases:
            with self.subTest(role=role):
                reviewed = self._write("docker-constructor.toml", reviewed_text)
                reviewed.with_name("docker-constructor.local.toml").write_text(local_text)
                with patch(
                    "docker.versioning.inventory.validate_configuration_documents",
                    wraps=validate_configuration_documents,
                ) as gate:
                    with self.assertRaises(ConfigurationDocumentError) as raised:
                        inventory.load_project_configuration(reviewed)
                self.assertEqual(role, raised.exception.role)
                self.assertEqual(1, gate.call_count)

    def test_build_planning_gate_blocks_cache_projection_and_docker_inputs(self) -> None:
        from docker.versioning.build_orchestration import BuildRequest, plan_build

        valid_reviewed = (
            Path(__file__).resolve().parents[1] / "docker-constructor.toml"
        ).read_text()
        cases = (
            (valid_reviewed, "value = [\n"),
            (valid_reviewed.replace("schema = 1", "schema = 99", 1), "[cache]\n"),
            (valid_reviewed, "[cache]\ndir = 42\n"),
        )
        for reviewed_text, local_text in cases:
            with self.subTest(local=local_text):
                reviewed = self._write("docker-constructor.toml", reviewed_text)
                reviewed.with_name("docker-constructor.local.toml").write_text(local_text)
                effects: list[str] = []
                request = BuildRequest(
                    inventory_path=str(reviewed), context=str(reviewed.parent),
                    dockerfile=str(reviewed.parent / "Dockerfile"),
                )
                with patch(
                    "docker.versioning.build_orchestration.resolve_effective_root",
                    side_effect=lambda *_args, **_kwargs: effects.append("cache"),
                ), patch(
                    "docker.versioning.build_orchestration.resolve_build_projection",
                    side_effect=lambda *_args, **_kwargs: effects.append("projection"),
                ):
                    result = plan_build(request)
                self.assertEqual("config", result.exit_kind.value)
                self.assertEqual([], effects)

    def test_release_gate_runs_no_owner_or_effect_when_any_document_is_invalid(self) -> None:
        reviewed = self._write("docker-constructor.toml", "schema = 1\n")
        local = reviewed.with_name("docker-constructor.local.toml")
        local.write_text("value = [\n")
        effects: list[str] = []

        with self.assertRaises(ConfigurationDocumentError):
            validate_configuration_documents(
                (
                    DocumentIdentity(DocumentRole.REVIEWED, reviewed),
                    DocumentIdentity(DocumentRole.LOCAL, local),
                ),
                lambda _documents: effects.append("effect"),
            )
        self.assertEqual([], effects)

"""Executable proof for the narrow prepared local-file release seam."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HELPER = REPO / "docker/_owned_file.py"


def shape(source):
    return ast.dump(ast.parse(source), include_attributes=False)


def prepared_file_proof(source):
    tree = ast.parse(source)
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
    if set(functions) != {"owned_file"} or set(classes) != {
        "_Closeable",
        "_FileRelease",
    }:
        return False
    imported_owner = any(
        isinstance(n, ast.ImportFrom)
        and n.module == "docker.filesystem.streams"
        and [(a.name, a.asname) for a in n.names] == [("OwnedStream", None)]
        for n in tree.body
    )
    function = functions["owned_file"]
    expected = """release: _FileRelease[T] = _FileRelease()
owner = OwnedStream(release, label=label)
with owner:
    release.stream = stream = opener()
    yield stream"""
    if not imported_owner or shape(
        "\n".join(ast.unparse(n) for n in function.body)
    ) != shape(expected):
        return False
    release = classes["_FileRelease"]
    methods = {n.name: n for n in release.body if isinstance(n, ast.FunctionDef)}
    if set(methods) != {"__init__", "close"}:
        return False
    return (
        shape("\n".join(ast.unparse(n) for n in methods["__init__"].body))
        == shape("self.stream: T | None = None")
        and shape("\n".join(ast.unparse(n) for n in methods["close"].body))
        == shape("if self.stream is not None:\n    self.stream.close()")
        and [ast.unparse(n) for n in function.decorator_list] == ["contextmanager"]
    )


class NativeFileStructureTests(unittest.TestCase):
    def test_actual_helper_prepares_before_acquisition_and_has_one_close_delegate(self):
        self.assertTrue(prepared_file_proof(HELPER.read_text()))

    def test_missing_or_reordered_ownership_is_rejected(self):
        source = HELPER.read_text()
        for old, new in (
            (
                "owner = OwnedStream(release, label=label)",
                "stream = opener()\n    owner = OwnedStream(release, label=label)",
            ),
            ("release.stream = stream = opener()", "stream = opener()"),
            ("yield stream", "with stream:\n            yield stream"),
            ("yield stream", "yield stream\n        release.stream = stream"),
            (
                "self.stream.close()",
                "self.stream.close()\n            self.stream.close()",
            ),
            ("with owner:", "with OwnedStream(release, label=label):"),
            ("yield stream", "os.close(stream.fileno())\n        yield stream"),
            (
                "from docker.filesystem.streams import OwnedStream",
                "from fake import OwnedStream",
            ),
        ):
            with self.subTest(mutation=new):
                self.assertIn(old, source)
                self.assertFalse(prepared_file_proof(source.replace(old, new)))

    def test_all_five_readers_use_the_prepared_seam(self):
        sites = {
            "docker/runtime_installer.py": {"read_projection", "read"},
            "docker/versioning/cache.py": {"get"},
            "docker/versioning/build_materialization.py": {"_verify_hit"},
            "docker/versioning/verification.py": {"_load_build_projection"},
        }
        total = 0
        for path, names in sites.items():
            tree = ast.parse((REPO / path).read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef) or node.name not in names:
                    continue
                calls = [
                    n
                    for n in ast.walk(node)
                    if isinstance(n, ast.Call) and ast.unparse(n.func) == "owned_file"
                ]
                # Protocol stubs named read have no acquisition.
                for call in calls:
                    self.assertEqual(len(call.args), 1)
                    self.assertIsInstance(call.args[0], ast.Lambda)
                    self.assertIsInstance(call.args[0].body, ast.Call)
                    self.assertIn(
                        ast.unparse(call.args[0].body.func), ("open", "path.open")
                    )
                    total += 1
                for context in (n for n in ast.walk(node) if isinstance(n, ast.With)):
                    for item in context.items:
                        if isinstance(item.context_expr, ast.Call):
                            self.assertNotIn(
                                ast.unparse(item.context_expr.func),
                                ("open", "path.open"),
                            )
        self.assertEqual(total, 5)

    def test_runtime_image_copies_private_helper(self):
        self.assertIn(
            "COPY docker/_owned_file.py /usr/local/lib/pi-cli/docker/_owned_file.py",
            (REPO / "Dockerfile").read_text(),
        )


if __name__ == "__main__":
    unittest.main()

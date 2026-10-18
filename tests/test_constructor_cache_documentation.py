"""Phase 7 cache documentation and maintained-source consistency checks."""

from pathlib import Path
import unittest


_ROOT = Path(__file__).parents[1]
_READMES = tuple(_ROOT / name for name in ("README.md", "README.en.md", "README.zh.md"))
_LEGACY = ("pi-cli/versioning", ".docker-generated/runtime-artifacts")


class TestCacheDocumentationConsistency(unittest.TestCase):
    def test_readmes_describe_active_layout_and_policy(self) -> None:
        for path in _READMES:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertIn("${XDG_CACHE_HOME}/docker-constructor", text)
                self.assertIn("runtime-artifacts/blobs", text)
                self.assertIn("versioning", text)
                self.assertIn("cache.ttl", text)
                self.assertIn("--no-cache", text)
                self.assertIn("0700", text)
                self.assertNotIn("--cache-dir", text)
                self.assertNotIn("--cache-ttl", text)
                for legacy in _LEGACY:
                    self.assertNotIn(legacy, text)

    def test_local_example_requires_a_dedicated_absolute_root(self) -> None:
        text = (_ROOT / "docker-constructor.local.example.toml").read_text()
        self.assertIn("absolute", text.lower())
        self.assertIn("dedicated", text.lower())
        self.assertIn("docker-constructor-custom", text)

    def test_maintenance_scripts_do_not_use_checkout_local_cache(self) -> None:
        for name in (
            "docker/collect-runtime-artifact-evidence.sh",
            "docker/collect-runtime-artifact-acceptance.sh",
        ):
            text = (_ROOT / name).read_text()
            with self.subTest(path=name):
                self.assertNotIn(".docker-generated/runtime-artifacts", text)
                self.assertIn("runtime-artifacts/blobs", text)


if __name__ == "__main__":
    unittest.main()

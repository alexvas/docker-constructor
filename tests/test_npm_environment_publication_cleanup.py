"""Phase 5 — post-assembly publication cleanup (task 5.6 follow-up).

A publication failure (including cancellation) must clean the matching
staging workspace using the same structured ``CleanupFailure`` model as
container execution.  The original publication failure stays primary; any
cleanup failure — ordinary or ``KeyboardInterrupt`` — is attached as a note
recording the staging path and mutable-residue risk.  Previously published
immutable outputs and unrelated staging workspaces are never touched.
"""

from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR.parent))

import docker.npm_environment.execution as execution_module
from docker.npm_environment import (
    LockedNpmError,
    ProcessResult,
    RootSpec,
    assemble_environment,
    assembler_script_digest,
    compute_assembler_identity,
    compute_assembler_input_identity,
    npm_policy_digest,
    preflight,
    prepare_staging_workspace,
    publication,
    publish_environment,
)
from docker.versioning.npm_diagnostic_stream import project_tail

_URL = "https://user:pass@registry.example.com/pkg?token=abc#frag"

_IMAGE = "sha256:" + "a" * 64
_NODE = "24.18.0"
_NPM = "11.16.0"
_PLATFORM = "linux-x64"


def _sri() -> str:
    return "sha512-" + base64.b64encode(bytes([0xAB]) * 64).decode()


def _lock(roots: dict[str, str]) -> bytes:
    packages = {
        "": {"name": "root", "version": "1.0.0", "dependencies": dict(roots)}
    }
    for name, version in roots.items():
        packages[f"node_modules/{name}"] = {
            "version": version,
            "resolved": f"https://registry.npmjs.org/{name}/-/{name}-{version}.tgz",
            "integrity": _sri(),
        }
    return json.dumps(
        {
            "name": "root",
            "version": "1.0.0",
            "lockfileVersion": 3,
            "requires": True,
            "packages": packages,
        }
    ).encode()


def _validated(roots: dict[str, str]):
    return preflight(
        _lock(roots),
        roots=tuple(RootSpec(n, v) for n, v in sorted(roots.items())),
        platform=_PLATFORM,
        node_version=_NODE,
        npm_version=_NPM,
    )


def _assembler():
    return compute_assembler_identity(
        image_digest=_IMAGE,
        node_version=_NODE,
        npm_version=_NPM,
        script_digest=assembler_script_digest(),
        policy_digest=npm_policy_digest(),
        platform=_PLATFORM,
    )


class FakeExecutor:
    """A successful Docker executor (staging is populated only by ``assemble``)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: tuple[str, ...]):
        self.calls.append(argv)
        return ProcessResult(argv, 0, "", "")


def _write_tree(root: Path, name: str, version: str, marker: bytes) -> None:
    manifest = root / "node_modules" / name / "package.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"name": name, "version": version}))
    (root / "node_modules" / name / "marker.txt").write_bytes(marker)


class PublicationCleanupTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="npm-env-pub-cleanup-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.cache_root = self.base / "cache"
        self.cache_root.mkdir()
        self.assembler = _assembler()
        self.validated = _validated({"a": "1.0.0"})
        self.input_identity = compute_assembler_input_identity(
            self.validated, self.assembler
        )
        self.namespace = publication.prepare_assembler_namespace(
            self.cache_root, self.assembler.digest
        )
        self.staging_path = self.namespace.staging / self.input_identity.digest
        # An unrelated staging workspace that cleanup must never touch.
        self.sentinel_staging = prepare_staging_workspace(self.namespace, "sentinel")

    def _staging_workspaces(self) -> list[Path]:
        return sorted(p for p in self.namespace.staging.iterdir() if p.is_dir())

    def _publish_prior(self):
        prior = _validated({"prior": "1.0.0"})
        tree = self.base / "prior-tree"
        tree.mkdir()
        _write_tree(tree, "prior", "1.0.0", b"prior-marker")
        prior_identity = compute_assembler_input_identity(prior, self.assembler)
        return publish_environment(
            validated=prior,
            tree_root=tree,
            namespace=self.namespace,
            input_identity=prior_identity,
        )

    def _run_failing(
        self,
        publication_exc: BaseException,
        *,
        tail_projector=None,
        secrets=(),
    ):
        with mock.patch.object(
            publication, "publish_environment", side_effect=publication_exc
        ):
            return assemble_environment(
                validated=self.validated,
                assembler=self.assembler,
                cache_root=self.cache_root,
                executor=FakeExecutor(),
                secrets=secrets,
                tail_projector=tail_projector,
            )

    def _assert_prior_untouched(self, prior) -> None:
        self.assertTrue(prior.environment_root.exists())
        self.assertEqual(
            (prior.environment_root / "node_modules" / "prior" / "marker.txt").read_bytes(),
            b"prior-marker",
        )
        self.assertTrue(self.sentinel_staging.exists())


class TestPublicationCleanup(PublicationCleanupTestCase):
    def test_publication_failure_with_successful_cleanup(self):
        prior = self._publish_prior()
        with self.assertRaises(LockedNpmError) as ctx:
            self._run_failing(
                LockedNpmError("output_validation_failed", "publication boom")
            )
        # Original publication failure stays primary; no cleanup notes.
        self.assertEqual(ctx.exception.reason, "output_validation_failed")
        self.assertEqual(getattr(ctx.exception, "__notes__", []), [])
        # Only the matching staging workspace was removed.
        self.assertNotIn(self.staging_path, self._staging_workspaces())
        self._assert_prior_untouched(prior)

    def test_publication_failure_plus_cleanup_failure(self):
        prior = self._publish_prior()
        with mock.patch.object(
            execution_module,
            "remove_staging_workspace",
            side_effect=OSError("cleanup exploded"),
        ):
            with self.assertRaises(LockedNpmError) as ctx:
                self._run_failing(
                    LockedNpmError("output_validation_failed", "publication boom")
                )
        self.assertEqual(ctx.exception.reason, "output_validation_failed")
        notes = ctx.exception.__notes__
        self.assertEqual(len(notes), 1)
        self.assertIn("staging", notes[0])
        self.assertIn("OSError", notes[0])
        self.assertIn("cleanup exploded", notes[0])
        self.assertIn("residue may remain at", notes[0])
        self.assertIn(str(self.staging_path), notes[0])
        self._assert_prior_untouched(prior)

    def test_publication_failure_plus_keyboard_interrupt_cleanup(self):
        prior = self._publish_prior()
        with mock.patch.object(
            execution_module,
            "remove_staging_workspace",
            side_effect=KeyboardInterrupt("cleanup interrupted"),
        ):
            with self.assertRaises(LockedNpmError) as ctx:
                self._run_failing(
                    LockedNpmError("output_validation_failed", "publication boom")
                )
        self.assertEqual(ctx.exception.reason, "output_validation_failed")
        notes = ctx.exception.__notes__
        self.assertEqual(len(notes), 1)
        self.assertIn("KeyboardInterrupt", notes[0])
        self.assertIn("cleanup interrupted", notes[0])
        self.assertIn("residue may remain at", notes[0])
        self.assertIn(str(self.staging_path), notes[0])
        self._assert_prior_untouched(prior)

    def test_keyboard_interrupt_publication_plus_cleanup_failure(self):
        prior = self._publish_prior()
        with mock.patch.object(
            execution_module,
            "remove_staging_workspace",
            side_effect=LockedNpmError("unsafe_staging_path", "cannot remove residue"),
        ):
            with self.assertRaises(KeyboardInterrupt) as ctx:
                self._run_failing(KeyboardInterrupt("publication interrupted"))
        # The cancellation during publication stays primary.
        self.assertIsInstance(ctx.exception, KeyboardInterrupt)
        notes = ctx.exception.__notes__
        self.assertEqual(len(notes), 1)
        self.assertIn("staging", notes[0])
        self.assertIn("unsafe_staging_path", notes[0])
        self.assertIn("cannot remove residue", notes[0])
        self.assertIn("residue may remain at", notes[0])
        self.assertIn(str(self.staging_path), notes[0])
        self._assert_prior_untouched(prior)

    def test_publication_cleanup_url_is_projected(self):
        # A publication-cleanup failure whose exception text carries a URL
        # must not leak it through the attached note when the host injects the
        # URL-free projector.
        prior = self._publish_prior()
        with mock.patch.object(
            execution_module,
            "remove_staging_workspace",
            side_effect=LockedNpmError(
                "unsafe_staging_path", f"cannot remove {_URL} (SUPERSECRET)"
            ),
        ):
            with self.assertRaises(LockedNpmError) as ctx:
                self._run_failing(
                    LockedNpmError("output_validation_failed", "publication boom"),
                    tail_projector=project_tail,
                    secrets=("SUPERSECRET",),
                )
        self.assertEqual(ctx.exception.reason, "output_validation_failed")
        notes = ctx.exception.__notes__
        self.assertEqual(len(notes), 1)
        self.assertIn("staging", notes[0])
        self.assertIn("unsafe_staging_path", notes[0])
        self.assertIn("residue may remain at", notes[0])
        self.assertIn(str(self.staging_path), notes[0])
        for fragment in (
            _URL,
            "https://",
            "registry.example.com",
            "user:pass",
            "token=abc",
            "#frag",
            "SUPERSECRET",
        ):
            self.assertNotIn(fragment, notes[0])
        self.assertIn("<redacted>", notes[0])
        self._assert_prior_untouched(prior)


if __name__ == "__main__":
    unittest.main()

"""6BD executable negative guidance and cross-document selection contracts."""

from pathlib import Path

import pytest

from tests.enforcement.process_convergence import convergence_report
from tests.enforcement.test_filesystem_convergence import _sources
from tests.enforcement.test_lifecycle_guide_links import LINK, _check_local_link

ROOT = Path(__file__).resolve().parents[2]

# These are source examples, never executed as unsafe resource operations.
# The final two model forbidden edits to the actual coordinator/domain action.
FORBIDDEN = {
    "Unprotected launch": (
        "docker/new_example.py",
        None,
        "from subprocess import Popen, PIPE\ndef f(argv):\n    return Popen(argv, stdout=PIPE, stderr=PIPE)\n",
    ),
    "Rejected-adoption close": (
        "docker/new_example.py",
        None,
        "from docker.filesystem.streams import OwnedStream\ndef f(child):\n    pipe = child.stdout\n    try: OwnedStream(pipe, label='')\n    except ValueError: pipe.close()\n",
    ),
    "Double raw-fileno ownership": (
        "docker/new_example.py",
        None,
        "from docker.filesystem.descriptors import OwnedDescriptor\ndef f(child, ops):\n    return OwnedDescriptor(ops, child.stdout.fileno(), label='duplicate')\n",
    ),
    "Repeated owner reconstruction": (
        "docker/new_example.py",
        None,
        "from docker.filesystem.streams import OwnedStream\ndef cleanup(child):\n    owner = OwnedStream(child.stdout, label='stdout')\n    owner.close()\n",
    ),
    "Close beneath a live reader": (
        "docker/new_example.py",
        None,
        "def cleanup(child, reader):\n    reader.join(.01)\n    child.stdout.close()\n",
    ),
    "Whole-shutdown override": (
        "docker/new_example.py",
        None,
        "from docker.process import OwnedProcess\nclass Policy(OwnedProcess):\n    def shutdown(self): pass\n",
    ),
    "Fresh grace per stage": (
        "docker/process.py",
        "time.monotonic() + stage.seconds, end - policy.local_cleanup_seconds",
        "time.monotonic() + self._grace, end - policy.local_cleanup_seconds",
    ),
    "Local exit as external completion": (
        "docker/npm_environment/execution.py",
        "complete = complete and status.complete",
        "complete = True",
    ),
}


@pytest.fixture(scope="module")
def sources():
    return _sources()


@pytest.mark.parametrize("pattern", FORBIDDEN)
def test_forbidden_process_examples_are_rejected(sources, pattern):
    path, old, replacement = FORBIDDEN[pattern]
    if old is not None:
        assert old in sources[path]
        replacement = sources[path].replace(old, replacement)
    assert convergence_report({**sources, path: replacement})["errors"]
    assert pattern in (ROOT / "docs/filesystem-lifecycle.md").read_text()


@pytest.mark.parametrize(
    "path", ["AGENTS.md", "openspec/config.yaml", "docs/filesystem-lifecycle.md"]
)
def test_project_guidance_selects_process_and_filesystem_owners(path):
    text = (ROOT / path).read_text()
    for token in (
        "OwnedProcess",
        "OwnedStream",
        "OwnedDescriptor",
        "DirectoryDescriptor",
        "closefd=False",
        "StopPolicy",
        "cleanup_status",
        "wait_cleanup",
        "cleanup_complete",
        "process-lifecycle.md",
    ):
        assert token in text, (path, token)
    for token in ("operation-plus-close", "interruption", "repeat"):
        assert token in text, (path, token)
    assert "wait/reap" in text and "Incomplete" in text
    assert "tests/enforcement/" in text
    if path == "AGENTS.md":
        assert "tests/test_" not in text
        assert "tests/enforcement/test_" not in text


@pytest.mark.parametrize("name", ["README.md", "README.en.md", "README.zh.md"])
def test_every_readme_resolves_both_ownership_guides_and_agent_rule(name):
    readme = ROOT / name
    links = {url for _, url, _ in LINK.findall(readme.read_text())}
    for url in (
        "docs/filesystem-lifecycle.md",
        "docs/process-lifecycle.md",
        "AGENTS.md",
    ):
        assert url in links
        _check_local_link(readme, url, None)
    assert "OwnedProcess" in readme.read_text()


def test_ownership_guides_link_each_other_and_the_assembler():
    docs = ROOT / "docs"
    for name in (
        "filesystem-lifecycle.md",
        "process-lifecycle.md",
        "npm-environment-assembler.md",
    ):
        guide = docs / name
        links = {url for _, url, _ in LINK.findall(guide.read_text())}
        for other in (
            "filesystem-lifecycle.md",
            "process-lifecycle.md",
            "npm-environment-assembler.md",
        ):
            if other != name:
                assert other in links, (name, other)
                _check_local_link(guide, other, None)

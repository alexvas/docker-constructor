"""Final whole-tree implicit ownership/API inventory, with explicit limits."""

import ast

from tests.enforcement.release_inventory import release_inventory
from tests.enforcement.test_filesystem_convergence import _sources


def test_complete_tree_release_and_api_inventory():
    sources = _sources()
    report = release_inventory(sources)
    assert report["files_scanned"] == sorted(sources)
    assert set(report["source_sha256"]) == set(sources)
    assert report["errors"] == []
    assert report["implicit_descriptor_errors"] == []
    assert len(report["implicit_descriptor_calls"]) == 8  # six wrappers, two factories
    assert (
        sum(row["origin"] == "os.fdopen" for row in report["implicit_descriptor_calls"])
        == 6
    )
    assert report["domain_fields"]["LifecycleOutcome"] == [
        "return_code",
        "errors",
        "stdout",
        "stderr",
    ]
    assert "OwnedProcess | None" in report["adapter_signatures"]["run_captured"]
    assert report["process_surface"]["OwnedProcess.wait_cleanup"]["signature"].endswith(
        "'LifecycleResult'"
    )
    # References include deferred callbacks and injected raw primitives, even
    # though neither necessarily executes a release at the reference site.
    assert any(
        row["expression"] == "os.close" and not row["direct_call"]
        for row in report["close_references"]
    )
    assert any(
        row["scope"].endswith("close_parent") for row in report["close_references"]
    )


def test_new_module_implicit_origins_are_inventoried_and_rejected():
    source = """from os import fdopen as wrap
from tempfile import mkstemp as create
def f():
    acquire = create
    fd, name = acquire()
    use(fd)
    alias = wrap
    return alias(fd, 'w')
"""
    path = "docker/new_release_consumer.py"
    report = release_inventory({**_sources(), path: source})
    rows = [row for row in report["implicit_descriptor_calls"] if row["path"] == path]
    assert {row["origin"] for row in rows} == {"os.fdopen", "tempfile.mkstemp"}
    errors = [
        row for row in report["implicit_descriptor_errors"] if row["path"] == path
    ]
    assert {row["kind"] for row in errors} == {"fdopen", "mkstemp"}
    assert all(ast.parse(row["expression"]) for row in rows)

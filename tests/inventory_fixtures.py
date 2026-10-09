"""Stable, test-owned inventory fixtures (the "behavioral" test lane).

Two test lanes
--------------

Behavioral lane
    Tests that exercise launcher, schema, artifact-selection, Pi-release,
    progress, effective-state, and visual behaviour use the committed,
    reviewed fixtures under ``tests/fixtures/`` via the helpers in this
    module.  Those fixtures are deliberately *not* the repository's
    ``docker-constructor.toml``: a routine dependency bump or the removal
    of an optional extension must never invalidate behavioural
    expectations.

Live-contract lane
    A small number of explicit tests read the repository's real
    ``docker-constructor.toml`` and assert only structural / cross-field
    invariants (successful load, selected-artifact agreement, Pi source
    contract, declared-owner visual headers, propagation to effective
    state).  They never pin dependency versions or optional-extension
    membership.

The helpers below assemble fresh per-test project documents in temporary
directories.  They are simple test-data assemblers, not schema validators
or replicas of production algorithms.
"""
from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

__all__ = [
    "ARTIFACT_BYTES_PREFIX",
    "FIXTURES_DIR",
    "REPO_ROOT",
    "STABLE_INVENTORY_PATH",
    "artifact_bytes",
    "artifact_integrity",
    "remove_extension",
    "rewrite_artifact_integrities",
    "stable_inventory_path",
    "stable_inventory_text",
    "with_dotted_extension",
    "with_empty_pi_extensions",
    "write_stable_inventory",
    "write_stable_project",
]

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
STABLE_INVENTORY_PATH = FIXTURES_DIR / "stable_inventory.toml"

#: Deterministic prefix for synthetic artifact bytes used by the launcher
#: and ordering fixtures.  Callers may pass their own ``prefix`` when they
#: need an isolated byte namespace.
ARTIFACT_BYTES_PREFIX = "stable-fixture-artifact:"


def stable_inventory_path() -> Path:
    """Return the committed stable reviewed-inventory fixture path."""
    return STABLE_INVENTORY_PATH


def stable_inventory_text() -> str:
    """Return the committed stable reviewed-inventory fixture as text."""
    return STABLE_INVENTORY_PATH.read_text(encoding="utf-8")


def artifact_bytes(url: str, *, prefix: str = ARTIFACT_BYTES_PREFIX) -> bytes:
    """Return deterministic synthetic bytes for *url*.

    The bytes never come from the network and are stable across runs, so
    a declared integrity can be derived locally.
    """
    return (prefix + url).encode("utf-8")


def artifact_integrity(
    url: str,
    *,
    algorithm: str = "sha512",
    prefix: str = ARTIFACT_BYTES_PREFIX,
) -> str:
    """Return ``<algorithm>-<base64>`` for :func:`artifact_bytes` of *url*."""
    digest = hashlib.new(algorithm, artifact_bytes(url, prefix=prefix)).digest()
    return f"{algorithm}-{base64.b64encode(digest).decode('ascii')}"


def rewrite_artifact_integrities(
    text: str,
    *,
    algorithm: str = "sha512",
    prefix: str = ARTIFACT_BYTES_PREFIX,
    overrides: dict[str, str] | None = None,
) -> str:
    """Rewrite every ``url``/``integrity`` pair to locally derived SRI.

    The reviewed fixture may order ``integrity`` before or after ``url``
    within an artifact table.  Both orderings are normalised into a
    ``url``-then-``integrity`` form so digests always match
    :func:`artifact_bytes`.  *overrides* maps a URL to an explicit
    integrity (for shared-identity scenarios).
    """
    resolved = overrides or {}

    def _render(url: str) -> str:
        integrity = resolved.get(url) or artifact_integrity(
            url, algorithm=algorithm, prefix=prefix,
        )
        return f'url = "{url}"\nintegrity = "{integrity}"'

    url_first = re.compile(r'url = "([^"]+)"\nintegrity = "[^"]+"')
    integrity_first = re.compile(r'integrity = "[^"]+"\nurl = "([^"]+)"')
    text = url_first.sub(lambda m: _render(m.group(1)), text)
    text = integrity_first.sub(lambda m: _render(m.group(1)), text)
    return text


_EXTENSION_HEADER = re.compile(
    r"^\[runtime\.pi-extensions\.(?P<name>[^.\]]+)(?:\.[^\]]*)?\]$"
)


def remove_extension(text: str, name: str) -> str:
    """Return *text* with every table belonging to extension *name* removed.

    This models "the optional extension was uninstalled" without touching
    any other declared extension.  It intentionally operates on the raw
    document so a typo fails loudly (the caller's negative test can then
    assert the intended field, not an unrelated missing table).  The
    immediately preceding ``# --- pi-extensions.<name> ---`` header is
    removed, while the next extension's leading comment is preserved.
    """
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    removed = False
    i = 0
    total = len(lines)
    while i < total:
        match = _EXTENSION_HEADER.match(lines[i].strip())
        if match is not None and match.group("name") == name:
            removed = True
            if (
                out
                and out[-1].strip() == f"# --- pi-extensions.{name} ---"
            ):
                out.pop()
            i += 1
            pending: list[str] = []
            while i < total and _EXTENSION_HEADER.match(lines[i].strip()) is None:
                pending.append(lines[i])
                i += 1
            # Preserve the trailing comment run, which belongs to the next
            # extension block (its visual header).
            keep: list[str] = []
            for line in reversed(pending):
                if line.strip().startswith("#"):
                    keep.append(line)
                else:
                    break
            out.extend(reversed(keep))
            continue
        out.append(lines[i])
        i += 1
    if not removed:
        raise KeyError(f"no such extension: {name!r}")
    return "".join(out)


_DOTTED_EXTENSION_TEMPLATE = '''

# --- pi-extensions.{name} ---
[runtime.pi-extensions."{name}"]
version = "{version}"

[runtime.pi-extensions."{name}".source]
type = "npm"
package = "{package}"

[runtime.pi-extensions."{name}".artifacts."{version}"]
url = "{url}"
integrity = "{integrity}"

[runtime.pi-extensions."{name}".update]
provider = "npm"
stable_only = true

[runtime.pi-extensions."{name}".override]
constraint = ">=1.0.0"
allow_prerelease = false
scheme = "numeric"

[runtime.pi-extensions."{name}".validation]
metadata_file = "package.json"
'''


def with_dotted_extension(
    text: str,
    *,
    name: str = "foo.bar",
    version: str = "1.0.0",
) -> str:
    """Return *text* with a valid extension whose name contains dots.

    The extension key is quoted (``runtime.pi-extensions."foo.bar"``)
    because the name is a single key containing a dot.  This exercises the
    production dotted-name ``path_segments`` handling and the header audit's
    quoted-key matching.
    """
    url = f"https://registry.npmjs.org/{name}/-/{name}-{version}.tgz"
    integrity = artifact_integrity(url)
    block = _DOTTED_EXTENSION_TEMPLATE.format(
        name=name,
        version=version,
        package=name,
        url=url,
        integrity=integrity,
    )
    return text.rstrip() + "\n" + block


def with_empty_pi_extensions(text: str) -> str:
    """Remove every optional extension and declare an explicit empty table.

    Production accepts an inventory with no optional extensions, so this
    variant must load and must not invent extension environment entries.
    """
    names: list[str] = []
    for line in text.splitlines():
        match = _EXTENSION_HEADER.match(line.strip())
        if match is not None and match.group("name") not in names:
            names.append(match.group("name"))
    for name in names:
        text = remove_extension(text, name)
    return text.rstrip() + "\n\n[runtime.pi-extensions]\n"


def write_stable_inventory(
    directory: str | Path,
    *,
    name: str = "docker-constructor.toml",
) -> Path:
    """Write a fresh copy of the stable fixture into *directory*.

    Each call writes an independent file, so two calls can never
    contaminate one another.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(stable_inventory_text(), encoding="utf-8")
    return path


def write_stable_project(
    directory: str | Path,
    *,
    inventory_name: str = "docker-constructor.toml",
    dockerfile: bool = True,
) -> Path:
    """Create a temporary constructor-project document tree.

    Returns the inventory path.  When *dockerfile* is true a minimal
    ``Dockerfile`` companion is written beside the inventory, matching the
    documented constructor-project layout.
    """
    path = write_stable_inventory(directory, name=inventory_name)
    if dockerfile:
        (path.parent / "Dockerfile").write_bytes(b"FROM scratch\n")
    return path

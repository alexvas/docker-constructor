"""Repository-owned host smoke test for the locked npm environment assembler.

This module is the testable core of the dedicated host smoke script
(``scripts/smoke-npm-assembler``).  It exercises the production assembly
path end to end — production run-vector rendering, exact reviewed
Node/npm versions, a real registry lockfile fixture, and the fixed npm
policy — and fails unless the assembly exits ``0`` with the expected
installed closure, no lifecycle-script effect, and no executable link.

It writes a canonical JSON report recording the reviewed image reference,
tool versions, script/policy and fixture digests, every executed check,
the result, and a run timestamp.  The core (:func:`run_smoke`) accepts an
injected executor and cache root so tests can cover argument validation,
failure propagation, and report serialization without nested Docker.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from .assembler import assembler_script_digest, npm_policy_digest
from .errors import LockedNpmError
from .execution import DockerRunExecutor, RunExecutor, assemble
from .identity import compute_assembler_identity
from .image_ref import validate_image_reference
from .model import RootSpec, ValidatedAssemblyInput
from .preflight import preflight

#: Reviewed tool versions the pinned image must assert and satisfy.
SMOKE_NODE_VERSION = "24.18.0"
SMOKE_NPM_VERSION = "11.16.0"
SMOKE_PLATFORM = "linux-x64"

#: Canonical report schema version.
SMOKE_REPORT_SCHEMA_VERSION = 1

#: The fixture package whose ``postinstall`` lifecycle script creates a
#: known marker file.  Its published script is ``touch
#: postinstall_ran_10_nov_12_23.txt``, so whenever npm runs lifecycle
#: scripts the marker file appears inside this package's own directory;
#: the fixed policy's ``--ignore-scripts`` must prevent it.
SMOKE_MARKER_PACKAGE = "@lilly_in_the_valley/test-postinstall-script-package"
SMOKE_MARKER_VERSION = "1.0.6"

#: Path of the lifecycle marker relative to the staging workspace root.
#: It must never exist when the fixed npm policy is honoured.
LIFECYCLE_MARKER = (
    "node_modules/@lilly_in_the_valley/test-postinstall-script-package/"
    "postinstall_ran_10_nov_12_23.txt"
)

#: A small, real, installable lockfile-v3 fixture.  Provenance: ``which``
#: and ``isexe`` exercise transitive closure and reviewed-root ``bin``
#: metadata (``--no-bin-links`` must suppress ``node_modules/.bin``), and
#: :data:`SMOKE_MARKER_PACKAGE` declares ``hasInstallScript`` so its
#: ``postinstall`` marker proves ``--ignore-scripts`` blocks lifecycle
#: scripts.  SRI and resolved URLs are the exact values published by
#: registry.npmjs.org for these versions.
_SMOKE_FIXTURE = {
    "name": "npm-env-smoke",
    "version": "1.0.0",
    "lockfileVersion": 3,
    "requires": True,
    "packages": {
        "": {
            "name": "npm-env-smoke",
            "version": "1.0.0",
            "dependencies": {
                "which": "2.0.2",
                "@lilly_in_the_valley/test-postinstall-script-package": "1.0.6",
            },
        },
        "node_modules/which": {
            "version": "2.0.2",
            "resolved": "https://registry.npmjs.org/which/-/which-2.0.2.tgz",
            "integrity": (
                "sha512-BLI3Tl1TW3Pvl70l3yq3Y64i+awpwXqsGBYWkkqMtnbXgrMD"
                "+yj7rhW0kuEDxzJaYXGjEW5ogapKNMEKNMjibA=="
            ),
            "bin": {"node-which": "bin/node-which"},
            "dependencies": {"isexe": "^2.0.0"},
        },
        "node_modules/isexe": {
            "version": "2.0.0",
            "resolved": "https://registry.npmjs.org/isexe/-/isexe-2.0.0.tgz",
            "integrity": (
                "sha512-RHxMLp9lnKHGHRng9QFhRCMbYAcVpn69smSGcq3f36xjg"
                "VVWThj4qqLbTLlq7Ssj8B+fIQ1EuCEGI2lKsyQeIw=="
            ),
        },
        "node_modules/@lilly_in_the_valley/test-postinstall-script-package": {
            "version": "1.0.6",
            "resolved": (
                "https://registry.npmjs.org/@lilly_in_the_valley/"
                "test-postinstall-script-package/-/"
                "test-postinstall-script-package-1.0.6.tgz"
            ),
            "integrity": (
                "sha512-3j+idJBzUNpQOok+KtAPQSwPSLdQQhTo2erqqNQlcnFZDU1"
                "FOtCmvYWIQ1MPrIhNgqwqcfvnZZENa1uDuJ/XcA=="
            ),
            "hasInstallScript": True,
        },
    },
}

_SMOKE_FIXTURE_BYTES = json.dumps(_SMOKE_FIXTURE, sort_keys=True, indent=2).encode(
    "utf-8"
)

_SMOKE_ROOTS = (
    RootSpec("@lilly_in_the_valley/test-postinstall-script-package", "1.0.6"),
    RootSpec("which", "2.0.2"),
)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def smoke_fixture_bytes() -> bytes:
    """Return the exact UTF-8 bytes of the embedded smoke lockfile fixture."""
    return _SMOKE_FIXTURE_BYTES


def smoke_fixture_digest() -> str:
    """Return the SHA-256 hex digest of the smoke lockfile fixture."""
    return _sha256_hex(_SMOKE_FIXTURE_BYTES)


def smoke_roots() -> tuple[RootSpec, ...]:
    """Return the reviewed roots of the smoke lockfile fixture."""
    return _SMOKE_ROOTS


@dataclass(frozen=True)
class SmokeArgs:
    """Parsed smoke-script arguments: an image reference and a report path."""

    image: str
    report_path: str


def parse_smoke_args(argv: Sequence[str]) -> SmokeArgs:
    """Parse and validate the two CLI arguments.

    Only the reviewed immutable image reference and an explicit report-file
    path are accepted.  Raises :class:`LockedNpmError` for any other
    argument count, an invalid/mutable image reference, an empty report
    path, a report path whose parent directory does not exist, or a report
    path that names an existing directory.
    """
    if len(argv) != 2:
        raise LockedNpmError(
            "invalid_smoke_arguments",
            "expected exactly two arguments — the reviewed immutable image "
            f"reference and an explicit report-file path — got {len(argv)}",
        )
    image, report_path = argv[0], argv[1]
    validate_image_reference(image)
    if not isinstance(report_path, str) or not report_path:
        raise LockedNpmError(
            "invalid_smoke_arguments",
            "report-file path must be a non-empty string",
        )
    report = Path(report_path)
    if report.is_dir():
        raise LockedNpmError(
            "invalid_smoke_arguments",
            f"report-file path {report_path!r} names an existing directory",
        )
    if not report.parent.exists():
        raise LockedNpmError(
            "invalid_smoke_arguments",
            f"report-file parent directory {str(report.parent)!r} does not exist",
        )
    return SmokeArgs(image, report_path)


def verify_installed_closure(
    *, staging: Path, validated: ValidatedAssemblyInput
) -> None:
    """Raise unless the staging closure is exactly the validated closure.

    Recursively enumerates package roots (directories containing a
    ``package.json``) under ``node_modules`` — including scoped and nested
    packages — and requires the discovered set to equal the validated lock
    paths exactly.  Each installed ``package.json`` must also declare the
    same name and version as the corresponding validated lock package.
    """
    node_modules = staging / "node_modules"
    try:
        discovered = sorted(
            p.parent.relative_to(staging).as_posix()
            for p in node_modules.rglob("package.json")
        )
    except OSError as exc:
        raise LockedNpmError(
            "smoke_closure_mismatch",
            f"cannot enumerate node_modules after assembly: {exc}",
        ) from exc

    expected = {pkg.path: (pkg.name, pkg.version) for pkg in validated.packages}

    for rel in discovered:
        if not rel.startswith("node_modules/"):
            raise LockedNpmError(
                "smoke_closure_mismatch",
                f"unexpected package.json outside node_modules: {rel!r}",
            )
        name_version = expected.get(rel)
        if name_version is None:
            raise LockedNpmError(
                "smoke_closure_mismatch",
                f"unexpected installed package {rel!r}",
            )
        expected_name, expected_version = name_version
        try:
            data = json.loads(
                (staging / rel / "package.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise LockedNpmError(
                "smoke_closure_mismatch",
                f"cannot read package.json for {rel!r}: {exc}",
            ) from exc
        actual_name = data.get("name")
        actual_version = data.get("version")
        if actual_name != expected_name or actual_version != expected_version:
            raise LockedNpmError(
                "smoke_closure_mismatch",
                f"installed package {rel!r} is "
                f"{actual_name!r}@{actual_version!r}, expected "
                f"{expected_name!r}@{expected_version!r}",
            )

    missing = sorted(set(expected) - set(discovered))
    if missing:
        raise LockedNpmError(
            "smoke_closure_mismatch",
            f"expected installed package {missing[0]!r} is missing",
        )


def verify_no_executable_link(*, staging: Path) -> None:
    """Raise unless ``node_modules/.bin`` is absent (``--no-bin-links``)."""
    if os.path.lexists(staging / "node_modules" / ".bin"):
        raise LockedNpmError(
            "smoke_executable_link",
            "node_modules/.bin exists; --no-bin-links must prevent "
            "executable-link creation",
        )


def verify_no_lifecycle_effect(*, staging: Path) -> None:
    """Raise unless the lifecycle marker is absent from the staging tree.

    The fixture package's ``postinstall`` creates :data:`LIFECYCLE_MARKER`
    inside its own directory.  Its absence proves the fixed policy's
    ``--ignore-scripts`` suppressed every install lifecycle script.
    """
    marker = staging / LIFECYCLE_MARKER
    if os.path.lexists(marker):
        raise LockedNpmError(
            "smoke_lifecycle_effect",
            f"lifecycle-script marker {LIFECYCLE_MARKER!r} exists; "
            "--ignore-scripts must suppress install scripts",
        )


def extract_image_digest(image: str) -> str:
    """Return the digest portion of a validated immutable image reference.

    The input must already have been accepted by
    :func:`validate_image_reference`.  A bare ``sha256:<64 lowercase hex>``
    digest is returned unchanged; a repository-qualified reference
    (``<registry>/<repository>@sha256:…`` or
    ``<registry>/<repository>:<tag>@sha256:…``) yields the digest that
    follows its single ``@`` separator.  Repository names and descriptive
    tags are never included in the result.
    """
    return image.rsplit("@", 1)[-1]


def render_smoke_report(
    *,
    image: str,
    checks: Sequence[dict[str, str]],
    result: str,
    failure: dict[str, str] | None,
    timestamp: str,
    validated: ValidatedAssemblyInput | None = None,
) -> dict[str, object]:
    """Build the canonical smoke-report dict."""
    return {
        "schema_version": SMOKE_REPORT_SCHEMA_VERSION,
        "image_digest": extract_image_digest(image),
        "node_version": SMOKE_NODE_VERSION,
        "npm_version": SMOKE_NPM_VERSION,
        "platform": SMOKE_PLATFORM,
        "script_digest": assembler_script_digest(),
        "policy_digest": npm_policy_digest(),
        "fixture_digest": smoke_fixture_digest(),
        "lockfile_digest": (
            validated.lockfile_digest
            if validated is not None
            else smoke_fixture_digest()
        ),
        "checks": list(checks),
        "result": result,
        "failure": failure,
        "timestamp": timestamp,
    }


def serialize_smoke_report(report: dict[str, object]) -> str:
    """Serialize *report* as canonical, deterministic JSON text."""
    return json.dumps(report, sort_keys=True, indent=2) + "\n"


def run_smoke(
    *,
    image: str,
    report_path: str | Path,
    executor: RunExecutor,
    cache_root: str | Path,
    timestamp: str | None = None,
) -> int:
    """Run the smoke assembly and write the canonical report.

    Returns ``0`` when every check passes and the report records
    ``result: "pass"``; returns ``1`` (after writing a ``result: "fail"``
    report) when any check fails.  Raises :class:`LockedNpmError` only when
    the report itself cannot be written.
    """
    checks: list[dict[str, str]] = []
    failure: dict[str, str] | None = None
    result_status = "pass"
    exit_code = 0
    validated: ValidatedAssemblyInput | None = None
    current = "image_reference"

    try:
        current = "image_reference"
        validate_image_reference(image)
        checks.append({"name": current, "status": "pass"})

        current = "preflight"
        validated = preflight(
            smoke_fixture_bytes(),
            roots=smoke_roots(),
            platform=SMOKE_PLATFORM,
            node_version=SMOKE_NODE_VERSION,
            npm_version=SMOKE_NPM_VERSION,
        )
        checks.append({"name": current, "status": "pass"})

        current = "assembler_identity"
        assembler = compute_assembler_identity(
            image_digest=image,
            node_version=SMOKE_NODE_VERSION,
            npm_version=SMOKE_NPM_VERSION,
            script_digest=assembler_script_digest(),
            policy_digest=npm_policy_digest(),
            platform=SMOKE_PLATFORM,
        )
        checks.append({"name": current, "status": "pass"})

        current = "assembly"
        result = assemble(
            validated=validated,
            assembler=assembler,
            cache_root=cache_root,
            executor=executor,
        )
        checks.append({"name": current, "status": "pass"})

        current = "installed_closure"
        verify_installed_closure(staging=result.staging, validated=validated)
        checks.append({"name": current, "status": "pass"})

        current = "no_lifecycle_script_effect"
        verify_no_lifecycle_effect(staging=result.staging)
        checks.append({"name": current, "status": "pass"})

        current = "no_executable_link"
        verify_no_executable_link(staging=result.staging)
        checks.append({"name": current, "status": "pass"})
    except LockedNpmError as exc:
        failure = {"reason": exc.reason, "detail": exc.detail}
        result_status = "fail"
        exit_code = 1
        checks.append({"name": current, "status": "fail"})
    except Exception as exc:
        failure = {
            "reason": type(exc).__name__,
            "detail": str(exc) or repr(exc),
        }
        result_status = "fail"
        exit_code = 1
        checks.append({"name": current, "status": "fail"})

    report = render_smoke_report(
        image=image,
        checks=checks,
        result=result_status,
        failure=failure,
        timestamp=timestamp if timestamp is not None else _now_iso(),
        validated=validated,
    )
    try:
        Path(report_path).write_text(serialize_smoke_report(report), encoding="utf-8")
    except OSError as exc:
        raise LockedNpmError(
            "smoke_report_write_failed",
            f"cannot write smoke report to {report_path}: {exc}",
        ) from exc
    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for the dedicated host smoke script."""
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    try:
        args = parse_smoke_args(argv)
    except LockedNpmError as exc:
        print(f"smoke-npm-assembler: {exc.reason}: {exc.detail}", file=sys.stderr)
        return 2

    cache_root = Path(tempfile.mkdtemp(prefix="npm-env-smoke-"))
    try:
        return run_smoke(
            image=args.image,
            report_path=args.report_path,
            executor=DockerRunExecutor(),
            cache_root=cache_root,
        )
    except LockedNpmError as exc:
        print(f"smoke-npm-assembler: {exc.reason}: {exc.detail}", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(cache_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

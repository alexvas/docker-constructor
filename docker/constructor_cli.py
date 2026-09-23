"""
Importable thin facade for the Pi Docker constructor.

Primary commands: ``build``, ``run``, ``check-updates``.
Auxiliary commands: ``validate``, ``show``, ``doctor``, ``verify``.

The facade owns only argument parsing, input validation, a generic
rendering layer, and exit-code mapping.  All domain behaviour is
accessed through an injectable ``CommandDispatcher`` protocol.

Read-only domain logic (validate, show, check-updates) is delegated to
``docker.versioning.readonly_service`` — the facade calls a single
``dispatch()`` entry point after resolving the inventory path.

This module is a pure importable package module — it **never** mutates
``sys.path``.  Direct execution is handled by the minimal bootstrap in
``docker/docker-constructor.py``.

Usage (imported)::

    from docker.constructor_cli import main
    exit_code = main(["validate", "--scope", "build"])

Usage (via executable wrapper)::

    docker/docker-constructor.py validate [--scope build|runtime|all]
"""

from __future__ import annotations

import argparse
import json as _json
import os as _os_builtin
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from docker.versioning.model import Inventory, LocalConfig

from docker.versioning.constructor_project import (
    ConstructorProject,
    resolve_constructor_project,
)
from docker.versioning.dispatch_types import CommandResult, ExitKind
from docker.versioning.immutable import deep_freeze
from docker.versioning.model import HostAccessPolicy
from docker.versioning.host_progress import HostDiagnosticEvent, HostPhaseEvent
from docker.versioning.host_presentation import (
    HostPresentationSession,
    TerminalHostRenderer,
    format_failure_report,
    select_presentation,
)

_INSTALLATION_ROOT = Path(__file__).resolve().parent.parent

_EXIT_CODES: dict[ExitKind, int] = {
    ExitKind.SUCCESS: 0,
    ExitKind.POLICY: 1,
    ExitKind.CLI: 2,
    ExitKind.CONFIG: 3,
    ExitKind.OPERATIONAL: 4,
}

# Maximum bytes of captured stdout / stderr included in run-failure
# diagnostics (text and JSON modes).  Exceeding content is truncated
# with a truncation marker so that evidence artifacts, log output, and
# JSON payloads stay bounded even when a container emits megabytes of
# unstructured diagnostics before exiting.
MAX_RUN_DIAGNOSTIC_BYTES: int = 65_536

_TRUNCATION_MARKER = "[truncated]"
_TRUNCATION_MARKER_BYTES = len(_TRUNCATION_MARKER.encode("utf-8"))


def _truncate_diagnostic(value: str) -> tuple[str, bool]:
    """Truncate *value* to at most ``MAX_RUN_DIAGNOSTIC_BYTES``
    UTF-8 bytes, ending with ``[truncated]`` when truncation
    is necessary.

    Returns ``(maybe_truncated, was_truncated)``."""
    encoded = value.encode("utf-8")
    if len(encoded) <= MAX_RUN_DIAGNOSTIC_BYTES:
        return value, False
    # Leave room for the marker.
    limit = MAX_RUN_DIAGNOSTIC_BYTES - _TRUNCATION_MARKER_BYTES
    if limit <= 0:
        return _TRUNCATION_MARKER, True
    truncated_bytes = encoded[:limit]
    # Drop any trailing incomplete multibyte sequence.
    try:
        truncated_text = truncated_bytes.decode("utf-8")
    except UnicodeDecodeError:
        # Strip trailing byte(s) until valid.
        for cut in range(1, 5):
            try:
                truncated_text = encoded[:limit - cut].decode("utf-8")
                break
            except UnicodeDecodeError:
                continue
        else:
            truncated_text = ""
    result = truncated_text + _TRUNCATION_MARKER
    return result, True


# ═══════════════════════════════════════════════════════════════════════
# Immutable dispatch boundary
# ═══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class CommandRequest:
    """Parsed argument bundle passed to the dispatcher.

    All fields are deeply immutable — ``command_args`` is stored as
    a ``MappingProxyType`` so that callers cannot mutate it after
    construction.
    """

    command: str
    constructor_project: ConstructorProject
    output: str          # "text" | "json"
    verbose: bool
    color: str           # "auto" | "always" | "never"
    command_args: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Always deep-freeze — even a pre-frozen proxy may wrap mutable children
        object.__setattr__(
            self,
            "command_args",
            deep_freeze(self.command_args),
        )




class CommandDispatcher(Protocol):
    """Domain boundary — every subcommand flows through ``execute``."""

    def execute(self, command: str, request: CommandRequest) -> CommandResult:
        ...


# ═══════════════════════════════════════════════════════════════════════
# Inventory resolution & domain dispatch
# ═══════════════════════════════════════════════════════════════════════


def _resolve_inventory_path(request: CommandRequest) -> Path:
    """Return the fixed inventory path for the selected project."""
    return request.constructor_project.inventory


def _resolve_runtime_projection(
    explicit: object, repo_root: Path, cache_root: Path,
) -> Path | None:
    """Resolve the runtime projection path for verification.

    *explicit* is the value of ``--runtime-projection`` (a string
    or ``None``). When absent, the most recent external runtime projection
    is used. Returns ``None`` only when the namespace or projection is absent;
    unsafe or mismatched project state raises ``ProjectStateError``.
    """
    if explicit:
        return Path(str(explicit))
    from docker.versioning.project_state import resolve_project_state
    runtime_dir = resolve_project_state(
        repo_root, cache_root=cache_root, create=False,
    ).runtime_root
    if not runtime_dir.is_dir():
        return None
    tomls = sorted(
        runtime_dir.glob("*.toml"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return tomls[0] if tomls else None


class _CommandProcessResult(Protocol):
    @property
    def return_code(self) -> int: ...

    @property
    def stdout(self) -> str: ...


class _CommandRunner(Protocol):
    def run(self, argv: Sequence[str]) -> _CommandProcessResult: ...


def _discover_runtime_projection_from_container(
    container: str, runner: _CommandRunner,
) -> Path | None:
    """Return the host source of the container's runtime projection mount.

    The running container is authoritative: its mount metadata binds the
    exact projection used for this session.  ``None`` lets callers retain
    their legacy generated-artifact fallback when inspection is unavailable.
    """
    try:
        result = runner.run([
            "docker", "inspect", "--format", "{{json .Mounts}}", container,
        ])
    except OSError:
        return None
    if result.return_code != 0:
        return None
    try:
        mounts = _json.loads(result.stdout)
    except (TypeError, ValueError):
        return None
    if not isinstance(mounts, list):
        return None
    for mount in mounts:
        if not isinstance(mount, dict):
            continue
        if mount.get("Destination") == "/run/pi-cli/docker-constructor.runtime.toml":
            source = mount.get("Source")
            if isinstance(source, str) and source:
                return Path(source)
    return None


def _discover_workspace_paths_from_container(
    container: str, runner: _CommandRunner,
) -> tuple[Path, ...] | None:
    """Read ``WORKSPACE_PATH_1..N`` env vars from *container*.

    Returns the container-side paths in numeric suffix order or
    ``None`` when the query fails, no projects are set, or the
    indices are not 1..N consecutive.
    """
    try:
        result = runner.run([
            "docker", "exec", container, "sh", "-c",
            "env | sort | grep '^WORKSPACE_PATH_'",
        ])
    except OSError:
        return None
    if result.return_code != 0 or not result.stdout.strip():
        return None
    # Parse (index, path) preserving duplicates
    parsed: list[tuple[int, str]] = []
    for line in result.stdout.strip().split("\n"):
        if "=" not in line:
            continue
        name, _, value = line.partition("=")
        value = value.strip()
        if not value:
            continue
        # Extract numeric suffix: WORKSPACE_PATH_<N>
        suffix = name[len("WORKSPACE_PATH_"):]
        if not suffix.isdigit():
            continue  # skip malformed keys like WORKSPACE_PATH_X
        parsed.append((int(suffix), value))
    if not parsed:
        return None
    # Sort by numeric index so order is preserved
    parsed.sort(key=lambda item: item[0])
    # Validate consecutive 1..N (no gaps, no WORKSPACE_PATH_10 → skip)
    indices = [idx for idx, _ in parsed]
    if indices != list(range(1, len(indices) + 1)):
        return None
    return tuple(Path(p) for _, p in parsed)


def _resolve_verify_host_access(
    inv_path: str,
    *,
    inventory: "Inventory | None" = None,
    local_config: "LocalConfig | None" = None,
) -> tuple[HostAccessPolicy | None, str | None, str | None]:
    """Resolve host-access expectations for runtime verification.

    Returns ``(host_access, address, error)``.

    * ``error`` is not ``None`` — inventory or companion
      unreadable/malformed/missing; caller must report CONFIG.
    * ``host_access=None``, ``address=None`` — disabled.
    * ``host_access=...``, ``address=...`` — enabled with addr.

    Callers inside a command transaction pass the already-loaded shared
    reviewed inventory and local aggregate result so no document is parsed
    twice. When they are omitted, this helper performs one shared
    transaction on its own.
    """
    from pathlib import Path
    from docker.versioning.inventory import (
        load_project_configuration,
        resolve_local_companion_path,
    )
    from docker.versioning.model import HostAccessPolicy

    _p = Path(inv_path)
    if inventory is None or local_config is None:
        try:
            inventory, local_config = load_project_configuration(_p)
        except Exception as exc:
            return None, None, f"cannot load inventory {_p}: {exc}"
    ha = getattr(inventory.runtime, "host_access", None)
    if not isinstance(ha, HostAccessPolicy) or not ha.enabled:
        return None, None, None
    mode = ha.mode
    if not mode:
        return None, None, None
    companion_path = resolve_local_companion_path(_p)
    address: str | None = None
    local_host_access = getattr(local_config, "host_access", None)
    local_address = getattr(local_host_access, "address", None)
    if local_address and local_address.strip():
        address = local_address.strip()
    if not companion_path.is_file():
        return None, None, (
            f"host access enabled ({mode}) but local companion "
            f"{companion_path} missing; run 'doctor' or create it"
        )
    if not address:
        return None, None, (
            f"host access enabled ({mode}) but {companion_path} "
            f"has no [host-access].address; run 'doctor'"
        )
    return ha, address, None


def _resolve_verify_corporate_network(
    inv_path: str,
    repo_root: Path,
    *,
    local_config: "LocalConfig | None" = None,
) -> tuple[bool, str | None, str | None, str | None]:
    """Resolve corporate trust/proxy expectations for runtime verification.

    Returns ``(trust_enabled, proxy_url, proxy_no_proxy, error)``.

    ``error`` is not ``None`` when the companion is malformed or the
    enabled fixed bundle is unusable; the caller must report CONFIG before
    any Docker inspection.

    Callers inside a command transaction pass the shared local aggregate
    result so the companion is not reopened; otherwise this helper loads it
    once itself.
    """
    from docker.versioning.inventory import (
        resolve_corporate_trust_bundle_path,
        resolve_local_corporate_settings,
        validate_corporate_trust_bundle,
    )

    try:
        if local_config is None:
            local = resolve_local_corporate_settings(
                Path(inv_path),
                repository_root=repo_root,
            )
        else:
            local = local_config
            if local.corporate_trust.enabled:
                validate_corporate_trust_bundle(
                    resolve_corporate_trust_bundle_path(repo_root)
                )
    except Exception as exc:
        return False, None, None, str(exc)
    return (
        local.corporate_trust.enabled,
        local.network_proxy.url,
        local.network_proxy.no_proxy,
        None,
    )


def _read_env_key(key: str, env_path: Path) -> str | None:
    """Read a single value from the selected project's ``.env`` file.

    Returns ``None`` when the file is missing or the key is absent.
    """
    if not env_path.is_file():
        return None
    try:
        for line in env_path.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                continue
            k, _, v = stripped.partition("=")
            if k.strip() == key:
                val = v.strip().strip("'\"")
                return val if val else None
    except OSError:
        pass
    return None


def _to_bool(value: object) -> bool:
    """Coerce a command-args value to bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return bool(value)


def _lexical_absolute_path(path: str) -> str:
    """Normalize *path* to an absolute spelling without resolving symlinks."""
    return _os_builtin.path.abspath(_os_builtin.path.normpath(path))


def _tui_workspace_selector(workspace_root: str | None = None) -> Any:
    """Create a curses-based :class:`~docker.launcher.WorkspaceSelector`
    backed by the maintained TUI from :mod:`docker.tui`.

    Builds a directory tree from *workspace_root* (or ``Path.home()`` as
    ultimate fallback) and opens the interactive curses interface.
    Returns a :class:`~docker.launcher.WorkspaceSelection` or
    ``None`` on cancel.

    The caller is responsible for resolving *workspace_root* according to
    the precedence: ``--workspace-root`` → ``.env WORKSPACE_ROOT``
    → ``Path.home()``.
    """
    import os as _os
    from docker.tui import build_tree, run_tui

    class TuiWorkspaceSelector:
        def select(self) -> Any:
            resolved = workspace_root
            if resolved is None:
                resolved = str(Path.home())

            base_path = Path(resolved)
            if not base_path.is_dir():
                # Invalid directory → fall back to home
                base_path = Path.home()

            tree_roots = build_tree(base_path)
            if not tree_roots:
                # Empty tree → try home as last resort
                home_tree = build_tree(Path.home())
                if not home_tree:
                    return None
                tree_roots = home_tree

            primary_item, extra_items = run_tui(
                [],  # no live IDE projects
                tree_roots,
                light_theme=False,
            )

            if primary_item is None:
                return None

            # Extract paths from FlatItem tree nodes
            primary_path = ""
            if (
                primary_item.kind == "tree"
                and primary_item.node is not None
            ):
                primary_path = str(primary_item.node.path)

            if not primary_path:
                return None

            # Tree nodes retain the spelling of a relative workspace root.
            # Convert selections to absolute lexical paths without resolving
            # symlinks, matching the workspace bind-mount path contract.
            primary_path = _lexical_absolute_path(primary_path)
            extra_paths: list[str] = []
            for item in extra_items:
                if item.kind == "tree" and item.node is not None:
                    extra_paths.append(_lexical_absolute_path(str(item.node.path)))

            from docker.launcher import WorkspaceSelection
            return WorkspaceSelection(
                workspace=primary_path,
                extra_workspaces=tuple(extra_paths),
            )

    return TuiWorkspaceSelector()


def _real_dispatcher(
    command: str,
    request: CommandRequest,
    *,
    _prompt_user: Callable[[str], bool] | None = None,
    _process_runner: Any = None,
    _container_inspector: Any = None,
    _run_executor: Any = None,
    _create_projection: Any = None,
    _workspace_selector: Any = None,
    _progress_renderer: Any = None,
    _host_presentation_factory: Any = None,
) -> CommandResult:
    """Thin facade wrapper that delegates to the internal services.

    Read-only commands (validate, show, check-updates) are routed to
    ``docker.versioning.readonly_service``.
    Build and doctor commands are routed to
    ``docker.versioning.build_orchestration``.

    When ``_prompt_user`` is ``None`` (default), the real
    ``_stdin_prompt`` is used.  Tests may inject a mock.

    When ``_process_runner`` is ``None`` (default), the facade
    creates a real :class:`~docker.launcher.ProcessRunner`.
    Likewise for ``_container_inspector`` (defaults to
    :class:`~docker.launcher.DockerContainerInspector`) and
    ``_run_executor`` (defaults to
    :class:`~docker.launcher.DockerRunExecutor`).
    Tests may inject fakes to prove boundary wiring without
    invoking Docker.

    When ``_workspace_selector`` is ``None`` (default), the facade
    creates a real curses-based :class:`~docker.launcher.WorkspaceSelector`.
    Tests may inject a fake to prove TUI wiring.

    ``_progress_renderer`` is the facade-owned interactive discovery
    renderer installed by :func:`main` for text-output check-updates
    with a TTY stderr; it is forwarded to the read-only service and
    otherwise left as ``None``.
    """
    prompt = _stdin_prompt if _prompt_user is None else _prompt_user
    # ── build confirmation ───────────────────────────────────────────
    if command == "build":
        from docker.networking import BuildOutputPolicy
        from docker.versioning.build_orchestration import (
            BuildRequest,
            orchestrate_build,
        )
        from docker.versioning.model import BuildLocalInputs

        inv_path = _resolve_inventory_path(request)
        # Single transaction-loading boundary: read reviewed inventory and
        # local companion exactly once, retain the host-only output policy
        # at the facade, and hand planning only domain-owned slices.
        try:
            from docker.versioning.inventory import load_project_configuration
            inventory, local_config = load_project_configuration(inv_path)
        except Exception as exc:
            return CommandResult(
                exit_kind=ExitKind.CONFIG,
                message=f"cannot load build configuration: {exc}",
            )
        dockerfile = request.constructor_project.dockerfile
        if not dockerfile.is_file():
            return CommandResult(
                exit_kind=ExitKind.CONFIG,
                message=f"Dockerfile not found: {dockerfile}",
            )

        dry_run = bool(request.command_args.get("dry_run", False))
        yes = bool(request.command_args.get("yes", False))

        if not dry_run and not yes:
            approval = prompt("Build the Pi container image?")
            if not approval:
                return CommandResult(
                    exit_kind=ExitKind.SUCCESS,
                    message="build cancelled by user",
                )
            c_args = dict(request.command_args)
            c_args["yes"] = True
        else:
            c_args = dict(request.command_args)

        c_args = _deep_freeze_command_args(c_args)

        # Local output policy remains facade-only: it selects no domain
        # behavior and is never attached to the build request.  Planning
        # receives only the narrow domain-owned local slices.  The facade
        # owns the presentation session (mailbox + worker) and installs only
        # its prompt-returning guarded sink on the request.
        session = (
            _host_presentation_factory(local_config.output)
            if _host_presentation_factory is not None
            else None
        )
        host_event_sink = session.sink if session is not None else None
        local_inputs = BuildLocalInputs.from_local_config(local_config)

        # Build typed DTO
        raw_overrides = c_args.get("overrides")
        if isinstance(raw_overrides, Mapping):
            overrides = MappingProxyType(dict(raw_overrides))
        else:
            overrides = MappingProxyType({})

        build_request = BuildRequest(
            inventory_path=str(inv_path),
            repo_root=str(_INSTALLATION_ROOT),
            project_root=str(request.constructor_project.root),
            context=str(request.constructor_project.root),
            dockerfile=str(dockerfile),
            platform=str(c_args.get("platform", "linux-amd64")),
            tag=c_args.get("tag") if c_args.get("tag") is not None else None,
            overrides=overrides,
            cache=_to_bool(c_args.get("cache", True)),
            pull=_to_bool(c_args.get("pull", False)),
            progress=str(c_args.get("progress", "auto")),
            output_policy=(BuildOutputPolicy.CAPTURED
                           if request.output == "json"
                           else BuildOutputPolicy.STREAMED),
            uid=c_args.get("uid") if c_args.get("uid") is not None else None,
            gid=c_args.get("gid") if c_args.get("gid") is not None else None,
            confirmed=_to_bool(c_args.get("yes", False)),
            dry_run=dry_run,
            event_sink=host_event_sink,
            host_presentation_complete=(
                session.shutdown if session is not None else None
            ),
        )

        try:
            result = orchestrate_build(
                build_request, inventory=inventory, local_inputs=local_inputs,
            )
        except BaseException:
            if session is not None:
                session.shutdown()
            raise

        # BuildResult → CommandResult
        data: dict[str, object] | None = None
        # A normal text build has already shown Docker's native output; do
        # not turn its command vector into a second primary result.
        show_vector = dry_run or request.output == "json" or request.verbose
        if show_vector and (result.build_args or result.display_string):
            data = {
                "build_args": list(result.build_args),
                "display_string": result.display_string,
            }
        if result.process_result and request.output == "json":
            if data is None:
                data = {}
            data.update({
                "return_code": result.process_result.return_code,
                "output_policy": result.process_result.output_policy.value,
                "stdout": result.process_result.stdout,
                "stderr": result.process_result.stderr,
            })
        if result.publish_result and (request.output == "json" or request.verbose):
            if data is None:
                data = {}
            data["published_path"] = result.publish_result.published_path
            # The generic text renderer displays ``display_string`` verbatim.
            # Keep build-specific diagnostic metadata at this facade boundary.
            if request.verbose and request.output == "text" and data.get("display_string"):
                data["display_string"] = (
                    f"{data['display_string']}\n"
                    f"published_path: {result.publish_result.published_path}"
                )

        message = result.message
        message_owned_by_presentation = False
        if (
            result.host_failure is not None
            and result.exit_kind is not ExitKind.SUCCESS
        ):
            report = format_failure_report(
                result.host_failure.phase,
                result.host_failure.step,
                summary=result.host_failure.summary,
                tail=result.host_failure.tail,
                tail_stream=result.host_failure.tail_stream,
                timeout_retained_context=(
                    result.host_failure.timeout_retained_context
                ),
                logical_resource=result.host_failure.logical_resource,
                hostnames=result.host_failure.hostnames,
                show_network_hosts=local_config.output.show_network_hosts,
                exception_types=result.host_failure.exception_types,
            )
            # The contextual report owns both the concise summary and the
            # structurally separate retained tail.  Appending ``result.message``
            # here would replay assembler details that are already represented
            # by that tail.
            message = report
            if request.output == "json":
                if data is None:
                    data = {}
                data["host_failure"] = {
                    "phase": result.host_failure.phase.value,
                    "step": result.host_failure.step.value,
                    "summary": result.host_failure.summary,
                    "tail": result.host_failure.tail,
                    "tail_stream": (
                        result.host_failure.tail_stream.value
                        if result.host_failure.tail_stream is not None
                        else None
                    ),
                    "logical_resource": result.host_failure.logical_resource,
                    "hostnames": list(result.host_failure.hostnames),
                    "exception_types": list(result.host_failure.exception_types),
                    "timeout_retained_context": (
                        result.host_failure.timeout_retained_context
                    ),
                }
            if session is not None:
                session.submit_final_report(report)
                # A live actor owns the attempt even if admission or rendering
                # fails; retain the report but never retry it synchronously.
                message_owned_by_presentation = True
        if session is not None:
            session.shutdown()
        return CommandResult(
            exit_kind=result.exit_kind,
            message=message,
            data=data,
            message_owned_by_presentation=message_owned_by_presentation,
        )

    # ── doctor confirmation ──────────────────────────────────────────
    if command == "doctor":
        from docker.versioning.build_orchestration import (
            DoctorRequest,
            orchestrate_doctor,
        )

        apply_override = bool(
            request.command_args.get("apply_override", False),
        )
        yes = bool(request.command_args.get("yes", False))

        if apply_override and not yes:
            approval = prompt(
                "Apply rootless Docker host-gateway override?"
            )
            if not approval:
                return CommandResult(
                    exit_kind=ExitKind.SUCCESS,
                    message="rootless override repair denied by user",
                )
            c_args = dict(request.command_args)
            c_args["yes"] = True
        else:
            c_args = dict(request.command_args)

        c_args = _deep_freeze_command_args(c_args)

        # Resolve inventory path
        try:
            inv_path = _resolve_inventory_path(request)
        except OSError as exc:
            return CommandResult(
                exit_kind=ExitKind.CONFIG,
                message=f"cannot resolve inventory path: {exc}",
            )

        # Build typed DTO
        raw_probe_image = c_args.get("probe_image")
        probe_image = (
            raw_probe_image if isinstance(raw_probe_image, str)
            else "alpine:3.20"
        )
        raw_probe_timeout = c_args.get("probe_timeout")
        probe_timeout = (
            raw_probe_timeout if isinstance(raw_probe_timeout, int)
            and not isinstance(raw_probe_timeout, bool) else None
        )
        doctor_request = DoctorRequest(
            apply_override=apply_override,
            repair_consent=_to_bool(c_args.get("yes", False)),
            inventory_path=inv_path,
            probe_image=probe_image,
            probe_timeout=probe_timeout,
        )
        result = orchestrate_doctor(doctor_request)

        # DoctorResult → CommandResult
        msg = result.message
        network_ok = result.selected_gateway is not None
        override_present = result.override_plan is not None
        override_state = getattr(result.override_plan, "state", None)

        lines: list[str] = []
        if network_ok:
            lines.append(f"Gateway reachable: {result.selected_gateway}")
        else:
            lines.append("No working gateway")
        if override_present and override_state is not None:
            lines.append(f"Override: {override_state.value}")
        if result.repair_applied:
            lines.append("Rootless override applied")
            post = result.post_repair_diagnosis
            if post is not None and post.resolved_address:
                lines.append(f"Post-repair gateway: {post.resolved_address}")
        if result.repair_failure is not None:
            lines.append(f"Repair failed: {result.repair_failure.detail}")
        if msg:
            lines.append(msg)

        # DoctorResult → CommandResult — always emit structured data
        # so that JSON consumers get the full diagnosis even on failures.
        data: dict[str, object] = {
            "gateway": result.selected_gateway,
            "repair_applied": result.repair_applied,
        }

        # -- initial diagnosis ----------------------------------------
        init = result.initial_diagnosis
        if init is not None:
            data["docker_mode"] = init.mode.value
            data["override_installed"] = init.override_installed
            data["override_needed"] = init.override_needed
            data["probes"] = [
                {
                    "candidate": p.candidate,
                    "ok": p.ok,
                    "resolved_ip": p.resolved_ip,
                    "detail": p.detail,
                }
                for p in init.probes
            ]

        # -- override plan --------------------------------------------
        if override_present and override_state is not None:
            data["override_state"] = override_state.value

        # -- repair failure (full structured) -------------------------
        if result.repair_failure is not None:
            rf = result.repair_failure
            data["repair_failure"] = {
                "operation": rf.operation,
                "path_or_command": rf.path_or_command,
                "detail": rf.detail,
                "persistence_applied": rf.persistence_applied,
            }

        # -- post-repair diagnosis ------------------------------------
        post = result.post_repair_diagnosis
        if post is not None:
            post_gw = post.resolved_address
            post_data: dict[str, object] = {
                "gateway": post_gw,
                "mode": post.mode.value,
                "probes": [
                    {
                        "candidate": p.candidate,
                        "ok": p.ok,
                        "resolved_ip": p.resolved_ip,
                        "detail": p.detail,
                    }
                    for p in post.probes
                ],
            }
            data["post_repair"] = post_data

        return CommandResult(
            exit_kind=result.exit_kind,
            message="\n".join(lines) if lines else None,
            data=data,
        )

    # ── run ───────────────────────────────────────────────────────────
    if command == "run":
        from docker.launcher import (
            DockerContainerInspector,
            DockerRunExecutor,
            NoWorkspaceError,
            ProcessRunner,
            RunRequest,
            orchestrate_run,
            resolve_workspace_selection,
        )

        try:
            inv_path = _resolve_inventory_path(request)
        except OSError as exc:
            return CommandResult(
                exit_kind=ExitKind.CONFIG,
                message=f"cannot resolve inventory path: {exc}",
            )

        c_args = _deep_freeze_command_args(request.command_args)

        # ── workspace selection ──────────────────────────────────
        workspace = c_args.get("workspace")
        extra_workspaces: tuple[str, ...] = tuple(
            c_args.get("extra_workspaces") or ()
        )
        tui = bool(c_args.get("tui", False))

        try:
            # Resolve selector: use injected fake, or create the real
            # curses-based TUI selector when --tui is requested.
            _selector = _workspace_selector
            if _selector is None and tui:
                # Resolve workspace root: CLI > project-local .env > home.
                _workspace_root = c_args.get("workspace_root")
                if _workspace_root is None:
                    _workspace_root = _read_env_key(
                        "WORKSPACE_ROOT", request.constructor_project.dotenv
                    )
                if _workspace_root is not None:
                    _workspace_root = _os_builtin.path.expanduser(str(_workspace_root))
                elif tui:
                    # No explicit base dir — selector will use
                    # Path.home() internally.
                    pass
                _selector = _tui_workspace_selector(workspace_root=_workspace_root)

            selection = resolve_workspace_selection(
                workspace=workspace if workspace is not None else None,
                extra_workspaces=extra_workspaces,
                tui=tui,
                selector=_selector,
            )
        except NoWorkspaceError as exc:
            return CommandResult(
                exit_kind=ExitKind.CONFIG,
                message=str(exc),
            )
        except ValueError as exc:
            return CommandResult(
                exit_kind=ExitKind.CONFIG,
                message=str(exc),
            )

        # ── runtime overrides ──────────────────────────────────
        raw_overrides: tuple[str, ...] = tuple(
            c_args.get("overrides") or ()
        )
        overrides: dict[str, str] = {}
        for raw in raw_overrides:
            if "=" not in raw:
                return CommandResult(
                    exit_kind=ExitKind.CONFIG,
                    message=f"invalid override {raw!r} (expected KEY=VALUE)",
                )
            key, _, value = raw.partition("=")
            overrides[key.strip()] = value.strip()

        # ── remaining flags ────────────────────────────────────
        image = c_args.get("image") or "pi-cli-pi:latest"
        dry_run = bool(c_args.get("dry_run", False))
        tty_flag = c_args.get("tty")
        tty = True if tty_flag is None else bool(tty_flag)
        stdin_open_flag = c_args.get("stdin_open")
        stdin_open = True if stdin_open_flag is None else bool(stdin_open_flag)
        chown = c_args.get("chown_on_start")
        chown_on_start = str(chown) if chown is not None else None
        command_args: tuple[str, ...] = tuple(
            c_args.get("passthrough") or ()
        )

        # ── build request and dispatch ─────────────────────────
        _runner = _process_runner
        if _runner is None:
            _runner = ProcessRunner()
        _inspector = _container_inspector
        if _inspector is None:
            _inspector = DockerContainerInspector(_runner)
        _executor = _run_executor
        if _executor is None:
            _executor = DockerRunExecutor(_runner)

        run_request = RunRequest(
            inventory_path=str(inv_path),
            repo_root=str(_INSTALLATION_ROOT),
            project_root=str(request.constructor_project.root),
            image=image,
            selection=selection,
            pi_home_host=str(Path.home() / ".pi"),
            overrides=overrides,
            tty=tty,
            stdin_open=stdin_open,
            chown_on_start=chown_on_start,
            command=command_args,
            dry_run=dry_run,
            executor=_executor,
            inspector=_inspector,
            _create_projection=_create_projection,
        )

        result = orchestrate_run(run_request)

        # ── RunResult → CommandResult ──────────────────────────
        data: dict[str, object] | None = None
        if result.run_args:
            interactive = run_request.tty or run_request.stdin_open
            data = {
                "run_args": list(result.run_args),
                "display_string": result.display_string,
                "container_name": result.container_name,
                "projection_hash": result.projection_hash,
                "mode": "interactive" if interactive else "captured",
                "artifact_cache_hits": list(result.artifact_cache_hits),
                "artifact_cache_misses": list(result.artifact_cache_misses),
            }
            # Preserve raw execution outcome for diagnosis.
            # Under --tty Docker may merge stderr into stdout;
            # capture both streams so callers never lose diagnostics.
            # Both streams are independently bounded so runaway
            # container output cannot blow up evidence or logs.
            if result.process_result is not None:
                data["exit_code"] = result.process_result.return_code
                if interactive:
                    # Streams were inherited — output already
                    # reached the terminal.  Omit captured data
                    # so JSON consumers are not confused by empty
                    # fields.
                    pass
                else:
                    for stream_name in ("stderr", "stdout"):
                        raw = getattr(result.process_result,
                                      stream_name)
                        truncated, was_cut = _truncate_diagnostic(
                            raw or "",
                        )
                        data[stream_name] = truncated
                        if was_cut:
                            data[f"{stream_name}_truncated"] = True

        return CommandResult(
            exit_kind=ExitKind(result.exit_kind.value),
            message=result.message,
            data=data,
        )

    # ── verify ─────────────────────────────────────────────────────────
    if command == "verify":
        from docker.versioning.verification import (
            VerifyBuildRequest,
            verify_build,
        )
        from docker.versioning.runtime_verification import (
            VerifyRuntimeRequest,
            verify_runtime,
        )
        from docker.launcher import ProcessRunner
        import json as _json

        try:
            inv_path = _resolve_inventory_path(request)
        except OSError as exc:
            return CommandResult(
                exit_kind=ExitKind.CONFIG,
                message=f"cannot resolve inventory path: {exc}",
            )

        c_args = _deep_freeze_command_args(request.command_args)
        scope = str(c_args.get("scope", "all"))
        json_out = bool(c_args.get("json", False)) or request.output == "json"
        collect_evidence_flag = bool(c_args.get("collect_evidence", False))

        # Resolve image tag
        image = c_args.get("image") or "pi-cli-pi:latest"

        # Build projection
        from pathlib import Path as _Path
        _project_root = request.constructor_project.root
        from docker.versioning.project_state import resolve_project_state
        from docker.versioning.inventory import load_project_configuration
        from docker.versioning.cache_storage import prepare_resolved_root, resolve_effective_root
        # One shared reviewed/local transaction for the whole verify command;
        # host-access, corporate-network, and cache consumers below receive
        # slices of the same local aggregate result.
        try:
            _inventory, _local_config = load_project_configuration(_Path(inv_path))
        except Exception as exc:
            return CommandResult(
                exit_kind=ExitKind.CONFIG,
                message=f"cannot load inventory {inv_path}: {exc}",
            )
        try:
            _cache_root = resolve_effective_root(
                getattr(getattr(_local_config, "cache", None), "dir", None),
                xdg_cache_home=_os_builtin.environ.get("XDG_CACHE_HOME"), home=_Path.home(),
            )
        except Exception as exc:
            return CommandResult(exit_kind=ExitKind.CONFIG,
                                 message=f"Failed to resolve constructor cache root: {exc}")


        results: Any = {}
        all_ok = True

        if scope in ("build", "all"):
            try:
                build_proj_path = resolve_project_state(
                    _project_root, cache_root=_cache_root, create=False,
                ).generated_root / "docker-constructor.build.effective.toml"
            except Exception as exc:
                return CommandResult(exit_kind=ExitKind.CONFIG,
                                     message=f"Failed to resolve constructor project state: {exc}")
            runner = _process_runner or ProcessRunner()
            b_result = verify_build(VerifyBuildRequest(
                image=image,
                effective_projection_path=build_proj_path,
                runner=runner,
            ))
            results["build"] = {
                "all_ok": b_result.all_ok,
                "observations": [
                    {"key": o.key, "ok": o.ok,
                     "expected": o.expected_value,
                     "observed": o.observed_value,
                     "command": list(o.command)}
                    for o in b_result.observations
                ],
                "errors": list(b_result.errors),
            }
            if not b_result.all_ok:
                all_ok = False

        if scope in ("runtime", "all"):
            _verify_ha, _verify_ha_addr, _verify_ha_err = \
                _resolve_verify_host_access(
                    str(inv_path),
                    inventory=_inventory,
                    local_config=_local_config,
                )
            if _verify_ha_err is not None:
                return CommandResult(
                    exit_kind=ExitKind.CONFIG,
                    message=_verify_ha_err,
                )
            (
                _verify_trust,
                _verify_proxy_url,
                _verify_proxy_no_proxy,
                _verify_net_err,
            ) = _resolve_verify_corporate_network(
                str(inv_path),
                request.constructor_project.root,
                local_config=_local_config,
            )
            if _verify_net_err is not None:
                return CommandResult(
                    exit_kind=ExitKind.CONFIG,
                    message=_verify_net_err,
                )
            # Resolve the operating container.
            container = c_args.get("container")
            if not container:
                # Auto-detect: docker ps -q --filter ancestor=<image>
                _runner = _process_runner
                if _runner is None:
                    _runner = ProcessRunner()
                try:
                    ps_result = _runner.run(
                        ["docker", "ps", "-q",
                         "--filter", f"ancestor={image}"]
                    )
                except OSError as exc:
                    results["runtime"] = {
                        "all_ok": False,
                        "checks": [],
                        "errors": [f"docker ps failed: {exc}"],
                    }
                    all_ok = False
                    container = None
                else:
                    if ps_result.return_code != 0:
                        results["runtime"] = {
                            "all_ok": False,
                            "checks": [],
                            "errors": [
                                f"docker ps failed (exit {ps_result.return_code}): "
                                f"{ps_result.stderr.strip()}"
                            ],
                        }
                        all_ok = False
                        container = None
                    else:
                        ids = [
                            lid for lid in ps_result.stdout.strip().split("\n")
                            if lid
                        ]
                        if not ids:
                            results["runtime"] = {
                                "all_ok": False,
                                "checks": [],
                                "errors": [
                                    f"no running container found for image "
                                    f"{image!r}; start a container first or "
                                    f"pass --container explicitly"
                                ],
                            }
                            all_ok = False
                            container = None
                        else:
                            container = ids[0]
            runtime_proj_path: Path | None = None
            if container:
                # ── resolve runner (shared by discovery + verify) ─
                _runner = _process_runner
                if _runner is None:
                    _runner = ProcessRunner()

                # ── resolve runtime projection ────────────────────
                explicit_projection = c_args.get("runtime_projection")
                try:
                    runtime_proj_path = _resolve_runtime_projection(
                        explicit_projection, _project_root, _cache_root
                    )
                except Exception as exc:
                    return CommandResult(
                        exit_kind=ExitKind.CONFIG,
                        message=f"Failed to resolve constructor project state: {exc}",
                    )
                if not explicit_projection:
                    runtime_proj_path = (
                        _discover_runtime_projection_from_container(
                            container, _runner,
                        )
                        or runtime_proj_path
                    )
                if runtime_proj_path is None:
                    results["runtime"] = {
                        "all_ok": False,
                        "checks": [],
                        "errors": [
                            "no runtime projection found; "
                            "pass --runtime-projection, ensure the running "
                            "container has its runtime projection mount, or "
                            "ensure external project-state runtime/ contains a "
                            ".toml file"
                        ],
                    }
                    all_ok = False
                    container = None

            if container:
                # ── resolve workspace paths ────────────────────────
                raw_workspaces = c_args.get("workspace_paths")
                if raw_workspaces:
                    _workspace_paths: tuple[Path, ...] = tuple(
                        Path(path) for path in raw_workspaces
                    )
                else:
                    _workspace_paths = _discover_workspace_paths_from_container(
                        container, _runner,
                    ) or ()
                if not _workspace_paths:
                    results["runtime"] = {
                        "all_ok": False,
                        "checks": [],
                        "errors": [
                            "no workspace paths available for runtime "
                            "verification; pass --workspace or ensure "
                            "the container has WORKSPACE_PATH_1..N set"
                        ],
                    }
                    all_ok = False
                    container = None

            if container and runtime_proj_path is not None:
                _container_pi_home: Path = Path("/home/dev/.pi")

                # Derive host-access expectations from inventory + local companion
                # Host-access pre-resolved above
                _host_access = _verify_ha
                _host_access_addr = _verify_ha_addr

                # _runner is already resolved above

                r_result = verify_runtime(VerifyRuntimeRequest(
                    container=container,
                    runtime_projection_path=runtime_proj_path,
                    workspace_paths=_workspace_paths,
                    container_pi_home=_container_pi_home,
                    runner=_runner,
                    host_access=_host_access,
                    host_access_address=_host_access_addr,
                    corporate_trust_enabled=_verify_trust,
                    proxy_url=_verify_proxy_url,
                    proxy_no_proxy=_verify_proxy_no_proxy,
                ))
                results["runtime"] = {
                    "all_ok": r_result.all_ok,
                    "checks": [
                        {"key": c.key, "ok": c.ok,
                         "detail": c.detail,
                         "command": list(c.command) if c.command else None,
                         "exit_code": c.exit_code,
                         "raw_stdout": c.raw_stdout,
                         "raw_stderr": c.raw_stderr}
                        for c in r_result.checks
                    ],
                    "errors": list(r_result.errors),
                }
                if not r_result.all_ok:
                    all_ok = False

        if collect_evidence_flag:
            from docker.versioning.evidence import (
                collect_evidence as _collect,
                write_static_evidence as _write_static,
                normalize_static_record as _norm_rec,
                _SystemClock as _SysClk,
                MAX_OUTPUT_BYTES as _MAX_BYTES,
                EvidenceCommand as _EvidenceCommand,
            )
            from datetime import datetime, timezone

            _image_name = image

            # Output directory — create a timestamped directory.
            _ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            raw_output_dir = c_args.get("output_dir")
            if isinstance(raw_output_dir, (str, _os_builtin.PathLike)):
                evidence_dir = _Path(raw_output_dir)
            elif bool(c_args.get("dry_run", False)):
                # Prospective only: do not initialise project state for dry-runs.
                evidence_dir = resolve_project_state(
                    _project_root, cache_root=_cache_root, create=False,
                ).evidence_root / _ts
            else:
                try:
                    evidence_dir = resolve_project_state(
                        _project_root, cache_root=prepare_resolved_root(_cache_root), create=True,
                    ).evidence_root / _ts
                except Exception as exc:
                    return CommandResult(exit_kind=ExitKind.CONFIG,
                                         message=f"Failed to initialize constructor project state: {exc}")

            _runner2 = _process_runner
            if _runner2 is None:
                _runner2 = ProcessRunner()

            _clk = _SysClk()

            # ── docker inspect (runs once, image metadata) ───────
            inspect_bundle = _collect(
                output_dir=evidence_dir,
                runner=_runner2,
                clock=_clk,
                image=_image_name,
                commands=(),
                dry_run=bool(c_args.get("dry_run", False)),
            )

            # ── evidence records from captured results ──────────
            captured_records: list[_EvidenceCommand] = list(
                inspect_bundle.commands
            )  # docker inspect goes first

            _now = _clk.now()
            _idx = len(captured_records)

            # Build: include failed observation commands.
            if "build" in results:
                for obs in results["build"]["observations"]:
                    if not obs["ok"] and obs.get("command"):
                        captured_records.append(_norm_rec(
                            argv=tuple(obs["command"]),
                            return_code=0,
                            stdout_raw=obs.get("observed") or "",
                            timestamp_epoch=_now,
                            output_dir=evidence_dir,
                            index=_idx,
                        ))
                        _idx += 1

            # Runtime: include captured diagnostic command results.
            if "runtime" in results:
                for chk in results["runtime"].get("checks", []):
                    if chk.get("command") and (
                        chk.get("raw_stdout") or chk.get("raw_stderr")
                        or chk.get("exit_code") is not None
                    ):
                        captured_records.append(_norm_rec(
                            argv=tuple(chk["command"]),
                            return_code=chk.get("exit_code"),
                            stdout_raw=chk.get("raw_stdout") or "",
                            stderr_raw=chk.get("raw_stderr") or "",
                            timestamp_epoch=_now,
                            output_dir=evidence_dir,
                            index=_idx,
                        ))
                        _idx += 1

            # ── assemble the bundle ─────────────────────────────
            bundle = _write_static(
                output_dir=evidence_dir,
                image=_image_name,
                records=captured_records,
                dry_run=bool(c_args.get("dry_run", False)),
            )
            results["collect_evidence"] = {
                "all_ok": True,
                "output_dir": str(bundle.output_dir),
                "index_path": str(bundle.index_path),
                "command_count": len(bundle.commands),
                "note_count": len(bundle.notes),
                "dry_run": bundle.dry_run,
            }

        if json_out:
            return CommandResult(
                exit_kind=ExitKind.SUCCESS if all_ok else ExitKind.OPERATIONAL,
                message=None,
                data={"verification": results},
            )

        # Text output
        lines: list[str] = []
        if "build" in results:
            b = results["build"]
            lines.append(f"Build verification: {'PASS' if b['all_ok'] else 'FAIL'}")
            for obs in b["observations"]:  # type: ignore[union-attr]
                ok = "✓" if obs["ok"] else "✗"
                lines.append(f"  {ok} {obs['key']}: {obs['observed']}")
            for err in b.get("errors", []):
                lines.append(f"  ERROR: {err}")
        if "runtime" in results:
            r = results["runtime"]
            lines.append(f"Runtime verification: {'PASS' if r['all_ok'] else 'FAIL'}")
            for chk in r.get("checks", []):
                ok = "✓" if chk["ok"] else "✗"
                lines.append(f"  {ok} {chk['key']}: {chk.get('detail', '')}")
            for err in r.get("errors", []):
                lines.append(f"  ERROR: {err}")

        return CommandResult(
            exit_kind=ExitKind.SUCCESS if all_ok else ExitKind.OPERATIONAL,
            message="\n".join(lines) if lines else "verification complete",
        )

    # ── read-only commands ────────────────────────────────────────────
    try:
        inv_path = _resolve_inventory_path(request)
    except OSError as exc:
        return CommandResult(
            exit_kind=ExitKind.CONFIG,
            message=f"cannot resolve inventory path: {exc}",
        )

    from docker.versioning.readonly_service import dispatch
    return dispatch(
        inv_path, command, command_args=request.command_args,
        progress=_progress_renderer,
    )


# ═══════════════════════════════════════════════════════════════════════
# User prompting (confirmation gate — Stage 9.4)
# ═══════════════════════════════════════════════════════════════════════


def _stdin_prompt(prompt_text: str) -> bool:
    """Prompt the user for a yes/no confirmation on stderr.

    Returns ``True`` for 'y' / 'yes', ``False`` for anything else
    (including EOF or a non-interactive stdin).
    """
    if not _stdin_is_interactive():
        return False
    try:
        response = input(f"{prompt_text} [y/N]: ")
    except EOFError:
        return False
    return response.strip().lower() in ("y", "yes")


def _stdin_is_interactive() -> bool:
    """Check if stdin is attached to a terminal."""
    try:
        stdin = sys.__stdin__
        return stdin is not None and stdin.isatty()
    except Exception:
        return False


def _deep_freeze_command_args(
    cmd_args: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Freeze nested lists/dicts in command_args so the downstream
    dispatch receives only immutable containers (matching the
    ``CommandRequest`` contract).
    """
    frozen: dict[str, Any] = {}
    for key, value in cmd_args.items():
        if isinstance(value, dict):
            frozen[key] = MappingProxyType({
                k: tuple(v) if isinstance(v, list) else v
                for k, v in value.items()
            })
        elif isinstance(value, list):
            frozen[key] = tuple(value)
        else:
            frozen[key] = value
    return MappingProxyType(frozen)


# ═══════════════════════════════════════════════════════════════════════
# Generic rendering (consumes CommandResult, not domain objects)
# ═══════════════════════════════════════════════════════════════════════

_RESET = "\x1b[0m"
_BOLD = "\x1b[1m"
_RED = "\x1b[31m"
_YELLOW = "\x1b[33m"
_GREEN = "\x1b[32m"
_GRAY = "\x1b[90m"

# Visual replacement-fragment comment header, e.g. ``# --- base.node ---``.
_REPLACEMENT_HEADER_RE = re.compile(r"# --- \S+ ---")


def _use_colour(color: str, *, stream_is_tty: bool) -> bool:
    if color == "always":
        return True
    if color == "never":
        return False
    return stream_is_tty  # "auto"


def _coloured(text: str, code: str, color: str, *,
              stream_is_tty: bool) -> str:
    if not _use_colour(color, stream_is_tty=stream_is_tty):
        return text
    return f"{code}{text}{_RESET}"


def _decorate_replacement_headers(text: str, *, enabled: bool) -> str:
    """Wrap replacement-fragment visual comment headers in SGR 90.

    Only whole lines matching the exact ``# --- <display path> ---``
    header format are decorated; each decorated header is immediately
    followed by a reset so no neighbouring text inherits the style.  The
    manual-replacement section label, TOML table headers, and TOML body
    are left untouched.  When *enabled* is false, *text* is returned
    unchanged.
    """
    if not enabled:
        return text
    return "\n".join(
        f"{_GRAY}{line}{_RESET}"
        if _REPLACEMENT_HEADER_RE.fullmatch(line)
        else line
        for line in text.split("\n")
    )


def _abbreviate_identifier(value: str) -> str:
    """Abbreviate long hex identifiers for text display.

    Only genuinely long hex strings (≥ 20 chars, e.g. revision digests,
    checksums) are shortened to their first five characters followed by
    ``…``.  Short hex strings such as ``12345`` and version-like values
    are left unchanged.

    - ``sha256:abc123def456...`` → ``sha256:abc12…``
    - ``deadbeefcafebabe0123456789abcdef012345…`` → ``deadb…``
    - ``12345`` → ``12345`` (too short)
    - everything else → unchanged
    """
    import re
    # sha256: prefix — abbreviate only when the entire payload is 20+ hex chars
    m = re.fullmatch(r"^(sha256:)([0-9a-fA-F]{20,})", value)
    if m:
        return m.group(1) + m.group(2)[:5] + "..."
    # Plain hex string of 20+ chars (revisions, digests, checksums)
    if re.fullmatch(r"[0-9a-fA-F]{20,}", value):
        return value[:5] + "..."
    return value


def _parse_published_at(published_at: object) -> Optional[str]:
    """Return the RFC 3339 date-time portion of an authoritative UTC value.

    Only ``YYYY-MM-DDTHH:MM:SS[.fraction](Z|+00:00)`` is authoritative.
    Absent, non-string, non-UTC, or unparseable values return ``None``.
    """
    import re
    from datetime import datetime

    if not isinstance(published_at, str) or not published_at:
        return None
    m = re.fullmatch(
        r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?)"
        r"(Z|(?:\+00:00))",
        published_at,
    )
    if not m:
        return None
    try:
        datetime.fromisoformat(m.group(1))
    except ValueError:
        return None
    return m.group(1)


def _format_published_date(published_at: object) -> str:
    """Compact publication value: ``YYYY-MM-DD`` or ``-``."""
    parsed = _parse_published_at(published_at)
    if parsed is None:
        return "-"
    return parsed[:10]


def _format_published_at(published_at: object) -> str:
    """Detailed publication value: ``YYYY-MM-DD HH:MM:SS GMT`` or ``-``."""
    from datetime import datetime

    parsed = _parse_published_at(published_at)
    if parsed is None:
        return "-"
    return datetime.fromisoformat(parsed).strftime("%Y-%m-%d %H:%M:%S GMT")


def _compact_target(path: str) -> str:
    """Shorten a target path for compact text display.

    Only a leading ``build.stages.`` prefix is removed; any other path
    is returned unchanged to avoid ambiguous general-purpose shortening.
    """
    prefix = "build.stages."
    if path.startswith(prefix):
        return path[len(prefix):]
    return path


class _HostEventRenderer(TerminalHostRenderer):
    """Facade-owned presentation renderer for host build events.

    Durable lines keep the historical ``Pi <phase>: <state>`` and
    ``Pi <phase> [<stream>]: <text>`` shape for direct callers, while the
    replaceable interactive status/slot methods are inherited from the shared
    terminal renderer used by the single presentation worker.
    """

    def __init__(self, stream: Any = None, *, output_policy: Any = None) -> None:
        super().__init__(stream)
        self._output_policy = output_policy

    @property
    def output_policy(self) -> Any:
        """Immutable presentation policy resolved by the shared transaction."""
        return self._output_policy

    def __call__(self, event: Any) -> None:
        if isinstance(event, HostPhaseEvent):
            text = f"Pi {event.phase.value.replace('_', ' ')}: {event.state.value}"
        elif isinstance(event, HostDiagnosticEvent):
            text = f"Pi {event.phase.value} [{event.stream.value}]: {event.text}"
        else:
            return
        self._stream.write(text if text.endswith("\n") else f"{text}\n")
        self._stream.flush()


def _make_presentation_session(
    policy: Any, *, text_output: bool, stderr_is_tty: bool
) -> HostPresentationSession | None:
    """Build one facade presentation session, or ``None`` when none is live.

    JSON and default noninteractive text create no live sink; explicit
    noninteractive ``lines`` and every interactive text mode do.  The policy's
    mode and hostname flag are retained only here, never on the domain request.
    """
    plan = select_presentation(
        policy.host_heartbeat,
        text_output=text_output,
        stderr_is_tty=stderr_is_tty,
        show_network_hosts=policy.show_network_hosts,
    )
    if not plan.live_sink:
        return None
    renderer = _HostEventRenderer(output_policy=policy)
    return HostPresentationSession(renderer, plan)


class _ProgressRenderer:
    """Facade-owned transient stderr renderer for interactive discovery.

    Writes one replaceable stderr line per progress event::

        Checking updates [N/T] TARGET (PROVIDER)…

    ``TARGET`` is the compact target path (leading ``build.stages.``
    prefix removed).  Each write returns to column zero and erases the
    previous line so only the latest event remains visible; no newline
    is ever emitted.  :meth:`clear` erases the line idempotently before
    any subsequent output.
    """

    _ERASE_LINE = "\r\x1b[K"

    def __init__(self, stream: Any = None) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._active = False

    def __call__(self, event: Any) -> None:
        line = (
            f"Checking updates [{event.index}/{event.total}] "
            f"{_compact_target(event.path)} ({event.provider})…"
        )
        self._stream.write(f"{self._ERASE_LINE}{line}")
        self._stream.flush()
        self._active = True

    def clear(self) -> None:
        """Erase the transient line (idempotent)."""
        if not self._active:
            return
        self._stream.write(self._ERASE_LINE)
        self._stream.flush()
        self._active = False


def _normalize_update_row(r: dict[str, object]) -> dict[str, str]:
    """Normalize one serialized update result for text display.

    Returns display-ready strings for both the compact and detailed
    renderers.  Shortening and abbreviation are presentation concerns
    and never touch serialized result data.
    """
    def _cell(key: str) -> str:
        v = r.get(key)
        if v is None:
            return "-"
        return str(v)

    current_raw = _cell("current")
    candidate_raw = _cell("candidate")
    # Abbreviate hex identifiers in text display only;
    # compare raw original values for the equality check.
    current = _abbreviate_identifier(current_raw)
    candidate = _abbreviate_identifier(candidate_raw)
    # Candidate equal to current (raw) → "-"
    if candidate_raw != "-" and candidate_raw == current_raw:
        candidate = "-"

    path = _cell("path")
    return {
        "path": path,
        "target": _compact_target(path),
        "provider": _cell("provider"),
        "current": current,
        "candidate": candidate,
        "current_next": f"{current} -> {candidate}",
        "status": _cell("status"),
        "kind": _cell("kind"),
        "applicable": "yes" if r.get("applicable") else "no",
        "published": _format_published_at(r.get("published_at")),
        "published_date": _format_published_date(r.get("published_at")),
        "detail": _cell("reason"),
    }


def _render_update_summary(results: list[object]) -> str:
    """Build the deterministic status-summary line for update reports."""
    counts: dict[str, int] = {}
    applicable_outdated = 0
    for r in results:
        if not isinstance(r, dict):
            continue
        status = r.get("status", "unknown")
        counts[status] = counts.get(status, 0) + 1
        if status == "outdated" and r.get("applicable"):
            applicable_outdated += 1

    status_order = (
        "current", "outdated", "skipped", "unavailable", "incomplete",
    )
    parts: list[str] = []
    for s in status_order:
        if s in counts:
            if s == "outdated":
                parts.append(
                    f"{counts[s]} outdated ({applicable_outdated} applicable)"
                )
            else:
                parts.append(f"{counts[s]} {s}")
    if parts:
        return "Updates: " + ", ".join(parts)
    return "No update targets found."


def _render_update_table(
    col_keys: tuple[str, ...],
    col_labels: tuple[str, ...],
    rows: list[dict[str, str]],
) -> str:
    """Render an aligned ASCII table with fixed columns and 2-space gaps."""
    def _col_width(idx: int, label: str) -> int:
        w = len(label)
        for row in rows:
            val = row[col_keys[idx]]
            if len(val) > w:
                w = len(val)
        return w

    widths = tuple(_col_width(i, label) for i, label in enumerate(col_labels))
    header = "  ".join(
        label.ljust(widths[i]) for i, label in enumerate(col_labels)
    )
    sep = "-" * len(header)
    lines = [sep, header, sep]
    for row in rows:
        lines.append("  ".join(
            row[key].ljust(widths[i]) for i, key in enumerate(col_keys)
        ))
    lines.append(sep)
    return "\n".join(lines)


def _render_suggestions_section(data: dict[str, object]) -> list[str]:
    """Build the optional review-only replacement block section."""
    if not bool(data.get("suggest", False)):
        return []
    fragments = data.get("replacement_fragments")
    if isinstance(fragments, str) and fragments.strip():
        return [
            "",
            "─── manual replacement blocks "
            "(review-only — not applied automatically) ───",
            fragments.rstrip("\n"),
        ]
    return [
        "",
        "─── replacement blocks "
        "───────────────────────────────────────",
        "No reviewable replacement blocks available.",
    ]


def _render_check_updates_text(data: object) -> str:
    """Render check-updates data as a compact human-readable report.

    Produces a deterministic summary line with applicable-outdated
    counts, a per-dependency table with fixed columns
    ``TARGET | PROVIDER | CURR -> NEXT | STATUS | PUBLISHED``, an
    optional ``Details:`` section for non-empty reasons, and an
    optional review-only manual replacement TOML block.
    """
    if not isinstance(data, dict):
        return str(data)

    results = data.get("results")
    if not isinstance(results, list):
        return str(data)

    rows: list[dict[str, str]] = []
    details: list[tuple[str, str]] = []
    for r in results:
        if not isinstance(r, dict):
            continue
        row = _normalize_update_row(r)
        rows.append(row)
        reason = r.get("reason")
        if isinstance(reason, str) and reason:
            details.append((row["target"], reason))

    col_keys = ("target", "provider", "current_next", "status",
                "published_date")
    col_labels = ("TARGET", "PROVIDER", "CURR -> NEXT", "STATUS",
                  "PUBLISHED")

    out = [
        _render_update_summary(results),
        "",
        _render_update_table(col_keys, col_labels, rows),
    ]

    # ── details (non-empty reasons, keyed by compact target) ─────
    if details:
        out.append("")
        out.append("Details:")
        for target, reason in details:
            out.append(f"  {target}: {reason}")

    out.extend(_render_suggestions_section(data))
    return "\n".join(out)


def _render_check_updates_details_text(data: object) -> str:
    """Render check-updates data as the full diagnostic report.

    Produces the established nine-column table
    ``PATH | PROVIDER | CURRENT | CANDIDATE | STATUS | KIND |
    APPLICABLE | PUBLISHED | DETAIL`` with full target paths and
    ``YYYY-MM-DD HH:MM:SS GMT`` publication values, followed by the
    shared review-only replacement block section when requested.
    """
    if not isinstance(data, dict):
        return str(data)

    results = data.get("results")
    if not isinstance(results, list):
        return str(data)

    rows = [
        _normalize_update_row(r) for r in results if isinstance(r, dict)
    ]
    col_keys = ("path", "provider", "current", "candidate", "status",
                "kind", "applicable", "published", "detail")
    col_labels = ("PATH", "PROVIDER", "CURRENT", "CANDIDATE", "STATUS",
                  "KIND", "APPLICABLE", "PUBLISHED", "DETAIL")

    out = [
        _render_update_summary(results),
        "",
        _render_update_table(col_keys, col_labels, rows),
    ]
    out.extend(_render_suggestions_section(data))
    return "\n".join(out)


def _render_data_text(data: object) -> str:
    """Render CommandResult data for text-mode display.

    Dicts with a ``display_string`` key (build dry-run, doctor
    summaries, run dry-run) render that string verbatim, followed
    by artifact cache hit/miss counts when present.

    Run-failure diagnostics carry a ``mode`` field:
    ``"interactive"`` means output was streamed to the terminal
    (nothing to re-render); ``"captured"`` surfaces ``stdout``
    and/or ``stderr`` with labels.
    """
    if isinstance(data, dict) and data.get("display_string"):
        ds = data["display_string"]
        parts: list[str] = []
        if ds is not None and str(ds):
            parts.append(str(ds))
        hits: list[str] = (
            list(data.get("artifact_cache_hits") or ())
            if isinstance(data, dict) else []
        )
        misses: list[str] = (
            list(data.get("artifact_cache_misses") or ())
            if isinstance(data, dict) else []
        )
        if hits:
            parts.append(
                f"Cache hits ({len(hits)}): "
                + ", ".join(hits),
            )
        if misses:
            parts.append(
                f"Planned downloads ({len(misses)}): "
                + ", ".join(misses),
            )
        return "\n       ".join(parts) if parts else ""
    # Run-failure diagnostics: only render captured streams.
    if isinstance(data, dict) and data.get("mode") == "interactive":
        # Output was streamed to the terminal — nothing to
        # re-render.
        return ""
    if isinstance(data, dict) and data.get("mode") == "captured":
        lines: list[str] = []
        out = data.get("stdout")
        if out and str(out).strip():
            lines.append(f"stdout: {str(out).strip()}")
        err = data.get("stderr")
        if err and str(err).strip():
            lines.append(f"stderr: {str(err).strip()}")
        return "\n       ".join(lines) if lines else ""
    return str(data)


def _render(
    command: str,
    result: CommandResult,
    *,
    fmt: str,
    color: str,
    verbose: bool,
    details: bool = False,
    stdout_is_tty: bool,
    stderr_is_tty: bool,
) -> tuple[str, str]:
    """Render a CommandResult to (stdout_text, stderr_text).

    Channel rules:
    * JSON mode — everything on stdout, machine-readable.
    * Text mode:
      - SUCCESS / POLICY → stdout (semantic output).
      - CLI / CONFIG / OPERATIONAL → stderr (diagnostics).
      - When both *data* and *message* are present, both are rendered.
      - *debug* detail is only appended to stderr when ``verbose`` is
        true, regardless of exit kind.
    """
    _ERROR_KINDS = {ExitKind.CLI, ExitKind.CONFIG, ExitKind.OPERATIONAL}
    is_error = result.exit_kind in _ERROR_KINDS

    out_lines: list[str] = []
    err_lines: list[str] = []

    if fmt == "json":
        payload: dict[str, Any] = {
            "command": command,
            "status": result.exit_kind.value,
        }
        if result.data is not None:
            data = result.data
            # Text-only replacement fragments never leak into machine JSON.
            if command == "check-updates" and isinstance(data, dict):
                data = {
                    k: v for k, v in data.items()
                    if k != "replacement_fragments"
                }
            payload["data"] = data
        if result.message is not None:
            payload["message"] = result.message
        out_lines.append(_json.dumps(payload, indent=2, sort_keys=True,
                                     default=str))
    else:
        # ── text mode ────────────────────────────────────────────
        level = result.exit_kind.value.upper()
        code_map = {
            ExitKind.SUCCESS: _GREEN,
            ExitKind.POLICY: _YELLOW,
            ExitKind.CLI: _RED,
            ExitKind.CONFIG: _RED,
            ExitKind.OPERATIONAL: _RED,
        }
        code = code_map.get(result.exit_kind, _RESET)
        # Colour is resolved against the *target* stream's tty state
        tty = stderr_is_tty if is_error else stdout_is_tty
        prefix = _coloured(f"[{level}]", code, color, stream_is_tty=tty)

        target: list[str] = err_lines if is_error else out_lines

        # Render message and data — both when present
        if result.message and not result.message_owned_by_presentation:
            target.append(f"{prefix} {result.message}")
        if result.data is not None:
            if command == "check-updates":
                rendered = (
                    _render_check_updates_details_text(result.data)
                    if details
                    else _render_check_updates_text(result.data)
                )
            else:
                rendered = _render_data_text(result.data)
            if command == "check-updates":
                # Replacement-fragment headers are terminal-only visual
                # boundaries: decorate them according to the target
                # stream's colour decision (stdout for success/policy,
                # stderr for errors), never the plain fragment text.
                rendered = _decorate_replacement_headers(
                    rendered,
                    enabled=_use_colour(color, stream_is_tty=tty),
                )
            if rendered:
                if result.message:
                    target.append(f"       data: {rendered}")
                else:
                    target.append(f"{prefix} {rendered}")
        if not result.message and result.data is None:
            target.append(prefix)

    # Debug detail always to stderr, regardless of exit kind
    if verbose and result.debug:
        err_lines.append(f"[debug] {result.debug}")

    return "\n".join(out_lines), "\n".join(err_lines)


# ═══════════════════════════════════════════════════════════════════════
# Parser construction
# ═══════════════════════════════════════════════════════════════════════

def _add_global_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--project-directory",
        default=None,
        metavar="DIR",
        help="Constructor project directory (default: current directory)",
    )
    parser.add_argument(
        "--output",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        default=False,
        help="Increase output verbosity",
    )
    parser.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="Colour output policy (default: auto)",
    )


def _parse_overrides(raw: Sequence[str]) -> dict[str, str]:
    """Parse ``PATH=VALUE`` override tokens.

    Rejects tokens without ``=``, leading/trailing spaces around ``=``,
    empty paths, or duplicate paths.
    """
    result: dict[str, str] = {}
    for token in raw:
        if "=" not in token:
            raise ValueError(
                f"invalid override {token!r}: expected PATH=VALUE"
            )
        path, value = token.split("=", 1)
        if path != path.strip() or value != value.strip():
            raise ValueError(
                f"invalid override {token!r}: no spaces allowed around '='"
            )
        if not path:
            raise ValueError(f"invalid override {token!r}: empty path")
        if path in result:
            raise ValueError(
                f"duplicate override path {path!r}: each path may only be "
                f"specified once"
            )
        result[path] = value
    return result


def _dispatch_command(
    args: argparse.Namespace,
    *,
    dispatcher: CommandDispatcher,
) -> CommandResult:
    """Build a CommandRequest from parsed args and invoke the dispatcher."""
    # Collect command-owned arguments into command_args
    cmd_args: dict[str, object] = {}
    for attr in vars(args):
        if attr in ("command", "func", "project_directory", "output", "verbose",
                     "color", "details"):
            continue
        value = getattr(args, attr)
        # Normalise overrides: raw PATH=VALUE list → parsed dict
        if attr == "overrides" and isinstance(value, list):
            try:
                value = _parse_overrides(value)
            except ValueError as exc:
                return CommandResult(
                    exit_kind=ExitKind.CLI,
                    message=str(exc),
                )
        cmd_args[attr] = value

    # ---- doctor-owned CLI validation --------------------------------
    if args.command == "doctor":
        timeout = cmd_args.get("probe_timeout")
        if (
            timeout is not None
            and (
                not isinstance(timeout, int)
                or isinstance(timeout, bool)
                or timeout < 1
                or timeout > 300
            )
        ):
            return CommandResult(
                exit_kind=ExitKind.CLI,
                message=(
                    f"invalid --probe-timeout {timeout}: "
                    f"must be 1–300"
                ),
            )
        image = cmd_args.get("probe_image")
        if image == "":
            return CommandResult(
                exit_kind=ExitKind.CLI,
                message="--probe-image must not be empty",
            )

    constructor_project = resolve_constructor_project(args.project_directory)
    request = CommandRequest(
        command=args.command,
        constructor_project=constructor_project,
        output=args.output,
        verbose=args.verbose,
        color=args.color,
        command_args=cmd_args,
    )
    execute = dispatcher.execute if hasattr(dispatcher, "execute") else dispatcher
    return execute(args.command, request)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="docker/docker-constructor.py",
        description="Pi Docker constructor — build, run, and inspect"
                    " the Pi container image.",
    )
    _add_global_args(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    # --- validate ---
    p_val = sub.add_parser("validate", help="Validate docker-constructor.toml")
    p_val.add_argument(
        "--scope",
        choices=("build", "runtime", "all"),
        default="all",
        help="Which sections to validate (default: all)",
    )
    p_val.set_defaults(func=_dispatch_command)

    # --- build (parser only; handler in Stage 9) ---
    p_build = sub.add_parser("build", help="Build the Pi container image")
    p_build.add_argument(
        "--platform",
        default="linux-amd64",
        help="Target platform (default: linux-amd64)",
    )
    p_build.add_argument(
        "--tag",
        default=None,
        help="Override the image tag",
    )
    p_build.add_argument(
        "--override",
        action="append",
        default=[],
        dest="overrides",
        metavar="PATH=VALUE",
        help="Override a version value (repeatable)",
    )
    p_build.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print the build vector without executing",
    )
    p_build.add_argument(
        "--yes", "-y",
        action="store_true",
        default=False,
        help="Skip confirmation prompts",
    )
    p_build.add_argument(
        "--cache",
        action="store_true",
        default=True,
        dest="cache",
        help="Enable Docker build cache (default)",
    )
    p_build.add_argument(
        "--no-cache",
        action="store_false",
        dest="cache",
        help="Disable Docker build cache",
    )
    p_build.add_argument(
        "--pull",
        action="store_true",
        default=False,
        help="Force pull base images",
    )
    p_build.add_argument(
        "--no-pull",
        action="store_false",
        dest="pull",
        help="Do not pull base images (default)",
    )
    p_build.add_argument(
        "--progress",
        default="auto",
        choices=("auto", "plain", "tty"),
        help="Progress output style (default: auto)",
    )
    p_build.add_argument(
        "--uid",
        type=int,
        default=None,
        help="Host UID for DEV_UID build arg",
    )
    p_build.add_argument(
        "--gid",
        type=int,
        default=None,
        help="Host GID for DEV_GID build arg",
    )
    p_build.set_defaults(func=_dispatch_command)

    # --- run (parser only; handler in Stage 9) ---
    p_run = sub.add_parser("run", help="Launch a Pi container session")
    p_run.add_argument(
        "-w", "--workspace",
        default=None,
        dest="workspace",
        metavar="PATH",
        help="Primary workspace directory",
    )
    p_run.add_argument(
        "--extra-workspace",
        action="append",
        default=[],
        dest="extra_workspaces",
        metavar="PATH",
        help="Extra workspace to mount (repeatable)",
    )
    p_run.add_argument(
        "--tui",
        action="store_true",
        default=False,
        help="Select workspaces interactively",
    )
    p_run.add_argument(
        "--image",
        default=None,
        dest="image",
        help="Image to run (default: pi-cli-pi:latest)",
    )
    p_run.add_argument(
        "--tty",
        action="store_true",
        default=None,
        dest="tty",
        help="Allocate a pseudo-TTY (default)",
    )
    p_run.add_argument(
        "--no-tty",
        action="store_false",
        dest="tty",
        help="Disable pseudo-TTY",
    )
    p_run.add_argument(
        "--no-interactive",
        action="store_false",
        dest="stdin_open",
        default=None,
        help="Do not keep STDIN open",
    )
    p_run.add_argument(
        "--workspace-root",
        default=None,
        dest="workspace_root",
        metavar="PATH",
        help="Root directory for filesystem tree view "
             "(overrides project-local .env WORKSPACE_ROOT)",
    )
    p_run.add_argument(
        "--chown-on-start",
        default=None,
        dest="chown_on_start",
        help="Value for CHOWN_WORK_ON_START env var",
    )
    p_run.add_argument(
        "--override",
        action="append",
        default=[],
        dest="overrides",
        metavar="KEY=VALUE",
        help="Runtime override (repeatable, e.g. "
             "runtime.pi-extensions.pi-read.version=0.3.0)",
    )
    p_run.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print the run vector without executing",
    )
    p_run.add_argument(
        "passthrough",
        nargs="*",
        default=[],
        metavar="command",
        help="Command and arguments to pass to the container entrypoint",
    )
    p_run.set_defaults(func=_dispatch_command)

    # --- doctor (parser only; handler in Stage 9) ---
    p_doc = sub.add_parser(
        "doctor", help="Diagnose host → container connectivity",
    )
    p_doc.add_argument(
        "--apply-rootless-override",
        action="store_true",
        dest="apply_override",
        default=False,
        help="Repair host-gateway override for rootless Docker",
    )
    p_doc.add_argument(
        "--yes", "-y",
        action="store_true",
        default=False,
        help="Apply rootless override without prompting",
    )
    p_doc.add_argument(
        "--probe-image",
        default=None,
        dest="probe_image",
        help="Image used for gateway probes",
    )
    p_doc.add_argument(
        "--probe-timeout",
        type=int,
        default=None,
        dest="probe_timeout",
        help="Timeout in seconds for probe containers (1-300)",
    )
    p_doc.set_defaults(func=_dispatch_command)

    # --- verify ---
    p_ver = sub.add_parser(
        "verify", help="Verify image and runtime integrity",
    )
    p_ver.add_argument(
        "--scope",
        choices=("build", "runtime", "all"),
        default="all",
        help="Which scopes to verify (default: all)",
    )
    p_ver.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output verification results as JSON",
    )
    p_ver.add_argument(
        "--collect-evidence",
        action="store_true",
        default=False,
        dest="collect_evidence",
        help="Collect evidence bundle with bounded output capture",
    )
    p_ver.add_argument(
        "--image",
        default=None,
        help="Image to verify (default: pi-cli-pi:latest)",
    )
    p_ver.add_argument(
        "--container",
        default=None,
        help="Running container name/ID for runtime checks (default: auto-detect from docker ps -q --filter ancestor=<image>)",
    )
    p_ver.add_argument(
        "--runtime-projection",
        default=None,
        dest="runtime_projection",
        help="Path to host-side runtime projection TOML for runtime checks "
             "(default: most recent projection in the selected project's "
             "external project-state namespace)",
    )
    p_ver.add_argument(
        "--workspace",
        action="append",
        default=None,
        dest="workspace_paths",
        help="Workspace path inside the container (repeatable). "
             "Default: auto-discovered from the container runtime contract.",
    )
    p_ver.add_argument(
        "--output-dir",
        default=None,
        help="Directory for evidence bundle output (default: timestamped "
             "directory in the selected project's external evidence root)",
    )
    p_ver.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        dest="dry_run",
        help="Record command metadata without executing",
    )
    p_ver.set_defaults(func=_dispatch_command)

    # --- show ---
    p_show = sub.add_parser(
        "show", help="Display reviewed inventory contents",
    )
    p_show.add_argument(
        "--scope",
        choices=("build", "runtime", "all"),
        default="all",
        help="Which sections to display (default: all)",
    )
    p_show.add_argument(
        "--effective",
        action="store_true",
        default=False,
        help="Display the effective projection instead of the"
             " reviewed source",
    )
    p_show.add_argument(
        "--override",
        action="append",
        default=[],
        dest="overrides",
        metavar="PATH=VALUE",
        help="Override a version value (repeatable, requires --effective)",
    )
    p_show.set_defaults(func=_dispatch_command)

    # --- check-updates ---
    p_upd = sub.add_parser(
        "check-updates", help="Check configured providers for updates",
    )
    p_upd.add_argument(
        "--scope",
        choices=("build", "runtime", "all"),
        default="all",
        help="Which scopes to check (default: all)",
    )
    p_upd.add_argument(
        "--only",
        action="append",
        default=[],
        dest="only_filter",
        metavar="FILTER",
        help="Provider name or inventory path (repeatable)",
    )
    p_upd.add_argument(
        "--include-prerelease",
        action="store_true",
        default=False,
        help="Include prerelease versions in queries",
    )
    p_upd.add_argument(
        "--strict",
        action="store_true",
        default=False,
        help="Treat provider failures as errors",
    )
    p_upd.add_argument(
        "--fail-on-outdated",
        action="store_true",
        default=False,
        help="Exit non-zero when any applicable update is found",
    )
    p_upd.add_argument(
        "--suggest",
        action="store_true",
        default=False,
        help="Include non-mutating TOML suggestions in output",
    )
    p_upd.add_argument(
        "--details",
        action="store_true",
        default=False,
        help="Render the full diagnostic report for check-updates",
    )
    p_upd.add_argument(
        "--no-cache",
        action="store_true",
        default=False,
        help="Disable HTTP cache",
    )
    p_upd.set_defaults(func=_dispatch_command)

    return parser


# ═══════════════════════════════════════════════════════════════════════
# Main entry point
# ═══════════════════════════════════════════════════════════════════════

_IsAtty = Callable[[], bool]


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    dispatcher: Optional[CommandDispatcher | Callable[[str, CommandRequest], CommandResult]] = None,
    stdout_isatty: Optional[_IsAtty] = None,
    stderr_isatty: Optional[_IsAtty] = None,
    _prompt_user: Callable[[str], bool] | None = None,
    _process_runner: Any = None,
    _container_inspector: Any = None,
    _run_executor: Any = None,
    _create_projection: Any = None,
    _workspace_selector: Any = None,
) -> int:
    """Parse arguments, dispatch, render, and map to exit code.

    Parameters
    ----------
    argv:
        Argument list (defaults to ``sys.argv[1:]``).
    dispatcher:
        Domain boundary — a callable ``(command, request) -> CommandResult``
        or an object with an ``execute`` method. The default dispatcher
        delegates to the internal read-only service.
    stdout_isatty:
        Terminal-detection override for stdout (for colour logic).
        Defaults to ``sys.stdout.isatty``.
    stderr_isatty:
        Terminal-detection override for stderr (for colour logic and
        interactive check-updates progress gating).
        Defaults to ``sys.stderr.isatty``.
    _prompt_user:
        Confirmation prompt override.  Defaults to ``_stdin_prompt``
        which reads from ``stdin``.  Tests may inject a mock.
    _process_runner:
        Override for :class:`~docker.launcher.ProcessRunner` used
        by the ``run`` command.  Tests may inject a fake.
    _container_inspector:
        Override for :class:`~docker.launcher.DockerContainerInspector`.
    _run_executor:
        Override for :class:`~docker.launcher.DockerRunExecutor`.
    _create_projection:
        Override for the projection-factory callable.
    """
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code is not None and exc.code != 0:
            return _EXIT_CODES[ExitKind.CLI]
        return _EXIT_CODES[ExitKind.SUCCESS]

    _stdout_tty = (stdout_isatty() if stdout_isatty is not None
                   else getattr(sys.stdout, "isatty", lambda: False)())
    _stderr_tty = (stderr_isatty() if stderr_isatty is not None
                   else getattr(sys.stderr, "isatty", lambda: False)())

    # Facade-owned interactive progress renderer: installed only for
    # text-output check-updates whose stderr is a TTY.  JSON output and
    # non-TTY stderr install no renderer and therefore emit no progress.
    progress_renderer: _ProgressRenderer | None = None
    host_presentation_factory: Callable[[Any], Any] | None = None
    if (
        args.command == "build"
        and args.output == "text"
        and not bool(getattr(args, "dry_run", False))
    ):
        host_presentation_factory = lambda policy: _make_presentation_session(
            policy, text_output=True, stderr_is_tty=_stderr_tty
        )
    if (
        args.command == "check-updates"
        and args.output == "text"
        and _stderr_tty
    ):
        progress_renderer = _ProgressRenderer()

    if dispatcher is None:
        prompt = _stdin_prompt if _prompt_user is None else _prompt_user
        disp: Callable[[str, CommandRequest], CommandResult] = (
            lambda c, r: _real_dispatcher(
                c, r,
                _prompt_user=prompt,
                _process_runner=_process_runner,
                _container_inspector=_container_inspector,
                _run_executor=_run_executor,
                _create_projection=_create_projection,
                _workspace_selector=_workspace_selector,
                _progress_renderer=progress_renderer,
                _host_presentation_factory=host_presentation_factory,
            )
        )
    else:
        disp = dispatcher

    result: CommandResult
    try:
        try:
            result = args.func(args, dispatcher=disp)
        except FileNotFoundError as exc:
            result = CommandResult(
                exit_kind=ExitKind.CONFIG,
                message=f"inventory not found: {exc}",
            )
        except OSError as exc:
            result = CommandResult(
                exit_kind=ExitKind.CONFIG,
                message=f"cannot read inventory: {exc}",
            )
        except ValueError as exc:
            result = CommandResult(
                exit_kind=ExitKind.CONFIG,
                message=f"invalid configuration: {exc}",
            )
        except RuntimeError as exc:
            result = CommandResult(
                exit_kind=ExitKind.OPERATIONAL,
                message=str(exc),
            )
        except NotImplementedError as exc:
            result = CommandResult(
                exit_kind=ExitKind.OPERATIONAL,
                message=str(exc),
            )
    finally:
        # Clear the transient progress line before any final output is
        # rendered or printed — on normal completion, after handled
        # exceptions, and before unhandled interruptions propagate.
        if progress_renderer is not None:
            progress_renderer.clear()

    stdout_text, stderr_text = _render(
        args.command,
        result,
        fmt=args.output,
        color=args.color,
        verbose=args.verbose,
        details=getattr(args, "details", False),
        stdout_is_tty=_stdout_tty,
        stderr_is_tty=_stderr_tty,
    )

    if stdout_text:
        print(stdout_text, file=sys.stdout)
    if stderr_text:
        print(stderr_text, file=sys.stderr)

    return _EXIT_CODES[result.exit_kind]

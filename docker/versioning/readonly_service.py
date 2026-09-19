"""Internal read-only service — domain logic for validate, show, check-updates.

This module owns inventory loading, scoped traversal, effective-resolution,
override application, provider setup, transport/token resolution, filtering,
and update execution.  It does **not** own argument parsing, user-facing
rendering, or the ``CommandDispatcher`` protocol boundary — those remain
in ``docker.docker-constructor`` (the facade).
"""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from docker.versioning.dispatch_types import CommandResult, ExitKind
from docker.versioning.model import Inventory


# ═══════════════════════════════════════════════════════════════════════
# Override parsing
# ═══════════════════════════════════════════════════════════════════════


def _parse_overrides(raw: Sequence[str]) -> dict[str, str]:
    """Parse ``PATH=VALUE`` override tokens.

    Rejects tokens with leading/trailing spaces, duplicate paths,
    or other malformations.
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
                f"duplicate override path {path!r}: each path may only be"
                f" specified once"
            )
        result[path] = value
    return result


# ═══════════════════════════════════════════════════════════════════════
# Handler functions (translate command_args → domain → CommandResult)
# ═══════════════════════════════════════════════════════════════════════


def _handle_validate(
    inventory: Inventory, command_args: Mapping[str, Any],
) -> CommandResult:
    """Validate — inventory is already loaded and valid, so this is a
    no-op that confirms success."""
    return CommandResult(
        exit_kind=ExitKind.SUCCESS,
        data={"valid": True},
        message="valid",
    )


def _handle_show(
    inventory: Inventory, command_args: Mapping[str, Any],
) -> CommandResult:
    """Display reviewed inventory (source or effective projection).

    Source mode returns the full reviewed inventory via ``to_plain_data``,
    filtered by ``--scope``.

    Effective mode calls the phase-specific resolution APIs —
    ``resolve_build_projection`` and ``resolve_runtime`` — and serialises
    their narrow DTOs.  Unlike ``apply_overrides()``, these projections
    do **not** retain reviewed source/provider/override metadata.
    Two separate projections are returned: ``build`` and ``runtime``.
    """
    from docker.versioning.effective import (
        resolve_build_projection,
        resolve_runtime,
        to_plain_data,
    )
    from docker.versioning.errors import EffectiveConfigError

    scope: str = str(command_args.get("scope", "all"))
    effective: bool = bool(command_args.get("effective", False))

    # Overrides may arrive pre-parsed (dict) from _dispatch_command
    # or raw (list/tuple) from injected test fakes.
    overrides_obj = command_args.get("overrides")
    overrides_parsed: dict[str, str] = {}
    if isinstance(overrides_obj, (dict, MappingProxyType)):
        overrides_parsed = dict(overrides_obj)
    elif isinstance(overrides_obj, (list, tuple)) and overrides_obj:
        try:
            overrides_parsed = _parse_overrides(list(overrides_obj))
        except ValueError as exc:
            return CommandResult(
                exit_kind=ExitKind.CLI,
                message=str(exc),
            )

    # ── overrides without --effective is an error ───────────────────
    if overrides_parsed and not effective:
        return CommandResult(
            exit_kind=ExitKind.CLI,
            message="--override requires --effective",
        )

    # ── scope-ownership guard ───────────────────────────────────────
    for path in overrides_parsed:
        if scope == "build" and path.startswith("runtime."):
            return CommandResult(
                exit_kind=ExitKind.CONFIG,
                message=(
                    f"override {path!r} is not valid for scope build"
                ),
            )
        if scope == "runtime" and path.startswith("build."):
            return CommandResult(
                exit_kind=ExitKind.CONFIG,
                message=(
                    f"override {path!r} is not valid for scope runtime"
                ),
            )

    # ── source mode: plain-data dump of reviewed inventory ──────────
    if not effective:
        plain = to_plain_data(inventory)
        if not isinstance(plain, dict):
            raise TypeError("inventory must serialize to a mapping")
        if scope == "build":
            filtered = {k: v for k, v in plain.items()
                        if k in ("schema", "build", "cache")}
        elif scope == "runtime":
            filtered = {k: v for k, v in plain.items()
                        if k in ("schema", "runtime")}
        else:
            filtered = plain
        return CommandResult(
            exit_kind=ExitKind.SUCCESS,
            data={"scope": scope, "effective": False,
                  "inventory": filtered},
        )

    # ── effective mode: phase-specific projections ──────────────────
    overrides_map: Mapping[str, str] = MappingProxyType(overrides_parsed)

    # Partition overrides by phase — each resolver rejects paths
    # it does not own, so we must not pass the combined map to both.
    build_overrides: Mapping[str, str] = MappingProxyType({
        k: v for k, v in overrides_parsed.items()
        if k.startswith("build.")
    })
    runtime_overrides: Mapping[str, str] = MappingProxyType({
        k: v for k, v in overrides_parsed.items()
        if k.startswith("runtime.")
    })

    data: dict[str, object] = {"scope": scope, "effective": True}

    try:
        if scope in ("build", "all"):
            build_proj = resolve_build_projection(
                inventory.build, build_overrides,
            )
            data["build"] = to_plain_data(build_proj)

        if scope in ("runtime", "all"):
            _, runtime_proj = resolve_runtime(
                inventory.runtime, runtime_overrides,
            )
            data["runtime"] = to_plain_data(runtime_proj)
    except EffectiveConfigError as exc:
        return CommandResult(
            exit_kind=ExitKind.CONFIG,
            message=str(exc),
        )

    return CommandResult(exit_kind=ExitKind.SUCCESS, data=data)


def _handle_check_updates(
    inventory: Inventory,
    command_args: Mapping[str, Any],
    *,
    progress: Any = None,
) -> CommandResult:
    """Check configured providers for updates.

    Returns structured results only — no presentation/rendering.
    The facade's output layer owns all text/JSON formatting.

    ``progress`` is an optional observational callback installed by the
    facade; it is forwarded verbatim to the sequential coordinator and
    never becomes part of structured result data.
    """
    from docker.versioning.updates import (
        _DEFAULT_PROVIDERS,
        check_updates,
    )
    from docker.versioning.providers.base import ProviderContext

    # --- resolve transports (production http / git) ---
    from docker.versioning.transports import build_transports

    inventory_cache = getattr(inventory, "cache", None)
    inventory_path = command_args.get("_inventory_path")
    # Cache consumers receive only the cache-owned slice of the shared local
    # aggregate result; they never reopen the companion and never receive the
    # aggregate (which would carry host-only presentation policy).
    local_cache = command_args.get("_local_cache")
    config = build_transports(
        no_cache=bool(command_args.get("no_cache", False)),
        inventory_cache=inventory_cache,
        local_cache=local_cache,
        suggest_mode=bool(command_args.get("suggest", False)),
    )

    context = ProviderContext(
        http=config.http,
        git=config.git,
        include_prerelease=bool(
            command_args.get("include_prerelease", False)
        ),
        tokens=config.tokens,
    )

    scope_str: str = str(command_args.get("scope", "all"))
    from docker.versioning.updates import Scope
    scope = Scope(scope_str)
    only: tuple[str, ...] = tuple(
        command_args.get("only_filter", ()) or ()
    )
    providers = _DEFAULT_PROVIDERS

    results = check_updates(
        inventory,
        providers=providers,
        context=context,
        only=only,
        scope=scope,
        progress=progress,
    )

    suggest: bool = bool(command_args.get("suggest", False))
    strict: bool = bool(command_args.get("strict", False))
    fail_on_outdated: bool = bool(
        command_args.get("fail_on_outdated", False)
    )

    # --- exit kind ---
    exit_kind = ExitKind.SUCCESS
    if strict and any(r.status.value == "unavailable" for r in results):
        exit_kind = ExitKind.OPERATIONAL
    elif fail_on_outdated and any(
        r.status.value == "outdated" and r.applicable for r in results
    ):
        exit_kind = ExitKind.POLICY

    # --- build structured data via canonical serializers ---
    from docker.versioning.updates import serialize_results, serialize_suggestions

    result_data: dict[str, object] = {
        "results": serialize_results(results),
        "suggest": suggest,
    }
    if suggest:
        suggestion_entries = serialize_suggestions(results)
        if suggestion_entries:
            result_data["suggestions"] = suggestion_entries
        # Complete, manually replaceable TOML fragments for text mode.  JSON
        # output keeps the structured leaf-change ``suggestions`` list above;
        # the facade strips this text-only key from JSON.
        from docker.versioning.inventory import load_inventory_raw
        from docker.versioning.updates import (
            build_update_targets,
            render_replacement_fragments,
        )
        if isinstance(inventory_path, (str, Path)):
            raw = load_inventory_raw(Path(inventory_path))
            targets = build_update_targets(inventory)
            result_data["replacement_fragments"] = render_replacement_fragments(
                raw, targets, results,
            )

    return CommandResult(
        exit_kind=exit_kind,
        data=result_data,
    )


_HANDLERS: dict[
    str, Callable[..., CommandResult]
] = {
    "validate": _handle_validate,
    "show": _handle_show,
    "check-updates": _handle_check_updates,
}


# ═══════════════════════════════════════════════════════════════════════
# Public entry point
# ═══════════════════════════════════════════════════════════════════════


def dispatch(
    inventory_path: Path,
    command: str,
    *,
    command_args: Mapping[str, Any],
    progress: Any = None,
) -> CommandResult:
    """Load inventory once, validate, and route to the appropriate handler.

    This is the sole public API of the read-only service module.
    The facade calls it from ``_real_dispatcher`` after resolving the
    inventory path from CLI arguments.

    ``progress`` is forwarded only to the ``check-updates`` handler; it
    is the facade-owned interactive discovery renderer and is otherwise
    ignored.
    """
    from docker.versioning.errors import (
        VersionConfigError,
        InventoryError,
        EffectiveConfigError,
        UpdateError,
    )
    from docker.versioning.inventory import load_project_configuration

    try:
        inventory, local_config = load_project_configuration(inventory_path)
    except FileNotFoundError:
        return CommandResult(
            exit_kind=ExitKind.CONFIG,
            message=f"inventory not found: {inventory_path}",
        )
    except (VersionConfigError, InventoryError) as exc:
        return CommandResult(
            exit_kind=ExitKind.CONFIG,
            message=str(exc),
        )

    handler = _HANDLERS.get(command)
    if handler is None:
        return CommandResult(
            exit_kind=ExitKind.CLI,
            message=f"'{command}' is not available in this stage",
        )

    try:
        handler_args = dict(command_args)
        handler_args["_inventory_path"] = inventory_path
        handler_args["_local_cache"] = getattr(local_config, "cache", None)
        if command == "check-updates":
            return handler(inventory, handler_args, progress=progress)
        return handler(inventory, handler_args)
    except (VersionConfigError, InventoryError, EffectiveConfigError,
            UpdateError) as exc:
        return CommandResult(
            exit_kind=ExitKind.CONFIG,
            message=str(exc),
        )

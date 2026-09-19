"""Domain-owned parsing for local host presentation configuration."""
from __future__ import annotations

from .errors import InventoryError
from .model import LocalOutputPolicy


_VALID_HEARTBEAT_MODES = frozenset({"interactive", "lines", "off"})


def parse_local_output_policy(
    output_raw: object,
    host_access_mode: str | None,
) -> LocalOutputPolicy:
    """Parse the closed local ``[output]`` table without presentation effects."""
    del host_access_mode
    if output_raw is None:
        return LocalOutputPolicy()
    if not isinstance(output_raw, dict):
        raise InventoryError(
            "local.output: expected table; use [output]",
            field="local.output",
        )
    unknown = set(output_raw) - {"host_heartbeat", "show_network_hosts"}
    if unknown:
        key = sorted(unknown)[0]
        raise InventoryError(
            f"local.output.{key}: unknown key; use only "
            "local.output.host_heartbeat and local.output.show_network_hosts",
            field=f"local.output.{key}",
        )
    heartbeat = output_raw.get("host_heartbeat", "interactive")
    if not isinstance(heartbeat, str):
        raise InventoryError(
            "local.output.host_heartbeat: expected string; use interactive, lines, or off",
            field="local.output.host_heartbeat",
        )
    if heartbeat not in _VALID_HEARTBEAT_MODES:
        raise InventoryError(
            "local.output.host_heartbeat: unsupported value; use interactive, lines, or off",
            field="local.output.host_heartbeat",
        )
    show_hosts = output_raw.get("show_network_hosts", False)
    if not isinstance(show_hosts, bool):
        raise InventoryError(
            "local.output.show_network_hosts: expected boolean",
            field="local.output.show_network_hosts",
        )
    return LocalOutputPolicy(heartbeat, show_hosts)

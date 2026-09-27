"""Domain-owned parsing for local host presentation configuration."""
from __future__ import annotations

from .errors import InventoryError
from .model import LocalOutputPolicy, NetworkUrlDisplay


_VALID_HEARTBEAT_MODES = frozenset({"interactive", "lines", "off"})
_VALID_NETWORK_URL_DISPLAYS = frozenset(value.value for value in NetworkUrlDisplay)


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
    unknown = set(output_raw) - {"host_heartbeat", "network_url_display"}
    if unknown:
        key = sorted(unknown)[0]
        raise InventoryError(
            f"local.output.{key}: unknown key; use only "
            "local.output.host_heartbeat and local.output.network_url_display",
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
    network_url_display = output_raw.get(
        "network_url_display", NetworkUrlDisplay.REDACTED.value
    )
    if not isinstance(network_url_display, str):
        raise InventoryError(
            "local.output.network_url_display: expected string; use redacted, host-path, or exact",
            field="local.output.network_url_display",
        )
    if network_url_display not in _VALID_NETWORK_URL_DISPLAYS:
        raise InventoryError(
            "local.output.network_url_display: unsupported value; use redacted, host-path, or exact",
            field="local.output.network_url_display",
        )
    return LocalOutputPolicy(heartbeat, NetworkUrlDisplay(network_url_display))

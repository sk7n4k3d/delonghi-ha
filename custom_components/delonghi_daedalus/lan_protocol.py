"""LAN WebSocket protocol helpers (Daedalus `/ws/lan2lan`).

Wire format captured from `com.delonghigroup.appliance_kit.web_socket`.
Everything here is pure (no I/O, no HA deps) so it can be tested offline.
"""

from __future__ import annotations

import ipaddress
import json
import secrets
from typing import Any

from .const import LAN_WS_PATH

_RESERVED_FRAME_KEYS = frozenset({"Message", "ConnectionId", "RequestId"})


class LanProtocolError(RuntimeError):
    """Raised for malformed / rejected LAN WS frames."""


def build_lan_ws_url(host: str) -> str:
    """Return `wss://<host>/ws/lan2lan`, bracketing IPv6 literals."""
    if _is_ipv6(host):
        return f"wss://[{host}]{LAN_WS_PATH}"
    return f"wss://{host}{LAN_WS_PATH}"


def build_auth_frame(*, serial_number: str, jwt: str) -> str:
    """Serialize the initial AUTH frame expected by the machine."""
    return json.dumps(
        {"Message": "AUTH", "SerialNo": serial_number, "AuthToken": jwt},
        separators=(",", ":"),
    )


def parse_auth_response(payload: dict[str, Any]) -> int:
    """Return `ConnectionId` from a successful AUTH response."""
    response = payload.get("Response")
    if response != "OK":
        reason = payload.get("Reason") or payload.get("Error") or response
        raise LanProtocolError(f"LAN AUTH rejected: {reason}")
    connection_id = payload.get("ConnectionId")
    if not isinstance(connection_id, int):
        raise LanProtocolError("LAN AUTH response missing numeric ConnectionId")
    return connection_id


def build_command_frame(
    *,
    message: str,
    connection_id: int,
    request_id: str,
    params: dict[str, Any] | None = None,
) -> str:
    """Serialize a command frame.

    Reserved top-level keys (`Message`, `ConnectionId`, `RequestId`) cannot be
    overridden by `params` — raise so callers notice at wiring time instead of
    silently shipping a malformed frame that the machine would drop.
    """
    params = params or {}
    overlap = _RESERVED_FRAME_KEYS.intersection(params)
    if overlap:
        raise LanProtocolError(f"params cannot override reserved keys: {sorted(overlap)}")

    body: dict[str, Any] = {
        "Message": message,
        "ConnectionId": connection_id,
        "RequestId": request_id,
    }
    body.update(params)
    return json.dumps(body, separators=(",", ":"))


def generate_request_id() -> str:
    """Return a URL-safe, unguessable correlation id (≥16 chars)."""
    # 16 bytes → 22 base64-urlsafe chars (no padding).
    return secrets.token_urlsafe(16)


def parse_message(raw: str | bytes) -> dict[str, Any]:
    """Decode a JSON object frame; reject arrays/scalars."""
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise LanProtocolError(f"LAN frame is not valid JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise LanProtocolError(f"LAN frame must be a JSON object, got {type(decoded).__name__}")
    return decoded


def _is_ipv6(host: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(host), ipaddress.IPv6Address)
    except ValueError:
        return False

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
    """Raised for malformed LAN WS frames (bad JSON, missing fields).

    Malformed does NOT mean rejected: a booting/upgrade-ing machine or a
    non-Daedalus service answering on port 443 produces these. Callers must
    treat them as transient — see :class:`LanAuthRejectedError`.
    """


class LanAuthRejectedError(LanProtocolError):
    """The machine PARSED our frame and explicitly answered ``Response != OK``.

    Only this (not a garbled reply) proves the credentials were seen and
    refused — it is the sole legitimate trigger for an auth escalation.
    """


def build_lan_ws_url(host: str) -> str:
    """Return `wss://<host>/ws/lan2lan`, bracketing IPv6 literals."""
    if _is_ipv6(host):
        return f"wss://[{host}]{LAN_WS_PATH}"
    return f"wss://{host}{LAN_WS_PATH}"


_LAN_NETWORKS = tuple(
    ipaddress.ip_network(network) for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
)


def validate_lan_host(host: str) -> str:
    """Require an unscoped private IP literal (RFC 1918 IPv4 or IPv6 ULA)."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError as exc:
        raise LanProtocolError("LAN host must be a numeric IP address") from exc
    if "%" in host or not any(ip in network for network in _LAN_NETWORKS):
        raise LanProtocolError("LAN host must be a private IPv4 or unique-local IPv6 address")
    return host


def build_auth_frame(*, serial_number: str, jwt: str, message: str = "AUTH") -> str:
    """Serialize the initial AUTH frame expected by the machine.

    `message` is the frame verb: most firmwares expect ``AUTH``, but some
    Eletta Ultra units only accept lowercase ``auth`` (issue #18). The
    caller (`api.connect_lan`) owns the fallback between the two.
    """
    return json.dumps(
        {"Message": message, "SerialNo": serial_number, "AuthToken": jwt},
        separators=(",", ":"),
    )


def parse_auth_response(payload: dict[str, Any]) -> int:
    """Return `ConnectionId` from a successful AUTH response.

    Raises :class:`LanAuthRejectedError` only when the machine answered a
    parsable object with ``Response != "OK"`` — a proven credential refusal.
    A well-formed answer without a numeric ``ConnectionId`` is a protocol
    anomaly (firmware mid-boot, unexpected service), not a rejection, and
    stays a plain :class:`LanProtocolError` (audit 2026-11-05).
    """
    response = payload.get("Response")
    if response != "OK":
        reason = payload.get("Reason") or payload.get("Error") or response
        # Reason is machine-controlled free text (can echo the serial —
        # audit #18 G2): hard-cap its length; the serial itself is scrubbed
        # by sanitize_text(serial=…) at the api.py call sites.
        raise LanAuthRejectedError(f"LAN AUTH rejected: {str(reason)[:120]}")
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
    except (TypeError, ValueError, RecursionError) as exc:
        raise LanProtocolError(f"LAN frame is not valid JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise LanProtocolError(f"LAN frame must be a JSON object, got {type(decoded).__name__}")
    return decoded


def _is_ipv6(host: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(host), ipaddress.IPv6Address)
    except ValueError:
        return False

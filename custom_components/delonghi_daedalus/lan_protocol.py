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


def validate_lan_host(host: str) -> str:
    """Reject hosts that aren't an IP literal in a private / loopback / link-local range.

    The Daedalus firmware presents a self-signed certificate, so the integration
    builds the TLS context with `verify_mode=CERT_NONE` (see `api._build_trust_all_ssl_context`).
    That's safe **only** as long as the target host is on the user's own LAN.
    A hostname or a public IP would let any on-path attacker MITM the WebSocket
    handshake and steal the JWT carried in the AUTH frame.

    Accepted:
        - RFC1918 v4 (10/8, 172.16/12, 192.168/16)
        - Loopback (127/8, ::1)
        - Link-local (169.254/16, fe80::/10)
        - Unique-local v6 (fc00::/7)

    Rejected:
        - Any DNS hostname (no resolution attempted — the user must enter an IP)
        - Public / globally-routable IPs

    Returns the original host string when valid; raises `LanProtocolError` otherwise.
    """
    try:
        ip = ipaddress.ip_address(host)
    except ValueError as exc:
        raise LanProtocolError(
            f"LAN host must be a numeric IP address, got {host!r} "
            "(self-signed TLS means we can't accept hostnames safely)"
        ) from exc
    if not (ip.is_private or ip.is_loopback or ip.is_link_local):
        raise LanProtocolError(
            f"LAN host {host!r} is not in a private / loopback / link-local range; "
            "trust-all TLS would expose the JWT to any on-path attacker"
        )
    return host


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

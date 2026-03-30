"""Structured logging with credential sanitization for De'Longhi Coffee."""

from __future__ import annotations

import logging
import re
import time
from collections import deque
from typing import Any

_LOGGER = logging.getLogger("custom_components.delonghi_coffee")

# Patterns to sanitize from log messages
_SANITIZE_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Ayla auth tokens (Bearer-style)
    (re.compile(r"auth_token\s+\S+"), "auth_token ***"),
    # JWT tokens (three base64 segments)
    (re.compile(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"), "***JWT***"),
    # Gigya session tokens / secrets
    (re.compile(r"(sessionToken|sessionSecret|secret|oauth_token)[\"']?\s*[:=]\s*[\"']?[\w.+/=-]{10,}"), r"\1=***"),
    # Ayla access/refresh tokens in JSON
    (re.compile(r"(access_token|refresh_token)[\"']?\s*[:=]\s*[\"']?[\w.+/=-]{10,}"), r"\1=***"),
    # Email addresses (partial mask)
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), lambda m: _mask_email(m.group(0))),
    # Passwords in POST data
    (re.compile(r"(password)[\"']?\s*[:=]\s*[\"']?[^\s,\"'}&]+"), r"\1=***"),
    # LAN keys (hex or base64, 16+ chars after key identifier)
    (re.compile(r"(lanip_key|local_key)[\"']?\s*[:=]\s*[\"']?[\w+/=-]{16,}"), r"\1=***"),
    # App secrets
    (re.compile(r"(app_secret)[\"']?\s*[:=]\s*[\"']?[\w-]{10,}"), r"\1=***"),
]


def _mask_email(email: str) -> str:
    """Mask email: s***n@g***.com."""
    local, _, domain = email.partition("@")
    if len(local) <= 2:
        masked_local = "*" * len(local)
    else:
        masked_local = local[0] + "***" + local[-1]
    parts = domain.split(".", 1)
    if len(parts) == 2 and len(parts[0]) > 2:
        masked_domain = parts[0][0] + "***." + parts[1]
    else:
        masked_domain = domain
    return f"{masked_local}@{masked_domain}"


def sanitize(msg: str) -> str:
    """Remove credentials and sensitive data from a log message."""
    for pattern, replacement in _SANITIZE_PATTERNS:
        if callable(replacement):
            msg = pattern.sub(replacement, msg)
        else:
            msg = pattern.sub(replacement, msg)
    return msg


class RateLimitTracker:
    """Sliding window rate counter for API calls."""

    def __init__(self, window_seconds: int = 3600) -> None:
        self._window = window_seconds
        self._calls: deque[float] = deque()
        self._total: int = 0
        self._warn_threshold: int = 200  # warn at 200 calls/hour (Ayla bans ~250+)
        self._warned: bool = False

    def record(self) -> int:
        """Record an API call, return current rate (calls/hour)."""
        now = time.monotonic()
        self._calls.append(now)
        self._total += 1
        # Expire old entries
        cutoff = now - self._window
        while self._calls and self._calls[0] < cutoff:
            self._calls.popleft()
        rate = len(self._calls)
        if rate >= self._warn_threshold and not self._warned:
            _LOGGER.warning(
                "API rate approaching limit: %d calls in last %d min (threshold: %d). "
                "Risk of IP ban from Ayla cloud.",
                rate,
                self._window // 60,
                self._warn_threshold,
            )
            self._warned = True
        elif rate < self._warn_threshold - 20:
            self._warned = False
        return rate

    @property
    def current_rate(self) -> int:
        """Current calls in the sliding window."""
        now = time.monotonic()
        cutoff = now - self._window
        while self._calls and self._calls[0] < cutoff:
            self._calls.popleft()
        return len(self._calls)

    @property
    def total_calls(self) -> int:
        """Total calls since creation."""
        return self._total


class ApiTimer:
    """Context manager to time API calls."""

    def __init__(self, operation: str, tracker: RateLimitTracker | None = None) -> None:
        self._operation = operation
        self._tracker = tracker
        self._start: float = 0

    def __enter__(self) -> ApiTimer:
        self._start = time.monotonic()
        if self._tracker:
            self._tracker.record()
        return self

    def __exit__(self, *exc: Any) -> None:
        duration_ms = (time.monotonic() - self._start) * 1000
        if exc[0] is not None:
            _LOGGER.debug(
                "[%s] failed after %.0fms: %s",
                self._operation,
                duration_ms,
                exc[1],
            )
        elif duration_ms > 5000:
            _LOGGER.warning("[%s] slow response: %.0fms", self._operation, duration_ms)
        else:
            _LOGGER.debug("[%s] completed in %.0fms", self._operation, duration_ms)

    @property
    def elapsed_ms(self) -> float:
        return (time.monotonic() - self._start) * 1000


def get_diagnostic_dump(
    properties: dict[str, Any],
    counters: dict[str, Any],
    status: dict[str, Any],
) -> dict[str, Any]:
    """Build a diagnostic dump safe for sharing (no credentials).

    Includes raw property names/types, counter values, machine state,
    and alarm data — everything needed to debug issues without exposing
    auth tokens or user data.
    """
    # Property summary (name → type + value length, no actual secrets)
    prop_summary: dict[str, Any] = {}
    for name, prop in sorted(properties.items()):
        val = prop.get("value")
        if val is None:
            prop_summary[name] = {"type": "null"}
        elif isinstance(val, str):
            if val.startswith("{"):
                prop_summary[name] = {"type": "json", "length": len(val)}
            elif len(val) > 100:
                prop_summary[name] = {"type": "base64", "length": len(val)}
            else:
                # Short string values are usually safe (integers, status strings)
                prop_summary[name] = {"type": "string", "value": val}
        else:
            prop_summary[name] = {"type": type(val).__name__, "value": val}

    return {
        "property_count": len(properties),
        "properties": prop_summary,
        "counters": counters,
        "machine_state": status.get("machine_state", "Unknown"),
        "cloud_status": status.get("status", "UNKNOWN"),
        "alarms": status.get("alarms", []),
        "alarm_word": status.get("alarm_word"),
        "monitor_raw": status.get("monitor_raw"),
        "profile": status.get("profile", 0),
    }

"""Structured logging with credential sanitization for De'Longhi Coffee."""

from __future__ import annotations

import contextlib
import logging
import re
import time
import traceback
from collections import deque
from typing import Any

# Hard ceiling for sanitize() input (audit 2026-11-05, perf finding): the
# scrubbers are linear but an unbounded traceback from a machine- or
# server-controlled string still costs proportional time on every record,
# on the caller's thread. Text past the cap is dropped (never rendered
# unfiltered); call sites already truncate HTTP bodies to 200 chars, real
# tracebacks land well under this.
_MAX_LEN = 65536
_TRUNCATED = "...[TRUNCATED]"

# Patterns to sanitize from log messages.
# Order matters only where a later rule would re-match an earlier replacement;
# all replacements below are idempotent (sanitizing twice changes nothing).
_SANITIZE_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Ayla auth tokens in any wire form: header ("auth_token <v>"), query,
    # JSON ("auth_token=<v>", "auth_token: <v>", {"auth_token": "<v>"}) —
    # audit 2026-11-05: the old space-only pattern let the `=` form through,
    # then the `[:=]?\s*\S+` form broke on the JSON shape (closing quote of
    # the key defeats `[:=]?`, the space after `:` defeats `\S+`) while
    # over-masking beyond the token. The key prefix is captured and kept;
    # only the value (8+ token-shaped chars) is replaced.
    (re.compile(r"([\"']?auth_token[\"']?\s*[:=]?\s*[\"']?)[\w.+/=-]{8,}", re.IGNORECASE), r"\1***"),
    # Device DSN inside Ayla URLs (`/apiv1/dsns/<DSN>/...`): requests'
    # HTTPError text embeds the full URL, so exception messages reaching
    # logs and diagnostics carry the DSN (audit 2026-11-05, M4 bypass).
    # A bare DSN elsewhere in a message stays readable — it is only the
    # credential-bearing URL path that must never be pasted publicly.
    (re.compile(r"/dsns/[^/?#\s]+/"), "/dsns/**REDACTED**/"),
    # JWT tokens (three base64 segments)
    (re.compile(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"), "***JWT***"),
    # Gigya session tokens / secrets
    (re.compile(r"(sessionToken|sessionSecret|secret|oauth_token)[\"']?\s*[:=]\s*[\"']?[\w.+/=-]{10,}"), r"\1=***"),
    # Ayla access/refresh tokens in JSON
    (re.compile(r"(access_token|refresh_token)[\"']?\s*[:=]\s*[\"']?[\w.+/=-]{10,}"), r"\1=***"),
    # Passwords in POST data
    (re.compile(r"(password)[\"']?\s*[:=]\s*[\"']?[^\s,\"'}&]+"), r"\1=***"),
    # LAN keys (hex or base64, 16+ chars after key identifier)
    (re.compile(r"(lanip_key|local_key)[\"']?\s*[:=]\s*[\"']?[\w+/=-]{16,}"), r"\1=***"),
    # App secrets
    (re.compile(r"(app_secret)[\"']?\s*[:=]\s*[\"']?[\w-]{10,}"), r"\1=***"),
]


def _mask_email(email: str) -> str:
    """Mask email: s***n@***.com.

    Every domain label except the last (the TLD) is replaced wholesale —
    masking only the first label left the organisation label standing in
    multi-label domains like ``mail.entreprise.fr`` (audit 2026-11-05).
    Single-label hosts (``localhost``) have no org label and stay visible;
    the local part is still masked, so the address never round-trips.
    """
    local, sep, domain = str(email).partition("@")
    if not sep:
        return "***" if local else str(email)
    if len(local) <= 2:
        masked_local = "*" * len(local)
    else:
        masked_local = local[0] + "***" + local[-1]
    labels = domain.split(".")
    if len(labels) == 1:
        masked_domain = domain
    else:
        masked_domain = ".".join(["***"] * (len(labels) - 1) + [labels[-1]])
    return f"{masked_local}@{masked_domain}"


# Reference shape of an email in free text (audit 2026-11-05). Kept as the
# exact semantics the linear scanner below must reproduce (and asserted as
# such in tests); it is NOT used to scan — a regex quantifier over
# ``[\w.+-]+`` backtracks per start position, which is quadratic on long
# non-matching text (a machine-piloted body or a big traceback froze the HA
# event loop ~9 s at 50 kB before the fix).
_EMAIL_REF_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*")

# ``\w`` plus the punctuation the address classes allow (unicode ``\w`` is
# approximated by str.isalnum() + "_", sufficient for address characters).
_LOCAL_CHARS = frozenset(".+-")
_DOMAIN_CHARS = frozenset("-")


def _is_local_char(ch: str) -> bool:
    return ch.isalnum() or ch in "_." or ch in _LOCAL_CHARS


def _is_domain_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_" or ch in _DOMAIN_CHARS


def _mask_emails(msg: str) -> str:
    """Partial-mask every email address in ``msg``, left-to-right, in O(n).

    Same match shape as ``_EMAIL_REF_RE``: scan local part ``[\\w.+-]+``
    backwards from each ``@``, consume the domain ``[\\w-]+(\\.[\\w-]+)*``
    forward (a bare ``@`` with no domain label does not match — e-mails in
    the text are never a silent no-op). Overlapping candidates after an
    already-masked span are skipped, like ``re.sub`` does.
    """
    if "@" not in msg:
        return msg
    parts: list[str] = []
    pos = 0
    end_of_msg = len(msg)
    while True:
        at = msg.find("@", pos)
        if at < 0:
            parts.append(msg[pos:])
            return "".join(parts)
        start = at
        while start > pos and _is_local_char(msg[start - 1]):
            start -= 1
        end = at + 1
        if start == at or end >= end_of_msg or not _is_domain_char(msg[end]):
            # Not an address at this '@' — keep the raw text, keep scanning.
            parts.append(msg[pos : at + 1])
            pos = at + 1
            continue
        while True:
            while end < end_of_msg and _is_domain_char(msg[end]):
                end += 1
            if end < end_of_msg and msg[end] == "." and end + 1 < end_of_msg and _is_domain_char(msg[end + 1]):
                end += 1
                continue
            break
        parts.append(msg[pos:start])
        parts.append(_mask_email(msg[start:end]))
        pos = end


def sanitize(msg: str) -> str:
    """Remove credentials and sensitive data from a log message.

    Accepts non-string input (exception objects are frequently interpolated
    into log args) and coerces — the sanitizer must never raise from inside
    a logging call. Text longer than ``_MAX_LEN`` is truncated (the tail is
    dropped, never passed through unfiltered).
    """
    if not isinstance(msg, str):
        msg = str(msg)
    if len(msg) > _MAX_LEN:
        msg = msg[: _MAX_LEN - len(_TRUNCATED)] + _TRUNCATED
    for pattern, replacement in _SANITIZE_PATTERNS:
        msg = pattern.sub(replacement, msg)
    msg = _mask_emails(msg)
    return msg


class SanitizeFilter(logging.Filter):
    """Apply :func:`sanitize` to every record rendered through this logger.

    Rewrites the already-interpolated message (so any future ``%s`` added to
    an existing call site is covered, not just the ones we remember to wrap by
    hand) and pre-renders + scrubs the exception traceback, which the
    formatter would otherwise emit verbatim.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — a broken %-template must not silence the log
            return True
        record.msg = sanitize(message)
        record.args = None
        if record.exc_info:
            # Pre-render + scrub the traceback: the formatter only calls
            # formatException when exc_text is empty, so the sanitized
            # rendering wins. exc_info itself is kept intact for
            # introspection (log capture tooling, tests). On a scrub failure
            # we leave exc_text empty and the formatter renders it unfiltered
            # — a raw line beats a lost log line.
            with contextlib.suppress(Exception):
                # rstrip: Formatter.formatException renders "\n" + text and
                # strips exactly the trailing newline (s[:-1]); a kept "\n"
                # appended an empty line after every traceback and broke
                # "one block = one entry" readers (grep -A, traceback
                # parsers) — audit 2026-11-05.
                record.exc_text = sanitize("".join(traceback.format_exception(*record.exc_info))).rstrip("\n")
        return True


def get_logger(name: str) -> logging.Logger:
    """Return a module logger wired with the sanitizer filter (audit 2026-11-05).

    Filters attached to a parent logger do NOT apply to child loggers in
    stdlib logging, so each module logger of the component carries its own
    instance — masking becomes inductive instead of per-call-site.
    """
    logger = logging.getLogger(name)
    if not any(isinstance(f, SanitizeFilter) for f in logger.filters):
        logger.addFilter(SanitizeFilter())
    return logger


_LOGGER = get_logger("custom_components.delonghi_coffee")


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
                "API rate approaching limit: %d calls in last %d min (threshold: %d). Risk of IP ban from Ayla cloud.",
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

"""Text redaction for Daedalus logs, config-flow messages and diagnostics.

Single implementation for the whole component (audit 2026-11-05): the config
flow's ad-hoc ``_mask_email`` and the diagnostics dumps previously diverged —
each masked only the first domain label (leaving the organisation label
standing) and neither scrubbed the LAN host or session material that exception
texts carry. A value that reaches a WARNING line, a reauth prompt or a
downloadable diagnostics dump may be pasted into a public GitHub issue
(same policy as ``delonghi_coffee.logger``), so every sink shares these rules.

The two custom components ship and install independently (HACS), so "shared"
means shared *within* the component; the rules are kept aligned with the
coffee component's ``logger.sanitize`` and covered by tests on both sides.
"""

from __future__ import annotations

import contextlib
import ipaddress
import logging
import re
import traceback

# Hard ceiling for sanitize_text() input (audit 2026-11-05, perf finding):
# the scrubbers are linear but an unbounded machine-/server-controlled string
# still costs proportional time, and sanitize_text runs on the HA event loop.
# Text past the cap is dropped (never rendered unfiltered).
_MAX_LEN = 65536
_TRUNCATED = "...[TRUNCATED]"

_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_WSS_RE = re.compile(r"(wss?://)[^\s\"',;]+")
# Private / loopback / link-local IPv4 literals. IPv6 is NOT pattern-matched
# here (writing a correct regex for ULA fc00::/7, LLA fe80::/10, ::1 and the
# zone-id variants is the classic trap); the configured host is scrubbed
# literally instead — see _host_text_forms() and the host registry. The IPv4
# pattern remains as the no-context defense-in-depth net.
_PRIV4_RE = re.compile(
    r"\b(?:10|127)(?:\.\d{1,3}){3}\b"
    r"|\b172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}\b"
    r"|\b192\.168(?:\.\d{1,3}){2}\b"
    r"|\b169\.254(?:\.\d{1,3}){2}\b"
)
_TOKEN_RE = re.compile(r"(sessionToken|sessionSecret|oauth_token|AuthToken)[\"']?\s*[:=]\s*[\"']?[\w.+/=-]{10,}")
# Reference shape of an email in free text (audit 2026-11-05). Kept as the
# exact semantics the linear scanner below must reproduce (asserted in tests);
# it is NOT used to scan — the quantifier over ``[\w.+-]+`` backtracks per
# start position, quadratic on long non-matching text.
_EMAIL_REF_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*")

_LOCAL_CHARS = frozenset(".+-")
_DOMAIN_CHARS = frozenset("-")

_REDACTED_HOST = "**REDACTED-HOST**"


def mask_email(email: object) -> str:
    """Mask an email for log/exception messages: s***n@***.com.

    Accepts any object (it is called during ``raise`` construction, where
    ``entry.data`` values are only schema-validated on the happy path — a
    non-str must degrade to a mask, never raise). Every domain label except
    the TLD is replaced, so the organisation label survives in neither
    single- nor multi-label domains.
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


def mask_host(host: object) -> str:
    """Partial-mask a LAN host for logs: keep the /16, hide the host part.

    ``192.168.1.42`` → ``192.168.x.x`` — enough to spot a subnet mismatch in
    a bug report, useless to an outsider. Non-IPv4 input is fully masked.
    """
    try:
        ip = ipaddress.ip_address(str(host))
    except ValueError:
        return "**REDACTED-IP**"
    if ip.version != 4:
        return "**REDACTED-IP**"
    parts = str(ip).split(".")
    return f"{parts[0]}.{parts[1]}.x.x"


def mask_serial(serial: object) -> str:
    """Partial-mask a serial number: only the last 4 chars stay visible."""
    text = str(serial)
    if len(text) <= 4:
        return "****"
    return "*" * (len(text) - 4) + text[-4:]


def _is_local_char(ch: str) -> bool:
    return ch.isalnum() or ch in "_." or ch in _LOCAL_CHARS


def _is_domain_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_" or ch in _DOMAIN_CHARS


def _mask_emails(msg: str) -> str:
    """Partial-mask every email address in ``msg``, left-to-right, in O(n).

    Same match shape as ``_EMAIL_REF_RE`` (and as the coffee component's
    scanner): scan the local part ``[\\w.+-]+`` backwards from each ``@``,
    consume the domain ``[\\w-]+(\\.[\\w-]+)*`` forward. Overlapping
    candidates after an already-masked span are skipped, like ``re.sub``.
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
        parts.append(mask_email(msg[start:end]))
        pos = end


def _host_text_forms(host: object) -> tuple[str, ...]:
    """Every textual shape a known host can take inside an error message.

    ``validate_lan_host`` accepts IPv4 and IPv6 literals (ULA ``fc00::/7``,
    LLA ``fe80::/10``, ``::1`` included — the comment claiming IPv4-only was
    wrong, audit 2026-11-05), and the aiohttp/WS error texts repeat the host
    bare after the ``wss://`` prefix, bracketed ``[host]`` for IPv6, or with a
    ``%zone`` suffix. Only values that parse as IP addresses are scrubbed —
    a user-typed hostname ("printer") must not blind-substring every message.
    """
    if host is None:
        return ()
    text = str(host).strip()
    if not text:
        return ()
    base = text.split("%", 1)[0]  # strip an IPv6 zone-id for the base form
    try:
        ipaddress.ip_address(base)
    except ValueError:
        return ()
    forms = (text, f"[{text}]", base, f"[{base}]")
    return tuple(dict.fromkeys(form for form in forms if form not in ("", "[]")))


# Forms of every LAN host this process has connected to. The SanitizeFilter
# cannot know the entry's host at format time, and ``raise … from exc`` chains
# library exceptions whose own text (``Cannot connect to host <h>:443``) lands
# in tracebacks and in the diagnostics ``last_exception``; registering the host
# when a connection is attempted keeps the scrubbing inductive instead of
# relying on every call site remembering. Re-adding an existing host is a
# no-op, so the set is bounded by the number of distinct configured hosts.
_KNOWN_HOST_FORMS: set[str] = set()


def register_lan_host(host: object) -> None:
    """Remember a LAN host so every sanitized text scrubs it, hostless."""
    _KNOWN_HOST_FORMS.update(_host_text_forms(host))


def _scrub_hosts(msg: str, host: object) -> str:
    """Literal-scrub the known host(s) — the fix for the IPv6 gap.

    A generic IPv6 regex is deliberately NOT attempted: a wrong one silently
    under-masks. Literal replacement of the known value covers every shape
    the host can appear in (bare, bracketed, zone-id, both endpoints of a
    ``tuple`` repr), longest-form first so ``[fe80::1%eth0]`` is replaced
    before its own substrings.
    """
    forms = sorted({*_KNOWN_HOST_FORMS, *_host_text_forms(host)}, key=len, reverse=True)
    for form in forms:
        msg = msg.replace(form, _REDACTED_HOST)
    return msg


def sanitize_text(text: object, *, host: object = None) -> str:
    """Scrub every known secret class from free text (exception messages…).

    Server- and machine-controlled strings (Gigya errorMessage echoing the
    loginID, the LAN frame ``Reason``) end up in WARNING logs and in the
    downloadable diagnostics dump; they must never carry JWTs, session
    tokens, emails or the machine LAN IP.

    ``host`` is the configured LAN host when the caller knows it: private
    IPv6 forms (ULA/LLA/``::1``, with or without zone-id) cannot be caught by
    the IPv4 pattern, so they are scrubbed literally. Hosts registered via
    :func:`register_lan_host` are scrubbed even without the argument.
    """
    msg = text if isinstance(text, str) else str(text)
    if len(msg) > _MAX_LEN:
        msg = msg[: _MAX_LEN - len(_TRUNCATED)] + _TRUNCATED
    msg = _JWT_RE.sub("***JWT***", msg)
    msg = _TOKEN_RE.sub(r"\1=***", msg)
    msg = _WSS_RE.sub(r"\1**REDACTED**", msg)
    msg = _PRIV4_RE.sub("**REDACTED-IP**", msg)
    msg = _scrub_hosts(msg, host)
    msg = _mask_emails(msg)
    return msg


class SanitizeFilter(logging.Filter):
    """Apply :func:`sanitize_text` to every record rendered through this logger.

    Mirrors ``delonghi_coffee.logger.SanitizeFilter`` (audit 2026-11-05):
    masking must be inductive, not per-call-site — this is the same logger
    HA's ``DataUpdateCoordinator`` uses for ``logger.exception("Unexpected
    error fetching …")``, so without a filter any unexpected traceback (the
    ``repr()`` of ``__cause__`` included) would leave the Daedalus component
    completely unscrubbed. Rewrites the interpolated message and pre-renders
    + scrubs the exception traceback, which the formatter would otherwise
    emit verbatim.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — a broken %-template must not silence the log
            return True
        record.msg = sanitize_text(message)
        record.args = None
        if record.exc_info:
            # Same mechanism as the coffee component: the formatter only
            # calls formatException when exc_text is empty, so the sanitized
            # pre-render wins. exc_info stays intact for introspection. On a
            # scrub failure exc_text stays empty and the formatter renders
            # raw — a raw line beats a lost log line.
            with contextlib.suppress(Exception):
                record.exc_text = sanitize_text("".join(traceback.format_exception(*record.exc_info))).rstrip("\n")
        return True


def get_logger(name: str) -> logging.Logger:
    """Return a module logger wired with the sanitizer filter.

    Filters attached to a parent logger do NOT apply to child loggers in
    stdlib logging, so each module logger of the component carries its own
    instance (same design as ``delonghi_coffee.logger.get_logger``).
    """
    logger = logging.getLogger(name)
    if not any(isinstance(f, SanitizeFilter) for f in logger.filters):
        logger.addFilter(SanitizeFilter())
    return logger

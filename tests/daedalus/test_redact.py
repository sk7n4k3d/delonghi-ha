"""Tests for the Daedalus redaction helper (audit 2026-11-05).

The redacter backs the config-flow WARNINGs, the coordinator escalation
messages and the diagnostics `last_exception` dump — all channels that end
up pasted into public GitHub issues (same policy as the coffee component's
``logger.sanitize``). The masking rules are deliberately aligned with
``delonghi_coffee.logger`` and asserted on both sides.
"""

import pytest

from custom_components.delonghi_daedalus import redact
from custom_components.delonghi_daedalus.redact import mask_email, mask_host, mask_serial, sanitize_text

# Hosts used by these tests only — unique enough that even a leaked registry
# entry from another test file cannot collide with the assertions below.
ULA = "fd12:3456::1"
LLA_ZONED = "fe80::99%eth0"
ULA_SHORT = "fc00::abcd"
LOOPBACK6 = "::1"


def _aiohttp_host_error(host: str) -> str:
    """Template of the aiohttp/WS texts that carry the host (audit F1)."""
    bare = host.split("%", 1)[0]
    return (
        f"LAN WS connect to wss://{host}/ws/lan2lan failed: Cannot connect to host "
        f"{host}:443 ssl:default [Connect call failed ('{bare}', 443, 0, 0)]"
    )


class TestMaskEmail:
    def test_org_label_never_survives(self):
        # single-label domain
        assert mask_email("sebastien.martin@example.com") == "s***n@***.com"
        # multi-label domain: the OLD masker left "entreprise.fr" standing
        assert mask_email("p.nom@mail.entreprise.fr") == "p***m@***.***.fr"

    def test_degrades_on_junk_input(self):
        # called during `raise` construction — must never raise (audit L:
        # non-str entry.data used to replace all_pools_unauthorized with AttributeError)
        assert mask_email(None) == "***"
        assert mask_email(42) == "***"
        assert mask_email("no-at-sign") == "***"

    def test_matches_coffee_component_output(self):
        """The two components must agree (audit 2026-11-05: they diverged)."""
        from custom_components.delonghi_coffee.logger import _mask_email as coffee_mask

        for addr in ("a@b.co", "user@mail.example.org", "x@localhost"):
            assert mask_email(addr) == coffee_mask(addr), addr


class TestMaskHost:
    def test_keeps_subnet_hides_host(self):
        assert mask_host("192.168.1.42") == "192.168.x.x"
        assert mask_host("10.99.88.77") == "10.99.x.x"

    def test_junk_fully_masked(self):
        assert mask_host("printer.local") == "**REDACTED-IP**"
        assert mask_host(None) == "**REDACTED-IP**"


class TestMaskSerial:
    def test_keeps_last_four(self):
        assert mask_serial("AC000W038925641") == "***********5641"

    def test_short_serials_fully_masked(self):
        assert mask_serial("SN1") == "****"
        assert mask_serial(None) == "****"


class TestSanitizeText:
    def test_scrubs_jwt(self):
        text = "AUTH failed with token eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc-_123"
        out = sanitize_text(text)
        assert "eyJ" not in out
        assert "***JWT***" in out

    def test_scrubs_lan_host_from_ws_url(self):
        text = "LAN WS connect to wss://192.168.1.42/ws/lan2lan failed: timeout"
        out = sanitize_text(text)
        assert "192.168.1.42" not in out
        assert "wss://**REDACTED**" in out

    def test_scrubs_bare_private_ip(self):
        assert "10.99.88.78" not in sanitize_text("peer reset by 10.99.88.78")

    def test_public_ip_not_scrubbed(self):
        # triage value: only LAN-range addresses are sensitive
        assert "8.8.8.8" in sanitize_text("dns server 8.8.8.8 unreachable")

    def test_scrubs_echoed_email(self):
        # Gigya errorMessage echoing the loginID (audit M1 degenerate path)
        out = sanitize_text("Gigya error 403042: loginID sebastien@corp.example.net invalid")
        assert "sebastien@corp.example.net" not in out
        assert "403042" in out  # the error CODE must survive — pool probing keys on it

    def test_scrubs_session_token_kv(self):
        out = sanitize_text('{"AuthToken": "st2.s.AbcDe_1234567890abcdefghijklmn"}')
        assert "AbcDe_1234567890" not in out

    def test_idempotent(self):
        once = sanitize_text("token eyJhbGciOiS.eyJzdWIiOiS.sig user a@b.co host wss://192.168.4.4/ws")
        assert sanitize_text(once) == once

    @pytest.mark.parametrize("junk", [None, 1, ["list"], {"k": "v"}])
    def test_non_str_input_coerced(self, junk):
        assert isinstance(sanitize_text(junk), str)


class TestKnownHostScrub:
    """Audit 2026-11-05 (F1, MEDIUM): _PRIV4_RE only covered IPv4 while
    validate_lan_host accepts ULA/LLA/::1 — the aiohttp error text repeats
    the host bare after the wss:// prefix and landed scrubbed-of-nothing in
    config-flow WARNINGs, UpdateFailed and the public diagnostics dump."""

    @pytest.mark.parametrize("host", [ULA, LLA_ZONED, ULA_SHORT, LOOPBACK6, "192.168.1.42"])
    def test_host_scrubbed_with_known_host_argument(self, host):
        out = sanitize_text(_aiohttp_host_error(host), host=host)
        assert host.split("%", 1)[0] not in out
        # IPv4 goes through the generic pattern (REDACTED-IP), IPv6 through
        # the literal host scrub (REDACTED-HOST) — either marker is correct.
        assert "REDACTED" in out
        # triage value preserved: the failure reason survives
        assert "Cannot connect to host" in out

    @pytest.mark.parametrize("host", [ULA, LLA_ZONED, LOOPBACK6])
    def test_host_scrubbed_via_registry_without_argument(self, host):
        """The SanitizeFilter and library-chained texts have no host in hand —
        connect_lan registers it at the attempt (audit F1/F2 intersection)."""
        redact.register_lan_host(host)
        out = sanitize_text(_aiohttp_host_error(host))
        assert host.split("%", 1)[0] not in out

    def test_zone_id_base_form_scrubbed(self):
        out = sanitize_text("Connect call failed ('fe80::99', 443, 0, 0)", host=LLA_ZONED)
        assert "fe80::99" not in out

    def test_bracketed_form_scrubbed(self):
        out = sanitize_text(f"invalid url wss://[{ULA}]/ws/lan2lan", host=ULA)
        assert ULA not in out

    def test_non_ip_host_never_drives_literal_scrub(self):
        # A user-typed hostname is not a secret and must not blind-replace
        # every "printer" in the text (validation rejects it separately).
        msg = "LAN host must be a numeric IP address, got 'printer'"
        assert "printer" in sanitize_text(msg, host="printer")

    def test_empty_or_junk_host_argument_is_noop(self):
        for junk in (None, "", "  ", 0, "wss://x"):
            assert sanitize_text("host fd12::7 stays", host=junk) == "host fd12::7 stays"

    @pytest.mark.parametrize("host", [ULA, LLA_ZONED, LOOPBACK6])
    def test_idempotent_with_host(self, host):
        once = sanitize_text(_aiohttp_host_error(host), host=host)
        assert sanitize_text(once, host=host) == once

    def test_registered_hosts_scrub_longest_first(self):
        # "::1" is a substring of nothing here, but both are registered:
        # a text carrying only the longer address must be fully scrubbed.
        redact.register_lan_host(LOOPBACK6)
        redact.register_lan_host(ULA)
        out = sanitize_text(f"peer {ULA} gone", host=None)
        assert ULA not in out and "fd12" not in out


class TestEmailLinearScan:
    """Audit 2026-11-05 (F3, MEDIUM): the email regex backtracks per start
    position — quadratic on long non-matching text, which the filter now
    renders for EVERY record including tracebacks. The linear scanner must
    reproduce the reference regex exactly."""

    CORPUS = (
        "a@b.co plain user@mail.example.org trailing x@localhost tag+u-1@sub.a-b.io "
        "a@b@@c.d @@nope @nodo tld@dot no@ at@no match. here "
        "user@dom.ain.tld.end done éè@café.fr 1.2.3.4@x.fr a@b.fr,c@d.fr"
    )

    def test_matches_reference_regex_on_corpus(self):
        import random
        import string

        random.seed(20261105)
        alphabet = string.ascii_letters + string.digits + "._+-@:,/é 0"
        cases = [self.CORPUS, *self.CORPUS.split()]
        for _ in range(400):
            cases.append("".join(random.choice(alphabet) for _ in range(random.randint(0, 60))))
        for text in cases:
            want = redact._EMAIL_REF_RE.sub(lambda m: mask_email(m.group(0)), text)
            assert redact._mask_emails(text) == want, repr(text)

    def test_sanitized_text_masks_emails(self):
        out = sanitize_text("loginID sebastien@corp.example.net invalid")
        assert "sebastien@corp.example.net" not in out
        assert "example" not in out

    def test_long_non_matching_text_is_linear(self):
        """50 kB used to cost ~9 s on the HA event loop (quadratic table)."""
        import time

        payload = "x" * 50_000 + " no address here"
        t0 = time.perf_counter()
        sanitize_text(payload)
        assert time.perf_counter() - t0 < 0.25  # generous; measured ~1-3 ms

    def test_sanitized_text_is_idempotent_on_email_shapes(self):
        # Multi-label domains only — see the single-label edge test below.
        corpus = "from a@b.fr cc user@mail.example.org x@localhost.co tag+u-1@sub.a-b.io"
        once = sanitize_text(corpus)
        assert sanitize_text(once) == once

    def test_single_label_domain_edge_is_preexisting(self):
        """Pinned behavior, identical to 8b2e607 (verified by running the
        pre-fix module): masking "tld@dot" leaves "t***d@dot", whose trailing
        "d@dot" still matches — a second pass over-masks one more char, then
        converges. Over-masking only (never a leak); the output format is
        pinned by the cross-component parity tests, so this is documented,
        not fixed."""
        once = sanitize_text("tld@dot")
        assert once == "t***d@dot"
        twice = sanitize_text(once)
        assert twice == "t****@dot"
        assert sanitize_text(twice) == twice


class TestLengthCap:
    """Audit 2026-11-05 (F3): unbounded input = unbounded work; the tail is
    dropped, never rendered unfiltered."""

    def test_truncates_past_cap(self):
        out = sanitize_text("A" * (redact._MAX_LEN + 1000) + " a@b.co")
        assert len(out) <= redact._MAX_LEN
        assert "TRUNCATED" in out
        assert "a@b.co" not in out  # tail dropped, not leaked

    def test_cap_is_idempotent(self):
        once = sanitize_text("A" * (redact._MAX_LEN + 1000))
        assert sanitize_text(once) == once


class TestDaedalusSanitizeFilter:
    """Audit 2026-11-05 (F2, MEDIUM): HA's DataUpdateCoordinator logs
    unexpected errors via logger.exception on THIS logger — without the
    filter the whole traceback, __cause__ reprs included, left unscrubbed."""

    def test_get_logger_installs_filter_idempotently(self):
        first = redact.get_logger("test.daedalus.idem")
        second = redact.get_logger("test.daedalus.idem")
        assert first is second
        assert sum(isinstance(f, redact.SanitizeFilter) for f in first.filters) == 1

    def test_filter_sanitizes_interpolated_args(self, caplog):
        import logging as _logging

        log = redact.get_logger("test.daedalus.filter")
        with caplog.at_level(_logging.ERROR, logger="test.daedalus.filter"):
            log.error(
                "LAN failed for %s: %s",
                ULA,
                "token eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc-_123",
            )
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "eyJ" not in joined

    def test_filter_sanitizes_traceback_and_rstrips(self, caplog):
        """F5: formatException renders "\n"+text[s:-1] — a kept trailing "\n"
        added an empty line per traceback and broke block readers."""
        import logging as _logging

        log = redact.get_logger("test.daedalus.filterexc")
        with caplog.at_level(_logging.ERROR, logger="test.daedalus.filterexc"):
            try:
                raise ValueError(f"machine said: wss://{ULA}/ws/lan2lan")
            except ValueError:
                log.exception("unexpected")
        rec = caplog.records[-1]
        assert rec.exc_info is not None
        assert rec.exc_text is not None
        assert ULA not in rec.exc_text
        assert not rec.exc_text.endswith("\n")

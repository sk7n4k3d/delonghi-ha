"""Test logging sanitization and rate tracking."""

import time
from unittest.mock import patch

import pytest

from custom_components.delonghi_coffee.logger import (
    _MAX_LEN,
    ApiTimer,
    RateLimitTracker,
    _mask_email,
    _mask_emails,
    get_diagnostic_dump,
    sanitize,
)


class TestSanitize:
    """Credential sanitization in log messages."""

    def test_mask_auth_token(self):
        """Ayla auth tokens must be masked."""
        msg = "Header: auth_token abc123def456_very_long_token"
        assert "abc123" not in sanitize(msg)
        assert "auth_token ***" in sanitize(msg)

    def test_mask_auth_token_query_form(self):
        """Audit 2026-11-05 (L1): the old space-only pattern let `auth_token=`
        and `auth_token:` through unmasked — today latent (the token travels
        in a header), but the mask must not depend on the separator."""
        for msg in (
            "Authorization: auth_token=SECRET1234567890",
            "auth_token: SECRET1234567890",
            "AUTH_TOKEN=SECRET1234567890",
        ):
            assert "SECRET1234567890" not in sanitize(msg), msg

    def test_mask_auth_token_json_form(self):
        """Audit 2026-11-05 (F4, LOW): the old rule broke on the JSON shape
        its own docstring advertised — the key's closing quote defeats
        `[:=]?` and the space after `:` defeats `\\S+`, leaving the token
        intact: {"auth_token": "s3cr3t..."} -> {"auth_token *** "s3cr3t..."}."""
        for msg, secret in (
            ('{"auth_token": "s3cr3tAAAAAAAA"}', "s3cr3tAAAAAAAA"),
            ("{'auth_token': 'ZZZZZZZZZZZZ'}", "ZZZZZZZZZZZZ"),  # dict repr
            ('{"AuthToken":"x","auth_token" : "s3cr3tAAAAAAAA"}', "s3cr3tAAAAAAAA"),
        ):
            out = sanitize(msg)
            assert secret not in out, msg
            assert sanitize(out) == out, msg  # idempotent

    def test_auth_token_value_only(self):
        """Mirror effect of F4: the old \\S+ over-masked past the token and
        lost triage context; the new rule masks only a token-shaped value."""
        assert sanitize("auth_token court") == "auth_token court"  # 5 chars, no token
        out = sanitize("/api?auth_token=s3cr3tAAAAAA&x=1")
        assert "s3cr3tAAAAAA" not in out
        assert "x=1" in out  # neighbouring query params survive
        assert "auth_token=***" in out

    def test_scrub_dsn_from_ayla_url(self):
        """Audit 2026-11-05 (M1): requests' HTTPError text embeds the full
        Ayla URL — the DSN rides exception messages into logs and dumps."""
        msg = "get_properties failed: 404 Client Error for url: https://ads.cloud.getayla.net/apiv1/dsns/AC000W038925641/properties.json"
        out = sanitize(msg)
        assert "AC000W038925641" not in out
        assert "/dsns/**REDACTED**/" in out
        # the rest of the URL keeps its triage value
        assert "properties.json" in out

    def test_sanitize_is_idempotent(self):
        msg = "auth_token=SECRET1234567890 a@b.co https://x/apiv1/dsns/AC000W1/props"
        once = sanitize(msg)
        assert sanitize(once) == once

    def test_sanitize_coerces_non_strings(self):
        """Exception objects are interpolated into log args constantly —
        sanitize() must never raise from inside a logging path."""
        assert sanitize(ValueError("boom")) == "boom"
        assert isinstance(sanitize(None), str)

    def test_mask_jwt(self):
        """JWT tokens (three dot-separated base64 segments) must be masked."""
        jwt = "eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiJodHRwczovL2ZpZGwiLCJzdWIiOiIxMjM0NTY3ODkwIn0.signature_here"
        msg = f"Token: {jwt}"
        result = sanitize(msg)
        assert "eyJ" not in result
        assert "***JWT***" in result

    def test_mask_email(self):
        """Email addresses must be partially masked."""
        msg = "Authenticating test.user@example.com"
        result = sanitize(msg)
        assert "test.user@example.com" not in result
        assert "@" in result  # Should still look like an email

    def test_mask_password(self):
        """Passwords in form data must be masked."""
        msg = "password=MyS3cretP4ss"
        result = sanitize(msg)
        assert "MyS3cretP4ss" not in result

    def test_mask_access_token(self):
        """Access tokens in JSON-like strings must be masked."""
        msg = "access_token=abcdef1234567890xyz"
        result = sanitize(msg)
        assert "abcdef1234567890xyz" not in result

    def test_mask_refresh_token(self):
        """Refresh tokens must be masked."""
        msg = 'refresh_token: "long_refresh_token_value_here"'
        result = sanitize(msg)
        assert "long_refresh_token_value_here" not in result

    def test_mask_lan_key(self):
        """LAN encryption keys must be masked."""
        msg = 'lanip_key="0123456789abcdef0123456789abcdef"'
        result = sanitize(msg)
        assert "0123456789abcdef" not in result

    def test_mask_app_secret(self):
        """App secrets must be masked."""
        msg = "app_secret=DLonghiCoffeeIdKit-HT6b0VNd4y6CSha9ivM5k8navLw"
        result = sanitize(msg)
        assert "HT6b0VNd4y6CSha9ivM5k8navLw" not in result

    def test_preserve_normal_text(self):
        """Normal log messages without credentials should be unchanged."""
        msg = "get_properties: 312 properties returned"
        assert sanitize(msg) == msg

    def test_preserve_dsn(self):
        """DSN is not a secret, should be preserved."""
        msg = "Fetching properties for AC000W038925641"
        assert sanitize(msg) == msg

    def test_mask_session_token(self):
        """Gigya session tokens must be masked."""
        msg = "sessionToken=st2.s.AcbDe_1234567890abcdefghijklmn"
        result = sanitize(msg)
        assert "AcbDe_1234567890" not in result

    def test_multiple_sensitive_values(self):
        """Multiple credentials in one message all get masked.

        Audit 2026-11-05 (F4): the auth_token value is a token-shaped run of
        8+ chars — the 6-char dummy this test used before relied on the old
        rule eating the whole rest of the word (over-masking the JSON form's
        real value left standing). Real Ayla tokens are far above the floor.
        """
        msg = "auth_token abc123def456 password=secret123 access_token=tok456789012345"
        result = sanitize(msg)
        assert "abc123def456" not in result
        assert "secret123" not in result
        assert "tok456789012345" not in result


class TestRateLimitTracker:
    """Sliding window rate counter."""

    def test_empty_rate(self):
        """New tracker starts at 0."""
        tracker = RateLimitTracker()
        assert tracker.current_rate == 0
        assert tracker.total_calls == 0

    def test_record_increments(self):
        """Each record() call increments the rate."""
        tracker = RateLimitTracker()
        tracker.record()
        tracker.record()
        tracker.record()
        assert tracker.current_rate == 3
        assert tracker.total_calls == 3

    def test_returns_current_rate(self):
        """record() returns the current rate."""
        tracker = RateLimitTracker()
        rate = tracker.record()
        assert rate == 1

    def test_window_expiry(self):
        """Old entries expire after the window."""
        tracker = RateLimitTracker(window_seconds=1)
        tracker.record()
        assert tracker.current_rate == 1
        time.sleep(1.1)
        assert tracker.current_rate == 0
        # total_calls should still count
        assert tracker.total_calls == 1


class TestApiTimer:
    """API call timing context manager."""

    def test_measures_elapsed(self):
        """Timer measures elapsed time."""
        timer = ApiTimer("test_op")
        with timer:
            time.sleep(0.01)
        assert timer.elapsed_ms >= 10

    def test_records_rate(self):
        """Timer records to rate tracker when provided."""
        tracker = RateLimitTracker()
        with ApiTimer("test_op", tracker):
            pass
        assert tracker.total_calls == 1


class TestDiagnosticDump:
    """Diagnostic dump builder."""

    def test_includes_property_count(self):
        """Dump includes total property count."""
        props = {"test_prop": {"value": "123"}}
        dump = get_diagnostic_dump(props, {}, {})
        assert dump["property_count"] == 1

    def test_masks_long_values(self):
        """Base64 values (recipes, monitor) show length not content."""
        props = {"d302_rec_2_espresso": {"value": "A" * 200}}
        dump = get_diagnostic_dump(props, {}, {})
        assert dump["properties"]["d302_rec_2_espresso"]["type"] == "base64"
        assert "value" not in dump["properties"]["d302_rec_2_espresso"]

    def test_short_strings_included(self):
        """Short string values (status, integers) are included."""
        props = {"d701_tot_bev_b": {"value": "1234"}}
        dump = get_diagnostic_dump(props, {}, {})
        assert dump["properties"]["d701_tot_bev_b"]["value"] == "1234"

    def test_null_values_marked(self):
        """Null values are marked as type null."""
        props = {"app_data_request": {"value": None}}
        dump = get_diagnostic_dump(props, {}, {})
        assert dump["properties"]["app_data_request"]["type"] == "null"

    def test_counters_included(self):
        """Counter data is included in dump."""
        counters = {"espresso": 300, "total_beverages": 1234}
        dump = get_diagnostic_dump({}, counters, {})
        assert dump["counters"] == counters

    def test_status_included(self):
        """Machine status data is included."""
        status = {"machine_state": "Ready", "status": "RUN", "alarms": []}
        dump = get_diagnostic_dump({}, {}, status)
        assert dump["machine_state"] == "Ready"
        assert dump["cloud_status"] == "RUN"

    def test_json_value_reported_by_length(self):
        """Values starting with '{' are marked as JSON with length — covers 158."""
        payload = '{"recipe": "espresso"}'
        dump = get_diagnostic_dump({"d302_rec_2_espresso": {"value": payload}}, {}, {})
        entry = dump["properties"]["d302_rec_2_espresso"]
        assert entry["type"] == "json"
        assert entry["length"] == len(payload)
        assert "value" not in entry

    def test_non_string_non_null_value(self):
        """Int/list/dict values report type + value — covers 165."""
        props = {
            "int_prop": {"value": 42},
            "list_prop": {"value": [1, 2, 3]},
        }
        dump = get_diagnostic_dump(props, {}, {})
        assert dump["properties"]["int_prop"] == {"type": "int", "value": 42}
        assert dump["properties"]["list_prop"] == {"type": "list", "value": [1, 2, 3]}


class TestSanitizeLogFilter:
    """Audit 2026-11-05 (L3): masking must be inductive — a logging.Filter
    applied to every module logger, not hand-wrapped call-site by call-site.
    """

    def test_get_logger_installs_filter_idempotently(self):
        from custom_components.delonghi_coffee.logger import SanitizeFilter, get_logger

        first = get_logger("test.delonghi.idem")
        second = get_logger("test.delonghi.idem")
        assert first is second
        assert sum(isinstance(f, SanitizeFilter) for f in first.filters) == 1

    def test_filter_sanitizes_interpolated_args(self, caplog):
        """A `%s` added tomorrow is masked without anyone remembering to
        wrap it — the whole point of the filter."""
        import logging as _logging

        from custom_components.delonghi_coffee.logger import get_logger

        log = get_logger("test.delonghi.filter")
        with caplog.at_level(_logging.DEBUG, logger="test.delonghi.filter"):
            log.error("Gigya failed for %s with %s", "user@corp.example.org", "auth_token=SUPERSECRET123")
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "user@corp.example.org" not in joined
        assert "SUPERSECRET123" not in joined

    def test_filter_sanitizes_traceback_but_keeps_exc_info(self, caplog):
        """The formatter only renders exc_info when exc_text is empty — the
        filter pre-renders a sanitized exc_text while leaving exc_info
        inspectable (HA UIs and tests read it)."""
        import logging as _logging

        from custom_components.delonghi_coffee.logger import get_logger

        log = get_logger("test.delonghi.filterexc")
        with caplog.at_level(_logging.ERROR, logger="test.delonghi.filterexc"):
            try:
                raise RuntimeError("boom for https://ads/apiv1/dsns/AC000W038925641/properties.json")
            except RuntimeError:
                log.exception("refresh failed")
        rec = caplog.records[-1]
        assert rec.exc_info is not None, "exc_info must survive for introspection"
        assert "AC000W038925641" not in (rec.exc_text or "")
        assert "/dsns/**REDACTED**/" in rec.exc_text

    def test_exc_text_has_no_trailing_newline(self, caplog):
        """Audit 2026-11-05 (F5, LOW): traceback.format_exception keeps its
        final \\n (formatException retranchait s[:-1]) — one empty line per
        traceback broke "one block = one entry" readers (grep -A, traceback
        parsers)."""
        import logging as _logging

        from custom_components.delonghi_coffee.logger import get_logger

        log = get_logger("test.delonghi.nl")
        with caplog.at_level(_logging.ERROR, logger="test.delonghi.nl"):
            try:
                raise RuntimeError("boom")
            except RuntimeError:
                log.exception("ctx")
        rec = caplog.records[-1]
        assert rec.exc_text and not rec.exc_text.endswith("\n")

    @pytest.mark.parametrize(
        ("pkg_name", "filter_module"),
        [
            ("custom_components.delonghi_coffee", "custom_components.delonghi_coffee.logger"),
            # Audit 2026-11-05 (F2, MEDIUM): the guard used to balance only
            # the coffee package — the Daedalus coordinator logger is the
            # one HA's update_coordinator uses for logger.exception.
            ("custom_components.delonghi_daedalus", "custom_components.delonghi_daedalus.redact"),
        ],
    )
    def test_module_loggers_all_filtered(self, pkg_name, filter_module):
        """Every module of a component must go through get_logger — one
        forgotten logging.getLogger() silently re-opens the leak."""
        import importlib
        import pkgutil

        pkg = importlib.import_module(pkg_name)
        filter_cls = importlib.import_module(filter_module).SanitizeFilter

        for mod in pkgutil.iter_modules(pkg.__path__):
            name = f"{pkg.__name__}.{mod.name}"
            if name.rsplit(".", 1)[-1] in {"logger", "redact"}:
                continue
            module = importlib.import_module(name)
            log = getattr(module, "_LOGGER", None)
            if log is not None:
                assert any(isinstance(f, filter_cls) for f in log.filters), name


class TestEmailLinearScan:
    """Audit 2026-11-05 (F3, MEDIUM): the e-mail regex backtracked per start
    position — quadratic on long non-matching text (n=50 000 → ~9 s), and the
    filter now runs it on every record including machine-piloted tracebacks.
    The scan is now literal (find('@') + extension), the cap bounds input."""

    def test_linear_scan_matches_reference_regex(self):
        import random
        import string

        from custom_components.delonghi_coffee.logger import _EMAIL_REF_RE

        random.seed(20261105)
        alphabet = string.ascii_letters + string.digits + "._+-@:,/é 0"
        corpus = (
            "a@b.co user@mail.example.org x@localhost tag+u-1@sub.a-b.io "
            "a@b@@c.d @@nope @nodo tld@dot no@ at@no user@dom.ain.tld.end "
            "éè@café.fr 1.2.3.4@x.fr a@b.fr,c@d.fr"
        )
        cases = [corpus, *corpus.split()]
        for _ in range(400):
            cases.append("".join(random.choice(alphabet) for _ in range(random.randint(0, 60))))
        for text in cases:
            want = _EMAIL_REF_RE.sub(lambda m: _mask_email(m.group(0)), text)
            assert _mask_emails(text) == want, repr(text)

    def test_long_non_matching_text_is_linear(self):
        import time

        payload = "x" * 50_000 + " and no address"
        t0 = time.perf_counter()
        out = sanitize(payload)
        elapsed = time.perf_counter() - t0
        assert out == payload
        assert elapsed < 0.25  # was ~8.7 s pre-fix on the same input

    def test_sanitized_output_still_idempotent_with_emails(self):
        msg = "mail from s***n@***.fr reply-to a@b.fr? password=hunter22very"
        once = sanitize(msg)
        assert sanitize(once) == once

    def test_input_capped(self):
        """Over-long input is truncated (tail dropped — never rendered
        unfiltered), and the marker keeps the pass idempotent."""
        out = sanitize("B" * (_MAX_LEN + 5000) + " secret=auth_token=s3cr3tAAAAAA")
        assert len(out) == _MAX_LEN
        assert "TRUNCATED" in out
        assert "s3cr3tAAAAAA" not in out
        assert sanitize(out) == out


class TestMaskEmail:
    """Direct coverage of the email masking helper."""

    def test_short_local_part_fully_masked(self):
        """Local part of two chars or fewer → pure asterisks (line 38)."""
        assert _mask_email("ab@example.com") == "**@***.com"
        assert _mask_email("a@example.com") == "*@***.com"

    def test_empty_local_part_masked_empty(self):
        """Empty local part still masks to an empty string."""
        assert _mask_email("@example.com") == "@***.com"

    def test_every_domain_label_but_tld_masked(self):
        """Audit 2026-11-05: masking only the first label left the org label
        standing in multi-label domains — every label but the LAST is hidden
        (no public-suffix list: `co.uk` is fully masked too, stricter)."""
        assert _mask_email("someone@io.local") == "s***e@***.local"
        assert _mask_email("p.nom@mail.entreprise.fr") == "p***m@***.***.fr"
        assert _mask_email("a.b@sub.domain.co.uk") == "a***b@***.***.***.uk"

    def test_domain_without_dot_returned_as_is(self):
        """No dot in domain → fallback branch: no organisation label exists,
        but the local part is still masked (never a silent no-op)."""
        # Single-segment hosts can't be split → masked_domain = domain.
        assert _mask_email("sebastien@localhost") == "s***n@localhost"

    def test_non_string_input_does_not_raise(self):
        """Called during ``raise`` construction on entry.data values — a
        non-str must degrade, not replace the auth error with AttributeError."""
        assert _mask_email(None) == "***"
        assert "@" not in _mask_email(12345)

    def test_long_local_and_long_domain(self):
        """Regression: full masking still works when both parts are long."""
        result = _mask_email("sebastien.homelab@devlabz.eu")
        # Local gets first+***+last; every domain label but the TLD is hidden.
        assert result.startswith("s***b@")
        assert result.endswith("@***.eu")
        assert "devlabz" not in result


class TestRateLimitTrackerAdvanced:
    """Cover the expiry-inside-record and warning branches."""

    def test_expiry_inside_record(self):
        """record() prunes old entries — covers line 77."""
        tracker = RateLimitTracker(window_seconds=1)
        tracker.record()
        time.sleep(1.1)
        # This new record() must prune the earlier call from the deque.
        rate = tracker.record()
        assert rate == 1
        assert tracker.total_calls == 2

    def test_warning_emitted_once_at_threshold(self):
        """Calls above threshold trigger _LOGGER.warning — covers 80-86."""
        tracker = RateLimitTracker()
        # Sanity: default threshold is 200.
        assert tracker._warn_threshold == 200

        with patch("custom_components.delonghi_coffee.logger._LOGGER") as mock_logger:
            for _ in range(205):
                tracker.record()

        # Threshold crossed exactly once → single warning.
        assert mock_logger.warning.call_count == 1
        args = mock_logger.warning.call_args.args
        # Ensure the numeric rate and threshold landed in the message.
        assert args[1] >= 200
        assert args[3] == 200

    def test_warning_state_resets_when_rate_drops(self):
        """After rate falls below threshold - 20, warning is re-armed."""
        tracker = RateLimitTracker(window_seconds=1)
        # Force the warning flag true so the reset branch runs.
        tracker._warned = True
        # A single record() with rate well below threshold - 20 resets it.
        tracker.record()
        assert tracker._warned is False


class TestApiTimerSlowResponse:
    """Slow-response warning branch in ApiTimer.__exit__."""

    def test_slow_response_logs_warning(self):
        """Duration > 5000ms triggers warning — covers line 130."""
        fake_times = iter([0.0, 10.0])  # start, then __exit__ → 10_000 ms.

        with (
            patch(
                "custom_components.delonghi_coffee.logger.time.monotonic",
                side_effect=lambda: next(fake_times),
            ),
            patch("custom_components.delonghi_coffee.logger._LOGGER") as mock_logger,
            ApiTimer("slow_call"),
        ):
            pass

        mock_logger.warning.assert_called_once()
        msg_args = mock_logger.warning.call_args.args
        assert "slow_call" in msg_args[1]
        # Duration is the second positional → ~10000 ms.
        assert msg_args[2] >= 5000

    def test_exception_path_logged_as_debug(self):
        """Exception during ApiTimer run is logged as debug."""
        with patch("custom_components.delonghi_coffee.logger._LOGGER") as mock_logger:
            try:
                with ApiTimer("failing_call"):
                    raise RuntimeError("boom")
            except RuntimeError:
                pass

        mock_logger.debug.assert_called_once()
        msg_args = mock_logger.debug.call_args.args
        assert "failing_call" in msg_args[1]

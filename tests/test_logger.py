"""Test logging sanitization and rate tracking."""

import time
from unittest.mock import patch

from custom_components.delonghi_coffee.logger import (
    ApiTimer,
    RateLimitTracker,
    _mask_email,
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
        """Multiple credentials in one message all get masked."""
        msg = "auth_token abc123 password=secret123 access_token=tok456789012345"
        result = sanitize(msg)
        assert "abc123" not in result
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


class TestMaskEmail:
    """Direct coverage of the email masking helper."""

    def test_short_local_part_fully_masked(self):
        """Local part of two chars or fewer → pure asterisks (line 38)."""
        assert _mask_email("ab@example.com") == "**@e***.com"
        assert _mask_email("a@example.com") == "*@e***.com"

    def test_empty_local_part_masked_empty(self):
        """Empty local part still masks to an empty string."""
        assert _mask_email("@example.com") == "@e***.com"

    def test_short_domain_prefix_kept_verbatim(self):
        """Domain with a short prefix (≤2 chars) is not masked — covers 45."""
        assert _mask_email("someone@io.local") == "s***e@io.local"

    def test_domain_without_dot_returned_as_is(self):
        """No dot in domain → fallback branch on line 45."""
        # Single-segment domains can't be split → masked_domain = domain.
        assert _mask_email("sebastien@localhost") == "s***n@localhost"

    def test_long_local_and_long_domain(self):
        """Regression: full masking still works when both parts are long."""
        result = _mask_email("sebastien.homelab@devlabz.eu")
        # Local gets first+***+last; domain gets first+"***."+rest.
        assert result.startswith("s***b@")
        assert result.endswith("@d***.eu")


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

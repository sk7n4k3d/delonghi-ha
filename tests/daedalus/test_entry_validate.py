"""Tests for entry.py validate_apikey_lengths startup self-check (H-daedalus-4)."""

from __future__ import annotations

import logging
from unittest.mock import patch

from custom_components.delonghi_daedalus import entry as entry_mod


def test_validate_apikey_lengths_passes_with_canonical_keys(caplog) -> None:
    """When const.GIGYA_API_KEYS holds the verbatim APK-extracted values,
    no error log is emitted (only debug)."""
    with caplog.at_level(logging.DEBUG, logger="custom_components.delonghi_daedalus.entry"):
        entry_mod._validate_apikey_lengths()
    assert "unexpected length" not in caplog.text
    # All three pools should have produced a debug confirm.
    assert "EU OK" in caplog.text or "Daedalus apiKey EU" in caplog.text


def test_validate_apikey_lengths_warns_on_truncated_key(caplog) -> None:
    """Simulate HACS mirror corruption: the EU pool is half-truncated. The
    self-check must emit an ERROR log naming the pool and the bad length.
    """
    bad = {"EU": "4_mXSplGaqrFT0H88", "EU_US": "X" * 66, "CH": "Y" * 66}
    with (
        patch.object(entry_mod, "GIGYA_API_KEYS", bad),
        caplog.at_level(logging.ERROR, logger="custom_components.delonghi_daedalus.entry"),
    ):
        entry_mod._validate_apikey_lengths()
    assert "EU" in caplog.text
    assert "unexpected length 17" in caplog.text
    assert "expected 24" in caplog.text
    # Non-broken pools must NOT be flagged
    assert "EU_US has unexpected" not in caplog.text


def test_validate_apikey_lengths_handles_empty_key(caplog) -> None:
    """Empty key shouldn't crash the helper — it must log gracefully."""
    bad = {"EU": "", "EU_US": "X" * 66, "CH": "Y" * 66}
    with (
        patch.object(entry_mod, "GIGYA_API_KEYS", bad),
        caplog.at_level(logging.ERROR, logger="custom_components.delonghi_daedalus.entry"),
    ):
        entry_mod._validate_apikey_lengths()
    assert "<empty>" in caplog.text
    assert "unexpected length 0" in caplog.text

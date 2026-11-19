"""Tests for entry.py validate_apikey_lengths startup self-check (H-daedalus-4)."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.delonghi_daedalus import DOMAIN, async_setup_entry
from custom_components.delonghi_daedalus import entry as entry_mod
from custom_components.delonghi_daedalus.device import DaedalusDevice


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


def test_setup_entry_installs_device_listener_and_removes_old_address_sensor() -> None:
    hass = MagicMock(data={})
    config_entry = MagicMock(entry_id="test-entry", data={})
    coordinator = MagicMock(
        hass=hass,
        entry=config_entry,
        serial_number="SN-test",
        data={},
        async_config_entry_first_refresh=AsyncMock(),
        async_shutdown=AsyncMock(),
    )
    coordinator.device = DaedalusDevice(coordinator)
    devices = MagicMock()
    devices.async_get_device.return_value = None
    entities = MagicMock()
    old_sensor = SimpleNamespace(
        entity_id="sensor.old_lan_ip", domain="sensor", platform=DOMAIN, unique_id="SN-test_cloud_LanIpAddress"
    )

    async def forward(*_args):
        coordinator.async_config_entry_first_refresh.assert_awaited_once()
        coordinator.async_add_listener.assert_called_once()
        config_entry.async_on_unload.assert_called_once_with(coordinator.async_add_listener.return_value)
        entities.async_remove.assert_called_once_with(old_sensor.entity_id)
        assert hass.data[DOMAIN][config_entry.entry_id] is coordinator

    hass.config_entries.async_forward_entry_setups = AsyncMock(side_effect=forward)
    with (
        patch.object(entry_mod, "DaedalusCoordinator", return_value=coordinator),
        patch("custom_components.delonghi_daedalus.device.device_registry.async_get", return_value=devices),
        patch("custom_components.delonghi_daedalus.device.entity_registry.async_get", return_value=entities),
        patch(
            "custom_components.delonghi_daedalus.device.entity_registry.async_entries_for_config_entry",
            return_value=[old_sensor],
        ),
    ):
        assert asyncio.run(async_setup_entry(hass, config_entry)) is True
    hass.config_entries.async_forward_entry_setups.assert_awaited_once_with(config_entry, entry_mod.PLATFORMS)

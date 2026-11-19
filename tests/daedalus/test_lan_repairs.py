"""Check that certificate repairs carry no raw machine identifiers."""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.delonghi_daedalus import lan_repairs
from custom_components.delonghi_daedalus.const import CONF_SERIAL_NUMBER, DOMAIN


def test_certificate_repair_has_masked_serial_and_entry_scoped_cleanup(monkeypatch):
    registry = MagicMock()
    monkeypatch.setattr(lan_repairs, "ir", registry)
    hass = MagicMock()
    hass.config_entries.async_get_entry.return_value.data = {CONF_SERIAL_NUMBER: "OLD-SN1234"}

    lan_repairs.async_raise_lan_issue(hass, "test-entry")
    registry.async_create_issue.assert_called_once_with(
        hass,
        DOMAIN,
        "verify_lan_test-entry",
        is_fixable=False,
        severity=registry.IssueSeverity.WARNING,
        translation_key="verify_lan",
        translation_placeholders={"serial": "******1234"},
    )

    lan_repairs.async_clear_lan_issue(hass, "test-entry")
    registry.async_delete_issue.assert_called_once_with(hass, DOMAIN, "verify_lan_test-entry")

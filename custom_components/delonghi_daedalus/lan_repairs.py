"""Repairs for Daedalus entries that need certificate verification."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import CONF_SERIAL_NUMBER, DOMAIN
from .redact import mask_serial


def async_raise_lan_issue(hass: HomeAssistant, entry_id: str) -> None:
    """Point the user to the reconfigure flow that can verify the certificate."""
    entry = hass.config_entries.async_get_entry(entry_id)
    serial = entry.data.get(CONF_SERIAL_NUMBER, "") if entry is not None else ""
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"verify_lan_{entry_id}",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="verify_lan",
        translation_placeholders={"serial": mask_serial(serial)},
    )


def async_clear_lan_issue(hass: HomeAssistant, entry_id: str) -> None:
    """Clear the repair once the entry is verified, cloud-only, or removed."""
    ir.async_delete_issue(hass, DOMAIN, f"verify_lan_{entry_id}")

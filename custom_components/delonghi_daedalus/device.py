"""Device registry metadata and cleanup for Daedalus."""

from __future__ import annotations

import re
from hashlib import sha256
from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers import device_registry, entity_registry
from homeassistant.helpers.device_registry import DeviceInfo

from .cloud import machine_model_code
from .cloud_keys import is_sensitive_cloud_key, lookup_cloud_key
from .const import CONF_MACHINE_NAME, CONF_MODEL, DOMAIN, MANUFACTURER

if TYPE_CHECKING:
    from .coordinator import DaedalusCoordinator

_VERSION = re.compile(r"[vV]?\d{1,4}(?:\.\d{1,4}){1,3}(?:[-+][a-zA-Z0-9.-]{1,20})?", re.ASCII)
_AROMATIC_MODEL = "PrimaDonna Aromatic ECAM630.70"
_DEFAULT_NAME = f"{MANUFACTURER} My Coffee Lounge"


class DaedalusDevice:
    """Keep device information across partial shadow updates."""

    def __init__(self, coordinator: DaedalusCoordinator) -> None:
        self._coordinator = coordinator
        digest = sha256(f"{DOMAIN}:{coordinator.serial_number}".encode()).hexdigest()
        self._identifiers = {(DOMAIN, digest)}
        self._capabilities: dict | None = None
        self._metadata: dict[str, str] = {}
        self._display_firmware: str | None = None
        self._mainboard_firmware: str | None = None
        self._mainboard_is_primary = False

    def _safe_value(self, text: str | None, cloud: dict) -> str | None:
        if not text:
            return None
        coordinator = self._coordinator
        for private in (
            coordinator.serial_number,
            coordinator.entry.data.get(CONF_MACHINE_NAME),
            cloud.get("machine_name"),
        ):
            if isinstance(private, str) and private and private.casefold() in text.casefold():
                return None
        return text

    def _firmware(self, value: object, cloud: dict) -> str | None:
        if isinstance(value, str) and _VERSION.fullmatch(text := value.strip()):
            return self._safe_value(text, cloud)
        return None

    def _refresh_metadata(self) -> None:
        coordinator = self._coordinator
        cloud = (coordinator.data or {}).get("cloud") or {}
        capabilities = cloud.get("capabilities_flat")
        model = self._safe_value(machine_model_code(coordinator.entry.data.get(CONF_MODEL)), cloud)
        if capabilities is not None and capabilities is not self._capabilities:
            self._capabilities = capabilities
            for key, value in capabilities.items():
                meta = lookup_cloud_key(key)
                if meta is None:
                    continue
                if meta.key == "machine_model" and not model:
                    model = self._safe_value(machine_model_code(value), cloud)
                elif meta.key == "display_firmware":
                    self._display_firmware = self._firmware(value, cloud) or self._display_firmware
                elif meta.key == "mainboard_firmware":
                    self._mainboard_firmware = self._firmware(value, cloud) or self._mainboard_firmware
        if model:
            if model.upper().replace(".", "") == "ECAM63070":
                model = _AROMATIC_MODEL
            self._metadata.update(model=model, name=f"{MANUFACTURER} {model}")
        if self._display_firmware:
            self._metadata["sw_version"] = self._display_firmware
        elif self._mainboard_firmware and (self._mainboard_is_primary or "sw_version" not in self._metadata):
            self._metadata["sw_version"] = self._mainboard_firmware
            self._mainboard_is_primary = True

    def info(self) -> DeviceInfo:
        self._refresh_metadata()
        fields = self._metadata.copy()
        fields["name"] = fields.get("name") or _DEFAULT_NAME
        return DeviceInfo(identifiers=self._identifiers, manufacturer=MANUFACTURER, **fields)

    @callback
    def async_setup(self) -> None:
        """Watch for device metadata and remove entities for private cloud fields."""
        coordinator = self._coordinator
        entry = coordinator.entry
        registry = device_registry.async_get(coordinator.hass)
        if existing := registry.async_get_device(identifiers=self._identifiers):
            for field in ("name", "model", "sw_version"):
                if value := getattr(existing, field):
                    self._metadata[field] = value

        self._async_remove_sensitive_cloud_entities()

        @callback
        def _update_metadata() -> None:
            self._refresh_metadata()
            device = registry.async_get_device(identifiers=self._identifiers)
            if device is None:
                return
            changes = {key: value for key, value in self._metadata.items() if getattr(device, key) != value}
            if changes:
                registry.async_update_device(device.id, **changes)

        _update_metadata()
        entry.async_on_unload(coordinator.async_add_listener(_update_metadata))

    @callback
    def _async_remove_sensitive_cloud_entities(self) -> None:
        coordinator = self._coordinator
        registry = entity_registry.async_get(coordinator.hass)
        prefixes = (f"{coordinator.serial_number}_cloud_", f"{coordinator.serial_number}_cloudcap_")
        for entity in entity_registry.async_entries_for_config_entry(registry, coordinator.entry.entry_id):
            if entity.platform != DOMAIN or entity.domain not in {"sensor", "binary_sensor"}:
                continue
            if any(
                entity.unique_id.startswith(prefix) and is_sensitive_cloud_key(entity.unique_id[len(prefix) :])
                for prefix in prefixes
            ):
                registry.async_remove(entity.entity_id)

"""Button platform for De'Longhi Coffee — one button per beverage."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import DeLonghiApi, DeLonghiApiError, DeLonghiAuthError
from .const import BEVERAGES, DOMAIN, resolve_beverage_meta
from .coordinator import DeLonghiCoordinator
from .lan import run_lan_diagnostic
from .sensor import _device_info

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up button entities."""
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    api: DeLonghiApi = data["api"]
    coordinator: DeLonghiCoordinator = data["coordinator"]
    dsn: str = data["dsn"]
    model: str = data["model"]
    device_name: str = data["device_name"]
    sw_version: str | None = data.get("sw_version")

    known_keys: set[str] = set()

    def _add_buttons(beverage_keys: list[str]) -> None:
        """Create button entities for newly discovered beverages."""
        new_entities: list[ButtonEntity] = []
        unknown_keys: list[str] = []
        for bev_key in beverage_keys:
            if bev_key in known_keys:
                continue
            known_keys.add(bev_key)
            meta, is_known = resolve_beverage_meta(bev_key, coordinator.custom_recipe_names)
            if not is_known:
                unknown_keys.append(bev_key)
            new_entities.append(
                DeLonghiBrewButton(api, coordinator, dsn, model, device_name, sw_version, bev_key, meta)
            )
        if new_entities:
            _LOGGER.info("Adding %d brew buttons", len(new_entities))
            async_add_entities(new_entities)
        if unknown_keys:
            _LOGGER.warning(
                "Unknown beverage keys — buttons created with default name/icon: %s. "
                "Please open an issue so these can be added to BEVERAGES.",
                unknown_keys,
            )

    # Add static control buttons
    async_add_entities(
        [
            DeLonghiCancelButton(api, coordinator, dsn, model, device_name, sw_version),
            DeLonghiSyncButton(api, coordinator, dsn, model, device_name, sw_version),
            DeLonghiLanDiagnosticButton(api, coordinator, dsn, model, device_name, sw_version),
        ]
    )

    # Add buttons for currently known beverages
    _add_buttons(coordinator.beverages)

    # If no beverages found yet, listen for coordinator updates
    # and add buttons when they're discovered on the next full refresh
    if not coordinator.beverages:
        _LOGGER.warning("No beverages discovered yet — will retry on next refresh")

        unsub: Callable[[], None] | None = None

        def _on_update() -> None:
            nonlocal unsub
            if coordinator.beverages:
                _add_buttons(coordinator.beverages)
                # Self-unregister once we've added the buttons
                if unsub is not None:
                    unsub()
                    unsub = None

        unsub = coordinator.async_add_listener(_on_update)
        # Ensure listener is removed on entry unload to avoid leak across reloads
        entry.async_on_unload(lambda: unsub() if unsub is not None else None)


class DeLonghiBrewButton(CoordinatorEntity[DeLonghiCoordinator], ButtonEntity):
    """Button to brew a specific beverage."""

    def __init__(
        self,
        api: DeLonghiApi,
        coordinator: DeLonghiCoordinator,
        dsn: str,
        model: str,
        device_name: str,
        sw_version: str | None,
        beverage_key: str,
        meta: dict[str, str],
    ) -> None:
        super().__init__(coordinator)
        self._api = api
        self._dsn = dsn
        self._beverage_key = beverage_key
        self._attr_unique_id = f"{dsn}_brew_{beverage_key}"
        self._attr_has_entity_name = True
        # Use translation_key when the resolved meta still matches the BEVERAGES
        # default (translated label). If the user has set a custom recipe name —
        # meta['name'] diverges from BEVERAGES[key]['name'] — fall back to
        # _attr_name so the label reflects the user's override instead of the
        # generic "Custom Drink N" translation.
        if beverage_key in BEVERAGES and meta["name"] == BEVERAGES[beverage_key]["name"]:
            self._attr_translation_key = f"brew_{beverage_key}"
        else:
            self._attr_name = meta["name"]
        self._attr_icon = meta["icon"]
        self._attr_device_info = _device_info(dsn, model, device_name, sw_version)

    @property
    def available(self) -> bool:
        """Hide the button when the coordinator is unhealthy or the machine can't brew.

        Pressing while offline / off produces silent command drops on the cloud
        side and leaves the user wondering why nothing happens. Reading
        coordinator.data defensively: early in setup it can be None.
        """
        if not super().available:
            return False
        data = self.coordinator.data or {}
        state = data.get("machine_state", "Unknown")
        return state not in ("Off", "Sleep")

    async def async_press(self) -> None:
        """Brew the beverage using the selected profile's recipe."""
        profile = self.coordinator.selected_profile or 1
        _LOGGER.info("Brewing %s on %s (profile %d)", self._beverage_key, self._dsn, profile)
        try:
            await self.hass.async_add_executor_job(self._api.brew_beverage, self._dsn, self._beverage_key, profile)
        except (DeLonghiApiError, DeLonghiAuthError) as err:
            raise HomeAssistantError(f"Failed to brew {self._beverage_key}: {err}") from err

        # Brew cycles last ~10-30s. Switch the coordinator to a 5s polling
        # interval for the next 90s so machine_state transitions through
        # Pre-brewing → Brewing → Frothing milk → Rinsing → Ready actually
        # surface to automations. Without this the default 60s poll misses
        # them and watchers never see anything happen.
        # Older coordinator stubs (test harness without request_fast_poll)
        # are tolerated by the AttributeError suppression.
        with contextlib.suppress(AttributeError):
            self.coordinator.request_fast_poll(duration_s=90.0, interval_s=5.0)


class DeLonghiCancelButton(CoordinatorEntity[DeLonghiCoordinator], ButtonEntity):
    """Button to cancel the current brew operation."""

    def __init__(
        self,
        api: DeLonghiApi,
        coordinator: DeLonghiCoordinator,
        dsn: str,
        model: str,
        device_name: str,
        sw_version: str | None,
    ) -> None:
        super().__init__(coordinator)
        self._api = api
        self._dsn = dsn
        self._attr_unique_id = f"{dsn}_cancel_brew"
        self._attr_has_entity_name = True
        self._attr_translation_key = "cancel_brew"
        self._attr_icon = "mdi:stop-circle-outline"
        self._attr_device_info = _device_info(dsn, model, device_name, sw_version)

    @property
    def available(self) -> bool:
        """Only available when machine is actively brewing."""
        state = self.coordinator.data.get("machine_state", "Unknown")
        return state == "Brewing"

    async def async_press(self) -> None:
        """Cancel current operation."""
        try:
            await self.hass.async_add_executor_job(self._api.cancel_brew, self._dsn)
        except (DeLonghiApiError, DeLonghiAuthError) as err:
            raise HomeAssistantError(f"Failed to cancel: {err}") from err


class DeLonghiSyncButton(CoordinatorEntity[DeLonghiCoordinator], ButtonEntity):
    """Button to force machine to sync its recipes to the cloud."""

    def __init__(
        self,
        api: DeLonghiApi,
        coordinator: DeLonghiCoordinator,
        dsn: str,
        model: str,
        device_name: str,
        sw_version: str | None,
    ) -> None:
        super().__init__(coordinator)
        self._api = api
        self._dsn = dsn
        self._attr_unique_id = f"{dsn}_sync_recipes"
        self._attr_has_entity_name = True
        self._attr_translation_key = "sync_recipes"
        self._attr_icon = "mdi:cloud-sync-outline"
        self._attr_device_info = _device_info(dsn, model, device_name, sw_version)

    @property
    def available(self) -> bool:
        """Only allow a sync when the coordinator has fresh data from the cloud."""
        if not super().available:
            return False
        data = self.coordinator.data or {}
        state = data.get("machine_state", "Unknown")
        return state not in ("Off", "Sleep")

    async def async_press(self) -> None:
        """Force sync recipes for the selected profile."""
        profile = self.coordinator.selected_profile or 1
        try:
            await self.hass.async_add_executor_job(self._api.sync_recipes, self._dsn, profile)
        except (DeLonghiApiError, DeLonghiAuthError) as err:
            raise HomeAssistantError(f"Failed to sync recipes: {err}") from err


class DeLonghiLanDiagnosticButton(CoordinatorEntity[DeLonghiCoordinator], ButtonEntity):
    """Diagnostic button that runs a full LAN pipeline check.

    Exercises cloud LAN config fetch, embedded server boot, handshake,
    app->device command poll and device->app datapoint push end-to-end
    against an in-process loopback instance. Every step is logged under
    ``custom_components.delonghi_coffee.lan`` so users collecting a
    transcript only need to enable that namespace.

    Any failure is caught and reported via logs and an info-level
    HomeAssistantError so the button press never raises into the HA
    supervisor. See issue #10.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        api: DeLonghiApi,
        coordinator: DeLonghiCoordinator,
        dsn: str,
        model: str,
        device_name: str,
        sw_version: str | None,
    ) -> None:
        super().__init__(coordinator)
        self._api = api
        self._dsn = dsn
        self._attr_unique_id = f"{dsn}_run_lan_diagnostic"
        self._attr_has_entity_name = True
        self._attr_translation_key = "run_lan_diagnostic"
        self._attr_icon = "mdi:lan-pending"
        self._attr_device_info = _device_info(dsn, model, device_name, sw_version)

    async def async_press(self) -> None:
        """Fetch LAN config, spin up the pipeline, log everything."""
        import requests as _requests

        _LOGGER.info("LAN diagnostic requested for %s", self._dsn)
        try:
            lan_config = await self.hass.async_add_executor_job(self._api.get_lan_config, self._dsn)
        except (
            DeLonghiApiError,
            DeLonghiAuthError,
            _requests.RequestException,
            TimeoutError,
            ValueError,
            KeyError,
        ) as err:
            _LOGGER.exception("LAN diagnostic: get_lan_config failed")
            raise HomeAssistantError(f"LAN diagnostic cloud fetch failed: {err}") from err

        result = await run_lan_diagnostic(
            lan_key=lan_config.get("lanip_key"),
            lan_ip=lan_config.get("lan_ip"),
            dsn=self._dsn,
        )
        if not result.success:
            _LOGGER.warning(
                "LAN diagnostic finished: %s (details=%s)",
                result.summary(),
                result.details,
            )
            raise HomeAssistantError(f"LAN diagnostic failed: {result.summary()}")
        _LOGGER.info("LAN diagnostic finished: %s", result.summary())

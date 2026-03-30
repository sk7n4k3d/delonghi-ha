"""Sensor platform for De'Longhi Coffee."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import DeLonghiCoordinator

_LOGGER = logging.getLogger(__name__)

COUNTER_SENSORS: dict[str, dict[str, str]] = {
    # Core counters
    "total_beverages": {"name": "Total Beverages", "icon": "mdi:coffee", "unit": "cups"},
    "total_espressos": {"name": "Total Espressos", "icon": "mdi:coffee", "unit": "cups"},
    "espresso": {"name": "Espressos", "icon": "mdi:coffee", "unit": "cups"},
    "coffee": {"name": "Coffees", "icon": "mdi:coffee", "unit": "cups"},
    "long_coffee": {"name": "Long Coffees", "icon": "mdi:coffee-outline", "unit": "cups"},
    "doppio": {"name": "Doppios", "icon": "mdi:coffee", "unit": "cups"},
    "americano": {"name": "Americanos", "icon": "mdi:coffee-outline", "unit": "cups"},
    "cappuccino": {"name": "Cappuccinos", "icon": "mdi:coffee-maker-outline", "unit": "cups"},
    "latte_macchiato": {"name": "Latte Macchiatos", "icon": "mdi:glass-mug-variant", "unit": "cups"},
    "caffe_latte": {"name": "Caffe Lattes", "icon": "mdi:glass-mug-variant", "unit": "cups"},
    "flat_white": {"name": "Flat Whites", "icon": "mdi:coffee", "unit": "cups"},
    "espresso_macchiato": {"name": "Espresso Macchiatos", "icon": "mdi:coffee", "unit": "cups"},
    "hot_milk": {"name": "Hot Milks", "icon": "mdi:cup", "unit": "cups"},
    "cappuccino_doppio": {"name": "Cappuccino Doppios", "icon": "mdi:coffee-maker-outline", "unit": "cups"},
    "cappuccino_mix": {"name": "Cappuccino Mix", "icon": "mdi:coffee-maker-outline", "unit": "cups"},
    "hot_water": {"name": "Hot Waters", "icon": "mdi:water-boiler", "unit": "cups"},
    "tea": {"name": "Teas", "icon": "mdi:tea", "unit": "cups"},
    "coffee_pot": {"name": "Coffee Pots", "icon": "mdi:coffee-maker", "unit": "cups"},
    "brew_over_ice": {"name": "Brew Over Ice", "icon": "mdi:snowflake", "unit": "cups"},
    # Maintenance
    "grounds_count": {"name": "Grounds Ejected", "icon": "mdi:delete-variant", "unit": "pucks"},
    "grounds_percentage": {"name": "Grounds Container", "icon": "mdi:delete-variant", "unit": "%"},
    "descale_count": {"name": "Descales Done", "icon": "mdi:water-check", "unit": "times"},
    "descale_progress": {"name": "Descale Progress", "icon": "mdi:water-alert", "unit": "%"},
    "total_water_ml": {"name": "Total Water Used", "icon": "mdi:water", "unit": "L", "scale": 0.001},
    "filter_percentage": {"name": "Water Filter Usage", "icon": "mdi:filter", "unit": "%"},
    "filter_replacements": {"name": "Filter Replacements", "icon": "mdi:filter-check", "unit": "times"},
    "water_through_filter_ml": {"name": "Water Through Filter", "icon": "mdi:water", "unit": "L", "scale": 0.001},
    # PrimaDonna Soul specific
    "total_black_beverages": {"name": "Total Black Beverages", "icon": "mdi:coffee", "unit": "cups"},
    "total_bw_beverages": {"name": "Total Black+White Beverages", "icon": "mdi:coffee", "unit": "cups"},
    "total_water_beverages": {"name": "Total Water Beverages", "icon": "mdi:water", "unit": "cups"},
    "milk_clean_count": {"name": "Milk Cleans", "icon": "mdi:spray-bottle", "unit": "times"},
    "beverages_since_descale": {"name": "Beverages Since Descale", "icon": "mdi:counter", "unit": "cups"},
    # Computed
    "computed_total": {"name": "Total All Beverages", "icon": "mdi:coffee", "unit": "cups"},
    # Custom / other
    "usage_tot_custom_b_bw": {"name": "Custom Beverages", "icon": "mdi:coffee-to-go", "unit": "cups"},
    "other_tot_bev_other": {"name": "Other Beverages", "icon": "mdi:cup", "unit": "cups"},
}


def _device_info(dsn: str, model: str, device_name: str, sw_version: str | None) -> dict[str, Any]:
    """Build consistent device_info dict."""
    info: dict[str, Any] = {
        "identifiers": {(DOMAIN, dsn)},
        "name": device_name,
        "manufacturer": "De'Longhi",
        "model": model,
    }
    if sw_version:
        info["sw_version"] = sw_version
    return info


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up sensor entities."""
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    coordinator: DeLonghiCoordinator = data["coordinator"]
    dsn: str = data["dsn"]
    model: str = data["model"]
    device_name: str = data["device_name"]
    sw_version: str | None = data.get("sw_version")

    entities: list[SensorEntity] = []

    # Status sensor
    entities.append(DeLonghiStatusSensor(coordinator, dsn, model, device_name, sw_version))

    # Profile sensor
    entities.append(DeLonghiProfileSensor(coordinator, dsn, model, device_name, sw_version))

    # Bean system sensor
    entities.append(DeLonghiBeanSensor(coordinator, dsn, model, device_name, sw_version))

    # Counter sensors — always create all; unsupported ones show as "unavailable"
    for key, meta in COUNTER_SENSORS.items():
        entities.append(DeLonghiCounterSensor(coordinator, dsn, model, device_name, sw_version, key, meta))

    async_add_entities(entities)


class DeLonghiStatusSensor(CoordinatorEntity[DeLonghiCoordinator], SensorEntity):
    """Machine status sensor."""

    def __init__(
        self,
        coordinator: DeLonghiCoordinator,
        dsn: str,
        model: str,
        device_name: str,
        sw_version: str | None,
    ) -> None:
        super().__init__(coordinator)
        self._dsn = dsn
        self._attr_unique_id = f"{dsn}_status"
        self._attr_has_entity_name = True
        self._attr_translation_key = "machine_status"
        self._attr_icon = "mdi:coffee-maker"
        self._attr_device_info = _device_info(dsn, model, device_name, sw_version)

    @property
    def native_value(self) -> str:
        """Return current machine state."""
        return self.coordinator.data.get("machine_state", "Unknown")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional state attributes."""
        alarms: list[dict[str, Any]] = self.coordinator.data.get("alarms", [])
        lan: dict[str, Any] = self.coordinator.data.get("lan_config", {})
        attrs: dict[str, Any] = {
            "cloud_status": self.coordinator.data.get("status", "UNKNOWN"),
            "profile": self.coordinator.data.get("profile", 0),
            "active_alarms": [a["name"] for a in alarms],
            "alarm_count": len(alarms),
            "api_calls_hour": self.coordinator.data.get("api_rate", 0),
            "api_total_calls": self.coordinator.data.get("api_total_calls", 0),
        }
        if lan:
            attrs["lan_enabled"] = lan.get("lan_enabled", False)
            attrs["lan_ip"] = lan.get("lan_ip")

        # ContentStack catalog info
        catalog = self.coordinator.data.get("drink_catalog", {})
        if catalog:
            attrs["contentstack_drinks"] = len(catalog)
        return attrs


# Percentage sensors that go down (not monotonically increasing)
_MEASUREMENT_SENSORS = {"grounds_percentage", "descale_progress", "filter_percentage"}


class DeLonghiCounterSensor(CoordinatorEntity[DeLonghiCoordinator], SensorEntity):
    """Beverage counter sensor."""

    def __init__(
        self,
        coordinator: DeLonghiCoordinator,
        dsn: str,
        model: str,
        device_name: str,
        sw_version: str | None,
        counter_key: str,
        meta: dict[str, str],
    ) -> None:
        super().__init__(coordinator)
        self._dsn = dsn
        self._counter_key = counter_key
        self._scale: float | None = meta.get("scale")
        self._attr_unique_id = f"{dsn}_{counter_key}"
        self._attr_has_entity_name = True
        self._attr_translation_key = counter_key
        self._attr_icon = meta["icon"]
        self._attr_native_unit_of_measurement = meta["unit"]
        self._attr_device_info = _device_info(dsn, model, device_name, sw_version)
        if self._scale:
            self._attr_suggested_display_precision = 1
        # Percentage sensors go up and down — use MEASUREMENT, not TOTAL_INCREASING
        if counter_key in _MEASUREMENT_SENSORS:
            self._attr_state_class = SensorStateClass.MEASUREMENT
        else:
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING

    @property
    def native_value(self) -> float | int | None:
        """Return current counter value."""
        counters: dict[str, Any] = self.coordinator.data.get("counters", {})
        val = counters.get(self._counter_key)
        if val is not None and self._scale:
            return round(val * self._scale, 1)
        return val


class DeLonghiProfileSensor(CoordinatorEntity[DeLonghiCoordinator], SensorEntity):
    """Active user profile sensor."""

    def __init__(
        self,
        coordinator: DeLonghiCoordinator,
        dsn: str,
        model: str,
        device_name: str,
        sw_version: str | None,
    ) -> None:
        super().__init__(coordinator)
        self._dsn = dsn
        self._attr_unique_id = f"{dsn}_active_profile"
        self._attr_has_entity_name = True
        self._attr_translation_key = "active_profile"
        self._attr_icon = "mdi:account"
        self._attr_device_info = _device_info(dsn, model, device_name, sw_version)

    @property
    def native_value(self) -> str:
        """Return active profile name."""
        # Use monitor profile (updated every 60s), fall back to cloud property (10min)
        active = self.coordinator.data.get("profile", 0)
        if active == 0:
            active = self.coordinator.data.get("active_profile", 1)
        profiles = self.coordinator.data.get("profiles", {})
        profile = profiles.get(active, {})
        return profile.get("name", f"Profile {active}")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return all profiles as attributes."""
        active = self.coordinator.data.get("active_profile", 1)
        profiles = self.coordinator.data.get("profiles", {})
        attrs: dict[str, Any] = {"active_profile_id": active}
        for pid, pdata in profiles.items():
            attrs[f"profile_{pid}_name"] = pdata.get("name", "")
            attrs[f"profile_{pid}_color"] = pdata.get("color", "")
            attrs[f"profile_{pid}_figure"] = pdata.get("figure", "")
        return attrs


class DeLonghiBeanSensor(CoordinatorEntity[DeLonghiCoordinator], SensorEntity):
    """Bean Adapt system sensor."""

    def __init__(
        self,
        coordinator: DeLonghiCoordinator,
        dsn: str,
        model: str,
        device_name: str,
        sw_version: str | None,
    ) -> None:
        super().__init__(coordinator)
        self._dsn = dsn
        self._attr_unique_id = f"{dsn}_bean_system"
        self._attr_has_entity_name = True
        self._attr_translation_key = "bean_system"
        self._attr_icon = "mdi:seed"
        self._attr_device_info = _device_info(dsn, model, device_name, sw_version)

    @property
    def native_value(self) -> int:
        """Return number of bean profiles configured."""
        beans = self.coordinator.data.get("beans", [])
        return len(beans)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return all bean profiles + ContentStack bean adapt calibration."""
        beans = self.coordinator.data.get("beans", [])
        attrs: dict[str, Any] = {}
        for bean in beans:
            bid = bean["id"]
            attrs[f"bean_{bid}_name"] = bean.get("name", "")
            attrs[f"bean_{bid}_english"] = bean.get("english_name", "")

        # ContentStack bean adapt calibration data
        ba = self.coordinator.data.get("bean_adapt", {})
        if ba:
            attrs["bean_types"] = ba.get("bean_types", [])
            attrs["roasting_levels"] = ba.get("roasting_levels", [])
            attrs["taste_feedback"] = ba.get("taste_feedback", [])
            attrs["grinder_range"] = f"{ba.get('grinder_min', 0)}-{ba.get('grinder_max', 0)} step={ba.get('grinder_step', 0)}"
            attrs["flow_range"] = f"{ba.get('flow_min', 0)}-{ba.get('flow_max', 0)} delta={ba.get('flow_delta', 0)}"
            attrs["preinfusion_water"] = f"{ba.get('preinfusion_water_min', 0)}-{ba.get('preinfusion_water_max', 0)} mL"
            for bt in ba.get("bean_table", []):
                attrs[f"powder_qty_{bt.get('bean_type', '').replace(' ', '_').lower()}"] = bt.get("powder_quantity", "")
            for rt in ba.get("roasting_table", []):
                level = rt.get("roast_level", "").lower()
                attrs[f"roast_{level}_stoichio"] = rt.get("stoichio_ratio", "")
                attrs[f"roast_{level}_machine_level"] = rt.get("machine_roasting_level", "")
                attrs[f"roast_{level}_temperature"] = rt.get("temperature", "")

        attrs["coffee_beans_catalog_count"] = self.coordinator.data.get("coffee_beans_count", 0)
        return attrs

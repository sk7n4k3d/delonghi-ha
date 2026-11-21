"""Independent ECAM630.70 carafe presence, selector, alarm, and rinse activity."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.delonghi_daedalus import binary_sensor, coordinator
from custom_components.delonghi_daedalus.cloud import CloudState, CloudTelemetryError
from custom_components.delonghi_daedalus.const import (
    CLOUD_POLL_SECONDS,
    CONF_CLOUD_TELEMETRY,
    CONF_JWT,
    CONF_MACHINE_NAME,
    CONF_MODEL,
    CONF_SERIAL_NUMBER,
    DOMAIN,
)

_RINSE_KEY = "hot_milk_carafe_rinse_in_progress"


def _coordinator(model: str | None = "ECAM630.70", *, cloud_enabled: bool = True):
    hass = MagicMock()
    entry = MagicMock()
    entry.data = {
        CONF_CLOUD_TELEMETRY: cloud_enabled,
        CONF_JWT: "fixture-jwt",
        CONF_MACHINE_NAME: "machine-1",
        CONF_MODEL: model,
        CONF_SERIAL_NUMBER: "SN-TEST",
    }
    machine = coordinator.DaedalusCoordinator(hass, entry)
    machine.hass = hass
    machine.entry = entry
    machine.data = {}
    machine.last_update_success = True
    machine._api = MagicMock()
    # Avoid exercising optional shadows in a MachineStatus test.
    machine._machine_settings_due_at = machine._brewing_statistics_due_at = float("inf")
    listeners = []
    machine.async_add_listener = MagicMock(side_effect=lambda callback: listeners.append(callback))

    def update_listeners():
        for callback in listeners:
            callback()

    machine.async_update_listeners = update_listeners
    hass.data = {DOMAIN: {entry.entry_id: machine}}
    return machine


def _report(
    state: str = "Busy",
    mode: int = 12,
    *,
    hot_jug: bool = True,
    selector: bool = True,
    clean: bool = False,
    progress: int = 23,
    model: str | None = None,
    missing: str | None = None,
    old_timestamp: str | None = None,
    include_metadata: bool = True,
    status_error: str | None = None,
) -> CloudState:
    reported = {
        "Status": state,
        "CurrMode": mode,
        "CurrFuncProgress": progress,
        "alarms": {"Clean": clean},
        "peripherals": {"HotJug": hot_jug, "Jug": hot_jug},
        "accessories": {"CleanHot": selector},
    }
    if missing:
        if "." in missing:
            group, field = missing.split(".")
            del reported[group][field]
        else:
            del reported[missing]
    now = int(time.time())

    def timestamp(field):
        return {"timestamp": now - CLOUD_POLL_SECONDS - 10 if old_timestamp == field else now}

    status = {"state": {"reported": reported}}
    if include_metadata:
        status["metadata"] = {
            "reported": {
                "Status": timestamp("Status"),
                "CurrMode": timestamp("CurrMode"),
                "accessories": {"CleanHot": timestamp("accessories.CleanHot")},
            }
        }
    shadows = {"MachineStatus": status}
    if model:
        shadows["MachineCapabilities"] = {"state": {"reported": {"MachineModelUI": model}}}
    return CloudState(shadows, "machine-1", status_error)


async def _setup(machine):
    entities = []
    await binary_sensor.async_setup_entry(machine.hass, machine.entry, entities.extend)
    return entities


def test_observed_hot_carafe_rinse_sequence(monkeypatch):
    fetch = AsyncMock(
        side_effect=[
            _report("Idle", 7, selector=False, clean=True, old_timestamp="Status"),
            _report(),
            _report(selector=False, progress=100),
            _report("Idle", 7, selector=True, progress=100),
            _report("Idle", 7, hot_jug=False, selector=False),
        ]
    )
    monkeypatch.setattr(coordinator, "fetch_cloud_state", fetch)

    async def scenario():
        machine = _coordinator()
        entities = await _setup(machine)
        assert all(entity._attr_translation_key != _RINSE_KEY for entity in entities)

        await machine._async_cloud_fetch()
        entities_by_name = {entity._attr_translation_key: entity for entity in entities}
        rinse = entities_by_name[_RINSE_KEY]
        assert rinse._attr_unique_id == f"SN-TEST_{_RINSE_KEY}"
        assert rinse.suggested_object_id == _RINSE_KEY
        assert getattr(rinse, "_attr_entity_registry_enabled_default", True)
        alarm = entities_by_name["cloud_cleaning"]
        selector = entities_by_name["cloud_hot_carafe_clean_selector"]
        presence = entities_by_name["cloud_hot_jug"]
        assert selector._attr_unique_id == "SN-TEST_cloud_accessories.CleanHot"
        assert selector.suggested_object_id == "hot_cleaning_cycle"
        assert selector._attr_entity_registry_enabled_default is False
        assert not machine.data["connected"]  # LAN is optional for cloud activity.
        assert (rinse.is_on, alarm.is_on, selector.is_on, presence.is_on) == (False, True, False, True)

        await machine._async_cloud_fetch()
        assert rinse.available and rinse.is_on is True
        assert (selector.is_on, presence.is_on) == (True, True)

        await machine._async_cloud_fetch()
        assert not rinse.available and rinse.is_on is None
        assert selector.is_on is False

        await machine._async_cloud_fetch()
        assert rinse.available and rinse.is_on is False
        assert selector.is_on is True

        await machine._async_cloud_fetch()
        assert rinse.available and rinse.is_on is False
        assert (alarm.is_on, selector.is_on, presence.is_on) == (False, False, False)
        assert sum(entity._attr_translation_key == _RINSE_KEY for entity in entities) == 1
        machine._cancel_cloud_expiry()

    asyncio.run(scenario())
    assert fetch.await_count == 5


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"mode": 9}, False),
        ({"hot_jug": False}, False),
        ({"state": "undefined"}, None),
        ({"mode": True}, None),
        ({"selector": 2}, None),
        ({"hot_jug": "true"}, None),
        ({"missing": "Status"}, None),
        ({"missing": "CurrMode"}, None),
        ({"missing": "peripherals.HotJug"}, None),
        ({"missing": "accessories.CleanHot"}, None),
        ({"old_timestamp": "Status"}, None),
        ({"old_timestamp": "CurrMode"}, None),
        ({"old_timestamp": "accessories.CleanHot"}, None),
        ({"include_metadata": False}, None),
        ({"model": "ECAM630.70", "status_error": "Device directory reports the machine offline"}, None),
        (None, None),
    ],
)
def test_reports_replace_active_rinse_without_reusing_partial_or_stale_values(monkeypatch, fields, expected):
    next_report = CloudTelemetryError("Cloud unavailable") if fields is None else _report(**fields)
    fetch = AsyncMock(side_effect=[_report(), next_report])
    monkeypatch.setattr(coordinator, "fetch_cloud_state", fetch)

    async def scenario():
        machine = _coordinator()
        entities = await _setup(machine)
        await machine._async_cloud_fetch()
        (rinse,) = [entity for entity in entities if entity._attr_translation_key == _RINSE_KEY]
        assert rinse.available and rinse.is_on is True

        await machine._async_cloud_fetch()
        assert rinse.available is (expected is not None)
        assert rinse.is_on is expected
        machine._cancel_cloud_expiry()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("configured", "reported", "enabled", "have_rinse"),
    [
        (None, "ECAM63070", True, True),
        (None, "206.3", True, False),
        ("ECAM470.75.TXB", "ECAM63070", True, False),
        ("ECAM630.75", "ECAM63070", True, False),
        ("ECAM63070", None, False, False),
    ],
)
def test_rinse_and_selector_label_need_model_and_cloud(monkeypatch, configured, reported, enabled, have_rinse):
    monkeypatch.setattr(coordinator, "fetch_cloud_state", AsyncMock(return_value=_report(model=reported)))

    async def scenario():
        machine = _coordinator(configured, cloud_enabled=enabled)
        entities = await _setup(machine)
        if enabled:
            await machine._async_cloud_fetch()
        names = {entity._attr_translation_key for entity in entities}
        assert (_RINSE_KEY in names) is have_rinse
        assert ("cloud_hot_carafe_clean_selector" in names) is have_rinse
        assert ("cloud_clean_hot" in names) is (enabled and not have_rinse)
        machine._cancel_cloud_expiry()

    asyncio.run(scenario())


def test_expired_coordinator_status_cannot_keep_rinse_active(monkeypatch):
    monkeypatch.setattr(coordinator, "fetch_cloud_state", AsyncMock(return_value=_report()))

    async def scenario():
        machine = _coordinator()
        entities = await _setup(machine)
        await machine._async_cloud_fetch()
        (rinse,) = [entity for entity in entities if entity._attr_translation_key == _RINSE_KEY]
        assert rinse.available and rinse.is_on is True

        machine._cancel_cloud_expiry()
        machine._expire_cloud()
        assert not rinse.available and rinse.is_on is None

    asyncio.run(scenario())

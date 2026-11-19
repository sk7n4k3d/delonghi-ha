"""Device metadata follows late snapshots without modifying user settings."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from custom_components.delonghi_daedalus.const import DOMAIN
from custom_components.delonghi_daedalus.device import DaedalusDevice


def test_device_metadata_updates_when_firmware_arrives() -> None:
    entry = MagicMock(data={"model": "ECAM63070"})
    coord = MagicMock(entry=entry, serial_number="SN-test", data={})
    managed = DaedalusDevice(coord)

    registered = SimpleNamespace(
        id="device",
        model=None,
        name="Coffee Machine",
        name_by_user="Coffee at home",
        area_id="kitchen",
        sw_version=None,
    )
    registry = MagicMock()
    registry.async_get_device.return_value = registered

    def update(_device_id, **changes):
        for key, value in changes.items():
            setattr(registered, key, value)

    registry.async_update_device.side_effect = update
    with (
        patch("custom_components.delonghi_daedalus.device.device_registry.async_get", return_value=registry),
        patch(
            "custom_components.delonghi_daedalus.device.entity_registry.async_entries_for_config_entry", return_value=[]
        ),
    ):
        managed.async_setup()
        callback = coord.async_add_listener.call_args.args[0]
        assert registered.name == "De'Longhi PrimaDonna Aromatic ECAM630.70"

        coord.data = {"cloud": {"capabilities_flat": {"FwMainBoardVersion": "1.1.0"}}}
        callback()
        registry.async_update_device.assert_called_with("device", sw_version="1.1.0")

        coord.data = {"cloud": {"capabilities_flat": {"FwUiVersion": "1.2.0", "FwMainBoardVersion": "1.1.0"}}}
        callback()
        registry.async_update_device.assert_called_with("device", sw_version="1.2.0")
        assert managed.info()["sw_version"] == "1.2.0"

        registry.async_update_device.reset_mock()
        for capabilities in ({}, {"FwUiVersion": "1.2.0"}, {"FwMainBoardVersion": "1.1.0"}):
            coord.data = {"cloud": {"capabilities_flat": capabilities}}
            callback()
        registry.async_update_device.assert_not_called()
        assert registered.name_by_user == "Coffee at home"
        assert registered.area_id == "kitchen"


def test_late_cloud_model_does_not_expose_private_values() -> None:
    coord = MagicMock(entry=SimpleNamespace(data={}), serial_number="private-serial", data={})
    managed = DaedalusDevice(coord)
    coord.data = {
        "cloud": {
            "capabilities_flat": {"MachineModelUi": "private-thing", "FwUiVersion": "unspecified private-thing"},
            "machine_name": "private-thing",
        }
    }
    assert managed.info()["name"] == "De'Longhi My Coffee Lounge"
    assert "sw_version" not in managed.info()

    coord.data = {"cloud": {"capabilities_flat": {"MachineModelUi": "ECAM630.70", "FwUiVersion": "1.2.0"}}}
    assert managed.info()["name"] == "De'Longhi PrimaDonna Aromatic ECAM630.70"
    assert managed.info()["sw_version"] == "1.2.0"


def test_upgrade_removes_only_registered_sensitive_cloud_fields() -> None:
    coord = MagicMock(entry=MagicMock(data={}), serial_number="SN-test", data={})
    entities = [
        SimpleNamespace(
            entity_id=f"{domain}.{slug}",
            domain=domain,
            platform=platform,
            unique_id=f"SN-test_{suffix}",
            disabled_by=None,
        )
        for domain, platform, slug, suffix in (
            ("sensor", DOMAIN, "renamed_address", "cloud_LanIpAddress"),
            ("sensor", DOMAIN, "old_address", "cloudcap_Network.LanIpAddress"),
            ("sensor", DOMAIN, "old_name", "cloudcap_Machine_Name"),
            ("binary_sensor", DOMAIN, "old_flag", "cloud_Network.Ble-PassCODE.valid"),
            ("sensor", DOMAIN, "lan_diagnostic", "lan_ip"),
            ("sensor", DOMAIN, "machine_state", "cloud_Status"),
            ("binary_sensor", DOMAIN, "drip_tray", "cloud_peripherals.DripPresence"),
            ("binary_sensor", DOMAIN, "ip_assigned", "cloud_Network.LanIpAddressAssigned"),
            ("sensor", "other", "external", "cloud_LanIpAddress"),
            ("switch", DOMAIN, "switch", "cloud_LanIpAddress"),
        )
    ]
    entities.append(
        SimpleNamespace(
            entity_id="sensor.wrong_machine", domain="sensor", platform=DOMAIN, unique_id="SN-other_cloud_LanIpAddress"
        )
    )
    registry = MagicMock()
    with (
        patch("custom_components.delonghi_daedalus.device.device_registry.async_get") as devices,
        patch("custom_components.delonghi_daedalus.device.entity_registry.async_get", return_value=registry),
        patch(
            "custom_components.delonghi_daedalus.device.entity_registry.async_entries_for_config_entry",
            return_value=entities,
        ),
    ):
        devices.return_value.async_get_device.return_value = None
        DaedalusDevice(coord).async_setup()
    assert [call.args[0] for call in registry.async_remove.call_args_list] == [
        "sensor.renamed_address",
        "sensor.old_address",
        "sensor.old_name",
        "binary_sensor.old_flag",
    ]

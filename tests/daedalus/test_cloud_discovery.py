"""Exercise the account directory and selected machine's reported LAN address."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, create_autospec

import pytest

from custom_components.delonghi_daedalus import cloud
from custom_components.delonghi_daedalus.const import AWS_REST_BASE_URL_PROD


def test_directory_reads_owned_and_shared_devices_without_redirects(monkeypatch):
    request = AsyncMock(
        return_value={
            "ownedByMe": [
                {"serialNumber": "SN1234", "machineName": "thing-a", "machineModel": "ECAM63070"},
                {"serialNumber": "SN0000", "machineName": "unexpected/topic"},
            ],
            "sharedToMe": [
                {"serialNumber": "sn1234", "machineName": "thing-a"},
                {"serialNumber": "SN4321", "machineName": "thing-b", "machineModel": "ECAM65075"},
            ],
        }
    )
    monkeypatch.setattr(cloud, "_fetch_devices", request)
    session = MagicMock()
    assert asyncio.run(cloud.list_machines(session, jwt="jwt")) == [
        cloud.DirectoryMachine("SN1234", "thing-a", "ECAM63070"),
        cloud.DirectoryMachine("SN0000", None, None),
        cloud.DirectoryMachine("SN4321", "thing-b", "ECAM65075"),
    ]
    request.assert_awaited_once_with(session, url=f"{AWS_REST_BASE_URL_PROD}devices", jwt="jwt", allow_redirects=False)


@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        ({"LanIpAddress": "192.168.2.7"}, "192.168.2.7"),
        ({"LanIpAddress": "fd80::3"}, "fd80::3"),
        ({"LanIpAddress": "1.1.1.1"}, None),
        ({"LanIpAddress": "169.254.169.254"}, None),
        ({"LanIpAddress": "http://192.168.2.7/"}, None),
        ({"Other": "192.168.2.7"}, None),
    ],
)
def test_reported_ip_comes_from_selected_machine_status(monkeypatch, reported, expected):
    client = create_autospec(cloud.ShadowClient, instance=True)
    client.fetch.return_value = {
        "MachineStatus": {"state": {"reported": reported, "desired": {"LanIpAddress": "192.168.1.9"}}}
    }
    shadow = MagicMock(return_value=client)
    monkeypatch.setattr(cloud, "ShadowClient", shadow)
    session = MagicMock()
    assert asyncio.run(cloud.fetch_reported_lan_ip(session, thing_name="thing-b", jwt="jwt")) == expected
    shadow.assert_called_once_with(session, thing_name="thing-b", jwt="jwt")
    client.fetch.assert_awaited_once_with(timeout=3)


def test_directory_preserves_legacy_identity_aliases():
    payload = {"devices": [{"serial": "SN-example", "thingName": "thing-example"}]}
    assert cloud._directory_machines(payload) == [cloud.DirectoryMachine("SN-example", "thing-example", None)]


def test_directory_drops_invalid_or_identity_matching_model_labels():
    payload = {
        "ownedByMe": [
            {"serialNumber": "ECAM63070", "machineName": "thing-one", "machineModel": "ECAM63070"},
            {"serialNumber": "SN5321", "machineName": "ECAM63065", "machineModel": "ECAM63065"},
            {"serialNumber": "SN5322", "machineName": "thing-three", "machineModel": "10.0.0.2"},
        ]
    }
    assert [device.model for device in cloud._directory_machines(payload)] == [None, None, None]


def test_nonempty_malformed_directory_signals_manual_fallback():
    with pytest.raises(cloud.CloudTelemetryError, match="no usable machine"):
        cloud._directory_machines({"ownedByMe": [{"name": "missing-serial"}], "sharedToMe": []})

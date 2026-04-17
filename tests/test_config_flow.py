"""Test config_flow.py — user flow + reauth + options flow."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.delonghi_coffee import config_flow as cf_mod
from custom_components.delonghi_coffee.api import DeLonghiApiError, DeLonghiAuthError
from custom_components.delonghi_coffee.config_flow import (
    DeLonghiCoffeeConfigFlow,
    DeLonghiOptionsFlow,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_hass():
    hass = MagicMock()

    async def _executor(func, *args, **kwargs):
        return func(*args, **kwargs)

    hass.async_add_executor_job = _executor
    hass.config_entries = MagicMock()
    hass.config_entries.async_update_entry = MagicMock()
    hass.config_entries.async_reload = AsyncMock()
    return hass


def _make_flow(hass=None) -> DeLonghiCoffeeConfigFlow:
    flow = DeLonghiCoffeeConfigFlow()
    flow.hass = hass or _make_hass()
    flow.context = {}
    return flow


class TestSchemaConstants:
    def test_user_schema_has_required_fields(self):
        # voluptuous Schema exposes its underlying spec as ``.schema`` (a dict).
        keys = {str(k): k for k in cf_mod.STEP_USER_DATA_SCHEMA.schema}
        assert any("region" in k for k in keys)
        assert any("email" in k for k in keys)
        assert any("password" in k for k in keys)

    def test_reauth_schema_only_password(self):
        keys = [str(k) for k in cf_mod.STEP_REAUTH_DATA_SCHEMA.schema]
        assert len(keys) == 1
        assert "password" in keys[0]

    def test_class_version_is_two(self):
        assert DeLonghiCoffeeConfigFlow.VERSION == 2


class TestUserStepInitial:
    def test_no_input_shows_form(self):
        flow = _make_flow()
        result = _run(flow.async_step_user(user_input=None))
        assert result["type"] == "form"
        assert result["step_id"] == "user"
        assert result["errors"] == {}


class TestUserStepHappyPath:
    def test_creates_entry_with_first_device(self):
        flow = _make_flow()
        api = MagicMock()
        api.authenticate = MagicMock()
        api.get_devices = MagicMock(return_value=[
            {
                "dsn": "DSN-A",
                "oem_model": "DL-striker-cb",
                "product_name": "Soul",
                "sw_version": "1.2.3",
            },
            {"dsn": "DSN-B"},  # ignored — only first kept
        ])

        with patch.object(cf_mod, "DeLonghiApi", return_value=api):
            result = _run(flow.async_step_user({
                "email": "u@x.com",
                "password": "secret",
                "region": "EU",
            }))

        assert result["type"] == "create_entry"
        assert result["title"] == "De'Longhi Soul"
        assert result["data"]["dsn"] == "DSN-A"
        assert result["data"]["model"] == "DL-striker-cb"
        assert result["data"]["device_name"] == "Soul"
        assert result["data"]["sw_version"] == "1.2.3"
        assert result["data"]["region"] == "EU"
        api.authenticate.assert_called_once()
        api.get_devices.assert_called_once()

    def test_uses_dsn_when_product_name_missing(self):
        flow = _make_flow()
        api = MagicMock()
        api.authenticate = MagicMock()
        api.get_devices = MagicMock(return_value=[{"dsn": "DSN-X"}])
        with patch.object(cf_mod, "DeLonghiApi", return_value=api):
            result = _run(flow.async_step_user({
                "email": "u@x.com",
                "password": "p",
                "region": "EU",
            }))
        assert result["title"] == "De'Longhi DSN-X"
        assert result["data"]["model"] == "unknown"

    def test_no_devices_returns_form_with_error(self):
        flow = _make_flow()
        api = MagicMock()
        api.authenticate = MagicMock()
        api.get_devices = MagicMock(return_value=[])
        with patch.object(cf_mod, "DeLonghiApi", return_value=api):
            result = _run(flow.async_step_user({
                "email": "u@x.com",
                "password": "p",
                "region": "EU",
            }))
        assert result["type"] == "form"
        assert result["errors"] == {"base": "no_devices"}


class TestUserStepErrors:
    def test_auth_error_returns_invalid_auth(self):
        flow = _make_flow()
        api = MagicMock()
        api.authenticate = MagicMock(side_effect=DeLonghiAuthError("nope"))
        with patch.object(cf_mod, "DeLonghiApi", return_value=api):
            result = _run(flow.async_step_user({
                "email": "u@x.com",
                "password": "p",
                "region": "EU",
            }))
        assert result["type"] == "form"
        assert result["errors"] == {"base": "invalid_auth"}

    def test_api_error_returns_cannot_connect(self):
        flow = _make_flow()
        api = MagicMock()
        api.authenticate = MagicMock(side_effect=DeLonghiApiError("network"))
        with patch.object(cf_mod, "DeLonghiApi", return_value=api):
            result = _run(flow.async_step_user({
                "email": "u@x.com",
                "password": "p",
                "region": "EU",
            }))
        assert result["errors"] == {"base": "cannot_connect"}

    def test_unexpected_error_returns_unknown(self):
        flow = _make_flow()
        api = MagicMock()
        api.authenticate = MagicMock(side_effect=RuntimeError("boom"))
        with patch.object(cf_mod, "DeLonghiApi", return_value=api):
            result = _run(flow.async_step_user({
                "email": "u@x.com",
                "password": "p",
                "region": "EU",
            }))
        assert result["errors"] == {"base": "unknown"}


class TestReauthStep:
    def test_step_reauth_loads_entry_then_delegates(self):
        flow = _make_flow()
        flow.context = {"entry_id": "eid"}
        fake_entry = MagicMock()
        fake_entry.data = {"email": "u@x.com", "region": "EU"}
        flow.hass.config_entries.async_get_entry = MagicMock(return_value=fake_entry)

        result = _run(flow.async_step_reauth({"email": "u@x.com"}))
        flow.hass.config_entries.async_get_entry.assert_called_once_with("eid")
        assert flow._reauth_entry is fake_entry
        # Delegates to async_step_reauth_confirm with no user_input
        assert result["type"] == "form"
        assert result["step_id"] == "reauth_confirm"

    def test_reauth_confirm_no_input_shows_form(self):
        flow = _make_flow()
        fake_entry = MagicMock()
        fake_entry.data = {"email": "u@x.com", "region": "EU"}
        flow._reauth_entry = fake_entry
        result = _run(flow.async_step_reauth_confirm(user_input=None))
        assert result["type"] == "form"
        assert result["description_placeholders"] == {"email": "u@x.com"}

    def test_reauth_confirm_no_entry_no_input(self):
        flow = _make_flow()
        flow._reauth_entry = None
        result = _run(flow.async_step_reauth_confirm(user_input=None))
        # Empty placeholder when entry not loaded yet
        assert result["description_placeholders"] == {"email": ""}

    def test_reauth_confirm_success_aborts(self):
        flow = _make_flow()
        fake_entry = MagicMock()
        fake_entry.data = {"email": "u@x.com", "region": "EU"}
        fake_entry.entry_id = "eid"
        flow._reauth_entry = fake_entry

        api = MagicMock()
        api.authenticate = MagicMock()
        with patch.object(cf_mod, "DeLonghiApi", return_value=api):
            result = _run(flow.async_step_reauth_confirm({"password": "newpwd"}))

        assert result["type"] == "abort"
        assert result["reason"] == "reauth_successful"
        flow.hass.config_entries.async_update_entry.assert_called_once()
        update_call = flow.hass.config_entries.async_update_entry.call_args
        assert update_call.args[0] is fake_entry
        assert update_call.kwargs["data"]["password"] == "newpwd"
        assert update_call.kwargs["data"]["email"] == "u@x.com"

    def test_reauth_confirm_auth_error(self):
        flow = _make_flow()
        fake_entry = MagicMock()
        fake_entry.data = {"email": "u@x.com", "region": "EU"}
        flow._reauth_entry = fake_entry
        api = MagicMock()
        api.authenticate = MagicMock(side_effect=DeLonghiAuthError("nope"))
        with patch.object(cf_mod, "DeLonghiApi", return_value=api):
            result = _run(flow.async_step_reauth_confirm({"password": "x"}))
        assert result["errors"] == {"base": "invalid_auth"}

    def test_reauth_confirm_api_error(self):
        flow = _make_flow()
        fake_entry = MagicMock()
        fake_entry.data = {"email": "u@x.com"}  # default region branch
        flow._reauth_entry = fake_entry
        api = MagicMock()
        api.authenticate = MagicMock(side_effect=DeLonghiApiError("boom"))
        with patch.object(cf_mod, "DeLonghiApi", return_value=api):
            result = _run(flow.async_step_reauth_confirm({"password": "x"}))
        assert result["errors"] == {"base": "cannot_connect"}

    def test_reauth_confirm_unknown_error(self):
        flow = _make_flow()
        fake_entry = MagicMock()
        fake_entry.data = {"email": "u@x.com", "region": "EU"}
        flow._reauth_entry = fake_entry
        api = MagicMock()
        api.authenticate = MagicMock(side_effect=RuntimeError("???"))
        with patch.object(cf_mod, "DeLonghiApi", return_value=api):
            result = _run(flow.async_step_reauth_confirm({"password": "x"}))
        assert result["errors"] == {"base": "unknown"}


class TestOptionsFlow:
    def test_factory_returns_options_flow(self):
        entry = MagicMock()
        result = DeLonghiCoffeeConfigFlow.async_get_options_flow(entry)
        assert isinstance(result, DeLonghiOptionsFlow)

    def test_show_form_with_current_value(self):
        entry = MagicMock()
        entry.options = {"diagnostic_mode": True}
        flow = DeLonghiOptionsFlow(entry)
        flow.hass = _make_hass()
        result = _run(flow.async_step_init(user_input=None))
        assert result["type"] == "form"
        assert result["step_id"] == "init"

    def test_save_options_updates_coordinator(self):
        entry = MagicMock()
        entry.entry_id = "eid"
        entry.options = {}
        flow = DeLonghiOptionsFlow(entry)
        flow.hass = _make_hass()
        coord = MagicMock()
        coord.diagnostic_mode = False
        flow.hass.data = {"delonghi_coffee": {"eid": {"coordinator": coord}}}

        result = _run(flow.async_step_init({"diagnostic_mode": True}))
        assert result["type"] == "create_entry"
        assert result["data"] == {"diagnostic_mode": True}
        assert coord.diagnostic_mode is True

    def test_save_options_without_coordinator_is_safe(self):
        entry = MagicMock()
        entry.entry_id = "eid"
        flow = DeLonghiOptionsFlow(entry)
        flow.hass = _make_hass()
        flow.hass.data = {}  # no DOMAIN entry yet
        result = _run(flow.async_step_init({"diagnostic_mode": False}))
        assert result["type"] == "create_entry"


@pytest.fixture(autouse=True)
def _ensure_event_loop():
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("closed")
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    yield

"""Config flow for De'Longhi Coffee integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResult

from .api import DeLonghiApi, DeLonghiApiError, DeLonghiAuthError
from .const import CONF_REGION, DOMAIN, REGIONS

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_REGION, default="EU"): vol.In({key: cfg["name"] for key, cfg in REGIONS.items()}),
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

STEP_REAUTH_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PASSWORD): str,
    }
)


class DeLonghiCoffeeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for De'Longhi Coffee."""

    VERSION = 2

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._reauth_entry: config_entries.ConfigEntry | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            region = user_input[CONF_REGION]

            try:
                api = DeLonghiApi(
                    user_input[CONF_EMAIL],
                    user_input[CONF_PASSWORD],
                    region=region,
                )
                await self.hass.async_add_executor_job(api.authenticate)
                devices = await self.hass.async_add_executor_job(api.get_devices)

                if not devices:
                    errors["base"] = "no_devices"
                else:
                    await self.async_set_unique_id(user_input[CONF_EMAIL])
                    self._abort_if_unique_id_configured()

                    device = devices[0]
                    return self.async_create_entry(
                        title=f"De'Longhi {device.get('product_name', device['dsn'])}",
                        data={
                            CONF_EMAIL: user_input[CONF_EMAIL],
                            CONF_PASSWORD: user_input[CONF_PASSWORD],
                            CONF_REGION: region,
                            "dsn": device["dsn"],
                            "model": device.get("oem_model", "unknown"),
                            "device_name": device.get("product_name"),
                            "sw_version": device.get("sw_version"),
                        },
                    )
            except DeLonghiAuthError:
                errors["base"] = "invalid_auth"
            except DeLonghiApiError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during config flow")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        """Handle reauth when credentials become invalid."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle reauth confirmation."""
        errors: dict[str, str] = {}

        if user_input is not None and self._reauth_entry is not None:
            email = self._reauth_entry.data[CONF_EMAIL]
            region = self._reauth_entry.data.get(CONF_REGION, "EU")
            password = user_input[CONF_PASSWORD]

            try:
                api = DeLonghiApi(email, password, region=region)
                await self.hass.async_add_executor_job(api.authenticate)

                self.hass.config_entries.async_update_entry(
                    self._reauth_entry,
                    data={**self._reauth_entry.data, CONF_PASSWORD: password},
                )
                await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
                return self.async_abort(reason="reauth_successful")

            except DeLonghiAuthError:
                errors["base"] = "invalid_auth"
            except DeLonghiApiError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during reauth")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_DATA_SCHEMA,
            description_placeholders={"email": self._reauth_entry.data[CONF_EMAIL] if self._reauth_entry else ""},
            errors=errors,
        )

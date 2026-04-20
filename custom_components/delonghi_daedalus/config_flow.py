"""Config flow for the De'Longhi Daedalus integration.

User supplies:
    - email + password (My Coffee Lounge account)
    - LAN IP of the machine
    - serial number printed on the machine

Flow validates by performing the Gigya login, fetching a JWT, then opening
the LAN `/ws/lan2lan` WebSocket to confirm that the (IP, SN, JWT) triple is
accepted by the firmware. On success the tokens are persisted in the config
entry so the runtime can skip cloud login on boot.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .api import (
    DaedalusApi,
    DaedalusAuthError,
    DaedalusConnectionError,
)
from .const import (
    CONF_EMAIL,
    CONF_HOST,
    CONF_JWT,
    CONF_MACHINE_NAME,
    CONF_PASSWORD,
    CONF_SERIAL_NUMBER,
    CONF_SESSION_TOKEN,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_SERIAL_NUMBER): str,
    }
)


class DaedalusConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a Daedalus config flow."""

    VERSION = 1

    def __init__(self) -> None:
        super().__init__()
        # Override-able at test time to inject a mock API client.
        self._api_factory = DaedalusApi

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle initial user step — credentials + LAN target."""
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=_USER_SCHEMA)

        errors: dict[str, str] = {}
        serial = user_input[CONF_SERIAL_NUMBER]
        host = user_input[CONF_HOST]

        # Unique id = email + SN, so the same account on multiple machines is OK.
        await self.async_set_unique_id(f"{user_input[CONF_EMAIL]}:{serial}")
        self._abort_if_unique_id_configured()

        api = self._api_factory()
        try:
            session_token, jwt = await api.login_and_get_jwt(
                email=user_input[CONF_EMAIL], password=user_input[CONF_PASSWORD]
            )
            lan = await api.connect_lan(host=host, serial_number=serial, jwt=jwt)
            await lan.close()
        except DaedalusAuthError:
            errors["base"] = "invalid_auth"
        except DaedalusConnectionError:
            errors["base"] = "cannot_connect"
        except Exception:  # noqa: BLE001 — last-resort net so the form still renders
            _LOGGER.exception("Unexpected error validating Daedalus machine")
            errors["base"] = "unknown"
        finally:
            await api.close()

        if errors:
            return self.async_show_form(step_id="user", data_schema=_USER_SCHEMA, errors=errors)

        return self.async_create_entry(
            title=f"My Coffee Lounge ({serial})",
            data={
                CONF_EMAIL: user_input[CONF_EMAIL],
                CONF_PASSWORD: user_input[CONF_PASSWORD],
                CONF_HOST: host,
                CONF_SERIAL_NUMBER: serial,
                CONF_MACHINE_NAME: serial,
                CONF_JWT: jwt,
                CONF_SESSION_TOKEN: session_token,
            },
        )

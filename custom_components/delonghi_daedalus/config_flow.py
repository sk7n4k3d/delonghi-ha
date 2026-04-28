"""Config flow for the De'Longhi Daedalus integration.

User supplies:
    - email + password (My Coffee Lounge account)
    - LAN IP of the machine
    - serial number printed on the machine

Flow validates by performing the Gigya login, fetching a JWT, then opening
the LAN `/ws/lan2lan` WebSocket to confirm that the (IP, SN, JWT) triple is
accepted by the firmware. On success the JWT and the long-lived Gigya
session token are persisted; the user's password is **not** kept on disk —
JWT rotation goes through the session token, and a HA reauth flow asks for
the password again on the rare occasion the session token is itself
revoked.
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
    CONF_POOL,
    CONF_SERIAL_NUMBER,
    CONF_SESSION_TOKEN,
    DOMAIN,
    GIGYA_API_KEYS,
    GIGYA_POOL_EU,
)

_LOGGER = logging.getLogger(__name__)

# Gigya errorCode returned when the account exists but has no access to this
# application's apiKey.  Happens when the account was registered via a
# different Gigya site (OIDC / OAuth2 redirect) rather than direct login.
_GIGYA_UNAUTHORIZED_USER = "403005"

_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_SERIAL_NUMBER): str,
        vol.Optional(CONF_POOL, default=GIGYA_POOL_EU): vol.In(list(GIGYA_API_KEYS.keys())),
    }
)


async def _probe_pools(
    api: DaedalusApi,
    *,
    email: str,
    password: str,
    preferred_pool: str,
) -> tuple[str, str, str]:
    """Try Gigya login across all pools, preferred pool first.

    Returns (pool, session_token, jwt) for the first pool that succeeds.

    If the preferred pool returns 403005 ("Unauthorized user") the remaining
    pools are tried automatically — this covers accounts registered on a
    different pool than EU without requiring the user to guess.

    Any other auth error (wrong password, rate-limit…) short-circuits
    immediately so we don't burn all pools on a typo.

    Raises DaedalusAuthError with message starting with "all_pools:" when
    every pool returns 403005 — the caller maps this to the dedicated
    translation key.
    """
    pools_ordered = [preferred_pool] + [p for p in GIGYA_API_KEYS if p != preferred_pool]
    last_exc: DaedalusAuthError | None = None

    for pool in pools_ordered:
        api_key = GIGYA_API_KEYS[pool]
        try:
            session_token, jwt = await api.login_and_get_jwt(email=email, password=password, api_key=api_key)
            if pool != preferred_pool:
                _LOGGER.info(
                    "Daedalus: preferred pool %s returned 403005, succeeded with %s",
                    preferred_pool,
                    pool,
                )
            return pool, session_token, jwt
        except DaedalusAuthError as exc:
            last_exc = exc
            if _GIGYA_UNAUTHORIZED_USER not in str(exc):
                raise  # wrong password / rate-limit — no point probing other pools
            _LOGGER.debug("Daedalus: pool %s → 403005, probing next pool", pool)

    raise DaedalusAuthError(f"all_pools: every Gigya pool returned 403005 for {email}") from last_exc


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
        preferred_pool = user_input.get(CONF_POOL, GIGYA_POOL_EU)
        resolved_pool = preferred_pool

        # Unique id = email + SN, so the same account on multiple machines is OK.
        await self.async_set_unique_id(f"{user_input[CONF_EMAIL]}:{serial}")
        self._abort_if_unique_id_configured()

        api = self._api_factory()
        session_token = jwt = ""
        try:
            resolved_pool, session_token, jwt = await _probe_pools(
                api,
                email=user_input[CONF_EMAIL],
                password=user_input[CONF_PASSWORD],
                preferred_pool=preferred_pool,
            )
            lan = await api.connect_lan(host=host, serial_number=serial, jwt=jwt)
            await lan.close()
        except DaedalusAuthError as exc:
            _LOGGER.warning(
                "Daedalus login refused (preferred_pool=%s, host=%s): %s",
                preferred_pool,
                host,
                exc,
            )
            errors["base"] = "all_pools_unauthorized" if str(exc).startswith("all_pools:") else "invalid_auth"
        except DaedalusConnectionError as exc:
            _LOGGER.warning(
                "Daedalus LAN probe failed (host=%s, serial=%s): %s",
                host,
                serial,
                exc,
            )
            errors["base"] = "cannot_connect"
        except Exception:  # noqa: BLE001 — last-resort net so the form still renders
            _LOGGER.exception("Unexpected error validating Daedalus machine")
            errors["base"] = "unknown"
        finally:
            await api.close()

        if errors:
            return self.async_show_form(step_id="user", data_schema=_USER_SCHEMA, errors=errors)

        # NOTE: password is intentionally NOT persisted in entry.data — Gigya
        # session_token + JWT cover both runtime auth (LAN AUTH frame) and
        # JWT rotation. If session_token is ever revoked we trigger reauth
        # via async_step_reauth and ask the user for the password again.
        return self.async_create_entry(
            title=f"My Coffee Lounge ({serial})",
            data={
                CONF_EMAIL: user_input[CONF_EMAIL],
                CONF_HOST: host,
                CONF_SERIAL_NUMBER: serial,
                CONF_MACHINE_NAME: serial,
                CONF_POOL: resolved_pool,
                CONF_JWT: jwt,
                CONF_SESSION_TOKEN: session_token,
            },
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        """HA-triggered reauth — typically when the stored session_token expires."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask only the password again; everything else stays as configured."""
        errors: dict[str, str] = {}
        entry = getattr(self, "_reauth_entry", None)

        if user_input is not None and entry is not None:
            email = entry.data[CONF_EMAIL]
            preferred_pool = entry.data.get(CONF_POOL, GIGYA_POOL_EU)
            password = user_input[CONF_PASSWORD]

            api = self._api_factory()
            try:
                resolved_pool, session_token, jwt = await _probe_pools(
                    api,
                    email=email,
                    password=password,
                    preferred_pool=preferred_pool,
                )
            except DaedalusAuthError as exc:
                _LOGGER.warning(
                    "Daedalus reauth refused (preferred_pool=%s): %s", preferred_pool, exc
                )
                errors["base"] = (
                    "all_pools_unauthorized"
                    if str(exc).startswith("all_pools:")
                    else "invalid_auth"
                )
            except Exception:  # noqa: BLE001 — reauth must always render a form
                _LOGGER.exception("Unexpected error during Daedalus reauth")
                errors["base"] = "unknown"
            else:
                self.hass.config_entries.async_update_entry(
                    entry,
                    data={
                        **entry.data,
                        CONF_POOL: resolved_pool,
                        CONF_JWT: jwt,
                        CONF_SESSION_TOKEN: session_token,
                    },
                )
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")
            finally:
                await api.close()

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={"email": entry.data[CONF_EMAIL] if entry else ""},
            errors=errors,
        )

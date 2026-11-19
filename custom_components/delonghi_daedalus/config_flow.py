"""Account discovery, machine selection and verified LAN setup."""

from __future__ import annotations

import asyncio
import re
import secrets
from collections import Counter
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.data_entry_flow import AbortFlow, FlowResult

from .api import (
    DaedalusApi,
    DaedalusAuthError,
    DaedalusConnectionError,
    DaedalusLanIdentityError,
    DaedalusLanSerialMismatchError,
)
from .cloud import CloudTelemetryError, DirectoryMachine, fetch_reported_lan_ip, list_machines
from .const import (
    CONF_AROMATIC_WARMUP,
    CONF_AROMATIC_WARMUP_PENDING,
    CONF_CLOUD_TELEMETRY,
    CONF_EMAIL,
    CONF_HOST,
    CONF_JWT,
    CONF_LAN_AUTH_MESSAGE,
    CONF_LAN_TLS_FINGERPRINT,
    CONF_MACHINE_NAME,
    CONF_MODEL,
    CONF_PASSWORD,
    CONF_POOL,
    CONF_SERIAL_NUMBER,
    CONF_SESSION_SECRET,
    CONF_SESSION_TOKEN,
    DOMAIN,
    GIGYA_API_KEYS,
    GIGYA_POOL_EU,
)
from .lan_protocol import LanProtocolError, validate_lan_host
from .lan_repairs import async_clear_lan_issue
from .redact import get_logger, sanitize_text
from .redact import mask_email as _mask_email
from .warmup import WarmupError
from .warmup_state import entry_lock, save_pending

_LOGGER = get_logger(__name__)

# Gigya errorCode returned when the account exists but has no access to this
# application's apiKey.  Happens when the account was registered via a
# different Gigya site (OIDC / OAuth2 redirect) rather than direct login.
_GIGYA_UNAUTHORIZED_USER = 403005


_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_POOL, default=GIGYA_POOL_EU): vol.In(list(GIGYA_API_KEYS.keys())),
        vol.Optional(CONF_CLOUD_TELEMETRY, default=False): bool,
    }
)


_CONF_DEVICE = "device"
_CONF_CLOUD_ONLY = "cloud_only"
_CONF_VERIFIED_FINGERPRINT = "verified_fingerprint"
_SERIAL_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")


async def _probe_pools(
    api: DaedalusApi,
    *,
    email: str,
    password: str,
    preferred_pool: str,
) -> tuple[str, str, str, str]:
    """Try Gigya login across all pools, preferred pool first.

    Returns (pool, session_token, session_secret, jwt) for the first pool
    that succeeds.

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
            session_token, session_secret, jwt = await api.login_and_get_jwt(
                email=email, password=password, api_key=api_key
            )
            if pool != preferred_pool:
                _LOGGER.info(
                    "Daedalus: preferred pool %s returned 403005, succeeded with %s",
                    preferred_pool,
                    pool,
                )
            return pool, session_token, session_secret, jwt
        except DaedalusAuthError as exc:
            last_exc = exc
            if exc.login_error_code != _GIGYA_UNAUTHORIZED_USER:
                raise  # credential refusal — no point probing other pools
            _LOGGER.debug("Daedalus: pool %s → 403005, probing next pool", pool)

    raise DaedalusAuthError(f"all_pools: every Gigya pool returned 403005 for {_mask_email(email)}") from last_exc


class DaedalusOptionsFlow(config_entries.OptionsFlow):
    """Post-setup options.

    The cloud telemetry toggle lives in entry.data (the sensor platforms
    gate on it at setup) and this integration registers no update listener,
    so flipping it updates entry.data and reloads the entry immediately.
    Without this flow, toggling meant deleting and re-adding the integration
    (issue #18, stivxgamer).
    """

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            async with entry_lock(self.hass, self._entry.entry_id):
                data = self._entry.data
                telemetry = bool(data.get(CONF_CLOUD_TELEMETRY))
                warmup = bool(data.get(CONF_AROMATIC_WARMUP))
                enabled = bool(user_input.get(CONF_CLOUD_TELEMETRY, telemetry))
                new_warmup = bool(user_input.get(CONF_AROMATIC_WARMUP, warmup))
                warmup_changed = data.get(CONF_POOL) == GIGYA_POOL_EU and new_warmup != warmup
                if telemetry and not enabled and not data.get(CONF_LAN_TLS_FINGERPRINT):
                    errors[CONF_CLOUD_TELEMETRY] = "lan_verification_required"
                if not errors and warmup_changed and new_warmup:
                    try:
                        await save_pending(self.hass, self._entry.entry_id, False)
                    except WarmupError:
                        errors["base"] = "warmup_storage_error"
                if not errors:
                    # Include any session credentials rotated during the storage write.
                    data = self._entry.data
                    new_data = {**data}
                    if enabled != telemetry:
                        new_data[CONF_CLOUD_TELEMETRY] = enabled
                    if warmup_changed and data.get(CONF_POOL) == GIGYA_POOL_EU:
                        new_data[CONF_AROMATIC_WARMUP] = new_warmup
                        if new_warmup:
                            new_data.pop(CONF_AROMATIC_WARMUP_PENDING, None)
                    if new_data != data:
                        self.hass.config_entries.async_update_entry(self._entry, data=new_data)
            if not errors:
                if enabled != telemetry or warmup_changed:
                    await self.hass.config_entries.async_reload(self._entry.entry_id)
                return self.async_create_entry(title="", data={})

        data = self._entry.data
        telemetry = bool(data.get(CONF_CLOUD_TELEMETRY))
        warmup = bool(data.get(CONF_AROMATIC_WARMUP))
        settings = {vol.Optional(CONF_CLOUD_TELEMETRY, default=telemetry): bool}
        if data.get(CONF_POOL) == GIGYA_POOL_EU:
            settings[vol.Optional(CONF_AROMATIC_WARMUP, default=warmup)] = bool
        return self.async_show_form(step_id="init", data_schema=vol.Schema(settings), errors=errors)


class DaedalusConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a Daedalus config flow."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> DaedalusOptionsFlow:
        return DaedalusOptionsFlow(config_entry)

    def __init__(self) -> None:
        super().__init__()
        self._api_factory = DaedalusApi
        self._data: dict[str, Any] = {}
        self._machines: dict[str, DirectoryMachine] = {}
        self._reconfigure_entry: ConfigEntry | None = None
        self._host = ""
        self._observed_fingerprint: str | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Sign in and list machines when the account directory is available."""
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            api = self._api_factory()
            try:
                pool, token, secret, jwt = await _probe_pools(
                    api,
                    email=email,
                    password=user_input[CONF_PASSWORD],
                    preferred_pool=user_input.get(CONF_POOL, GIGYA_POOL_EU),
                )
                self._data = {
                    CONF_EMAIL: email,
                    CONF_POOL: pool,
                    CONF_SESSION_TOKEN: token,
                    CONF_SESSION_SECRET: secret,
                    CONF_JWT: jwt,
                    CONF_CLOUD_TELEMETRY: bool(user_input.get(CONF_CLOUD_TELEMETRY, False)),
                }
                machines = await list_machines(api.http_session, jwt=jwt)
            except DaedalusAuthError as exc:
                _LOGGER.warning("Daedalus login refused")
                errors["base"] = "all_pools_unauthorized" if str(exc).startswith("all_pools:") else "invalid_auth"
            except (DaedalusConnectionError, TimeoutError):
                errors["base"] = "cannot_login"
            except CloudTelemetryError:
                return self._manual_form(errors={"base": "discovery_unavailable"})
            except Exception:  # noqa: BLE001 — server failures should leave the form usable
                _LOGGER.warning("Unexpected error while signing in to My Coffee Lounge")
                errors["base"] = "unknown"
            else:
                if not machines:
                    return self.async_abort(reason="no_devices")
                self._machines = {secrets.token_urlsafe(8): machine for machine in machines}
                return await self.async_step_device()
            finally:
                await api.close()
        return self.async_show_form(step_id="user", data_schema=_USER_SCHEMA, errors=errors)

    async def async_step_device(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Require an explicit machine choice, including for a one-machine account."""
        if user_input is not None and (machine := self._machines.get(user_input.get(_CONF_DEVICE))) is not None:
            await self._set_machine(machine)
            return await self.async_step_lan()
        titles = {key: self._machine_title(machine.model) for key, machine in self._machines.items()}
        counts = Counter(titles.values())
        choices = {
            key: f"{title} ({self._serial_hint(self._machines[key].serial)})" if counts[title] > 1 else title
            for key, title in titles.items()
        }
        errors = {_CONF_DEVICE: "invalid_device"} if user_input is not None else {}
        return self.async_show_form(
            step_id="device", data_schema=vol.Schema({vol.Required(_CONF_DEVICE): vol.In(choices)}), errors=errors
        )

    def _manual_form(self, *, errors: dict[str, str]) -> FlowResult:
        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema({vol.Required(CONF_SERIAL_NUMBER): str}),
            errors=errors,
        )

    async def async_step_manual(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Allow manual entry only when automatic discovery could not complete."""
        if user_input is None:
            return self._manual_form(errors={})
        serial = user_input[CONF_SERIAL_NUMBER].strip()
        if not _SERIAL_RE.fullmatch(serial):
            return self._manual_form(errors={CONF_SERIAL_NUMBER: "invalid_serial"})
        await self._set_machine(DirectoryMachine(serial, None, None))
        return await self.async_step_lan()

    async def _set_machine(self, machine: DirectoryMachine) -> None:
        email = self._data[CONF_EMAIL]
        serial = machine.serial
        await self.async_set_unique_id(f"{email.casefold()}:{serial.casefold()}")
        self._abort_if_unique_id_configured()
        # Older entries preserve the original case in their unique ID.
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if (
                entry.data.get(CONF_EMAIL, "").casefold() == email.casefold()
                and entry.data.get(CONF_SERIAL_NUMBER, "").casefold() == serial.casefold()
            ):
                raise AbortFlow("already_configured")
        self._data[CONF_SERIAL_NUMBER] = serial
        self._data[CONF_MACHINE_NAME] = machine.thing_name or serial
        if machine.model:
            self._data[CONF_MODEL] = machine.model
        if machine.thing_name:
            api = self._api_factory()
            try:
                self._host = (
                    await fetch_reported_lan_ip(
                        api.http_session, thing_name=machine.thing_name, jwt=self._data[CONF_JWT]
                    )
                ) or ""
            except (CloudTelemetryError, OSError):
                self._host = ""
            finally:
                await api.close()

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Change the LAN address or verify a new certificate for the existing machine."""
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if entry is None:
            return self.async_abort(reason="entry_missing")
        self._reconfigure_entry = entry
        self._data = dict(entry.data)
        self._host = self._data.get(CONF_HOST, "")
        return await self.async_step_lan(user_input)

    async def async_step_lan(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Validate the LAN address and inspect its TLS certificate without credentials."""
        if user_input is None:
            return self._lan_form(errors={})
        self._observed_fingerprint = None
        host = user_input.get(CONF_HOST, "").strip()
        if user_input.get(_CONF_CLOUD_ONLY) and self._data.get(CONF_CLOUD_TELEMETRY):
            self._host = ""
            return await self._finish_lan()
        try:
            self._host = validate_lan_host(host)
        except LanProtocolError:
            return self._lan_form(errors={CONF_HOST: "invalid_host"})

        api = self._api_factory()
        try:
            self._observed_fingerprint = await api.probe_lan_fingerprint(host=self._host)
        except DaedalusConnectionError:
            return self._lan_form(errors={"base": "cannot_connect"})
        finally:
            await api.close()
        if self._observed_fingerprint == self._data.get(CONF_LAN_TLS_FINGERPRINT):
            return await self._finish_lan()
        return await self.async_step_verify_lan()

    async def async_step_verify_lan(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Require approval before trusting a new certificate and sending a token."""
        if self._observed_fingerprint is None:
            return await self.async_step_lan()
        if user_input and user_input.get(_CONF_CLOUD_ONLY) and self._data.get(CONF_CLOUD_TELEMETRY):
            self._host = ""
            self._observed_fingerprint = None
            return await self._finish_lan()
        if user_input and user_input.get("change_address"):
            return await self.async_step_lan()
        if user_input and user_input.get(_CONF_VERIFIED_FINGERPRINT) is True:
            return await self._finish_lan()
        fields = {
            vol.Optional(_CONF_VERIFIED_FINGERPRINT, default=False): bool,
            vol.Optional("change_address", default=False): bool,
        }
        if self._data.get(CONF_CLOUD_TELEMETRY):
            fields[vol.Optional(_CONF_CLOUD_ONLY, default=False)] = bool
        fingerprint = self._observed_fingerprint.upper()
        return self.async_show_form(
            step_id="verify_lan",
            data_schema=vol.Schema(fields),
            errors={"base": "fingerprint_not_verified"} if user_input is not None else {},
            description_placeholders={
                "host": self._host,
                "serial": self._serial_hint(self._data[CONF_SERIAL_NUMBER]),
                "fingerprint": ":".join(fingerprint[i : i + 2] for i in range(0, len(fingerprint), 2)),
            },
        )

    def _lan_form(self, *, errors: dict[str, str]) -> FlowResult:
        fields = {vol.Optional(CONF_HOST, default=self._host): str}
        if self._data.get(CONF_CLOUD_TELEMETRY):
            fields[vol.Optional(_CONF_CLOUD_ONLY, default=False)] = bool
        return self.async_show_form(
            step_id="lan",
            data_schema=vol.Schema(fields),
            description_placeholders={"serial": self._serial_hint(self._data[CONF_SERIAL_NUMBER])},
            errors=errors,
        )

    @staticmethod
    def _machine_title(model: str | None) -> str:
        if model == "ECAM63070":
            return "PrimaDonna Aromatic"
        return model or "My Coffee Lounge"

    @staticmethod
    def _serial_hint(serial: str) -> str:
        return f"…{serial[-4:]}" if len(serial) > 4 else "…"

    async def _lookup_entry_machine(self, *, serial: str, jwt: str) -> DirectoryMachine | None:
        api = self._api_factory()
        try:
            async with asyncio.timeout(5):
                machines = await list_machines(api.http_session, jwt=jwt)
        except (CloudTelemetryError, TimeoutError):
            return None
        finally:
            await api.close()
        return next((machine for machine in machines if machine.serial.casefold() == serial.casefold()), None)

    async def _finish_lan(self) -> FlowResult:
        """Confirm verified authentication and commit the entry atomically."""
        changes = {CONF_HOST: self._host, CONF_LAN_TLS_FINGERPRINT: self._observed_fingerprint}
        entry = self._reconfigure_entry
        data = entry.data if entry is not None else self._data
        if self._observed_fingerprint is not None:
            try:
                changes.update(await self._validate_lan(data, self._observed_fingerprint))
            except DaedalusLanSerialMismatchError:
                return self._lan_form(errors={"base": "serial_mismatch"})
            except DaedalusLanIdentityError:
                return self._lan_form(errors={"base": "certificate_changed"})
            except DaedalusConnectionError:
                return self._lan_form(errors={"base": "cannot_connect"})
            except DaedalusAuthError:
                return self._lan_form(errors={"base": "lan_auth_rejected"})

        if entry is None:
            return self.async_create_entry(title=self._machine_title(data.get(CONF_MODEL)), data={**data, **changes})
        serial = data[CONF_SERIAL_NUMBER]
        if not data.get(CONF_MODEL) or data.get(CONF_MACHINE_NAME) in (None, serial):
            machine = await self._lookup_entry_machine(serial=serial, jwt=changes.get(CONF_JWT, data[CONF_JWT]))
            if machine is not None and machine.model:
                changes[CONF_MODEL] = machine.model
            if machine is not None and machine.thing_name:
                changes[CONF_MACHINE_NAME] = machine.thing_name
        async with entry_lock(self.hass, entry.entry_id):
            if not self._host and not entry.data.get(CONF_CLOUD_TELEMETRY):
                self._data[CONF_CLOUD_TELEMETRY] = False
                self._host = entry.data.get(CONF_HOST, "")
                return self._lan_form(errors={"base": "cloud_only_unavailable"})
            if entry.data.get(CONF_JWT) != data.get(CONF_JWT):
                changes.pop(CONF_JWT, None)
            updated = {**entry.data, **changes}
            title = entry.title
            if title in (f"My Coffee Lounge ({serial})", "My Coffee Lounge"):
                title = self._machine_title(updated.get(CONF_MODEL))
            self.hass.config_entries.async_update_entry(entry, title=title, data=updated)
        async_clear_lan_issue(self.hass, entry.entry_id)
        await self.hass.config_entries.async_reload(entry.entry_id)
        return self.async_abort(reason="reconfigure_successful")

    async def _validate_lan(self, data: dict[str, Any], fingerprint: str) -> dict[str, str]:
        api = self._api_factory()
        jwt = data[CONF_JWT]
        kwargs = {
            "host": self._host,
            "serial_number": data[CONF_SERIAL_NUMBER],
            "auth_message": data.get(CONF_LAN_AUTH_MESSAGE, "AUTH"),
            "fingerprint": fingerprint,
        }
        try:
            try:
                lan = await api.connect_lan(jwt=jwt, **kwargs)
            except DaedalusAuthError:
                if not (token := data.get(CONF_SESSION_TOKEN)):
                    raise
                pool = data.get(CONF_POOL, GIGYA_POOL_EU)
                jwt = await api.refresh_jwt(
                    session_token=token,
                    session_secret=data.get(CONF_SESSION_SECRET),
                    api_key=GIGYA_API_KEYS.get(pool, GIGYA_API_KEYS[GIGYA_POOL_EU]),
                )
                lan = await api.connect_lan(jwt=jwt, **kwargs)
            await lan.close()
            return {CONF_JWT: jwt, CONF_LAN_AUTH_MESSAGE: lan.auth_message}
        finally:
            await api.close()

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        """HA-triggered reauth — typically when the stored session_token expires."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Ask only the password again; everything else stays as configured."""
        errors: dict[str, str] = {}
        entry = getattr(self, "_reauth_entry", None)

        if user_input is not None and entry is not None:
            email = entry.data[CONF_EMAIL]
            preferred_pool = entry.data.get(CONF_POOL, GIGYA_POOL_EU)
            password = user_input[CONF_PASSWORD]

            api = self._api_factory()
            try:
                resolved_pool, session_token, session_secret, jwt = await _probe_pools(
                    api,
                    email=email,
                    password=password,
                    preferred_pool=preferred_pool,
                )
            except DaedalusAuthError as exc:
                _LOGGER.warning(
                    "Daedalus reauth refused (preferred_pool=%s): %s",
                    preferred_pool,
                    sanitize_text(exc, host=entry.data.get(CONF_HOST)),
                )
                errors["base"] = "all_pools_unauthorized" if str(exc).startswith("all_pools:") else "invalid_auth"
            except (DaedalusConnectionError, TimeoutError):
                errors["base"] = "cannot_login"
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
                        CONF_SESSION_SECRET: session_secret,
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

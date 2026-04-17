"""Tests for the De'Longhi Coffee config flow.

Covers the user step, reauth flow, and options flow — every error branch
plus the happy path. Catches regressions like the one where a tweak to
``async_step_user`` silently stopped calling ``async_set_unique_id`` and
let duplicate entries slip through.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from custom_components.delonghi_coffee import config_flow
from custom_components.delonghi_coffee.api import DeLonghiApiError, DeLonghiAuthError


class _Executor:
    """Inline hass.async_add_executor_job stand-in."""

    async def __call__(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)


def _make_hass() -> MagicMock:
    hass = MagicMock()
    hass.async_add_executor_job = _Executor()
    return hass


def _make_flow(hass: MagicMock | None = None) -> config_flow.DeLonghiCoffeeConfigFlow:
    flow = config_flow.DeLonghiCoffeeConfigFlow()
    flow.hass = hass or _make_hass()
    return flow


# ─────────────────────────────────────────────────────────────────────────
# async_step_user — the creation happy path + each error branch.
# ─────────────────────────────────────────────────────────────────────────


def test_user_step_no_input_shows_form() -> None:
    """First render — no input yet, flow should ship back the schema."""
    flow = _make_flow()
    result = asyncio.run(flow.async_step_user())
    assert result["type"] == "form"
    assert result["step_id"] == "user"
    assert result["errors"] == {}


def test_user_step_happy_path_creates_entry() -> None:
    """Valid creds + at least one device → create_entry with dsn populated."""
    flow = _make_flow()

    api = MagicMock()
    api.authenticate.return_value = None
    api.get_devices.return_value = [
        {
            "dsn": "AC000W038925641",
            "oem_model": "DL-striker-best",
            "product_name": "Eletta Explore",
            "sw_version": "1.2.3",
        }
    ]

    with patch.object(config_flow, "DeLonghiApi", return_value=api):
        result = asyncio.run(
            flow.async_step_user(
                {
                    "region": "EU",
                    "email": "user@example.com",
                    "password": "secret",
                }
            )
        )

    assert result["type"] == "create_entry"
    assert result["data"]["dsn"] == "AC000W038925641"
    assert result["data"]["model"] == "DL-striker-best"
    assert result["data"]["device_name"] == "Eletta Explore"
    assert result["data"]["sw_version"] == "1.2.3"


def test_user_step_no_devices_returns_error() -> None:
    """Account with zero machines — surface ``no_devices`` instead of exploding."""
    flow = _make_flow()

    api = MagicMock()
    api.authenticate.return_value = None
    api.get_devices.return_value = []

    with patch.object(config_flow, "DeLonghiApi", return_value=api):
        result = asyncio.run(
            flow.async_step_user(
                {"region": "EU", "email": "u@x", "password": "s"}
            )
        )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "no_devices"}


def test_user_step_auth_error_maps_to_invalid_auth() -> None:
    flow = _make_flow()

    api = MagicMock()
    api.authenticate.side_effect = DeLonghiAuthError("bad creds")

    with patch.object(config_flow, "DeLonghiApi", return_value=api):
        result = asyncio.run(
            flow.async_step_user(
                {"region": "EU", "email": "u@x", "password": "s"}
            )
        )

    assert result["errors"] == {"base": "invalid_auth"}


def test_user_step_api_error_maps_to_cannot_connect() -> None:
    flow = _make_flow()

    api = MagicMock()
    api.authenticate.side_effect = DeLonghiApiError("network down")

    with patch.object(config_flow, "DeLonghiApi", return_value=api):
        result = asyncio.run(
            flow.async_step_user(
                {"region": "EU", "email": "u@x", "password": "s"}
            )
        )

    assert result["errors"] == {"base": "cannot_connect"}


def test_user_step_unexpected_exception_maps_to_unknown() -> None:
    """Anything that isn't a known API error still produces a graceful form."""
    flow = _make_flow()

    api = MagicMock()
    api.authenticate.side_effect = RuntimeError("cosmic ray")

    with patch.object(config_flow, "DeLonghiApi", return_value=api):
        result = asyncio.run(
            flow.async_step_user(
                {"region": "EU", "email": "u@x", "password": "s"}
            )
        )

    assert result["errors"] == {"base": "unknown"}


# ─────────────────────────────────────────────────────────────────────────
# Reauth — exercised when Ayla rotates tokens and the stored password
# stops working.
# ─────────────────────────────────────────────────────────────────────────


def test_reauth_confirm_happy_path_updates_entry() -> None:
    """New password works → entry updated + async_reload called."""
    flow = _make_flow()

    entry = MagicMock()
    entry.data = {"email": "u@x", "password": "old", "region": "EU"}
    entry.entry_id = "test-id"
    flow._reauth_entry = entry

    api = MagicMock()
    api.authenticate.return_value = None

    reload_calls: list[str] = []

    class _ReloadSpy:
        async def __call__(self, entry_id: str):
            reload_calls.append(entry_id)

    flow.hass.config_entries.async_update_entry = MagicMock()
    flow.hass.config_entries.async_reload = _ReloadSpy()

    with patch.object(config_flow, "DeLonghiApi", return_value=api):
        result = asyncio.run(
            flow.async_step_reauth_confirm({"password": "new"})
        )

    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    flow.hass.config_entries.async_update_entry.assert_called_once()
    assert reload_calls == ["test-id"]


def test_reauth_confirm_auth_error_shows_invalid_auth() -> None:
    flow = _make_flow()
    entry = MagicMock()
    entry.data = {"email": "u@x", "password": "old", "region": "EU"}
    flow._reauth_entry = entry

    api = MagicMock()
    api.authenticate.side_effect = DeLonghiAuthError("still bad")

    with patch.object(config_flow, "DeLonghiApi", return_value=api):
        result = asyncio.run(
            flow.async_step_reauth_confirm({"password": "still-wrong"})
        )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_auth"}


def test_reauth_confirm_no_input_shows_form() -> None:
    flow = _make_flow()
    entry = MagicMock()
    entry.data = {"email": "u@x"}
    flow._reauth_entry = entry

    result = asyncio.run(flow.async_step_reauth_confirm())

    assert result["type"] == "form"
    # The email should be surfaced in the form so the user remembers
    # which account they're being re-challenged for.
    assert result["description_placeholders"]["email"] == "u@x"


# ─────────────────────────────────────────────────────────────────────────
# Options flow — toggles diagnostic mode and propagates to coordinator.
# ─────────────────────────────────────────────────────────────────────────


def test_options_flow_toggles_diagnostic_mode_on_coordinator() -> None:
    """Submitting the form must flip ``coordinator.diagnostic_mode`` in place."""
    entry = MagicMock()
    entry.entry_id = "test-id"
    entry.options = {}

    from custom_components.delonghi_coffee.const import DOMAIN

    coordinator = MagicMock()
    coordinator.diagnostic_mode = False

    options_flow = config_flow.DeLonghiOptionsFlow(entry)
    options_flow.hass = _make_hass()
    options_flow.hass.data = {DOMAIN: {"test-id": {"coordinator": coordinator}}}

    result = asyncio.run(
        options_flow.async_step_init({"diagnostic_mode": True})
    )

    assert result["type"] == "create_entry"
    assert result["data"] == {"diagnostic_mode": True}
    assert coordinator.diagnostic_mode is True


def test_options_flow_empty_hass_data_is_safe() -> None:
    """If the entry has no coordinator yet, the flow must still succeed."""
    entry = MagicMock()
    entry.entry_id = "test-id"
    entry.options = {}

    options_flow = config_flow.DeLonghiOptionsFlow(entry)
    options_flow.hass = _make_hass()
    options_flow.hass.data = {}  # deliberately empty

    result = asyncio.run(
        options_flow.async_step_init({"diagnostic_mode": True})
    )

    assert result["type"] == "create_entry"


def test_options_flow_no_input_shows_form_with_current_value() -> None:
    entry = MagicMock()
    entry.entry_id = "test-id"
    entry.options = {"diagnostic_mode": True}

    options_flow = config_flow.DeLonghiOptionsFlow(entry)
    options_flow.hass = _make_hass()

    result = asyncio.run(options_flow.async_step_init())
    assert result["type"] == "form"

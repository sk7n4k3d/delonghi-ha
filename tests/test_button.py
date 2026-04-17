"""Button platform tests.

Focus on the pure beverage → button metadata resolver so we can verify the
full set of beverages advertised by PrimaDonna Soul end up with proper
names/icons, without having to spin up a Home Assistant instance.
"""

import json
from pathlib import Path

from custom_components.delonghi_coffee.const import BEVERAGES, resolve_beverage_meta

TRANSLATIONS_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "delonghi_coffee" / "translations"


# The actual list captured from jostrasser / lodzen's PrimaDonna Soul
# (see issue #11, latest-3.txt attachment). This is the real ECAM61075MB
# output of parse_available_beverages after the v1.4 naming-convention fix.
PRIMADONNA_SOUL_BEVERAGES = [
    "2x_espresso",
    "americano",
    "brew_over_ice",
    "caffelatte",
    "capp_doppio",
    "capp_reverse",
    "cappuccino",
    "coffee_pot",
    "cortado",
    "doppio",
    "espr_macchiato",
    "espresso",
    "flat_white",
    "hot_milk",
    "hot_water",
    "latte_macchiato",
    "long_black",
    "long_coffee",
    "mug_to_go",
    "regular",
    "tea",
]


class TestResolveBeverageMeta:
    """Unit tests for the resolve_beverage_meta helper."""

    def test_known_beverage_returns_const_entry(self):
        meta, is_known = resolve_beverage_meta("espresso", {})
        assert is_known is True
        assert meta["name"] == BEVERAGES["espresso"]["name"]
        assert meta["icon"] == BEVERAGES["espresso"]["icon"]

    def test_custom_recipe_name_wins(self):
        meta, is_known = resolve_beverage_meta("custom_1", {"custom_1": "Morning Booster"})
        assert is_known is True
        assert meta["name"] == "Morning Booster"
        assert meta["icon"] == "mdi:coffee-to-go"

    def test_custom_recipe_fallback_when_no_name(self):
        meta, is_known = resolve_beverage_meta("custom_2", {})
        assert is_known is False
        assert meta["name"] == "Custom 2"
        assert meta["icon"] == "mdi:coffee"

    def test_unknown_beverage_fallback(self):
        meta, is_known = resolve_beverage_meta("mystery_brew", {})
        assert is_known is False
        assert meta["name"] == "Mystery Brew"
        assert meta["icon"] == "mdi:coffee"

    def test_custom_recipe_copy_is_independent(self):
        """Mutating the returned dict must not affect BEVERAGES."""
        meta, _ = resolve_beverage_meta("espresso", {})
        meta["name"] = "Mutated"
        assert BEVERAGES["espresso"]["name"] != "Mutated"


class TestPrimaDonnaSoulButtonCoverage:
    """Regression #11: every PrimaDonna Soul beverage gets a proper button."""

    def test_21_beverages_count(self):
        """Jostrasser / lodzen report exactly 21 advertised beverages."""
        assert len(PRIMADONNA_SOUL_BEVERAGES) == 21

    def test_every_beverage_resolves_to_known_meta(self):
        """None of the PrimaDonna Soul beverages should hit the fallback."""
        unknown: list[str] = []
        for bev in PRIMADONNA_SOUL_BEVERAGES:
            _, is_known = resolve_beverage_meta(bev, {})
            if not is_known:
                unknown.append(bev)
        assert not unknown, f"Unknown PrimaDonna beverage keys: {unknown}"

    def test_every_beverage_has_english_translation(self):
        """Every PrimaDonna Soul beverage needs a brew_{key} entry in en.json."""
        en = json.loads((TRANSLATIONS_DIR / "en.json").read_text(encoding="utf-8"))
        button_keys = set(en["entity"]["button"].keys())
        missing = [bev for bev in PRIMADONNA_SOUL_BEVERAGES if f"brew_{bev}" not in button_keys]
        assert not missing, f"PrimaDonna Soul beverages without en translation: {missing}"

    def test_every_beverage_has_french_translation(self):
        fr = json.loads((TRANSLATIONS_DIR / "fr.json").read_text(encoding="utf-8"))
        button_keys = set(fr["entity"]["button"].keys())
        missing = [bev for bev in PRIMADONNA_SOUL_BEVERAGES if f"brew_{bev}" not in button_keys]
        assert not missing, f"PrimaDonna Soul beverages without fr translation: {missing}"


# ─────────────────────────────────────────────────────────────────────────
# Button entity behaviour — availability gates + dispatch + error mapping.
# These are the bugs that silently cost users commands when the machine
# is asleep or offline. Keeping them green == buttons stay honest.
# ─────────────────────────────────────────────────────────────────────────

import asyncio  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

import pytest  # noqa: E402

from custom_components.delonghi_coffee.api import (  # noqa: E402
    DeLonghiApiError,
    DeLonghiAuthError,
)


class _Executor:
    """Stand-in for hass.async_add_executor_job — runs callables inline."""

    async def __call__(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)


def _make_coord(machine_state: str = "Ready", selected_profile: int = 1) -> MagicMock:
    coord = MagicMock()
    coord.data = {"machine_state": machine_state}
    coord.selected_profile = selected_profile
    coord.custom_recipe_names = {}
    coord.beverages = []
    return coord


def _make_hass() -> MagicMock:
    hass = MagicMock()
    hass.async_add_executor_job = _Executor()
    return hass


def _attach_hass(entity) -> None:
    """Entities rely on ``self.hass`` when dispatching to the executor."""
    entity.hass = _make_hass()


def _run(coro):
    return asyncio.run(coro)


class TestBrewButtonEntity:
    """DeLonghiBrewButton press / availability behaviour."""

    def _entity(self, state: str = "Ready"):
        from custom_components.delonghi_coffee.button import DeLonghiBrewButton

        api = MagicMock()
        coord = _make_coord(state)
        meta = {"name": "Espresso", "icon": "mdi:coffee"}
        entity = DeLonghiBrewButton(
            api=api,
            coordinator=coord,
            dsn="DSN",
            model="test",
            device_name="Test",
            sw_version="1.0",
            beverage_key="espresso",
            meta=meta,
        )
        _attach_hass(entity)
        entity.coordinator = coord
        return entity, api

    def test_available_when_ready(self):
        entity, _ = self._entity("Ready")
        assert entity.available is True

    def test_unavailable_when_off(self):
        entity, _ = self._entity("Off")
        assert entity.available is False

    def test_unavailable_when_sleeping(self):
        entity, _ = self._entity("Sleep")
        assert entity.available is False

    def test_press_dispatches_brew_beverage(self):
        entity, api = self._entity("Ready")
        _run(entity.async_press())
        api.brew_beverage.assert_called_once_with("DSN", "espresso", 1)

    def test_press_raises_home_assistant_error_on_api_error(self):
        from homeassistant.exceptions import HomeAssistantError

        entity, api = self._entity("Ready")
        api.brew_beverage.side_effect = DeLonghiApiError("boom")
        with pytest.raises(HomeAssistantError) as exc_info:
            _run(entity.async_press())
        assert "boom" in str(exc_info.value) or "espresso" in str(exc_info.value)

    def test_press_uses_selected_profile_from_coordinator(self):
        entity, api = self._entity("Ready")
        entity.coordinator.selected_profile = 4
        _run(entity.async_press())
        api.brew_beverage.assert_called_once_with("DSN", "espresso", 4)


class TestCancelButtonEntity:
    def _entity(self, state: str = "Brewing"):
        from custom_components.delonghi_coffee.button import DeLonghiCancelButton

        api = MagicMock()
        coord = _make_coord(state)
        entity = DeLonghiCancelButton(
            api=api, coordinator=coord, dsn="DSN", model="t", device_name="T", sw_version="1"
        )
        _attach_hass(entity)
        entity.coordinator = coord
        return entity, api

    def test_available_only_while_brewing(self):
        brewing, _ = self._entity("Brewing")
        assert brewing.available is True
        idle, _ = self._entity("Ready")
        assert idle.available is False
        off, _ = self._entity("Off")
        assert off.available is False

    def test_press_calls_cancel_brew(self):
        entity, api = self._entity("Brewing")
        _run(entity.async_press())
        api.cancel_brew.assert_called_once_with("DSN")

    def test_press_raises_on_auth_error(self):
        entity, api = self._entity("Brewing")
        api.cancel_brew.side_effect = DeLonghiAuthError("expired")
        from homeassistant.exceptions import HomeAssistantError
        with pytest.raises(HomeAssistantError):
            _run(entity.async_press())


class TestSyncButtonEntity:
    def _entity(self, state: str = "Ready"):
        from custom_components.delonghi_coffee.button import DeLonghiSyncButton

        api = MagicMock()
        coord = _make_coord(state, selected_profile=2)
        entity = DeLonghiSyncButton(
            api=api, coordinator=coord, dsn="DSN", model="t", device_name="T", sw_version="1"
        )
        _attach_hass(entity)
        entity.coordinator = coord
        return entity, api

    def test_available_when_ready(self):
        entity, _ = self._entity("Ready")
        assert entity.available is True

    def test_unavailable_when_off(self):
        entity, _ = self._entity("Off")
        assert entity.available is False

    def test_press_syncs_current_profile(self):
        entity, api = self._entity("Ready")
        _run(entity.async_press())
        api.sync_recipes.assert_called_once_with("DSN", 2)

    def test_press_defaults_to_profile_one_when_none_selected(self):
        entity, api = self._entity("Ready")
        entity.coordinator.selected_profile = None
        _run(entity.async_press())
        api.sync_recipes.assert_called_once_with("DSN", 1)


class TestLanDiagnosticButtonEntity:
    def _entity(self):
        from custom_components.delonghi_coffee.button import DeLonghiLanDiagnosticButton

        api = MagicMock()
        coord = _make_coord()
        entity = DeLonghiLanDiagnosticButton(
            api=api, coordinator=coord, dsn="DSN", model="t", device_name="T", sw_version="1"
        )
        _attach_hass(entity)
        return entity, api

    def test_press_fetches_lan_config_and_runs_diagnostic(self, monkeypatch):
        from custom_components.delonghi_coffee import button as button_mod
        from custom_components.delonghi_coffee.lan import LanDiagnosticResult

        entity, api = self._entity()
        api.get_lan_config.return_value = {"lanip_key": "K", "lan_ip": "1.2.3.4"}

        calls = []

        async def _fake_run(**kwargs):
            calls.append(kwargs)
            return LanDiagnosticResult(success=True, stage="teardown", details={})

        monkeypatch.setattr(button_mod, "run_lan_diagnostic", _fake_run)

        _run(entity.async_press())

        assert len(calls) == 1
        assert calls[0]["lan_key"] == "K"
        assert calls[0]["lan_ip"] == "1.2.3.4"
        assert calls[0]["dsn"] == "DSN"

    def test_press_raises_when_cloud_fetch_fails(self, monkeypatch):
        entity, api = self._entity()
        # Narrowed except only catches known API / network failure modes.
        # Use DeLonghiApiError rather than a bare RuntimeError — that's
        # what the API layer actually raises on cloud trouble.
        api.get_lan_config.side_effect = DeLonghiApiError("cloud down")

        from homeassistant.exceptions import HomeAssistantError
        with pytest.raises(HomeAssistantError):
            _run(entity.async_press())

    def test_press_raises_when_diagnostic_reports_failure(self, monkeypatch):
        from custom_components.delonghi_coffee import button as button_mod
        from custom_components.delonghi_coffee.lan import LanDiagnosticResult

        entity, api = self._entity()
        api.get_lan_config.return_value = {"lanip_key": "K", "lan_ip": "1.2.3.4"}

        async def _fake_run(**kwargs):
            return LanDiagnosticResult(success=False, stage="handshake", reason="boom")

        monkeypatch.setattr(button_mod, "run_lan_diagnostic", _fake_run)

        from homeassistant.exceptions import HomeAssistantError
        with pytest.raises(HomeAssistantError):
            _run(entity.async_press())

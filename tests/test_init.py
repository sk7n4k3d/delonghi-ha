"""Tests for custom_components.delonghi_coffee.__init__ — setup/unload/migrate/services."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import (  # noqa: E402
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
)

import custom_components.delonghi_coffee as init_mod  # noqa: E402
from custom_components.delonghi_coffee.api import (  # noqa: E402
    DeLonghiApiError,
    DeLonghiAuthError,
)
from custom_components.delonghi_coffee.const import DOMAIN, MODEL_NAMES  # noqa: E402


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


def _make_entry(
    *,
    version=2,
    minor_version=0,
    data=None,
    options=None,
    entry_id="eid-1",
):
    """Build a mock ConfigEntry."""
    entry = MagicMock()
    entry.version = version
    entry.minor_version = minor_version
    entry.entry_id = entry_id
    entry.data = (
        data
        if data is not None
        else {
            "email": "user@example.com",
            "password": "hunter2",
            "dsn": "DSN-ABC",
            "model": "DL-pd-soul",
            "region": "EU",
        }
    )
    entry.options = options if options is not None else {}
    return entry


def _make_hass():
    """Build a mock HomeAssistant with async_add_executor_job that calls sync."""
    hass = MagicMock()
    hass.data = {}

    async def _run_executor(func, *args, **kwargs):
        return func(*args, **kwargs)

    hass.async_add_executor_job = _run_executor
    hass.config_entries = MagicMock()
    hass.config_entries.async_update_entry = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    hass.services = MagicMock()
    hass.services.async_register = MagicMock()
    hass.services.async_remove = MagicMock()
    # Register-once guard: return False so _register_services actually wires
    # the handlers we need to assert against.
    hass.services.has_service = MagicMock(return_value=False)
    return hass


def _make_fake_api(**overrides):
    """Build a MagicMock API with the methods we care about."""
    api = MagicMock()
    api.authenticate = MagicMock()
    api.get_devices = MagicMock(return_value=[{"dsn": "DSN-ABC"}])
    api.device_name = overrides.get("device_name", "API_Name")
    api.sw_version = overrides.get("sw_version", "9.9.9")
    api.brew_custom = MagicMock()
    api.cancel_brew = MagicMock()
    api.sync_recipes = MagicMock()
    api.select_bean_system = MagicMock()
    api.write_bean_system = MagicMock()
    return api


def _make_fake_coord():
    coord = MagicMock()
    coord.async_config_entry_first_refresh = AsyncMock()
    coord.selected_profile = None
    coord.diagnostic_mode = False
    return coord


def _run_setup_entry(hass, entry, fake_api=None, fake_coord=None):
    """Patch DeLonghiApi + DeLonghiCoordinator, run async_setup_entry, return (api, coord)."""
    api = fake_api or _make_fake_api()
    coord = fake_coord or _make_fake_coord()
    with (
        patch.object(init_mod, "DeLonghiApi", return_value=api) as api_cls,
        patch.object(init_mod, "DeLonghiCoordinator", return_value=coord) as coord_cls,
    ):
        result = _run(init_mod.async_setup_entry(hass, entry))
    return result, api, coord, api_cls, coord_cls


def _collect_services(hass):
    """Collect registered service handlers, keyed by service name."""
    services = {}
    for call in hass.services.async_register.call_args_list:
        args = call[0]
        # async_register(domain, name, handler, ...)
        services[args[1]] = args[2]
    return services


# ---------------------------------------------------------------------------
# async_migrate_entry
# ---------------------------------------------------------------------------


class TestAsyncMigrateEntry:
    def test_v1_to_v2_adds_region_eu(self):
        hass = _make_hass()
        entry = _make_entry(
            version=1,
            data={"email": "u", "password": "p", "dsn": "D", "model": "m"},
        )
        ok = _run(init_mod.async_migrate_entry(hass, entry))
        assert ok is True
        hass.config_entries.async_update_entry.assert_called_once()
        call_kwargs = hass.config_entries.async_update_entry.call_args
        assert call_kwargs[0][0] is entry
        assert call_kwargs[1]["data"] == {
            "email": "u",
            "password": "p",
            "dsn": "D",
            "model": "m",
            "region": "EU",
        }
        assert call_kwargs[1]["version"] == 2

    def test_v2_is_noop(self):
        hass = _make_hass()
        entry = _make_entry(version=2)
        ok = _run(init_mod.async_migrate_entry(hass, entry))
        assert ok is True
        hass.config_entries.async_update_entry.assert_not_called()

    def test_v3_is_noop(self):
        hass = _make_hass()
        entry = _make_entry(version=3)
        ok = _run(init_mod.async_migrate_entry(hass, entry))
        assert ok is True
        hass.config_entries.async_update_entry.assert_not_called()


# ---------------------------------------------------------------------------
# async_setup_entry — happy path & auth/api errors
# ---------------------------------------------------------------------------


class TestAsyncSetupEntryHappyPath:
    def test_full_happy_path_fetches_device_info_from_api(self):
        hass = _make_hass()
        entry = _make_entry(
            data={
                "email": "u@x",
                "password": "pw",
                "dsn": "DSN-1",
                "model": "DL-pd-soul",
                "region": "EU",
            },
            options={"diagnostic_mode": True},
        )
        fake_api = _make_fake_api(device_name="PrimaDonnaSoul_UUID", sw_version="2.5")
        fake_coord = _make_fake_coord()
        result, api, coord, api_cls, coord_cls = _run_setup_entry(hass, entry, fake_api, fake_coord)

        assert result is True
        # API constructor received the correct args
        api_cls.assert_called_once_with("u@x", "pw", region="EU", oem_model="DL-pd-soul")
        # authenticate + get_devices both called (entry data missing device_name/sw_version)
        api.authenticate.assert_called_once()
        api.get_devices.assert_called_once()
        # Coordinator constructed with (hass, api, dsn)
        coord_cls.assert_called_once_with(hass, api, "DSN-1")
        # diagnostic_mode propagated
        assert coord.diagnostic_mode is True
        coord.async_config_entry_first_refresh.assert_awaited_once()
        # hass.data populated
        stored = hass.data[DOMAIN]["eid-1"]
        assert stored["api"] is api
        assert stored["coordinator"] is coord
        assert stored["dsn"] == "DSN-1"
        assert stored["model"] == MODEL_NAMES["DL-pd-soul"]  # "PrimaDonna Soul"
        assert stored["device_name"] == "PrimaDonnaSoul_UUID"
        assert stored["sw_version"] == "2.5"
        # Platforms forwarded
        hass.config_entries.async_forward_entry_setups.assert_awaited_once_with(entry, init_mod.PLATFORMS)
        # All 5 services registered
        names = {c[0][1] for c in hass.services.async_register.call_args_list}
        assert names == {
            "brew_custom",
            "cancel_brew",
            "sync_recipes",
            "select_bean_profile",
            "write_bean_profile",
        }

    def test_device_name_and_sw_version_already_in_entry_skip_get_devices(self):
        hass = _make_hass()
        entry = _make_entry(
            data={
                "email": "e",
                "password": "p",
                "dsn": "DSN-X",
                "model": "DL-pd-soul",
                "region": "EU",
                "device_name": "StoredName",
                "sw_version": "1.2.3",
            }
        )
        api = _make_fake_api()
        result, api, coord, *_ = _run_setup_entry(hass, entry, api)

        assert result is True
        api.authenticate.assert_called_once()
        api.get_devices.assert_not_called()
        stored = hass.data[DOMAIN]["eid-1"]
        assert stored["device_name"] == "StoredName"
        assert stored["sw_version"] == "1.2.3"

    def test_get_devices_api_error_is_swallowed(self):
        hass = _make_hass()
        entry = _make_entry()
        api = _make_fake_api()
        api.get_devices.side_effect = DeLonghiApiError("down")
        result, api, coord, *_ = _run_setup_entry(hass, entry, api)

        assert result is True
        # device_name ends up falling back to "De'Longhi {friendly}"
        stored = hass.data[DOMAIN]["eid-1"]
        assert stored["device_name"] == f"De'Longhi {MODEL_NAMES['DL-pd-soul']}"
        # sw_version stayed None
        assert stored["sw_version"] is None

    def test_get_devices_auth_error_is_swallowed(self):
        hass = _make_hass()
        entry = _make_entry()
        api = _make_fake_api()
        api.get_devices.side_effect = DeLonghiAuthError("token expired")
        result, api, coord, *_ = _run_setup_entry(hass, entry, api)

        assert result is True
        stored = hass.data[DOMAIN]["eid-1"]
        assert stored["device_name"] == f"De'Longhi {MODEL_NAMES['DL-pd-soul']}"

    def test_device_name_equals_dsn_triggers_friendly_fallback(self):
        hass = _make_hass()
        entry = _make_entry(
            data={
                "email": "e",
                "password": "p",
                "dsn": "DSN-EQ",
                "model": "DL-pd-soul",
                "region": "EU",
                "device_name": "DSN-EQ",  # equals dsn → fallback
                "sw_version": "9.9",
            }
        )
        result, api, coord, *_ = _run_setup_entry(hass, entry)
        stored = hass.data[DOMAIN]["eid-1"]
        assert stored["device_name"] == f"De'Longhi {MODEL_NAMES['DL-pd-soul']}"

    def test_unknown_model_returns_raw_oem_as_friendly(self):
        hass = _make_hass()
        entry = _make_entry(
            data={
                "email": "e",
                "password": "p",
                "dsn": "DSN-U",
                "model": "ECAM-ZZZZ-UNKNOWN",
                "region": "EU",
            }
        )
        result, api, coord, *_ = _run_setup_entry(hass, entry)
        stored = hass.data[DOMAIN]["eid-1"]
        # friendly_model falls back to oem_model when not in MODEL_NAMES
        assert stored["model"] == "ECAM-ZZZZ-UNKNOWN"

    def test_missing_model_defaults_to_unknown_literal(self):
        """When entry.data has no 'model', code uses 'unknown' as the oem fallback."""
        hass = _make_hass()
        entry = _make_entry(
            data={
                "email": "e",
                "password": "p",
                "dsn": "DSN-N",
                "region": "EU",
                # no "model"
            }
        )
        api = _make_fake_api()
        api.get_devices.return_value = []  # force friendly-name fallback branch
        result, api, coord, *_ = _run_setup_entry(hass, entry, api)
        stored = hass.data[DOMAIN]["eid-1"]
        # model default string from __init__ is "unknown"
        assert stored["model"] == "unknown"
        assert stored["device_name"] == "De'Longhi unknown"

    def test_diagnostic_mode_defaults_to_false_when_absent(self):
        hass = _make_hass()
        entry = _make_entry(options={})  # no diagnostic_mode
        result, api, coord, *_ = _run_setup_entry(hass, entry)
        assert coord.diagnostic_mode is False

    def test_region_defaults_to_eu_when_absent(self):
        hass = _make_hass()
        entry = _make_entry(
            data={
                "email": "e",
                "password": "p",
                "dsn": "DSN-R",
                "model": "DL-pd-soul",
                # no region
            }
        )
        _, api, coord, api_cls, _ = _run_setup_entry(hass, entry)
        # DeLonghiApi should be called with region="EU" as fallback
        assert api_cls.call_args[1]["region"] == "EU"

    def test_get_devices_returns_empty_list_skips_update(self):
        hass = _make_hass()
        entry = _make_entry()
        api = _make_fake_api()
        api.get_devices.return_value = []  # falsy → no update of device_name/sw_version
        api.device_name = "ShouldNotBeUsed"
        api.sw_version = "ShouldNotBeUsed"
        _, api, coord, *_ = _run_setup_entry(hass, entry, api)
        stored = hass.data[DOMAIN]["eid-1"]
        # Since devices was [] and entry had no device_name, fallback to friendly-name form
        assert stored["device_name"] == f"De'Longhi {MODEL_NAMES['DL-pd-soul']}"
        assert stored["sw_version"] is None


class TestAsyncSetupEntryErrors:
    def test_auth_error_raises_config_entry_auth_failed(self):
        hass = _make_hass()
        entry = _make_entry()
        api = _make_fake_api()
        api.authenticate.side_effect = DeLonghiAuthError("bad creds")
        with (
            patch.object(init_mod, "DeLonghiApi", return_value=api),
            patch.object(init_mod, "DeLonghiCoordinator", return_value=_make_fake_coord()),
            pytest.raises(ConfigEntryAuthFailed),
        ):
            _run(init_mod.async_setup_entry(hass, entry))

    def test_api_error_raises_config_entry_not_ready(self):
        hass = _make_hass()
        entry = _make_entry()
        api = _make_fake_api()
        api.authenticate.side_effect = DeLonghiApiError("backend down")
        with (
            patch.object(init_mod, "DeLonghiApi", return_value=api),
            patch.object(init_mod, "DeLonghiCoordinator", return_value=_make_fake_coord()),
            pytest.raises(ConfigEntryNotReady),
        ):
            _run(init_mod.async_setup_entry(hass, entry))


# ---------------------------------------------------------------------------
# Service handlers
# ---------------------------------------------------------------------------


class TestBrewCustomService:
    def _setup(self, coord_selected_profile=None):
        hass = _make_hass()
        entry = _make_entry()
        api = _make_fake_api()
        coord = _make_fake_coord()
        coord.selected_profile = coord_selected_profile
        _run_setup_entry(hass, entry, api, coord)
        services = _collect_services(hass)
        return hass, api, coord, services["brew_custom"]

    def test_brew_custom_happy_path_with_all_params(self):
        hass, api, coord, handler = self._setup(coord_selected_profile=3)
        call = MagicMock()
        call.data = {
            "beverage": "espresso",
            "coffee_qty": 40,
            "milk_qty": 0,
            "water_qty": 0,
            "taste": 4,
            "milk_froth": 1,
            "temperature": 2,
            "profile": 2,
        }
        _run(handler(call))
        api.brew_custom.assert_called_once_with("DSN-ABC", "espresso", 40, 0, 0, 4, 1, 2, 2)

    def test_brew_custom_uses_defaults_for_optional(self):
        hass, api, coord, handler = self._setup(coord_selected_profile=5)
        call = MagicMock()
        call.data = {"beverage": "americano"}
        _run(handler(call))
        # defaults: coffee_qty=None, milk_qty=None, water_qty=None,
        #           taste=3, milk_froth=2, temperature=1, profile=5 (coord.selected_profile)
        api.brew_custom.assert_called_once_with("DSN-ABC", "americano", None, None, None, 3, 2, 1, 5)

    def test_brew_custom_profile_fallback_to_1_when_coord_none(self):
        hass, api, coord, handler = self._setup(coord_selected_profile=None)
        call = MagicMock()
        call.data = {"beverage": "latte"}
        _run(handler(call))
        # coord.selected_profile is None → fallback to 1
        api.brew_custom.assert_called_once_with("DSN-ABC", "latte", None, None, None, 3, 2, 1, 1)

    def test_brew_custom_api_error_raises_home_assistant_error(self):
        hass, api, coord, handler = self._setup()
        api.brew_custom.side_effect = DeLonghiApiError("tank empty")
        call = MagicMock()
        call.data = {"beverage": "espresso"}
        with pytest.raises(HomeAssistantError, match="tank empty"):
            _run(handler(call))

    def test_brew_custom_auth_error_raises_home_assistant_error(self):
        hass, api, coord, handler = self._setup()
        api.brew_custom.side_effect = DeLonghiAuthError("expired")
        call = MagicMock()
        call.data = {"beverage": "espresso"}
        with pytest.raises(HomeAssistantError, match="expired"):
            _run(handler(call))


class TestCancelBrewService:
    def _setup(self):
        hass = _make_hass()
        entry = _make_entry()
        api = _make_fake_api()
        coord = _make_fake_coord()
        _run_setup_entry(hass, entry, api, coord)
        services = _collect_services(hass)
        return hass, api, services["cancel_brew"]

    def test_cancel_brew_calls_api(self):
        hass, api, handler = self._setup()
        call = MagicMock()
        call.data = {}
        _run(handler(call))
        api.cancel_brew.assert_called_once_with("DSN-ABC")

    def test_cancel_brew_api_error_raises_home_assistant_error(self):
        hass, api, handler = self._setup()
        api.cancel_brew.side_effect = DeLonghiApiError("nope")
        with pytest.raises(HomeAssistantError, match="nope"):
            _run(handler(MagicMock(data={})))

    def test_cancel_brew_auth_error_raises_home_assistant_error(self):
        hass, api, handler = self._setup()
        api.cancel_brew.side_effect = DeLonghiAuthError("401")
        with pytest.raises(HomeAssistantError, match="401"):
            _run(handler(MagicMock(data={})))


class TestSyncRecipesService:
    def _setup(self, coord_selected_profile=None):
        hass = _make_hass()
        entry = _make_entry()
        api = _make_fake_api()
        coord = _make_fake_coord()
        coord.selected_profile = coord_selected_profile
        _run_setup_entry(hass, entry, api, coord)
        services = _collect_services(hass)
        return hass, api, coord, services["sync_recipes"]

    def test_sync_recipes_explicit_profile(self):
        hass, api, coord, handler = self._setup(coord_selected_profile=2)
        call = MagicMock()
        call.data = {"profile": 4}
        _run(handler(call))
        api.sync_recipes.assert_called_once_with("DSN-ABC", 4)

    def test_sync_recipes_uses_selected_profile(self):
        hass, api, coord, handler = self._setup(coord_selected_profile=3)
        _run(handler(MagicMock(data={})))
        api.sync_recipes.assert_called_once_with("DSN-ABC", 3)

    def test_sync_recipes_fallback_to_1(self):
        hass, api, coord, handler = self._setup(coord_selected_profile=None)
        _run(handler(MagicMock(data={})))
        api.sync_recipes.assert_called_once_with("DSN-ABC", 1)

    def test_sync_recipes_error_raises_home_assistant_error(self):
        hass, api, coord, handler = self._setup()
        api.sync_recipes.side_effect = DeLonghiApiError("boom")
        with pytest.raises(HomeAssistantError, match="boom"):
            _run(handler(MagicMock(data={})))

    def test_sync_recipes_auth_error_raises_home_assistant_error(self):
        hass, api, coord, handler = self._setup()
        api.sync_recipes.side_effect = DeLonghiAuthError("creds")
        with pytest.raises(HomeAssistantError, match="creds"):
            _run(handler(MagicMock(data={})))


class TestSelectBeanProfileService:
    def _setup(self):
        hass = _make_hass()
        entry = _make_entry()
        api = _make_fake_api()
        coord = _make_fake_coord()
        _run_setup_entry(hass, entry, api, coord)
        services = _collect_services(hass)
        return hass, api, services["select_bean_profile"]

    def test_select_bean_profile_converts_slot_to_int(self):
        hass, api, handler = self._setup()
        call = MagicMock()
        call.data = {"slot": "2"}  # string → int conversion
        _run(handler(call))
        api.select_bean_system.assert_called_once_with("DSN-ABC", 2)

    def test_select_bean_profile_accepts_int_slot(self):
        hass, api, handler = self._setup()
        _run(handler(MagicMock(data={"slot": 3})))
        api.select_bean_system.assert_called_once_with("DSN-ABC", 3)

    def test_select_bean_profile_api_error_raises_ha_error(self):
        hass, api, handler = self._setup()
        api.select_bean_system.side_effect = DeLonghiApiError("slot invalid")
        with pytest.raises(HomeAssistantError, match="slot invalid"):
            _run(handler(MagicMock(data={"slot": 1})))

    def test_select_bean_profile_auth_error_raises_ha_error(self):
        hass, api, handler = self._setup()
        api.select_bean_system.side_effect = DeLonghiAuthError("auth")
        with pytest.raises(HomeAssistantError, match="auth"):
            _run(handler(MagicMock(data={"slot": 1})))


class TestWriteBeanProfileService:
    def _setup(self):
        hass = _make_hass()
        entry = _make_entry()
        api = _make_fake_api()
        coord = _make_fake_coord()
        _run_setup_entry(hass, entry, api, coord)
        services = _collect_services(hass)
        return hass, api, services["write_bean_profile"]

    def test_write_bean_profile_converts_all_ints(self):
        hass, api, handler = self._setup()
        call = MagicMock()
        call.data = {
            "slot": "1",
            "name": 12345,  # will be str()'d
            "temperature": "2",
            "intensity": "4",
            "grinder": "6",
            "flag1": "1",
            "flag2": "0",
        }
        _run(handler(call))
        api.write_bean_system.assert_called_once_with("DSN-ABC", 1, "12345", 2, 4, 6, 1, 0)

    def test_write_bean_profile_uses_defaults(self):
        hass, api, handler = self._setup()
        call = MagicMock()
        call.data = {"slot": 2, "name": "House Blend"}
        _run(handler(call))
        # defaults: temperature=0, intensity=0, grinder=0, flag1=0, flag2=1
        api.write_bean_system.assert_called_once_with("DSN-ABC", 2, "House Blend", 0, 0, 0, 0, 1)

    def test_write_bean_profile_api_error_raises_ha_error(self):
        hass, api, handler = self._setup()
        api.write_bean_system.side_effect = DeLonghiApiError("invalid slot")
        call = MagicMock()
        call.data = {"slot": 1, "name": "x"}
        with pytest.raises(HomeAssistantError, match="invalid slot"):
            _run(handler(call))

    def test_write_bean_profile_auth_error_raises_ha_error(self):
        hass, api, handler = self._setup()
        api.write_bean_system.side_effect = DeLonghiAuthError("401")
        call = MagicMock()
        call.data = {"slot": 1, "name": "x"}
        with pytest.raises(HomeAssistantError, match="401"):
            _run(handler(call))


# ---------------------------------------------------------------------------
# async_unload_entry
# ---------------------------------------------------------------------------


class TestAsyncUnloadEntry:
    def test_unload_success_last_entry_removes_services(self):
        hass = _make_hass()
        entry = _make_entry(entry_id="e1")
        hass.data[DOMAIN] = {"e1": {"anything": 1}}
        hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)

        ok = _run(init_mod.async_unload_entry(hass, entry))
        assert ok is True
        assert "e1" not in hass.data[DOMAIN]
        # All 5 services removed
        removed_names = {c[0][1] for c in hass.services.async_remove.call_args_list}
        assert removed_names == {
            "brew_custom",
            "cancel_brew",
            "sync_recipes",
            "select_bean_profile",
            "write_bean_profile",
        }

    def test_unload_success_other_entries_remain_keeps_services(self):
        hass = _make_hass()
        entry = _make_entry(entry_id="e1")
        hass.data[DOMAIN] = {"e1": {}, "e2": {"leftover": True}}
        hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)

        ok = _run(init_mod.async_unload_entry(hass, entry))
        assert ok is True
        assert "e1" not in hass.data[DOMAIN]
        assert "e2" in hass.data[DOMAIN]
        hass.services.async_remove.assert_not_called()

    def test_unload_platform_fail_keeps_data_and_services(self):
        hass = _make_hass()
        entry = _make_entry(entry_id="e1")
        hass.data[DOMAIN] = {"e1": {"keep": True}}
        hass.config_entries.async_unload_platforms = AsyncMock(return_value=False)

        ok = _run(init_mod.async_unload_entry(hass, entry))
        assert ok is False
        # Data NOT popped, services NOT removed
        assert "e1" in hass.data[DOMAIN]
        hass.services.async_remove.assert_not_called()


# ---------------------------------------------------------------------------
# Event loop autouse fixture (mirrors other test files)
# ---------------------------------------------------------------------------


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

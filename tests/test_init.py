"""Tests for custom_components.delonghi_coffee.__init__ — setup/unload/migrate/services."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import (  # noqa: E402
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
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
    coord.async_refresh_after_command = AsyncMock()
    coord.selected_profile = None
    coord.diagnostic_mode = False
    # Issue #78: services resolve the profile via resolve_profile(); mirror it.
    coord.resolve_profile = MagicMock(side_effect=lambda: coord.selected_profile)
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
        # Coordinator constructed with (hass, api, dsn) + the config entry
        # (explicit config_entry is what makes the reauth escalation
        # deterministic — audit 2026-11-05)
        coord_cls.assert_called_once_with(hass, api, "DSN-1", config_entry=entry)
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
        # All 7 services registered (5 original + 2 baseline override helpers)
        names = {c[0][1] for c in hass.services.async_register.call_args_list}
        assert names == {
            "brew_custom",
            "cancel_brew",
            "sync_recipes",
            "select_bean_profile",
            "write_bean_profile",
            "set_baseline_from_screen",
            "reset_local_baseline",
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

    def test_brew_custom_calls_resolver_not_selected_profile(self):
        """coderabbit #79: prove the service uses resolve_profile(), not a direct
        selected_profile read — make the two disagree."""
        hass, api, coord, handler = self._setup(coord_selected_profile=None)
        coord.resolve_profile = MagicMock(return_value=3)  # differs from selected (None)
        call = MagicMock()
        call.data = {"beverage": "americano"}
        _run(handler(call))
        coord.resolve_profile.assert_called_once()
        assert api.brew_custom.call_args[0][-1] == 3  # profile passed to API is the resolver's

    def test_brew_custom_requires_profile_when_unresolved(self):
        # Issue #78: no silent default to slot 1 — the service refuses when no
        # profile is given and none is selected.
        from homeassistant.exceptions import HomeAssistantError

        hass, api, coord, handler = self._setup(coord_selected_profile=None)
        call = MagicMock()
        call.data = {"beverage": "latte"}
        with pytest.raises(HomeAssistantError):
            _run(handler(call))
        api.brew_custom.assert_not_called()

    def test_brew_custom_omitted_profile_falls_back_to_resolver(self):
        # PR #79: `profile` absent → the resolver decides (saved explicit
        # choice 4 here) and that is what reaches the API.
        hass, api, coord, handler = self._setup(coord_selected_profile=4)
        call = MagicMock()
        call.data = {"beverage": "espresso"}
        _run(handler(call))
        coord.resolve_profile.assert_called_once()
        api.brew_custom.assert_called_once_with("DSN-ABC", "espresso", None, None, None, 3, 2, 1, 4)

    def test_brew_custom_explicit_profile_beats_selection_without_resolving(self):
        # An explicit `profile` is the caller's intent: the resolver must not
        # run at all, so a stale selection (2) cannot override it.
        hass, api, coord, handler = self._setup(coord_selected_profile=2)
        coord.resolve_profile = MagicMock(return_value=1)
        call = MagicMock()
        call.data = {"beverage": "espresso", "profile": 5}
        _run(handler(call))
        coord.resolve_profile.assert_not_called()
        api.brew_custom.assert_called_once_with("DSN-ABC", "espresso", None, None, None, 3, 2, 1, 5)

    @pytest.mark.parametrize("bad", [0, 6, 7, -1, "3", 2.5, None, True])
    def test_brew_custom_invalid_explicit_profile_raises_before_api(self, bad):
        # A present-but-falsy `profile: 0` used to fall through `or` into the
        # resolver and be silently dropped. It is now a caller error, raised
        # before any API call.
        hass, api, coord, handler = self._setup(coord_selected_profile=3)
        call = MagicMock()
        call.data = {"beverage": "espresso", "profile": bad}
        with pytest.raises(HomeAssistantError, match="Invalid profile"):
            _run(handler(call))
        api.brew_custom.assert_not_called()
        coord.resolve_profile.assert_not_called()

    def test_brew_custom_api_error_raises_home_assistant_error(self):
        # Issue #78: a profile must resolve before the API call; give one so the
        # test exercises the API-error path rather than the resolver gate.
        hass, api, coord, handler = self._setup(coord_selected_profile=1)
        api.brew_custom.side_effect = DeLonghiApiError("tank empty")
        call = MagicMock()
        call.data = {"beverage": "espresso"}
        with pytest.raises(HomeAssistantError, match="tank empty"):
            _run(handler(call))

    def test_brew_custom_auth_error_raises_home_assistant_error(self):
        hass, api, coord, handler = self._setup(coord_selected_profile=1)
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

    def test_sync_recipes_calls_resolver_not_selected_profile(self):
        """coderabbit #79: the service must consult resolve_profile(), not read
        selected_profile directly — make the two disagree."""
        hass, api, coord, handler = self._setup(coord_selected_profile=None)
        coord.resolve_profile = MagicMock(return_value=3)
        _run(handler(MagicMock(data={})))
        coord.resolve_profile.assert_called_once()
        api.sync_recipes.assert_called_once_with("DSN-ABC", 3)

    def test_sync_recipes_requires_profile_when_unresolved(self):
        # Issue #78: no silent default to slot 1.
        from homeassistant.exceptions import HomeAssistantError

        hass, api, coord, handler = self._setup(coord_selected_profile=None)
        with pytest.raises(HomeAssistantError):
            _run(handler(MagicMock(data={})))
        api.sync_recipes.assert_not_called()

    def test_sync_recipes_omitted_profile_falls_back_to_resolver(self):
        # PR #79: `profile` absent → resolver wins (saved explicit choice 4).
        hass, api, coord, handler = self._setup(coord_selected_profile=4)
        _run(handler(MagicMock(data={})))
        coord.resolve_profile.assert_called_once()
        api.sync_recipes.assert_called_once_with("DSN-ABC", 4)

    def test_sync_recipes_explicit_profile_beats_selection_without_resolving(self):
        hass, api, coord, handler = self._setup(coord_selected_profile=2)
        coord.resolve_profile = MagicMock(return_value=1)
        _run(handler(MagicMock(data={"profile": 5})))
        coord.resolve_profile.assert_not_called()
        api.sync_recipes.assert_called_once_with("DSN-ABC", 5)

    @pytest.mark.parametrize("bad", [0, 6, 7, -1, "3", 2.5, None, True])
    def test_sync_recipes_invalid_explicit_profile_raises_before_api(self, bad):
        hass, api, coord, handler = self._setup(coord_selected_profile=3)
        with pytest.raises(HomeAssistantError, match="Invalid profile"):
            _run(handler(MagicMock(data={"profile": bad})))
        api.sync_recipes.assert_not_called()
        coord.resolve_profile.assert_not_called()

    def test_sync_recipes_error_raises_home_assistant_error(self):
        # Issue #78: a profile must resolve before the API call; give one so the
        # test exercises the API-error path rather than the resolver gate.
        hass, api, coord, handler = self._setup(coord_selected_profile=1)
        api.sync_recipes.side_effect = DeLonghiApiError("boom")
        with pytest.raises(HomeAssistantError, match="boom"):
            _run(handler(MagicMock(data={})))

    def test_sync_recipes_auth_error_raises_home_assistant_error(self):
        hass, api, coord, handler = self._setup(coord_selected_profile=1)
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
        # All 7 services removed (5 original + 2 baseline override helpers)
        removed_names = {c[0][1] for c in hass.services.async_remove.call_args_list}
        assert removed_names == {
            "brew_custom",
            "cancel_brew",
            "sync_recipes",
            "select_bean_profile",
            "write_bean_profile",
            "set_baseline_from_screen",
            "reset_local_baseline",
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


class TestResetLocalBaselineService:
    """Issue #90: dropping every manual counter needs a deliberate confirm.

    Each baseline is a number somebody read off the machine's screen; losing
    one silently means a sensor silently reverts to a frozen cloud value.
    """

    def _setup(self, baselines=None):
        from custom_components.delonghi_coffee.local_baseline import LocalBaselineStore

        hass = _make_hass()
        entry = _make_entry()
        coord = _make_fake_coord()
        # _make_fake_coord leaves async_request_refresh a plain MagicMock;
        # the handler awaits it, so this service needs the awaitable twin.
        coord.async_request_refresh = AsyncMock()
        store = LocalBaselineStore(hass, "DSN-ABC")
        if baselines:
            _run(store.async_set_many(baselines))
        coord.local_baseline = store
        _run_setup_entry(hass, entry, _make_fake_api(), coord)
        return _collect_services(hass)["reset_local_baseline"], store, coord

    def test_without_confirm_nothing_is_dropped(self):
        handler, store, _ = self._setup({"espresso": 1476, "tea": 35})
        with pytest.raises(ServiceValidationError, match="No baseline was dropped"):
            _run(handler(MagicMock(data={})))
        assert store.get("espresso") == 1476
        assert store.get("tea") == 35

    def test_preview_lists_what_would_be_lost(self):
        handler, store, _ = self._setup({"espresso": 1476})
        stamp = store.get_set_at("espresso")
        with pytest.raises(ServiceValidationError) as exc:
            _run(handler(MagicMock(data={})))
        assert "espresso=1476" in str(exc.value)
        assert stamp in str(exc.value)

    def test_string_true_is_not_a_confirmation(self):
        """HA selectors hand back a bool, but a templated call can send "true"."""
        handler, store, _ = self._setup({"espresso": 1476})
        with pytest.raises(ServiceValidationError):
            _run(handler(MagicMock(data={"confirm": "true"})))
        assert store.get("espresso") == 1476

    def test_confirm_drops_everything_and_refreshes(self):
        handler, store, coord = self._setup({"espresso": 1476, "tea": 35})
        _run(handler(MagicMock(data={"confirm": True})))
        assert store.all_keys() == []
        coord.async_request_refresh.assert_called_once()

    def test_empty_store_reports_none_instead_of_crashing(self):
        handler, _, _ = self._setup()
        with pytest.raises(ServiceValidationError, match="none"):
            _run(handler(MagicMock(data={})))


class TestSetBaselineFromScreenInertness:
    """Issue #92: an entry the cloud already beats is stored, dated and then
    ignored forever by max(cloud, baseline). It must be accepted — a frozen or
    missing cloud is exactly when a baseline earns its keep — but said out loud.
    """

    def _setup(self, counters):
        from custom_components.delonghi_coffee.local_baseline import LocalBaselineStore

        hass = _make_hass()
        entry = _make_entry()
        coord = _make_fake_coord()
        coord.async_request_refresh = AsyncMock()
        coord.local_baseline = LocalBaselineStore(hass, "DSN-ABC")
        coord.data = {"counters": counters}
        _run_setup_entry(hass, entry, _make_fake_api(), coord)
        return hass, _collect_services(hass)["set_baseline_from_screen"], coord.local_baseline

    def test_inert_entry_is_stored_and_reported(self, caplog):
        import logging

        _, handler, store = self._setup({"total_espressos": 2472})
        with caplog.at_level(logging.WARNING, logger="custom_components.delonghi_coffee"):
            resp = _run(handler(MagicMock(data={"total_espressos": 1476})))
        # Accepted, not refused — that is the whole point of the issue.
        assert store.get("total_espressos") == 1476
        assert resp["inert"] == [{"counter": "total_espressos", "entered": 1476, "cloud": 2472}]
        assert resp["effective"] == []
        assert any("total_espressos=1476" in r.getMessage() and "2472" in r.getMessage() for r in caplog.records)
        assert any("inert" in n.lower() for n in resp["notes"])

    def test_entry_above_the_cloud_is_silent(self, caplog):
        import logging

        _, handler, store = self._setup({"espresso": 1406})
        with caplog.at_level(logging.WARNING, logger="custom_components.delonghi_coffee"):
            resp = _run(handler(MagicMock(data={"espresso": 1500})))
        assert store.get("espresso") == 1500
        assert resp["inert"] == []
        assert resp["effective"] == ["espresso"]
        assert resp["not_checked"] == []
        assert resp["notes"] == ["All stored values are above the cloud value and will be used."]
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_unknown_cloud_value_is_not_claimed_either_way(self, caplog):
        """No cloud number to compare against is not evidence of inertness."""
        import logging

        _, handler, store = self._setup({})
        with caplog.at_level(logging.WARNING, logger="custom_components.delonghi_coffee"):
            resp = _run(handler(MagicMock(data={"espresso": 1476})))
        assert store.get("espresso") == 1476
        assert resp["inert"] == []
        assert resp["not_checked"] == ["espresso"]
        assert any("Not checked" in n for n in resp["notes"])
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_mixed_call_flags_only_the_dead_key(self):
        _, handler, store = self._setup({"total_espressos": 2472, "espresso": 1406})
        resp = _run(handler(MagicMock(data={"total_espressos": 1476, "espresso": 1500})))
        assert [i["counter"] for i in resp["inert"]] == ["total_espressos"]
        assert resp["effective"] == ["espresso"]
        # Both are written; the live key is not held hostage by the dead one.
        assert store.get("total_espressos") == 1476
        assert store.get("espresso") == 1500

    def test_equal_to_the_cloud_counts_as_inert(self):
        """max() keeps the cloud value at equality too, so ≤ is the right test."""
        _, handler, _ = self._setup({"espresso": 1476})
        resp = _run(handler(MagicMock(data={"espresso": 1476})))
        assert [i["counter"] for i in resp["inert"]] == ["espresso"]

    def test_non_numeric_cloud_is_not_compared(self):
        """parse_counters keeps unparsable values as raw strings — comparing
        one would raise, and the entry must still be stored."""
        _, handler, store = self._setup({"espresso": "beaucoup"})
        resp = _run(handler(MagicMock(data={"espresso": 1476})))
        assert store.get("espresso") == 1476
        assert resp["not_checked"] == ["espresso"]

    def test_service_declares_an_optional_response(self):
        """OPTIONAL keeps callers that never ask for a response working."""
        from homeassistant.core import SupportsResponse

        hass = _make_hass()
        _run_setup_entry(hass, _make_entry(), _make_fake_api(), _make_fake_coord())
        calls = [c for c in hass.services.async_register.call_args_list if c[0][1] == "set_baseline_from_screen"]
        assert calls and calls[0].kwargs.get("supports_response") is SupportsResponse.OPTIONAL


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

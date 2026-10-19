"""ContentStack load-failure backoff.

A permanently unreachable CMS (e.g. eu-cdn.contentstack.com refused by a
home DNS filter) used to retrigger the fetch on *every* full refresh with a
full traceback, spamming the HA error log forever (observed 2026-10-19:
one ERROR block every 10 min since HA start). The coordinator must back off
exponentially, log the traceback once and degrade to single-line warnings.
"""

import asyncio
import logging
from time import monotonic
from unittest.mock import MagicMock, patch

from custom_components.delonghi_coffee import coordinator as coord_mod
from custom_components.delonghi_coffee.const import FULL_REFRESH_INTERVAL
from custom_components.delonghi_coffee.contentstack import ContentStackFetchError
from custom_components.delonghi_coffee.coordinator import (
    CONTENTSTACK_RETRY_MAX,
    DeLonghiCoordinator,
)

_LOGGER_NAME = "custom_components.delonghi_coffee.coordinator"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_hass():
    hass = MagicMock()

    async def _run_executor(func, *args, **kwargs):
        return func(*args, **kwargs)

    hass.async_add_executor_job = _run_executor
    return hass


def _make_api(**overrides):
    api = MagicMock()
    api.get_status = MagicMock(
        return_value={
            "machine_state": "Off",
            "profile": 0,
            "monitor_raw": None,
            "status": "RUN",
            "alarms": [],
            "alarm_word": None,
        }
    )
    api.parse_counters = MagicMock(return_value={})
    api.parse_profiles = MagicMock(return_value={"active": 1, "profiles": {}})
    api.parse_bean_systems = MagicMock(return_value=[])
    api.parse_bean_system_par = MagicMock(return_value={})
    api.parse_available_beverages = MagicMock(return_value=["espresso"])
    api.get_custom_recipe_names = MagicMock(return_value={})
    api.get_properties = MagicMock(return_value={"d270_serialnumber": {"value": "ECAM63050FOO"}})
    api.get_lan_config = MagicMock(return_value=None)
    api.ping_connected = MagicMock(return_value=True)
    api.request_monitor = MagicMock()
    api.rate_tracker = MagicMock(current_rate=0, total_calls=0)
    api._oem_model = "DL-striker-cb"
    api.model_info = {}
    for k, v in overrides.items():
        setattr(api, k, v)
    return api


def _make_coord():
    hass = _make_hass()
    api = _make_api()
    coord = DeLonghiCoordinator(hass, api, "DSN-CS")
    coord.hass = hass
    coord._last_all_props = {"d270_serialnumber": {"value": "ECAM63050FOO"}}
    return coord


def _failing_fetch():
    return patch.object(coord_mod, "fetch_drink_catalog", side_effect=ContentStackFetchError("prod_drink fetch failed"))


def test_init_backoff_defaults():
    coord = _make_coord()
    assert coord._cs_failures == 0
    # 0.0 → first attempt is never gated
    assert coord._cs_next_retry == 0.0


def test_failure_counts_and_sets_backoff_window():
    coord = _make_coord()
    with _failing_fetch(), patch.object(coord_mod, "fetch_bean_adapt"), patch.object(coord_mod, "fetch_coffee_beans"):
        _run(coord._load_contentstack())
    assert coord._contentstack_loaded is False
    assert coord._cs_failures == 1
    delay = coord._cs_next_retry - monotonic()
    # first retry must NOT be the bare 10-min refresh cycle, but stay sane
    assert FULL_REFRESH_INTERVAL < delay <= FULL_REFRESH_INTERVAL * 4


def test_backoff_grows_and_caps():
    coord = _make_coord()
    delays = []
    with _failing_fetch(), patch.object(coord_mod, "fetch_bean_adapt"), patch.object(coord_mod, "fetch_coffee_beans"):
        for expected_failures in range(1, 12):
            _run(coord._load_contentstack())
            assert coord._cs_failures == expected_failures
            delays.append(coord._cs_next_retry - monotonic())
    # strictly increasing until the cap
    growing = delays[:6]
    assert all(b > a for a, b in zip(growing, growing[1:], strict=False)), f"backoff must grow: {delays}"
    assert delays[-1] <= CONTENTSTACK_RETRY_MAX + 1
    assert delays[-1] >= CONTENTSTACK_RETRY_MAX - 2, "long failures must settle at the cap"


def test_first_failure_logs_traceback_then_single_line_warnings(caplog):
    coord = _make_coord()
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
    with _failing_fetch(), patch.object(coord_mod, "fetch_bean_adapt"), patch.object(coord_mod, "fetch_coffee_beans"):
        for _ in range(3):
            _run(coord._load_contentstack())
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR and r.name == _LOGGER_NAME]
    assert len(errors) == 1, f"traceback must appear once, got {len(errors)} ERROR records"
    assert errors[0].exc_info is not None, "first failure should keep the traceback"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and r.name == _LOGGER_NAME]
    assert len(warnings) == 2, f"later failures should be warnings, got {len(warnings)}"
    assert all(r.exc_info is None for r in warnings), "warning records must carry no traceback"


def test_success_resets_counter_and_logs_recovery(caplog):
    coord = _make_coord()
    with _failing_fetch(), patch.object(coord_mod, "fetch_bean_adapt"), patch.object(coord_mod, "fetch_coffee_beans"):
        _run(coord._load_contentstack())
    assert coord._cs_failures == 1
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
    with (
        patch.object(coord_mod, "fetch_drink_catalog", return_value={1: {"name": "Espresso"}}),
        patch.object(coord_mod, "fetch_bean_adapt", return_value=None),
        patch.object(coord_mod, "fetch_coffee_beans", return_value=[]),
    ):
        _run(coord._load_contentstack())
    assert coord._cs_failures == 0
    assert coord._cs_next_retry == 0.0
    recovery = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and r.name == _LOGGER_NAME and "recover" in r.message.lower()
    ]
    assert len(recovery) == 1, "recovery should be logged exactly once"


def _force_full_refresh(coord):
    # negative offset ⇒ (monotonic() - offset) >= FULL_REFRESH_INTERVAL always
    coord._last_full_refresh = -FULL_REFRESH_INTERVAL - 1
    coord._last_keepalive = -10_000_000


def test_refresh_gates_fetch_during_backoff_window():
    coord = _make_coord()
    coord._cs_next_retry = monotonic() + 3600  # backoff still open
    with (
        _failing_fetch() as m_dc,
        patch.object(coord_mod, "fetch_bean_adapt"),
        patch.object(coord_mod, "fetch_coffee_beans"),
    ):
        _force_full_refresh(coord)
        _run(coord._async_update_data())
    m_dc.assert_not_called()
    assert coord._contentstack_loaded is False


def test_refresh_retries_once_backoff_expires():
    coord = _make_coord()
    coord._cs_next_retry = monotonic() - 1  # window elapsed
    with (
        patch.object(coord_mod, "fetch_drink_catalog", return_value={}) as m_dc,
        patch.object(coord_mod, "fetch_bean_adapt", return_value=None),
        patch.object(coord_mod, "fetch_coffee_beans", return_value=[]),
    ):
        _force_full_refresh(coord)
        _run(coord._async_update_data())
        m_dc.assert_called_once()
    assert coord._contentstack_loaded is True

"""Switch platform for De'Longhi Coffee — power on/off toggle."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import DeLonghiApi, DeLonghiApiError, DeLonghiAuthError
from .const import (
    BLOCKING_ALARM_BITS,
    DOMAIN,
    POWER_OFF_CMD,
    POWER_ON_CMD,
    POWER_RETRY_DELAY,
    POWER_STALE_THRESHOLD,
    POWER_WAKE_DELAY,
)
from .coordinator import DeLonghiCoordinator
from .sensor import _device_info

_LOGGER = logging.getLogger(__name__)

# Re-exported under legacy names so existing tests and private callers still resolve.
_WAKE_DELAY: float = POWER_WAKE_DELAY
_RETRY_DELAY: float = POWER_RETRY_DELAY
_STALE_THRESHOLD: int = POWER_STALE_THRESHOLD


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up switch entity."""
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    api: DeLonghiApi = data["api"]
    coordinator: DeLonghiCoordinator = data["coordinator"]
    dsn: str = data["dsn"]
    model: str = data["model"]
    device_name: str = data["device_name"]
    sw_version: str | None = data.get("sw_version")

    async_add_entities([DeLonghiPowerSwitch(api, coordinator, dsn, model, device_name, sw_version)])


class DeLonghiPowerSwitch(CoordinatorEntity[DeLonghiCoordinator], SwitchEntity):
    """Switch to power on/off the coffee machine."""

    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(
        self,
        api: DeLonghiApi,
        coordinator: DeLonghiCoordinator,
        dsn: str,
        model: str,
        device_name: str,
        sw_version: str | None,
    ) -> None:
        super().__init__(coordinator)
        self._api = api
        self._dsn = dsn
        self._assumed_on = False
        self._last_commanded_on: bool | None = None
        self._monitor_stale_count: int = 0
        self._last_monitor_state: str | None = None
        self._cmd_lock = asyncio.Lock()
        # Track the background retry coroutine so it can be cancelled on
        # entity removal — otherwise it survives reloads as an orphan task.
        self._retry_task: asyncio.Task | None = None
        self._attr_unique_id = f"{dsn}_power"
        self._attr_has_entity_name = True
        self._attr_translation_key = "power"
        self._attr_icon = "mdi:coffee-maker"
        self._attr_device_info = _device_info(dsn, model, device_name, sw_version)

    @property
    def assumed_state(self) -> bool:
        """Always True — cloud monitor is unreliable and can show stale state."""
        return True

    @property
    def is_on(self) -> bool:
        """Return True if machine is on.

        Trust the monitor when available. After a power command, detect
        staleness if the monitor contradicts for 3+ consecutive polls.
        """
        state = self.coordinator.data.get("machine_state", "Unknown")

        if state == "Unknown":
            return self._assumed_on

        if self._last_commanded_on is not None:
            monitor_says_on = state not in ("Off", "Going to sleep")

            if monitor_says_on != self._last_commanded_on:
                if state == self._last_monitor_state:
                    self._monitor_stale_count += 1
                else:
                    self._monitor_stale_count = 1
            else:
                self._monitor_stale_count = 0
                self._last_commanded_on = None
                _LOGGER.debug("Switch: monitor confirmed %s state", state)

        self._last_monitor_state = state

        if self._monitor_stale_count >= _STALE_THRESHOLD and self._last_commanded_on is not None:
            return self._assumed_on

        result = state not in ("Off", "Going to sleep")
        if self._last_commanded_on is None:
            self._assumed_on = result
        return result

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Power on the machine.

        Sequence captured from official Coffee Link app via MITM:
        1. Wake ping (app_device_connected)
        2. Wait 15s for WiFi module to become receptive
        3. Send power ON command (app_data_request)
        4. Post-command ping (keeps MQTT session alive)
        5. If no confirmation after 3min, retry once
        """
        if self._cmd_lock.locked():
            _LOGGER.warning("Power command already in progress, ignoring")
            return

        # Surface blocking alarms upfront — without this, a turn_on with
        # Water Tank Empty (or any other blocking alarm) silently sends the
        # wake/POWER_ON commands and the machine sits in Turning On forever
        # while the user wonders why nothing happens.
        self._announce_blocking_alarms_for_power_on()

        async with self._cmd_lock:
            _LOGGER.info("Powering on %s", self._dsn)
            try:
                # Phase 1: Wake ping + delay (like app: ping → 15s wait)
                # Fallback to request_monitor if ping unsupported (PrimaDonna Soul et al.)
                try:
                    ping_ok = await self.hass.async_add_executor_job(self._api.ping_connected, self._dsn)
                    if not ping_ok:
                        await self.hass.async_add_executor_job(self._api.request_monitor, self._dsn)
                    _LOGGER.debug("Wake sent, waiting %.0fs", _WAKE_DELAY)
                    await asyncio.sleep(_WAKE_DELAY)
                except (DeLonghiApiError, DeLonghiAuthError):
                    _LOGGER.debug("Wake failed, sending power ON anyway")

                # Phase 2: Power ON command
                if not await self.coordinator.send_command_lan(POWER_ON_CMD):
                    await self.hass.async_add_executor_job(self._api.send_command, self._dsn, POWER_ON_CMD)
                _LOGGER.info("Power ON command sent")

                # Phase 3: Post-command refresh (force the machine to push its new state)
                try:
                    ping_ok = await self.hass.async_add_executor_job(self._api.ping_connected, self._dsn)
                    if not ping_ok:
                        await self.hass.async_add_executor_job(self._api.request_monitor, self._dsn)
                except (DeLonghiApiError, DeLonghiAuthError):
                    pass

                self._assumed_on = True
                self._last_commanded_on = True
                self._monitor_stale_count = 0
            except (DeLonghiApiError, DeLonghiAuthError) as err:
                raise HomeAssistantError(f"Failed to power on: {err}") from err
            self.async_write_ha_state()

            # Phase 4: Background retry after 3 min if monitor doesn't confirm.
            # Cancel any previous retry still pending from a prior command so
            # we never have two retries racing against the machine.
            if self._retry_task is not None and not self._retry_task.done():
                self._retry_task.cancel()
            self._retry_task = self.hass.async_create_task(self._retry_power_on())

    def _announce_blocking_alarms_for_power_on(self) -> None:
        """Surface blocking alarms before sending POWER_ON_CMD.

        The De'Longhi firmware accepts the wake-up + POWER_ON sequence even
        when a blocking alarm is active (Water Tank Empty, Hydraulic Problem,
        etc.). It just sits in Turning On indefinitely — never reaching Ready
        — leaving the user with no visible reason. We emit:
          - WARNING in the HA log with the explicit list of blocking alarms;
          - persistent_notification in the HA UI so the issue surfaces in the
            user's notification tray instead of buried in logs.
        We do *not* abort the command: some alarm states clear themselves
        once the wake sequence runs (e.g. transient probe blips after a
        long sleep), and short-circuiting power-on would regress users who
        actually want to wake the machine to clear an old alarm.
        """
        alarms = self.coordinator.data.get("alarms") or []
        blocking = [a for a in alarms if isinstance(a, dict) and a.get("bit") in BLOCKING_ALARM_BITS]
        if not blocking:
            return

        names = ", ".join(a.get("name", f"bit{a.get('bit')}") for a in blocking)
        _LOGGER.warning(
            "Power on requested with blocking alarms active — machine may stay in 'Turning On': %s",
            names,
        )

        # The persistent notification puts the same info in the HA UI so the
        # user sees it without reading logs. Failing to schedule the
        # notification (test harness, missing service) must never break the
        # power-on path.
        try:
            self.hass.async_create_task(
                self.hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "title": f"Cafetière bloquée — {self._dsn}",
                        "message": (
                            "La machine ne pourra pas finir de s'allumer tant que ces "
                            "alarmes restent actives :\n\n- "
                            + "\n- ".join(a.get("name", f"bit{a.get('bit')}") for a in blocking)
                        ),
                        "notification_id": f"delonghi_blocking_alarms_{self._dsn}",
                    },
                    blocking=False,
                )
            )
        except Exception as err:  # noqa: BLE001 — defensive, must never abort turn_on
            _LOGGER.debug("Skipping persistent_notification (harness or unsupported): %s", err)

    async def _retry_power_on(self) -> None:
        """Retry power on if monitor hasn't confirmed within 3 minutes.

        Aborts silently if the user issued a turn_off in the interval — the
        machine being Off is then the desired state, not a wake failure.
        Without this guard the retry would re-allume the machine three minutes
        after every quick turn_on → turn_off sequence (observed 2026-05-01).
        """
        await asyncio.sleep(_RETRY_DELAY)

        if self._last_commanded_on is not True:
            _LOGGER.debug("Power ON retry aborted: user turned off in between")
            return

        state = self.coordinator.data.get("machine_state", "Unknown")
        if state in ("Off", "Unknown", "Going to sleep"):
            _LOGGER.info("Power ON not confirmed after %.0fs, retrying", _RETRY_DELAY)
            try:
                ping_ok = await self.hass.async_add_executor_job(self._api.ping_connected, self._dsn)
                if not ping_ok:
                    await self.hass.async_add_executor_job(self._api.request_monitor, self._dsn)
                await asyncio.sleep(_WAKE_DELAY)
                if not await self.coordinator.send_command_lan(POWER_ON_CMD):
                    await self.hass.async_add_executor_job(self._api.send_command, self._dsn, POWER_ON_CMD)
                ping_ok = await self.hass.async_add_executor_job(self._api.ping_connected, self._dsn)
                if not ping_ok:
                    await self.hass.async_add_executor_job(self._api.request_monitor, self._dsn)
                _LOGGER.info("Power ON retry sent")
            except (DeLonghiApiError, DeLonghiAuthError) as err:
                _LOGGER.warning("Power ON retry failed: %s", err)
        else:
            _LOGGER.debug("Power ON confirmed by monitor (%s), no retry needed", state)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Power off the machine (standby)."""
        if self._cmd_lock.locked():
            _LOGGER.warning("Power command already in progress, ignoring")
            return

        async with self._cmd_lock:
            _LOGGER.info("Powering off %s", self._dsn)

            # Cancel any pending power-on retry — the user just changed their
            # mind. Without this, the retry would fire 3 min later, see
            # machine_state=Off (because we just turned it off), and renvoie
            # POWER_ON_CMD, re-allumant la machine contre la volonté du user.
            if self._retry_task is not None and not self._retry_task.done():
                self._retry_task.cancel()

            try:
                if not await self.coordinator.send_command_lan(POWER_OFF_CMD):
                    await self.hass.async_add_executor_job(self._api.send_command, self._dsn, POWER_OFF_CMD)

                # Post-command refresh — force the machine to push its new state
                # so the HA monitor reflects Off immediately instead of relying on
                # the next full refresh cycle.
                try:
                    ping_ok = await self.hass.async_add_executor_job(self._api.ping_connected, self._dsn)
                    if not ping_ok:
                        await self.hass.async_add_executor_job(self._api.request_monitor, self._dsn)
                except (DeLonghiApiError, DeLonghiAuthError):
                    pass

                self._assumed_on = False
                self._last_commanded_on = False
                self._monitor_stale_count = 0
            except (DeLonghiApiError, DeLonghiAuthError) as err:
                raise HomeAssistantError(f"Failed to power off: {err}") from err
            self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Cancel the pending power-on retry task, if any, before teardown."""
        if self._retry_task is not None and not self._retry_task.done():
            self._retry_task.cancel()
            # Retry body already swallows API errors; swallow cancel too.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._retry_task
        self._retry_task = None
        await super().async_will_remove_from_hass()

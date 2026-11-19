"""Persist one write-ahead warm-up marker per Home Assistant entry."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .warmup import WarmupError

_LOCKS = f"{DOMAIN}_warmup_locks"


def entry_lock(hass: HomeAssistant, entry_id: str) -> asyncio.Lock:
    """Serialize sends and option changes for one config entry."""
    locks: dict[str, asyncio.Lock] = hass.data.setdefault(_LOCKS, {})
    if entry_id not in locks:
        locks[entry_id] = asyncio.Lock()
    return locks[entry_id]


def _store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    return Store(hass, 1, f"{DOMAIN}.warmup.{entry_id}", atomic_writes=True)


async def is_pending(hass: HomeAssistant, entry_id: str) -> bool:
    """Read the marker with the entry lock held."""
    try:
        marker = await _store(hass, entry_id).async_load()
    except (HomeAssistantError, OSError):
        raise WarmupError("Could not read the warm-up attempt marker.") from None
    return marker is not None and (not isinstance(marker, dict) or marker.get("pending") is not False)


async def save_pending(hass: HomeAssistant, entry_id: str, pending: bool) -> None:
    """Persist and verify the marker with the entry lock held."""

    async def write() -> None:
        marker = {"pending": pending, "revision": uuid4().hex}
        try:
            await _store(hass, entry_id).async_save(marker)
            # Home Assistant can log a write error without raising. Read
            # back through a fresh Store so buffered data cannot qualify.
            if await _store(hass, entry_id).async_load() == marker:
                return
        except (HomeAssistantError, OSError):
            pass
        raise WarmupError("Could not save the warm-up attempt marker.")

    task = asyncio.create_task(write())
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        # The storage executor can keep writing after cancellation. Hold the
        # entry lock until it finishes, so a reset cannot race that write.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except WarmupError:
                break
        if not task.cancelled():
            task.exception()
        raise

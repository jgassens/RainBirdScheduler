"""Rain Bird Scheduler: intent-based irrigation on the core integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import SchedulerCoordinator
from .storage import SchedulerStorage

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CALENDAR,
    Platform.EVENT,
    Platform.SENSOR,
    Platform.SWITCH,
]

type SchedulerConfigEntry = ConfigEntry[SchedulerCoordinator]

DATA_SERVICES_REGISTERED = f"{DOMAIN}_services_registered"
DATA_WEBSOCKET_REGISTERED = f"{DOMAIN}_websocket_registered"
DATA_PANEL_REGISTERED = f"{DOMAIN}_panel_registered"


async def async_setup_entry(
    hass: HomeAssistant, entry: SchedulerConfigEntry
) -> bool:
    """Set up one scheduler for one Rain Bird controller."""
    coordinator = SchedulerCoordinator(hass, entry)
    await coordinator.async_setup()
    entry.runtime_data = coordinator

    if not hass.data.get(DATA_SERVICES_REGISTERED):
        from .services import async_register_services

        async_register_services(hass)
        hass.data[DATA_SERVICES_REGISTERED] = True

    if not hass.data.get(DATA_WEBSOCKET_REGISTERED):
        from .websocket import async_register_websocket_commands

        async_register_websocket_commands(hass)
        hass.data[DATA_WEBSOCKET_REGISTERED] = True

    if not hass.data.get(DATA_PANEL_REGISTERED):
        from .frontend import async_register_panel

        await async_register_panel(hass)
        hass.data[DATA_PANEL_REGISTERED] = True

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: SchedulerConfigEntry
) -> bool:
    """Unload a scheduler entry (§11.4 ordering: snapshot, then unload)."""
    coordinator = entry.runtime_data
    await coordinator.async_shutdown()
    unloaded = await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS
    )

    # One global panel: remove it when the final scheduler entry unloads.
    if not any(
        other.entry_id != entry.entry_id
        for other in hass.config_entries.async_loaded_entries(DOMAIN)
    ):
        from .frontend import async_unregister_panel

        async_unregister_panel(hass)
        hass.data[DATA_PANEL_REGISTERED] = False
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete this entry's stores; programs and history go with it."""
    await SchedulerStorage(hass, entry.entry_id).async_remove()

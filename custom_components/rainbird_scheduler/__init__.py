"""Rain Bird Scheduler: intent-based irrigation on the core integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

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
DATA_STATIC_PATHS_REGISTERED = f"{DOMAIN}_static_paths_registered"
DATA_PANEL_REGISTERED = f"{DOMAIN}_panel_registered"


async def async_setup_entry(
    hass: HomeAssistant, entry: SchedulerConfigEntry
) -> bool:
    """Set up one scheduler for one Rain Bird controller."""
    coordinator = SchedulerCoordinator(hass, entry)
    await coordinator.async_setup()
    entry.runtime_data = coordinator
    try:
        if not hass.data.get(DATA_SERVICES_REGISTERED):
            from .services import async_register_services

            async_register_services(hass)
            hass.data[DATA_SERVICES_REGISTERED] = True

        if not hass.data.get(DATA_WEBSOCKET_REGISTERED):
            from .websocket import async_register_websocket_commands

            async_register_websocket_commands(hass)
            hass.data[DATA_WEBSOCKET_REGISTERED] = True

        # Static routes cannot be unregistered: register them exactly once
        # per HA lifetime so reloading the last entry never collides with
        # the previous registration. The flag is claimed before the first
        # await so two entries setting up concurrently cannot both pass
        # the check (registration is idempotent-safe afterwards).
        if not hass.data.get(DATA_STATIC_PATHS_REGISTERED):
            from .frontend import async_register_static_paths

            hass.data[DATA_STATIC_PATHS_REGISTERED] = True
            await async_register_static_paths(hass)

        if not hass.data.get(DATA_PANEL_REGISTERED):
            from .frontend import async_register_panel

            hass.data[DATA_PANEL_REGISTERED] = True
            await async_register_panel(hass)

        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        # HA never calls async_unload_entry for an entry that never
        # loaded; a failed setup must not leak a live coordinator.
        await coordinator.async_shutdown()
        raise
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
    # The versioned static route stays — it cannot be unregistered and is
    # harmless while unloaded.
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
    storage = SchedulerStorage(hass, entry.entry_id)
    # The coordinator names its repair issues after the entry id and its
    # zone ids; collect the zone ids before the config store is gone.
    config = await storage.async_load_config()
    await storage.async_remove()
    ir.async_delete_issue(hass, DOMAIN, f"missing_source_{entry.entry_id}")
    if config is not None:
        for zone_id in config.zones:
            ir.async_delete_issue(hass, DOMAIN, f"zone_unresolved_{zone_id}")

"""Base entity: helper entities link to the source controller device (§9)."""

from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.device import async_entity_id_to_device
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .coordinator import SIGNAL_STATE, SchedulerCoordinator


def controller_source_entity(coordinator: SchedulerCoordinator) -> str | None:
    """Pick a controller-level source entity for device linkage."""
    for candidate in (
        coordinator._rain_delay_entity,
        coordinator._rain_sensor_entity,
        coordinator._native_calendar_entity,
        coordinator._any_zone_entity(),
    ):
        if candidate:
            return candidate
    return None


class RainBirdSchedulerEntity(Entity):
    """Scheduler entity linked to the Rain Bird controller device."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: SchedulerCoordinator, key: str) -> None:
        self.coordinator = coordinator
        entry = coordinator.entry
        self._attr_unique_id = f"{entry.unique_id or entry.entry_id}_{key}"
        source_entity = controller_source_entity(coordinator)
        if source_entity is not None:
            # Assign device_entry directly; never add our config entry to
            # the source integration's device (Core 2026.8 rule).
            self.device_entry = async_entity_id_to_device(
                coordinator.hass, source_entity
            )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_STATE.format(self.coordinator.entry.entry_id),
                self._handle_update,
            )
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

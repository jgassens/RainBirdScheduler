"""Lifecycle event entity (plan §29).

The event entity exposes run lifecycle events for automations and UI
discovery. It is not the history database; the bounded history store
remains authoritative.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SchedulerConfigEntry
from .const import LIFECYCLE_EVENT_TYPES
from .coordinator import SIGNAL_LIFECYCLE, SchedulerCoordinator
from .entity import RainBirdSchedulerEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SchedulerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([IrrigationLifecycleEvent(entry.runtime_data)])


class IrrigationLifecycleEvent(RainBirdSchedulerEntity, EventEntity):
    _attr_translation_key = "irrigation_lifecycle"
    _attr_event_types = LIFECYCLE_EVENT_TYPES

    def __init__(self, coordinator: SchedulerCoordinator) -> None:
        super().__init__(coordinator, "irrigation_lifecycle")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_LIFECYCLE.format(self.coordinator.entry.entry_id),
                self._handle_lifecycle_event,
            )
        )

    @callback
    def _handle_lifecycle_event(
        self, event_type: str, data: dict[str, Any]
    ) -> None:
        self._trigger_event(event_type, data)
        self.async_write_ha_state()

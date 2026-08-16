"""Stop-controller button (plan §7, §30).

The label is deliberately "Stop controller": turning off any Rain Bird zone
stops ALL watering on the controller, so no per-zone stop is offered.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SchedulerConfigEntry
from .coordinator import SchedulerCoordinator
from .entity import RainBirdSchedulerEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SchedulerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([StopControllerButton(entry.runtime_data)])


class StopControllerButton(RainBirdSchedulerEntity, ButtonEntity):
    _attr_translation_key = "stop_controller"

    def __init__(self, coordinator: SchedulerCoordinator) -> None:
        super().__init__(coordinator, "stop_controller")

    async def async_press(self) -> None:
        await self.coordinator.executor.async_stop()

"""Scheduler-enabled switch (plan §30)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
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
    async_add_entities([SchedulerEnabledSwitch(entry.runtime_data)])


class SchedulerEnabledSwitch(RainBirdSchedulerEntity, SwitchEntity):
    _attr_translation_key = "scheduler_enabled"

    def __init__(self, coordinator: SchedulerCoordinator) -> None:
        super().__init__(coordinator, "scheduler_enabled")

    @property
    def is_on(self) -> bool:
        return self.coordinator.config.controller.enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_enabled(False)

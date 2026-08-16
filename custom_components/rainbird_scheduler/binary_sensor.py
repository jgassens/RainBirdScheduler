"""Controller-level binary sensors (plan §30)."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SchedulerConfigEntry
from .coordinator import SchedulerCoordinator
from .entity import RainBirdSchedulerEntity
from .models import ExecutorState

_RUNNING_STATES = {
    ExecutorState.STARTING,
    ExecutorState.WATERING,
    ExecutorState.INTER_ZONE_GAP,
    ExecutorState.RECONCILING,
    ExecutorState.STOPPING,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SchedulerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [
            SchedulerRunningSensor(coordinator),
            NativeConflictSensor(coordinator),
            ExternalWateringSensor(coordinator),
        ]
    )


class SchedulerRunningSensor(RainBirdSchedulerEntity, BinarySensorEntity):
    _attr_translation_key = "scheduler_running"
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, coordinator: SchedulerCoordinator) -> None:
        super().__init__(coordinator, "scheduler_running")

    @property
    def is_on(self) -> bool:
        return self.coordinator.executor_state in _RUNNING_STATES

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        return {"executor_state": self.coordinator.executor_state.value}


class NativeConflictSensor(RainBirdSchedulerEntity, BinarySensorEntity):
    _attr_translation_key = "native_schedule_conflict"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: SchedulerCoordinator) -> None:
        super().__init__(coordinator, "native_schedule_conflict")

    @property
    def is_on(self) -> bool:
        return self.coordinator.native_schedule_conflict


class ExternalWateringSensor(RainBirdSchedulerEntity, BinarySensorEntity):
    _attr_translation_key = "external_watering"

    def __init__(self, coordinator: SchedulerCoordinator) -> None:
        super().__init__(coordinator, "external_watering")

    @property
    def is_on(self) -> bool:
        return self.coordinator.external_watering

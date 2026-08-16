"""Controller-level sensors (plan §30)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
)
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
    coordinator = entry.runtime_data
    async_add_entities(
        [
            NextIrrigationSensor(coordinator),
            ActiveZoneSensor(coordinator),
            ExpectedEndSensor(coordinator),
            SeasonalAdjustmentSensor(coordinator),
            LastRunSensor(coordinator),
        ]
    )


class NextIrrigationSensor(RainBirdSchedulerEntity, SensorEntity):
    _attr_translation_key = "next_irrigation"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: SchedulerCoordinator) -> None:
        super().__init__(coordinator, "next_irrigation")

    @property
    def native_value(self) -> datetime | None:
        run = self.coordinator.next_pending_run()
        if run is None or not run.steps:
            return None
        return run.steps[0].planned_start_utc

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        run = self.coordinator.next_pending_run()
        if run is None:
            return {}
        return {
            "program": run.program_name,
            "requested_start": run.requested_start_utc.isoformat(),
            "zones": [step.zone_name for step in run.steps],
        }


class ActiveZoneSensor(RainBirdSchedulerEntity, SensorEntity):
    _attr_translation_key = "active_zone"

    def __init__(self, coordinator: SchedulerCoordinator) -> None:
        super().__init__(coordinator, "active_zone")

    @property
    def native_value(self) -> str | None:
        executor = self.coordinator.executor
        if executor is None or not executor.is_active:
            return None
        step = self.coordinator.active_step()
        if step is None:
            return None
        return step.zone_name

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        step = self.coordinator.active_step()
        journal = self.coordinator.executor.journal
        if step is None or journal.run_plan is None:
            return {}
        return {
            "program": journal.run_plan.program_name,
            "cycle": f"{step.cycle_index}/{step.cycle_count}",
            "duration_minutes": step.duration_minutes,
            "executor_state": journal.state.value,
        }


class ExpectedEndSensor(RainBirdSchedulerEntity, SensorEntity):
    _attr_translation_key = "expected_end"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: SchedulerCoordinator) -> None:
        super().__init__(coordinator, "expected_end")

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.executor.journal.current_step_expected_end


class SeasonalAdjustmentSensor(RainBirdSchedulerEntity, SensorEntity):
    _attr_translation_key = "seasonal_adjustment"
    _attr_native_unit_of_measurement = "%"

    def __init__(self, coordinator: SchedulerCoordinator) -> None:
        super().__init__(coordinator, "seasonal_adjustment")

    @property
    def native_value(self) -> float | None:
        journal = self.coordinator.executor.journal
        plan = journal.run_plan or self.coordinator.next_pending_run()
        if plan is None:
            return None
        snapshot = plan.adjustment_snapshot
        if not snapshot.per_zone:
            return 100.0
        factors = [
            float(result.seasonal_factor)
            for result in snapshot.per_zone.values()
        ]
        return round(sum(factors) / len(factors), 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        journal = self.coordinator.executor.journal
        plan = journal.run_plan or self.coordinator.next_pending_run()
        if plan is None:
            return {}
        return {"provider": plan.adjustment_snapshot.provider_kind.value}


class LastRunSensor(RainBirdSchedulerEntity, SensorEntity):
    _attr_translation_key = "last_run"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: SchedulerCoordinator) -> None:
        super().__init__(coordinator, "last_run")

    @property
    def native_value(self) -> datetime | None:
        runs = self.coordinator.history.history.runs
        if not runs:
            return None
        last = runs[-1]
        return (
            last.actual_end_utc
            or last.actual_start_utc
            or last.requested_start_utc
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        runs = self.coordinator.history.history.runs
        if not runs:
            return {}
        last = runs[-1]
        return {
            "program": last.program_name,
            "outcome": last.outcome.value,
            "reason": last.reason,
        }

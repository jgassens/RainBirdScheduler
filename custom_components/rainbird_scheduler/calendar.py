"""Irrigation plan calendar (plan §30).

Exposes compiled zone and cycle events — not one program-wide block — e.g.
"Morning Lawn — Back Lawn — Cycle 2/3".
"""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import SchedulerConfigEntry
from .coordinator import SchedulerCoordinator
from .entity import RainBirdSchedulerEntity
from .models import RunPlan, RunStep


def _step_summary(run: RunPlan, step: RunStep) -> str:
    summary = f"{run.program_name} — {step.zone_name}"
    if step.cycle_count > 1:
        summary += f" — Cycle {step.cycle_index}/{step.cycle_count}"
    return summary


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SchedulerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([IrrigationPlanCalendar(entry.runtime_data)])


class IrrigationPlanCalendar(RainBirdSchedulerEntity, CalendarEntity):
    _attr_translation_key = "irrigation_plan"

    def __init__(self, coordinator: SchedulerCoordinator) -> None:
        super().__init__(coordinator, "irrigation_plan")

    def _all_events(self) -> list[CalendarEvent]:
        events: list[CalendarEvent] = []
        journal = self.coordinator.executor.journal
        plans = list(self.coordinator.timeline.runs)
        if journal.run_plan is not None:
            plans.insert(0, journal.run_plan)
        seen: set[str] = set()
        for run in plans:
            if run.occurrence_id in seen:
                continue
            seen.add(run.occurrence_id)
            if (
                run.occurrence_id in journal.completed_occurrences
                and run.occurrence_id != journal.active_occurrence_id
            ):
                continue
            for step in run.steps:
                events.append(
                    CalendarEvent(
                        start=step.planned_start_utc,
                        end=step.planned_end_utc,
                        summary=_step_summary(run, step),
                        description=(
                            f"Requested start "
                            f"{step.requested_start_utc.isoformat()}; "
                            f"exact runtime {step.exact_minutes} min, "
                            f"commanded {step.duration_minutes} min."
                        ),
                        uid=f"{run.run_id}-{step.index}",
                    )
                )
        events.sort(key=lambda event: event.start)
        return events

    @property
    def event(self) -> CalendarEvent | None:
        now = dt_util.utcnow()
        for entry in self._all_events():
            if entry.end > now:
                return entry
        return None

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        return [
            event
            for event in self._all_events()
            if event.end > start_date and event.start < end_date
        ]

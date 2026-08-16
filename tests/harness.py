"""Deterministic executor harness: fake clock, timers, driver, and stores."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from custom_components.rainbird_scheduler import serde
from custom_components.rainbird_scheduler.driver.base import (
    CommandUncertainError,
    DriverError,
    ZoneValidationError,
)
from custom_components.rainbird_scheduler.executor import RunExecutor
from custom_components.rainbird_scheduler.models import (
    CommandResult,
    ControllerConfig,
    ControllerObservation,
    ExecutionJournal,
    ObservationFreshness,
    Program,
    RunOutcome,
    RunPlan,
    ZoneProfile,
    ZoneReference,
)
from custom_components.rainbird_scheduler.planner import compile_timeline

from .helpers import make_controller, make_input, make_occurrence

START = datetime(2026, 6, 3, 14, 0, tzinfo=UTC)  # 9:00 AM America/Chicago


class TimeMachine:
    """Fake clock plus one-shot timer scheduler."""

    def __init__(self, start: datetime = START) -> None:
        self.now = start
        self._timers: list[list[Any]] = []  # [when, callback, active]

    def now_fn(self) -> datetime:
        return self.now

    def call_at(self, when, callback):
        entry = [when, callback, True]
        self._timers.append(entry)

        def cancel() -> None:
            entry[2] = False

        return cancel

    def pending(self) -> list[datetime]:
        return sorted(entry[0] for entry in self._timers if entry[2])

    async def advance(self, seconds: float) -> None:
        """Advance time, firing due timers in order at their exact instants."""
        target = self.now + timedelta(seconds=seconds)
        while True:
            due = [
                entry
                for entry in self._timers
                if entry[2] and entry[0] <= target
            ]
            if not due:
                break
            entry = min(due, key=lambda item: item[0])
            entry[2] = False
            self.now = max(self.now, entry[0])
            await entry[1]()
        self.now = target


@dataclass
class FakeDriver:
    """Scriptable irrigation driver."""

    tm: TimeMachine
    observation: ControllerObservation | None = None
    start_calls: list[tuple[ZoneReference, int, str]] = field(default_factory=list)
    stop_calls: int = 0
    refresh_calls: int = 0
    start_errors: list[Exception | None] = field(default_factory=list)
    stop_error: Exception | None = None

    def __post_init__(self) -> None:
        if self.observation is None:
            self.observation = self.make_observation()

    @property
    def capabilities(self):
        from custom_components.rainbird_scheduler.models import (
            DriverCapabilities,
        )

        return DriverCapabilities()

    def make_observation(
        self,
        active: set[str] | None = None,
        *,
        rain_sensor: bool | None = False,
        rain_delay: int | None = 0,
        available: bool = True,
        observed_at: datetime | None = None,
    ) -> ControllerObservation:
        return ControllerObservation(
            observed_at_utc=observed_at or self.tm.now,
            active_zone_ids=frozenset(active or set()),
            rain_sensor_active=rain_sensor,
            rain_delay_days=rain_delay,
            source_available=available,
            freshness=ObservationFreshness.FRESH,
        )

    def set_observation(self, active: set[str] | None = None, **kwargs) -> None:
        self.observation = self.make_observation(active, **kwargs)

    async def async_start_zone(
        self, zone: ZoneReference, duration_minutes: int, command_id: str
    ) -> CommandResult:
        self.start_calls.append((zone, duration_minutes, command_id))
        if self.start_errors:
            error = self.start_errors.pop(0)
            if error is not None:
                raise error
        return CommandResult(ok=True)

    async def async_stop_controller(self) -> CommandResult:
        self.stop_calls += 1
        if self.stop_error is not None:
            raise self.stop_error
        return CommandResult(ok=True)

    async def async_observe(self) -> ControllerObservation:
        assert self.observation is not None
        return self.observation

    async def async_request_observation(self) -> None:
        self.refresh_calls += 1


class FakeJournalStore:
    """Journal store that validates JSON-serializability on every write."""

    def __init__(self) -> None:
        self.writes: list[dict] = []

    async def async_write_journal_now(self, journal: ExecutionJournal) -> None:
        snapshot = serde.dump(journal)
        json.dumps(snapshot)  # must be plain JSON
        self.writes.append(snapshot)


class FakeHistory:
    def __init__(self) -> None:
        self.finished: list[tuple[str, RunOutcome, str | None]] = []
        self.interventions: list[tuple[str, str]] = []

    def record_run_finished(self, journal, outcome, reason) -> None:
        run_id = journal.run_plan.run_id if journal.run_plan else "?"
        self.finished.append((run_id, outcome, reason))

    def record_intervention(self, kind, message, run_id=None) -> None:
        self.interventions.append((kind, message))


@dataclass
class Rig:
    """One fully wired executor over fakes."""

    tm: TimeMachine
    driver: FakeDriver
    store: FakeJournalStore
    history: FakeHistory
    events: list[tuple[str, dict]]
    controller: ControllerConfig
    programs: dict[str, Program]
    zones: dict[str, ZoneProfile]
    plan: RunPlan
    executor: RunExecutor

    async def zone_event(
        self,
        zone_id: str,
        is_on: bool,
        *,
        active: set[str] | None = None,
        rain_sensor: bool | None = False,
        rain_delay: int | None = 0,
        available: bool = True,
    ) -> ControllerObservation:
        """Feed a zone state change with a matching observation."""
        if active is None:
            active = {zone_id} if is_on else set()
        observation = self.driver.make_observation(
            active,
            rain_sensor=rain_sensor,
            rain_delay=rain_delay,
            available=available,
        )
        self.driver.observation = observation
        await self.executor.async_handle_zone_state(zone_id, is_on, observation)
        return observation

    def event_types(self) -> list[str]:
        return [name for name, _ in self.events]

    def journal(self) -> ExecutionJournal:
        return self.executor.journal

    def final_step_results(self) -> dict[str, dict]:
        """Return the last persisted non-empty step-result map."""
        for write in reversed(self.store.writes):
            if write["step_results"]:
                return write["step_results"]
        raise AssertionError("no step results were ever persisted")


def build_rig(
    zones: list[ZoneProfile],
    program: Program,
    *,
    controller: ControllerConfig | None = None,
    start_utc: datetime = START,
    journal: ExecutionJournal | None = None,
) -> Rig:
    controller = controller or make_controller()
    occurrence = make_occurrence(program, start_utc)
    timeline = compile_timeline(
        make_input(controller, [program], zones, [occurrence])
    )
    assert timeline.runs, f"no runs compiled: {timeline.conflicts}"
    plan = timeline.runs[0]

    tm = TimeMachine(start_utc)
    driver = FakeDriver(tm)
    store = FakeJournalStore()
    history = FakeHistory()
    events: list[tuple[str, dict]] = []
    programs = {program.id: program}
    zone_map = {zone.id: zone for zone in zones}

    executor = RunExecutor(
        driver=driver,
        journal=journal or ExecutionJournal(),
        journal_store=store,
        timers=tm,
        now_fn=tm.now_fn,
        get_controller_config=lambda: controller,
        get_program=programs.get,
        get_zone_reference=lambda zone_id: (
            zone_map[zone_id].reference if zone_id in zone_map else None
        ),
        emit_event=lambda name, data: events.append((name, data)),
        history=history,
    )
    return Rig(
        tm=tm,
        driver=driver,
        store=store,
        history=history,
        events=events,
        controller=controller,
        programs=programs,
        zones=zone_map,
        plan=plan,
        executor=executor,
    )


def three_zone_rig(**controller_overrides) -> Rig:
    """The plan §2 flagship setup: 12/8/15 minutes at a shared 9:00 start."""
    from .helpers import make_program, make_zone

    zones = [
        make_zone("front-lawn", 1, base_runtime_minutes=Decimal(12)),
        make_zone("side-lawn", 2, base_runtime_minutes=Decimal(8)),
        make_zone("back-lawn", 3, base_runtime_minutes=Decimal(15)),
    ]
    program = make_program(
        "morning-lawn", ["front-lawn", "side-lawn", "back-lawn"]
    )
    controller = make_controller(**controller_overrides)
    return build_rig(zones, program, controller=controller)


__all__ = [
    "START",
    "CommandUncertainError",
    "DriverError",
    "FakeDriver",
    "FakeHistory",
    "FakeJournalStore",
    "Rig",
    "TimeMachine",
    "ZoneValidationError",
    "build_rig",
    "three_zone_rig",
]

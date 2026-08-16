"""Factories shared across the test suite (pure-model helpers)."""

from __future__ import annotations

from datetime import UTC, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from custom_components.rainbird_scheduler.models import (
    ControllerConfig,
    Program,
    ProgramOccurrence,
    ProgramZoneStep,
    RecurrenceKind,
    RecurrenceRule,
    ZoneProfile,
    ZoneReference,
    make_occurrence_id,
)
from custom_components.rainbird_scheduler.planner import PlannerInput

TZ = ZoneInfo("America/Chicago")


def make_controller(**overrides) -> ControllerConfig:
    defaults = dict(
        id="controller-1",
        revision=1,
        source_config_entry_id="source-entry",
        source_unique_id="aa:bb:cc:dd:ee:ff",
    )
    defaults.update(overrides)
    return ControllerConfig(**defaults)


def make_zone(zone_id: str, station: int, **overrides) -> ZoneProfile:
    defaults = dict(
        id=zone_id,
        revision=1,
        reference=ZoneReference(
            source_unique_id=f"aabbcc-{station}",
            source_config_entry_id="source-entry",
            entity_registry_id=f"reg-{zone_id}",
            station_number=station,
            last_known_entity_id=f"switch.zone_{station}",
        ),
        display_name=zone_id.replace("-", " ").title(),
        base_runtime_minutes=Decimal(10),
    )
    defaults.update(overrides)
    return ZoneProfile(**defaults)


def make_program(
    program_id: str,
    zone_ids: list[str],
    start: time = time(9, 0),
    **overrides,
) -> Program:
    defaults = dict(
        id=program_id,
        revision=1,
        name=program_id.replace("-", " ").title(),
        recurrence=RecurrenceRule(
            kind=RecurrenceKind.WEEKLY,
            weekdays=frozenset(range(7)),
        ),
        nominal_start_times=[start],
        zone_steps=[
            ProgramZoneStep(zone_id=zone_id, position=index)
            for index, zone_id in enumerate(zone_ids)
        ],
    )
    defaults.update(overrides)
    return Program(**defaults)


def make_occurrence(
    program: Program,
    start_utc: datetime,
    created_at: datetime | None = None,
    manual: bool = False,
) -> ProgramOccurrence:
    created = created_at or datetime(2026, 6, 1, tzinfo=UTC)
    return ProgramOccurrence(
        occurrence_id=make_occurrence_id(program.id, start_utc),
        program_id=program.id,
        scheduled_start_utc=start_utc,
        scheduled_start_local=start_utc.astimezone(TZ),
        created_at_utc=created,
        manual=manual,
    )


def make_input(
    controller: ControllerConfig,
    programs: list[Program],
    zones: list[ZoneProfile],
    occurrences: list[ProgramOccurrence],
    adjustments: dict | None = None,
    compiled_at: datetime | None = None,
) -> PlannerInput:
    return PlannerInput(
        controller_config=controller,
        programs={program.id: program for program in programs},
        zone_profiles={zone.id: zone for zone in zones},
        candidate_occurrences=occurrences,
        adjustment_snapshots=adjustments or {},
        tz=TZ,
        compiled_at_utc=compiled_at or datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
    )

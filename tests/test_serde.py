"""Round-trip serialization for stored models."""

from __future__ import annotations

import json
from datetime import UTC, datetime, time
from decimal import Decimal

import pytest

from custom_components.rainbird_scheduler import serde
from custom_components.rainbird_scheduler.models import (
    AdjustmentResult,
    AdjustmentSnapshot,
    CommandDisposition,
    CommandType,
    ConfigData,
    ControllerObservation,
    ExecutionJournal,
    ExecutorState,
    HistoryData,
    ObservationFreshness,
    PendingCommand,
    ProgramZoneStep,
    ProviderKind,
    RunOutcome,
    RunPlan,
    RunRecord,
    RunStep,
    StepResult,
    StepStatus,
    WateringWindow,
    ZoneProfile,
)

from .helpers import make_controller, make_program, make_zone

NOW = datetime(2026, 6, 3, 14, 0, tzinfo=UTC)


def roundtrip(value, target_type):
    dumped = serde.dump(value)
    # Must be plain JSON.
    encoded = json.dumps(dumped)
    return serde.load(target_type, json.loads(encoded))


def test_config_data_roundtrip() -> None:
    program = make_program("p", ["zone-a"])
    program.watering_window = WateringWindow(
        start_local=time(6, 0), end_local=time(9, 30)
    )
    config = ConfigData(
        controller=make_controller(),
        zones={"zone-a": make_zone("zone-a", 1)},
        programs={"p": program},
    )
    assert roundtrip(config, ConfigData) == config


def test_journal_roundtrip_with_active_run() -> None:
    step = RunStep(
        index=0,
        zone_id="zone-a",
        zone_name="Zone A",
        station_number=1,
        cycle_index=1,
        cycle_count=2,
        requested_start_utc=NOW,
        planned_start_utc=NOW,
        planned_end_utc=NOW,
        duration_minutes=5,
        exact_minutes=Decimal("5.25"),
    )
    plan = RunPlan(
        run_id="run",
        occurrence_id="p:2026",
        program_id="p",
        program_name="P",
        requested_start_utc=NOW,
        compiled_at_utc=NOW,
        controller_config_revision=3,
        program_revision=8,
        adjustment_snapshot=AdjustmentSnapshot(
            provider_kind=ProviderKind.MANUAL_PERCENT,
            computed_at_utc=NOW,
            per_zone={
                "zone-a": AdjustmentResult(
                    base_runtime_minutes=Decimal(10),
                    exact_adjusted_minutes=Decimal("5.25"),
                    quantized_minutes=5,
                    seasonal_factor=Decimal("52.5"),
                    weather_factor=None,
                    rain_credit_minutes=Decimal(0),
                    carried_deficit_minutes=Decimal(0),
                    input_timestamps={"manual": NOW},
                    stale_inputs=(),
                    explanation=("Manual 52.5%",),
                )
            },
        ),
        steps=(step,),
    )
    journal = ExecutionJournal(
        state=ExecutorState.WATERING,
        active_occurrence_id="p:2026",
        run_plan=plan,
        current_step_index=0,
        step_results={0: StepResult(status=StepStatus.RUNNING, attempts=1)},
        pending_command=PendingCommand(
            command_id="cmd",
            command_type=CommandType.START_ZONE,
            zone_id="zone-a",
            duration_minutes=5,
            intended_at_utc=NOW,
            attempt_number=1,
            disposition=CommandDisposition.SENT,
        ),
        current_step_actual_start=NOW,
        current_step_expected_end=NOW,
        last_observation=ControllerObservation(
            observed_at_utc=NOW,
            active_zone_ids=frozenset({"zone-a"}),
            rain_sensor_active=False,
            rain_delay_days=0,
            source_available=True,
            freshness=ObservationFreshness.FRESH,
        ),
        completed_occurrences={"p:2025": NOW},
        updated_at_utc=NOW,
    )
    assert roundtrip(journal, ExecutionJournal) == journal


def test_history_roundtrip() -> None:
    history = HistoryData(
        runs=[
            RunRecord(
                run_id="run",
                occurrence_id="p:2026",
                program_id="p",
                program_name="P",
                manual=False,
                requested_start_utc=NOW,
                planned_start_utc=NOW,
                planned_end_utc=NOW,
                actual_start_utc=NOW,
                actual_end_utc=None,
                outcome=RunOutcome.ABORTED_SENSOR,
                reason="sensor_cut",
                provider_kind=ProviderKind.FIXED,
                retries=1,
                uncertain_commands=0,
            )
        ]
    )
    assert roundtrip(history, HistoryData) == history


def test_null_rejected_for_non_optional_decimal_field() -> None:
    data = serde.dump(make_zone("zone-a", 1))
    data["base_runtime_minutes"] = None
    with pytest.raises(ValueError, match="base_runtime_minutes"):
        serde.load(ZoneProfile, data)


def test_null_rejected_for_non_optional_int_field() -> None:
    data = serde.dump(ProgramZoneStep(zone_id="zone-a", position=0))
    data["requested_offset_seconds"] = None
    with pytest.raises(ValueError, match="requested_offset_seconds"):
        serde.load(ProgramZoneStep, data)


def test_null_rejected_for_bare_target() -> None:
    with pytest.raises(ValueError):
        serde.load(Decimal, None)
    with pytest.raises(ValueError):
        serde.load(int, None)


def test_null_still_valid_for_optional_fields() -> None:
    data = serde.dump(make_zone("zone-a", 1))
    data["max_cycle_minutes"] = None
    data["precipitation_rate_mm_per_hour"] = None
    loaded = serde.load(ZoneProfile, data)
    assert loaded.max_cycle_minutes is None
    assert loaded.precipitation_rate_mm_per_hour is None


def test_dedup_window_is_bounded() -> None:
    journal = ExecutionJournal()
    for index in range(600):
        journal.record_occurrence_finished(
            f"occ-{index}", NOW.replace(microsecond=index % 1000)
        )
    from custom_components.rainbird_scheduler.const import DEDUP_MAX_OCCURRENCES

    assert len(journal.completed_occurrences) <= DEDUP_MAX_OCCURRENCES


def test_controller_roundtrips_freeze_config() -> None:
    from custom_components.rainbird_scheduler.models import (
        ControllerConfig,
        FreezeGuardConfig,
        FreezeUnavailablePolicy,
        TemperatureUnit,
    )

    controller = make_controller(
        freeze_guard=FreezeGuardConfig(
            enabled=True,
            temperature_entity_id="weather.home",
            threshold=Decimal(36),
            unit=TemperatureUnit.FAHRENHEIT,
            when_unavailable=FreezeUnavailablePolicy.BLOCK_WATERING,
        ),
        rain_sensor_override_entity_id="binary_sensor.custom_rain",
    )
    loaded = serde.load(ControllerConfig, serde.dump(controller))
    assert loaded.freeze_guard.enabled is True
    assert loaded.freeze_guard.temperature_entity_id == "weather.home"
    assert loaded.freeze_guard.unit is TemperatureUnit.FAHRENHEIT
    assert (
        loaded.freeze_guard.when_unavailable
        is FreezeUnavailablePolicy.BLOCK_WATERING
    )
    assert loaded.rain_sensor_override_entity_id == "binary_sensor.custom_rain"


def test_legacy_config_loads_freeze_defaults() -> None:
    from custom_components.rainbird_scheduler.models import ControllerConfig

    legacy = {
        "id": "c",
        "revision": 1,
        "source_config_entry_id": "e",
        "source_unique_id": "u",
    }
    loaded = serde.load(ControllerConfig, legacy)
    assert loaded.freeze_guard.enabled is False
    assert loaded.default_freeze_policy.skip_when_freezing is True
    assert loaded.rain_sensor_override_entity_id is None


def test_observation_roundtrips_temperature() -> None:
    observation = ControllerObservation(
        observed_at_utc=NOW,
        active_zone_ids=frozenset(),
        rain_sensor_active=False,
        rain_delay_days=0,
        source_available=True,
        freshness=ObservationFreshness.FRESH,
        current_temperature_c=Decimal("-3.5"),
        temperature_stale=True,
    )
    loaded = serde.load(ControllerObservation, serde.dump(observation))
    assert loaded.current_temperature_c == Decimal("-3.5")
    assert loaded.temperature_stale is True

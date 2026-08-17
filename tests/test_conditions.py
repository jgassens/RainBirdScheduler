"""Pre-start precondition evaluation, including the freeze guard."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from custom_components.rainbird_scheduler.conditions import (
    evaluate_preconditions,
)
from custom_components.rainbird_scheduler.models import (
    AuthorityMode,
    ControllerObservation,
    FreezeGuardConfig,
    FreezeUnavailablePolicy,
    ObservationFreshness,
    SkipReason,
    TemperatureUnit,
    attribute_sensor_trip,
)

from .helpers import make_controller, make_program

_NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


def obs(**kwargs) -> ControllerObservation:
    base = {
        "observed_at_utc": _NOW,
        "active_zone_ids": frozenset(),
        "rain_sensor_active": None,
        "rain_delay_days": 0,
        "source_available": True,
        "freshness": ObservationFreshness.FRESH,
    }
    base.update(kwargs)
    return ControllerObservation(**base)


def _guarded_controller(**guard_kwargs):
    guard = FreezeGuardConfig(
        enabled=True,
        temperature_entity_id="sensor.t",
        threshold=Decimal(2),
        **guard_kwargs,
    )
    return make_controller(freeze_guard=guard)


# --- existing (non-freeze) precedence, to lock behavior ---------------------


def test_scheduler_disabled_blocks_first() -> None:
    controller = make_controller(enabled=False)
    result = evaluate_preconditions(
        controller, make_program("p", []), obs(), manual=False
    )
    assert result is not None
    assert result.reason is SkipReason.SCHEDULER_DISABLED


def test_native_authoritative_blocks_automatic_only() -> None:
    controller = make_controller(
        authority_mode=AuthorityMode.NATIVE_AUTHORITATIVE
    )
    program = make_program("p", [])
    assert (
        evaluate_preconditions(controller, program, obs(), manual=False).reason
        is SkipReason.AUTHORITY_MODE
    )
    assert (
        evaluate_preconditions(controller, program, obs(), manual=True) is None
    )


def test_rain_delay_precedes_wet_sensor() -> None:
    result = evaluate_preconditions(
        make_controller(),
        make_program("p", []),
        obs(rain_delay_days=2, rain_sensor_active=True),
        manual=False,
    )
    assert result.reason is SkipReason.RAIN_DELAY


# --- freeze guard -----------------------------------------------------------


def test_freeze_blocks_when_cold() -> None:
    result = evaluate_preconditions(
        _guarded_controller(),
        make_program("p", []),
        obs(current_temperature_c=Decimal(0)),
        manual=False,
    )
    assert result.reason is SkipReason.LOW_TEMPERATURE
    assert result.transient is False


def test_freeze_allows_when_warm() -> None:
    assert (
        evaluate_preconditions(
            _guarded_controller(),
            make_program("p", []),
            obs(current_temperature_c=Decimal(10)),
            manual=False,
        )
        is None
    )


def test_freeze_disabled_by_program_policy() -> None:
    program = make_program("p", [])
    program.freeze_policy.skip_when_freezing = False
    assert (
        evaluate_preconditions(
            _guarded_controller(),
            program,
            obs(current_temperature_c=Decimal(0)),
            manual=False,
        )
        is None
    )


def test_unknown_temperature_allows_by_default() -> None:
    assert (
        evaluate_preconditions(
            _guarded_controller(),
            make_program("p", []),
            obs(current_temperature_c=None),
            manual=False,
        )
        is None
    )


def test_unknown_temperature_blocks_transiently_when_configured() -> None:
    result = evaluate_preconditions(
        _guarded_controller(
            when_unavailable=FreezeUnavailablePolicy.BLOCK_WATERING
        ),
        make_program("p", []),
        obs(current_temperature_c=None),
        manual=False,
    )
    assert result.reason is SkipReason.LOW_TEMPERATURE
    assert result.transient is True


def test_fahrenheit_threshold_converts() -> None:
    guard = FreezeGuardConfig(
        enabled=True,
        temperature_entity_id="sensor.t",
        threshold=Decimal(36),  # 36 °F ≈ 2.2 °C
        unit=TemperatureUnit.FAHRENHEIT,
    )
    controller = make_controller(freeze_guard=guard)
    # 2 °C is below 36 °F: blocked.
    assert (
        evaluate_preconditions(
            controller,
            make_program("p", []),
            obs(current_temperature_c=Decimal(2)),
            manual=False,
        ).reason
        is SkipReason.LOW_TEMPERATURE
    )
    # 3 °C is above 36 °F: allowed.
    assert (
        evaluate_preconditions(
            controller,
            make_program("p", []),
            obs(current_temperature_c=Decimal(3)),
            manual=False,
        )
        is None
    )


def test_disambiguation_relabels_cold_boolean_as_freeze() -> None:
    result = evaluate_preconditions(
        _guarded_controller(),
        make_program("p", []),
        obs(rain_sensor_active=True, current_temperature_c=Decimal(0)),
        manual=False,
    )
    assert result.reason is SkipReason.LOW_TEMPERATURE


def test_disambiguation_keeps_rain_when_mild_or_unknown() -> None:
    controller = _guarded_controller()
    program = make_program("p", [])
    assert (
        evaluate_preconditions(
            controller,
            program,
            obs(rain_sensor_active=True, current_temperature_c=Decimal(15)),
            manual=False,
        ).reason
        is SkipReason.RAIN_SENSOR_WET
    )
    assert (
        evaluate_preconditions(
            controller,
            program,
            obs(rain_sensor_active=True, current_temperature_c=None),
            manual=False,
        ).reason
        is SkipReason.RAIN_SENSOR_WET
    )


def test_attribute_sensor_trip_helper() -> None:
    guard = FreezeGuardConfig(
        enabled=True, temperature_entity_id="sensor.t", threshold=Decimal(2)
    )
    assert (
        attribute_sensor_trip(
            guard, obs(rain_sensor_active=True, current_temperature_c=Decimal(0))
        )
        == "freeze"
    )
    assert (
        attribute_sensor_trip(
            guard,
            obs(rain_sensor_active=True, current_temperature_c=Decimal(15)),
        )
        == "rain"
    )
    assert (
        attribute_sensor_trip(guard, obs(rain_sensor_active=False)) == "unknown"
    )

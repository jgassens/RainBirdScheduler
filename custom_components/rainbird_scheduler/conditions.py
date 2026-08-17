"""Pre-start condition evaluation (plan §23, §27). Pure module."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .models import (
    AuthorityMode,
    ControllerConfig,
    ControllerObservation,
    FreezeGuardConfig,
    FreezeUnavailablePolicy,
    Program,
    SkipReason,
    TemperatureUnit,
    attribute_sensor_trip,
    freeze_active,
)


@dataclass(frozen=True)
class PreconditionResult:
    """Why a start is blocked, and whether waiting could clear it."""

    reason: SkipReason
    detail: str
    transient: bool  # True: waiting may clear it (delay per missed-run policy)


def format_temperature(guard: FreezeGuardConfig, celsius: Decimal) -> str:
    """Render a Celsius value in the guard's configured unit for messages."""
    if guard.unit is TemperatureUnit.FAHRENHEIT:
        value = celsius * Decimal(9) / Decimal(5) + Decimal(32)
        unit = "°F"
    else:
        value = celsius
        unit = "°C"
    return f"{value.quantize(Decimal('0.1'))} {unit}"


def _threshold_label(guard: FreezeGuardConfig) -> str:
    return f"{guard.threshold.quantize(Decimal('0.1'))} {guard.unit.value}"


def evaluate_preconditions(
    controller: ControllerConfig,
    program: Program,
    observation: ControllerObservation | None,
    *,
    manual: bool,
) -> PreconditionResult | None:
    """Return the blocking condition for starting a run, or None."""
    if not controller.enabled:
        return PreconditionResult(
            reason=SkipReason.SCHEDULER_DISABLED,
            detail="Scheduler is disabled for this controller.",
            transient=False,
        )
    if (
        controller.authority_mode is AuthorityMode.NATIVE_AUTHORITATIVE
        and not manual
    ):
        return PreconditionResult(
            reason=SkipReason.AUTHORITY_MODE,
            detail=(
                "Native Rain Bird programs own automatic watering; the "
                "scheduler will not launch automatic runs."
            ),
            transient=False,
        )
    if observation is None:
        return None

    if not observation.source_available:
        return PreconditionResult(
            reason=SkipReason.SOURCE_UNAVAILABLE,
            detail="Rain Bird source entities are unavailable.",
            transient=True,
        )
    if (
        program.rain_policy.honor_native_delay
        and observation.rain_delay_days is not None
        and observation.rain_delay_days > 0
    ):
        return PreconditionResult(
            reason=SkipReason.RAIN_DELAY,
            detail=(
                f"Native Rain Bird rain delay is active "
                f"({observation.rain_delay_days} day(s)). Rain Bird does not "
                "apply it to manual zone commands, so the scheduler enforces "
                "it itself."
            ),
            transient=False,
        )
    guard = controller.freeze_guard
    freeze_pol = program.freeze_policy
    if (
        program.rain_policy.skip_when_sensor_wet
        and observation.rain_sensor_active
    ):
        # Disambiguate a WR2 combo trip: relabel as freeze only when the guard
        # is genuinely enforcing it, so turning freeze-skip off keeps the honest
        # "rain sensor wet" reason.
        if (
            guard.enabled
            and freeze_pol.skip_when_freezing
            and attribute_sensor_trip(guard, observation) == "freeze"
        ):
            assert observation.current_temperature_c is not None
            return PreconditionResult(
                reason=SkipReason.LOW_TEMPERATURE,
                detail=(
                    "The sensor is active and the temperature "
                    f"({format_temperature(guard, observation.current_temperature_c)}) "
                    f"is at or below the freeze threshold "
                    f"({_threshold_label(guard)})."
                ),
                transient=False,
            )
        return PreconditionResult(
            reason=SkipReason.RAIN_SENSOR_WET,
            detail="The rain sensor is wet.",
            transient=False,
        )
    if guard.enabled and freeze_pol.skip_when_freezing:
        active = freeze_active(guard, observation)
        if active is True:
            assert observation.current_temperature_c is not None
            return PreconditionResult(
                reason=SkipReason.LOW_TEMPERATURE,
                detail=(
                    f"Temperature {format_temperature(guard, observation.current_temperature_c)} "
                    f"is at or below the freeze threshold "
                    f"({_threshold_label(guard)})."
                ),
                transient=False,
            )
        if (
            active is None
            and guard.when_unavailable
            is FreezeUnavailablePolicy.BLOCK_WATERING
        ):
            return PreconditionResult(
                reason=SkipReason.LOW_TEMPERATURE,
                detail=(
                    "The temperature source is unavailable or stale and the "
                    "freeze guard is set to block watering until it recovers."
                ),
                transient=True,
            )
    if observation.active_zone_ids:
        return PreconditionResult(
            reason=SkipReason.EXTERNAL_ACTIVITY,
            detail=(
                "Another watering session is active on the controller "
                "(app, native program, or manual)."
            ),
            transient=True,
        )
    return None

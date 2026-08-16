"""Pre-start condition evaluation (plan §23, §27). Pure module."""

from __future__ import annotations

from dataclasses import dataclass

from .models import (
    AuthorityMode,
    ControllerConfig,
    ControllerObservation,
    Program,
    SkipReason,
)


@dataclass(frozen=True)
class PreconditionResult:
    """Why a start is blocked, and whether waiting could clear it."""

    reason: SkipReason
    detail: str
    transient: bool  # True: waiting may clear it (delay per missed-run policy)


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
    if (
        program.rain_policy.skip_when_sensor_wet
        and observation.rain_sensor_active
    ):
        return PreconditionResult(
            reason=SkipReason.RAIN_SENSOR_WET,
            detail="The rain sensor is wet.",
            transient=False,
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

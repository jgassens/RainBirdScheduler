"""Provider protocol and shared calculation helpers."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from ..models import (
    AdjustmentResult,
    Program,
    ProgramOccurrence,
    ProgramZoneStep,
    ZoneProfile,
)
from ..planner import quantize_zone_minutes


class StateReader(Protocol):
    """Minimal view of ``hass.states.get`` (keeps providers testable)."""

    def __call__(self, entity_id: str) -> Any | None: ...


class DurationProvider(Protocol):
    """Calculates one zone's adjusted runtime with full provenance."""

    async def async_calculate(
        self,
        zone: ZoneProfile,
        program: Program,
        occurrence: ProgramOccurrence,
    ) -> AdjustmentResult:
        """Return the adjusted runtime for ``zone`` in ``occurrence``."""
        ...


def effective_base_minutes(zone: ZoneProfile, program: Program) -> Decimal:
    """The program-step override wins over the zone's base runtime."""
    for step in program.zone_steps:
        if (
            step.zone_id == zone.id
            and step.base_runtime_override_minutes is not None
        ):
            return step.base_runtime_override_minutes
    return zone.base_runtime_minutes


def zone_step(program: Program, zone_id: str) -> ProgramZoneStep | None:
    for step in program.zone_steps:
        if step.zone_id == zone_id:
            return step
    return None


def percent_result(
    *,
    base: Decimal,
    percent: Decimal,
    explanation: list[str],
    weather_factor: Decimal | None = None,
    input_timestamps: dict[str, datetime] | None = None,
    stale_inputs: tuple[str, ...] = (),
) -> AdjustmentResult:
    """Build the common percent-scaled result."""
    if percent < 0:
        percent = Decimal(0)
        explanation.append("Negative percentage clamped to 0%.")
    exact = base * percent / Decimal(100)
    if exact < 0:
        exact = Decimal(0)
    return AdjustmentResult(
        base_runtime_minutes=base,
        exact_adjusted_minutes=exact,
        quantized_minutes=quantize_zone_minutes(exact),
        seasonal_factor=percent,
        weather_factor=weather_factor,
        rain_credit_minutes=Decimal(0),
        carried_deficit_minutes=Decimal(0),
        input_timestamps=input_timestamps or {},
        stale_inputs=stale_inputs,
        explanation=tuple(explanation),
    )

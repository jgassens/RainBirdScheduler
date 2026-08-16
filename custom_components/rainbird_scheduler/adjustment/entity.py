"""External entity providers: percentage and calculated runtime."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from ..models import AdjustmentResult, Program, ProgramOccurrence, ZoneProfile
from ..planner import quantize_zone_minutes
from .base import StateReader, effective_base_minutes, percent_result

STALE_AFTER = timedelta(hours=6)


def _read_decimal(
    get_state: StateReader, entity_id: str | None
) -> tuple[Decimal | None, datetime | None, str]:
    """Read a numeric state; returns (value, last_updated, note)."""
    if not entity_id:
        return None, None, "No source entity configured."
    state = get_state(entity_id)
    if state is None or state.state in ("unknown", "unavailable"):
        return None, None, f"Entity {entity_id} is unavailable."
    try:
        value = Decimal(str(state.state))
    except (InvalidOperation, ValueError):
        return None, None, (
            f"Entity {entity_id} state {state.state!r} is not numeric."
        )
    reported = getattr(state, "last_reported", None) or state.last_updated
    return value, reported, ""


class EntityPercentProvider:
    """A number/sensor entity supplies the percentage."""

    def __init__(
        self,
        entity_id: str | None,
        *,
        get_state: StateReader,
        now_fn: Callable[[], datetime],
    ) -> None:
        self._entity_id = entity_id
        self._get_state = get_state
        self._now = now_fn

    async def async_calculate(
        self,
        zone: ZoneProfile,
        program: Program,
        occurrence: ProgramOccurrence,
    ) -> AdjustmentResult:
        base = effective_base_minutes(zone, program)
        value, updated, note = _read_decimal(self._get_state, self._entity_id)
        if value is None:
            return percent_result(
                base=base,
                percent=Decimal(100),
                explanation=[
                    note,
                    "Fell back to 100% (base runtime unchanged).",
                ],
                stale_inputs=(self._entity_id or "percent_entity",),
            )
        stale = (
            updated is not None and self._now() - updated > STALE_AFTER
        )
        return percent_result(
            base=base,
            percent=value,
            explanation=[
                f"Base runtime {base} min × {value}% from "
                f"{self._entity_id}."
            ],
            input_timestamps=(
                {self._entity_id: updated}
                if self._entity_id and updated
                else {}
            ),
            stale_inputs=(self._entity_id,) if stale and self._entity_id else (),
        )


class EntityRuntimeProvider:
    """An entity supplies the total calculated runtime in minutes.

    The external total is distributed across the program's zones in
    proportion to their base runtimes, so relative zone weighting is
    preserved. The proportionality is stated in the explanation.
    """

    def __init__(
        self,
        entity_id: str | None,
        *,
        get_state: StateReader,
        now_fn: Callable[[], datetime],
        get_zone: Callable[[str], ZoneProfile | None] | None = None,
    ) -> None:
        self._entity_id = entity_id
        self._get_state = get_state
        self._now = now_fn
        self._get_zone = get_zone

    async def async_calculate(
        self,
        zone: ZoneProfile,
        program: Program,
        occurrence: ProgramOccurrence,
    ) -> AdjustmentResult:
        base = effective_base_minutes(zone, program)
        total, updated, note = _read_decimal(self._get_state, self._entity_id)
        if total is None:
            return percent_result(
                base=base,
                percent=Decimal(100),
                explanation=[
                    note,
                    "Fell back to 100% (base runtime unchanged).",
                ],
                stale_inputs=(self._entity_id or "runtime_entity",),
            )
        if total < 0:
            total = Decimal(0)

        base_total = Decimal(0)
        for step in program.zone_steps:
            if not step.enabled:
                continue
            profile = (
                zone
                if step.zone_id == zone.id
                else (self._get_zone(step.zone_id) if self._get_zone else None)
            )
            if profile is not None:
                base_total += effective_base_minutes(profile, program)
        if base_total <= 0:
            exact = Decimal(0)
            share_note = "Program has no base runtime; zone gets 0 minutes."
        else:
            exact = total * base / base_total
            share_note = (
                f"External total {total} min × ({base}/{base_total} "
                "base-runtime share)."
            )
        stale = updated is not None and self._now() - updated > STALE_AFTER
        seasonal = (
            (exact / base * Decimal(100)).quantize(Decimal("0.1"))
            if base > 0
            else Decimal(0)
        )
        return AdjustmentResult(
            base_runtime_minutes=base,
            exact_adjusted_minutes=exact,
            quantized_minutes=quantize_zone_minutes(exact),
            seasonal_factor=seasonal,
            weather_factor=None,
            rain_credit_minutes=Decimal(0),
            carried_deficit_minutes=Decimal(0),
            input_timestamps=(
                {self._entity_id: updated}
                if self._entity_id and updated
                else {}
            ),
            stale_inputs=(
                (self._entity_id,) if stale and self._entity_id else ()
            ),
            explanation=(
                f"Runtime from {self._entity_id}.",
                share_note,
            ),
        )

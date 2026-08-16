"""Manual percentage provider."""

from __future__ import annotations

from decimal import Decimal

from ..models import AdjustmentResult, Program, ProgramOccurrence, ZoneProfile
from .base import effective_base_minutes, percent_result


class ManualPercentProvider:
    """User-chosen flat percentage."""

    def __init__(self, percent: Decimal) -> None:
        self._percent = percent

    async def async_calculate(
        self,
        zone: ZoneProfile,
        program: Program,
        occurrence: ProgramOccurrence,
    ) -> AdjustmentResult:
        base = effective_base_minutes(zone, program)
        return percent_result(
            base=base,
            percent=self._percent,
            explanation=[
                f"Base runtime {base} min × manual {self._percent}%."
            ],
        )

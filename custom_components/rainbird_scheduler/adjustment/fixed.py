"""Fixed 100% provider: base runtimes pass through unchanged."""

from __future__ import annotations

from decimal import Decimal

from ..models import AdjustmentResult, Program, ProgramOccurrence, ZoneProfile
from .base import effective_base_minutes, percent_result


class FixedProvider:
    """No adjustment; the deterministic default."""

    async def async_calculate(
        self,
        zone: ZoneProfile,
        program: Program,
        occurrence: ProgramOccurrence,
    ) -> AdjustmentResult:
        base = effective_base_minutes(zone, program)
        return percent_result(
            base=base,
            percent=Decimal(100),
            explanation=[
                f"Base runtime {base} min used unchanged (fixed 100%)."
            ],
        )

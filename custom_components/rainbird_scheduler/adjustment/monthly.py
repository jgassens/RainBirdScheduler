"""Twelve-month seasonal curve provider."""

from __future__ import annotations

from decimal import Decimal

from ..models import AdjustmentResult, Program, ProgramOccurrence, ZoneProfile
from .base import effective_base_minutes, percent_result

_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


class MonthlyCurveProvider:
    """Percentage per calendar month of the occurrence's local date."""

    def __init__(self, monthly_percents: tuple[Decimal, ...] | None) -> None:
        if monthly_percents is None or len(monthly_percents) != 12:
            monthly_percents = tuple(Decimal(100) for _ in range(12))
        self._curve = monthly_percents

    async def async_calculate(
        self,
        zone: ZoneProfile,
        program: Program,
        occurrence: ProgramOccurrence,
    ) -> AdjustmentResult:
        base = effective_base_minutes(zone, program)
        month = occurrence.scheduled_start_local.month
        percent = self._curve[month - 1]
        return percent_result(
            base=base,
            percent=percent,
            explanation=[
                f"Base runtime {base} min × {percent}% "
                f"({_MONTHS[month - 1]} seasonal curve)."
            ],
        )

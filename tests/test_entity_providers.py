"""External entity providers: unusable states and distribution basis."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from custom_components.rainbird_scheduler.adjustment.entity import (
    EntityPercentProvider,
    EntityRuntimeProvider,
)

from .helpers import make_occurrence, make_program, make_zone

NOW = datetime(2026, 6, 3, 14, 0, tzinfo=UTC)


@dataclass
class _FakeState:
    state: str
    last_updated: datetime = NOW


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "not-a-number"])
async def test_non_finite_percent_state_falls_back_to_100(raw: str) -> None:
    """'nan'/'inf' states (esphome, templates) count as unavailable."""
    zone = make_zone("garden", 1, base_runtime_minutes=Decimal(20))
    program = make_program("p", ["garden"])
    occurrence = make_occurrence(program, NOW)
    provider = EntityPercentProvider(
        "sensor.percent",
        get_state=lambda _: _FakeState(raw),
        now_fn=lambda: NOW,
    )
    result = await provider.async_calculate(zone, program, occurrence)
    assert result.exact_adjusted_minutes == Decimal(20)
    assert result.quantized_minutes == 20
    assert result.stale_inputs == ("sensor.percent",)


async def test_non_finite_runtime_state_falls_back_to_100() -> None:
    zone = make_zone("garden", 1, base_runtime_minutes=Decimal(20))
    program = make_program("p", ["garden"])
    occurrence = make_occurrence(program, NOW)
    provider = EntityRuntimeProvider(
        "sensor.total",
        get_state=lambda _: _FakeState("nan"),
        now_fn=lambda: NOW,
    )
    result = await provider.async_calculate(zone, program, occurrence)
    assert result.exact_adjusted_minutes == Decimal(20)
    assert result.stale_inputs == ("sensor.total",)


async def test_runtime_distribution_ignores_profile_disabled_zones() -> None:
    """Zones that will not run get no share of the external total."""
    zone_a = make_zone("a", 1, base_runtime_minutes=Decimal(10))
    zone_b = make_zone("b", 2, base_runtime_minutes=Decimal(10), enabled=False)
    zones = {"a": zone_a, "b": zone_b}
    program = make_program("p", ["a", "b"])
    occurrence = make_occurrence(program, NOW)
    provider = EntityRuntimeProvider(
        "sensor.total",
        get_state=lambda _: _FakeState("60"),
        now_fn=lambda: NOW,
        get_zone=zones.get,
    )
    result = await provider.async_calculate(zone_a, program, occurrence)
    # Zone B's profile is disabled, so the whole 60-minute external total
    # lands on zone A (previously it only got its 30-minute half).
    assert result.exact_adjusted_minutes == Decimal(60)

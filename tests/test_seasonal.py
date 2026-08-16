"""Automatic seasonal provider: nearest-city lookup and curve math."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from custom_components.rainbird_scheduler.adjustment import create_provider
from custom_components.rainbird_scheduler.adjustment.seasonal import (
    CITIES,
    SeasonalAutoProvider,
    nearest_city,
)
from custom_components.rainbird_scheduler.models import (
    AdjustmentProviderConfig,
    ProviderKind,
)

from .helpers import make_occurrence, make_program, make_zone

_NOW = datetime(2026, 6, 1, tzinfo=UTC)


def test_every_city_curve_has_twelve_months_peaking_at_100() -> None:
    for city in CITIES:
        assert len(city.monthly_percent) == 12, city.name
        assert max(city.monthly_percent) == 100, city.name
        assert min(city.monthly_percent) > 0, city.name


def test_nearest_city_prefers_the_closest() -> None:
    # Fort Worth, TX is ~30 miles from Dallas.
    city, miles = nearest_city(32.76, -97.33)
    assert city.name == "Dallas, TX"
    assert miles < 50


def test_nearest_city_honolulu_and_anchorage_reachable() -> None:
    assert nearest_city(21.3, -157.8)[0].name == "Honolulu, HI"
    assert nearest_city(61.2, -149.9)[0].name == "Anchorage, AK"


@pytest.mark.parametrize(
    ("month", "expected_percent"),
    [(1, 30), (7, 100), (10, 60)],
)
async def test_seasonal_percent_follows_city_curve(
    month: int, expected_percent: int
) -> None:
    zone = make_zone("garden", 1, base_runtime_minutes=Decimal(20))
    program = make_program("p", ["garden"])
    occurrence = make_occurrence(
        program, datetime(2026, month, 15, 14, 0, tzinfo=UTC)
    )
    provider = SeasonalAutoProvider((33.45, -112.07))  # Phoenix
    result = await provider.async_calculate(zone, program, occurrence)
    assert result.seasonal_factor == Decimal(expected_percent)
    assert result.exact_adjusted_minutes == Decimal(20) * expected_percent / 100
    assert "Phoenix, AZ" in result.explanation[0]


async def test_seasonal_uses_local_month_not_utc() -> None:
    zone = make_zone("garden", 1)
    program = make_program("p", ["garden"])
    # 03:00 UTC on Feb 1 is still Jan 31 in America/Chicago.
    occurrence = make_occurrence(
        program, datetime(2026, 2, 1, 3, 0, tzinfo=UTC)
    )
    provider = SeasonalAutoProvider((41.88, -87.63))  # Chicago
    result = await provider.async_calculate(zone, program, occurrence)
    assert "January" in result.explanation[0]


async def test_missing_location_falls_back_to_100_percent() -> None:
    zone = make_zone("garden", 1, base_runtime_minutes=Decimal(10))
    program = make_program("p", ["garden"])
    occurrence = make_occurrence(program, _NOW)
    provider = SeasonalAutoProvider(None)
    result = await provider.async_calculate(zone, program, occurrence)
    assert result.quantized_minutes == 10
    assert result.stale_inputs == ("home_location",)


async def test_create_provider_wires_seasonal_auto() -> None:
    provider = create_provider(
        AdjustmentProviderConfig(kind=ProviderKind.SEASONAL_AUTO),
        get_state=lambda _: None,
        now_fn=lambda: _NOW,
        location=(25.76, -80.19),  # Miami
    )
    assert isinstance(provider, SeasonalAutoProvider)
    zone = make_zone("garden", 1, base_runtime_minutes=Decimal(10))
    program = make_program("p", ["garden"])
    occurrence = make_occurrence(
        program, datetime(2026, 1, 15, 14, 0, tzinfo=UTC)
    )
    result = await provider.async_calculate(zone, program, occurrence)
    # Miami January = 55% of peak.
    assert result.seasonal_factor == Decimal(55)

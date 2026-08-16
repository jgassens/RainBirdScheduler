"""Quantization and cycle-allocation invariants (plan §15)."""

from __future__ import annotations

from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from custom_components.rainbird_scheduler.planner import (
    allocate_cycles,
    quantize_zone_minutes,
)


def test_round_half_up() -> None:
    assert quantize_zone_minutes(Decimal("0.4")) == 0
    assert quantize_zone_minutes(Decimal("0.5")) == 1
    assert quantize_zone_minutes(Decimal("2.49")) == 2
    assert quantize_zone_minutes(Decimal("11.5")) == 12
    assert quantize_zone_minutes(Decimal("12")) == 12
    assert quantize_zone_minutes(Decimal("0")) == 0


def test_cycle_allocation_example() -> None:
    # Plan §15.3: 11 minutes with a 4-minute max cycle -> 4, 4, 3.
    assert allocate_cycles(11, 4) == [4, 4, 3]
    assert allocate_cycles(12, 4) == [4, 4, 4]
    assert allocate_cycles(4, 4) == [4]
    assert allocate_cycles(5, 4) == [3, 2]
    assert allocate_cycles(0, 4) == []
    assert allocate_cycles(7, None) == [7]
    assert allocate_cycles(7, 0) == [7]


@given(
    exact=st.decimals(
        min_value=Decimal(0),
        max_value=Decimal(2000),
        allow_nan=False,
        allow_infinity=False,
        places=3,
    )
)
def test_quantize_matches_half_up_definition(exact: Decimal) -> None:
    quantized = quantize_zone_minutes(exact)
    assert quantized == int((exact + Decimal("0.5")).to_integral_value(
        rounding="ROUND_FLOOR"
    ))


@given(
    total=st.integers(min_value=1, max_value=1440),
    max_cycle=st.integers(min_value=1, max_value=120),
)
def test_cycles_sum_exactly_and_respect_max(total: int, max_cycle: int) -> None:
    cycles = allocate_cycles(total, max_cycle)
    # The invariant is: sum(cycle durations) == quantized zone total.
    assert sum(cycles) == total
    assert all(1 <= cycle <= max_cycle for cycle in cycles)
    # Balanced split: durations differ by at most one minute.
    assert max(cycles) - min(cycles) <= 1

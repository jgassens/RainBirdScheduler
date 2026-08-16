"""Cycle+Soak compilation (plan §16) and controller-wide invariants."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from custom_components.rainbird_scheduler.planner import compile_timeline

from .helpers import (
    make_controller,
    make_input,
    make_occurrence,
    make_program,
    make_zone,
)

NINE_AM = datetime(2026, 6, 3, 14, 0, tzinfo=UTC)
GAP = timedelta(seconds=5)


def test_cycle_split_and_soak_spacing_single_zone() -> None:
    zones = [
        make_zone(
            "bed",
            1,
            base_runtime_minutes=Decimal(11),
            max_cycle_minutes=4,
            minimum_soak_minutes=10,
        )
    ]
    program = make_program("p", ["bed"])
    occurrence = make_occurrence(program, NINE_AM)
    timeline = compile_timeline(
        make_input(make_controller(), [program], zones, [occurrence])
    )
    steps = timeline.runs[0].steps
    assert [step.duration_minutes for step in steps] == [4, 4, 3]
    assert [step.cycle_index for step in steps] == [1, 2, 3]
    assert all(step.cycle_count == 3 for step in steps)
    # Soak: each later cycle starts >= previous end + 10 minutes.
    for earlier, later in zip(steps, steps[1:], strict=False):
        assert later.planned_start_utc >= earlier.planned_end_utc + timedelta(
            minutes=10
        )
        assert later.soak_before


def test_other_zones_fill_soak_periods() -> None:
    zones = [
        make_zone(
            "cycled",
            1,
            base_runtime_minutes=Decimal(8),
            max_cycle_minutes=4,
            minimum_soak_minutes=10,
        ),
        make_zone("filler", 2, base_runtime_minutes=Decimal(6)),
    ]
    program = make_program("p", ["cycled", "filler"])
    occurrence = make_occurrence(program, NINE_AM)
    timeline = compile_timeline(
        make_input(make_controller(), [program], zones, [occurrence])
    )
    steps = timeline.runs[0].steps
    order = [(step.zone_id, step.cycle_index) for step in steps]
    assert order == [("cycled", 1), ("filler", 1), ("cycled", 2)]
    # The filler ran inside the soak period, right after the gap.
    assert steps[1].planned_start_utc == steps[0].planned_end_utc + GAP
    # Cycle 2 still respected its soak interval.
    assert steps[2].planned_start_utc >= steps[0].planned_end_utc + timedelta(
        minutes=10
    )


def test_cycle_sums_equal_quantized_totals() -> None:
    zones = [
        make_zone(
            "a",
            1,
            base_runtime_minutes=Decimal("11.4"),
            max_cycle_minutes=4,
            minimum_soak_minutes=5,
        ),
        make_zone(
            "b",
            2,
            base_runtime_minutes=Decimal("7.5"),
            max_cycle_minutes=3,
            minimum_soak_minutes=5,
        ),
    ]
    program = make_program("p", ["a", "b"])
    occurrence = make_occurrence(program, NINE_AM)
    timeline = compile_timeline(
        make_input(make_controller(), [program], zones, [occurrence])
    )
    totals: dict[str, int] = defaultdict(int)
    for step in timeline.runs[0].steps:
        totals[step.zone_id] += step.duration_minutes
    # 11.4 quantizes to 11; 7.5 quantizes to 8. Cycle sums match exactly.
    assert totals == {"a": 11, "b": 8}


zone_specs = st.lists(
    st.tuples(
        st.decimals(
            min_value=Decimal("0.1"),
            max_value=Decimal(45),
            allow_nan=False,
            allow_infinity=False,
            places=1,
        ),
        st.one_of(st.none(), st.integers(min_value=1, max_value=8)),
        st.integers(min_value=0, max_value=20),
        st.integers(min_value=0, max_value=900),
    ),
    min_size=1,
    max_size=5,
)


@settings(max_examples=60, deadline=None)
@given(specs=zone_specs)
def test_compiled_timeline_invariants(specs) -> None:
    """Property-based invariants from plan §41."""
    zones = []
    zone_ids = []
    for index, (base, max_cycle, soak, offset) in enumerate(specs):
        zone_id = f"zone-{index}"
        zone_ids.append((zone_id, offset))
        zones.append(
            make_zone(
                zone_id,
                index + 1,
                base_runtime_minutes=base,
                max_cycle_minutes=max_cycle,
                minimum_soak_minutes=soak,
            )
        )
    program = make_program("p", [zone_id for zone_id, _ in zone_ids])
    for step, (_, offset) in zip(program.zone_steps, zone_ids, strict=True):
        step.requested_offset_seconds = offset
    occurrence = make_occurrence(program, NINE_AM)
    controller = make_controller()
    inp = make_input(controller, [program], zones, [occurrence])

    first = compile_timeline(inp)
    second = compile_timeline(inp)

    from custom_components.rainbird_scheduler import serde

    # Identical inputs produce identical timelines.
    assert serde.dump(first) == serde.dump(second)

    steps = sorted(
        (step for run in first.runs for step in run.steps),
        key=lambda step: step.planned_start_utc,
    )
    zone_profiles = {zone.id: zone for zone in zones}

    for earlier, later in zip(steps, steps[1:], strict=False):
        # No two compiled steps overlap; the inter-zone gap is respected.
        assert later.planned_start_utc >= earlier.planned_end_utc + GAP

    per_zone_steps: dict[str, list] = defaultdict(list)
    for step in steps:
        # Actual start never precedes the requested start.
        assert step.planned_start_utc >= step.requested_start_utc
        # Every command duration is a valid controller command.
        assert 1 <= step.duration_minutes <= 1440
        per_zone_steps[step.zone_id].append(step)

    for zone_id, zone_steps in per_zone_steps.items():
        zone = zone_profiles[zone_id]
        from custom_components.rainbird_scheduler.planner import (
            quantize_zone_minutes,
        )

        # Cycle durations sum exactly to the quantized zone total.
        assert sum(s.duration_minutes for s in zone_steps) == (
            quantize_zone_minutes(zone.base_runtime_minutes)
        )
        soak = timedelta(minutes=zone.minimum_soak_minutes or 0)
        for earlier, later in zip(zone_steps, zone_steps[1:], strict=False):
            # No cycle begins before its minimum soak interval.
            assert later.planned_start_utc >= earlier.planned_end_utc + soak

    # A positive sub-resolution runtime is never silently removed.
    run = first.runs[0]
    accounted = set(per_zone_steps) | {sz.zone_id for sz in run.skipped_zones}
    assert accounted == {zone.id for zone in zones}
    for skipped in run.skipped_zones:
        assert skipped.reason is not None
        assert skipped.detail

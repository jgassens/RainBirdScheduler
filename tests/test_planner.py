"""Pure planner behavior (plan §2, §14, §15)."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

from custom_components.rainbird_scheduler import serde
from custom_components.rainbird_scheduler.models import (
    MinimumRuntimePolicy,
    MissedRunPolicy,
    SkipReason,
    WateringWindow,
    WindowPolicy,
)
from custom_components.rainbird_scheduler.planner import compile_timeline

from .helpers import (
    make_controller,
    make_input,
    make_occurrence,
    make_program,
    make_zone,
)

# 9:00 AM America/Chicago in June == 14:00 UTC.
NINE_AM = datetime(2026, 6, 3, 14, 0, tzinfo=UTC)


def test_shared_requested_start_serializes_with_gaps() -> None:
    """The plan §2 flagship example, exactly."""
    zones = [
        make_zone("front-lawn", 1, base_runtime_minutes=Decimal(12)),
        make_zone("side-lawn", 2, base_runtime_minutes=Decimal(8)),
        make_zone("back-lawn", 3, base_runtime_minutes=Decimal(15)),
    ]
    program = make_program(
        "morning-lawn", ["front-lawn", "side-lawn", "back-lawn"]
    )
    occurrence = make_occurrence(program, NINE_AM)
    timeline = compile_timeline(
        make_input(make_controller(), [program], zones, [occurrence])
    )

    assert len(timeline.runs) == 1
    steps = timeline.runs[0].steps
    assert [step.zone_name for step in steps] == [
        "Front Lawn",
        "Side Lawn",
        "Back Lawn",
    ]
    assert steps[0].planned_start_utc == NINE_AM
    assert steps[0].planned_end_utc == NINE_AM + timedelta(minutes=12)
    assert steps[1].planned_start_utc == NINE_AM + timedelta(minutes=12, seconds=5)
    assert steps[1].planned_end_utc == NINE_AM + timedelta(minutes=20, seconds=5)
    assert steps[2].planned_start_utc == NINE_AM + timedelta(minutes=20, seconds=10)
    assert steps[2].planned_end_utc == NINE_AM + timedelta(minutes=35, seconds=10)
    # Every zone shares the same requested start.
    assert all(step.requested_start_utc == NINE_AM for step in steps)


def test_identical_inputs_produce_identical_timelines() -> None:
    zones = [make_zone(f"zone-{i}", i) for i in range(1, 4)]
    program = make_program("p", [zone.id for zone in zones])
    occurrence = make_occurrence(program, NINE_AM)
    inp = make_input(make_controller(), [program], zones, [occurrence])
    first = compile_timeline(inp)
    second = compile_timeline(inp)
    assert serde.dump(first) == serde.dump(second)
    assert first.runs[0].run_id == second.runs[0].run_id


def test_actual_start_never_before_requested() -> None:
    zones = [
        make_zone("a", 1, base_runtime_minutes=Decimal(30)),
        make_zone("b", 2, base_runtime_minutes=Decimal(5)),
    ]
    program = make_program("p", ["a", "b"])
    program.zone_steps[1].requested_offset_seconds = 600
    occurrence = make_occurrence(program, NINE_AM)
    timeline = compile_timeline(
        make_input(make_controller(), [program], zones, [occurrence])
    )
    steps = timeline.runs[0].steps
    for step in steps:
        assert step.planned_start_utc >= step.requested_start_utc
    # Zone b requested 9:10 but zone a runs until 9:30; b is delayed.
    assert steps[1].planned_start_utc == NINE_AM + timedelta(minutes=30, seconds=5)


def test_two_programs_overlap_priority_orders_deterministically() -> None:
    zones = [make_zone("a", 1), make_zone("b", 2)]
    high = make_program("high", ["a"], priority=1)
    low = make_program("low", ["b"], priority=50)
    occurrences = [
        make_occurrence(low, NINE_AM),
        make_occurrence(high, NINE_AM),
    ]
    timeline = compile_timeline(
        make_input(make_controller(), [high, low], zones, occurrences)
    )
    assert len(timeline.runs) == 2
    all_steps = sorted(
        (step for run in timeline.runs for step in run.steps),
        key=lambda step: step.planned_start_utc,
    )
    assert [step.zone_id for step in all_steps] == ["a", "b"]
    # No overlap between the two runs.
    assert all_steps[1].planned_start_utc >= all_steps[0].planned_end_utc


def test_sub_resolution_zone_skipped_with_reason() -> None:
    zones = [make_zone("tiny", 1, base_runtime_minutes=Decimal("0.4"))]
    program = make_program("p", ["tiny"])
    occurrence = make_occurrence(program, NINE_AM)
    timeline = compile_timeline(
        make_input(make_controller(), [program], zones, [occurrence])
    )
    run = timeline.runs[0]
    assert run.steps == ()
    assert len(run.skipped_zones) == 1
    skipped = run.skipped_zones[0]
    assert skipped.reason is SkipReason.BELOW_RESOLUTION
    assert "0.4" in skipped.detail


def test_sub_resolution_clamp_policy_yields_one_minute() -> None:
    zones = [
        make_zone(
            "tiny",
            1,
            base_runtime_minutes=Decimal("0.4"),
            minimum_runtime_policy=MinimumRuntimePolicy.CLAMP_TO_ONE_MINUTE,
        )
    ]
    program = make_program("p", ["tiny"])
    occurrence = make_occurrence(program, NINE_AM)
    timeline = compile_timeline(
        make_input(make_controller(), [program], zones, [occurrence])
    )
    steps = timeline.runs[0].steps
    assert len(steps) == 1
    assert steps[0].duration_minutes == 1
    assert steps[0].exact_minutes == Decimal("0.4")


def test_zero_runtime_zone_skipped() -> None:
    zones = [make_zone("off", 1, base_runtime_minutes=Decimal(0))]
    program = make_program("p", ["off"])
    occurrence = make_occurrence(program, NINE_AM)
    timeline = compile_timeline(
        make_input(make_controller(), [program], zones, [occurrence])
    )
    run = timeline.runs[0]
    assert run.steps == ()
    assert run.skipped_zones[0].reason is SkipReason.BELOW_RESOLUTION


def test_watering_window_skip_step() -> None:
    zones = [
        make_zone("a", 1, base_runtime_minutes=Decimal(12)),
        make_zone("b", 2, base_runtime_minutes=Decimal(8)),
    ]
    program = make_program("p", ["a", "b"])
    program.watering_window = WateringWindow(
        start_local=time(9, 0), end_local=time(9, 10)
    )
    occurrence = make_occurrence(program, NINE_AM)
    timeline = compile_timeline(
        make_input(make_controller(), [program], zones, [occurrence])
    )
    run = timeline.runs[0]
    assert [step.zone_id for step in run.steps] == ["a"]
    assert run.skipped_zones[0].zone_id == "b"
    assert run.skipped_zones[0].reason is SkipReason.OUT_OF_WINDOW


def test_watering_window_truncate_last() -> None:
    zones = [make_zone("a", 1, base_runtime_minutes=Decimal(12))]
    program = make_program("p", ["a"])
    program.watering_window = WateringWindow(
        start_local=time(9, 0),
        end_local=time(9, 10),
        policy=WindowPolicy.TRUNCATE_LAST,
    )
    occurrence = make_occurrence(program, NINE_AM)
    timeline = compile_timeline(
        make_input(make_controller(), [program], zones, [occurrence])
    )
    steps = timeline.runs[0].steps
    assert len(steps) == 1
    assert steps[0].duration_minutes == 10
    assert steps[0].planned_end_utc == NINE_AM + timedelta(minutes=10)


def test_watering_window_defer_occurrence_records_conflict() -> None:
    zones = [make_zone("a", 1, base_runtime_minutes=Decimal(12))]
    program = make_program("p", ["a"])
    program.watering_window = WateringWindow(
        start_local=time(8, 0),
        end_local=time(8, 30),
        policy=WindowPolicy.DEFER_OCCURRENCE,
    )
    occurrence = make_occurrence(program, NINE_AM)  # outside the window
    timeline = compile_timeline(
        make_input(make_controller(), [program], zones, [occurrence])
    )
    assert timeline.runs == ()
    assert len(timeline.conflicts) == 1
    assert timeline.conflicts[0].reason is SkipReason.OUT_OF_WINDOW


def test_missed_tolerance_skip_policy_drops_late_occurrence() -> None:
    zones = [
        make_zone("long", 1, base_runtime_minutes=Decimal(60)),
        make_zone("late", 2, base_runtime_minutes=Decimal(5)),
    ]
    blocker = make_program("blocker", ["long"], priority=1)
    skipper = make_program("skipper", ["late"], priority=2)
    skipper.missed_run_policy = MissedRunPolicy.SKIP
    occurrences = [
        make_occurrence(blocker, NINE_AM),
        make_occurrence(skipper, NINE_AM),
    ]
    timeline = compile_timeline(
        make_input(make_controller(), [blocker, skipper], zones, occurrences)
    )
    # The blocker runs 60 minutes; the skipper's earliest start (9:60:05)
    # exceeds its 30-minute missed-run tolerance and it is skipped.
    assert len(timeline.runs) == 1
    assert timeline.runs[0].program_id == "blocker"
    assert any(
        conflict.reason is SkipReason.MISSED_TOLERANCE
        for conflict in timeline.conflicts
    )


def test_missed_tolerance_run_late_policy_keeps_occurrence() -> None:
    zones = [
        make_zone("long", 1, base_runtime_minutes=Decimal(60)),
        make_zone("late", 2, base_runtime_minutes=Decimal(5)),
    ]
    blocker = make_program("blocker", ["long"], priority=1)
    runner = make_program("runner", ["late"], priority=2)  # default RUN_LATE
    occurrences = [
        make_occurrence(blocker, NINE_AM),
        make_occurrence(runner, NINE_AM),
    ]
    timeline = compile_timeline(
        make_input(make_controller(), [blocker, runner], zones, occurrences)
    )
    assert len(timeline.runs) == 2
    late_run = next(run for run in timeline.runs if run.program_id == "runner")
    assert late_run.steps[0].planned_start_utc == NINE_AM + timedelta(
        minutes=60, seconds=5
    )


def test_disabled_program_occurrence_conflicts_unless_manual() -> None:
    zones = [make_zone("a", 1)]
    program = make_program("p", ["a"])
    program.enabled = False
    automatic = make_occurrence(program, NINE_AM)
    timeline = compile_timeline(
        make_input(make_controller(), [program], zones, [automatic])
    )
    assert timeline.runs == ()
    assert timeline.conflicts[0].reason is SkipReason.PROGRAM_DISABLED

    manual = make_occurrence(program, NINE_AM, manual=True)
    timeline = compile_timeline(
        make_input(make_controller(), [program], zones, [manual])
    )
    assert len(timeline.runs) == 1
    assert timeline.runs[0].manual


def test_durations_are_valid_controller_commands() -> None:
    zones = [
        make_zone("big", 1, base_runtime_minutes=Decimal(5000)),
        make_zone("normal", 2, base_runtime_minutes=Decimal("7.5")),
    ]
    program = make_program("p", ["big", "normal"])
    occurrence = make_occurrence(program, NINE_AM)
    timeline = compile_timeline(
        make_input(make_controller(), [program], zones, [occurrence])
    )
    for step in timeline.runs[0].steps:
        assert 1 <= step.duration_minutes <= 1440


def test_all_zones_skipped_emits_cancellation_warning() -> None:
    zones = [
        make_zone("off-a", 1, base_runtime_minutes=Decimal(0)),
        make_zone("off-b", 2, base_runtime_minutes=Decimal(0)),
    ]
    program = make_program("p", ["off-a", "off-b"])
    occurrence = make_occurrence(program, NINE_AM)
    timeline = compile_timeline(
        make_input(make_controller(), [program], zones, [occurrence])
    )
    assert len(timeline.warnings) == 1
    warning = timeline.warnings[0]
    assert warning.occurrence_id == occurrence.occurrence_id
    assert warning.program_id == program.id
    assert "will not water" in warning.message
    assert "all 2 zone(s) skipped" in warning.message
    assert "below_resolution" in warning.message


def test_partial_skip_does_not_warn_of_cancellation() -> None:
    zones = [
        make_zone("on", 1, base_runtime_minutes=Decimal(10)),
        make_zone("off", 2, base_runtime_minutes=Decimal(0)),
    ]
    program = make_program("p", ["on", "off"])
    occurrence = make_occurrence(program, NINE_AM)
    timeline = compile_timeline(
        make_input(make_controller(), [program], zones, [occurrence])
    )
    assert timeline.warnings == ()
    assert len(timeline.runs[0].steps) == 1
    assert timeline.runs[0].skipped_zones[0].zone_id == "off"

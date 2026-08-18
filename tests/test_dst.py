"""Daylight-saving rules (plan §13): explicit, deterministic, tested."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from custom_components.rainbird_scheduler.models import (
    DstNonexistentPolicy,
    RecurrenceRule,
)
from custom_components.rainbird_scheduler.recurrence import (
    occurrences_between,
    resolve_local_start,
)

from .helpers import make_program

NY = ZoneInfo("America/New_York")
CREATED = datetime(2026, 1, 1, tzinfo=UTC)

# US 2026: spring forward Sun Mar 8 (02:00 -> 03:00), fall back Sun Nov 1.
SPRING_DAY = date(2026, 3, 8)
FALL_DAY = date(2026, 11, 1)


def test_normal_day_resolves_directly() -> None:
    resolved = resolve_local_start(
        date(2026, 6, 1), time(9, 0), NY, DstNonexistentPolicy.SHIFT_FORWARD
    )
    assert resolved == datetime(2026, 6, 1, 13, 0, tzinfo=UTC)  # EDT -4


def test_spring_forward_shifts_to_first_valid_instant() -> None:
    # 02:30 does not exist on 2026-03-08; the first valid local instant is
    # 03:00 EDT, i.e. 07:00 UTC.
    resolved = resolve_local_start(
        SPRING_DAY, time(2, 30), NY, DstNonexistentPolicy.SHIFT_FORWARD
    )
    assert resolved == datetime(2026, 3, 8, 7, 0, tzinfo=UTC)
    local = resolved.astimezone(NY)
    assert local.hour == 3
    assert local.minute == 0


def test_spring_forward_skip_policy_drops_and_warns() -> None:
    program = make_program("early", ["zone-a"], start=time(2, 30))
    program.recurrence = RecurrenceRule(
        weekdays=frozenset(range(7)),
        dst_nonexistent_policy=DstNonexistentPolicy.SKIP,
    )
    start = datetime(2026, 3, 7, 12, 0, tzinfo=UTC)
    end = datetime(2026, 3, 9, 12, 0, tzinfo=UTC)
    occurrences, warnings = occurrences_between(program, NY, start, end, CREATED)
    days = {occ.scheduled_start_local.date() for occ in occurrences}
    assert SPRING_DAY not in days
    assert date(2026, 3, 9) in days
    assert any("daylight-saving gap" in w.message for w in warnings)


def test_fall_back_runs_first_instance_only() -> None:
    # 01:30 happens twice on 2026-11-01; only the first (EDT, 05:30 UTC)
    # instance may run.
    program = make_program("fall", ["zone-a"], start=time(1, 30))
    start = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    end = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
    occurrences, warnings = occurrences_between(program, NY, start, end, CREATED)
    assert not warnings
    assert len(occurrences) == 1
    assert occurrences[0].scheduled_start_utc == datetime(
        2026, 11, 1, 5, 30, tzinfo=UTC
    )
    # The day after, 01:30 EST maps to 06:30 UTC.
    occurrences, _ = occurrences_between(
        program,
        NY,
        datetime(2026, 11, 2, 0, 0, tzinfo=UTC),
        datetime(2026, 11, 3, 0, 0, tzinfo=UTC),
        CREATED,
    )
    assert occurrences[0].scheduled_start_utc == datetime(
        2026, 11, 2, 6, 30, tzinfo=UTC
    )


def test_occurrence_id_uses_selected_utc_instant() -> None:
    resolved = resolve_local_start(
        SPRING_DAY, time(2, 30), NY, DstNonexistentPolicy.SHIFT_FORWARD
    )
    assert resolved is not None
    program = make_program("early", ["zone-a"], start=time(2, 30))
    occurrences, _ = occurrences_between(
        program,
        NY,
        datetime(2026, 3, 8, 0, 0, tzinfo=UTC),
        datetime(2026, 3, 9, 0, 0, tzinfo=UTC),
        CREATED,
    )
    assert len(occurrences) == 1
    assert occurrences[0].occurrence_id.endswith(resolved.isoformat())


def test_gap_collapsed_starts_dedupe_to_one_occurrence() -> None:
    # 02:00 and 02:30 both fall inside the spring-forward gap; both shift to
    # 03:00 EDT (07:00 UTC). Only one occurrence may survive: duplicate
    # occurrence ids make the planner merge builds with doubled zone steps.
    program = make_program("early", ["zone-a"], start=time(2, 0))
    program.nominal_start_times = [time(2, 0), time(2, 30)]
    occurrences, _ = occurrences_between(
        program,
        NY,
        datetime(2026, 3, 8, 0, 0, tzinfo=UTC),
        datetime(2026, 3, 9, 0, 0, tzinfo=UTC),
        CREATED,
    )
    assert len(occurrences) == 1
    assert occurrences[0].scheduled_start_utc == datetime(
        2026, 3, 8, 7, 0, tzinfo=UTC
    )

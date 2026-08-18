"""Recurrence engine behavior (plan §13)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time

from custom_components.rainbird_scheduler.models import (
    RecurrenceKind,
    RecurrenceRule,
)
from custom_components.rainbird_scheduler.recurrence import (
    date_qualifies,
    occurrences_between,
)

from .helpers import TZ, make_program

CREATED = datetime(2026, 6, 1, tzinfo=UTC)


def window(start: datetime, days: int = 7) -> tuple[datetime, datetime]:
    from datetime import timedelta

    return start, start + timedelta(days=days)


def test_weekly_selected_days() -> None:
    rule = RecurrenceRule(
        kind=RecurrenceKind.WEEKLY, weekdays=frozenset({0, 2, 5})
    )
    # 2026-06-01 is a Monday.
    assert date_qualifies(rule, date(2026, 6, 1))
    assert not date_qualifies(rule, date(2026, 6, 2))
    assert date_qualifies(rule, date(2026, 6, 3))
    assert date_qualifies(rule, date(2026, 6, 6))
    assert not date_qualifies(rule, date(2026, 6, 7))


def test_odd_and_even_days() -> None:
    odd = RecurrenceRule(kind=RecurrenceKind.ODD_DAYS)
    even = RecurrenceRule(kind=RecurrenceKind.EVEN_DAYS)
    assert date_qualifies(odd, date(2026, 6, 1))
    assert not date_qualifies(odd, date(2026, 6, 2))
    assert date_qualifies(even, date(2026, 6, 2))
    # The 31st is odd; the even rule skips it.
    assert not date_qualifies(even, date(2026, 7, 31))


def test_interval_from_anchor() -> None:
    rule = RecurrenceRule(
        kind=RecurrenceKind.INTERVAL,
        interval_days=3,
        anchor_date=date(2026, 6, 1),
    )
    assert date_qualifies(rule, date(2026, 6, 1))
    assert not date_qualifies(rule, date(2026, 6, 2))
    assert date_qualifies(rule, date(2026, 6, 4))
    # Days before the anchor never qualify.
    assert not date_qualifies(rule, date(2026, 5, 29))
    # Interval without an anchor is inert.
    assert not date_qualifies(
        RecurrenceRule(kind=RecurrenceKind.INTERVAL, interval_days=3),
        date(2026, 6, 1),
    )


def test_month_and_date_bounds() -> None:
    rule = RecurrenceRule(
        kind=RecurrenceKind.WEEKLY,
        weekdays=frozenset(range(7)),
        months=frozenset({6}),
        start_date=date(2026, 6, 10),
        end_date=date(2026, 6, 20),
    )
    assert not date_qualifies(rule, date(2026, 6, 9))
    assert date_qualifies(rule, date(2026, 6, 10))
    assert date_qualifies(rule, date(2026, 6, 20))
    assert not date_qualifies(rule, date(2026, 6, 21))
    assert not date_qualifies(rule, date(2026, 7, 1))


def test_occurrences_daily_program_over_week() -> None:
    program = make_program("morning", ["zone-a"], start=time(9, 0))
    start, end = window(datetime(2026, 6, 1, 0, 0, tzinfo=UTC))
    occurrences, warnings = occurrences_between(program, TZ, start, end, CREATED)
    assert not warnings
    assert len(occurrences) == 7
    # 9:00 America/Chicago in June is 14:00 UTC (CDT).
    assert all(occ.scheduled_start_utc.hour == 14 for occ in occurrences)
    assert all(occ.scheduled_start_local.hour == 9 for occ in occurrences)
    # Occurrence ids embed the program and the UTC instant.
    first = occurrences[0]
    assert first.occurrence_id == (
        f"{program.id}:{first.scheduled_start_utc.isoformat()}"
    )


def test_occurrences_multiple_start_times_sorted() -> None:
    program = make_program("twice", ["zone-a"], start=time(9, 0))
    program.nominal_start_times = [time(18, 30), time(6, 15)]
    start, end = window(datetime(2026, 6, 1, 0, 0, tzinfo=UTC), days=1)
    occurrences, _ = occurrences_between(program, TZ, start, end, CREATED)
    assert len(occurrences) == 2
    assert occurrences[0].scheduled_start_utc < occurrences[1].scheduled_start_utc
    assert occurrences[0].scheduled_start_local.time() == time(6, 15)


def test_duplicate_start_times_dedupe_to_one_occurrence() -> None:
    program = make_program("twice", ["zone-a"], start=time(9, 0))
    program.nominal_start_times = [time(9, 0), time(9, 0)]
    start, end = window(datetime(2026, 6, 1, 0, 0, tzinfo=UTC), days=1)
    occurrences, _ = occurrences_between(program, TZ, start, end, CREATED)
    assert len(occurrences) == 1


def test_disabled_or_empty_program_yields_nothing() -> None:
    program = make_program("off", ["zone-a"])
    program.enabled = False
    start, end = window(datetime(2026, 6, 1, tzinfo=UTC))
    assert occurrences_between(program, TZ, start, end, CREATED) == ([], [])

    empty = make_program("empty", ["zone-a"])
    empty.nominal_start_times = []
    assert occurrences_between(empty, TZ, start, end, CREATED) == ([], [])


def test_window_bounds_are_half_open() -> None:
    program = make_program("edge", ["zone-a"], start=time(9, 0))
    exact = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)  # 9:00 CDT
    occurrences, _ = occurrences_between(
        program, TZ, exact, exact.replace(hour=15), CREATED
    )
    assert len(occurrences) == 1
    # An occurrence exactly at the window end is excluded.
    occurrences, _ = occurrences_between(
        program, TZ, exact.replace(hour=13), exact, CREATED
    )
    assert occurrences == []

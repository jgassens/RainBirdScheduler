"""Recurrence engine behavior (plan §13)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

from astral import Observer
from astral.sun import sunrise as astral_sunrise

from custom_components.rainbird_scheduler.models import (
    RecurrenceKind,
    RecurrenceRule,
    StartKind,
    StartTime,
)
from custom_components.rainbird_scheduler.recurrence import (
    date_qualifies,
    occurrences_between,
)

from .helpers import TZ, make_program

CREATED = datetime(2026, 6, 1, tzinfo=UTC)

# Roughly Chicago: matches the test timezone, far from polar edge cases.
LOCATION = (41.8781, -87.6298)


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


def test_sunrise_start_with_negative_offset() -> None:
    program = make_program("dawn", ["zone-a"])
    program.nominal_start_times = [
        StartTime(kind=StartKind.SUNRISE, offset_minutes=-30)
    ]
    start, end = window(datetime(2026, 6, 1, 0, 0, tzinfo=UTC), days=1)
    occurrences, warnings = occurrences_between(
        program, TZ, start, end, CREATED, location=LOCATION
    )
    assert not warnings
    assert len(occurrences) == 1
    expected = astral_sunrise(
        Observer(latitude=LOCATION[0], longitude=LOCATION[1]),
        date=date(2026, 6, 1),
        tzinfo=TZ,
    ) - timedelta(minutes=30)
    expected = expected.astimezone(UTC).replace(second=0, microsecond=0)
    assert occurrences[0].scheduled_start_utc == expected
    # Chicago sunrise in June is ~05:15 local; -30 min lands before 5 AM.
    local = occurrences[0].scheduled_start_local
    assert 4 <= local.hour <= 5
    assert local.second == 0


def test_solar_start_follows_the_season() -> None:
    program = make_program("dawn", ["zone-a"])
    program.nominal_start_times = [StartTime(kind=StartKind.SUNSET)]

    june, june_end = window(datetime(2026, 6, 1, 0, 0, tzinfo=UTC), days=1)
    december, dec_end = window(datetime(2026, 12, 1, 0, 0, tzinfo=UTC), days=1)
    summer, _ = occurrences_between(
        program, TZ, june, june_end, CREATED, location=LOCATION
    )
    winter, _ = occurrences_between(
        program, TZ, december, dec_end, CREATED, location=LOCATION
    )
    assert len(summer) == 1 and len(winter) == 1
    # Chicago sunset: ~8:20 PM in June, ~4:20 PM in December.
    assert summer[0].scheduled_start_local.hour >= 19
    assert winter[0].scheduled_start_local.hour <= 17


def test_mixed_clock_and_solar_starts_sort_by_instant() -> None:
    program = make_program("mixed", ["zone-a"])
    program.nominal_start_times = [
        StartTime(kind=StartKind.SUNSET, offset_minutes=15),
        StartTime(kind=StartKind.CLOCK, at=time(6, 0)),
    ]
    # Window aligned to local midnight so exactly one local day is inside.
    start, end = window(datetime(2026, 6, 1, 5, 0, tzinfo=UTC), days=1)
    occurrences, warnings = occurrences_between(
        program, TZ, start, end, CREATED, location=LOCATION
    )
    assert not warnings
    assert len(occurrences) == 2
    assert occurrences[0].scheduled_start_local.time() == time(6, 0)
    assert occurrences[1].scheduled_start_local.hour >= 19


def test_solar_start_without_location_warns_and_skips() -> None:
    program = make_program("dawn", ["zone-a"])
    program.nominal_start_times = [
        StartTime(kind=StartKind.SUNRISE, offset_minutes=-45),
        StartTime(kind=StartKind.CLOCK, at=time(7, 0)),
    ]
    start, end = window(datetime(2026, 6, 1, 0, 0, tzinfo=UTC), days=2)
    occurrences, warnings = occurrences_between(
        program, TZ, start, end, CREATED, location=None
    )
    # Clock starts still happen; the solar ones are skipped with ONE
    # warning, not one per day.
    assert len(occurrences) == 2
    assert all(
        occ.scheduled_start_local.time() == time(7, 0) for occ in occurrences
    )
    assert len(warnings) == 1
    assert "no home location" in warnings[0].message


def test_polar_day_solar_start_warns_and_skips() -> None:
    program = make_program("midnight-sun", ["zone-a"])
    program.nominal_start_times = [StartTime(kind=StartKind.SUNSET)]
    start, end = window(datetime(2026, 6, 20, 0, 0, tzinfo=UTC), days=1)
    # Longyearbyen: the sun does not set in June.
    occurrences, warnings = occurrences_between(
        program, TZ, start, end, CREATED, location=(78.22, 15.63)
    )
    assert occurrences == []
    assert warnings
    assert "does not rise/set" in warnings[0].message


def test_legacy_time_and_string_starts_still_work() -> None:
    program = make_program("legacy", ["zone-a"])
    # Direct construction with plain time objects and ISO strings — the
    # shapes stored configs and older callers use.
    program.nominal_start_times = [time(9, 0), "18:30:00"]
    start, end = window(datetime(2026, 6, 1, 0, 0, tzinfo=UTC), days=1)
    occurrences, warnings = occurrences_between(program, TZ, start, end, CREATED)
    assert not warnings
    assert [occ.scheduled_start_local.time() for occ in occurrences] == [
        time(9, 0),
        time(18, 30),
    ]

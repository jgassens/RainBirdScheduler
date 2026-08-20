"""Recurrence engine.

Pure module: no Home Assistant imports. Recurrence rules are stored in local
wall-clock terms and converted to UTC per occurrence, with explicit
daylight-saving behavior:

* Spring-forward nonexistent local times either shift to the first valid
  local instant (the transition itself, default) or are skipped, per rule
  policy.
* Fall-back repeated local times run the first wall-clock instance only
  (``fold=0``); the second instance is never emitted.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, tzinfo

from astral import Observer
from astral.sun import sunrise, sunset

from .models import (
    DstNonexistentPolicy,
    PlannerWarning,
    Program,
    ProgramOccurrence,
    RecurrenceKind,
    RecurrenceRule,
    StartKind,
    StartTime,
    make_occurrence_id,
)


def date_qualifies(rule: RecurrenceRule, day: date) -> bool:
    """Return True if ``day`` qualifies under ``rule``."""
    if rule.start_date is not None and day < rule.start_date:
        return False
    if rule.end_date is not None and day > rule.end_date:
        return False
    if rule.months is not None and day.month not in rule.months:
        return False

    if rule.kind is RecurrenceKind.WEEKLY:
        return day.weekday() in rule.weekdays
    if rule.kind is RecurrenceKind.ODD_DAYS:
        return day.day % 2 == 1
    if rule.kind is RecurrenceKind.EVEN_DAYS:
        return day.day % 2 == 0
    if rule.kind is RecurrenceKind.INTERVAL:
        if rule.anchor_date is None or not rule.interval_days:
            return False
        delta = (day - rule.anchor_date).days
        return delta >= 0 and delta % rule.interval_days == 0
    return False


def _roundtrips(naive: datetime, tz: tzinfo) -> bool:
    local = naive.replace(tzinfo=tz)
    return local.astimezone(UTC).astimezone(tz).replace(tzinfo=None) == naive


def _is_nonexistent(naive: datetime, tz: tzinfo) -> bool:
    return not _roundtrips(naive, tz)


def _first_valid_instant_after_gap(naive: datetime, tz: tzinfo) -> datetime:
    """Return the UTC transition instant for a nonexistent local time.

    For any local time inside a spring-forward gap, the first valid local
    instant is the transition itself. Binary-search the UTC instant where
    the offset changes, between the two fold mappings.
    """
    lo = naive.replace(tzinfo=tz, fold=1).astimezone(UTC)  # before transition
    hi = naive.replace(tzinfo=tz, fold=0).astimezone(UTC)  # after transition
    if lo > hi:
        lo, hi = hi, lo
    target_offset = hi.astimezone(tz).utcoffset()
    while hi - lo > timedelta(minutes=1):
        mid = lo + (hi - lo) / 2
        mid = mid.replace(second=0, microsecond=0)
        if mid <= lo:
            break
        if mid.astimezone(tz).utcoffset() == target_offset:
            hi = mid
        else:
            lo = mid
    return hi


def resolve_local_start(
    day: date,
    start: time,
    tz: tzinfo,
    policy: DstNonexistentPolicy,
) -> datetime | None:
    """Resolve a local wall-clock start to a UTC instant, or None if skipped."""
    naive = datetime.combine(day, start)
    if _is_nonexistent(naive, tz):
        if policy is DstNonexistentPolicy.SKIP:
            return None
        return _first_valid_instant_after_gap(naive, tz)
    # Ambiguous or normal: fold=0 selects the first wall-clock instance.
    return naive.replace(tzinfo=tz, fold=0).astimezone(UTC)


def resolve_solar_start(
    start: StartTime,
    day: date,
    location: tuple[float, float],
    tz: tzinfo,
) -> datetime:
    """UTC instant for a sunrise/sunset start on the local date ``day``.

    Solar events are instants, not wall-clock times, so daylight-saving
    gaps and folds cannot affect them. Raises ``ValueError`` when the sun
    never crosses the horizon that day (polar summer/winter).
    """
    observer = Observer(latitude=location[0], longitude=location[1])
    event = sunrise if start.kind is StartKind.SUNRISE else sunset
    instant: datetime = event(observer, date=day, tzinfo=tz)
    instant = instant + timedelta(minutes=start.offset_minutes)
    # Solar instants carry seconds; schedules read better on whole minutes.
    return instant.astimezone(UTC).replace(second=0, microsecond=0)


def occurrences_between(
    program: Program,
    tz: tzinfo,
    window_start_utc: datetime,
    window_end_utc: datetime,
    created_at_utc: datetime,
    *,
    location: tuple[float, float] | None = None,
) -> tuple[list[ProgramOccurrence], list[PlannerWarning]]:
    """Return the program's occurrences with starts in the UTC window."""
    occurrences: list[ProgramOccurrence] = []
    warnings: list[PlannerWarning] = []
    if not program.enabled or not program.nominal_start_times:
        return occurrences, warnings
    starts = [StartTime.normalize(s) for s in program.nominal_start_times]
    seen_ids: set[str] = set()
    warned: set[str] = set()

    def warn(message: str) -> None:
        if message in warned:
            return
        warned.add(message)
        warnings.append(
            PlannerWarning(
                occurrence_id=None,
                program_id=program.id,
                zone_id=None,
                message=message,
            )
        )

    # Pad by a day on each side: a local date's start times can map to UTC
    # instants on the neighboring UTC dates.
    first_day = window_start_utc.astimezone(tz).date() - timedelta(days=1)
    last_day = window_end_utc.astimezone(tz).date() + timedelta(days=1)

    day = first_day
    while day <= last_day:
        if date_qualifies(program.recurrence, day):
            resolved: list[tuple[datetime, StartTime]] = []
            for start in starts:
                if start.kind is StartKind.CLOCK:
                    assert start.at is not None
                    utc_start = resolve_local_start(
                        day,
                        start.at,
                        tz,
                        program.recurrence.dst_nonexistent_policy,
                    )
                    if utc_start is None:
                        warn(
                            f"Skipped nonexistent local start "
                            f"{day.isoformat()} {start.at.isoformat()} "
                            "(daylight-saving gap)"
                        )
                        continue
                elif location is None:
                    warn(
                        f"Start '{start.label()}' skipped: Home Assistant "
                        "has no home location configured, so sunrise and "
                        "sunset cannot be computed."
                    )
                    continue
                else:
                    try:
                        utc_start = resolve_solar_start(
                            start, day, location, tz
                        )
                    except ValueError:
                        warn(
                            f"Start '{start.label()}' skipped on "
                            f"{day.isoformat()}: the sun does not "
                            "rise/set there that day."
                        )
                        continue
                resolved.append((utc_start, start))

            # Distinct nominal starts can resolve to the same instant
            # (e.g. 02:00 and 02:30 both shift to 03:00 across a
            # spring-forward gap): process in resolved order with a
            # deterministic tie-break and keep the first.
            resolved.sort(key=lambda item: (item[0], item[1].sort_key()))
            for utc_start, _start in resolved:
                if not window_start_utc <= utc_start < window_end_utc:
                    continue
                occurrence_id = make_occurrence_id(program.id, utc_start)
                if occurrence_id in seen_ids:
                    continue
                seen_ids.add(occurrence_id)
                occurrences.append(
                    ProgramOccurrence(
                        occurrence_id=occurrence_id,
                        program_id=program.id,
                        scheduled_start_utc=utc_start,
                        scheduled_start_local=utc_start.astimezone(tz),
                        created_at_utc=created_at_utc,
                    )
                )
        day += timedelta(days=1)

    occurrences.sort(key=lambda occ: occ.scheduled_start_utc)
    return occurrences, warnings

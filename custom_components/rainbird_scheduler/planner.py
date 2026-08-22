"""Pure planner.

This module must not import Home Assistant. It converts candidate program
occurrences plus adjustment snapshots into one deterministic, non-overlapping
controller timeline.

The controller is modeled as a resource with capacity one. All candidate
steps across overlapping occurrences merge into a single controller-wide
queue ordered by requested start, program priority (lower value first),
occurrence creation timestamp, zone position, and stable zone id.

Runtime quantization happens exactly once per zone (round-half-up to whole
minutes); the integer zone total is then split into cycles whose durations
sum exactly to it.
"""

from __future__ import annotations

import math
import uuid
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta, tzinfo
from decimal import ROUND_HALF_UP, Decimal
from itertools import pairwise

from .const import MAX_COMMAND_MINUTES
from .models import (
    AdjustmentResult,
    AdjustmentSnapshot,
    CompiledControllerTimeline,
    ControllerConfig,
    MinimumRuntimePolicy,
    MissedRunPolicy,
    PlannerConflict,
    PlannerWarning,
    Program,
    ProgramOccurrence,
    ProgramZoneStep,
    ProviderKind,
    RunPlan,
    RunStep,
    SkippedZone,
    SkipReason,
    WateringWindow,
    WindowPolicy,
    ZoneProfile,
)

_RUN_ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "rainbird-scheduler/run")


def quantize_zone_minutes(exact_minutes: Decimal) -> int:
    """Round an exact runtime to whole controller minutes, half-up."""
    return int(exact_minutes.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def allocate_cycles(total_minutes: int, max_cycle_minutes: int | None) -> list[int]:
    """Split a quantized zone total into cycles that sum exactly to it."""
    if total_minutes <= 0:
        return []
    if not max_cycle_minutes or max_cycle_minutes <= 0:
        return [total_minutes]
    if total_minutes <= max_cycle_minutes:
        return [total_minutes]
    cycle_count = math.ceil(total_minutes / max_cycle_minutes)
    base, remainder = divmod(total_minutes, cycle_count)
    return [base + 1 if index < remainder else base for index in range(cycle_count)]


def neutral_adjustment(base_runtime_minutes: Decimal) -> AdjustmentResult:
    """Return a fixed-100% adjustment for a zone with no provider input."""
    return AdjustmentResult(
        base_runtime_minutes=base_runtime_minutes,
        exact_adjusted_minutes=base_runtime_minutes,
        quantized_minutes=quantize_zone_minutes(base_runtime_minutes),
        seasonal_factor=Decimal(100),
        weather_factor=None,
        rain_credit_minutes=Decimal(0),
        carried_deficit_minutes=Decimal(0),
        input_timestamps={},
        stale_inputs=(),
        explanation=("No adjustment input; base runtime used at 100%.",),
    )


def deterministic_run_id(occurrence_id: str) -> str:
    """Derive a stable run id so identical inputs produce identical plans."""
    return uuid.uuid5(_RUN_ID_NAMESPACE, occurrence_id).hex


@dataclass(frozen=True)
class PlannerInput:
    """Everything the planner needs, supplied by the caller."""

    controller_config: ControllerConfig
    programs: dict[str, Program]
    zone_profiles: dict[str, ZoneProfile]
    candidate_occurrences: list[ProgramOccurrence]
    adjustment_snapshots: dict[str, AdjustmentSnapshot]
    tz: tzinfo
    compiled_at_utc: datetime


@dataclass
class _WorkItem:
    occurrence: ProgramOccurrence
    program: Program
    zone: ZoneProfile
    step: ProgramZoneStep
    requested_start_utc: datetime
    exact_minutes: Decimal
    total_minutes: int
    cycles: deque[int] = field(default_factory=deque)
    cycle_count: int = 0
    emitted_cycles: int = 0
    soak: timedelta = timedelta(0)
    next_eligible_utc: datetime = field(default=datetime.min)

    @property
    def sort_key(self) -> tuple[datetime, int, datetime, int, str]:
        return (
            self.requested_start_utc,
            self.program.priority,
            self.occurrence.created_at_utc,
            self.step.position,
            self.zone.id,
        )


@dataclass
class _OccurrenceBuild:
    occurrence: ProgramOccurrence
    program: Program
    snapshot: AdjustmentSnapshot
    steps: list[RunStep] = field(default_factory=list)
    skipped: list[SkippedZone] = field(default_factory=list)
    dropped: bool = False


def _window_contains(window: WateringWindow, local_time: time) -> bool:
    start, end = window.start_local, window.end_local
    if start == end:
        return True
    if start < end:
        return start <= local_time < end
    return local_time >= start or local_time < end


def _minutes_until_window_end(
    window: WateringWindow, local_dt: datetime
) -> int:
    """Whole minutes from ``local_dt`` until the window closes."""
    end = window.end_local
    end_dt = local_dt.replace(
        hour=end.hour, minute=end.minute, second=end.second, microsecond=0
    )
    if end_dt <= local_dt:
        end_dt += timedelta(days=1)
    return int((end_dt - local_dt).total_seconds() // 60)


def _effective_max_cycle(zone: ZoneProfile, step: ProgramZoneStep) -> int | None:
    if step.max_cycle_minutes_override is not None:
        return step.max_cycle_minutes_override
    return zone.max_cycle_minutes


def _effective_soak_minutes(zone: ZoneProfile, step: ProgramZoneStep) -> int:
    if step.minimum_soak_minutes_override is not None:
        return step.minimum_soak_minutes_override
    return zone.minimum_soak_minutes or 0


def _effective_base_runtime(zone: ZoneProfile, step: ProgramZoneStep) -> Decimal:
    if step.base_runtime_override_minutes is not None:
        return step.base_runtime_override_minutes
    return zone.base_runtime_minutes


def _effective_min_runtime_policy(
    controller: ControllerConfig, zone: ZoneProfile
) -> MinimumRuntimePolicy:
    return zone.minimum_runtime_policy or controller.minimum_runtime_policy


def compile_timeline(inp: PlannerInput) -> CompiledControllerTimeline:
    """Compile candidate occurrences into one non-overlapping timeline."""
    conflicts: list[PlannerConflict] = []
    warnings: list[PlannerWarning] = []
    builds: dict[str, _OccurrenceBuild] = {}
    work_items: list[_WorkItem] = []

    for occurrence in sorted(
        inp.candidate_occurrences,
        key=lambda occ: (occ.scheduled_start_utc, occ.occurrence_id),
    ):
        program = inp.programs.get(occurrence.program_id)
        if program is None:
            conflicts.append(
                PlannerConflict(
                    occurrence_id=occurrence.occurrence_id,
                    program_id=occurrence.program_id,
                    zone_id=None,
                    reason=SkipReason.PROGRAM_DISABLED,
                    message="Program no longer exists.",
                )
            )
            continue
        if not program.enabled and not occurrence.manual:
            conflicts.append(
                PlannerConflict(
                    occurrence_id=occurrence.occurrence_id,
                    program_id=program.id,
                    zone_id=None,
                    reason=SkipReason.PROGRAM_DISABLED,
                    message=f"Program {program.name!r} is disabled.",
                )
            )
            continue

        snapshot = inp.adjustment_snapshots.get(occurrence.occurrence_id)
        if snapshot is None:
            snapshot = AdjustmentSnapshot(
                provider_kind=ProviderKind.FIXED,
                computed_at_utc=inp.compiled_at_utc,
                per_zone={},
            )
        build = _OccurrenceBuild(
            occurrence=occurrence, program=program, snapshot=snapshot
        )
        builds[occurrence.occurrence_id] = build

        occurrence_items: list[_WorkItem] = []
        for step in sorted(program.zone_steps, key=lambda s: s.position):
            item = _build_work_item(inp, build, step)
            if item is not None:
                occurrence_items.append(item)
        work_items.extend(occurrence_items)

        if not occurrence_items and build.skipped:
            reasons = sorted({skip.reason.value for skip in build.skipped})
            local = occurrence.scheduled_start_local
            warnings.append(
                PlannerWarning(
                    occurrence_id=occurrence.occurrence_id,
                    program_id=program.id,
                    zone_id=None,
                    message=(
                        f"Program {program.name!r} at "
                        f"{local.strftime('%a %b %d %H:%M')} will not water: "
                        f"all {len(build.skipped)} zone(s) skipped "
                        f"({', '.join(reasons)})."
                    ),
                )
            )

    _schedule(inp, work_items, builds, conflicts)
    _enforce_lateness(inp, builds, conflicts)
    _detect_collisions(inp, builds, conflicts, warnings)

    runs = tuple(
        _finalize_run(inp, build)
        for build in sorted(
            builds.values(),
            key=lambda b: (
                b.occurrence.scheduled_start_utc,
                b.occurrence.occurrence_id,
            ),
        )
        if not build_dropped_entirely(build)
    )
    return CompiledControllerTimeline(
        runs=runs, conflicts=tuple(conflicts), warnings=tuple(warnings)
    )


def build_dropped_entirely(build: _OccurrenceBuild) -> bool:
    """Return True if the occurrence produced neither steps nor skip records."""
    return build.dropped and not build.steps and not build.skipped


def _build_work_item(
    inp: PlannerInput, build: _OccurrenceBuild, step: ProgramZoneStep
) -> _WorkItem | None:
    occurrence = build.occurrence
    zone = inp.zone_profiles.get(step.zone_id)
    if zone is None:
        build.skipped.append(
            SkippedZone(
                zone_id=step.zone_id,
                zone_name=step.zone_id,
                reason=SkipReason.SOURCE_UNAVAILABLE,
                detail="Zone profile no longer exists.",
            )
        )
        return None
    if not step.enabled:
        build.skipped.append(
            SkippedZone(
                zone_id=zone.id,
                zone_name=zone.display_name,
                reason=SkipReason.ZONE_DISABLED,
                detail="Step disabled in program.",
            )
        )
        return None
    if not zone.enabled:
        build.skipped.append(
            SkippedZone(
                zone_id=zone.id,
                zone_name=zone.display_name,
                reason=SkipReason.ZONE_DISABLED,
                detail="Zone disabled.",
            )
        )
        return None

    adjustment = build.snapshot.per_zone.get(zone.id)
    if adjustment is None:
        adjustment = neutral_adjustment(_effective_base_runtime(zone, step))

    exact = adjustment.exact_adjusted_minutes
    total = adjustment.quantized_minutes
    if total > MAX_COMMAND_MINUTES:
        total = MAX_COMMAND_MINUTES

    if total <= 0:
        if exact > 0:
            policy = _effective_min_runtime_policy(inp.controller_config, zone)
            if policy is MinimumRuntimePolicy.CLAMP_TO_ONE_MINUTE:
                total = 1
            else:
                detail = (
                    f"Exact runtime {exact} min is below the controller's "
                    "one-minute resolution"
                )
                if policy is MinimumRuntimePolicy.CARRY_FORWARD:
                    detail += "; deficit carried forward by the provider"
                build.skipped.append(
                    SkippedZone(
                        zone_id=zone.id,
                        zone_name=zone.display_name,
                        reason=SkipReason.BELOW_RESOLUTION,
                        detail=detail,
                    )
                )
                return None
        else:
            build.skipped.append(
                SkippedZone(
                    zone_id=zone.id,
                    zone_name=zone.display_name,
                    reason=SkipReason.BELOW_RESOLUTION,
                    detail="No runtime requested.",
                )
            )
            return None

    cycles = allocate_cycles(total, _effective_max_cycle(zone, step))
    requested = occurrence.scheduled_start_utc + timedelta(
        seconds=step.requested_offset_seconds
    )
    return _WorkItem(
        occurrence=occurrence,
        program=build.program,
        zone=zone,
        step=step,
        requested_start_utc=requested,
        exact_minutes=exact,
        total_minutes=total,
        cycles=deque(cycles),
        cycle_count=len(cycles),
        soak=timedelta(minutes=_effective_soak_minutes(zone, step)),
        next_eligible_utc=requested,
    )


def _schedule(
    inp: PlannerInput,
    work_items: list[_WorkItem],
    builds: dict[str, _OccurrenceBuild],
    conflicts: list[PlannerConflict],
) -> None:
    gap = timedelta(seconds=inp.controller_config.inter_zone_gap_seconds)
    eligible = [item for item in work_items if item.cycles]
    if not eligible:
        return

    # One run occupies the controller from its first step to its last —
    # soak waits included. The executor is strictly single-flight per
    # controller, so steps of different occurrences must never interleave
    # (a plan that tucks another run into a soak gap is unexecutable and
    # surfaces later as a mystifying busy-skip). Occurrences claim the
    # controller in request order; a later request that lands inside an
    # earlier run's block simply waits: its planned start is pushed to
    # the block's end, and the requested-vs-planned columns show the wait
    # up front. The missed-run policy then decides run-late vs skip in
    # _enforce_lateness.
    by_occurrence: dict[str, list[_WorkItem]] = {}
    for item in eligible:
        by_occurrence.setdefault(item.occurrence.occurrence_id, []).append(
            item
        )

    def claim_order(
        items: list[_WorkItem],
    ) -> tuple[datetime, int, datetime, str]:
        return (
            min(item.requested_start_utc for item in items),
            items[0].program.priority,
            items[0].occurrence.created_at_utc,
            items[0].occurrence.occurrence_id,
        )

    controller_free: datetime | None = None
    for pending in sorted(by_occurrence.values(), key=claim_order):
        build = builds[pending[0].occurrence.occurrence_id]
        cursor = min(item.next_eligible_utc for item in pending)
        if controller_free is not None and cursor < controller_free:
            cursor = controller_free
        _schedule_occurrence(inp, pending, build, conflicts, cursor, gap)
        if build.steps and not build.dropped:
            block_end = max(step.planned_end_utc for step in build.steps)
            controller_free = block_end + gap


def _schedule_occurrence(
    inp: PlannerInput,
    pending: list[_WorkItem],
    build: _OccurrenceBuild,
    conflicts: list[PlannerConflict],
    cursor: datetime,
    gap: timedelta,
) -> None:
    """Lay out one occurrence's cycles from ``cursor`` onward."""
    while any(item.cycles for item in pending):
        if build.dropped:
            for item in pending:
                item.cycles.clear()
            return
        ready = [
            item
            for item in pending
            if item.cycles and item.next_eligible_utc <= cursor
        ]
        if not ready:
            cursor = max(
                cursor,
                min(
                    item.next_eligible_utc
                    for item in pending
                    if item.cycles
                ),
            )
            continue

        item = min(ready, key=lambda entry: entry.sort_key)

        duration = item.cycles[0]
        window = item.program.watering_window
        if window is not None:
            local = cursor.astimezone(inp.tz)
            if not _window_contains(window, local.time()):
                _apply_window_violation(item, build, conflicts)
                continue
            if window.policy is WindowPolicy.TRUNCATE_LAST:
                available = _minutes_until_window_end(window, local)
                if duration > available:
                    if available < 1:
                        _apply_window_violation(item, build, conflicts)
                        continue
                    duration = available
                    # The zone cannot fit its remaining cycles: truncate
                    # here, record the dropped cycles, and correct the
                    # surviving steps' cycle count.
                    dropped = len(item.cycles) - 1
                    if dropped:
                        build.skipped.append(
                            SkippedZone(
                                zone_id=item.zone.id,
                                zone_name=item.zone.display_name,
                                reason=SkipReason.OUT_OF_WINDOW,
                                detail=(
                                    f"{dropped} remaining cycle(s) could not "
                                    "start inside the watering window"
                                ),
                            )
                        )
                    item.cycles = deque([duration])
                    item.cycle_count = item.emitted_cycles + 1
                    build.steps = [
                        replace(step, cycle_count=item.cycle_count)
                        if step.zone_id == item.zone.id
                        and step.requested_start_utc
                        == item.requested_start_utc
                        else step
                        for step in build.steps
                    ]

        item.cycles.popleft()
        item.emitted_cycles += 1
        start = cursor
        end = start + timedelta(minutes=duration)
        build.steps.append(
            RunStep(
                index=len(build.steps),
                zone_id=item.zone.id,
                zone_name=item.zone.display_name,
                station_number=item.zone.reference.station_number,
                cycle_index=item.emitted_cycles,
                cycle_count=item.cycle_count,
                requested_start_utc=item.requested_start_utc,
                planned_start_utc=start,
                planned_end_utc=end,
                duration_minutes=duration,
                exact_minutes=item.exact_minutes,
                soak_before=item.emitted_cycles > 1 and item.soak > timedelta(0),
            )
        )
        item.next_eligible_utc = end + item.soak
        cursor = end + gap


def _apply_window_violation(
    item: _WorkItem,
    build: _OccurrenceBuild,
    conflicts: list[PlannerConflict],
) -> None:
    window = item.program.watering_window
    assert window is not None
    remaining = len(item.cycles)
    item.cycles.clear()
    if window.policy in (WindowPolicy.SKIP_STEP, WindowPolicy.TRUNCATE_LAST):
        build.skipped.append(
            SkippedZone(
                zone_id=item.zone.id,
                zone_name=item.zone.display_name,
                reason=SkipReason.OUT_OF_WINDOW,
                detail=(
                    f"{remaining} remaining cycle(s) could not start inside "
                    "the watering window"
                ),
            )
        )
        return
    # DEFER_OCCURRENCE and REQUIRE_INTERVENTION drop the whole occurrence.
    build.dropped = True
    build.steps.clear()
    conflicts.append(
        PlannerConflict(
            occurrence_id=build.occurrence.occurrence_id,
            program_id=build.program.id,
            zone_id=item.zone.id,
            reason=SkipReason.OUT_OF_WINDOW,
            message=(
                "Occurrence deferred: work extends past the watering window"
                if window.policy is WindowPolicy.DEFER_OCCURRENCE
                else "Watering window conflict requires intervention"
            ),
        )
    )


def _enforce_lateness(
    inp: PlannerInput,
    builds: dict[str, _OccurrenceBuild],
    conflicts: list[PlannerConflict],
) -> None:
    tolerance = timedelta(
        minutes=inp.controller_config.missed_run_tolerance_minutes
    )
    for build in builds.values():
        if not build.steps:
            continue
        if build.program.missed_run_policy is not MissedRunPolicy.SKIP:
            continue
        # Lateness is measured from the earliest step's requested start
        # (occurrence start plus that step's requested offset): the offset is
        # user intent, not lateness.
        first_start = min(step.planned_start_utc for step in build.steps)
        first_requested = min(step.requested_start_utc for step in build.steps)
        latest = first_requested + tolerance
        if first_start > latest:
            build.dropped = True
            build.steps.clear()
            conflicts.append(
                PlannerConflict(
                    occurrence_id=build.occurrence.occurrence_id,
                    program_id=build.program.id,
                    zone_id=None,
                    reason=SkipReason.MISSED_TOLERANCE,
                    message=(
                        "Occurrence skipped: compiled start exceeds the "
                        "missed-run tolerance"
                    ),
                )
            )


def _detect_collisions(
    inp: PlannerInput,
    builds: dict[str, _OccurrenceBuild],
    conflicts: list[PlannerConflict],
    warnings: list[PlannerWarning],
) -> None:
    """Independent no-overlap verifier over the finished schedule.

    The scheduler above is *believed* to serialize runs; this pass
    *checks* it, so the invariant survives future edits to `_schedule`.
    Two guarantees are enforced on every compile:

    * Steps across ALL runs are pairwise non-overlapping and separated
      by the inter-zone gap.
    * Whole runs never interleave: each run's block (first step start to
      last step end, soak waits included) is disjoint from every other
      run's block. The executor is single-flight per controller, so an
      interleaved plan is unexecutable no matter how valid its steps
      look individually.

    A violating run is withheld with a PLANNER_COLLISION conflict — a
    loudly-reported planner bug — instead of being handed to the
    executor, where it would surface hours later as a mystery busy-skip.
    """
    gap = timedelta(seconds=inp.controller_config.inter_zone_gap_seconds)
    active = [b for b in builds.values() if b.steps and not b.dropped]
    # Verify in execution order; on violation drop the later block so the
    # survivors remain a valid serial plan.
    active.sort(key=lambda b: min(s.planned_start_utc for s in b.steps))

    def _span(build: _OccurrenceBuild) -> tuple[datetime, datetime]:
        return (
            min(s.planned_start_utc for s in build.steps),
            max(s.planned_end_utc for s in build.steps),
        )

    def _withhold(build: _OccurrenceBuild, detail: str) -> None:
        build.dropped = True
        build.steps.clear()
        conflicts.append(
            PlannerConflict(
                occurrence_id=build.occurrence.occurrence_id,
                program_id=build.program.id,
                zone_id=None,
                reason=SkipReason.PLANNER_COLLISION,
                message=(
                    f"Planner bug: {detail}. The run was withheld instead "
                    "of being handed to the controller; please report this."
                ),
            )
        )
        warnings.append(
            PlannerWarning(
                occurrence_id=build.occurrence.occurrence_id,
                program_id=build.program.id,
                zone_id=None,
                message=(
                    f"Program {build.program.name!r} was withheld by the "
                    f"collision detector: {detail}."
                ),
            )
        )

    # Intra-run: consecutive steps must respect the inter-zone gap.
    for build in list(active):
        steps = sorted(build.steps, key=lambda s: s.planned_start_utc)
        for earlier, later in pairwise(steps):
            if later.planned_start_utc < earlier.planned_end_utc + gap:
                _withhold(
                    build,
                    f"steps {earlier.zone_name!r} and {later.zone_name!r} "
                    "overlap within one run",
                )
                active.remove(build)
                break

    # Inter-run: blocks must be disjoint, in order, gap-separated.
    survivor: _OccurrenceBuild | None = None
    for build in list(active):
        if survivor is None:
            survivor = build
            continue
        _prev_start, prev_end = _span(survivor)
        start, _end = _span(build)
        if start < prev_end + gap:
            _withhold(
                build,
                f"run {build.program.name!r} "
                f"({start.isoformat()}) begins inside "
                f"{survivor.program.name!r}'s block "
                f"(ends {prev_end.isoformat()})",
            )
            continue
        survivor = build


def _finalize_run(inp: PlannerInput, build: _OccurrenceBuild) -> RunPlan:
    steps = tuple(
        RunStep(
            index=index,
            zone_id=step.zone_id,
            zone_name=step.zone_name,
            station_number=step.station_number,
            cycle_index=step.cycle_index,
            cycle_count=step.cycle_count,
            requested_start_utc=step.requested_start_utc,
            planned_start_utc=step.planned_start_utc,
            planned_end_utc=step.planned_end_utc,
            duration_minutes=step.duration_minutes,
            exact_minutes=step.exact_minutes,
            soak_before=step.soak_before,
        )
        for index, step in enumerate(
            sorted(build.steps, key=lambda s: s.planned_start_utc)
        )
    )
    return RunPlan(
        run_id=deterministic_run_id(build.occurrence.occurrence_id),
        occurrence_id=build.occurrence.occurrence_id,
        program_id=build.program.id,
        program_name=build.program.name,
        requested_start_utc=build.occurrence.scheduled_start_utc,
        compiled_at_utc=inp.compiled_at_utc,
        controller_config_revision=inp.controller_config.revision,
        program_revision=build.program.revision,
        adjustment_snapshot=build.snapshot,
        steps=steps,
        skipped_zones=tuple(build.skipped),
        manual=build.occurrence.manual,
    )

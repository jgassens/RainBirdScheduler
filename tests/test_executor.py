"""Executor: normal runs and user controls (plan §17–§19)."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from custom_components.rainbird_scheduler.executor import (
    DuplicateOccurrenceError,
)
from custom_components.rainbird_scheduler.models import (
    CommandDisposition,
    ExecutorState,
    RunOutcome,
    SkipReason,
    StepStatus,
)

from .harness import START, three_zone_rig


async def test_normal_multi_zone_run() -> None:
    rig = three_zone_rig()
    await rig.executor.async_start_run(rig.plan)

    assert rig.journal().state is ExecutorState.WATERING
    assert len(rig.driver.start_calls) == 1
    assert rig.driver.start_calls[0][0].station_number == 1
    assert rig.driver.start_calls[0][1] == 12

    # Zone 1 ends at 14:12; after the 5s gap zone 2 starts.
    await rig.tm.advance(12 * 60)
    assert rig.journal().state is ExecutorState.INTER_ZONE_GAP
    await rig.tm.advance(5)
    assert rig.journal().state is ExecutorState.WATERING
    assert rig.driver.start_calls[1][0].station_number == 2
    assert rig.driver.start_calls[1][1] == 8

    # Run everything to completion.
    await rig.tm.advance(2 * 3600)
    assert rig.journal().state is ExecutorState.IDLE
    assert [call[1] for call in rig.driver.start_calls] == [12, 8, 15]
    assert rig.history.finished == [
        (rig.plan.run_id, RunOutcome.COMPLETED, None)
    ]
    assert rig.event_types() == [
        "run_started",
        "zone_started",
        "zone_completed",
        "zone_started",
        "zone_completed",
        "zone_started",
        "zone_completed",
        "run_completed",
    ]
    # The occurrence is recorded for deduplication.
    assert rig.plan.occurrence_id in rig.journal().completed_occurrences
    # The journal was persisted at every transition and stayed JSON-safe.
    assert len(rig.store.writes) >= 10


async def test_zone_on_event_marks_command_accepted() -> None:
    rig = three_zone_rig()
    await rig.executor.async_start_run(rig.plan)
    assert rig.journal().pending_command is not None
    assert rig.journal().pending_command.disposition is CommandDisposition.SENT

    await rig.zone_event("front-lawn", True)
    assert (
        rig.journal().pending_command.disposition is CommandDisposition.ACCEPTED
    )
    # Still on the commanded clock.
    assert rig.journal().state is ExecutorState.WATERING


async def test_early_end_within_tolerance_completes_step() -> None:
    rig = three_zone_rig()
    await rig.executor.async_start_run(rig.plan)
    # Zone reports off 30s before its expected end (tolerance 45s).
    await rig.tm.advance(12 * 60 - 30)
    await rig.zone_event("front-lawn", False)
    journal = rig.journal()
    assert journal.state is ExecutorState.INTER_ZONE_GAP
    assert journal.step_results[0].status is StepStatus.COMPLETED
    assert journal.step_results[0].actual_end_utc == rig.tm.now
    # The next zone still starts.
    await rig.tm.advance(10)
    assert rig.driver.start_calls[1][0].station_number == 2


async def test_stop_mid_run_aborts_and_labels_remaining() -> None:
    rig = three_zone_rig()
    await rig.executor.async_start_run(rig.plan)
    await rig.tm.advance(60)
    await rig.executor.async_stop()

    assert rig.driver.stop_calls == 1
    journal = rig.journal()
    assert journal.state is ExecutorState.IDLE
    assert rig.history.finished[0][1] is RunOutcome.ABORTED_USER
    # The active step records the stop; pending steps record the skip reason.
    results = rig.final_step_results()
    assert results["0"]["status"] == StepStatus.EXTERNAL_STOP.value
    assert results["1"]["status"] == StepStatus.SKIPPED.value
    assert SkipReason.USER_STOP.value in results["1"]["reason"]
    assert rig.plan.occurrence_id in journal.completed_occurrences


async def test_stop_without_active_run_still_stops_controller() -> None:
    rig = three_zone_rig()
    await rig.executor.async_stop()
    assert rig.driver.stop_calls == 1
    assert rig.journal().state is ExecutorState.IDLE
    assert rig.history.finished == []


async def test_completed_occurrence_is_never_executed_twice() -> None:
    rig = three_zone_rig()
    await rig.executor.async_start_run(rig.plan)
    await rig.tm.advance(2 * 3600)
    assert rig.journal().state is ExecutorState.IDLE

    with pytest.raises(DuplicateOccurrenceError):
        await rig.executor.async_start_run(rig.plan)
    assert len(rig.driver.start_calls) == 3  # no extra commands


async def test_skip_current_stops_and_continues() -> None:
    rig = three_zone_rig()
    await rig.executor.async_start_run(rig.plan)
    await rig.tm.advance(60)
    await rig.executor.async_skip_current()

    assert rig.driver.stop_calls == 1
    journal = rig.journal()
    assert journal.step_results[0].status is StepStatus.SKIPPED
    assert journal.step_results[0].reason == "user_skip"
    assert journal.state is ExecutorState.INTER_ZONE_GAP
    await rig.tm.advance(5)
    assert rig.driver.start_calls[1][0].station_number == 2


async def test_pause_requires_explicit_resume() -> None:
    rig = three_zone_rig()
    await rig.executor.async_start_run(rig.plan)
    await rig.tm.advance(60)
    await rig.executor.async_pause()

    journal = rig.journal()
    assert rig.driver.stop_calls == 1
    assert journal.state is ExecutorState.PAUSED_EXTERNAL
    assert journal.paused_reason == "user_pause"

    # An all-idle observation does NOT auto-resume a user pause.
    await rig.zone_event("front-lawn", False, active=set())
    assert rig.journal().state is ExecutorState.PAUSED_EXTERNAL

    await rig.executor.async_resume()
    assert rig.journal().state is ExecutorState.WATERING
    assert rig.driver.start_calls[1][0].station_number == 2


async def test_resume_past_tolerance_expires_run() -> None:
    rig = three_zone_rig()
    await rig.executor.async_start_run(rig.plan)
    await rig.tm.advance(60)
    await rig.executor.async_pause()
    await rig.tm.advance(45 * 60)  # pause itself outlasts the tolerance
    await rig.executor.async_resume()

    journal = rig.journal()
    assert journal.state is ExecutorState.IDLE
    outcome = rig.history.finished[0][1]
    assert outcome is RunOutcome.SKIPPED
    results = rig.final_step_results()
    assert SkipReason.MISSED_TOLERANCE.value in results["1"]["reason"]


async def test_short_midrun_pause_resumes_even_past_start_tolerance() -> None:
    """The Aug 18 8 PM signature: a brief mid-run pause must not kill
    the rest of a long program just because the wall clock passed
    requested start + tolerance while the run was legitimately working.
    """
    rig = three_zone_rig()
    await rig.executor.async_start_run(rig.plan)
    # Zone 1 completes on its clock at 14:12; during the gap another
    # zone lights up and pauses the run.
    await rig.tm.advance(12 * 60 + 2)
    await rig.zone_event("back-lawn", True, active={"back-lawn"})
    assert rig.journal().state is ExecutorState.PAUSED_EXTERNAL

    # The external zone clears 19 minutes later — 14:31, past the
    # 14:30 start-tolerance cutoff, but the pause itself was short.
    await rig.tm.advance(19 * 60)
    await rig.zone_event("back-lawn", False, active=set())

    journal = rig.journal()
    assert journal.state is ExecutorState.WATERING
    assert journal.current_step_index == 1
    assert rig.driver.start_calls[-1][0].station_number == 2

    # The rest of the program still completes.
    await rig.tm.advance(2 * 3600)
    assert rig.journal().state is ExecutorState.IDLE
    assert rig.history.finished[0][1] is RunOutcome.COMPLETED


async def test_long_midrun_pause_still_expires() -> None:
    rig = three_zone_rig()
    await rig.executor.async_start_run(rig.plan)
    await rig.tm.advance(12 * 60 + 2)
    await rig.zone_event("back-lawn", True, active={"back-lawn"})
    assert rig.journal().state is ExecutorState.PAUSED_EXTERNAL

    # The controller stays busy for 40 minutes: longer than the
    # tolerance, so the remaining zones are skipped.
    await rig.tm.advance(40 * 60)
    await rig.zone_event("back-lawn", False, active=set())

    journal = rig.journal()
    assert journal.state is ExecutorState.IDLE
    assert rig.history.finished[0][1] is RunOutcome.COMPLETED_WITH_SKIPS
    assert rig.history.finished[0][2] == SkipReason.MISSED_TOLERANCE.value


async def test_pre_start_pause_keeps_start_tolerance_anchor() -> None:
    """A run that never began must still expire against requested start."""
    rig = three_zone_rig()
    rig.journal().last_observation = rig.driver.make_observation(
        {"back-lawn"}
    )
    await rig.executor.async_start_run(rig.plan)
    # Nothing was commanded; the run paused before its first zone.
    assert rig.journal().state is ExecutorState.PAUSED_EXTERNAL
    assert rig.driver.start_calls == []

    # The controller frees up only 35 minutes later: too late to start.
    await rig.tm.advance(35 * 60)
    await rig.zone_event("back-lawn", False, active=set())

    assert rig.journal().state is ExecutorState.IDLE
    assert rig.history.finished[0][1] is RunOutcome.SKIPPED
    assert rig.history.finished[0][2] == SkipReason.MISSED_TOLERANCE.value


async def test_pre_start_rain_delay_skips_run() -> None:
    rig = three_zone_rig()
    rig.journal().last_observation = rig.driver.make_observation(rain_delay=2)
    await rig.executor.async_start_run(rig.plan)

    assert rig.driver.start_calls == []
    assert rig.journal().state is ExecutorState.IDLE
    assert rig.history.finished[0][1] is RunOutcome.SKIPPED
    assert rig.history.finished[0][2] == SkipReason.RAIN_DELAY.value
    assert "run_skipped" in rig.event_types()


async def test_pre_start_wet_sensor_skips_run() -> None:
    rig = three_zone_rig()
    rig.journal().last_observation = rig.driver.make_observation(
        rain_sensor=True
    )
    await rig.executor.async_start_run(rig.plan)
    assert rig.driver.start_calls == []
    assert rig.history.finished[0][2] == SkipReason.RAIN_SENSOR_WET.value


async def test_commanded_clock_ignores_stale_on_state() -> None:
    """A stale 'on' report must not block progression (plan §19)."""
    rig = three_zone_rig()
    await rig.executor.async_start_run(rig.plan)
    # Acceptance observed shortly after start; the switch then never
    # reports again (stale by the time the zone ends).
    await rig.tm.advance(30)
    await rig.zone_event("front-lawn", True)
    stale_time = rig.tm.now
    await rig.tm.advance(12 * 60 - 30)
    # Despite last observation showing the zone on (from 11.5 min ago),
    # the step completed on the commanded clock.
    journal = rig.journal()
    assert journal.step_results[0].status is StepStatus.COMPLETED
    assert journal.last_observation.observed_at_utc == stale_time
    await rig.tm.advance(5)
    assert rig.driver.start_calls[1][0].station_number == 2


async def test_expected_end_uses_commanded_duration() -> None:
    rig = three_zone_rig()
    await rig.executor.async_start_run(rig.plan)
    journal = rig.journal()
    assert journal.current_step_expected_end == START + timedelta(minutes=12)


async def test_shutdown_cancels_armed_timer_and_is_idempotent() -> None:
    rig = three_zone_rig()
    await rig.executor.async_start_run(rig.plan)
    assert rig.tm.pending()  # the expected-end timer is armed

    rig.executor.shutdown()
    rig.executor.shutdown()  # safe to call repeatedly

    assert rig.tm.pending() == []
    # Nothing fires afterwards; the journal is left untouched.
    await rig.tm.advance(15 * 60)
    assert rig.journal().state is ExecutorState.WATERING


async def test_prestart_tolerance_anchors_to_deferred_planned_start() -> None:
    """A run the planner deferred behind another block measures its
    start tolerance from the deferred (planned) start, not from a
    requested time that was never executable."""
    rig = three_zone_rig()
    deferred = rig.plan.steps[0].planned_start_utc + timedelta(hours=4)
    plan = replace(
        rig.plan,
        steps=tuple(
            replace(
                step,
                planned_start_utc=step.planned_start_utc + timedelta(hours=4),
                planned_end_utc=step.planned_end_utc + timedelta(hours=4),
            )
            for step in rig.plan.steps
        ),
    )
    # The controller looks busy at the deferred start: pre-start pause.
    rig.journal().last_observation = rig.driver.make_observation(
        {"ghost-zone"}
    )
    rig.tm.now = deferred
    await rig.executor.async_start_run(plan)
    assert rig.journal().state is ExecutorState.PAUSED_EXTERNAL
    assert rig.driver.start_calls == []

    # It clears 10 minutes later — hours past requested+tolerance, but
    # well inside the DEFERRED start + tolerance: the run must begin.
    await rig.tm.advance(10 * 60)
    await rig.zone_event("ghost-zone", False, active=set())
    assert rig.journal().state is ExecutorState.WATERING
    assert len(rig.driver.start_calls) == 1

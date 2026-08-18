"""Restart recovery reconciliation (plan §28)."""

from __future__ import annotations

from datetime import timedelta

from custom_components.rainbird_scheduler.models import (
    CommandDisposition,
    CommandType,
    ExecutorState,
    PendingCommand,
    RunOutcome,
    SkipReason,
    StepResult,
    StepStatus,
)

from .harness import START, Rig, three_zone_rig


def _prime_active_run(
    rig: Rig,
    state: ExecutorState,
    *,
    step_index: int = 0,
    started_minutes_ago: float | None = None,
) -> None:
    """Put a persisted-looking active run into the journal by hand."""
    journal = rig.journal()
    journal.state = state
    journal.active_occurrence_id = rig.plan.occurrence_id
    journal.run_plan = rig.plan
    journal.current_step_index = step_index
    journal.step_results = {step.index: StepResult() for step in rig.plan.steps}
    step = rig.plan.steps[step_index]
    if started_minutes_ago is not None:
        start = rig.tm.now - timedelta(minutes=started_minutes_ago)
        journal.step_results[step_index].status = StepStatus.RUNNING
        journal.step_results[step_index].attempts = 1
        journal.step_results[step_index].actual_start_utc = start
        journal.current_step_actual_start = start
        journal.current_step_expected_end = start + timedelta(
            minutes=step.duration_minutes
        )
    else:
        journal.pending_command = PendingCommand(
            command_id="persisted",
            command_type=CommandType.START_ZONE,
            zone_id=step.zone_id,
            duration_minutes=step.duration_minutes,
            intended_at_utc=rig.tm.now,
            attempt_number=1,
            disposition=CommandDisposition.SENT,
        )


async def test_watering_zone_active_waits_until_persisted_end() -> None:
    rig = three_zone_rig()
    rig.tm.now = START + timedelta(minutes=5)
    _prime_active_run(rig, ExecutorState.WATERING, started_minutes_ago=5)
    rig.driver.set_observation({"front-lawn"})

    await rig.executor.async_recover()

    # No duplicate command; the executor just waits for the persisted end.
    assert rig.driver.start_calls == []
    assert rig.journal().state is ExecutorState.WATERING
    assert rig.tm.pending() == [START + timedelta(minutes=12)]

    await rig.tm.advance(8 * 60)
    assert rig.journal().step_results[0].status is StepStatus.COMPLETED
    await rig.tm.advance(10)
    # The run continued into zone 2 with exactly one new command.
    assert [call[0].station_number for call in rig.driver.start_calls] == [2]


async def test_watering_idle_recently_past_end_completes_step() -> None:
    rig = three_zone_rig()
    rig.tm.now = START + timedelta(minutes=14)  # end was 12 min mark
    _prime_active_run(rig, ExecutorState.WATERING, started_minutes_ago=14)
    rig.driver.set_observation(set())

    await rig.executor.async_recover()

    journal = rig.journal()
    assert journal.step_results[0].status is StepStatus.COMPLETED
    # Continue with zone 2 after the gap.
    await rig.tm.advance(10)
    assert [call[0].station_number for call in rig.driver.start_calls] == [2]


async def test_watering_idle_long_past_end_skips_remaining() -> None:
    rig = three_zone_rig()
    rig.tm.now = START + timedelta(minutes=90)
    _prime_active_run(rig, ExecutorState.WATERING, started_minutes_ago=90)
    rig.driver.set_observation(set())

    await rig.executor.async_recover()

    journal = rig.journal()
    assert journal.state is ExecutorState.IDLE
    assert rig.history.finished[0][1] is RunOutcome.COMPLETED_WITH_SKIPS
    assert rig.history.finished[0][2] == (
        SkipReason.RESTART_MISSED_TOLERANCE.value
    )
    assert rig.driver.start_calls == []


async def test_starting_zone_active_treats_command_as_accepted() -> None:
    rig = three_zone_rig()
    _prime_active_run(rig, ExecutorState.STARTING)
    rig.tm.now = START + timedelta(minutes=1)
    rig.driver.set_observation({"front-lawn"})

    await rig.executor.async_recover()

    journal = rig.journal()
    assert journal.state is ExecutorState.WATERING
    assert journal.pending_command.disposition is CommandDisposition.ACCEPTED
    # Expected end derives from the persisted command intent.
    assert journal.current_step_expected_end == START + timedelta(minutes=12)
    assert rig.driver.start_calls == []


async def test_starting_idle_within_tolerance_retries() -> None:
    rig = three_zone_rig()
    _prime_active_run(rig, ExecutorState.STARTING)
    rig.tm.now = START + timedelta(minutes=2)
    rig.driver.set_observation(set())

    await rig.executor.async_recover()

    # The command was re-issued (safe: controller observed idle).
    assert [call[0].station_number for call in rig.driver.start_calls] == [1]
    assert rig.journal().state is ExecutorState.WATERING


async def test_starting_idle_past_tolerance_skips() -> None:
    rig = three_zone_rig()
    _prime_active_run(rig, ExecutorState.STARTING)
    rig.tm.now = START + timedelta(minutes=45)
    rig.driver.set_observation(set())

    await rig.executor.async_recover()

    assert rig.driver.start_calls == []
    assert rig.journal().state is ExecutorState.IDLE
    assert rig.history.finished[0][1] is RunOutcome.SKIPPED


async def test_different_zone_active_pauses() -> None:
    rig = three_zone_rig()
    rig.tm.now = START + timedelta(minutes=5)
    _prime_active_run(rig, ExecutorState.WATERING, started_minutes_ago=5)
    rig.driver.set_observation({"back-lawn"})

    await rig.executor.async_recover()

    assert rig.journal().state is ExecutorState.PAUSED_EXTERNAL
    assert rig.driver.start_calls == []


async def test_gap_state_continues_run() -> None:
    rig = three_zone_rig()
    rig.tm.now = START + timedelta(minutes=12, seconds=2)
    _prime_active_run(rig, ExecutorState.INTER_ZONE_GAP)
    journal = rig.journal()
    journal.pending_command = None
    journal.step_results[0].status = StepStatus.COMPLETED
    journal.step_results[0].actual_end_utc = START + timedelta(minutes=12)
    rig.driver.set_observation(set())

    await rig.executor.async_recover()
    await rig.tm.advance(10)

    assert [call[0].station_number for call in rig.driver.start_calls] == [2]


async def test_stopping_state_finishes_abort() -> None:
    rig = three_zone_rig()
    rig.tm.now = START + timedelta(minutes=5)
    _prime_active_run(rig, ExecutorState.STOPPING, started_minutes_ago=5)
    rig.driver.set_observation(set())

    await rig.executor.async_recover()

    assert rig.journal().state is ExecutorState.IDLE
    assert rig.history.finished[0][1] is RunOutcome.ABORTED_USER


async def test_stopping_state_finishes_abort_despite_external_zone() -> None:
    """A persisted stop takes precedence over external-activity pausing."""
    rig = three_zone_rig()
    rig.tm.now = START + timedelta(minutes=5)
    _prime_active_run(rig, ExecutorState.STOPPING, started_minutes_ago=5)
    rig.driver.set_observation({"back-lawn"})

    await rig.executor.async_recover()

    assert rig.journal().state is ExecutorState.IDLE
    assert rig.history.finished[0][1] is RunOutcome.ABORTED_USER


async def test_persisted_stop_request_finishes_abort_despite_external() -> None:
    """A journal persisted mid-async_stop still finishes the abort."""
    rig = three_zone_rig()
    rig.tm.now = START + timedelta(minutes=5)
    _prime_active_run(rig, ExecutorState.WATERING, started_minutes_ago=5)
    rig.journal().stop_requested = True
    rig.driver.set_observation({"back-lawn"})

    await rig.executor.async_recover()

    journal = rig.journal()
    assert journal.state is ExecutorState.IDLE
    assert rig.history.finished[0][1] is RunOutcome.ABORTED_USER
    assert journal.stop_requested is False


async def test_recovered_occurrence_not_run_twice() -> None:
    rig = three_zone_rig()
    rig.tm.now = START + timedelta(minutes=90)
    _prime_active_run(rig, ExecutorState.WATERING, started_minutes_ago=90)
    rig.driver.set_observation(set())
    await rig.executor.async_recover()

    # The occurrence landed in the dedup window during recovery.
    assert rig.plan.occurrence_id in rig.journal().completed_occurrences
    import pytest

    from custom_components.rainbird_scheduler.executor import (
        DuplicateOccurrenceError,
    )

    with pytest.raises(DuplicateOccurrenceError):
        await rig.executor.async_start_run(rig.plan)


async def test_reconciling_end_context_zone_on_before_end_resumes() -> None:
    """Restart mid early-end reconcile with the zone on: resume the step.

    The step is already RUNNING with an actual start, so recovery must
    never re-send the start command (that would re-water the zone).
    """
    rig = three_zone_rig()
    rig.tm.now = START + timedelta(minutes=5)
    _prime_active_run(rig, ExecutorState.RECONCILING, started_minutes_ago=5)
    rig.driver.set_observation({"front-lawn"})

    await rig.executor.async_recover()

    journal = rig.journal()
    assert journal.state is ExecutorState.WATERING
    assert journal.step_results[0].status is StepStatus.RUNNING
    assert rig.driver.start_calls == []
    assert rig.tm.pending() == [START + timedelta(minutes=12)]

    # The step completes on its original commanded clock.
    await rig.tm.advance(8 * 60)
    assert rig.journal().step_results[0].status is StepStatus.COMPLETED


async def test_reconciling_end_context_zone_on_past_end_confirms_overrun() -> None:
    """Restart mid overrun reconcile with the zone still on past its end."""
    rig = three_zone_rig()
    rig.tm.now = START + timedelta(minutes=14)  # end was the 12-minute mark
    _prime_active_run(rig, ExecutorState.RECONCILING, started_minutes_ago=14)
    rig.driver.set_observation({"front-lawn"})

    await rig.executor.async_recover()

    # No re-start: recovery re-enters the overrun confirmation instead.
    assert rig.driver.start_calls == []
    assert rig.journal().state is ExecutorState.RECONCILING

    # Still on at the first confirmation: a second cycle is required.
    await rig.tm.advance(31)
    assert rig.journal().state is ExecutorState.RECONCILING
    assert rig.driver.stop_calls == 0

    # Still on at the second confirmation: stop the controller, fail run.
    await rig.tm.advance(31)
    assert rig.journal().state is ExecutorState.IDLE
    assert rig.driver.stop_calls == 1
    assert rig.history.finished[0][1] is RunOutcome.FAILED
    assert rig.history.finished[0][2] == "controller_overrun"
    results = rig.final_step_results()
    assert results["0"]["status"] == StepStatus.OVERRUN_STOPPED.value


async def test_reconciling_end_context_zone_off_past_end_completes() -> None:
    """Restart mid overrun reconcile with the zone off: it stopped alone."""
    rig = three_zone_rig()
    rig.tm.now = START + timedelta(minutes=14)
    _prime_active_run(rig, ExecutorState.RECONCILING, started_minutes_ago=14)
    rig.driver.set_observation(set())

    await rig.executor.async_recover()

    journal = rig.journal()
    assert journal.step_results[0].status is StepStatus.COMPLETED
    assert rig.driver.start_calls == []
    # The run advances to the next zone after the gap.
    await rig.tm.advance(10)
    assert [call[0].station_number for call in rig.driver.start_calls] == [2]


async def test_reconciling_end_context_zone_off_past_tolerance_expires() -> None:
    """Restart long after the end with the zone off: expire, don't re-run."""
    rig = three_zone_rig()
    rig.tm.now = START + timedelta(minutes=90)
    _prime_active_run(rig, ExecutorState.RECONCILING, started_minutes_ago=90)
    rig.driver.set_observation(set())

    await rig.executor.async_recover()

    assert rig.driver.start_calls == []
    assert rig.journal().state is ExecutorState.IDLE
    assert rig.history.finished[0][1] is RunOutcome.COMPLETED_WITH_SKIPS
    assert rig.history.finished[0][2] == (
        SkipReason.RESTART_MISSED_TOLERANCE.value
    )


async def test_reconciling_end_context_zone_off_before_end_classifies() -> None:
    """Restart mid early-end reconcile with the zone off: classify the stop."""
    rig = three_zone_rig()
    rig.tm.now = START + timedelta(minutes=5)
    _prime_active_run(rig, ExecutorState.RECONCILING, started_minutes_ago=5)
    rig.driver.set_observation(set())

    await rig.executor.async_recover()

    assert rig.driver.start_calls == []
    assert rig.journal().state is ExecutorState.IDLE
    assert rig.history.finished[0][1] is RunOutcome.ABORTED_EXTERNAL
    results = rig.final_step_results()
    assert results["0"]["status"] == StepStatus.EXTERNAL_STOP.value


async def test_reconciling_start_context_idle_still_retries() -> None:
    """A start-context reconcile (step still PENDING) keeps the old path."""
    rig = three_zone_rig()
    _prime_active_run(rig, ExecutorState.RECONCILING)
    rig.tm.now = START + timedelta(minutes=2)
    rig.driver.set_observation(set())

    await rig.executor.async_recover()

    # The controller was observed idle: re-issuing the start is safe.
    assert [call[0].station_number for call in rig.driver.start_calls] == [1]
    assert rig.journal().state is ExecutorState.WATERING

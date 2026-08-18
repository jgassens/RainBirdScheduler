"""Uncertain command reconciliation (plan §12, §21)."""

from __future__ import annotations

from custom_components.rainbird_scheduler.models import (
    CommandDisposition,
    ExecutorState,
    RunOutcome,
    StepStatus,
)

from .harness import CommandUncertainError, DriverError, three_zone_rig


async def test_uncertain_then_observed_active_is_accepted_not_resent() -> None:
    rig = three_zone_rig()
    rig.driver.start_errors = [CommandUncertainError("timeout")]
    await rig.executor.async_start_run(rig.plan)

    journal = rig.journal()
    assert journal.state is ExecutorState.RECONCILING
    assert journal.pending_command.disposition is CommandDisposition.UNCERTAIN
    assert journal.uncertain_count == 1
    assert rig.driver.refresh_calls >= 1

    # The controller actually accepted: the zone shows active.
    rig.driver.set_observation({"front-lawn"})
    await rig.tm.advance(45)  # start_observation_timeout

    journal = rig.journal()
    assert journal.state is ExecutorState.WATERING
    assert journal.pending_command.disposition is CommandDisposition.ACCEPTED
    # CRITICAL: the command was not blindly repeated.
    assert len(rig.driver.start_calls) == 1

    # The run continues to the next zones on the commanded clock.
    await rig.tm.advance(2 * 3600)
    assert rig.history.finished[0][1] is RunOutcome.COMPLETED
    assert len(rig.driver.start_calls) == 3


async def test_uncertain_then_idle_retries_with_backoff() -> None:
    rig = three_zone_rig()
    rig.driver.start_errors = [CommandUncertainError("timeout"), None]
    await rig.executor.async_start_run(rig.plan)
    assert rig.journal().state is ExecutorState.RECONCILING

    rig.driver.set_observation(set())  # idle: eligible for retry
    await rig.tm.advance(45)
    # First retry is armed 2 seconds out.
    assert rig.journal().state is ExecutorState.RECONCILING
    await rig.tm.advance(2)

    journal = rig.journal()
    assert journal.state is ExecutorState.WATERING
    assert len(rig.driver.start_calls) == 2
    assert journal.retry_count == 1
    assert journal.step_results[0].attempts == 2


async def test_attempts_exhausted_fails_run_with_reason() -> None:
    rig = three_zone_rig()
    rig.driver.start_errors = [CommandUncertainError("boom")] * 10
    await rig.executor.async_start_run(rig.plan)
    rig.driver.set_observation(set())

    await rig.tm.advance(3600)  # enough for all reconcile+retry cycles

    journal = rig.journal()
    assert journal.state is ExecutorState.IDLE
    assert rig.history.finished[0][1] is RunOutcome.FAILED
    assert rig.history.finished[0][2] == "command_failed"
    assert any(
        kind == "command_failed" for kind, _ in rig.history.interventions
    )
    # MAX_COMMAND_ATTEMPTS total sends, never more.
    assert len(rig.driver.start_calls) == 5
    results = rig.final_step_results()
    assert results["0"]["status"] == StepStatus.FAILED.value


async def test_no_retry_past_latest_permissible_start() -> None:
    rig = three_zone_rig(missed_run_tolerance_minutes=0)
    rig.driver.start_errors = [CommandUncertainError("timeout")] * 10
    await rig.executor.async_start_run(rig.plan)
    rig.driver.set_observation(set())

    # Classification happens 45s after the request; the zero-minute
    # tolerance is already exceeded, so no retry may be attempted.
    await rig.tm.advance(45)
    journal = rig.journal()
    assert journal.state is ExecutorState.IDLE
    assert rig.history.finished[0][1] is RunOutcome.FAILED
    assert len(rig.driver.start_calls) == 1


async def test_zone_on_event_during_reconciliation_accepts_command() -> None:
    rig = three_zone_rig()
    rig.driver.start_errors = [CommandUncertainError("timeout")]
    await rig.executor.async_start_run(rig.plan)
    assert rig.journal().state is ExecutorState.RECONCILING

    await rig.tm.advance(5)
    await rig.zone_event("front-lawn", True)

    journal = rig.journal()
    assert journal.state is ExecutorState.WATERING
    assert journal.pending_command.disposition is CommandDisposition.ACCEPTED
    assert len(rig.driver.start_calls) == 1


async def test_uncertain_with_different_zone_active_is_external() -> None:
    rig = three_zone_rig()
    rig.driver.start_errors = [CommandUncertainError("timeout")]
    await rig.executor.async_start_run(rig.plan)

    rig.driver.set_observation({"back-lawn"})
    await rig.tm.advance(45)

    # Default interruption policy pauses rather than fighting for control.
    assert rig.journal().state is ExecutorState.PAUSED_EXTERNAL
    assert len(rig.driver.start_calls) == 1


async def test_pause_during_start_reconcile_sends_best_effort_stop() -> None:
    """The uncertain start may have been accepted: stop before pausing."""
    rig = three_zone_rig()
    rig.driver.start_errors = [CommandUncertainError("timeout")]
    await rig.executor.async_start_run(rig.plan)
    assert rig.journal().state is ExecutorState.RECONCILING

    await rig.executor.async_pause()

    assert rig.driver.stop_calls == 1
    journal = rig.journal()
    assert journal.state is ExecutorState.PAUSED_EXTERNAL
    assert journal.step_results[0].status is StepStatus.PENDING


async def test_skip_during_start_reconcile_sends_best_effort_stop() -> None:
    """The uncertain start may have been accepted: stop before skipping."""
    rig = three_zone_rig()
    rig.driver.start_errors = [CommandUncertainError("timeout")]
    await rig.executor.async_start_run(rig.plan)
    assert rig.journal().state is ExecutorState.RECONCILING

    await rig.executor.async_skip_current()

    assert rig.driver.stop_calls == 1
    journal = rig.journal()
    assert journal.step_results[0].status is StepStatus.SKIPPED
    assert journal.state is ExecutorState.INTER_ZONE_GAP
    await rig.tm.advance(5)
    assert rig.driver.start_calls[-1][0].station_number == 2


async def test_pause_during_start_reconcile_failed_stop_does_not_block() -> None:
    """An uncertain best-effort stop must not block the pause itself."""
    rig = three_zone_rig()
    rig.driver.start_errors = [CommandUncertainError("timeout")]
    rig.driver.stop_error = DriverError("nope")
    await rig.executor.async_start_run(rig.plan)

    await rig.executor.async_pause()

    journal = rig.journal()
    assert journal.state is ExecutorState.PAUSED_EXTERNAL
    assert journal.pending_command.disposition is CommandDisposition.UNCERTAIN


async def test_zone_on_event_during_reconciliation_emits_zone_started() -> None:
    rig = three_zone_rig()
    rig.driver.start_errors = [CommandUncertainError("timeout")]
    await rig.executor.async_start_run(rig.plan)
    assert rig.journal().state is ExecutorState.RECONCILING

    await rig.tm.advance(5)
    await rig.zone_event("front-lawn", True)

    assert rig.journal().state is ExecutorState.WATERING
    assert rig.event_types() == ["run_started", "zone_started"]

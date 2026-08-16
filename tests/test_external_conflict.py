"""External activity, app conflicts, and overrun handling (plan §20, §27)."""

from __future__ import annotations

from custom_components.rainbird_scheduler.models import (
    ExecutorState,
    InterruptionPolicy,
    RunOutcome,
    StepStatus,
)

from .harness import three_zone_rig


async def test_external_zone_pauses_run_by_default() -> None:
    rig = three_zone_rig()
    await rig.executor.async_start_run(rig.plan)
    await rig.tm.advance(3 * 60)

    # A different Rain Bird zone activates (app or physical controller).
    await rig.zone_event(
        "back-lawn", True, active={"front-lawn", "back-lawn"}
    )

    journal = rig.journal()
    assert journal.state is ExecutorState.PAUSED_EXTERNAL
    assert journal.step_results[0].status is StepStatus.EXTERNAL_STOP
    assert "run_interrupted" in rig.event_types()
    # The scheduler never fights for the controller.
    assert len(rig.driver.start_calls) == 1
    assert rig.driver.stop_calls == 0


async def test_external_conflict_auto_resumes_when_idle() -> None:
    rig = three_zone_rig()
    await rig.executor.async_start_run(rig.plan)
    await rig.tm.advance(3 * 60)
    await rig.zone_event("back-lawn", True, active={"back-lawn"})
    assert rig.journal().state is ExecutorState.PAUSED_EXTERNAL

    await rig.tm.advance(4 * 60)
    await rig.zone_event("back-lawn", False, active=set())

    # Resumed with the next pending step (still within tolerance).
    assert rig.journal().state is ExecutorState.WATERING
    assert rig.driver.start_calls[-1][0].station_number == 2


async def test_external_zone_abort_policy() -> None:
    rig = three_zone_rig()
    rig.programs["morning-lawn"].external_interruption_policy = (
        InterruptionPolicy.ABORT
    )
    await rig.executor.async_start_run(rig.plan)
    await rig.tm.advance(3 * 60)
    await rig.zone_event("back-lawn", True, active={"back-lawn"})

    assert rig.journal().state is ExecutorState.IDLE
    assert rig.history.finished[0][1] is RunOutcome.ABORTED_EXTERNAL


async def test_external_stop_aborts_run() -> None:
    rig = three_zone_rig()
    await rig.executor.async_start_run(rig.plan)
    await rig.tm.advance(5 * 60)

    # Zone off early: no rain, no stop request, controller healthy.
    await rig.zone_event("front-lawn", False)

    journal = rig.journal()
    assert journal.state is ExecutorState.IDLE
    assert rig.history.finished[0][1] is RunOutcome.ABORTED_EXTERNAL
    assert rig.history.finished[0][2] == "external_stop"
    results = rig.final_step_results()
    assert results["0"]["status"] == StepStatus.EXTERNAL_STOP.value


async def test_confirmed_overrun_stops_controller() -> None:
    rig = three_zone_rig()
    await rig.executor.async_start_run(rig.plan)
    await rig.tm.advance(12 * 60)  # commanded end reached, in gap
    assert rig.journal().state is ExecutorState.INTER_ZONE_GAP

    # Fresh evidence after the expected end: the zone is still on.
    await rig.tm.advance(1)
    await rig.zone_event("front-lawn", True)
    journal = rig.journal()
    assert journal.state is ExecutorState.RECONCILING
    refreshes = rig.driver.refresh_calls
    assert refreshes >= 1

    # Evidence persists through the confirmation window.
    await rig.tm.advance(20)
    await rig.zone_event("front-lawn", True)
    await rig.tm.advance(10)

    journal = rig.journal()
    assert journal.state is ExecutorState.IDLE
    assert rig.driver.stop_calls == 1
    assert rig.history.finished[0][1] is RunOutcome.FAILED
    assert rig.history.finished[0][2] == "controller_overrun"
    assert "controller_overrun" in rig.event_types()
    assert any(
        kind == "controller_overrun" for kind, _ in rig.history.interventions
    )
    results = rig.final_step_results()
    assert results["0"]["status"] == StepStatus.OVERRUN_STOPPED.value


async def test_overrun_suspicion_cleared_by_off_event() -> None:
    rig = three_zone_rig()
    await rig.executor.async_start_run(rig.plan)
    await rig.tm.advance(12 * 60)
    await rig.tm.advance(1)
    await rig.zone_event("front-lawn", True)
    assert rig.journal().state is ExecutorState.RECONCILING

    # The zone goes off before confirmation: no overrun, run continues.
    await rig.tm.advance(5)
    await rig.zone_event("front-lawn", False)
    await rig.tm.advance(30)

    assert rig.driver.stop_calls == 0
    assert rig.journal().state is ExecutorState.WATERING
    assert rig.driver.start_calls[-1][0].station_number == 2


async def test_stale_on_state_never_triggers_overrun() -> None:
    rig = three_zone_rig()
    await rig.executor.async_start_run(rig.plan)
    # Acceptance evidence arrives mid-run and then goes silent.
    await rig.tm.advance(60)
    await rig.zone_event("front-lawn", True)
    # The stale "on" from 11 minutes ago is not contradictory evidence.
    await rig.tm.advance(11 * 60 + 10)
    assert rig.journal().step_results[0].status is StepStatus.COMPLETED
    assert rig.journal().state in (
        ExecutorState.INTER_ZONE_GAP,
        ExecutorState.WATERING,
    )

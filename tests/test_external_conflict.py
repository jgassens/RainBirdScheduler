"""External activity, app conflicts, and overrun handling (plan §20, §27)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from custom_components.rainbird_scheduler.models import (
    ExecutorState,
    InterruptionPolicy,
    RunOutcome,
    StepStatus,
)

from .harness import DriverError, build_rig, three_zone_rig
from .helpers import make_program, make_zone


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

    # A single early "off" is suspicion, not proof: confirmation first.
    assert rig.journal().state is ExecutorState.RECONCILING
    assert rig.driver.refresh_calls >= 1

    # The refreshed source still shows the zone off: genuine stop.
    await rig.tm.advance(31)

    journal = rig.journal()
    assert journal.state is ExecutorState.IDLE
    assert rig.history.finished[0][1] is RunOutcome.ABORTED_EXTERNAL
    assert rig.history.finished[0][2] == "external_stop"
    results = rig.final_step_results()
    assert results["0"]["status"] == StepStatus.EXTERNAL_STOP.value


async def test_stale_early_off_does_not_abort_run() -> None:
    """A poll carrying pre-start data must not kill the run (Aug 18 4 AM).

    The source polls a slow controller: an observation in flight when the
    zone was commanded can land after WATERING began, showing the zone
    off. The refresh requested by the reconcile then shows the truth.
    """
    rig = three_zone_rig()
    await rig.executor.async_start_run(rig.plan)

    # 26 seconds in (the Aug 17 06:05:06 -> 06:05:32 signature), a stale
    # observation reports our just-started zone off.
    await rig.tm.advance(26)
    await rig.zone_event("front-lawn", False)
    assert rig.journal().state is ExecutorState.RECONCILING

    # The requested refresh lands: the zone is actually on.
    await rig.tm.advance(5)
    await rig.zone_event("front-lawn", True)

    # Back to watering the same step; nothing was aborted or stopped.
    journal = rig.journal()
    assert journal.state is ExecutorState.WATERING
    assert journal.step_results[0].status is StepStatus.RUNNING
    assert rig.driver.stop_calls == 0
    assert not rig.history.finished

    # The step still completes on its commanded clock.
    await rig.tm.advance(12 * 60)
    assert rig.journal().step_results[0].status is StepStatus.COMPLETED


async def test_stale_early_off_resolved_by_confirmation_pull() -> None:
    """Same phantom, but no fresh event arrives: the confirm's live read
    of the source shows the zone on and the step resumes."""
    rig = three_zone_rig()
    await rig.executor.async_start_run(rig.plan)

    await rig.tm.advance(26)
    await rig.zone_event("front-lawn", False)
    assert rig.journal().state is ExecutorState.RECONCILING

    # No change event fires, but the source itself now reports the zone
    # on when the confirmation reads it.
    rig.driver.set_observation({"front-lawn"})
    await rig.tm.advance(31)

    journal = rig.journal()
    assert journal.state is ExecutorState.WATERING
    assert journal.step_results[0].status is StepStatus.RUNNING
    assert not rig.history.finished


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

    # Evidence persists through BOTH confirmation cycles: the zone is
    # still on at each live read, ~30s apart, each after a refresh.
    await rig.tm.advance(20)
    await rig.zone_event("front-lawn", True)
    await rig.tm.advance(10)
    assert rig.journal().state is ExecutorState.RECONCILING
    assert rig.driver.stop_calls == 0
    await rig.tm.advance(31)

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


async def test_transient_post_end_on_does_not_fail_run() -> None:
    """Controller lag past the expected end is benign (Aug 18 7:00:32).

    Our expected end starts at command send; the controller's own timer
    starts at command processing, so it legitimately stops a few seconds
    later. Evidence from that window must not stop the controller.
    """
    rig = three_zone_rig()
    await rig.executor.async_start_run(rig.plan)
    await rig.tm.advance(12 * 60)  # commanded end reached, in gap
    await rig.tm.advance(1)

    # The controller is caught mid-stop just after the expected end.
    await rig.zone_event("front-lawn", True)
    assert rig.journal().state is ExecutorState.RECONCILING

    # By the first confirmation's live read the zone reports off.
    rig.driver.set_observation(set())
    await rig.tm.advance(31)

    assert rig.driver.stop_calls == 0
    assert not any(
        kind == "controller_overrun" for kind, _ in rig.history.interventions
    )
    # The run moved on to the next zone rather than failing.
    journal = rig.journal()
    assert journal.state in (
        ExecutorState.WATERING,
        ExecutorState.INTER_ZONE_GAP,
    )
    assert journal.step_results[0].status is StepStatus.COMPLETED


async def test_overrun_cleared_at_second_confirmation() -> None:
    """Still-on at the first read but off by the second: no failure."""
    rig = three_zone_rig()
    await rig.executor.async_start_run(rig.plan)
    await rig.tm.advance(12 * 60)
    await rig.tm.advance(1)
    await rig.zone_event("front-lawn", True)
    assert rig.journal().state is ExecutorState.RECONCILING

    # First confirmation still sees it on -> second cycle armed.
    await rig.tm.advance(31)
    assert rig.journal().state is ExecutorState.RECONCILING
    assert rig.driver.stop_calls == 0

    # Second confirmation's live read shows it off.
    rig.driver.set_observation(set())
    await rig.tm.advance(31)

    assert rig.driver.stop_calls == 0
    journal = rig.journal()
    assert journal.step_results[0].status is StepStatus.COMPLETED
    assert not rig.history.finished or (
        rig.history.finished[0][1] is not RunOutcome.FAILED
    )


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


async def test_stale_on_for_finished_zone_does_not_conflict() -> None:
    """A late poll caching the just-finished zone 'on' is not a conflict.

    The WATERING event path applies the same staleness discount as
    _external_zones: evidence observed at/before that zone's actual end
    explains nothing about external activity.
    """
    rig = three_zone_rig()
    await rig.executor.async_start_run(rig.plan)
    await rig.tm.advance(12 * 60)  # zone 1 completed
    await rig.tm.advance(5)  # gap elapsed; zone 2 watering
    journal = rig.journal()
    assert journal.state is ExecutorState.WATERING
    assert journal.current_step_index == 1
    end_of_zone_1 = journal.step_results[0].actual_end_utc

    # A late poll surfaces a cached "on" for zone 1, observed at its end.
    stale = rig.driver.make_observation({"front-lawn"}, observed_at=end_of_zone_1)
    await rig.executor.async_handle_zone_state("front-lawn", True, stale)

    assert rig.journal().state is ExecutorState.WATERING
    assert not rig.history.finished

    # A genuinely fresh "on" for that zone is still a conflict.
    await rig.zone_event("front-lawn", True, active={"front-lawn", "side-lawn"})
    assert rig.journal().state is ExecutorState.PAUSED_EXTERNAL


def _soaked_garden_rig():
    """One zone, two 5-minute cycles separated by a 60-minute soak —
    the live 'Garden' program that failed every morning."""
    zones = [
        make_zone(
            "garden",
            1,
            base_runtime_minutes=Decimal(10),
            max_cycle_minutes=5,
            minimum_soak_minutes=60,
        )
    ]
    program = make_program("garden-program", ["garden"])
    return build_rig(zones, program)


async def test_same_zone_relit_deep_in_soak_is_external_not_overrun() -> None:
    """The Aug 18/19 07:00 AM signature.

    Cycle 1 waters 06:00-06:05 and completes. During the hour-long soak
    before cycle 2, the controller's own 7:00 program lights the same
    zone. That is a fresh external start, not our command overrunning —
    the run must pause for the conflict instead of stopping the
    controller and failing.
    """
    rig = _soaked_garden_rig()
    await rig.executor.async_start_run(rig.plan)
    await rig.tm.advance(5 * 60)  # cycle 1 completes on its clock
    journal = rig.journal()
    assert journal.step_results[0].status is StepStatus.COMPLETED
    assert journal.state is ExecutorState.INTER_ZONE_GAP

    # 55 minutes into the soak, the zone reports on again (native program).
    await rig.tm.advance(55 * 60)
    await rig.zone_event("garden", True)

    journal = rig.journal()
    assert journal.state is ExecutorState.PAUSED_EXTERNAL
    # Cycle 1's completed record is untouched; nothing was stopped.
    assert journal.step_results[0].status is StepStatus.COMPLETED
    assert rig.driver.stop_calls == 0
    assert not rig.history.finished
    assert not any(
        kind == "controller_overrun" for kind, _ in rig.history.interventions
    )
    assert any(
        kind == "external_activity" for kind, _ in rig.history.interventions
    )

    # The native program finishes 10 minutes later; the run resumes and
    # cycle 2 still waters (pause was well within tolerance).
    await rig.tm.advance(10 * 60)
    await rig.zone_event("garden", False, active=set())
    journal = rig.journal()
    assert journal.state is ExecutorState.WATERING
    assert journal.current_step_index == 1
    assert len(rig.driver.start_calls) == 2

    await rig.tm.advance(2 * 3600)
    assert rig.journal().state is ExecutorState.IDLE
    assert rig.history.finished[0][1] is RunOutcome.COMPLETED


async def test_same_zone_on_just_after_step_end_is_still_overrun_path() -> None:
    """Close to the commanded end, the overrun reconcile still owns it."""
    rig = _soaked_garden_rig()
    await rig.executor.async_start_run(rig.plan)
    await rig.tm.advance(5 * 60)
    assert rig.journal().state is ExecutorState.INTER_ZONE_GAP

    # 30 seconds after the end: plausibly our own command still running.
    await rig.tm.advance(30)
    await rig.zone_event("garden", True)
    assert rig.journal().state is ExecutorState.RECONCILING

    # Confirmed on at both fresh reads: genuine overrun, controller stopped.
    await rig.tm.advance(31)
    await rig.zone_event("garden", True)
    await rig.tm.advance(31)
    assert rig.driver.stop_calls == 1
    assert rig.history.finished[0][1] is RunOutcome.FAILED


async def test_overrun_confirm_ignores_stale_cached_on() -> None:
    """A confirm read carrying a pre-end cached "on" must not fail the run.

    The Aug 18/19 6 AM signature: the refresh requested by the reconcile
    never lands (slow LNK poll), so both confirmation reads serve the same
    cached switch state from before the commanded end. That is an echo of
    our own watering, not proof the valve is still open — the controller
    stops the zone on its own timer.
    """
    rig = three_zone_rig()
    await rig.executor.async_start_run(rig.plan)
    await rig.tm.advance(12 * 60)
    expected_end = rig.tm.now
    await rig.tm.advance(1)

    # A fresh post-end "on" raises the suspicion (this part is genuine).
    await rig.zone_event("front-lawn", True)
    assert rig.journal().state is ExecutorState.RECONCILING

    # But the confirm's live read returns state last confirmed BEFORE the
    # commanded end: the poll never refreshed.
    stale_report = expected_end - timedelta(seconds=20)
    rig.driver.observation = rig.driver.make_observation(
        {"front-lawn"},
        zone_reported_at={"front-lawn": stale_report},
    )
    await rig.tm.advance(31)

    journal = rig.journal()
    assert rig.driver.stop_calls == 0
    assert journal.step_results[0].status is StepStatus.COMPLETED
    assert not any(
        kind == "controller_overrun" for kind, _ in rig.history.interventions
    )
    # The run moved on instead of failing.
    assert journal.state in (
        ExecutorState.WATERING,
        ExecutorState.INTER_ZONE_GAP,
    )


async def test_snapshot_built_now_with_stale_zone_state_no_phantom_pause() -> None:
    """The 8 PM 'completed_with_skips' signature, first half.

    An unrelated event (temperature, rain sensor) during the inter-zone
    gap builds a fresh snapshot that still carries the just-finished
    zone's stale "on" (the source has not re-polled). The snapshot's
    build time must not lend that state freshness: the next step starts.
    """
    rig = three_zone_rig()
    await rig.executor.async_start_run(rig.plan)
    await rig.tm.advance(12 * 60)  # zone 1 completes; gap armed
    journal = rig.journal()
    assert journal.state is ExecutorState.INTER_ZONE_GAP
    end_of_zone_1 = journal.step_results[0].actual_end_utc
    assert end_of_zone_1 is not None

    # Temperature event 2s into the gap: snapshot built "now", zone 1
    # still cached on from before its end.
    await rig.tm.advance(2)
    stale = rig.driver.make_observation(
        {"front-lawn"},
        zone_reported_at={
            "front-lawn": end_of_zone_1 - timedelta(seconds=30)
        },
    )
    rig.driver.observation = stale
    await rig.executor.async_handle_weather_state(stale)

    # Gap elapses: zone 2 starts instead of pausing for phantom activity.
    await rig.tm.advance(5)
    journal = rig.journal()
    assert journal.state is ExecutorState.WATERING
    assert journal.current_step_index == 1
    assert rig.driver.start_calls[-1][0].station_number == 2
    assert not any(
        kind == "external_activity" for kind, _ in rig.history.interventions
    )


async def test_fresh_external_zone_at_step_start_pauses_and_logs() -> None:
    """Genuinely fresh external activity at a step boundary still pauses —
    and now leaves a Failures & interventions record naming the zones."""
    rig = three_zone_rig()
    await rig.executor.async_start_run(rig.plan)
    await rig.tm.advance(12 * 60)
    await rig.tm.advance(2)

    # The controller's own program lights up another zone, freshly.
    fresh = rig.driver.make_observation({"back-lawn"})
    rig.driver.observation = fresh
    await rig.executor.async_handle_weather_state(fresh)

    await rig.tm.advance(5)
    assert rig.journal().state is ExecutorState.PAUSED_EXTERNAL
    assert any(
        kind == "external_activity" and "Back Lawn" in message
        for kind, message in rig.history.interventions
    )


async def test_overrun_confirm_survives_observe_failure() -> None:
    """A failing observe must not wedge the run in RECONCILING."""
    rig = three_zone_rig()
    await rig.executor.async_start_run(rig.plan)
    await rig.tm.advance(12 * 60)  # commanded end reached, in gap
    await rig.tm.advance(1)
    await rig.zone_event("front-lawn", True)
    assert rig.journal().state is ExecutorState.RECONCILING

    async def _failing_observe():
        raise DriverError("read failed")

    rig.driver.async_observe = _failing_observe
    # First confirmation falls back to the last observation (zone on) and
    # arms the second cycle instead of raising out of the timer callback.
    await rig.tm.advance(31)
    assert rig.journal().state is ExecutorState.RECONCILING
    await rig.tm.advance(31)

    assert rig.journal().state is ExecutorState.IDLE
    assert rig.driver.stop_calls == 1
    assert rig.history.finished[0][1] is RunOutcome.FAILED
    assert rig.history.finished[0][2] == "controller_overrun"


async def test_early_end_confirm_survives_observe_failure() -> None:
    """A failing observe still classifies the early end, no wedge."""
    rig = three_zone_rig()
    await rig.executor.async_start_run(rig.plan)
    await rig.tm.advance(5 * 60)
    await rig.zone_event("front-lawn", False)
    assert rig.journal().state is ExecutorState.RECONCILING

    async def _failing_observe():
        raise DriverError("read failed")

    rig.driver.async_observe = _failing_observe
    await rig.tm.advance(31)

    # The fallback is the last observation (the early "off"), so the stop
    # is classified exactly as if the confirmation read had seen it.
    assert rig.journal().state is ExecutorState.IDLE
    assert rig.history.finished[0][1] is RunOutcome.ABORTED_EXTERNAL

"""Rain-sensor cuts and early-end classification (plan §22, §23)."""

from __future__ import annotations

from custom_components.rainbird_scheduler.models import (
    ExecutorState,
    RunOutcome,
    SensorCutBehavior,
    SkipReason,
    StepStatus,
)

from .harness import build_rig, three_zone_rig
from .helpers import make_program, make_zone


async def test_sensor_cut_aborts_run_by_default() -> None:
    rig = three_zone_rig()
    await rig.executor.async_start_run(rig.plan)
    await rig.tm.advance(5 * 60)

    # The controller cut the zone; the rain sensor is wet.
    await rig.zone_event("front-lawn", False, rain_sensor=True)

    journal = rig.journal()
    assert journal.state is ExecutorState.IDLE
    assert rig.history.finished[0][1] is RunOutcome.ABORTED_SENSOR
    assert "sensor_cut" in rig.event_types()
    results = rig.final_step_results()
    assert results["0"]["status"] == StepStatus.SENSOR_CUT.value
    assert results["1"]["status"] == StepStatus.SKIPPED.value
    assert SkipReason.RAIN_SENSOR_WET.value in results["1"]["reason"]
    # No automatic restart: the executor sent nothing further.
    assert len(rig.driver.start_calls) == 1
    assert rig.driver.stop_calls == 0


async def test_sensor_cut_pause_until_dry_resumes() -> None:
    from decimal import Decimal

    zones = [
        make_zone("a", 1, base_runtime_minutes=Decimal(10)),
        make_zone("b", 2, base_runtime_minutes=Decimal(10)),
    ]
    program = make_program("p", ["a", "b"])
    program.rain_policy.sensor_cut_behavior = SensorCutBehavior.PAUSE_UNTIL_DRY
    rig = build_rig(zones, program)

    await rig.executor.async_start_run(rig.plan)
    await rig.tm.advance(3 * 60)
    await rig.zone_event("a", False, rain_sensor=True)
    assert rig.journal().state is ExecutorState.PAUSED_SENSOR

    # Sensor dries within the tolerance: the remaining zone runs.
    await rig.tm.advance(5 * 60)
    await rig.executor.async_handle_sensor_state(
        rig.driver.make_observation(set(), rain_sensor=False)
    )
    assert rig.journal().state is ExecutorState.WATERING
    assert rig.driver.start_calls[-1][0].station_number == 2


async def test_sensor_wet_between_steps_stops_remaining() -> None:
    rig = three_zone_rig()
    await rig.executor.async_start_run(rig.plan)
    await rig.tm.advance(12 * 60)  # zone 1 completed, in gap
    assert rig.journal().state is ExecutorState.INTER_ZONE_GAP

    await rig.executor.async_handle_sensor_state(
        rig.driver.make_observation(set(), rain_sensor=True)
    )
    await rig.tm.advance(5)  # gap elapses; pre-step check sees the wet sensor

    journal = rig.journal()
    assert journal.state is ExecutorState.IDLE
    assert rig.history.finished[0][1] is RunOutcome.ABORTED_SENSOR
    assert len(rig.driver.start_calls) == 1


async def test_power_loss_classification() -> None:
    rig = three_zone_rig()
    await rig.executor.async_start_run(rig.plan)
    await rig.tm.advance(4 * 60)

    await rig.zone_event(
        "front-lawn", False, active=set(), available=False
    )

    assert rig.history.finished[0][1] is RunOutcome.ABORTED_POWER_LOSS
    results = rig.final_step_results()
    assert results["0"]["reason"] == "controller_power_loss"

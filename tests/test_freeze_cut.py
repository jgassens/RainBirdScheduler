"""Software freeze cut, hysteresis resume, and cross-condition blocking.

Mirrors test_sensor_cut.py, but the freeze guard is a software check on a
temperature source: a mid-run cut must issue an explicit stop (the valve is
still open), unlike a rain cut where the hardware already closed it.
"""

from __future__ import annotations

from decimal import Decimal

from custom_components.rainbird_scheduler.models import (
    CommandDisposition,
    ExecutorState,
    FreezeGuardConfig,
    FreezeUnavailablePolicy,
    RunOutcome,
    SensorCutBehavior,
    SkipReason,
    StepStatus,
)

from .harness import DriverError, build_rig
from .helpers import make_controller, make_program, make_zone


def freeze_rig(
    behavior: SensorCutBehavior = SensorCutBehavior.ABORT_RUN,
    *,
    threshold: Decimal = Decimal(1),
    when_unavailable: FreezeUnavailablePolicy | None = None,
):
    zones = [
        make_zone("a", 1, base_runtime_minutes=Decimal(10)),
        make_zone("b", 2, base_runtime_minutes=Decimal(10)),
    ]
    program = make_program("p", ["a", "b"])
    program.freeze_policy.freeze_cut_behavior = behavior
    guard = FreezeGuardConfig(
        enabled=True, temperature_entity_id="sensor.t", threshold=threshold
    )
    if when_unavailable is not None:
        guard.when_unavailable = when_unavailable
    return build_rig(zones, program, controller=make_controller(freeze_guard=guard))


async def test_freeze_cut_aborts_and_sends_stop() -> None:
    rig = freeze_rig()
    await rig.executor.async_start_run(rig.plan)
    await rig.tm.advance(3 * 60)

    # Temperature drops below threshold while zone a is watering.
    await rig.weather_event(temperature_c=Decimal("-2"))

    journal = rig.journal()
    assert journal.state is ExecutorState.IDLE
    assert rig.history.finished[0][1] is RunOutcome.ABORTED_SENSOR
    # The key difference from a rain cut: we actively stopped the valve.
    assert rig.driver.stop_calls == 1
    results = rig.final_step_results()
    assert results["0"]["status"] == StepStatus.SENSOR_CUT.value
    assert results["0"]["reason"] == "low_temperature"
    assert results["1"]["status"] == StepStatus.SKIPPED.value
    assert SkipReason.LOW_TEMPERATURE.value in results["1"]["reason"]
    # The cut is labeled as a freeze for the lifecycle event.
    kinds = [data.get("kind") for name, data in rig.events if name == "sensor_cut"]
    assert kinds == ["freeze"]


async def test_freeze_pause_resumes_only_past_hysteresis() -> None:
    rig = freeze_rig(SensorCutBehavior.PAUSE_UNTIL_DRY)
    await rig.executor.async_start_run(rig.plan)
    await rig.tm.advance(3 * 60)

    await rig.weather_event(temperature_c=Decimal(0))
    assert rig.journal().state is ExecutorState.PAUSED_SENSOR
    assert rig.journal().paused_reason == "low_temperature"

    # Threshold 1 °C + 1 °C hysteresis = 2 °C required to resume.
    await rig.tm.advance(60)
    await rig.weather_event(temperature_c=Decimal("1.5"))
    assert rig.journal().state is ExecutorState.PAUSED_SENSOR

    await rig.tm.advance(60)
    await rig.weather_event(temperature_c=Decimal("2.5"))
    assert rig.journal().state is ExecutorState.WATERING
    assert rig.driver.start_calls[-1][0].station_number == 2
    assert rig.journal().paused_reason is None


async def test_freeze_defer_remaining_aborts_with_deferred_detail() -> None:
    rig = freeze_rig(SensorCutBehavior.DEFER_REMAINING)
    await rig.executor.async_start_run(rig.plan)
    await rig.tm.advance(3 * 60)
    await rig.weather_event(temperature_c=Decimal(0))

    assert rig.journal().state is ExecutorState.IDLE
    assert rig.history.finished[0][1] is RunOutcome.ABORTED_SENSOR
    results = rig.final_step_results()
    assert results["1"]["reason"] == "low_temperature:deferred"


async def test_freeze_during_gap_caught_by_pre_step() -> None:
    rig = freeze_rig()
    await rig.executor.async_start_run(rig.plan)
    # Zone a completes normally; executor enters the inter-zone gap.
    await rig.tm.advance(10 * 60)
    assert rig.journal().state is ExecutorState.INTER_ZONE_GAP

    # A cold reading during the gap does not cut (nothing is watering)...
    await rig.weather_event(temperature_c=Decimal(0))
    assert rig.journal().state is ExecutorState.INTER_ZONE_GAP
    assert rig.driver.stop_calls == 0

    # ...but the pre-step check blocks zone b when the gap elapses.
    await rig.tm.advance(5)
    assert rig.journal().state is ExecutorState.IDLE
    assert rig.history.finished[0][1] is RunOutcome.ABORTED_SENSOR
    assert len(rig.driver.start_calls) == 1


async def test_freeze_pause_expires_past_tolerance() -> None:
    rig = freeze_rig(SensorCutBehavior.PAUSE_UNTIL_DRY)
    await rig.executor.async_start_run(rig.plan)
    await rig.tm.advance(3 * 60)
    await rig.weather_event(temperature_c=Decimal(0))
    assert rig.journal().state is ExecutorState.PAUSED_SENSOR

    # Past the 30-minute missed-run tolerance, warming expires the run.
    await rig.tm.advance(45 * 60)
    await rig.weather_event(temperature_c=Decimal(10))
    assert rig.journal().state is ExecutorState.IDLE
    assert rig.history.finished[0][1] in (
        RunOutcome.COMPLETED_WITH_SKIPS,
        RunOutcome.SKIPPED,
    )


async def test_freeze_pause_does_not_resume_while_wet() -> None:
    rig = freeze_rig(SensorCutBehavior.PAUSE_UNTIL_DRY)
    await rig.executor.async_start_run(rig.plan)
    await rig.tm.advance(3 * 60)
    await rig.weather_event(temperature_c=Decimal(0))
    assert rig.journal().state is ExecutorState.PAUSED_SENSOR

    # Warm enough to clear freeze, but the rain sensor is now wet: stay paused.
    await rig.tm.advance(60)
    await rig.weather_event(temperature_c=Decimal(5), rain_sensor=True)
    assert rig.journal().state is ExecutorState.PAUSED_SENSOR

    # Both clear: resume.
    await rig.tm.advance(60)
    await rig.weather_event(temperature_c=Decimal(5), rain_sensor=False)
    assert rig.journal().state is ExecutorState.WATERING


async def test_rain_pause_does_not_resume_while_freezing() -> None:
    zones = [
        make_zone("a", 1, base_runtime_minutes=Decimal(10)),
        make_zone("b", 2, base_runtime_minutes=Decimal(10)),
    ]
    program = make_program("p", ["a", "b"])
    program.rain_policy.sensor_cut_behavior = SensorCutBehavior.PAUSE_UNTIL_DRY
    guard = FreezeGuardConfig(
        enabled=True, temperature_entity_id="sensor.t", threshold=Decimal(1)
    )
    rig = build_rig(zones, program, controller=make_controller(freeze_guard=guard))

    await rig.executor.async_start_run(rig.plan)
    await rig.tm.advance(3 * 60)
    # Rain cut (hardware): zone off, sensor wet.
    await rig.zone_event("a", False, rain_sensor=True)
    assert rig.journal().state is ExecutorState.PAUSED_SENSOR

    # Sensor dries but it is now freezing: do not resume into a freeze.
    await rig.tm.advance(60)
    await rig.executor.async_handle_sensor_state(
        rig.driver.make_observation(
            set(), rain_sensor=False, temperature_c=Decimal(0)
        )
    )
    assert rig.journal().state is ExecutorState.PAUSED_SENSOR

    # Dry and warm: resume.
    await rig.tm.advance(60)
    await rig.executor.async_handle_sensor_state(
        rig.driver.make_observation(
            set(), rain_sensor=False, temperature_c=Decimal(10)
        )
    )
    assert rig.journal().state is ExecutorState.WATERING


async def test_unknown_temperature_mid_run_does_not_cut() -> None:
    rig = freeze_rig()
    await rig.executor.async_start_run(rig.plan)
    await rig.tm.advance(3 * 60)

    # A missing reading is never a definitive freeze: keep watering.
    await rig.weather_event(temperature_c=None)
    assert rig.journal().state is ExecutorState.WATERING
    assert rig.driver.stop_calls == 0


async def test_block_when_unavailable_stops_at_pre_step() -> None:
    rig = freeze_rig(when_unavailable=FreezeUnavailablePolicy.BLOCK_WATERING)
    await rig.executor.async_start_run(rig.plan)
    await rig.tm.advance(10 * 60)  # zone a done, entering gap
    assert rig.journal().state is ExecutorState.INTER_ZONE_GAP

    # Temperature source goes unknown; the block policy stops the next step.
    await rig.weather_event(temperature_c=None)
    await rig.tm.advance(5)
    assert rig.journal().state is ExecutorState.IDLE
    assert rig.history.finished[0][1] is RunOutcome.ABORTED_SENSOR
    assert len(rig.driver.start_calls) == 1


async def test_uncertain_freeze_cut_stop_retries_until_confirmed() -> None:
    """A failed cut stop has a watchdog: retry while the run stays paused."""
    rig = freeze_rig(SensorCutBehavior.PAUSE_UNTIL_DRY)
    rig.driver.stop_error = DriverError("nope")
    await rig.executor.async_start_run(rig.plan)
    await rig.tm.advance(3 * 60)

    await rig.weather_event(temperature_c=Decimal(0))
    assert rig.journal().state is ExecutorState.PAUSED_SENSOR
    assert rig.driver.stop_calls == 1
    assert (
        rig.journal().pending_command.disposition is CommandDisposition.UNCERTAIN
    )

    # The first retry fires 5s later and confirms the stop.
    rig.driver.stop_error = None
    await rig.tm.advance(5)
    assert rig.driver.stop_calls == 2
    assert rig.journal().pending_command.disposition is CommandDisposition.SENT

    # No further retries once the stop confirmed.
    await rig.tm.advance(60)
    assert rig.driver.stop_calls == 2


async def test_uncertain_stop_retry_is_bounded() -> None:
    rig = freeze_rig(SensorCutBehavior.PAUSE_UNTIL_DRY)
    rig.driver.stop_error = DriverError("nope")
    await rig.executor.async_start_run(rig.plan)
    await rig.tm.advance(3 * 60)
    await rig.weather_event(temperature_c=Decimal(0))

    await rig.tm.advance(5)
    assert rig.driver.stop_calls == 2
    await rig.tm.advance(20)
    assert rig.driver.stop_calls == 3
    # The retry budget is exhausted: no more attempts, no timer pending.
    await rig.tm.advance(120)
    assert rig.driver.stop_calls == 3
    assert rig.tm.pending() == []

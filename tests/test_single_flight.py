"""Single-flight behavior (plan §17.1)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from custom_components.rainbird_scheduler.executor import ControllerBusyError
from custom_components.rainbird_scheduler.models import ExecutorState
from custom_components.rainbird_scheduler.planner import compile_timeline

from .harness import START, three_zone_rig
from .helpers import make_input, make_occurrence


def _second_plan(rig, minutes_later: int = 5):
    program = rig.programs["morning-lawn"]
    occurrence = make_occurrence(
        program, START + timedelta(minutes=minutes_later), manual=True
    )
    timeline = compile_timeline(
        make_input(
            rig.controller,
            [program],
            list(rig.zones.values()),
            [occurrence],
        )
    )
    return timeline.runs[0]


async def test_second_run_is_rejected_not_queued() -> None:
    rig = three_zone_rig()
    await rig.executor.async_start_run(rig.plan)
    commands_before = len(rig.driver.start_calls)

    with pytest.raises(ControllerBusyError) as excinfo:
        await rig.executor.async_start_run(_second_plan(rig))

    assert excinfo.value.program_name == "Morning Lawn"
    assert excinfo.value.run_id == rig.plan.run_id
    # The active run is untouched and no extra commands were sent.
    assert rig.journal().state is ExecutorState.WATERING
    assert rig.journal().run_plan.run_id == rig.plan.run_id
    assert len(rig.driver.start_calls) == commands_before


async def test_rejected_while_paused_too() -> None:
    rig = three_zone_rig()
    await rig.executor.async_start_run(rig.plan)
    await rig.executor.async_pause()
    with pytest.raises(ControllerBusyError):
        await rig.executor.async_start_run(_second_plan(rig))


async def test_new_run_allowed_after_completion() -> None:
    rig = three_zone_rig()
    await rig.executor.async_start_run(rig.plan)
    await rig.tm.advance(2 * 3600)
    assert rig.journal().state is ExecutorState.IDLE

    second = _second_plan(rig, minutes_later=180)
    await rig.executor.async_start_run(second)
    assert rig.journal().run_plan.run_id == second.run_id

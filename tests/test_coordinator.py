"""Coordinator scheduling: planned arming, retry loops, recovery nudges."""

from __future__ import annotations

from datetime import time, timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.rainbird_scheduler.executor import ControllerBusyError
from custom_components.rainbird_scheduler.models import (
    ExecutorState,
    RunOutcome,
    SkipReason,
)
from custom_components.rainbird_scheduler.storage import SchedulerStorage

from .conftest import setup_scheduler


@pytest.fixture(autouse=True)
def _custom_integrations(enable_custom_integrations: None) -> None:
    """Allow loading the custom integration in this module."""


def register_fake_controller(hass: HomeAssistant) -> list[tuple[str, int]]:
    """Fake the rainbird start service and switch turn_off; record starts."""
    started: list[tuple[str, int]] = []

    async def fake_start(call: ServiceCall) -> None:
        entity_id = call.data["entity_id"]
        entity_id = entity_id[0] if isinstance(entity_id, list) else entity_id
        started.append((entity_id, int(call.data["duration"])))
        state = hass.states.get(entity_id)
        hass.states.async_set(entity_id, "on", dict(state.attributes))

    async def fake_turn_off(call: ServiceCall) -> None:
        entity_id = call.data["entity_id"]
        entity_id = entity_id[0] if isinstance(entity_id, list) else entity_id
        state = hass.states.get(entity_id)
        if state is not None:
            hass.states.async_set(entity_id, "off", dict(state.attributes))

    hass.services.async_register("rainbird", "start_irrigation", fake_start)
    hass.services.async_register("switch", "turn_off", fake_turn_off)
    return started


def zone_id_for(coordinator: Any, station: int) -> str:
    """Look up the discovered zone id for a station number."""
    return next(
        zone.id
        for zone in coordinator.config.zones.values()
        if zone.reference.station_number == station
    )


def local_start(delta: timedelta) -> time:
    """A local wall-clock start ``delta`` from now (second precision)."""
    return (dt_util.now() + delta).time().replace(microsecond=0)


def backdated_start(minutes: int) -> time:
    """A local wall-clock start ``minutes`` in the past, same local day."""
    now_local = dt_util.now()
    past = now_local - timedelta(minutes=minutes)
    if past.date() != now_local.date():
        pytest.skip("too close to local midnight for a backdated start")
    return past.time().replace(microsecond=0)


def program_payload(
    zone_ids: list[str], start: time, name: str, **overrides: Any
) -> dict[str, Any]:
    """A full serde payload for async_create_program (fixed 100% durations)."""
    payload: dict[str, Any] = {
        "name": name,
        "enabled": True,
        "priority": 100,
        "recurrence": {"kind": "weekly", "weekdays": [0, 1, 2, 3, 4, 5, 6]},
        "nominal_start_times": [start.isoformat()],
        "zone_steps": [
            {"zone_id": zone_id, "position": position, "enabled": True}
            for position, zone_id in enumerate(zone_ids)
        ],
        "adjustment_provider": {"kind": "fixed"},
        "rain_policy": {
            "honor_native_delay": False,
            "skip_when_sensor_wet": False,
            "sensor_cut_behavior": "abort_run",
        },
        "missed_run_policy": "run_late",
        "external_interruption_policy": "pause",
        "watering_window": None,
    }
    payload.update(overrides)
    return payload


async def test_overlapping_program_arms_at_planned_start(
    hass: HomeAssistant, scheduler_entry: MockConfigEntry
) -> None:
    """A run the planner delayed behind another must wait for its planned
    start instead of firing at its nominal time into a busy controller."""
    await setup_scheduler(hass, scheduler_entry)
    coordinator = scheduler_entry.runtime_data
    started = register_fake_controller(hass)
    zone_a = zone_id_for(coordinator, 1)
    zone_b = zone_id_for(coordinator, 2)

    await coordinator.async_create_program(
        program_payload(
            [zone_a],
            local_start(timedelta(minutes=1)),
            "Long",
            zone_steps=[
                {
                    "zone_id": zone_a,
                    "position": 0,
                    "enabled": True,
                    "base_runtime_override_minutes": "35",
                }
            ],
        )
    )
    await coordinator.async_create_program(
        program_payload([zone_b], local_start(timedelta(minutes=2)), "Behind")
    )
    await hass.async_block_till_done()

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=70))
    await hass.async_block_till_done()
    assert started == [("switch.rain_bird_sprinkler_1", 35)]

    # Past B's nominal start, mid-run for A: B must not have fired into the
    # busy controller and entered the launch-retry loop.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=150))
    await hass.async_block_till_done()
    assert coordinator.executor.journal.state is ExecutorState.WATERING
    assert coordinator._unsub_retry is None

    # A's 35-minute zone ends; only then does B's planned start arrive.
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(minutes=35, seconds=30)
    )
    await hass.async_block_till_done()
    # The real controller turns the zone off when its time elapses.
    hass.states.async_set(
        "switch.rain_bird_sprinkler_1", "off", {"zone": 1}
    )
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=40))
    await hass.async_block_till_done()
    assert started == [
        ("switch.rain_bird_sprinkler_1", 35),
        ("switch.rain_bird_sprinkler_2", 10),
    ]


async def test_elapsed_occurrence_within_tolerance_still_runs(
    hass: HomeAssistant, scheduler_entry: MockConfigEntry
) -> None:
    """An occurrence never armed (e.g. HA was down) runs late while it is
    within the missed-run tolerance (RUN_LATE policy)."""
    await setup_scheduler(hass, scheduler_entry)
    coordinator = scheduler_entry.runtime_data
    started = register_fake_controller(hass)
    await coordinator.async_create_program(
        program_payload([zone_id_for(coordinator, 1)], backdated_start(5), "Late")
    )
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert started == [("switch.rain_bird_sprinkler_1", 10)]


async def test_expired_occurrence_records_missed_tolerance_skip(
    hass: HomeAssistant, scheduler_entry: MockConfigEntry
) -> None:
    """Past its missed-run deadline, an elapsed occurrence is recorded as
    skipped instead of vanishing silently."""
    await setup_scheduler(hass, scheduler_entry)
    coordinator = scheduler_entry.runtime_data
    started = register_fake_controller(hass)
    program = await coordinator.async_create_program(
        program_payload(
            [zone_id_for(coordinator, 1)], backdated_start(60), "Expired"
        )
    )
    await hass.async_block_till_done()

    assert started == []
    skips = [
        record
        for record in coordinator.history.history.runs
        if record.outcome is RunOutcome.SKIPPED
    ]
    assert len(skips) == 1
    assert skips[0].program_id == program.id
    assert skips[0].reason == SkipReason.MISSED_TOLERANCE.value


async def test_retry_loop_does_not_starve_later_occurrence(
    hass: HomeAssistant, scheduler_entry: MockConfigEntry
) -> None:
    """While A waits out a transient launch block, B still gets armed and
    runs at its planned start."""
    await setup_scheduler(hass, scheduler_entry)
    coordinator = scheduler_entry.runtime_data
    started = register_fake_controller(hass)
    zone_a = zone_id_for(coordinator, 1)
    zone_b = zone_id_for(coordinator, 2)

    # Unknown temperature + block-watering guard transiently blocks any
    # program that skips on freeze; B opts out of the freeze check.
    hass.states.async_set(
        "sensor.outdoor_temp", "unknown", {"unit_of_measurement": "°C"}
    )
    await coordinator.async_update_controller(
        {
            "freeze_guard": {
                "enabled": True,
                "temperature_entity_id": "sensor.outdoor_temp",
                "threshold": "2",
                "unit": "°C",
                "when_unavailable": "block_watering",
            }
        },
        expected_revision=1,
    )
    await coordinator.async_create_program(
        program_payload([zone_a], local_start(timedelta(minutes=1)), "Guarded")
    )
    await coordinator.async_create_program(
        program_payload(
            [zone_b],
            local_start(timedelta(minutes=2)),
            "Unguarded",
            freeze_policy={
                "skip_when_freezing": False,
                "freeze_cut_behavior": "abort_run",
            },
        )
    )
    await hass.async_block_till_done()

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=70))
    await hass.async_block_till_done()
    # A is transiently blocked and parked in the retry loop.
    assert started == []
    assert coordinator._unsub_retry is not None

    # B comes due (planned behind A's blocked run) and must still launch.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=12))
    await hass.async_block_till_done()
    assert started == [("switch.rain_bird_sprinkler_2", 10)]


async def test_paused_external_run_resumes_after_reload(
    hass: HomeAssistant, scheduler_entry: MockConfigEntry
) -> None:
    """A run paused for external activity whose cause cleared while
    unloaded is nudged back to life after setup."""
    await setup_scheduler(hass, scheduler_entry)
    coordinator = scheduler_entry.runtime_data
    started = register_fake_controller(hass)

    await coordinator.async_run_zones(
        [
            {"entity_id": "switch.rain_bird_sprinkler_1", "duration": 5},
            {"entity_id": "switch.rain_bird_sprinkler_2", "duration": 5},
        ]
    )
    await hass.async_block_till_done()
    assert started == [("switch.rain_bird_sprinkler_1", 5)]

    # External activity on another zone pauses the run (no paused_reason).
    hass.states.async_set("switch.rain_bird_sprinkler_3", "on", {"zone": 3})
    await hass.async_block_till_done()
    journal = coordinator.executor.journal
    assert journal.state is ExecutorState.PAUSED_EXTERNAL
    assert journal.paused_reason is None

    # Unload; the external zone stops "while HA is down".
    assert await hass.config_entries.async_unload(scheduler_entry.entry_id)
    await hass.async_block_till_done()
    hass.states.async_set("switch.rain_bird_sprinkler_1", "off", {"zone": 1})
    hass.states.async_set("switch.rain_bird_sprinkler_3", "off", {"zone": 3})

    assert await hass.config_entries.async_setup(scheduler_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = scheduler_entry.runtime_data
    # The nudge resumed the run; the remaining zone was commanded.
    assert ("switch.rain_bird_sprinkler_2", 5) in started


async def test_user_paused_run_is_not_auto_resumed_after_reload(
    hass: HomeAssistant, scheduler_entry: MockConfigEntry
) -> None:
    """A user pause carries a reason and must survive a restart."""
    await setup_scheduler(hass, scheduler_entry)
    coordinator = scheduler_entry.runtime_data
    started = register_fake_controller(hass)

    await coordinator.async_run_zones(
        [
            {"entity_id": "switch.rain_bird_sprinkler_1", "duration": 5},
            {"entity_id": "switch.rain_bird_sprinkler_2", "duration": 5},
        ]
    )
    await hass.async_block_till_done()
    await coordinator.executor.async_pause(reason="user_pause")
    await hass.async_block_till_done()
    assert coordinator.executor.journal.paused_reason == "user_pause"

    assert await hass.config_entries.async_unload(scheduler_entry.entry_id)
    await hass.async_block_till_done()
    hass.states.async_set("switch.rain_bird_sprinkler_1", "off", {"zone": 1})

    assert await hass.config_entries.async_setup(scheduler_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = scheduler_entry.runtime_data
    assert coordinator.executor.journal.state is ExecutorState.PAUSED_EXTERNAL
    assert started == [("switch.rain_bird_sprinkler_1", 5)]


async def test_failed_manual_runs_never_evict_active_program(
    hass: HomeAssistant, scheduler_entry: MockConfigEntry
) -> None:
    """Failed run_zones calls must not evict the active run's ephemeral
    program (its rain-policy bypass would be silently lost)."""
    await setup_scheduler(hass, scheduler_entry)
    coordinator = scheduler_entry.runtime_data
    register_fake_controller(hass)

    await coordinator.async_run_zones(
        [{"entity_id": "switch.rain_bird_sprinkler_1", "duration": 30}]
    )
    await hass.async_block_till_done()
    journal = coordinator.executor.journal
    assert journal.run_plan is not None
    active_program_id = journal.run_plan.program_id

    for _ in range(5):
        with pytest.raises(ControllerBusyError):
            await coordinator.async_run_zones(
                [{"entity_id": "switch.rain_bird_sprinkler_2", "duration": 1}]
            )
    assert coordinator._get_program(active_program_id) is not None


async def test_zone_discovered_after_setup_is_persisted(
    hass: HomeAssistant,
    source_entry: MockConfigEntry,
    scheduler_entry: MockConfigEntry,
) -> None:
    """A runtime zone discovery is written to the config store at once, so
    the zone keeps its id across restarts."""
    await setup_scheduler(hass, scheduler_entry)
    coordinator = scheduler_entry.runtime_data
    assert len(coordinator.config.zones) == 3

    registry = er.async_get(hass)
    registry.async_get_or_create(
        "switch",
        "rainbird",
        "aabbcc-4",
        config_entry=source_entry,
        original_name="Sprinkler 4",
        suggested_object_id="rain_bird_sprinkler_4",
    )
    hass.states.async_set(
        "switch.rain_bird_sprinkler_4",
        "off",
        {"zone": 4, "friendly_name": "Sprinkler 4"},
    )
    await hass.async_block_till_done()

    assert len(coordinator.config.zones) == 4
    stored = await SchedulerStorage(
        hass, scheduler_entry.entry_id
    ).async_load_config()
    assert stored is not None
    assert {z.reference.station_number for z in stored.zones.values()} == {
        1,
        2,
        3,
        4,
    }

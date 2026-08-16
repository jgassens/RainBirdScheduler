"""Integration setup, discovery, and an end-to-end run (HA harness)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.rainbird_scheduler.models import ExecutorState

from .conftest import setup_scheduler


@pytest.fixture(autouse=True)
def _custom_integrations(enable_custom_integrations: None) -> None:
    """Allow loading the custom integration in this module."""


async def test_setup_discovers_zones_and_creates_entities(
    hass: HomeAssistant, scheduler_entry: MockConfigEntry
) -> None:
    await setup_scheduler(hass, scheduler_entry)
    coordinator = scheduler_entry.runtime_data

    zones = coordinator.config.zones
    assert len(zones) == 3
    stations = sorted(z.reference.station_number for z in zones.values())
    assert stations == [1, 2, 3]
    # Zone display names came from the source entities.
    names = {z.display_name for z in zones.values()}
    assert "Sprinkler 1" in names

    registry = er.async_get(hass)
    unique = scheduler_entry.unique_id
    for suffix in (
        "next_irrigation",
        "active_zone",
        "expected_end",
        "seasonal_adjustment",
        "last_run",
        "scheduler_running",
        "native_schedule_conflict",
        "external_watering",
        "scheduler_enabled",
        "stop_controller",
        "irrigation_lifecycle",
        "irrigation_plan",
    ):
        assert any(
            entry.unique_id == f"{unique}_{suffix}"
            for entry in registry.entities.values()
        ), f"missing entity {suffix}"

    assert coordinator.executor.journal.state is ExecutorState.IDLE


async def test_unload_entry(
    hass: HomeAssistant, scheduler_entry: MockConfigEntry
) -> None:
    await setup_scheduler(hass, scheduler_entry)
    assert await hass.config_entries.async_unload(scheduler_entry.entry_id)
    await hass.async_block_till_done()
    assert scheduler_entry.state.value == "not_loaded"


async def test_end_to_end_manual_zone_run(
    hass: HomeAssistant, scheduler_entry: MockConfigEntry
) -> None:
    """A manual two-zone run drives the fake controller to completion."""
    await setup_scheduler(hass, scheduler_entry)
    coordinator = scheduler_entry.runtime_data

    started: list[tuple[str, int]] = []
    stopped: list[str] = []

    async def fake_start(call: ServiceCall) -> None:
        entity_id = call.data["entity_id"]
        entity_id = entity_id[0] if isinstance(entity_id, list) else entity_id
        started.append((entity_id, int(call.data["duration"])))
        state = hass.states.get(entity_id)
        hass.states.async_set(entity_id, "on", dict(state.attributes))

    async def fake_turn_off(call: ServiceCall) -> None:
        entity_id = call.data["entity_id"]
        entity_id = entity_id[0] if isinstance(entity_id, list) else entity_id
        stopped.append(entity_id)
        state = hass.states.get(entity_id)
        hass.states.async_set(entity_id, "off", dict(state.attributes))

    hass.services.async_register("rainbird", "start_irrigation", fake_start)
    hass.services.async_register("switch", "turn_off", fake_turn_off)

    await coordinator.async_run_zones(
        [
            {"entity_id": "switch.rain_bird_sprinkler_1", "duration": 1},
            {"entity_id": "switch.rain_bird_sprinkler_2", "duration": 1},
        ]
    )
    await hass.async_block_till_done()

    journal = coordinator.executor.journal
    assert journal.state is ExecutorState.WATERING
    assert started == [("switch.rain_bird_sprinkler_1", 1)]

    # Zone 1's commanded minute elapses; the switch reports off.
    hass.states.async_set(
        "switch.rain_bird_sprinkler_1", "off", {"zone": 1}
    )
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(minutes=1, seconds=2)
    )
    await hass.async_block_till_done()

    # After the 5s inter-zone gap, zone 2 starts.
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(minutes=1, seconds=10)
    )
    await hass.async_block_till_done()
    assert started[-1] == ("switch.rain_bird_sprinkler_2", 1)

    # Zone 2 finishes; run completes after the final gap elapses.
    hass.states.async_set(
        "switch.rain_bird_sprinkler_2", "off", {"zone": 2}
    )
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(minutes=2, seconds=20)
    )
    await hass.async_block_till_done()
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(minutes=2, seconds=40)
    )
    await hass.async_block_till_done()

    assert journal.state is ExecutorState.IDLE
    runs = coordinator.history.history.runs
    assert len(runs) == 1
    assert runs[0].outcome.value in ("completed", "completed_with_skips")
    # Every zone got exactly one command; nothing was blindly repeated.
    assert [item[0] for item in started] == [
        "switch.rain_bird_sprinkler_1",
        "switch.rain_bird_sprinkler_2",
    ]


async def test_stop_controller_button_uses_switch_turn_off(
    hass: HomeAssistant, scheduler_entry: MockConfigEntry
) -> None:
    await setup_scheduler(hass, scheduler_entry)
    stopped: list[str] = []

    async def fake_turn_off(call: ServiceCall) -> None:
        entity_id = call.data["entity_id"]
        stopped.append(
            entity_id[0] if isinstance(entity_id, list) else entity_id
        )

    hass.services.async_register("switch", "turn_off", fake_turn_off)
    coordinator = scheduler_entry.runtime_data
    await coordinator.executor.async_stop()
    assert len(stopped) == 1
    assert stopped[0].startswith("switch.rain_bird_sprinkler")

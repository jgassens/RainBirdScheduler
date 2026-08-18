"""WebSocket API: CRUD, optimistic concurrency, subscription (plan §33)."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rainbird_scheduler.const import DOMAIN

from .conftest import setup_scheduler


@pytest.fixture(autouse=True)
def _custom_integrations(enable_custom_integrations: None) -> None:
    """Allow loading the custom integration in this module."""


async def _client(hass, hass_ws_client, scheduler_entry):
    await setup_scheduler(hass, scheduler_entry)
    return await hass_ws_client(hass)


PROGRAM_PAYLOAD = {
    "name": "Morning Lawn",
    "enabled": True,
    "priority": 10,
    "recurrence": {"kind": "weekly", "weekdays": [0, 2, 5]},
    "nominal_start_times": ["09:00:00"],
    "zone_steps": [],
    "adjustment_provider": {"kind": "manual_percent", "percent": "80"},
    "rain_policy": {
        "honor_native_delay": True,
        "skip_when_sensor_wet": True,
        "sensor_cut_behavior": "abort_run",
    },
    "missed_run_policy": "run_late",
    "external_interruption_policy": "pause",
    "watering_window": None,
}


async def test_entries_and_config_get(
    hass: HomeAssistant, hass_ws_client, scheduler_entry: MockConfigEntry
) -> None:
    client = await _client(hass, hass_ws_client, scheduler_entry)

    await client.send_json(
        {"id": 1, "type": f"{DOMAIN}/entries"}
    )
    result = await client.receive_json()
    assert result["success"]
    assert result["result"][0]["entry_id"] == scheduler_entry.entry_id

    await client.send_json(
        {
            "id": 2,
            "type": f"{DOMAIN}/config/get",
            "entry_id": scheduler_entry.entry_id,
        }
    )
    result = await client.receive_json()
    assert result["success"]
    assert result["result"]["controller"]["revision"] == 1
    assert len(result["result"]["zones"]) == 3


async def test_program_crud_with_revisions(
    hass: HomeAssistant, hass_ws_client, scheduler_entry: MockConfigEntry
) -> None:
    client = await _client(hass, hass_ws_client, scheduler_entry)
    coordinator = scheduler_entry.runtime_data
    zone_id = next(iter(coordinator.config.zones))

    program = dict(PROGRAM_PAYLOAD)
    program["zone_steps"] = [
        {"zone_id": zone_id, "position": 0, "enabled": True}
    ]
    await client.send_json(
        {
            "id": 1,
            "type": f"{DOMAIN}/program/create",
            "entry_id": scheduler_entry.entry_id,
            "program": program,
        }
    )
    result = await client.receive_json()
    assert result["success"], result
    program_id = result["result"]["program"]["id"]
    assert result["result"]["program"]["revision"] == 1

    # Update with a stale revision: rejected, never overwritten (plan §41).
    await client.send_json(
        {
            "id": 2,
            "type": f"{DOMAIN}/program/update",
            "entry_id": scheduler_entry.entry_id,
            "program_id": program_id,
            "expected_revision": 99,
            "patch": {"name": "Hijacked"},
        }
    )
    result = await client.receive_json()
    assert not result["success"]
    assert result["error"]["code"] == "revision_conflict"
    assert coordinator.config.programs[program_id].name == "Morning Lawn"

    # Correct revision succeeds and bumps the revision.
    await client.send_json(
        {
            "id": 3,
            "type": f"{DOMAIN}/program/update",
            "entry_id": scheduler_entry.entry_id,
            "program_id": program_id,
            "expected_revision": 1,
            "patch": {"name": "Morning Lawn 2", "priority": 5},
        }
    )
    result = await client.receive_json()
    assert result["success"]
    assert result["result"]["program"]["revision"] == 2
    assert result["result"]["program"]["name"] == "Morning Lawn 2"

    # Duplicate arrives disabled; delete removes it.
    await client.send_json(
        {
            "id": 4,
            "type": f"{DOMAIN}/program/duplicate",
            "entry_id": scheduler_entry.entry_id,
            "program_id": program_id,
        }
    )
    result = await client.receive_json()
    assert result["success"]
    copy_id = result["result"]["program"]["id"]
    assert result["result"]["program"]["enabled"] is False

    await client.send_json(
        {
            "id": 5,
            "type": f"{DOMAIN}/program/delete",
            "entry_id": scheduler_entry.entry_id,
            "program_id": copy_id,
        }
    )
    result = await client.receive_json()
    assert result["success"]
    assert copy_id not in coordinator.config.programs


async def test_zone_update_and_plan_preview(
    hass: HomeAssistant, hass_ws_client, scheduler_entry: MockConfigEntry
) -> None:
    client = await _client(hass, hass_ws_client, scheduler_entry)
    coordinator = scheduler_entry.runtime_data
    zone_id = next(iter(coordinator.config.zones))

    await client.send_json(
        {
            "id": 1,
            "type": f"{DOMAIN}/zone/update",
            "entry_id": scheduler_entry.entry_id,
            "zone_id": zone_id,
            "expected_revision": 1,
            "patch": {"display_name": "Front Lawn", "base_runtime_minutes": "12.5"},
        }
    )
    result = await client.receive_json()
    assert result["success"], result
    assert result["result"]["zone"]["display_name"] == "Front Lawn"
    assert coordinator.config.zones[zone_id].display_name == "Front Lawn"

    await client.send_json(
        {
            "id": 2,
            "type": f"{DOMAIN}/plan/preview",
            "entry_id": scheduler_entry.entry_id,
        }
    )
    result = await client.receive_json()
    assert result["success"]
    assert "timeline" in result["result"]
    assert result["result"]["state"]["executor_state"] == "idle"


async def test_zone_update_rejects_null_base_runtime(
    hass: HomeAssistant, hass_ws_client, scheduler_entry: MockConfigEntry
) -> None:
    client = await _client(hass, hass_ws_client, scheduler_entry)
    coordinator = scheduler_entry.runtime_data
    zone_id = next(iter(coordinator.config.zones))
    before = coordinator.config.zones[zone_id]

    await client.send_json(
        {
            "id": 1,
            "type": f"{DOMAIN}/zone/update",
            "entry_id": scheduler_entry.entry_id,
            "zone_id": zone_id,
            "expected_revision": before.revision,
            "patch": {"base_runtime_minutes": None},
        }
    )
    result = await client.receive_json()
    assert not result["success"]
    assert result["error"]["code"] == "invalid_zone"
    assert "base_runtime_minutes" in result["error"]["message"]

    # The zone is untouched: value, revision, and no null smuggled in.
    after = coordinator.config.zones[zone_id]
    assert after.base_runtime_minutes == before.base_runtime_minutes
    assert after.revision == before.revision


async def test_authority_change_requires_acknowledgment(
    hass: HomeAssistant, hass_ws_client, scheduler_entry: MockConfigEntry
) -> None:
    client = await _client(hass, hass_ws_client, scheduler_entry)

    await client.send_json(
        {
            "id": 1,
            "type": f"{DOMAIN}/config/update",
            "entry_id": scheduler_entry.entry_id,
            "expected_revision": 1,
            "patch": {"authority_mode": "native_authoritative"},
        }
    )
    result = await client.receive_json()
    assert not result["success"]
    assert result["error"]["code"] == "authority_ack_required"

    await client.send_json(
        {
            "id": 2,
            "type": f"{DOMAIN}/config/update",
            "entry_id": scheduler_entry.entry_id,
            "expected_revision": 1,
            "patch": {"authority_mode": "native_authoritative"},
            "acknowledge_authority_change": True,
        }
    )
    result = await client.receive_json()
    assert result["success"]
    assert (
        result["result"]["controller"]["authority_mode"]
        == "native_authoritative"
    )


async def test_subscribe_pushes_initial_state(
    hass: HomeAssistant, hass_ws_client, scheduler_entry: MockConfigEntry
) -> None:
    client = await _client(hass, hass_ws_client, scheduler_entry)
    await client.send_json(
        {
            "id": 1,
            "type": f"{DOMAIN}/subscribe",
            "entry_id": scheduler_entry.entry_id,
        }
    )
    result = await client.receive_json()
    assert result["success"]
    event = await client.receive_json()
    assert event["type"] == "event"
    assert event["event"]["kind"] == "state"
    assert event["event"]["state"]["executor_state"] == "idle"


async def test_unknown_entry_rejected(
    hass: HomeAssistant, hass_ws_client, scheduler_entry: MockConfigEntry
) -> None:
    client = await _client(hass, hass_ws_client, scheduler_entry)
    await client.send_json(
        {"id": 1, "type": f"{DOMAIN}/config/get", "entry_id": "nope"}
    )
    result = await client.receive_json()
    assert not result["success"]
    assert result["error"]["code"] == "entry_not_found"


async def test_config_update_patches_freeze_guard(
    hass: HomeAssistant, hass_ws_client, scheduler_entry: MockConfigEntry
) -> None:
    client = await _client(hass, hass_ws_client, scheduler_entry)
    await client.send_json(
        {
            "id": 1,
            "type": f"{DOMAIN}/config/update",
            "entry_id": scheduler_entry.entry_id,
            "expected_revision": 1,
            "patch": {
                "freeze_guard": {
                    "enabled": True,
                    "temperature_entity_id": "weather.home",
                    "threshold": "2",
                    "unit": "°C",
                    "when_unavailable": "allow_watering",
                }
            },
        }
    )
    result = await client.receive_json()
    assert result["success"]
    guard = result["result"]["controller"]["freeze_guard"]
    assert guard["enabled"] is True
    assert guard["temperature_entity_id"] == "weather.home"


async def test_config_update_rejects_bare_number_entity(
    hass: HomeAssistant, hass_ws_client, scheduler_entry: MockConfigEntry
) -> None:
    client = await _client(hass, hass_ws_client, scheduler_entry)
    await client.send_json(
        {
            "id": 1,
            "type": f"{DOMAIN}/config/update",
            "entry_id": scheduler_entry.entry_id,
            "expected_revision": 1,
            "patch": {
                "freeze_guard": {
                    "enabled": True,
                    "temperature_entity_id": "50",
                }
            },
        }
    )
    result = await client.receive_json()
    assert not result["success"]
    assert result["error"]["code"] == "invalid_config"


async def test_config_update_rejects_enabled_freeze_guard_without_entity(
    hass: HomeAssistant, hass_ws_client, scheduler_entry: MockConfigEntry
) -> None:
    client = await _client(hass, hass_ws_client, scheduler_entry)
    coordinator = scheduler_entry.runtime_data
    await client.send_json(
        {
            "id": 1,
            "type": f"{DOMAIN}/config/update",
            "entry_id": scheduler_entry.entry_id,
            "expected_revision": 1,
            "patch": {"freeze_guard": {"enabled": True}},
        }
    )
    result = await client.receive_json()
    assert not result["success"]
    assert result["error"]["code"] == "invalid_config"
    # The stored guard is untouched: still disabled, revision unbumped.
    assert coordinator.config.controller.freeze_guard.enabled is False
    assert coordinator.config.controller.revision == 1


async def test_config_update_rejects_freeze_guard_entity_cleared(
    hass: HomeAssistant, hass_ws_client, scheduler_entry: MockConfigEntry
) -> None:
    client = await _client(hass, hass_ws_client, scheduler_entry)
    await client.send_json(
        {
            "id": 1,
            "type": f"{DOMAIN}/config/update",
            "entry_id": scheduler_entry.entry_id,
            "expected_revision": 1,
            "patch": {
                "freeze_guard": {
                    "enabled": True,
                    "temperature_entity_id": None,
                }
            },
        }
    )
    result = await client.receive_json()
    assert not result["success"]
    assert result["error"]["code"] == "invalid_config"


async def test_config_update_allows_disabled_freeze_guard_without_entity(
    hass: HomeAssistant, hass_ws_client, scheduler_entry: MockConfigEntry
) -> None:
    client = await _client(hass, hass_ws_client, scheduler_entry)
    await client.send_json(
        {
            "id": 1,
            "type": f"{DOMAIN}/config/update",
            "entry_id": scheduler_entry.entry_id,
            "expected_revision": 1,
            "patch": {"freeze_guard": {"enabled": False}},
        }
    )
    result = await client.receive_json()
    assert result["success"]
    assert result["result"]["controller"]["freeze_guard"]["enabled"] is False


async def test_config_update_conflict_on_stale_revision(
    hass: HomeAssistant, hass_ws_client, scheduler_entry: MockConfigEntry
) -> None:
    client = await _client(hass, hass_ws_client, scheduler_entry)
    await client.send_json(
        {
            "id": 1,
            "type": f"{DOMAIN}/config/update",
            "entry_id": scheduler_entry.entry_id,
            "expected_revision": 999,
            "patch": {
                "freeze_guard": {
                    "enabled": True,
                    "temperature_entity_id": "sensor.outdoor_temperature",
                }
            },
        }
    )
    result = await client.receive_json()
    assert not result["success"]
    assert result["error"]["code"] == "revision_conflict"

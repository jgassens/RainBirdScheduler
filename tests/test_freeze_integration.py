"""Coordinator-level wiring for the freeze guard and rain-sensor override."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import setup_scheduler


@pytest.fixture(autouse=True)
def _custom_integrations(enable_custom_integrations: None) -> None:
    """Allow loading the custom integration in this module."""


async def test_discovery_prefers_moisture_sensor(
    hass: HomeAssistant,
    source_entry: MockConfigEntry,
    scheduler_entry: MockConfigEntry,
) -> None:
    registry = er.async_get(hass)
    # A second binary_sensor that sorts before the rain sensor by entity id;
    # without the device-class preference it would win the tie-break.
    other = registry.async_get_or_create(
        "binary_sensor",
        "rainbird",
        "aabbcc-connectivity",
        config_entry=source_entry,
        original_name="Connectivity",
        original_device_class="connectivity",
        suggested_object_id="rain_bird_connectivity",
    )
    hass.states.async_set(other.entity_id, "on", {})

    await setup_scheduler(hass, scheduler_entry)
    coordinator = scheduler_entry.runtime_data

    ref = coordinator.config.controller.rain_sensor_reference
    assert ref is not None
    assert ref.last_known_entity_id == "binary_sensor.rain_bird_rainsensor"


async def test_override_survives_rediscovery(
    hass: HomeAssistant, scheduler_entry: MockConfigEntry
) -> None:
    await setup_scheduler(hass, scheduler_entry)
    coordinator = scheduler_entry.runtime_data

    await coordinator.async_update_controller(
        {"rain_sensor_override_entity_id": "binary_sensor.my_rain"},
        coordinator.config.controller.revision,
    )
    assert coordinator._effective_rain_sensor() == "binary_sensor.my_rain"

    # A registry re-scan must not clobber the user override.
    coordinator._discover_sources()
    assert (
        coordinator.config.controller.rain_sensor_override_entity_id
        == "binary_sensor.my_rain"
    )
    assert coordinator._effective_rain_sensor() == "binary_sensor.my_rain"
    # The discovered reference still tracks the real Rain Bird sensor.
    assert coordinator._rain_sensor_entity == "binary_sensor.rain_bird_rainsensor"


async def test_temperature_entity_reaches_observation(
    hass: HomeAssistant, scheduler_entry: MockConfigEntry
) -> None:
    await setup_scheduler(hass, scheduler_entry)
    coordinator = scheduler_entry.runtime_data

    hass.states.async_set(
        "sensor.outdoor_temp", "5", {"unit_of_measurement": "°C"}
    )
    await coordinator.async_update_controller(
        {
            "freeze_guard": {
                "enabled": True,
                "temperature_entity_id": "sensor.outdoor_temp",
                "threshold": "2",
                "unit": "°C",
                "when_unavailable": "allow_watering",
            }
        },
        coordinator.config.controller.revision,
    )
    await hass.async_block_till_done()

    # A change to the temperature source rebuilds the observation, proving the
    # config update re-subscribed and the reading is routed through.
    hass.states.async_set(
        "sensor.outdoor_temp", "-3", {"unit_of_measurement": "°C"}
    )
    await hass.async_block_till_done()

    observation = coordinator.last_observation
    assert observation is not None
    assert observation.current_temperature_c is not None
    assert observation.current_temperature_c < 0

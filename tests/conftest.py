"""Shared fixtures for Home Assistant harness tests.

Pure-model tests do not use these; the ``enable_custom_integrations``
autouse fixture lives in the HA test modules themselves so the fast pure
tests never spin up a hass instance.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rainbird_scheduler.const import (
    CONF_AUTHORITY_MODE,
    CONF_SOURCE_CONFIG_ENTRY_ID,
    CONF_SOURCE_UNIQUE_ID,
    DOMAIN,
)

SOURCE_UNIQUE_ID = "aa:bb:cc:dd:ee:ff"


@pytest.fixture
def source_entry(hass: HomeAssistant) -> MockConfigEntry:
    """A mock core Rain Bird config entry with zones and helper entities."""
    source = MockConfigEntry(
        domain="rainbird",
        title="Rain Bird",
        unique_id=SOURCE_UNIQUE_ID,
        data={},
    )
    source.add_to_hass(hass)
    registry = er.async_get(hass)
    for station in (1, 2, 3):
        entry = registry.async_get_or_create(
            "switch",
            "rainbird",
            f"aabbcc-{station}",
            config_entry=source,
            original_name=f"Sprinkler {station}",
            suggested_object_id=f"rain_bird_sprinkler_{station}",
        )
        hass.states.async_set(
            entry.entity_id,
            "off",
            {"zone": station, "friendly_name": f"Sprinkler {station}"},
        )
    sensor = registry.async_get_or_create(
        "binary_sensor",
        "rainbird",
        "aabbcc-rainsensor",
        config_entry=source,
        original_name="Rainsensor",
        suggested_object_id="rain_bird_rainsensor",
    )
    hass.states.async_set(sensor.entity_id, "off", {})
    delay = registry.async_get_or_create(
        "number",
        "rainbird",
        "aabbcc-raindelay",
        config_entry=source,
        original_name="Rain delay",
        suggested_object_id="rain_bird_rain_delay",
    )
    hass.states.async_set(delay.entity_id, "0", {})
    return source


@pytest.fixture
def scheduler_entry(
    hass: HomeAssistant, source_entry: MockConfigEntry
) -> MockConfigEntry:
    """An un-setup scheduler entry bound to the mock controller."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Rain Bird Scheduler",
        unique_id=f"rainbird:{SOURCE_UNIQUE_ID}",
        data={
            CONF_SOURCE_CONFIG_ENTRY_ID: source_entry.entry_id,
            CONF_SOURCE_UNIQUE_ID: SOURCE_UNIQUE_ID,
            CONF_AUTHORITY_MODE: "ha_authoritative",
        },
    )
    entry.add_to_hass(hass)
    return entry


async def setup_scheduler(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """Set up the scheduler entry and settle the event loop."""
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

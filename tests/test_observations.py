"""Temperature reading and normalization in build_observation."""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.rainbird_scheduler.observations import build_observation


@pytest.fixture(autouse=True)
def _custom_integrations(enable_custom_integrations: None) -> None:
    """Allow loading the custom integration in this module."""


def _temp(hass: HomeAssistant, now=None):
    return build_observation(
        hass,
        zone_entities={},
        rain_sensor_entity=None,
        rain_delay_entity=None,
        temperature_entity="sensor.t",
        now=now or dt_util.utcnow(),
    ).current_temperature_c


async def test_sensor_fahrenheit_converts_to_celsius(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set("sensor.t", "32", {"unit_of_measurement": "°F"})
    assert _temp(hass) == 0


async def test_sensor_celsius_passes_through(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.t", "5", {"unit_of_measurement": "°C"})
    assert _temp(hass) == 5


async def test_weather_entity_reads_attribute(hass: HomeAssistant) -> None:
    hass.states.async_set(
        "weather.home",
        "sunny",
        {"temperature": 30, "temperature_unit": "°F"},
    )
    observation = build_observation(
        hass,
        zone_entities={},
        rain_sensor_entity=None,
        rain_delay_entity=None,
        temperature_entity="weather.home",
        now=dt_util.utcnow(),
    )
    assert observation.current_temperature_c is not None
    assert observation.current_temperature_c < 0  # 30 °F is below freezing


async def test_unavailable_source_is_unknown(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.t", "unavailable", {})
    assert _temp(hass) is None


async def test_non_numeric_source_is_unknown(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.t", "warm", {"unit_of_measurement": "°C"})
    assert _temp(hass) is None


async def test_stale_reading_is_unknown_and_flagged(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set("sensor.t", "1", {"unit_of_measurement": "°C"})
    # Evaluate two hours later than the state's timestamp.
    future = dt_util.utcnow() + timedelta(hours=2)
    observation = build_observation(
        hass,
        zone_entities={},
        rain_sensor_entity=None,
        rain_delay_entity=None,
        temperature_entity="sensor.t",
        now=future,
    )
    assert observation.current_temperature_c is None
    assert observation.temperature_stale is True


@pytest.mark.parametrize("raw", ["inf", "1e400", "-inf", "not-a-number"])
async def test_unparsable_rain_delay_is_unknown(
    hass: HomeAssistant, raw: str
) -> None:
    # int(float('inf')) raises OverflowError, which must not escape into
    # the state-change task.
    hass.states.async_set("number.rain_delay", raw, {})
    observation = build_observation(
        hass,
        zone_entities={},
        rain_sensor_entity=None,
        rain_delay_entity="number.rain_delay",
        now=dt_util.utcnow(),
    )
    assert observation.rain_delay_days is None

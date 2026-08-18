"""Temperature reading and normalization in build_observation."""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.rainbird_scheduler.models import (
    ControllerObservation,
    RainDelayStatus,
    TemperatureStatus,
)
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
    assert observation.temperature_status is TemperatureStatus.STALE


def _temp_observation(
    hass: HomeAssistant, entity: str | None = "sensor.t"
) -> ControllerObservation:
    return build_observation(
        hass,
        zone_entities={},
        rain_sensor_entity=None,
        rain_delay_entity=None,
        temperature_entity=entity,
        now=dt_util.utcnow(),
    )


async def test_temperature_status_distinguishes_causes(
    hass: HomeAssistant,
) -> None:
    """Each way of not knowing the temperature carries its own status."""
    # No source configured at all.
    assert (
        _temp_observation(hass, entity=None).temperature_status
        is TemperatureStatus.NO_ENTITY
    )

    # Configured but absent from the state machine / unavailable.
    assert (
        _temp_observation(hass).temperature_status
        is TemperatureStatus.UNAVAILABLE
    )
    hass.states.async_set("sensor.t", "unavailable", {})
    assert (
        _temp_observation(hass).temperature_status
        is TemperatureStatus.UNAVAILABLE
    )

    # Weather entity present but carrying no temperature attribute.
    hass.states.async_set("weather.home", "sunny", {})
    assert (
        _temp_observation(hass, entity="weather.home").temperature_status
        is TemperatureStatus.NO_VALUE
    )

    # Unparsable state.
    hass.states.async_set("sensor.t", "warm", {"unit_of_measurement": "°C"})
    assert (
        _temp_observation(hass).temperature_status
        is TemperatureStatus.INVALID
    )

    # Healthy read.
    hass.states.async_set("sensor.t", "5", {"unit_of_measurement": "°C"})
    observation = _temp_observation(hass)
    assert observation.temperature_status is TemperatureStatus.OK
    assert observation.current_temperature_c == 5


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
    assert observation.rain_delay_status is RainDelayStatus.INVALID


def _delay_observation(hass: HomeAssistant) -> ControllerObservation:
    return build_observation(
        hass,
        zone_entities={},
        rain_sensor_entity=None,
        rain_delay_entity="number.rain_delay",
        now=dt_util.utcnow(),
    )


async def test_rain_delay_status_distinguishes_causes(
    hass: HomeAssistant,
) -> None:
    """Each way of not knowing the delay carries its own status (no bare
    'unknown' conflating them)."""
    # No entity discovered at all.
    observation = build_observation(
        hass,
        zone_entities={},
        rain_sensor_entity=None,
        rain_delay_entity=None,
        now=dt_util.utcnow(),
    )
    assert observation.rain_delay_status is RainDelayStatus.NO_ENTITY

    # Entity id known but absent from the state machine.
    assert (
        _delay_observation(hass).rain_delay_status
        is RainDelayStatus.UNAVAILABLE
    )

    hass.states.async_set("number.rain_delay", "unavailable", {})
    assert (
        _delay_observation(hass).rain_delay_status
        is RainDelayStatus.UNAVAILABLE
    )

    # Exists but has not produced a value yet (post-restart, pre-poll).
    hass.states.async_set("number.rain_delay", "unknown", {})
    assert (
        _delay_observation(hass).rain_delay_status
        is RainDelayStatus.NOT_YET_READ
    )

    # Healthy read.
    hass.states.async_set("number.rain_delay", "2", {})
    observation = _delay_observation(hass)
    assert observation.rain_delay_status is RainDelayStatus.OK
    assert observation.rain_delay_days == 2

"""Observation building from source entity states (plan §19)."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from homeassistant.const import (
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.util.unit_conversion import TemperatureConverter

from .const import (
    FREEZE_TEMP_STALE_AFTER_SECONDS,
    OBSERVATION_FRESHNESS_WINDOW_SECONDS,
)
from .models import ControllerObservation, ObservationFreshness

_BAD_STATES = (STATE_UNAVAILABLE, STATE_UNKNOWN)


def _usable(state: State | None) -> bool:
    return state is not None and state.state not in _BAD_STATES


def _read_temperature_c(
    hass: HomeAssistant, entity_id: str, now: datetime
) -> tuple[Decimal | None, bool]:
    """Read a temperature source normalized to Celsius.

    Returns ``(celsius, stale)``. ``weather.*`` entities carry the temperature
    in an attribute; other domains put it in the state. A reading older than
    the staleness window returns ``(None, True)`` so the guard fails to
    "unknown" rather than trusting cold data from hours ago.
    """
    state = hass.states.get(entity_id)
    if not _usable(state):
        return None, False
    assert state is not None

    if entity_id.startswith("weather."):
        raw = state.attributes.get("temperature")
        unit = state.attributes.get("temperature_unit")
    else:
        raw = state.state
        unit = state.attributes.get("unit_of_measurement")
    if raw is None:
        return None, False
    if unit not in (UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT):
        # Missing/odd unit: assume the instance's configured unit system.
        unit = hass.config.units.temperature_unit

    try:
        celsius = TemperatureConverter.convert(
            float(raw), unit, UnitOfTemperature.CELSIUS
        )
        value = Decimal(str(celsius))
    except (ValueError, TypeError, InvalidOperation):
        return None, False

    reported = getattr(state, "last_reported", None) or state.last_updated
    if now - reported > timedelta(seconds=FREEZE_TEMP_STALE_AFTER_SECONDS):
        return None, True
    return value, False


def build_observation(
    hass: HomeAssistant,
    *,
    zone_entities: dict[str, str],
    rain_sensor_entity: str | None,
    rain_delay_entity: str | None,
    temperature_entity: str | None = None,
    now: datetime,
) -> ControllerObservation:
    """Snapshot what the source entities currently report."""
    active: set[str] = set()
    any_usable = False
    newest_report: datetime | None = None

    for zone_id, entity_id in zone_entities.items():
        state = hass.states.get(entity_id)
        if not _usable(state):
            continue
        any_usable = True
        assert state is not None
        reported = getattr(state, "last_reported", None) or state.last_updated
        if newest_report is None or reported > newest_report:
            newest_report = reported
        if state.state == STATE_ON:
            active.add(zone_id)

    rain_sensor_active: bool | None = None
    if rain_sensor_entity:
        state = hass.states.get(rain_sensor_entity)
        if _usable(state):
            assert state is not None
            rain_sensor_active = state.state == STATE_ON

    rain_delay_days: int | None = None
    if rain_delay_entity:
        state = hass.states.get(rain_delay_entity)
        if _usable(state):
            assert state is not None
            try:
                rain_delay_days = int(float(state.state))
            except (ValueError, OverflowError):
                # 'inf'/'1e400' overflow the int conversion.
                rain_delay_days = None

    current_temperature_c: Decimal | None = None
    temperature_stale = False
    if temperature_entity:
        current_temperature_c, temperature_stale = _read_temperature_c(
            hass, temperature_entity, now
        )

    if newest_report is None:
        freshness = ObservationFreshness.UNKNOWN
    elif now - newest_report <= timedelta(
        seconds=OBSERVATION_FRESHNESS_WINDOW_SECONDS
    ):
        freshness = ObservationFreshness.FRESH
    else:
        freshness = ObservationFreshness.STALE

    return ControllerObservation(
        observed_at_utc=now,
        active_zone_ids=frozenset(active),
        rain_sensor_active=rain_sensor_active,
        rain_delay_days=rain_delay_days,
        source_available=any_usable,
        freshness=freshness,
        current_temperature_c=current_temperature_c,
        temperature_stale=temperature_stale,
    )

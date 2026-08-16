"""Observation building from source entity states (plan §19)."""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State

from .const import OBSERVATION_FRESHNESS_WINDOW_SECONDS
from .models import ControllerObservation, ObservationFreshness

_BAD_STATES = (STATE_UNAVAILABLE, STATE_UNKNOWN)


def _usable(state: State | None) -> bool:
    return state is not None and state.state not in _BAD_STATES


def build_observation(
    hass: HomeAssistant,
    *,
    zone_entities: dict[str, str],
    rain_sensor_entity: str | None,
    rain_delay_entity: str | None,
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
            except ValueError:
                rain_delay_days = None

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
    )

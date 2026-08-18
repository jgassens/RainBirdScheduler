"""Production HA-entity driver: service-call failures become DriverErrors."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceRegistry
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rainbird_scheduler.driver.base import (
    CommandUncertainError,
)
from custom_components.rainbird_scheduler.driver.ha_entity import (
    HomeAssistantEntityDriver,
)
from custom_components.rainbird_scheduler.models import (
    ControllerObservation,
    ObservationFreshness,
    ZoneReference,
)

NOW = datetime(2026, 6, 3, 14, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _custom_integrations(enable_custom_integrations: None) -> None:
    """Allow loading the custom integration in this module."""


def _zone_reference(
    hass: HomeAssistant, source: MockConfigEntry, station: int = 1
) -> ZoneReference:
    registry = er.async_get(hass)
    entry = registry.async_get(f"switch.rain_bird_sprinkler_{station}")
    assert entry is not None
    return ZoneReference(
        source_unique_id=source.unique_id or "",
        source_config_entry_id=source.entry_id,
        entity_registry_id=entry.id,
        station_number=station,
        last_known_entity_id=entry.entity_id,
    )


def _driver(hass: HomeAssistant, entity_id: str) -> HomeAssistantEntityDriver:
    return HomeAssistantEntityDriver(
        hass,
        any_zone_entity=lambda: entity_id,
        observe=lambda: ControllerObservation(
            observed_at_utc=NOW,
            active_zone_ids=frozenset(),
            rain_sensor_active=False,
            rain_delay_days=0,
            source_available=True,
            freshness=ObservationFreshness.FRESH,
        ),
        now_fn=lambda: NOW,
    )


@pytest.fixture
def patch_async_call(monkeypatch: pytest.MonkeyPatch):
    """Patch the service call; ServiceRegistry uses __slots__."""

    def _apply(error: BaseException) -> None:
        async def _call(*args: Any, **kwargs: Any) -> None:
            raise error

        monkeypatch.setattr(ServiceRegistry, "async_call", _call)

    return _apply


async def test_start_zone_timeout_becomes_command_uncertain(
    hass: HomeAssistant,
    source_entry: MockConfigEntry,
    patch_async_call,
) -> None:
    # The LNK module accepting TCP then hanging surfaces as a plain
    # TimeoutError out of the blocking service call.
    reference = _zone_reference(hass, source_entry)
    patch_async_call(TimeoutError("LNK hung"))
    driver = _driver(hass, reference.last_known_entity_id)
    with pytest.raises(CommandUncertainError) as excinfo:
        await driver.async_start_zone(reference, 5, "cmd-1")
    assert isinstance(excinfo.value.__cause__, TimeoutError)


async def test_stop_controller_timeout_becomes_command_uncertain(
    hass: HomeAssistant,
    source_entry: MockConfigEntry,
    patch_async_call,
) -> None:
    reference = _zone_reference(hass, source_entry)
    patch_async_call(TimeoutError("LNK hung"))
    driver = _driver(hass, reference.last_known_entity_id)
    with pytest.raises(CommandUncertainError) as excinfo:
        await driver.async_stop_controller()
    assert isinstance(excinfo.value.__cause__, TimeoutError)


async def test_home_assistant_error_still_maps_to_command_uncertain(
    hass: HomeAssistant,
    source_entry: MockConfigEntry,
    patch_async_call,
) -> None:
    reference = _zone_reference(hass, source_entry)
    patch_async_call(HomeAssistantError("service failed"))
    driver = _driver(hass, reference.last_known_entity_id)
    with pytest.raises(CommandUncertainError) as excinfo:
        await driver.async_start_zone(reference, 5, "cmd-1")
    assert isinstance(excinfo.value.__cause__, HomeAssistantError)


async def test_cancellation_propagates_unwrapped(
    hass: HomeAssistant,
    source_entry: MockConfigEntry,
    patch_async_call,
) -> None:
    reference = _zone_reference(hass, source_entry)
    patch_async_call(asyncio.CancelledError())
    driver = _driver(hass, reference.last_known_entity_id)
    with pytest.raises(asyncio.CancelledError):
        await driver.async_start_zone(reference, 5, "cmd-1")

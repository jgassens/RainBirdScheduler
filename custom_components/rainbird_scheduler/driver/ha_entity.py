"""Production driver: drives irrigation through Home Assistant entities.

This backend never talks to the controller. It calls the core rainbird
integration's public action for exactly one zone at a time, stops the whole
controller through ``switch.turn_off``, and observes through entity states
(plan §3.1, §7).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from ..const import SOURCE_DOMAIN
from ..models import (
    CommandResult,
    ControllerObservation,
    DriverCapabilities,
    ZoneReference,
)
from .base import CommandUncertainError, ZoneValidationError

_LOGGER = logging.getLogger(__name__)

ATTR_ZONE = "zone"


def resolve_zone_entity(
    hass: HomeAssistant, reference: ZoneReference
) -> str:
    """Resolve and validate a zone reference at execution time (plan §8)."""
    registry = er.async_get(hass)
    entry = registry.async_get(reference.entity_registry_id)
    if entry is None:
        raise ZoneValidationError(
            f"Zone entity for station {reference.station_number} no longer "
            "exists in the entity registry"
        )
    if entry.platform != SOURCE_DOMAIN:
        raise ZoneValidationError(
            f"Entity {entry.entity_id} is no longer provided by the "
            f"{SOURCE_DOMAIN} integration"
        )
    if entry.config_entry_id != reference.source_config_entry_id:
        raise ZoneValidationError(
            f"Entity {entry.entity_id} no longer belongs to the expected "
            "Rain Bird controller"
        )
    state = hass.states.get(entry.entity_id)
    if state is not None:
        zone_attr = state.attributes.get(ATTR_ZONE)
        if zone_attr is not None and int(zone_attr) != reference.station_number:
            raise ZoneValidationError(
                f"Entity {entry.entity_id} now reports station "
                f"{zone_attr}, expected {reference.station_number}"
            )
    return entry.entity_id


class HomeAssistantEntityDriver:
    """Version 1 execution backend (plan §3.1)."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        any_zone_entity: Callable[[], str | None],
        observe: Callable[[], ControllerObservation],
        now_fn: Callable[[], datetime],
    ) -> None:
        self._hass = hass
        self._any_zone_entity = any_zone_entity
        self._observe = observe
        self._now = now_fn

    @property
    def capabilities(self) -> DriverCapabilities:
        return DriverCapabilities(
            stack_manual_runs=False, read_current_queue=False
        )

    async def async_start_zone(
        self,
        zone: ZoneReference,
        duration_minutes: int,
        command_id: str,
    ) -> CommandResult:
        entity_id = resolve_zone_entity(self._hass, zone)
        try:
            # Always exactly one entity target; broad targets could resolve
            # to multiple switches and create simultaneous commands (§7).
            await self._hass.services.async_call(
                SOURCE_DOMAIN,
                "start_irrigation",
                {
                    "entity_id": entity_id,
                    "duration": duration_minutes,
                },
                blocking=True,
            )
        except HomeAssistantError as err:
            raise CommandUncertainError(str(err)) from err
        except Exception as err:
            # The source integration can leak non-HA errors out of the
            # service call (aiohttp total timeouts, pyrainbird decode
            # errors). The executor only handles DriverError, and an
            # unhandled exception would strand the journal with no timers
            # armed; treat the outcome as uncertain and reconcile.
            raise CommandUncertainError(str(err)) from err
        _LOGGER.debug(
            "Started %s for %s min (command %s)",
            entity_id,
            duration_minutes,
            command_id,
        )
        return CommandResult(ok=True)

    async def async_stop_controller(self) -> CommandResult:
        """Stop ALL controller watering (a zone switch off is global, §7)."""
        entity_id = self._any_zone_entity()
        if entity_id is None:
            raise CommandUncertainError(
                "No Rain Bird zone entity available to stop the controller"
            )
        try:
            await self._hass.services.async_call(
                "switch",
                "turn_off",
                {"entity_id": entity_id},
                blocking=True,
            )
        except HomeAssistantError as err:
            raise CommandUncertainError(str(err)) from err
        except Exception as err:
            # Same leak surface as async_start_zone: unexpected errors mean
            # the stop may not have landed, so reconcile before retrying.
            raise CommandUncertainError(str(err)) from err
        return CommandResult(ok=True)

    async def async_observe(self) -> ControllerObservation:
        return self._observe()

    async def async_request_observation(self) -> None:
        """Best-effort active refresh; failures never fail the run (§19)."""
        entity_id = self._any_zone_entity()
        if entity_id is None:
            return
        try:
            await self._hass.services.async_call(
                "homeassistant",
                "update_entity",
                {"entity_id": entity_id},
                blocking=False,
            )
        except HomeAssistantError as err:
            _LOGGER.debug("Best-effort refresh failed: %s", err)

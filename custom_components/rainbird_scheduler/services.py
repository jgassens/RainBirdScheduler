"""Public actions (plan §34)."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_DAYS,
    ATTR_DURATION,
    ATTR_ENABLED,
    ATTR_ENTITY_ID,
    ATTR_PROGRAM_ID,
    ATTR_ZONES,
    DOMAIN,
    SERVICE_PAUSE,
    SERVICE_RECALCULATE,
    SERVICE_RESUME,
    SERVICE_RUN_PROGRAM,
    SERVICE_RUN_ZONES,
    SERVICE_SET_PROGRAM_ENABLED,
    SERVICE_SET_RAIN_DELAY,
    SERVICE_SKIP_CURRENT,
    SERVICE_STOP_CONTROLLER,
)
from .coordinator import SchedulerCoordinator
from .executor import ControllerBusyError

_ZONE_SPEC = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Required(ATTR_DURATION): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=1440)
        ),
    }
)

_ENTRY_SCHEMA = vol.Schema(
    {vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string}
)


def _loaded_coordinators(hass: HomeAssistant) -> list[SchedulerCoordinator]:
    return [
        entry.runtime_data
        for entry in hass.config_entries.async_loaded_entries(DOMAIN)
    ]


def _resolve(
    hass: HomeAssistant, entry_id: str | None
) -> SchedulerCoordinator:
    coordinators = _loaded_coordinators(hass)
    if entry_id:
        for coordinator in coordinators:
            if coordinator.entry.entry_id == entry_id:
                return coordinator
        raise HomeAssistantError(f"Unknown scheduler entry: {entry_id}")
    if len(coordinators) == 1:
        return coordinators[0]
    if not coordinators:
        raise HomeAssistantError("No Rain Bird Scheduler is configured")
    raise HomeAssistantError(
        "Multiple schedulers exist; specify config_entry_id"
    )


def _resolve_by_program(
    hass: HomeAssistant, entry_id: str | None, program_id: str
) -> SchedulerCoordinator:
    if entry_id:
        return _resolve(hass, entry_id)
    matches = [
        coordinator
        for coordinator in _loaded_coordinators(hass)
        if program_id in coordinator.config.programs
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise HomeAssistantError(f"Unknown program: {program_id}")
    raise HomeAssistantError(
        "Program id exists on multiple controllers; specify config_entry_id"
    )


@callback
def async_register_services(hass: HomeAssistant) -> None:
    """Register domain services once."""

    async def run_program(call: ServiceCall) -> None:
        program_id = call.data[ATTR_PROGRAM_ID]
        coordinator = _resolve_by_program(
            hass, call.data.get(ATTR_CONFIG_ENTRY_ID), program_id
        )
        try:
            await coordinator.async_run_program(program_id)
        except ControllerBusyError as err:
            raise HomeAssistantError(str(err)) from err

    async def run_zones(call: ServiceCall) -> None:
        zones = call.data[ATTR_ZONES]
        owners: set[str] = set()
        owner: SchedulerCoordinator | None = None
        for spec in zones:
            entity_id = spec[ATTR_ENTITY_ID]
            for coordinator in _loaded_coordinators(hass):
                if entity_id in coordinator._entity_to_zone:
                    owners.add(coordinator.entry.entry_id)
                    owner = coordinator
                    break
            else:
                raise HomeAssistantError(
                    f"{entity_id} is not a Rain Bird zone of any "
                    "scheduled controller"
                )
        if len(owners) > 1:
            # Never span controllers: that is exactly the simultaneous
            # command condition the scheduler exists to prevent.
            raise HomeAssistantError(
                "Zones span more than one Rain Bird controller"
            )
        assert owner is not None
        try:
            await owner.async_run_zones(list(zones))
        except ControllerBusyError as err:
            raise HomeAssistantError(str(err)) from err

    async def stop_controller(call: ServiceCall) -> None:
        coordinator = _resolve(hass, call.data.get(ATTR_CONFIG_ENTRY_ID))
        await coordinator.executor.async_stop()

    async def pause(call: ServiceCall) -> None:
        coordinator = _resolve(hass, call.data.get(ATTR_CONFIG_ENTRY_ID))
        await coordinator.executor.async_pause()

    async def resume(call: ServiceCall) -> None:
        coordinator = _resolve(hass, call.data.get(ATTR_CONFIG_ENTRY_ID))
        await coordinator.executor.async_resume()

    async def skip_current(call: ServiceCall) -> None:
        coordinator = _resolve(hass, call.data.get(ATTR_CONFIG_ENTRY_ID))
        await coordinator.executor.async_skip_current()

    async def recalculate(call: ServiceCall) -> None:
        coordinator = _resolve(hass, call.data.get(ATTR_CONFIG_ENTRY_ID))
        await coordinator.async_recalculate()

    async def set_program_enabled(call: ServiceCall) -> None:
        program_id = call.data[ATTR_PROGRAM_ID]
        coordinator = _resolve_by_program(
            hass, call.data.get(ATTR_CONFIG_ENTRY_ID), program_id
        )
        program = coordinator.config.programs.get(program_id)
        if program is None:
            raise HomeAssistantError(f"Unknown program: {program_id}")
        await coordinator.async_update_program(
            program_id,
            {"enabled": call.data[ATTR_ENABLED]},
            expected_revision=program.revision,
        )

    async def set_rain_delay(call: ServiceCall) -> None:
        coordinator = _resolve(hass, call.data.get(ATTR_CONFIG_ENTRY_ID))
        await coordinator.async_set_rain_delay(call.data[ATTR_DAYS])

    hass.services.async_register(
        DOMAIN,
        SERVICE_RUN_PROGRAM,
        run_program,
        schema=_ENTRY_SCHEMA.extend(
            {vol.Required(ATTR_PROGRAM_ID): cv.string}
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RUN_ZONES,
        run_zones,
        schema=vol.Schema(
            {
                vol.Required(ATTR_ZONES): vol.All(
                    cv.ensure_list, [_ZONE_SPEC], vol.Length(min=1)
                )
            }
        ),
    )
    hass.services.async_register(
        DOMAIN, SERVICE_STOP_CONTROLLER, stop_controller, schema=_ENTRY_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_PAUSE, pause, schema=_ENTRY_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RESUME, resume, schema=_ENTRY_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SKIP_CURRENT, skip_current, schema=_ENTRY_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RECALCULATE, recalculate, schema=_ENTRY_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_PROGRAM_ENABLED,
        set_program_enabled,
        schema=_ENTRY_SCHEMA.extend(
            {
                vol.Required(ATTR_PROGRAM_ID): cv.string,
                vol.Required(ATTR_ENABLED): cv.boolean,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_RAIN_DELAY,
        set_rain_delay,
        schema=_ENTRY_SCHEMA.extend(
            {
                vol.Required(ATTR_DAYS): vol.All(
                    vol.Coerce(int), vol.Range(min=0, max=14)
                )
            }
        ),
    )

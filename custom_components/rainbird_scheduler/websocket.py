"""WebSocket API for the panel (plan §33).

Reads are open to any authenticated user; every mutation requires an
administrator. All mutable objects carry revisions and updates use
optimistic concurrency.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from . import serde
from .const import DOMAIN
from .coordinator import (
    SIGNAL_CONFIG,
    SIGNAL_LIFECYCLE,
    SIGNAL_STATE,
    RevisionConflictError,
    SchedulerCoordinator,
)
from .executor import ControllerBusyError
from .models import AuthorityMode

ERR_NOT_FOUND = "entry_not_found"
ERR_REVISION = "revision_conflict"
ERR_BUSY = "controller_busy"
ERR_ACK = "authority_ack_required"


def _coordinator(
    hass: HomeAssistant, entry_id: str
) -> SchedulerCoordinator | None:
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        if entry.entry_id == entry_id:
            return cast(SchedulerCoordinator, entry.runtime_data)
    return None


_Handler = Callable[
    [
        "HomeAssistant",
        "websocket_api.ActiveConnection",
        dict[str, Any],
        SchedulerCoordinator,
    ],
    Awaitable[None],
]


def _with_coordinator(
    handler: _Handler,
) -> Callable[
    [HomeAssistant, websocket_api.ActiveConnection, dict[str, Any]], None
]:
    """Resolve the coordinator or send entry_not_found."""

    @websocket_api.async_response
    async def wrapper(
        hass: HomeAssistant,
        connection: websocket_api.ActiveConnection,
        msg: dict[str, Any],
    ) -> None:
        coordinator = _coordinator(hass, msg["entry_id"])
        if coordinator is None:
            connection.send_error(
                msg["id"], ERR_NOT_FOUND, "Unknown scheduler entry"
            )
            return
        try:
            await handler(hass, connection, msg, coordinator)
        except RevisionConflictError as err:
            connection.send_error(
                msg["id"],
                ERR_REVISION,
                f"Revision conflict; current revision is "
                f"{err.current_revision}",
            )
        except ControllerBusyError as err:
            connection.send_error(msg["id"], ERR_BUSY, str(err))
        except HomeAssistantError as err:
            connection.send_error(msg["id"], "invalid_request", str(err))

    return wrapper


def _snapshot(coordinator: SchedulerCoordinator) -> dict[str, Any]:
    journal = coordinator.executor.journal
    active_step = coordinator.active_step()
    next_run = coordinator.next_pending_run()
    observation = coordinator.last_observation
    return {
        "executor_state": journal.state.value,
        "active_run": serde.dump(journal.run_plan),
        "active_step_index": journal.current_step_index,
        "active_zone": active_step.zone_name if active_step else None,
        "expected_end": serde.dump(journal.current_step_expected_end),
        "paused_reason": journal.paused_reason,
        "next_run": serde.dump(next_run),
        "external_watering": coordinator.external_watering,
        "native_schedule_conflict": coordinator.native_schedule_conflict,
        "source_available": coordinator.source_available,
        "observation": serde.dump(observation),
    }


@callback
def async_register_websocket_commands(hass: HomeAssistant) -> None:
    """Register all commands once."""
    for command in (
        ws_entries,
        ws_config_get,
        ws_config_update,
        ws_program_list,
        ws_program_create,
        ws_program_update,
        ws_program_delete,
        ws_program_duplicate,
        ws_zone_list,
        ws_zone_update,
        ws_plan_preview,
        ws_run_start,
        ws_run_start_zones,
        ws_run_stop,
        ws_run_pause,
        ws_run_resume,
        ws_run_skip_current,
        ws_history_list,
        ws_diagnostics_get,
        ws_subscribe,
    ):
        websocket_api.async_register_command(hass, command)


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/entries"})
@callback
def ws_entries(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    connection.send_result(
        msg["id"],
        [
            {"entry_id": entry.entry_id, "title": entry.title}
            for entry in hass.config_entries.async_loaded_entries(DOMAIN)
        ],
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/config/get",
        vol.Required("entry_id"): str,
    }
)
@_with_coordinator
async def ws_config_get(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    coordinator: SchedulerCoordinator,
) -> None:
    connection.send_result(
        msg["id"],
        {
            "controller": serde.dump(coordinator.config.controller),
            "zones": serde.dump(coordinator.config.zones),
            "programs": serde.dump(coordinator.config.programs),
        },
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/config/update",
        vol.Required("entry_id"): str,
        vol.Required("expected_revision"): int,
        vol.Required("patch"): dict,
        vol.Optional("acknowledge_authority_change", default=False): bool,
    }
)
@_with_coordinator
async def ws_config_update(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    coordinator: SchedulerCoordinator,
) -> None:
    patch = msg["patch"]
    if "authority_mode" in patch:
        new_mode = AuthorityMode(patch["authority_mode"])
        if (
            new_mode is not coordinator.config.controller.authority_mode
            and not msg["acknowledge_authority_change"]
        ):
            connection.send_error(
                msg["id"],
                ERR_ACK,
                "Changing the authority mode requires "
                "acknowledge_authority_change: true",
            )
            return
    updated = await coordinator.async_update_controller(
        patch, msg["expected_revision"]
    )
    connection.send_result(msg["id"], {"controller": serde.dump(updated)})


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/program/list",
        vol.Required("entry_id"): str,
    }
)
@_with_coordinator
async def ws_program_list(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    coordinator: SchedulerCoordinator,
) -> None:
    connection.send_result(
        msg["id"], {"programs": serde.dump(coordinator.config.programs)}
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/program/create",
        vol.Required("entry_id"): str,
        vol.Required("program"): dict,
    }
)
@_with_coordinator
async def ws_program_create(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    coordinator: SchedulerCoordinator,
) -> None:
    try:
        program = await coordinator.async_create_program(msg["program"])
    except (TypeError, ValueError, KeyError) as err:
        connection.send_error(msg["id"], "invalid_program", str(err))
        return
    connection.send_result(msg["id"], {"program": serde.dump(program)})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/program/update",
        vol.Required("entry_id"): str,
        vol.Required("program_id"): str,
        vol.Required("expected_revision"): int,
        vol.Required("patch"): dict,
    }
)
@_with_coordinator
async def ws_program_update(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    coordinator: SchedulerCoordinator,
) -> None:
    try:
        program = await coordinator.async_update_program(
            msg["program_id"], msg["patch"], msg["expected_revision"]
        )
    except (TypeError, ValueError, KeyError) as err:
        connection.send_error(msg["id"], "invalid_program", str(err))
        return
    connection.send_result(msg["id"], {"program": serde.dump(program)})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/program/delete",
        vol.Required("entry_id"): str,
        vol.Required("program_id"): str,
    }
)
@_with_coordinator
async def ws_program_delete(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    coordinator: SchedulerCoordinator,
) -> None:
    await coordinator.async_delete_program(msg["program_id"])
    connection.send_result(msg["id"], {})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/program/duplicate",
        vol.Required("entry_id"): str,
        vol.Required("program_id"): str,
    }
)
@_with_coordinator
async def ws_program_duplicate(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    coordinator: SchedulerCoordinator,
) -> None:
    program = await coordinator.async_duplicate_program(msg["program_id"])
    connection.send_result(msg["id"], {"program": serde.dump(program)})


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/zone/list",
        vol.Required("entry_id"): str,
    }
)
@_with_coordinator
async def ws_zone_list(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    coordinator: SchedulerCoordinator,
) -> None:
    zones = serde.dump(coordinator.config.zones)
    for zone_id, payload in zones.items():
        payload["entity_id"] = coordinator._zone_to_entity.get(
            zone_id
        )
    connection.send_result(msg["id"], {"zones": zones})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/zone/update",
        vol.Required("entry_id"): str,
        vol.Required("zone_id"): str,
        vol.Required("expected_revision"): int,
        vol.Required("patch"): dict,
    }
)
@_with_coordinator
async def ws_zone_update(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    coordinator: SchedulerCoordinator,
) -> None:
    try:
        zone = await coordinator.async_update_zone(
            msg["zone_id"], msg["patch"], msg["expected_revision"]
        )
    except (TypeError, ValueError, KeyError) as err:
        connection.send_error(msg["id"], "invalid_zone", str(err))
        return
    connection.send_result(msg["id"], {"zone": serde.dump(zone)})


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/plan/preview",
        vol.Required("entry_id"): str,
    }
)
@_with_coordinator
async def ws_plan_preview(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    coordinator: SchedulerCoordinator,
) -> None:
    connection.send_result(
        msg["id"],
        {
            "timeline": serde.dump(coordinator.timeline),
            "state": _snapshot(coordinator),
        },
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/run/start",
        vol.Required("entry_id"): str,
        vol.Required("program_id"): str,
    }
)
@_with_coordinator
async def ws_run_start(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    coordinator: SchedulerCoordinator,
) -> None:
    await coordinator.async_run_program(msg["program_id"])
    connection.send_result(msg["id"], {})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/run/start_zones",
        vol.Required("entry_id"): str,
        vol.Required("zones"): [
            {
                vol.Required("entity_id"): str,
                vol.Required("duration"): vol.All(
                    int, vol.Range(min=1, max=1440)
                ),
            }
        ],
    }
)
@_with_coordinator
async def ws_run_start_zones(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    coordinator: SchedulerCoordinator,
) -> None:
    await coordinator.async_run_zones(msg["zones"])
    connection.send_result(msg["id"], {})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/run/stop",
        vol.Required("entry_id"): str,
    }
)
@_with_coordinator
async def ws_run_stop(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    coordinator: SchedulerCoordinator,
) -> None:
    await coordinator.executor.async_stop()
    connection.send_result(msg["id"], {})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/run/pause",
        vol.Required("entry_id"): str,
    }
)
@_with_coordinator
async def ws_run_pause(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    coordinator: SchedulerCoordinator,
) -> None:
    await coordinator.executor.async_pause()
    connection.send_result(msg["id"], {})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/run/resume",
        vol.Required("entry_id"): str,
    }
)
@_with_coordinator
async def ws_run_resume(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    coordinator: SchedulerCoordinator,
) -> None:
    await coordinator.executor.async_resume()
    connection.send_result(msg["id"], {})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/run/skip_current",
        vol.Required("entry_id"): str,
    }
)
@_with_coordinator
async def ws_run_skip_current(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    coordinator: SchedulerCoordinator,
) -> None:
    await coordinator.executor.async_skip_current()
    connection.send_result(msg["id"], {})


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/history/list",
        vol.Required("entry_id"): str,
        vol.Optional("limit", default=50): vol.All(
            int, vol.Range(min=1, max=250)
        ),
    }
)
@_with_coordinator
async def ws_history_list(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    coordinator: SchedulerCoordinator,
) -> None:
    history = coordinator.history.history
    limit = msg["limit"]
    runs = history.runs[-limit:]
    run_ids = {run.run_id for run in runs}
    connection.send_result(
        msg["id"],
        {
            "runs": serde.dump(list(reversed(runs))),
            "zone_records": serde.dump(
                [
                    record
                    for record in history.zone_records
                    if record.run_id in run_ids
                ]
            ),
            "interventions": serde.dump(history.interventions[-limit:]),
        },
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/diagnostics/get",
        vol.Required("entry_id"): str,
    }
)
@_with_coordinator
async def ws_diagnostics_get(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    coordinator: SchedulerCoordinator,
) -> None:
    from .diagnostics import build_diagnostics

    connection.send_result(msg["id"], build_diagnostics(coordinator))


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/subscribe",
        vol.Required("entry_id"): str,
    }
)
@_with_coordinator
async def ws_subscribe(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    coordinator: SchedulerCoordinator,
) -> None:
    entry_id = coordinator.entry.entry_id

    @callback
    def push_state() -> None:
        connection.send_message(
            websocket_api.event_message(
                msg["id"], {"kind": "state", "state": _snapshot(coordinator)}
            )
        )

    @callback
    def push_config() -> None:
        connection.send_message(
            websocket_api.event_message(msg["id"], {"kind": "config"})
        )

    @callback
    def push_lifecycle(event_type: str, data: dict[str, Any]) -> None:
        connection.send_message(
            websocket_api.event_message(
                msg["id"],
                {"kind": "lifecycle", "event": event_type, "data": data},
            )
        )

    unsubs = [
        async_dispatcher_connect(
            hass, SIGNAL_STATE.format(entry_id), push_state
        ),
        async_dispatcher_connect(
            hass, SIGNAL_CONFIG.format(entry_id), push_config
        ),
        async_dispatcher_connect(
            hass, SIGNAL_LIFECYCLE.format(entry_id), push_lifecycle
        ),
    ]

    @callback
    def unsubscribe() -> None:
        for unsub in unsubs:
            unsub()

    connection.subscriptions[msg["id"]] = unsubscribe
    connection.send_result(msg["id"])
    push_state()

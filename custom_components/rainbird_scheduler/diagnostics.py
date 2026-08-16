"""Diagnostics (plan §31 Diagnostics section)."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import SchedulerConfigEntry, serde
from .coordinator import SchedulerCoordinator
from .driver import NATIVE_QUEUE_ENABLED


def build_diagnostics(coordinator: SchedulerCoordinator) -> dict[str, Any]:
    """Assemble the diagnostics payload (also served over WebSocket)."""
    journal = coordinator.executor.journal
    source_entry = coordinator.hass.config_entries.async_get_entry(
        coordinator.source_entry_id
    )
    return {
        "source": {
            "config_entry_id": coordinator.source_entry_id,
            "exists": source_entry is not None,
            "state": str(source_entry.state) if source_entry else None,
            "title": source_entry.title if source_entry else None,
            "unique_id": source_entry.unique_id if source_entry else None,
        },
        "controller_config": serde.dump(coordinator.config.controller),
        "zones": {
            zone_id: {
                "display_name": zone.display_name,
                "station": zone.reference.station_number,
                "entity_id": coordinator._zone_to_entity.get(
                    zone_id
                ),
                "enabled": zone.enabled,
            }
            for zone_id, zone in coordinator.config.zones.items()
        },
        "programs": {
            program_id: {
                "name": program.name,
                "enabled": program.enabled,
                "revision": program.revision,
                "zone_count": len(program.zone_steps),
            }
            for program_id, program in coordinator.config.programs.items()
        },
        "journal": {
            "state": journal.state.value,
            "active_occurrence_id": journal.active_occurrence_id,
            "current_step_index": journal.current_step_index,
            "expected_end": serde.dump(journal.current_step_expected_end),
            "retry_count": journal.retry_count,
            "uncertain_count": journal.uncertain_count,
            "dedup_window_size": len(journal.completed_occurrences),
            "updated_at": serde.dump(journal.updated_at_utc),
        },
        "last_observation": serde.dump(coordinator.last_observation),
        "timeline": {
            "compiled_runs": len(coordinator.timeline.runs),
            "conflicts": serde.dump(list(coordinator.timeline.conflicts)),
            "warnings": serde.dump(list(coordinator.timeline.warnings)),
        },
        "history_counts": {
            "runs": len(coordinator.history.history.runs),
            "zone_records": len(coordinator.history.history.zone_records),
            "interventions": len(coordinator.history.history.interventions),
        },
        "capabilities": {
            "native_queue_enabled": NATIVE_QUEUE_ENABLED,
            "manual_run_budget_behavior": (
                coordinator.config.controller.manual_run_budget_behavior.value
            ),
        },
        "notes": [
            "The LNK module accepts one request at a time. Controller busy "
            "or app activity may delay commands. Avoid leaving the Rain "
            "Bird app actively communicating during scheduled runs.",
        ],
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SchedulerConfigEntry
) -> dict[str, Any]:
    """Home Assistant diagnostics download."""
    coordinator = entry.runtime_data
    payload = build_diagnostics(coordinator)
    payload["history"] = serde.dump(coordinator.history.history)
    return payload

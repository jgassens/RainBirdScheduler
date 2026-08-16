"""Storage: round-trips, corruption tolerance, bounded rings (plan §11)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from homeassistant.core import HomeAssistant

from custom_components.rainbird_scheduler.const import (
    HISTORY_MAX_RUNS,
    config_store_key,
    journal_store_key,
)
from custom_components.rainbird_scheduler.models import (
    ConfigData,
    ExecutionJournal,
    ExecutorState,
    ProviderKind,
    RunOutcome,
)
from custom_components.rainbird_scheduler.storage import (
    HistoryRecorder,
    SchedulerStorage,
)

from .helpers import make_controller, make_program, make_zone

NOW = datetime(2026, 6, 3, 14, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _custom_integrations(enable_custom_integrations: None) -> None:
    """Allow loading the custom integration in this module."""


async def test_config_roundtrip(hass: HomeAssistant) -> None:
    storage = SchedulerStorage(hass, "entry1")
    config = ConfigData(
        controller=make_controller(),
        zones={"z": make_zone("z", 1)},
        programs={"p": make_program("p", ["z"])},
    )
    await storage.async_save_config(config)
    loaded = await SchedulerStorage(hass, "entry1").async_load_config()
    assert loaded == config


async def test_journal_roundtrip_and_immediate_write(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    storage = SchedulerStorage(hass, "entry1")
    journal = ExecutionJournal(state=ExecutorState.WATERING)
    journal.completed_occurrences["occ"] = NOW
    await storage.async_write_journal_now(journal)
    # The write is immediate: it is already in the store backend.
    assert journal_store_key("entry1") in hass_storage
    loaded = await SchedulerStorage(hass, "entry1").async_load_journal()
    assert loaded.state is ExecutorState.WATERING
    assert loaded.completed_occurrences == {"occ": NOW}


async def test_corrupted_stores_fall_back_to_defaults(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    hass_storage[config_store_key("entry1")] = {
        "version": 1,
        "minor_version": 1,
        "key": config_store_key("entry1"),
        "data": {"controller": {"nonsense": True}},
    }
    hass_storage[journal_store_key("entry1")] = {
        "version": 1,
        "minor_version": 1,
        "key": journal_store_key("entry1"),
        "data": {"state": "not_a_state"},
    }
    storage = SchedulerStorage(hass, "entry1")
    assert await storage.async_load_config() is None
    journal = await storage.async_load_journal()
    assert journal.state is ExecutorState.IDLE


async def test_history_ring_is_bounded(hass: HomeAssistant) -> None:
    storage = SchedulerStorage(hass, "entry1")
    history = await storage.async_load_history()
    recorder = HistoryRecorder(history, storage, lambda: NOW)
    for index in range(HISTORY_MAX_RUNS + 25):
        recorder.record_run_skipped(
            run_id=f"run-{index}",
            occurrence_id=f"occ-{index}",
            program_id="p",
            program_name="P",
            requested_start_utc=NOW,
            reason="rain_delay",
            provider_kind=ProviderKind.FIXED,
        )
    assert len(history.runs) == HISTORY_MAX_RUNS
    # Oldest entries were trimmed; the newest survived.
    assert history.runs[-1].run_id == f"run-{HISTORY_MAX_RUNS + 24}"
    assert history.runs[0].run_id == "run-25"
    assert history.runs[0].outcome is RunOutcome.SKIPPED


async def test_flush_persists_everything_immediately(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    storage = SchedulerStorage(hass, "entry1")
    history = await storage.async_load_history()
    recorder = HistoryRecorder(history, storage, lambda: NOW)
    recorder.record_intervention("test", "message")
    journal = ExecutionJournal()
    await storage.async_flush(journal, history)
    stored = hass_storage["rainbird_scheduler.entry1.history"]
    assert stored["data"]["interventions"][0]["message"] == "message"

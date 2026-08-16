"""Persistent storage (plan §11): config, execution journal, history.

Three versioned stores per config entry. Journal writes are immediate and
serialized behind a lock; history and configuration use the delayed-save
path only where the plan permits it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from . import serde
from .const import (
    HISTORY_MAX_INTERVENTIONS,
    HISTORY_MAX_RUNS,
    HISTORY_MAX_ZONE_RECORDS,
    STORAGE_VERSION_CONFIG,
    STORAGE_VERSION_HISTORY,
    STORAGE_VERSION_JOURNAL,
    config_store_key,
    history_store_key,
    journal_store_key,
)
from .models import (
    ConfigData,
    ExecutionJournal,
    HistoryData,
    InterventionRecord,
    ProviderKind,
    RunOutcome,
    RunRecord,
    ZoneRecord,
)

_LOGGER = logging.getLogger(__name__)

HISTORY_SAVE_DELAY_SECONDS = 10.0


class SchedulerStorage:
    """Owns the three stores for one scheduler config entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._config_store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION_CONFIG, config_store_key(entry_id)
        )
        self._journal_store: Store[dict[str, Any]] = Store(
            hass,
            STORAGE_VERSION_JOURNAL,
            journal_store_key(entry_id),
            atomic_writes=True,
        )
        self._history_store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION_HISTORY, history_store_key(entry_id)
        )
        self._journal_lock = asyncio.Lock()

    # -- load ----------------------------------------------------------

    async def async_load_config(self) -> ConfigData | None:
        raw = await self._config_store.async_load()
        if raw is None:
            return None
        try:
            return serde.load(ConfigData, raw)
        except (TypeError, ValueError, KeyError) as err:
            _LOGGER.error(
                "Configuration store is corrupted (%s); starting fresh", err
            )
            return None

    async def async_load_journal(self) -> ExecutionJournal:
        raw = await self._journal_store.async_load()
        if raw is None:
            return ExecutionJournal()
        try:
            return serde.load(ExecutionJournal, raw)
        except (TypeError, ValueError, KeyError) as err:
            _LOGGER.error(
                "Execution journal is corrupted (%s); starting fresh. Any "
                "in-flight run cannot be recovered",
                err,
            )
            return ExecutionJournal()

    async def async_load_history(self) -> HistoryData:
        raw = await self._history_store.async_load()
        if raw is None:
            return HistoryData()
        try:
            return serde.load(HistoryData, raw)
        except (TypeError, ValueError, KeyError) as err:
            _LOGGER.error("History store is corrupted (%s); resetting", err)
            return HistoryData()

    # -- save ----------------------------------------------------------

    async def async_save_config(self, config: ConfigData) -> None:
        """Configuration writes are infrequent and immediate."""
        await self._config_store.async_save(serde.dump(config))

    async def async_write_journal_now(self, journal: ExecutionJournal) -> None:
        """Immediate, serialized journal persistence (plan §11.4)."""
        snapshot = serde.dump(journal)
        async with self._journal_lock:
            await self._journal_store.async_save(snapshot)

    def save_history_soon(self, history: HistoryData) -> None:
        """History detail is noncritical: delayed save is permitted."""
        self._history_store.async_delay_save(
            lambda: serde.dump(history), HISTORY_SAVE_DELAY_SECONDS
        )

    async def async_flush(
        self, journal: ExecutionJournal, history: HistoryData
    ) -> None:
        """Unload/shutdown path: snapshot everything immediately."""
        await self.async_write_journal_now(journal)
        await self._history_store.async_save(serde.dump(history))

    async def async_remove(self) -> None:
        """Delete all stores (config entry removal)."""
        await self._config_store.async_remove()
        await self._journal_store.async_remove()
        await self._history_store.async_remove()


class HistoryRecorder:
    """Builds bounded history records; implements the executor's sink."""

    def __init__(
        self,
        history: HistoryData,
        storage: SchedulerStorage,
        now_fn: Callable[[], datetime],
        on_change: Callable[[], None] = lambda: None,
    ) -> None:
        self.history = history
        self._storage = storage
        self._now = now_fn
        self._on_change = on_change

    def record_run_finished(
        self,
        journal: ExecutionJournal,
        outcome: RunOutcome,
        reason: str | None,
    ) -> None:
        plan = journal.run_plan
        if plan is None:
            return
        starts = [
            result.actual_start_utc
            for result in journal.step_results.values()
            if result.actual_start_utc is not None
        ]
        ends = [
            result.actual_end_utc
            for result in journal.step_results.values()
            if result.actual_end_utc is not None
        ]
        self.history.runs.append(
            RunRecord(
                run_id=plan.run_id,
                occurrence_id=plan.occurrence_id,
                program_id=plan.program_id,
                program_name=plan.program_name,
                manual=plan.manual,
                requested_start_utc=plan.requested_start_utc,
                planned_start_utc=(
                    plan.steps[0].planned_start_utc if plan.steps else None
                ),
                planned_end_utc=plan.planned_end_utc,
                actual_start_utc=min(starts) if starts else None,
                actual_end_utc=max(ends) if ends else None,
                outcome=outcome,
                reason=reason,
                provider_kind=plan.adjustment_snapshot.provider_kind,
                retries=journal.retry_count,
                uncertain_commands=journal.uncertain_count,
            )
        )
        for step in plan.steps:
            result = journal.step_results.get(step.index)
            if result is None:
                continue
            self.history.zone_records.append(
                ZoneRecord(
                    run_id=plan.run_id,
                    step_index=step.index,
                    zone_id=step.zone_id,
                    zone_name=step.zone_name,
                    cycle_index=step.cycle_index,
                    cycle_count=step.cycle_count,
                    planned_start_utc=step.planned_start_utc,
                    planned_end_utc=step.planned_end_utc,
                    actual_start_utc=result.actual_start_utc,
                    actual_end_utc=result.actual_end_utc,
                    commanded_minutes=step.duration_minutes,
                    exact_minutes=step.exact_minutes,
                    status=result.status,
                    reason=result.reason,
                )
            )
        self._trim_and_save()

    def record_run_skipped(
        self,
        *,
        run_id: str,
        occurrence_id: str,
        program_id: str,
        program_name: str,
        requested_start_utc: datetime,
        reason: str,
        provider_kind: ProviderKind = ProviderKind.FIXED,
        manual: bool = False,
    ) -> None:
        """Record an occurrence that was skipped before it was claimed."""
        self.history.runs.append(
            RunRecord(
                run_id=run_id,
                occurrence_id=occurrence_id,
                program_id=program_id,
                program_name=program_name,
                manual=manual,
                requested_start_utc=requested_start_utc,
                planned_start_utc=None,
                planned_end_utc=None,
                actual_start_utc=None,
                actual_end_utc=None,
                outcome=RunOutcome.SKIPPED,
                reason=reason,
                provider_kind=provider_kind,
                retries=0,
                uncertain_commands=0,
            )
        )
        self._trim_and_save()

    def record_intervention(
        self, kind: str, message: str, run_id: str | None = None
    ) -> None:
        self.history.interventions.append(
            InterventionRecord(
                recorded_at_utc=self._now(),
                kind=kind,
                message=message,
                run_id=run_id,
            )
        )
        self._trim_and_save()

    def _trim_and_save(self) -> None:
        history = self.history
        if len(history.runs) > HISTORY_MAX_RUNS:
            del history.runs[: len(history.runs) - HISTORY_MAX_RUNS]
        if len(history.zone_records) > HISTORY_MAX_ZONE_RECORDS:
            del history.zone_records[
                : len(history.zone_records) - HISTORY_MAX_ZONE_RECORDS
            ]
        if len(history.interventions) > HISTORY_MAX_INTERVENTIONS:
            del history.interventions[
                : len(history.interventions) - HISTORY_MAX_INTERVENTIONS
            ]
        self._storage.save_history_soon(history)
        self._on_change()

"""Scheduler coordinator: one per config entry / controller.

Owns configuration (with optimistic-concurrency revisions), source-entity
discovery and resolution, the compile-preview timeline, occurrence
launching, and the wiring between Home Assistant events and the pure
executor state machine.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import uuid
from collections.abc import Callable, Coroutine
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_call_later,
    async_track_point_in_utc_time,
    async_track_state_change_event,
)
from homeassistant.util import dt as dt_util

from . import serde
from .adjustment import create_provider
from .conditions import evaluate_preconditions
from .const import (
    CONF_AUTHORITY_MODE,
    CONF_SOURCE_CONFIG_ENTRY_ID,
    CONF_SOURCE_UNIQUE_ID,
    DOMAIN,
    EVENT_RUN_COMPLETED,
    EVENT_RUN_FAILED,
    EVENT_RUN_SKIPPED,
    OBSERVATION_FRESHNESS_WINDOW_SECONDS,
    PLAN_HORIZON_DAYS,
    SOURCE_DOMAIN,
)
from .driver.base import ZoneValidationError
from .driver.ha_entity import HomeAssistantEntityDriver, resolve_zone_entity
from .executor import ControllerBusyError, DuplicateOccurrenceError, RunExecutor
from .models import (
    AdjustmentProviderConfig,
    AdjustmentSnapshot,
    AuthorityMode,
    CompiledControllerTimeline,
    ConfigData,
    ControllerConfig,
    ControllerObservation,
    EntityReference,
    ExecutorState,
    MissedRunPolicy,
    Program,
    ProgramOccurrence,
    ProgramZoneStep,
    ProviderKind,
    RainPolicy,
    RunPlan,
    RunStep,
    SkipReason,
    ZoneProfile,
    ZoneReference,
)
from .planner import PlannerInput, compile_timeline, deterministic_run_id
from .recurrence import occurrences_between
from .storage import HistoryRecorder, SchedulerStorage

_LOGGER = logging.getLogger(__name__)

LAUNCH_RETRY_SECONDS = 60.0

SIGNAL_STATE = f"{DOMAIN}_state" + "_{}"
SIGNAL_CONFIG = f"{DOMAIN}_config" + "_{}"
SIGNAL_LIFECYCLE = f"{DOMAIN}_lifecycle" + "_{}"

_RUN_TERMINAL_EVENTS = {
    EVENT_RUN_COMPLETED,
    EVENT_RUN_FAILED,
    EVENT_RUN_SKIPPED,
}


class RevisionConflictError(HomeAssistantError):
    """The caller's expected revision is stale."""

    def __init__(self, current_revision: int, current_value: Any) -> None:
        super().__init__(f"Revision conflict (current: {current_revision})")
        self.current_revision = current_revision
        self.current_value = current_value


class _HATimers:
    """Production TimerScheduler over Home Assistant's event helpers."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    def call_at(
        self, when: datetime, cb: Callable[[], Coroutine[Any, Any, None]]
    ) -> Any:
        @callback
        def _fire(_now: datetime) -> None:
            self._hass.async_create_task(cb())

        return async_track_point_in_utc_time(self._hass, _fire, when)


class SchedulerCoordinator:
    """Everything for one scheduled Rain Bird controller."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.storage = SchedulerStorage(hass, entry.entry_id)
        self.config: ConfigData = None  # type: ignore[assignment]  # set in setup
        self.history: HistoryRecorder = None  # type: ignore[assignment]
        self.executor: RunExecutor = None  # type: ignore[assignment]
        self.timeline: CompiledControllerTimeline = CompiledControllerTimeline(
            runs=(), conflicts=(), warnings=()
        )
        self.external_watering = False
        self.last_observation: ControllerObservation | None = None
        self.source_available = True

        self._entity_to_zone: dict[str, str] = {}
        self._zone_to_entity: dict[str, str] = {}
        self._rain_sensor_entity: str | None = None
        self._rain_delay_entity: str | None = None
        self._native_calendar_entity: str | None = None
        self._unsub_state: Any | None = None
        self._unsub_registry: Any | None = None
        self._unsub_occurrence: Any | None = None
        self._unsub_retry: Any | None = None
        self._ephemeral_programs: dict[str, Program] = {}
        self._occurrence_index: dict[str, ProgramOccurrence] = {}
        self._new_zone_found = False
        # Occurrences owned by the launch-retry loop (transient block or
        # controller busy); the normal arming path must not double-arm them.
        self._retry_occurrences: dict[str, ProgramOccurrence] = {}
        # External-watering detection: when the executor was last seen
        # active (stale switch echoes of our own runs are reported before
        # this instant) and when we last logged an external episode.
        self._last_active_seen_utc: datetime | None = None
        self._last_external_intervention_utc: datetime | None = None
        # What the occurrence timer is currently armed for, so re-arming the
        # same launch instant keeps the live timer instead of churning it.
        self._armed_occurrence_id: str | None = None
        self._armed_when: datetime | None = None
        # Fire-and-forget tasks (recalculates, journal writes, launches),
        # tracked so shutdown can stop them before they re-arm anything.
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._shutdown = False

    # ------------------------------------------------------------------
    # Setup / teardown
    # ------------------------------------------------------------------

    @property
    def source_entry_id(self) -> str:
        return cast(str, self.entry.data[CONF_SOURCE_CONFIG_ENTRY_ID])

    async def async_setup(self) -> None:
        config = await self.storage.async_load_config()
        if config is None:
            config = ConfigData(
                controller=ControllerConfig(
                    id=self.entry.unique_id or self.entry.entry_id,
                    revision=1,
                    source_config_entry_id=self.source_entry_id,
                    source_unique_id=self.entry.data.get(
                        CONF_SOURCE_UNIQUE_ID
                    ),
                    authority_mode=AuthorityMode(
                        self.entry.data.get(
                            CONF_AUTHORITY_MODE,
                            AuthorityMode.HA_AUTHORITATIVE.value,
                        )
                    ),
                )
            )
        self.config = config

        journal = await self.storage.async_load_journal()
        history_data = await self.storage.async_load_history()
        self.history = HistoryRecorder(
            history_data,
            self.storage,
            dt_util.utcnow,
            on_change=self._notify_state,
        )

        self._check_source_entry()
        self._discover_sources()
        if self._discovered_new_zones():
            await self.storage.async_save_config(self.config)

        driver = HomeAssistantEntityDriver(
            self.hass,
            any_zone_entity=self._any_zone_entity,
            observe=self.build_observation,
            now_fn=dt_util.utcnow,
        )
        self.executor = RunExecutor(
            driver=driver,
            journal=journal,
            journal_store=self.storage,
            timers=_HATimers(self.hass),
            now_fn=dt_util.utcnow,
            get_controller_config=lambda: self.config.controller,
            get_program=self._get_program,
            get_zone_reference=self._get_zone_reference,
            emit_event=self._emit_lifecycle,
            history=self.history,
            on_state_change=self._notify_state,
            get_zone_name=self._zone_display_name,
        )

        self._subscribe_states()
        self._unsub_registry = self.hass.bus.async_listen(
            er.EVENT_ENTITY_REGISTRY_UPDATED, self._on_registry_updated
        )

        await self.executor.async_recover()
        # A run whose pause cause cleared while HA was down would otherwise
        # wait for the next state-change event; nudge it with a fresh
        # reading so it resumes (or expires) promptly. User pauses carry a
        # paused_reason and are never auto-resumed here.
        journal = self.executor.journal
        if journal.state is ExecutorState.PAUSED_SENSOR:
            await self.executor.async_handle_weather_state(
                self.build_observation()
            )
        elif (
            journal.state is ExecutorState.PAUSED_EXTERNAL
            and journal.paused_reason is None
        ):
            step = self.active_step()
            if step is not None:
                observation = self.build_observation()
                await self.executor.async_handle_zone_state(
                    step.zone_id,
                    step.zone_id in observation.active_zone_ids,
                    observation,
                )
        await self.async_recalculate()

    async def async_shutdown(self) -> None:
        """Unload path (§11.4): stop timers and tracked tasks, snapshot."""
        self._shutdown = True
        for unsub in (
            self._unsub_state,
            self._unsub_registry,
            self._unsub_occurrence,
            self._unsub_retry,
        ):
            if unsub is not None:
                unsub()
        self._unsub_state = None
        self._unsub_registry = None
        self._unsub_occurrence = None
        self._unsub_retry = None
        # The executor's armed timer must not outlive the coordinator: an
        # orphaned executor would keep issuing commands while the next
        # setup recovers the same journal.
        if self.executor is not None:
            self.executor.shutdown()
        # In-flight fire-and-forget work (recalculates, journal writes,
        # launches) must not re-arm timers or write after unload.
        tasks = tuple(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.storage.async_flush(
            self.executor.journal, self.history.history
        )

    def _track_background(self, coro: Coroutine[Any, Any, None]) -> None:
        """Run ``coro`` fire-and-forget, tracked so shutdown can stop it."""
        if self._shutdown:
            coro.close()
            return
        task = self.hass.async_create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def async_remove(self) -> None:
        await self.storage.async_remove()

    # ------------------------------------------------------------------
    # Discovery and resolution (plan §7, §8)
    # ------------------------------------------------------------------

    def _check_source_entry(self) -> None:
        source = self.hass.config_entries.async_get_entry(self.source_entry_id)
        if source is None:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"missing_source_{self.entry.entry_id}",
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="missing_source",
                translation_placeholders={"title": self.entry.title},
            )
            self.source_available = False
        else:
            ir.async_delete_issue(
                self.hass, DOMAIN, f"missing_source_{self.entry.entry_id}"
            )
            self.source_available = True

    def _discover_sources(self) -> None:
        """Discover zone switches and helper source entities."""
        registry = er.async_get(self.hass)
        entries = er.async_entries_for_config_entry(
            registry, self.source_entry_id
        )
        by_unique = {
            zone.reference.source_unique_id: zone
            for zone in self.config.zones.values()
        }
        self._entity_to_zone.clear()
        self._zone_to_entity.clear()
        rain_sensor_candidates: list[er.RegistryEntry] = []

        for entry in entries:
            if entry.platform != SOURCE_DOMAIN:
                continue
            domain = entry.entity_id.split(".", 1)[0]
            if domain == "switch":
                self._register_zone(entry, by_unique)
            elif domain == "binary_sensor":
                rain_sensor_candidates.append(entry)
            elif domain == "number":
                self._rain_delay_entity = entry.entity_id
                self.config.controller.rain_delay_reference = EntityReference(
                    entity_registry_id=entry.id,
                    last_known_entity_id=entry.entity_id,
                )
            elif domain == "calendar":
                self._native_calendar_entity = entry.entity_id
                self.config.controller.native_calendar_reference = (
                    EntityReference(
                        entity_registry_id=entry.id,
                        last_known_entity_id=entry.entity_id,
                    )
                )

        if rain_sensor_candidates:
            # Prefer the moisture-classed sensor (the rain sensor) over any
            # other binary_sensor the source exposes; deterministic tie-break by
            # entity id. This only sets the discovered reference — a user
            # override lives in ``rain_sensor_override_entity_id`` and is never
            # touched here, so it survives registry re-scans.
            def _is_moisture(entry: er.RegistryEntry) -> bool:
                return "moisture" in (
                    entry.device_class,
                    entry.original_device_class,
                )

            chosen = min(
                rain_sensor_candidates,
                key=lambda e: (not _is_moisture(e), e.entity_id),
            )
            self._rain_sensor_entity = chosen.entity_id
            self.config.controller.rain_sensor_reference = EntityReference(
                entity_registry_id=chosen.id,
                last_known_entity_id=chosen.entity_id,
            )

    def _register_zone(
        self, entry: er.RegistryEntry, by_unique: dict[str, ZoneProfile]
    ) -> None:
        state = self.hass.states.get(entry.entity_id)
        station: int | None = None
        if state is not None and state.attributes.get("zone") is not None:
            station = int(state.attributes["zone"])
        else:
            # Fall back to the trailing number in the unique id.
            digits = ""
            for char in reversed(entry.unique_id or ""):
                if char.isdigit():
                    digits = char + digits
                else:
                    break
            if digits:
                station = int(digits)
        if station is None:
            _LOGGER.warning(
                "Could not determine station number for %s", entry.entity_id
            )
            return

        unique = entry.unique_id or entry.entity_id
        profile = by_unique.get(unique)
        reference = ZoneReference(
            source_unique_id=unique,
            source_config_entry_id=self.source_entry_id,
            entity_registry_id=entry.id,
            station_number=station,
            last_known_entity_id=entry.entity_id,
        )
        if profile is None:
            display = (
                state.attributes.get("friendly_name")
                if state is not None
                else None
            ) or entry.name or entry.original_name or f"Zone {station}"
            profile = ZoneProfile(
                id=uuid.uuid4().hex,
                revision=1,
                reference=reference,
                display_name=display,
            )
            self.config.zones[profile.id] = profile
            self._new_zone_found = True
        else:
            profile.reference = reference
        self._entity_to_zone[entry.entity_id] = profile.id
        self._zone_to_entity[profile.id] = entry.entity_id

    def _discovered_new_zones(self) -> bool:
        found = self._new_zone_found
        self._new_zone_found = False
        return found

    def _any_zone_entity(self) -> str | None:
        for entity_id in self._entity_to_zone:
            return entity_id
        return None

    def _get_zone_reference(self, zone_id: str) -> ZoneReference | None:
        zone = self.config.zones.get(zone_id)
        if zone is None:
            return None
        try:
            entity_id = resolve_zone_entity(self.hass, zone.reference)
        except ZoneValidationError as err:
            _LOGGER.warning("Zone %s failed validation: %s", zone_id, err)
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"zone_unresolved_{zone_id}",
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="zone_unresolved",
                translation_placeholders={
                    "zone": zone.display_name,
                    "error": str(err),
                },
            )
            return None
        self._entity_to_zone[entity_id] = zone_id
        self._zone_to_entity[zone_id] = entity_id
        return zone.reference

    def _get_program(self, program_id: str) -> Program | None:
        return self.config.programs.get(program_id) or (
            self._ephemeral_programs.get(program_id)
        )

    # ------------------------------------------------------------------
    # Observations and event fan-in (plan §19, §27)
    # ------------------------------------------------------------------

    def _effective_rain_sensor(self) -> str | None:
        """User override wins over the auto-discovered rain sensor."""
        return (
            self.config.controller.rain_sensor_override_entity_id
            or self._rain_sensor_entity
        )

    def _temperature_entity(self) -> str | None:
        return self.config.controller.freeze_guard.temperature_entity_id

    def _subscribe_states(self) -> None:
        if self._unsub_state is not None:
            self._unsub_state()
        watched = list(self._entity_to_zone)
        for extra in (
            self._effective_rain_sensor(),
            self._rain_delay_entity,
            self._temperature_entity(),
        ):
            if extra and extra not in watched:
                watched.append(extra)
        if not watched:
            self._unsub_state = None
            return
        self._unsub_state = async_track_state_change_event(
            self.hass, watched, self._on_source_state_event
        )

    def build_observation(self) -> ControllerObservation:
        from .observations import build_observation

        observation = build_observation(
            self.hass,
            zone_entities=self._zone_to_entity,
            rain_sensor_entity=self._effective_rain_sensor(),
            rain_delay_entity=self._rain_delay_entity,
            temperature_entity=self._temperature_entity(),
            now=dt_util.utcnow(),
        )
        self.last_observation = observation
        return observation

    async def _on_source_state_event(self, event: Event[Any]) -> None:
        entity_id = event.data["entity_id"]
        new_state = event.data.get("new_state")
        observation = self.build_observation()

        zone_id = self._entity_to_zone.get(entity_id)
        if zone_id is not None:
            is_on = new_state is not None and new_state.state == STATE_ON
            await self.executor.async_handle_zone_state(
                zone_id, is_on, observation
            )
        elif entity_id == self._effective_rain_sensor():
            await self.executor.async_handle_sensor_state(observation)
        elif entity_id == self._temperature_entity():
            await self.executor.async_handle_weather_state(observation)

        self._refresh_flags(observation)
        self._notify_state()

    def _zone_display_name(self, zone_id: str) -> str | None:
        zone = self.config.zones.get(zone_id)
        return zone.display_name if zone is not None else None

    def _external_suppression_floor(self) -> datetime | None:
        """Latest instant our own runs could explain an "on" report.

        Event traffic while a run is active moves the in-memory marker;
        the journal's finished-occurrence times (persisted, restart-safe)
        cover runs whose polls were quiet. A grace window on top absorbs
        the controller stopping a few seconds after our commanded clock
        and the source re-reporting that lag.
        """
        candidates = [self._last_active_seen_utc]
        finished = self.executor.journal.completed_occurrences.values()
        candidates.append(max(finished, default=None))
        floor = max((c for c in candidates if c is not None), default=None)
        if floor is None:
            return None
        return floor + timedelta(
            seconds=OBSERVATION_FRESHNESS_WINDOW_SECONDS
        )

    def _refresh_flags(self, observation: ControllerObservation) -> None:
        now = observation.observed_at_utc
        if self.executor.is_active:
            self._last_active_seen_utc = now
            self.external_watering = False
            return
        # Only zones the source re-confirmed on AFTER our last run (plus
        # a grace window) count: a just-finished zone's switch stays "on"
        # until the next source poll, and that stale echo — or the
        # controller's own few-second stop lag — is not external watering.
        floor = self._external_suppression_floor()
        external = {
            zone_id
            for zone_id in observation.active_zone_ids
            if floor is None
            or observation.zone_reported_at(zone_id) > floor
        }
        was_external = self.external_watering
        self.external_watering = bool(external)
        if not external or was_external:
            return
        last = self._last_external_intervention_utc
        if last is not None and now - last < timedelta(minutes=10):
            return
        self._last_external_intervention_utc = now
        names = ", ".join(
            sorted(self._zone_display_name(z) or z for z in external)
        )
        hint = (
            " The controller's own schedule is enabled (its native calendar "
            "has programmed events) — that is almost certainly what is "
            "watering. While this scheduler is authoritative, clear the "
            "controller's internal programs to stop the two from fighting."
            if self.native_schedule_conflict
            else " Likely causes: a native Rain Bird program on the "
            "controller, the Rain Bird app, or another Home Assistant "
            "automation."
        )
        self.history.record_intervention(
            "external_watering",
            f"Zone(s) {names} started watering outside any scheduled run."
            + hint,
        )

    async def _on_registry_updated(self, event: Event[Any]) -> None:
        data = event.data
        if data.get("action") not in ("update", "remove", "create"):
            return
        entity_id = data.get("entity_id", "")
        if entity_id not in self._entity_to_zone and not entity_id.startswith(
            ("switch.", "binary_sensor.", "number.", "calendar.")
        ):
            return
        registry = er.async_get(self.hass)
        entry = registry.async_get(entity_id)
        if (
            entry is not None
            and entry.config_entry_id != self.source_entry_id
            and entity_id not in self._entity_to_zone
        ):
            return
        # Re-resolve everything; renames and removals must reflect promptly.
        self._discover_sources()
        self._subscribe_states()
        if self._discovered_new_zones():
            # A zone discovered after setup would otherwise get a fresh
            # random id on every restart until some unrelated CRUD write.
            await self._persist_config()
        else:
            self._notify_state()

    @property
    def native_schedule_conflict(self) -> bool:
        """Native calendar present while HA owns automatic watering."""
        if (
            self.config.controller.authority_mode
            is not AuthorityMode.HA_AUTHORITATIVE
        ):
            return False
        if not self._native_calendar_entity:
            return False
        state = self.hass.states.get(self._native_calendar_entity)
        if state is None or state.state in ("unavailable", "unknown"):
            return False
        return state.state == STATE_ON or bool(
            state.attributes.get("start_time")
        )

    # ------------------------------------------------------------------
    # Planning loop (plan §13, §14)
    # ------------------------------------------------------------------

    async def build_adjustment_snapshot(
        self, program: Program, occurrence: ProgramOccurrence
    ) -> AdjustmentSnapshot:
        provider = create_provider(
            program.adjustment_provider,
            get_state=self.hass.states.get,
            now_fn=dt_util.utcnow,
            get_zone=self.config.zones.get,
            location=(
                self.hass.config.latitude,
                self.hass.config.longitude,
            ),
        )
        per_zone = {}
        for step in program.zone_steps:
            if not step.enabled:
                continue
            zone = self.config.zones.get(step.zone_id)
            if zone is None or not zone.enabled:
                continue
            per_zone[zone.id] = await provider.async_calculate(
                zone, program, occurrence
            )
        return AdjustmentSnapshot(
            provider_kind=program.adjustment_provider.kind,
            computed_at_utc=dt_util.utcnow(),
            per_zone=per_zone,
        )

    async def async_recalculate(self) -> None:
        """Recompile the preview timeline and arm the next occurrence."""
        now = dt_util.utcnow()
        tz = dt_util.get_default_time_zone()
        # Start the horizon at the top of today (local) rather than "now" so
        # occurrences that have already run today stay in the compiled
        # timeline — the panel dims them instead of dropping the whole row to
        # "no watering". next_pending_run gates on each run's launch
        # deadline, so a wider window never re-fires an elapsed run.
        window_start = dt_util.as_utc(dt_util.start_of_local_day())
        window_end = now + timedelta(days=PLAN_HORIZON_DAYS)

        occurrences: list[ProgramOccurrence] = []
        for program in self.config.programs.values():
            found, _warnings = occurrences_between(
                program, tz, window_start, window_end, now
            )
            occurrences.extend(found)

        snapshots: dict[str, AdjustmentSnapshot] = {}
        for occurrence in occurrences:
            owner = self.config.programs.get(occurrence.program_id)
            if owner is None:
                continue
            snapshots[occurrence.occurrence_id] = (
                await self.build_adjustment_snapshot(owner, occurrence)
            )

        self.timeline = compile_timeline(
            PlannerInput(
                controller_config=self.config.controller,
                programs=self.config.programs,
                zone_profiles=self.config.zones,
                candidate_occurrences=occurrences,
                adjustment_snapshots=snapshots,
                tz=tz,
                compiled_at_utc=now,
            )
        )
        self._occurrence_index = {
            occ.occurrence_id: occ for occ in occurrences
        }
        self._arm_next_occurrence()
        self._notify_state()

    @staticmethod
    def _planned_launch(run: RunPlan) -> datetime:
        """Earliest planned step start — the delay-and-preserve launch time.

        The planner deliberately pushes overlapping runs past their nominal
        requested start; the compiled timeline, not the request, is when the
        controller can actually take the run.
        """
        return min(
            (step.planned_start_utc for step in run.steps),
            default=run.requested_start_utc,
        )

    def _launch_deadline(self, run: RunPlan) -> datetime:
        """Latest instant the run may still launch (missed-run policy).

        RUN_LATE tolerates lateness against the planned start; SKIP must
        begin within tolerance of the requested time (the planner already
        enforces that at compile time, so this gate matches its intent).
        """
        tolerance = timedelta(
            minutes=self.config.controller.missed_run_tolerance_minutes
        )
        program = self.config.programs.get(run.program_id)
        if (
            program is not None
            and program.missed_run_policy is MissedRunPolicy.SKIP
        ):
            return run.requested_start_utc + tolerance
        return self._planned_launch(run) + tolerance

    def next_pending_run(self) -> RunPlan | None:
        journal = self.executor.journal if self.executor else None
        completed = journal.completed_occurrences if journal else {}
        active = journal.active_occurrence_id if journal else None
        now = dt_util.utcnow()
        for run in self.timeline.runs:
            if not run.steps:
                continue
            if run.occurrence_id in completed or run.occurrence_id == active:
                continue
            if run.occurrence_id in self._retry_occurrences:
                # Owned by the launch-retry loop; never double-arm it.
                continue
            # Today's already-elapsed occurrences live in the timeline for
            # display and stay launchable until their missed-run deadline;
            # the arming path records a proper skip once that passes.
            if now > self._launch_deadline(run):
                continue
            return run
        return None

    def _expire_stale_occurrences(self) -> None:
        """Record MISSED_TOLERANCE skips for occurrences past their deadline.

        Elapsed runs stay in the timeline for display and late launching;
        once the launch deadline passes they would otherwise vanish from
        every view with no run, skip record, or history.
        """
        journal = self.executor.journal
        now = dt_util.utcnow()
        for run in self.timeline.runs:
            if not run.steps:
                continue
            if (
                run.occurrence_id in journal.completed_occurrences
                or run.occurrence_id == journal.active_occurrence_id
            ):
                continue
            occurrence = self._occurrence_index.get(run.occurrence_id)
            program = self.config.programs.get(run.program_id)
            if occurrence is None or program is None:
                continue
            if now <= self._launch_deadline(run):
                continue
            self._retry_occurrences.pop(run.occurrence_id, None)
            self._record_occurrence_skip(
                occurrence, program, SkipReason.MISSED_TOLERANCE
            )

    def _arm_next_occurrence(self) -> None:
        """(Re)arm the one-shot launch timer for the next pending run.

        Re-arming is idempotent: when the already-armed occurrence is still
        next at the same instant, the live timer is kept. Blind
        cancel-and-re-arm churns the handle on every recalculate and can
        swallow a launch when a retry re-arms while the occurrence timer is
        already in flight.
        """
        run: RunPlan | None = None
        if (
            self.config.controller.enabled
            and self.config.controller.authority_mode
            is not AuthorityMode.NATIVE_AUTHORITATIVE
        ):
            self._expire_stale_occurrences()
            run = self.next_pending_run()
        occurrence = (
            self._occurrence_index.get(run.occurrence_id)
            if run is not None
            else None
        )
        if run is None or occurrence is None:
            self._disarm_occurrence()
            return
        # Arm at the planned start, not the nominal requested start: an
        # occurrence the planner delayed behind another run must not fire
        # into a busy controller and burn its missed-run tolerance.
        when = max(self._planned_launch(run), dt_util.utcnow())
        if (
            self._unsub_occurrence is not None
            and self._armed_occurrence_id == occurrence.occurrence_id
            and self._armed_when == when
        ):
            return
        self._disarm_occurrence()
        self._armed_occurrence_id = occurrence.occurrence_id
        self._armed_when = when

        @callback
        def _fire(_now: datetime) -> None:
            self._unsub_occurrence = None
            self._armed_occurrence_id = None
            self._armed_when = None
            self._track_background(self._launch_occurrence(occurrence))

        self._unsub_occurrence = async_track_point_in_utc_time(
            self.hass, _fire, when
        )

    def _disarm_occurrence(self) -> None:
        if self._unsub_occurrence is not None:
            self._unsub_occurrence()
            self._unsub_occurrence = None
        self._armed_occurrence_id = None
        self._armed_when = None

    def _occurrence_launch_deadline(
        self, occurrence: ProgramOccurrence
    ) -> datetime:
        """Latest launch instant, taken from the occurrence's timeline run.

        The full timeline carries the delay-and-preserve planned start; if
        the occurrence is no longer in it, fall back to the requested start.
        """
        timeline_run = next(
            (
                run
                for run in self.timeline.runs
                if run.occurrence_id == occurrence.occurrence_id
            ),
            None,
        )
        if timeline_run is not None:
            return self._launch_deadline(timeline_run)
        return occurrence.scheduled_start_utc + timedelta(
            minutes=self.config.controller.missed_run_tolerance_minutes
        )

    async def _launch_occurrence(self, occurrence: ProgramOccurrence) -> None:
        program = self.config.programs.get(occurrence.program_id)
        if program is None:
            await self.async_recalculate()
            return
        journal = self.executor.journal
        if (
            occurrence.occurrence_id in journal.completed_occurrences
            or occurrence.occurrence_id == journal.active_occurrence_id
        ):
            # Already finished or mid-run: a stale duplicate arm must not
            # re-launch (its own active zone would read as external
            # activity and park it in the retry loop).
            await self.async_recalculate()
            return

        observation = self.build_observation()
        journal.last_observation = observation
        blocked = evaluate_preconditions(
            self.config.controller, program, observation, manual=False
        )
        now = dt_util.utcnow()
        deadline = self._occurrence_launch_deadline(occurrence)
        if blocked is not None:
            if blocked.transient and now <= deadline:
                self._arm_launch_retry(occurrence)
                return
            self._record_occurrence_skip(occurrence, program, blocked.reason)
            await self.async_recalculate()
            return

        snapshot = await self.build_adjustment_snapshot(program, occurrence)
        timeline = compile_timeline(
            PlannerInput(
                controller_config=self.config.controller,
                programs=self.config.programs,
                zone_profiles=self.config.zones,
                candidate_occurrences=[occurrence],
                adjustment_snapshots={occurrence.occurrence_id: snapshot},
                tz=dt_util.get_default_time_zone(),
                compiled_at_utc=now,
            )
        )
        if not timeline.runs:
            reason = (
                timeline.conflicts[0].reason
                if timeline.conflicts
                else SkipReason.PROGRAM_DISABLED
            )
            self._record_occurrence_skip(occurrence, program, reason)
            await self.async_recalculate()
            return

        try:
            await self.executor.async_start_run(timeline.runs[0])
        except ControllerBusyError:
            # Genuinely external busy (e.g. a manual run started after the
            # timeline was compiled): retry within the launch deadline.
            if now <= deadline:
                self._arm_launch_retry(occurrence)
                return
            self._record_occurrence_skip(
                occurrence, program, SkipReason.CONTROLLER_BUSY
            )
        except DuplicateOccurrenceError:
            pass
        await self.async_recalculate()

    def _arm_launch_retry(self, occurrence: ProgramOccurrence) -> None:
        """Retry a transiently blocked launch; keep later runs on schedule.

        One retry timer serves all blocked occurrences: the earliest pass
        re-attempts each of them, so a second blocked occurrence never
        cancels the first one's retry (and vice versa).
        """
        self._retry_occurrences[occurrence.occurrence_id] = occurrence
        if self._unsub_retry is None:

            @callback
            def _retry(_now: datetime) -> None:
                self._unsub_retry = None
                due = list(self._retry_occurrences.values())
                self._retry_occurrences.clear()
                for pending in due:
                    self._track_background(self._launch_occurrence(pending))

            self._unsub_retry = async_call_later(
                self.hass, LAUNCH_RETRY_SECONDS, _retry
            )
        # A later occurrence may come due while this one waits out its
        # retry; make sure the normal arming path covers it.
        self._arm_next_occurrence()

    def _record_occurrence_skip(
        self,
        occurrence: ProgramOccurrence,
        program: Program,
        reason: SkipReason,
    ) -> None:
        journal = self.executor.journal
        journal.record_occurrence_finished(
            occurrence.occurrence_id, dt_util.utcnow()
        )
        self.history.record_run_skipped(
            run_id=deterministic_run_id(occurrence.occurrence_id),
            occurrence_id=occurrence.occurrence_id,
            program_id=program.id,
            program_name=program.name,
            requested_start_utc=occurrence.scheduled_start_utc,
            reason=reason.value,
            provider_kind=program.adjustment_provider.kind,
        )
        self._emit_lifecycle(
            EVENT_RUN_SKIPPED,
            {
                "program_id": program.id,
                "program_name": program.name,
                "occurrence_id": occurrence.occurrence_id,
                "reason": reason.value,
            },
        )
        self._track_background(
            self.storage.async_write_journal_now(journal)
        )

    # ------------------------------------------------------------------
    # Manual runs (plan §17.1, §34)
    # ------------------------------------------------------------------

    async def async_run_program(self, program_id: str) -> None:
        program = self.config.programs.get(program_id)
        if program is None:
            raise HomeAssistantError(f"Unknown program: {program_id}")
        now = dt_util.utcnow()
        occurrence = ProgramOccurrence(
            occurrence_id=f"{program_id}:manual:{now.isoformat()}",
            program_id=program_id,
            scheduled_start_utc=now,
            scheduled_start_local=now.astimezone(
                dt_util.get_default_time_zone()
            ),
            created_at_utc=now,
            manual=True,
        )
        snapshot = await self.build_adjustment_snapshot(program, occurrence)
        timeline = compile_timeline(
            PlannerInput(
                controller_config=self.config.controller,
                programs=self.config.programs,
                zone_profiles=self.config.zones,
                candidate_occurrences=[occurrence],
                adjustment_snapshots={occurrence.occurrence_id: snapshot},
                tz=dt_util.get_default_time_zone(),
                compiled_at_utc=now,
            )
        )
        if not timeline.runs or not timeline.runs[0].steps:
            raise HomeAssistantError(
                "Program produced no executable steps"
            )
        journal = self.executor.journal
        journal.last_observation = self.build_observation()
        await self.executor.async_start_run(timeline.runs[0])
        self._notify_state()

    async def async_run_zones(
        self, zones: list[dict[str, Any]]
    ) -> None:
        """Run an ordered ad-hoc zone list (validated up front)."""
        steps: list[ProgramZoneStep] = []
        for position, spec in enumerate(zones):
            entity_id = spec["entity_id"]
            zone_id = self._entity_to_zone.get(entity_id)
            if zone_id is None:
                raise HomeAssistantError(
                    f"{entity_id} is not a Rain Bird zone of this controller"
                )
            duration = int(spec["duration"])
            if not 1 <= duration <= 1440:
                raise HomeAssistantError(
                    f"Duration for {entity_id} must be 1-1440 minutes"
                )
            steps.append(
                ProgramZoneStep(
                    zone_id=zone_id,
                    position=position,
                    base_runtime_override_minutes=Decimal(duration),
                )
            )
        now = dt_util.utcnow()
        program = Program(
            id=f"manual-zones:{uuid.uuid4().hex[:8]}",
            revision=1,
            name="Manual zones",
            priority=1,
            nominal_start_times=[],
            zone_steps=steps,
            adjustment_provider=AdjustmentProviderConfig(
                kind=ProviderKind.FIXED
            ),
            # An explicit manual zone run is a user override: it does not
            # re-check rain delay or the sensor before starting.
            rain_policy=RainPolicy(
                honor_native_delay=False, skip_when_sensor_wet=False
            ),
        )
        occurrence = ProgramOccurrence(
            occurrence_id=f"{program.id}:{now.isoformat()}",
            program_id=program.id,
            scheduled_start_utc=now,
            scheduled_start_local=now.astimezone(
                dt_util.get_default_time_zone()
            ),
            created_at_utc=now,
            manual=True,
        )
        timeline = compile_timeline(
            PlannerInput(
                controller_config=self.config.controller,
                programs={program.id: program},
                zone_profiles=self.config.zones,
                candidate_occurrences=[occurrence],
                adjustment_snapshots={},
                tz=dt_util.get_default_time_zone(),
                compiled_at_utc=now,
            )
        )
        if not timeline.runs or not timeline.runs[0].steps:
            raise HomeAssistantError("No executable zones in the request")
        self._ephemeral_programs[program.id] = program
        # Never evict the program an active run is executing: _get_program
        # falls back to the controller default rain policy for a missing
        # id, silently re-enabling the rain checks this run disabled.
        active_plan = self.executor.journal.run_plan
        active_program = (
            active_plan.program_id if active_plan is not None else None
        )
        while len(self._ephemeral_programs) > 5:
            victim = next(
                (
                    program_id
                    for program_id in self._ephemeral_programs
                    if program_id != active_program
                ),
                None,
            )
            if victim is None:
                break
            del self._ephemeral_programs[victim]
        journal = self.executor.journal
        journal.last_observation = self.build_observation()
        try:
            await self.executor.async_start_run(timeline.runs[0])
        except Exception:
            # A run that never started must not keep its program; failed
            # attempts would otherwise pile up and evict a live one.
            self._ephemeral_programs.pop(program.id, None)
            raise
        self._notify_state()

    async def async_set_rain_delay(self, days: int) -> None:
        """Set the native rain delay (measured in DAYS, plan §23)."""
        if self._rain_delay_entity is None:
            raise HomeAssistantError(
                "The Rain Bird integration exposes no rain delay entity"
            )
        await self.hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": self._rain_delay_entity, "value": days},
            blocking=True,
        )

    # ------------------------------------------------------------------
    # Configuration CRUD with optimistic concurrency (plan §33)
    # ------------------------------------------------------------------

    async def _persist_config(self) -> None:
        await self.storage.async_save_config(self.config)
        async_dispatcher_send(
            self.hass, SIGNAL_CONFIG.format(self.entry.entry_id)
        )
        await self.async_recalculate()

    @staticmethod
    def _apply_patch(target: Any, patch: dict[str, Any]) -> Any:
        """Typed patch via serde: unknown fields are rejected."""
        import typing

        hints = typing.get_type_hints(type(target))
        updates: dict[str, Any] = {}
        for key, raw in patch.items():
            if key in ("id", "revision") or key not in hints:
                raise HomeAssistantError(f"Field not patchable: {key}")
            try:
                updates[key] = serde.load(hints[key], raw)
            except ValueError as err:
                raise ValueError(
                    f"Invalid value for field {key!r}: {err}"
                ) from err
        return dataclasses.replace(target, **updates)

    async def async_update_controller(
        self, patch: dict[str, Any], expected_revision: int
    ) -> ControllerConfig:
        current = self.config.controller
        if current.revision != expected_revision:
            raise RevisionConflictError(current.revision, serde.dump(current))
        updated = cast(
            ControllerConfig, self._apply_patch(current, patch)
        )
        updated.revision = current.revision + 1
        self.config.controller = updated
        # The temperature source or rain-sensor override may have changed;
        # re-subscribe so the new entity is watched without a reload.
        self._subscribe_states()
        await self._persist_config()
        return updated

    async def async_set_enabled(self, enabled: bool) -> None:
        self.config.controller.enabled = enabled
        self.config.controller.revision += 1
        await self._persist_config()

    async def async_create_program(self, data: dict[str, Any]) -> Program:
        data = dict(data)
        data["id"] = uuid.uuid4().hex
        data["revision"] = 1
        program = serde.load(Program, data)
        self.config.programs[program.id] = program
        await self._persist_config()
        return program

    async def async_update_program(
        self, program_id: str, patch: dict[str, Any], expected_revision: int
    ) -> Program:
        current = self.config.programs.get(program_id)
        if current is None:
            raise HomeAssistantError(f"Unknown program: {program_id}")
        if current.revision != expected_revision:
            raise RevisionConflictError(current.revision, serde.dump(current))
        updated = cast(Program, self._apply_patch(current, patch))
        updated.revision = current.revision + 1
        self.config.programs[program_id] = updated
        await self._persist_config()
        return updated

    async def async_delete_program(self, program_id: str) -> None:
        if program_id not in self.config.programs:
            raise HomeAssistantError(f"Unknown program: {program_id}")
        del self.config.programs[program_id]
        await self._persist_config()

    async def async_duplicate_program(self, program_id: str) -> Program:
        current = self.config.programs.get(program_id)
        if current is None:
            raise HomeAssistantError(f"Unknown program: {program_id}")
        copy = serde.load(Program, serde.dump(current))
        copy.id = uuid.uuid4().hex
        copy.revision = 1
        copy.name = f"{current.name} (copy)"
        copy.enabled = False
        self.config.programs[copy.id] = copy
        await self._persist_config()
        return copy

    async def async_update_zone(
        self, zone_id: str, patch: dict[str, Any], expected_revision: int
    ) -> ZoneProfile:
        current = self.config.zones.get(zone_id)
        if current is None:
            raise HomeAssistantError(f"Unknown zone: {zone_id}")
        if current.revision != expected_revision:
            raise RevisionConflictError(current.revision, serde.dump(current))
        if "reference" in patch:
            raise HomeAssistantError("Field not patchable: reference")
        updated = cast(ZoneProfile, self._apply_patch(current, patch))
        updated.revision = current.revision + 1
        self.config.zones[zone_id] = updated
        await self._persist_config()
        return updated

    # ------------------------------------------------------------------
    # Fan-out
    # ------------------------------------------------------------------

    def _emit_lifecycle(self, event_type: str, data: dict[str, Any]) -> None:
        async_dispatcher_send(
            self.hass,
            SIGNAL_LIFECYCLE.format(self.entry.entry_id),
            event_type,
            data,
        )
        if event_type in _RUN_TERMINAL_EVENTS:
            self._track_background(self.async_recalculate())

    def _notify_state(self) -> None:
        async_dispatcher_send(
            self.hass, SIGNAL_STATE.format(self.entry.entry_id)
        )

    # ------------------------------------------------------------------
    # Snapshots for entities / websocket
    # ------------------------------------------------------------------

    @property
    def executor_state(self) -> ExecutorState:
        if self.executor is None:
            return ExecutorState.IDLE
        return self.executor.journal.state

    def active_step(self) -> RunStep | None:
        journal = self.executor.journal
        if journal.run_plan is None:
            return None
        index = journal.current_step_index
        if index >= len(journal.run_plan.steps):
            return None
        return journal.run_plan.steps[index]

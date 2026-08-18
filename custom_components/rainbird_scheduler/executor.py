"""Journaled executor state machine (plan §17–§22, §27, §28).

Pure asyncio module: no Home Assistant imports. Every dependency with a
runtime effect — driver, journal persistence, timers, clock, history, event
emission — is injected, so the full state machine is testable with fakes.

Design rules from the plan:

* The commanded duration is the primary execution clock.
* No long coroutines with sleeps: every transition persists the journal,
  arms at most one future timer, and returns.
* Only fresh contradictory evidence (an observation after the expected end
  that still shows the zone on) blocks progression; stale switch state never
  does.
* A timed-out command is journaled UNCERTAIN and reconciled against
  observed state before any retry.
* Only one active run may exist per controller (single flight).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from datetime import datetime, timedelta
from typing import Any, Protocol

from .const import (
    COMMAND_RETRY_DELAYS_SECONDS,
    EVENT_CONTROLLER_OVERRUN,
    EVENT_RUN_COMPLETED,
    EVENT_RUN_FAILED,
    EVENT_RUN_INTERRUPTED,
    EVENT_RUN_SKIPPED,
    EVENT_RUN_STARTED,
    EVENT_SENSOR_CUT,
    EVENT_ZONE_COMPLETED,
    EVENT_ZONE_STARTED,
    MAX_COMMAND_ATTEMPTS,
)
from .driver.base import (
    DriverError,
    IrrigationDriver,
    ZoneValidationError,
)
from .models import (
    CommandDisposition,
    CommandType,
    ControllerConfig,
    ControllerObservation,
    ExecutionJournal,
    ExecutorState,
    FreezePolicy,
    FreezeUnavailablePolicy,
    InterruptionPolicy,
    PendingCommand,
    Program,
    RunOutcome,
    RunPlan,
    SensorCutBehavior,
    SkipReason,
    StepResult,
    StepStatus,
    ZoneReference,
    freeze_active,
    freeze_cleared,
)

# Distinguishes the two conditions that share ExecutorState.PAUSED_SENSOR.
_PAUSE_REASON_FREEZE = "low_temperature"

# Bounded best-effort retries for an uncertain stop while the run is still
# active (the controller also stops the zone on its own commanded timer, so
# more than this is noise, not safety).
_STOP_RETRY_DELAYS_SECONDS = (5, 20)

_LOGGER = logging.getLogger(__name__)

CancelTimer = Callable[[], None]


class TimerScheduler(Protocol):
    """Schedules one-shot callbacks; production wraps HA's event helpers."""

    def call_at(
        self, when: datetime, callback: Callable[[], Coroutine[Any, Any, None]]
    ) -> CancelTimer:
        """Run ``callback`` at ``when``; returns a cancel function."""
        ...


class JournalStore(Protocol):
    """Immediate journal persistence."""

    async def async_write_journal_now(self, journal: ExecutionJournal) -> None:
        """Persist the journal before the transition proceeds."""
        ...


class HistorySink(Protocol):
    """Receives finished-run records; persistence is the sink's concern."""

    def record_run_finished(
        self, journal: ExecutionJournal, outcome: RunOutcome, reason: str | None
    ) -> None:
        """Record a finished run with its per-step results."""
        ...

    def record_intervention(
        self, kind: str, message: str, run_id: str | None = None
    ) -> None:
        """Record a failure or intervention."""
        ...


EventEmitter = Callable[[str, dict[str, Any]], None]


class ControllerBusyError(Exception):
    """A watering run is already active on this controller."""

    def __init__(self, run_id: str, program_name: str) -> None:
        super().__init__(f"A watering run is already active ({program_name})")
        self.run_id = run_id
        self.program_name = program_name


class DuplicateOccurrenceError(Exception):
    """The occurrence already ran within the deduplication window."""


_TERMINAL_STEP_STATUSES = {
    StepStatus.COMPLETED,
    StepStatus.SKIPPED,
    StepStatus.SENSOR_CUT,
    StepStatus.EXTERNAL_STOP,
    StepStatus.OVERRUN_STOPPED,
    StepStatus.FAILED,
}

_ACTIVE_STATES = {
    ExecutorState.WAITING,
    ExecutorState.STARTING,
    ExecutorState.WATERING,
    ExecutorState.INTER_ZONE_GAP,
    ExecutorState.PAUSED_EXTERNAL,
    ExecutorState.PAUSED_SENSOR,
    ExecutorState.RECONCILING,
    ExecutorState.STOPPING,
}


class RunExecutor:
    """One executor and one execution lock per controller."""

    def __init__(
        self,
        *,
        driver: IrrigationDriver,
        journal: ExecutionJournal,
        journal_store: JournalStore,
        timers: TimerScheduler,
        now_fn: Callable[[], datetime],
        get_controller_config: Callable[[], ControllerConfig],
        get_program: Callable[[str], Program | None],
        get_zone_reference: Callable[[str], ZoneReference | None],
        emit_event: EventEmitter,
        history: HistorySink,
        on_state_change: Callable[[], None] = lambda: None,
    ) -> None:
        self._driver = driver
        self.journal = journal
        self._store = journal_store
        self._timers = timers
        self._now = now_fn
        self._get_config = get_controller_config
        self._get_program = get_program
        self._get_zone_reference = get_zone_reference
        self._emit = emit_event
        self._history = history
        self._notify = on_state_change
        self._lock = asyncio.Lock()
        self._cancel_timer: CancelTimer | None = None
        self._timer_generation = 0
        # Why RECONCILING was entered: "start" (uncertain command),
        # "overrun" (fresh contradictory end evidence), or "early_end"
        # (the running zone observed off well before its end). Not
        # persisted; on restart the current step's result discriminates
        # instead: a start-context reconcile leaves the step PENDING
        # (start re-classification is safe), while an end-context
        # reconcile leaves it RUNNING with actual_start_utc set (the zone
        # already watered, so recovery confirms against a fresh
        # observation and never re-sends the start).
        self._reconcile_context: str | None = None
        self._overrun_confirm_attempts = 0
        self._stop_retry_attempts = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_active(self) -> bool:
        """Return True while a run occupies the controller."""
        return self.journal.state in _ACTIVE_STATES

    async def async_start_run(self, plan: RunPlan) -> None:
        """Claim and start a run; single flight per controller."""
        async with self._lock:
            if self.is_active:
                assert self.journal.run_plan is not None
                raise ControllerBusyError(
                    self.journal.run_plan.run_id,
                    self.journal.run_plan.program_name,
                )
            if plan.occurrence_id in self.journal.completed_occurrences:
                raise DuplicateOccurrenceError(plan.occurrence_id)

            journal = self.journal
            journal.state = ExecutorState.WAITING
            journal.active_occurrence_id = plan.occurrence_id
            journal.run_plan = plan
            journal.current_step_index = 0
            journal.step_results = {
                step.index: StepResult() for step in plan.steps
            }
            journal.pending_command = None
            journal.current_step_actual_start = None
            journal.current_step_expected_end = None
            journal.retry_count = 0
            journal.uncertain_count = 0
            journal.stop_requested = False
            journal.paused_reason = None
            await self._write()
            self._emit(
                EVENT_RUN_STARTED,
                {
                    "run_id": plan.run_id,
                    "program_id": plan.program_id,
                    "program_name": plan.program_name,
                    "manual": plan.manual,
                },
            )
            if not plan.steps:
                await self._finish_run(
                    RunOutcome.SKIPPED, "no_executable_steps"
                )
                return
            await self._advance()
        self._notify()

    async def async_stop(self, *, reason: str = "user_stop") -> None:
        """Stop the controller; abort the active run if one exists."""
        async with self._lock:
            journal = self.journal
            journal.stop_requested = True
            had_run = self.is_active and journal.run_plan is not None
            if had_run:
                journal.state = ExecutorState.STOPPING
            self._cancel_pending_timer()
            await self._write()
            await self._send_stop()
            if had_run:
                current = self._current_result()
                if current is not None and current.status in (
                    StepStatus.RUNNING,
                    StepStatus.PENDING,
                ):
                    self._end_step(
                        self.journal.current_step_index,
                        StepStatus.EXTERNAL_STOP
                        if current.status is StepStatus.RUNNING
                        else StepStatus.SKIPPED,
                        reason,
                    )
                self._skip_remaining(SkipReason.USER_STOP, reason)
                await self._finish_run(RunOutcome.ABORTED_USER, reason)
            else:
                journal.stop_requested = False
                await self._write()
        self._notify()

    async def async_pause(self, *, reason: str = "user_pause") -> None:
        """Stop watering but keep the run resumable."""
        async with self._lock:
            if not self.is_active or self.journal.run_plan is None:
                return
            state = self.journal.state
            self._cancel_pending_timer()
            current = self._current_result()
            if current is not None and current.status is StepStatus.RUNNING:
                await self._send_stop()
                self._end_step(
                    self.journal.current_step_index,
                    StepStatus.EXTERNAL_STOP,
                    reason,
                )
            elif (
                state is ExecutorState.RECONCILING
                and current is not None
                and current.status is StepStatus.PENDING
            ):
                # Leaving a start-context reconcile: the uncertain start
                # may still have been accepted, so send a best-effort
                # stop (an uncertain stop here must not block the pause).
                await self._send_stop()
            self.journal.state = ExecutorState.PAUSED_EXTERNAL
            self.journal.paused_reason = reason
            await self._write()
        self._notify()

    async def async_resume(self) -> None:
        """Resume a paused run, honoring the missed-run tolerance."""
        async with self._lock:
            if self.journal.state not in (
                ExecutorState.PAUSED_EXTERNAL,
                ExecutorState.PAUSED_SENSOR,
            ):
                return
            self.journal.paused_reason = None
            await self._resume_or_expire()
        self._notify()

    async def async_skip_current(self) -> None:
        """Skip the active step and continue with the next one."""
        async with self._lock:
            if self.journal.run_plan is None or not self.is_active:
                return
            current = self._current_result()
            if current is None:
                return
            state = self.journal.state
            self._cancel_pending_timer()
            if current.status is StepStatus.RUNNING:
                await self._send_stop()
            elif (
                state is ExecutorState.RECONCILING
                and current.status is StepStatus.PENDING
            ):
                # Leaving a start-context reconcile: the uncertain start
                # may still have been accepted, so send a best-effort
                # stop (an uncertain stop here must not block the skip).
                await self._send_stop()
            self._end_step(
                self.journal.current_step_index,
                StepStatus.SKIPPED,
                "user_skip",
            )
            self.journal.state = ExecutorState.INTER_ZONE_GAP
            await self._write()
            self._arm_in(self._config().inter_zone_gap_seconds, self._on_gap)
        self._notify()

    async def async_handle_zone_state(
        self, zone_id: str, is_on: bool, observation: ControllerObservation
    ) -> None:
        """Feed a source zone state change into the state machine."""
        async with self._lock:
            self.journal.last_observation = observation
            state = self.journal.state
            if self.journal.run_plan is None:
                return
            if state is ExecutorState.WATERING or state in (
                ExecutorState.STARTING,
                ExecutorState.RECONCILING,
            ):
                await self._zone_event_during_run(zone_id, is_on, observation)
            elif state is ExecutorState.INTER_ZONE_GAP:
                await self._zone_event_during_gap(zone_id, is_on, observation)
            elif (
                state is ExecutorState.PAUSED_EXTERNAL
                and not observation.active_zone_ids
                and self.journal.paused_reason is None
            ) or (
                state is ExecutorState.PAUSED_SENSOR
                and self._sensor_pause_cleared(observation)
            ):
                await self._resume_or_expire()
        self._notify()

    async def async_handle_sensor_state(
        self, observation: ControllerObservation
    ) -> None:
        """Feed a rain-sensor change (used to resume sensor pauses)."""
        async with self._lock:
            self.journal.last_observation = observation
            if (
                self.journal.state is ExecutorState.PAUSED_SENSOR
                and self._sensor_pause_cleared(observation)
            ):
                await self._resume_or_expire()
        self._notify()

    async def async_handle_weather_state(
        self, observation: ControllerObservation
    ) -> None:
        """Feed a temperature change: cut an active run on freeze, or resume."""
        async with self._lock:
            self.journal.last_observation = observation
            if self.journal.run_plan is None:
                return
            state = self.journal.state
            guard = self._config().freeze_guard
            if state is ExecutorState.WATERING:
                program = self._get_program(self._plan().program_id)
                if (
                    guard.enabled
                    and self._freeze_policy(program).skip_when_freezing
                    and freeze_active(guard, observation) is True
                ):
                    await self._freeze_cut()
            elif (
                state is ExecutorState.PAUSED_SENSOR
                and self._sensor_pause_cleared(observation)
            ):
                await self._resume_or_expire()
        self._notify()

    def _sensor_pause_cleared(
        self, observation: ControllerObservation
    ) -> bool:
        """May a sensor pause resume? Both conditions must be clear.

        A freeze pause needs the temperature back above threshold + hysteresis
        (and not newly wet); a rain-wet pause needs the boolean dry (and not
        newly freezing) — so a run never resumes straight into the other block.
        """
        guard = self._config().freeze_guard
        if self.journal.paused_reason == _PAUSE_REASON_FREEZE:
            return (
                freeze_cleared(guard, observation)
                and observation.rain_sensor_active is not True
            )
        return (
            observation.rain_sensor_active is False
            and freeze_active(guard, observation) is not True
        )

    async def _freeze_cut(self) -> None:
        """Software freeze cut of the active zone: stop, then apply behavior.

        Unlike a rain cut (the hardware already closed the valve), the zone is
        still running, so this issues an explicit stop before classifying.
        """
        journal = self.journal
        plan = self._plan()
        index = journal.current_step_index
        step = plan.steps[index]
        self._cancel_pending_timer()
        await self._send_stop()
        self._end_step(index, StepStatus.SENSOR_CUT, _PAUSE_REASON_FREEZE)
        self._emit(
            EVENT_SENSOR_CUT,
            {
                "run_id": plan.run_id,
                "zone_id": step.zone_id,
                "zone_name": step.zone_name,
                "kind": "freeze",
            },
        )
        behavior = self._freeze_policy(
            self._get_program(plan.program_id)
        ).freeze_cut_behavior
        if behavior is SensorCutBehavior.PAUSE_UNTIL_DRY:
            journal.state = ExecutorState.PAUSED_SENSOR
            journal.paused_reason = _PAUSE_REASON_FREEZE
            await self._write()
            return
        detail = (
            "deferred"
            if behavior is SensorCutBehavior.DEFER_REMAINING
            else "freeze cut"
        )
        self._skip_remaining(SkipReason.LOW_TEMPERATURE, detail)
        await self._finish_run(
            RunOutcome.ABORTED_SENSOR, SkipReason.LOW_TEMPERATURE.value
        )

    async def async_recover(self) -> None:
        """Reconcile the persisted journal against observed state (§28)."""
        async with self._lock:
            journal = self.journal
            if not self.is_active or journal.run_plan is None:
                return
            try:
                await self._driver.async_request_observation()
            except DriverError:
                pass
            try:
                observation = await self._driver.async_observe()
            except DriverError:
                observation = None
            if observation is not None:
                journal.last_observation = observation
            await self._recover_locked(observation)
        self._notify()

    def shutdown(self) -> None:
        """Cancel any armed timer; safe to call multiple times."""
        self._cancel_pending_timer()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _config(self) -> ControllerConfig:
        return self._get_config()

    def _plan(self) -> RunPlan:
        assert self.journal.run_plan is not None
        return self.journal.run_plan

    def _current_result(self) -> StepResult | None:
        return self.journal.step_results.get(self.journal.current_step_index)

    def _current_zone_id(self) -> str | None:
        plan = self.journal.run_plan
        if plan is None:
            return None
        index = self.journal.current_step_index
        if index >= len(plan.steps):
            return None
        return plan.steps[index].zone_id

    async def _write(self) -> None:
        self.journal.updated_at_utc = self._now()
        await self._store.async_write_journal_now(self.journal)

    def _cancel_pending_timer(self) -> None:
        self._timer_generation += 1
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None

    def _arm_at(
        self,
        when: datetime,
        handler: Callable[[], Awaitable[None]],
    ) -> None:
        self._cancel_pending_timer()
        generation = self._timer_generation

        async def _fire() -> None:
            async with self._lock:
                if generation != self._timer_generation:
                    return
                self._cancel_timer = None
                await handler()
            self._notify()

        self._cancel_timer = self._timers.call_at(when, _fire)

    def _arm_in(
        self, seconds: float, handler: Callable[[], Awaitable[None]]
    ) -> None:
        self._arm_at(self._now() + timedelta(seconds=seconds), handler)

    def _latest_permissible_start(self) -> datetime:
        plan = self._plan()
        tolerance = timedelta(
            minutes=self._config().missed_run_tolerance_minutes
        )
        return plan.requested_start_utc + tolerance

    def _fresh_evidence_zone_on(self, zone_id: str, since: datetime) -> bool:
        observation = self.journal.last_observation
        if observation is None:
            return False
        return (
            zone_id in observation.active_zone_ids
            and observation.observed_at_utc >= since
        )

    async def _send_stop(self) -> None:
        command = PendingCommand.for_stop(self._now())
        self.journal.pending_command = command
        await self._write()
        try:
            await self._driver.async_stop_controller()
            command.disposition = CommandDisposition.SENT
            self._stop_retry_attempts = 0
        except DriverError as err:
            command.disposition = CommandDisposition.UNCERTAIN
            self.journal.uncertain_count += 1
            _LOGGER.warning("Stop command uncertain: %s", err)
        await self._write()
        if (
            command.disposition is CommandDisposition.UNCERTAIN
            and self.is_active
            and self._stop_retry_attempts < len(_STOP_RETRY_DELAYS_SECONDS)
        ):
            # A genuinely failed stop has no other watchdog once the
            # caller moves on: retry a bounded number of times while the
            # run stays active.
            delay = _STOP_RETRY_DELAYS_SECONDS[self._stop_retry_attempts]
            self._stop_retry_attempts += 1
            self._arm_in(delay, self._retry_uncertain_stop)

    async def _retry_uncertain_stop(self) -> None:
        """Re-send a stop whose last attempt never confirmed."""
        command = self.journal.pending_command
        if (
            command is None
            or command.command_type is not CommandType.STOP_CONTROLLER
            or command.disposition is not CommandDisposition.UNCERTAIN
        ):
            return
        await self._send_stop()

    # ------------------------------------------------------------------
    # Step lifecycle
    # ------------------------------------------------------------------

    async def _advance(self) -> None:
        """Start the next pending step, or finish the run."""
        journal = self.journal
        plan = self._plan()
        next_index = None
        for step in plan.steps:
            result = journal.step_results.get(step.index)
            if result is not None and result.status is StepStatus.PENDING:
                next_index = step.index
                break
        if next_index is None:
            statuses = {r.status for r in journal.step_results.values()}
            if statuses <= {StepStatus.COMPLETED}:
                await self._finish_run(RunOutcome.COMPLETED, None)
            else:
                await self._finish_run(
                    RunOutcome.COMPLETED_WITH_SKIPS, "some_steps_skipped"
                )
            return
        journal.current_step_index = next_index
        # No planned-start wait here: _advance is also the resume path
        # after pauses and user skips, where the compiled times are stale
        # and continuing immediately is the intent. The soak wait is
        # enforced where steps complete on plan (_arm_gap_to_next_start).
        await self._start_step(next_index)

    def _next_pending_index(self) -> int | None:
        journal = self.journal
        plan = self._plan()
        for step in plan.steps:
            result = journal.step_results.get(step.index)
            if result is not None and result.status is StepStatus.PENDING:
                return step.index
        return None

    async def _start_step(self, index: int) -> None:
        journal = self.journal
        plan = self._plan()
        step = plan.steps[index]

        blocked = await self._pre_step_block(index)
        if blocked:
            return

        reference = self._get_zone_reference(step.zone_id)
        if reference is None:
            self._end_step(
                index, StepStatus.FAILED, SkipReason.SOURCE_UNAVAILABLE.value
            )
            self._history.record_intervention(
                "zone_unresolved",
                f"Zone {step.zone_name!r} could not be resolved to a "
                "Rain Bird entity; run aborted.",
                run_id=plan.run_id,
            )
            self._skip_remaining(
                SkipReason.SOURCE_UNAVAILABLE, "zone_unresolved"
            )
            await self._finish_run(RunOutcome.FAILED, "zone_unresolved")
            return

        attempt = journal.step_results[index].attempts + 1
        command = PendingCommand.for_step(step, self._now(), attempt)
        journal.state = ExecutorState.STARTING
        journal.pending_command = command
        journal.step_results[index].attempts = attempt
        await self._write()

        try:
            await self._driver.async_start_zone(
                reference, step.duration_minutes, command.command_id
            )
        except ZoneValidationError as err:
            command.disposition = CommandDisposition.REJECTED
            self._end_step(index, StepStatus.FAILED, str(err))
            self._history.record_intervention(
                "zone_validation_failed", str(err), run_id=plan.run_id
            )
            self._skip_remaining(
                SkipReason.SOURCE_UNAVAILABLE, "zone_validation_failed"
            )
            await self._finish_run(RunOutcome.FAILED, "zone_validation_failed")
            return
        except DriverError as err:
            await self._reconcile_uncertain_start(index, err)
            return

        now = self._now()
        journal.state = ExecutorState.WATERING
        journal.current_step_actual_start = now
        journal.current_step_expected_end = now + timedelta(
            minutes=step.duration_minutes
        )
        command.disposition = CommandDisposition.SENT
        journal.step_results[index].status = StepStatus.RUNNING
        journal.step_results[index].actual_start_utc = now
        await self._write()
        self._emit(
            EVENT_ZONE_STARTED,
            {
                "run_id": plan.run_id,
                "program_id": plan.program_id,
                "zone_id": step.zone_id,
                "zone_name": step.zone_name,
                "cycle": f"{step.cycle_index}/{step.cycle_count}",
                "duration_minutes": step.duration_minutes,
            },
        )
        self._arm_at(
            journal.current_step_expected_end, self._on_expected_end
        )

    async def _pre_step_block(self, index: int) -> bool:
        """Apply rain and external checks before each zone step (§23, §27)."""
        journal = self.journal
        plan = self._plan()
        program = self._get_program(plan.program_id)
        observation = journal.last_observation
        rain_policy = (
            program.rain_policy
            if program is not None
            else self._config().default_rain_policy
        )

        if observation is not None:
            if (
                rain_policy.honor_native_delay
                and observation.rain_delay_days is not None
                and observation.rain_delay_days > 0
            ):
                self._skip_remaining(
                    SkipReason.RAIN_DELAY,
                    f"native rain delay {observation.rain_delay_days} day(s)",
                )
                await self._finish_run(
                    RunOutcome.COMPLETED_WITH_SKIPS
                    if self._any_completed()
                    else RunOutcome.SKIPPED,
                    SkipReason.RAIN_DELAY.value,
                )
                return True
            if (
                rain_policy.skip_when_sensor_wet
                and observation.rain_sensor_active
            ):
                self._skip_remaining(
                    SkipReason.RAIN_SENSOR_WET, "rain sensor wet"
                )
                await self._finish_run(
                    RunOutcome.ABORTED_SENSOR
                    if self._any_completed()
                    else RunOutcome.SKIPPED,
                    SkipReason.RAIN_SENSOR_WET.value,
                )
                return True

            if self._freeze_blocks(program, observation):
                self._skip_remaining(
                    SkipReason.LOW_TEMPERATURE, "temperature at/below threshold"
                )
                await self._finish_run(
                    RunOutcome.ABORTED_SENSOR
                    if self._any_completed()
                    else RunOutcome.SKIPPED,
                    SkipReason.LOW_TEMPERATURE.value,
                )
                return True

            external = self._external_zones(observation, index)
            if external:
                journal.state = ExecutorState.PAUSED_EXTERNAL
                await self._write()
                self._emit(
                    EVENT_RUN_INTERRUPTED,
                    {
                        "run_id": plan.run_id,
                        "reason": SkipReason.EXTERNAL_ACTIVITY.value,
                        "zones": sorted(external),
                    },
                )
                return True
        return False

    def _freeze_policy(self, program: Program | None) -> FreezePolicy:
        return (
            program.freeze_policy
            if program is not None
            else self._config().default_freeze_policy
        )

    def _freeze_blocks(
        self, program: Program | None, observation: ControllerObservation
    ) -> bool:
        """True when the freeze guard should stop a run from proceeding.

        Definite cold blocks; unknown temperature blocks only under the
        block-when-unavailable policy (matching the pre-occurrence check).
        """
        guard = self._config().freeze_guard
        if not guard.enabled or not self._freeze_policy(program).skip_when_freezing:
            return False
        active = freeze_active(guard, observation)
        if active is True:
            return True
        return active is None and guard.when_unavailable is (
            FreezeUnavailablePolicy.BLOCK_WATERING
        )

    def _external_zones(
        self, observation: ControllerObservation, upcoming_index: int
    ) -> frozenset[str]:
        """Active zones that are not explained by our own previous step."""
        plan = self._plan()
        active = set(observation.active_zone_ids)
        if not active:
            return frozenset()
        for step in plan.steps[:upcoming_index]:
            result = self.journal.step_results.get(step.index)
            if result is None or result.actual_end_utc is None:
                continue
            # A stale "on" for a zone we just finished is not evidence of
            # external activity unless observed after that zone ended.
            if (
                step.zone_id in active
                and observation.observed_at_utc <= result.actual_end_utc
            ):
                active.discard(step.zone_id)
        return frozenset(active)

    def _any_completed(self) -> bool:
        return any(
            result.status is StepStatus.COMPLETED
            for result in self.journal.step_results.values()
        )

    def _end_step(self, index: int, status: StepStatus, reason: str | None) -> None:
        result = self.journal.step_results.get(index)
        if result is None:
            return
        result.status = status
        result.reason = reason
        if result.actual_end_utc is None and status is not StepStatus.SKIPPED:
            result.actual_end_utc = self._now()

    def _skip_remaining(self, reason: SkipReason, detail: str) -> None:
        for result in self.journal.step_results.values():
            if result.status in (StepStatus.PENDING, StepStatus.RUNNING):
                if result.status is StepStatus.RUNNING:
                    result.status = StepStatus.EXTERNAL_STOP
                    result.actual_end_utc = self._now()
                else:
                    result.status = StepStatus.SKIPPED
                result.reason = f"{reason.value}:{detail}" if detail else reason.value

    async def _complete_step(
        self, index: int, actual_end: datetime | None = None
    ) -> None:
        journal = self.journal
        plan = self._plan()
        step = plan.steps[index]
        result = journal.step_results[index]
        result.status = StepStatus.COMPLETED
        result.actual_end_utc = actual_end or self._now()
        journal.state = ExecutorState.INTER_ZONE_GAP
        journal.pending_command = None
        journal.current_step_expected_end = None
        self._reconcile_context = None
        await self._write()
        self._emit(
            EVENT_ZONE_COMPLETED,
            {
                "run_id": plan.run_id,
                "zone_id": step.zone_id,
                "zone_name": step.zone_name,
                "cycle": f"{step.cycle_index}/{step.cycle_count}",
            },
        )
        self._arm_gap_to_next_start()

    def _arm_gap_to_next_start(self) -> None:
        """Arm _on_gap at the gap end — or the planned start for soaks.

        A step with ``soak_before`` must not start before its compiled
        start time: that time carries the zone's minimum soak (§16), and
        advancing at the bare gap would re-command a zone seconds after
        the controller stopped it, skipping the soak and racing the next
        observation into a phantom "external stop". Steps without a soak
        keep the old behavior (start after the gap, even when the prior
        zone finished early).
        """
        plan = self._plan()
        target = self._now() + timedelta(
            seconds=self._config().inter_zone_gap_seconds
        )
        next_index = self._next_pending_index()
        if next_index is not None:
            next_step = plan.steps[next_index]
            if next_step.soak_before and next_step.planned_start_utc > target:
                target = next_step.planned_start_utc
        self._arm_at(target, self._on_gap)

    async def _on_expected_end(self) -> None:
        journal = self.journal
        if journal.state is not ExecutorState.WATERING:
            return
        index = journal.current_step_index
        zone_id = self._current_zone_id()
        expected_end = journal.current_step_expected_end
        if (
            zone_id is not None
            and expected_end is not None
            and self._fresh_evidence_zone_on(zone_id, expected_end)
        ):
            await self._enter_overrun_reconcile()
            return
        await self._complete_step(index, actual_end=expected_end)

    async def _on_gap(self) -> None:
        if self.journal.state is not ExecutorState.INTER_ZONE_GAP:
            return
        await self._advance()

    # ------------------------------------------------------------------
    # Overrun handling (§20)
    # ------------------------------------------------------------------

    async def _enter_overrun_reconcile(self) -> None:
        journal = self.journal
        journal.state = ExecutorState.RECONCILING
        self._reconcile_context = "overrun"
        self._overrun_confirm_attempts = 0
        await self._write()
        try:
            await self._driver.async_request_observation()
        except DriverError:
            pass
        self._arm_in(
            self._config().overrun_confirmation_seconds, self._confirm_overrun
        )

    async def _confirm_overrun(self) -> None:
        journal = self.journal
        if (
            journal.state is not ExecutorState.RECONCILING
            or self._reconcile_context != "overrun"
        ):
            return
        index = journal.current_step_index
        zone_id = self._current_zone_id()
        expected_end = journal.current_step_expected_end
        # Read the source live rather than trusting the last change event:
        # the question is whether the zone is on NOW, not at the time some
        # earlier observation was delivered. A failed read is not fresh
        # evidence either way: fall back to the last observation (as
        # _classify_uncertain does) rather than wedge in RECONCILING.
        observation: ControllerObservation | None
        try:
            observation = await self._driver.async_observe()
            journal.last_observation = observation
        except DriverError:
            observation = journal.last_observation
        still_on = (
            zone_id is not None
            and expected_end is not None
            and observation is not None
            and zone_id in observation.active_zone_ids
        )
        if not still_on:
            await self._complete_step(index, actual_end=expected_end)
            return
        if self._overrun_confirm_attempts == 0:
            # A read straight after the expected end can catch the
            # controller a few seconds from stopping on its own (its timer
            # starts at command processing, after ours) or a poll carrying
            # pre-stop data. Demand a second confirmation cycle before
            # stopping the whole controller.
            self._overrun_confirm_attempts = 1
            try:
                await self._driver.async_request_observation()
            except DriverError:
                pass
            self._arm_in(
                self._config().overrun_confirmation_seconds,
                self._confirm_overrun,
            )
            return

        plan = self._plan()
        step = plan.steps[index]
        await self._send_stop()
        self._end_step(index, StepStatus.OVERRUN_STOPPED, "controller_overrun")
        self._skip_remaining(SkipReason.EXTERNAL_ACTIVITY, "controller_overrun")
        self._history.record_intervention(
            "controller_overrun",
            f"Zone {step.zone_name!r} remained active past its commanded "
            "end; the controller was stopped.",
            run_id=plan.run_id,
        )
        self._emit(
            EVENT_CONTROLLER_OVERRUN,
            {"run_id": plan.run_id, "zone_id": zone_id},
        )
        await self._finish_run(RunOutcome.FAILED, "controller_overrun")

    # ------------------------------------------------------------------
    # Early-end confirmation (§22)
    # ------------------------------------------------------------------

    async def _enter_early_end_reconcile(self) -> None:
        journal = self.journal
        journal.state = ExecutorState.RECONCILING
        self._reconcile_context = "early_end"
        await self._write()
        try:
            await self._driver.async_request_observation()
        except DriverError:
            pass
        self._arm_in(
            self._config().overrun_confirmation_seconds,
            self._confirm_early_end,
        )

    async def _confirm_early_end(self) -> None:
        journal = self.journal
        if (
            journal.state is not ExecutorState.RECONCILING
            or self._reconcile_context != "early_end"
        ):
            return
        index = journal.current_step_index
        zone_id = self._current_zone_id()
        # Same fallback as _classify_uncertain: a failed read falls back to
        # the last observation instead of wedging the run in RECONCILING.
        observation: ControllerObservation | None
        try:
            observation = await self._driver.async_observe()
            journal.last_observation = observation
        except DriverError:
            observation = journal.last_observation
        if observation is None or (
            zone_id is not None and zone_id in observation.active_zone_ids
        ):
            # The zone is on after a refresh (or no contradicting read is
            # available at all): the early "off" was stale source data,
            # not a real stop. Resume the step in place.
            self._resume_watering_current_step()
            await self._write()
            return
        self._reconcile_context = None
        await self._classify_early_end(index, observation)

    def _resume_watering_current_step(self) -> None:
        """Return from an early-end reconcile to WATERING unchanged."""
        journal = self.journal
        self._reconcile_context = None
        journal.state = ExecutorState.WATERING
        command = journal.pending_command
        if command is not None and command.disposition is (
            CommandDisposition.SENT
        ):
            command.disposition = CommandDisposition.ACCEPTED
        expected_end = journal.current_step_expected_end
        if expected_end is not None:
            self._arm_at(expected_end, self._on_expected_end)

    # ------------------------------------------------------------------
    # Uncertain command handling (§21)
    # ------------------------------------------------------------------

    async def _reconcile_uncertain_start(
        self, index: int, error: Exception
    ) -> None:
        journal = self.journal
        command = journal.pending_command
        if command is not None:
            command.disposition = CommandDisposition.UNCERTAIN
        journal.uncertain_count += 1
        journal.state = ExecutorState.RECONCILING
        self._reconcile_context = "start"
        await self._write()
        _LOGGER.warning(
            "Start command for step %s uncertain: %s", index, error
        )
        try:
            await self._driver.async_request_observation()
        except DriverError:
            pass
        self._arm_in(
            self._config().start_observation_timeout_seconds,
            self._classify_uncertain,
        )

    async def _classify_uncertain(self) -> None:
        journal = self.journal
        if journal.state is not ExecutorState.RECONCILING:
            return
        index = journal.current_step_index
        plan = self._plan()
        step = plan.steps[index]
        command = journal.pending_command
        observation: ControllerObservation | None
        try:
            observation = await self._driver.async_observe()
            journal.last_observation = observation
        except DriverError:
            observation = journal.last_observation

        now = self._now()
        if observation is not None and step.zone_id in observation.active_zone_ids:
            # Requested zone active: treat the command as accepted.
            if command is not None:
                command.disposition = CommandDisposition.ACCEPTED
            start = command.intended_at_utc if command else now
            journal.state = ExecutorState.WATERING
            journal.current_step_actual_start = start
            journal.current_step_expected_end = start + timedelta(
                minutes=step.duration_minutes
            )
            journal.step_results[index].status = StepStatus.RUNNING
            journal.step_results[index].actual_start_utc = start
            await self._write()
            self._emit(
                EVENT_ZONE_STARTED,
                {
                    "run_id": plan.run_id,
                    "program_id": plan.program_id,
                    "zone_id": step.zone_id,
                    "zone_name": step.zone_name,
                    "cycle": f"{step.cycle_index}/{step.cycle_count}",
                    "duration_minutes": step.duration_minutes,
                },
            )
            if journal.current_step_expected_end <= now:
                await self._complete_step(
                    index, actual_end=journal.current_step_expected_end
                )
            else:
                self._arm_at(
                    journal.current_step_expected_end, self._on_expected_end
                )
            return

        external = (
            self._external_zones(observation, index)
            if observation is not None
            else frozenset()
        )
        if external:
            # Different zone active: external conflict.
            await self._external_conflict(external)
            return

        source_available = (
            observation.source_available if observation is not None else False
        )
        attempts = journal.step_results[index].attempts
        if command is not None:
            command.disposition = CommandDisposition.REJECTED
        if (
            attempts < MAX_COMMAND_ATTEMPTS
            and now <= self._latest_permissible_start()
        ):
            journal.retry_count += 1
            await self._write()
            delay = COMMAND_RETRY_DELAYS_SECONDS[
                min(attempts - 1, len(COMMAND_RETRY_DELAYS_SECONDS) - 1)
            ]
            if not source_available:
                _LOGGER.warning(
                    "Controller unavailable; retrying step %s in %ss",
                    index,
                    delay,
                )
            self._arm_in(delay, self._retry_step)
            return

        self._end_step(index, StepStatus.FAILED, SkipReason.COMMAND_FAILED.value)
        self._skip_remaining(SkipReason.COMMAND_FAILED, "start command failed")
        self._history.record_intervention(
            "command_failed",
            f"Starting zone {step.zone_name!r} failed after "
            f"{attempts} attempt(s).",
            run_id=plan.run_id,
        )
        await self._finish_run(RunOutcome.FAILED, "command_failed")

    async def _retry_step(self) -> None:
        journal = self.journal
        if journal.state is not ExecutorState.RECONCILING:
            return
        journal.state = ExecutorState.WAITING
        await self._start_step(journal.current_step_index)

    # ------------------------------------------------------------------
    # Zone state events (§19, §22, §27)
    # ------------------------------------------------------------------

    async def _zone_event_during_run(
        self, zone_id: str, is_on: bool, observation: ControllerObservation
    ) -> None:
        journal = self.journal
        index = journal.current_step_index
        current_zone = self._current_zone_id()
        plan = self._plan()

        if is_on and zone_id == current_zone:
            command = journal.pending_command
            if command is not None and command.disposition in (
                CommandDisposition.SENT,
                CommandDisposition.UNCERTAIN,
            ):
                command.disposition = CommandDisposition.ACCEPTED
                await self._write()
            if (
                journal.state is ExecutorState.RECONCILING
                and self._reconcile_context == "early_end"
            ):
                # Direct evidence the early "off" was stale: resume the step.
                self._resume_watering_current_step()
                await self._write()
                return
            if journal.state is ExecutorState.STARTING or (
                journal.state is ExecutorState.RECONCILING
                and self._reconcile_context == "start"
            ):
                # Acceptance observed before classification finished.
                step = plan.steps[index]
                start = observation.observed_at_utc
                journal.state = ExecutorState.WATERING
                journal.current_step_actual_start = start
                journal.current_step_expected_end = start + timedelta(
                    minutes=step.duration_minutes
                )
                journal.step_results[index].status = StepStatus.RUNNING
                journal.step_results[index].actual_start_utc = start
                await self._write()
                self._emit(
                    EVENT_ZONE_STARTED,
                    {
                        "run_id": plan.run_id,
                        "program_id": plan.program_id,
                        "zone_id": step.zone_id,
                        "zone_name": step.zone_name,
                        "cycle": f"{step.cycle_index}/{step.cycle_count}",
                        "duration_minutes": step.duration_minutes,
                    },
                )
                self._arm_at(
                    journal.current_step_expected_end, self._on_expected_end
                )
            return

        if is_on and zone_id != current_zone:
            # A late poll can carry a cached "on" for the zone that just
            # finished: apply the same staleness discount _external_zones
            # gives observations before declaring a conflict.
            if zone_id in self._external_zones(observation, index):
                await self._external_conflict(frozenset({zone_id}))
            return

        if not is_on and zone_id == current_zone:
            if journal.state is not ExecutorState.WATERING:
                return
            expected_end = journal.current_step_expected_end
            if expected_end is None:
                return
            now = observation.observed_at_utc
            tolerance = timedelta(
                seconds=self._config().early_end_tolerance_seconds
            )
            if now >= expected_end - tolerance:
                # Ended within tolerance: early completion.
                self._cancel_pending_timer()
                await self._complete_step(index, actual_end=now)
                return
            if observation.rain_sensor_active or not observation.source_available:
                # The stop arrives with its own explanation: classify now.
                await self._classify_early_end(index, observation)
                return
            # An unexplained early "off" is not trusted: the source polls
            # a slow controller, so an observation in flight when the zone
            # was commanded can land afterwards carrying pre-start state.
            # Ask for a refresh and confirm before classifying (§22).
            await self._enter_early_end_reconcile()

    async def _zone_event_during_gap(
        self, zone_id: str, is_on: bool, observation: ControllerObservation
    ) -> None:
        if not is_on:
            return
        journal = self.journal
        index = journal.current_step_index
        plan = self._plan()
        step = plan.steps[index]
        result = journal.step_results.get(index)
        if (
            zone_id == step.zone_id
            and result is not None
            and result.actual_end_utc is not None
            and observation.observed_at_utc > result.actual_end_utc
        ):
            # Our just-finished zone reports on after its end: overrun path.
            journal.current_step_expected_end = result.actual_end_utc
            await self._enter_overrun_reconcile()
            return
        if zone_id != step.zone_id:
            await self._external_conflict(frozenset({zone_id}))

    async def _classify_early_end(
        self, index: int, observation: ControllerObservation
    ) -> None:
        """Classify an early zone end (§22)."""
        journal = self.journal
        plan = self._plan()
        step = plan.steps[index]
        self._cancel_pending_timer()

        if journal.stop_requested:
            return  # The stop path owns the bookkeeping.

        if observation.rain_sensor_active:
            self._end_step(index, StepStatus.SENSOR_CUT, "rain sensor active")
            self._emit(
                EVENT_SENSOR_CUT,
                {
                    "run_id": plan.run_id,
                    "zone_id": step.zone_id,
                    "zone_name": step.zone_name,
                },
            )
            program = self._get_program(plan.program_id)
            behavior = (
                program.rain_policy.sensor_cut_behavior
                if program is not None
                else self._config().default_rain_policy.sensor_cut_behavior
            )
            if behavior is SensorCutBehavior.PAUSE_UNTIL_DRY:
                journal.state = ExecutorState.PAUSED_SENSOR
                await self._write()
                return
            detail = (
                "deferred"
                if behavior is SensorCutBehavior.DEFER_REMAINING
                else "sensor cut"
            )
            self._skip_remaining(SkipReason.RAIN_SENSOR_WET, detail)
            await self._finish_run(
                RunOutcome.ABORTED_SENSOR, SkipReason.RAIN_SENSOR_WET.value
            )
            return

        if not observation.source_available:
            self._end_step(
                index, StepStatus.EXTERNAL_STOP, "controller_power_loss"
            )
            self._skip_remaining(
                SkipReason.SOURCE_UNAVAILABLE, "controller power loss"
            )
            await self._finish_run(
                RunOutcome.ABORTED_POWER_LOSS, "controller_power_loss"
            )
            return

        self._end_step(index, StepStatus.EXTERNAL_STOP, "external_stop")
        self._emit(
            EVENT_RUN_INTERRUPTED,
            {
                "run_id": plan.run_id,
                "reason": "external_stop",
                "zone_id": step.zone_id,
            },
        )
        self._skip_remaining(SkipReason.EXTERNAL_ACTIVITY, "external stop")
        await self._finish_run(RunOutcome.ABORTED_EXTERNAL, "external_stop")

    async def _external_conflict(self, zones: frozenset[str]) -> None:
        journal = self.journal
        plan = self._plan()
        program = self._get_program(plan.program_id)
        policy = (
            program.external_interruption_policy
            if program is not None
            else InterruptionPolicy.PAUSE
        )
        self._cancel_pending_timer()
        current = self._current_result()
        if current is not None and current.status is StepStatus.RUNNING:
            self._end_step(
                journal.current_step_index,
                StepStatus.EXTERNAL_STOP,
                "external_zone_active",
            )
        self._emit(
            EVENT_RUN_INTERRUPTED,
            {
                "run_id": plan.run_id,
                "reason": SkipReason.EXTERNAL_ACTIVITY.value,
                "zones": sorted(zones),
            },
        )
        if policy is InterruptionPolicy.ABORT:
            self._skip_remaining(
                SkipReason.EXTERNAL_ACTIVITY, "external zone active"
            )
            await self._finish_run(
                RunOutcome.ABORTED_EXTERNAL,
                SkipReason.EXTERNAL_ACTIVITY.value,
            )
            return
        journal.state = ExecutorState.PAUSED_EXTERNAL
        await self._write()

    async def _resume_or_expire(self) -> None:
        """Continue a paused run, or expire it past the tolerance."""
        journal = self.journal
        journal.paused_reason = None
        if self._now() <= self._latest_permissible_start():
            journal.state = ExecutorState.WAITING
            await self._write()
            await self._advance()
            return
        self._skip_remaining(
            SkipReason.MISSED_TOLERANCE, "paused past missed-run tolerance"
        )
        await self._finish_run(
            RunOutcome.COMPLETED_WITH_SKIPS
            if self._any_completed()
            else RunOutcome.SKIPPED,
            SkipReason.MISSED_TOLERANCE.value,
        )

    # ------------------------------------------------------------------
    # Restart recovery (§28)
    # ------------------------------------------------------------------

    async def _complete_or_expire_step(
        self, index: int, expected_end: datetime, now: datetime
    ) -> None:
        """Complete a recovered step past its end, or expire the run."""
        tolerance = timedelta(
            minutes=self._config().missed_run_tolerance_minutes
        )
        if now - expected_end <= tolerance:
            await self._complete_step(index, actual_end=expected_end)
            return
        self._end_step(
            index,
            StepStatus.COMPLETED,
            SkipReason.RESTART_MISSED_TOLERANCE.value,
        )
        self._skip_remaining(
            SkipReason.RESTART_MISSED_TOLERANCE,
            "restart after expected completion",
        )
        await self._finish_run(
            RunOutcome.COMPLETED_WITH_SKIPS,
            SkipReason.RESTART_MISSED_TOLERANCE.value,
        )

    async def _recover_locked(
        self, observation: ControllerObservation | None
    ) -> None:
        journal = self.journal
        plan = self._plan()
        index = journal.current_step_index
        if index >= len(plan.steps):
            await self._finish_run(
                RunOutcome.COMPLETED_WITH_SKIPS, "recovered_past_end"
            )
            return
        step = plan.steps[index]
        now = self._now()
        zone_active = (
            observation is not None
            and step.zone_id in observation.active_zone_ids
        )
        external = (
            self._external_zones(observation, index)
            if observation is not None
            else frozenset()
        ) - {step.zone_id}

        state = journal.state
        if state is ExecutorState.STOPPING or journal.stop_requested:
            # A persisted stop request takes precedence over the external
            # activity check: finish the abort even if another zone looks
            # active, or the pause would silently undo the stop.
            self._skip_remaining(SkipReason.USER_STOP, "stop during restart")
            await self._finish_run(RunOutcome.ABORTED_USER, "user_stop")
            return

        if external:
            journal.state = ExecutorState.PAUSED_EXTERNAL
            await self._write()
            return

        if state in (ExecutorState.PAUSED_EXTERNAL, ExecutorState.PAUSED_SENSOR):
            await self._write()
            return

        if state is ExecutorState.WATERING:
            expected_end = journal.current_step_expected_end
            if expected_end is None:
                expected_end = now
            if zone_active:
                if expected_end <= now:
                    await self._complete_step(index, actual_end=expected_end)
                else:
                    self._arm_at(expected_end, self._on_expected_end)
                return
            if expected_end <= now:
                await self._complete_or_expire_step(index, expected_end, now)
                return
            # Zone idle but the commanded window is still open: trust the
            # commanded clock; the switch may simply be stale.
            self._arm_at(expected_end, self._on_expected_end)
            return

        current = journal.step_results.get(index)
        if (
            state is ExecutorState.RECONCILING
            and current is not None
            and current.status is StepStatus.RUNNING
            and current.actual_start_utc is not None
        ):
            # An end-context reconcile (overrun or early end): the step
            # already ran, so re-sending the start would re-water the
            # zone. Confirm against a fresh observation instead, mirroring
            # _confirm_overrun / _confirm_early_end.
            expected_end = journal.current_step_expected_end
            if expected_end is None:
                expected_end = now
            if observation is None:
                # No fresh read: restart the confirm loop rather than guess.
                if expected_end <= now:
                    await self._enter_overrun_reconcile()
                else:
                    await self._enter_early_end_reconcile()
                return
            if zone_active:
                if expected_end <= now:
                    # Still on past its end: rerun the overrun confirmation.
                    await self._enter_overrun_reconcile()
                else:
                    # The early "off" was stale: resume the commanded clock.
                    self._resume_watering_current_step()
                    await self._write()
                return
            if expected_end <= now:
                # Off after the end: the controller stopped on its own.
                await self._complete_or_expire_step(index, expected_end, now)
                return
            # Off before the end: classify the early stop and move on.
            await self._classify_early_end(index, observation)
            return

        if state in (
            ExecutorState.STARTING,
            ExecutorState.RECONCILING,
            ExecutorState.WAITING,
        ):
            command = journal.pending_command
            if zone_active:
                start = (
                    command.intended_at_utc if command is not None else now
                )
                if command is not None:
                    command.disposition = CommandDisposition.ACCEPTED
                journal.state = ExecutorState.WATERING
                journal.current_step_actual_start = start
                journal.current_step_expected_end = start + timedelta(
                    minutes=step.duration_minutes
                )
                journal.step_results[index].status = StepStatus.RUNNING
                journal.step_results[index].actual_start_utc = start
                await self._write()
                if journal.current_step_expected_end <= now:
                    await self._complete_step(
                        index, actual_end=journal.current_step_expected_end
                    )
                else:
                    self._arm_at(
                        journal.current_step_expected_end,
                        self._on_expected_end,
                    )
                return
            if now <= self._latest_permissible_start():
                journal.state = ExecutorState.WAITING
                await self._write()
                await self._start_step(index)
                return
            self._skip_remaining(
                SkipReason.RESTART_MISSED_TOLERANCE,
                "restart past missed-run tolerance",
            )
            await self._finish_run(
                RunOutcome.COMPLETED_WITH_SKIPS
                if self._any_completed()
                else RunOutcome.SKIPPED,
                SkipReason.RESTART_MISSED_TOLERANCE.value,
            )
            return

        if state is ExecutorState.INTER_ZONE_GAP:
            # Re-arm through the soak-aware helper so a restart mid-soak
            # does not fast-forward the remaining cycles.
            self._arm_gap_to_next_start()
            return

    # ------------------------------------------------------------------
    # Run completion
    # ------------------------------------------------------------------

    async def _finish_run(self, outcome: RunOutcome, reason: str | None) -> None:
        journal = self.journal
        plan = journal.run_plan
        self._cancel_pending_timer()
        self._reconcile_context = None
        if plan is not None:
            journal.record_occurrence_finished(plan.occurrence_id, self._now())
            # Persist the terminal step statuses before the journal resets
            # (§11.4: a step skip/interruption is an immediate write).
            await self._write()
            self._history.record_run_finished(journal, outcome, reason)
            event = {
                RunOutcome.COMPLETED: EVENT_RUN_COMPLETED,
                RunOutcome.COMPLETED_WITH_SKIPS: EVENT_RUN_COMPLETED,
                RunOutcome.SKIPPED: EVENT_RUN_SKIPPED,
                RunOutcome.FAILED: EVENT_RUN_FAILED,
            }.get(outcome, EVENT_RUN_INTERRUPTED)
            self._emit(
                event,
                {
                    "run_id": plan.run_id,
                    "program_id": plan.program_id,
                    "outcome": outcome.value,
                    "reason": reason,
                },
            )
        journal.state = ExecutorState.IDLE
        journal.run_plan = None
        journal.active_occurrence_id = None
        journal.pending_command = None
        journal.current_step_index = 0
        journal.step_results = {}
        journal.current_step_actual_start = None
        journal.current_step_expected_end = None
        journal.stop_requested = False
        journal.paused_reason = None
        await self._write()

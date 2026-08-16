"""Domain model for the Rain Bird Scheduler.

Everything in this module is Home Assistant independent so the recurrence
engine, planner, and tests can use it without a running Home Assistant.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from enum import StrEnum

from .const import (
    DEDUP_MAX_OCCURRENCES,
    DEFAULT_EARLY_END_TOLERANCE_SECONDS,
    DEFAULT_INTER_ZONE_GAP_SECONDS,
    DEFAULT_MISSED_RUN_TOLERANCE_MINUTES,
    DEFAULT_OVERRUN_CONFIRMATION_SECONDS,
    DEFAULT_START_OBSERVATION_TIMEOUT_SECONDS,
)

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class AuthorityMode(StrEnum):
    """Who owns automatic watering."""

    HA_AUTHORITATIVE = "ha_authoritative"
    NATIVE_AUTHORITATIVE = "native_authoritative"
    COEXISTENCE = "coexistence"


class ConflictPolicy(StrEnum):
    """What to do when external watering interrupts a scheduler run."""

    PAUSE = "pause"
    ABORT = "abort"


class SensorCutBehavior(StrEnum):
    """What to do after a rain-sensor cut ends a zone early."""

    ABORT_RUN = "abort_run"
    PAUSE_UNTIL_DRY = "pause_until_dry"
    DEFER_REMAINING = "defer_remaining"


class MinimumRuntimePolicy(StrEnum):
    """How to treat a positive runtime below the one-minute resolution."""

    SKIP_WITH_WARNING = "skip_with_warning"
    CLAMP_TO_ONE_MINUTE = "clamp_to_one_minute"
    CARRY_FORWARD = "carry_forward"


class ManualBudgetBehavior(StrEnum):
    """Whether native seasonal adjust applies to LNK manual runs."""

    UNKNOWN = "unknown"
    IGNORED = "ignored"
    APPLIED = "applied"


class MissedRunPolicy(StrEnum):
    """What to do with an occurrence that could not start on time."""

    RUN_LATE = "run_late"
    SKIP = "skip"


class InterruptionPolicy(StrEnum):
    """What a program wants after an external interruption."""

    ABORT = "abort"
    PAUSE = "pause"


class SoilType(StrEnum):
    """Soil type used for Cycle+Soak suggestions."""

    CLAY = "clay"
    LOAM = "loam"
    SAND = "sand"
    UNKNOWN = "unknown"


class SlopeClass(StrEnum):
    """Slope class used for Cycle+Soak suggestions."""

    FLAT = "flat"
    MODERATE = "moderate"
    STEEP = "steep"


class WindowPolicy(StrEnum):
    """How a watering window constrains compiled steps."""

    SKIP_STEP = "skip_step"
    TRUNCATE_LAST = "truncate_last"
    DEFER_OCCURRENCE = "defer_occurrence"
    REQUIRE_INTERVENTION = "require_intervention"


class RecurrenceKind(StrEnum):
    """Supported recurrence families."""

    WEEKLY = "weekly"
    ODD_DAYS = "odd_days"
    EVEN_DAYS = "even_days"
    INTERVAL = "interval"


class DstNonexistentPolicy(StrEnum):
    """Behavior for spring-forward nonexistent local start times."""

    SHIFT_FORWARD = "shift_forward"
    SKIP = "skip"


class ProviderKind(StrEnum):
    """Duration adjustment provider families."""

    FIXED = "fixed"
    MANUAL_PERCENT = "manual_percent"
    MONTHLY_CURVE = "monthly_curve"
    ENTITY_PERCENT = "entity_percent"
    ENTITY_RUNTIME = "entity_runtime"


class ExecutorState(StrEnum):
    """Executor state machine states."""

    IDLE = "idle"
    WAITING = "waiting"
    STARTING = "starting"
    WATERING = "watering"
    INTER_ZONE_GAP = "inter_zone_gap"
    PAUSED_EXTERNAL = "paused_external"
    PAUSED_SENSOR = "paused_sensor"
    STOPPING = "stopping"
    RECONCILING = "reconciling"
    FAILED = "failed"


class CommandType(StrEnum):
    """Controller command families."""

    START_ZONE = "start_zone"
    STOP_CONTROLLER = "stop_controller"


class CommandDisposition(StrEnum):
    """Journaled lifecycle of a controller command."""

    INTENDED = "intended"
    SENT = "sent"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNCERTAIN = "uncertain"


class ObservationFreshness(StrEnum):
    """How trustworthy an observation timestamp is."""

    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


class StepStatus(StrEnum):
    """Status of one compiled step during and after execution."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    SENSOR_CUT = "sensor_cut"
    EXTERNAL_STOP = "external_stop"
    OVERRUN_STOPPED = "overrun_stopped"
    FAILED = "failed"


class RunOutcome(StrEnum):
    """Final outcome of a run."""

    COMPLETED = "completed"
    COMPLETED_WITH_SKIPS = "completed_with_skips"
    ABORTED_SENSOR = "aborted_sensor"
    ABORTED_EXTERNAL = "aborted_external"
    ABORTED_USER = "aborted_user"
    ABORTED_POWER_LOSS = "aborted_power_loss"
    FAILED = "failed"
    SKIPPED = "skipped"


class SkipReason(StrEnum):
    """Structured reasons for skips, deferrals, and aborts."""

    RAIN_DELAY = "rain_delay"
    RAIN_SENSOR_WET = "rain_sensor_wet"
    BELOW_RESOLUTION = "below_resolution"
    OUT_OF_WINDOW = "out_of_window"
    MISSED_TOLERANCE = "missed_tolerance"
    RESTART_MISSED_TOLERANCE = "restart_missed_tolerance"
    DUPLICATE_OCCURRENCE = "duplicate_occurrence"
    CONTROLLER_BUSY = "controller_busy"
    SCHEDULER_DISABLED = "scheduler_disabled"
    AUTHORITY_MODE = "authority_mode"
    EXTERNAL_ACTIVITY = "external_activity"
    USER_STOP = "user_stop"
    ZONE_DISABLED = "zone_disabled"
    PROGRAM_DISABLED = "program_disabled"
    SOURCE_UNAVAILABLE = "source_unavailable"
    DST_NONEXISTENT = "dst_nonexistent"
    COMMAND_FAILED = "command_failed"


class EarlyEndClassification(StrEnum):
    """Classification of a zone that ended earlier than commanded."""

    SENSOR_CUT = "sensor_cut"
    EXTERNAL_STOP = "external_stop"
    CONTROLLER_POWER_LOSS = "controller_power_loss"


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EntityReference:
    """Durable reference to a Home Assistant entity."""

    entity_registry_id: str
    last_known_entity_id: str


@dataclass(frozen=True)
class ZoneReference:
    """Durable reference to a source Rain Bird zone switch."""

    source_unique_id: str
    source_config_entry_id: str
    entity_registry_id: str
    station_number: int
    last_known_entity_id: str


# ---------------------------------------------------------------------------
# Policies and configuration
# ---------------------------------------------------------------------------


@dataclass
class RainPolicy:
    """Rain handling for a program (or the controller default)."""

    honor_native_delay: bool = True
    skip_when_sensor_wet: bool = True
    sensor_cut_behavior: SensorCutBehavior = SensorCutBehavior.ABORT_RUN


@dataclass
class WateringWindow:
    """Permitted local start window; ``end`` <= ``start`` spans midnight."""

    start_local: time
    end_local: time
    policy: WindowPolicy = WindowPolicy.SKIP_STEP


@dataclass
class RecurrenceRule:
    """Recurrence stored in local wall-clock terms."""

    kind: RecurrenceKind = RecurrenceKind.WEEKLY
    weekdays: frozenset[int] = frozenset()  # 0=Monday .. 6=Sunday
    interval_days: int | None = None
    anchor_date: date | None = None
    start_date: date | None = None
    end_date: date | None = None
    months: frozenset[int] | None = None  # 1..12; None = all months
    dst_nonexistent_policy: DstNonexistentPolicy = DstNonexistentPolicy.SHIFT_FORWARD


@dataclass
class AdjustmentProviderConfig:
    """Configuration for one duration provider."""

    kind: ProviderKind = ProviderKind.FIXED
    percent: Decimal = Decimal(100)
    monthly_percents: tuple[Decimal, ...] | None = None  # 12 values, Jan..Dec
    entity_id: str | None = None


@dataclass
class ControllerConfig:
    """Controller-level scheduler configuration."""

    id: str
    revision: int
    source_config_entry_id: str
    source_unique_id: str | None
    enabled: bool = True
    authority_mode: AuthorityMode = AuthorityMode.HA_AUTHORITATIVE
    inter_zone_gap_seconds: int = DEFAULT_INTER_ZONE_GAP_SECONDS
    start_observation_timeout_seconds: int = (
        DEFAULT_START_OBSERVATION_TIMEOUT_SECONDS
    )
    early_end_tolerance_seconds: int = DEFAULT_EARLY_END_TOLERANCE_SECONDS
    overrun_confirmation_seconds: int = DEFAULT_OVERRUN_CONFIRMATION_SECONDS
    missed_run_tolerance_minutes: int = DEFAULT_MISSED_RUN_TOLERANCE_MINUTES
    external_conflict_policy: ConflictPolicy = ConflictPolicy.PAUSE
    default_rain_policy: RainPolicy = field(default_factory=RainPolicy)
    minimum_runtime_policy: MinimumRuntimePolicy = (
        MinimumRuntimePolicy.SKIP_WITH_WARNING
    )
    rain_sensor_reference: EntityReference | None = None
    rain_delay_reference: EntityReference | None = None
    native_calendar_reference: EntityReference | None = None
    manual_run_budget_behavior: ManualBudgetBehavior = ManualBudgetBehavior.UNKNOWN


@dataclass
class ZoneProfile:
    """Per-zone irrigation profile."""

    id: str
    revision: int
    reference: ZoneReference
    display_name: str
    enabled: bool = True
    base_runtime_minutes: Decimal = Decimal(10)
    precipitation_rate_mm_per_hour: Decimal | None = None
    flow_rate_lpm: Decimal | None = None
    irrigated_area_m2: Decimal | None = None
    irrigation_efficiency: Decimal = Decimal("0.75")
    soil_type: SoilType = SoilType.UNKNOWN
    slope_class: SlopeClass = SlopeClass.FLAT
    root_depth_mm: Decimal | None = None
    max_cycle_minutes: int | None = None
    minimum_soak_minutes: int | None = None
    minimum_runtime_policy: MinimumRuntimePolicy | None = None


@dataclass
class ProgramZoneStep:
    """One zone's membership in a program."""

    zone_id: str
    position: int
    enabled: bool = True
    requested_offset_seconds: int = 0
    base_runtime_override_minutes: Decimal | None = None
    max_cycle_minutes_override: int | None = None
    minimum_soak_minutes_override: int | None = None


@dataclass
class Program:
    """A user-authored irrigation program."""

    id: str
    revision: int
    name: str
    enabled: bool = True
    priority: int = 100
    recurrence: RecurrenceRule = field(default_factory=RecurrenceRule)
    nominal_start_times: list[time] = field(default_factory=list)
    zone_steps: list[ProgramZoneStep] = field(default_factory=list)
    adjustment_provider: AdjustmentProviderConfig = field(
        default_factory=AdjustmentProviderConfig
    )
    rain_policy: RainPolicy = field(default_factory=RainPolicy)
    missed_run_policy: MissedRunPolicy = MissedRunPolicy.RUN_LATE
    external_interruption_policy: InterruptionPolicy = InterruptionPolicy.PAUSE
    watering_window: WateringWindow | None = None


# ---------------------------------------------------------------------------
# Occurrences, adjustments, and compiled plans
# ---------------------------------------------------------------------------


def make_occurrence_id(program_id: str, scheduled_start_utc: datetime) -> str:
    """Build the restart-stable deduplication key for one occurrence."""
    return f"{program_id}:{scheduled_start_utc.isoformat()}"


@dataclass(frozen=True)
class ProgramOccurrence:
    """One scheduled instance of a program."""

    occurrence_id: str
    program_id: str
    scheduled_start_utc: datetime
    scheduled_start_local: datetime
    created_at_utc: datetime
    manual: bool = False


@dataclass(frozen=True)
class AdjustmentResult:
    """Provenance-complete adjusted runtime for one zone."""

    base_runtime_minutes: Decimal
    exact_adjusted_minutes: Decimal
    quantized_minutes: int
    seasonal_factor: Decimal
    weather_factor: Decimal | None
    rain_credit_minutes: Decimal
    carried_deficit_minutes: Decimal
    input_timestamps: dict[str, datetime]
    stale_inputs: tuple[str, ...]
    explanation: tuple[str, ...]


@dataclass(frozen=True)
class AdjustmentSnapshot:
    """All zone adjustments captured when a run plan was compiled."""

    provider_kind: ProviderKind
    computed_at_utc: datetime
    per_zone: dict[str, AdjustmentResult]


@dataclass(frozen=True)
class RunStep:
    """One compiled controller command (a single zone cycle)."""

    index: int
    zone_id: str
    zone_name: str
    station_number: int
    cycle_index: int  # 1-based
    cycle_count: int
    requested_start_utc: datetime
    planned_start_utc: datetime
    planned_end_utc: datetime
    duration_minutes: int
    exact_minutes: Decimal
    soak_before: bool = False


@dataclass(frozen=True)
class SkippedZone:
    """A zone the planner excluded, with its structured reason."""

    zone_id: str
    zone_name: str
    reason: SkipReason
    detail: str


@dataclass(frozen=True)
class RunPlan:
    """Immutable execution plan for one occurrence."""

    run_id: str
    occurrence_id: str
    program_id: str
    program_name: str
    requested_start_utc: datetime
    compiled_at_utc: datetime
    controller_config_revision: int
    program_revision: int
    adjustment_snapshot: AdjustmentSnapshot
    steps: tuple[RunStep, ...]
    skipped_zones: tuple[SkippedZone, ...] = ()
    manual: bool = False

    @property
    def planned_end_utc(self) -> datetime | None:
        """Return the planned end of the final step."""
        if not self.steps:
            return None
        return self.steps[-1].planned_end_utc


def new_run_id() -> str:
    """Return a fresh run id."""
    return uuid.uuid4().hex


@dataclass(frozen=True)
class PlannerConflict:
    """A conflict the planner could not resolve automatically."""

    occurrence_id: str
    program_id: str
    zone_id: str | None
    reason: SkipReason
    message: str


@dataclass(frozen=True)
class PlannerWarning:
    """A non-blocking planner observation."""

    occurrence_id: str | None
    program_id: str | None
    zone_id: str | None
    message: str


@dataclass(frozen=True)
class CompiledControllerTimeline:
    """Deterministic output of one planner invocation."""

    runs: tuple[RunPlan, ...]
    conflicts: tuple[PlannerConflict, ...]
    warnings: tuple[PlannerWarning, ...]


# ---------------------------------------------------------------------------
# Execution journal
# ---------------------------------------------------------------------------


@dataclass
class PendingCommand:
    """Journaled intent for one controller command."""

    command_id: str
    command_type: CommandType
    zone_id: str | None
    duration_minutes: int | None
    intended_at_utc: datetime
    attempt_number: int
    disposition: CommandDisposition

    @classmethod
    def for_step(
        cls, step: RunStep, now: datetime, attempt_number: int = 1
    ) -> PendingCommand:
        """Create the journaled intent for starting one step."""
        return cls(
            command_id=uuid.uuid4().hex,
            command_type=CommandType.START_ZONE,
            zone_id=step.zone_id,
            duration_minutes=step.duration_minutes,
            intended_at_utc=now,
            attempt_number=attempt_number,
            disposition=CommandDisposition.INTENDED,
        )

    @classmethod
    def for_stop(cls, now: datetime) -> PendingCommand:
        """Create the journaled intent for a controller-wide stop."""
        return cls(
            command_id=uuid.uuid4().hex,
            command_type=CommandType.STOP_CONTROLLER,
            zone_id=None,
            duration_minutes=None,
            intended_at_utc=now,
            attempt_number=1,
            disposition=CommandDisposition.INTENDED,
        )


@dataclass
class ControllerObservation:
    """A snapshot of what the source entities currently report."""

    observed_at_utc: datetime
    active_zone_ids: frozenset[str]
    rain_sensor_active: bool | None
    rain_delay_days: int | None
    source_available: bool
    freshness: ObservationFreshness


@dataclass
class StepResult:
    """Mutable execution record for one compiled step."""

    status: StepStatus = StepStatus.PENDING
    actual_start_utc: datetime | None = None
    actual_end_utc: datetime | None = None
    reason: str | None = None
    attempts: int = 0


@dataclass
class ExecutionJournal:
    """The minimal state needed to recover safely after a restart."""

    state: ExecutorState = ExecutorState.IDLE
    active_occurrence_id: str | None = None
    run_plan: RunPlan | None = None
    current_step_index: int = 0
    step_results: dict[int, StepResult] = field(default_factory=dict)
    pending_command: PendingCommand | None = None
    current_step_actual_start: datetime | None = None
    current_step_expected_end: datetime | None = None
    last_observation: ControllerObservation | None = None
    retry_count: int = 0
    stop_requested: bool = False
    completed_occurrences: dict[str, datetime] = field(default_factory=dict)
    updated_at_utc: datetime | None = None

    def record_occurrence_finished(
        self, occurrence_id: str, finished_at: datetime
    ) -> None:
        """Record an occurrence in the bounded deduplication window."""
        self.completed_occurrences[occurrence_id] = finished_at
        if len(self.completed_occurrences) > DEDUP_MAX_OCCURRENCES:
            oldest = sorted(
                self.completed_occurrences.items(), key=lambda item: item[1]
            )
            for occurrence, _ in oldest[
                : len(self.completed_occurrences) - DEDUP_MAX_OCCURRENCES
            ]:
                del self.completed_occurrences[occurrence]


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


@dataclass
class ZoneRecord:
    """History record for one executed (or skipped) step."""

    run_id: str
    step_index: int
    zone_id: str
    zone_name: str
    cycle_index: int
    cycle_count: int
    planned_start_utc: datetime
    planned_end_utc: datetime
    actual_start_utc: datetime | None
    actual_end_utc: datetime | None
    commanded_minutes: int
    exact_minutes: Decimal
    status: StepStatus
    reason: str | None


@dataclass
class RunRecord:
    """History record for one run."""

    run_id: str
    occurrence_id: str
    program_id: str
    program_name: str
    manual: bool
    requested_start_utc: datetime
    planned_start_utc: datetime | None
    planned_end_utc: datetime | None
    actual_start_utc: datetime | None
    actual_end_utc: datetime | None
    outcome: RunOutcome
    reason: str | None
    provider_kind: ProviderKind
    retries: int
    uncertain_commands: int


@dataclass
class InterventionRecord:
    """History record for a failure, repair, or manual intervention."""

    recorded_at_utc: datetime
    kind: str
    message: str
    run_id: str | None = None
    zone_id: str | None = None


@dataclass
class HistoryData:
    """Bounded history rings; the authoritative run database."""

    runs: list[RunRecord] = field(default_factory=list)
    zone_records: list[ZoneRecord] = field(default_factory=list)
    interventions: list[InterventionRecord] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Top-level configuration payload
# ---------------------------------------------------------------------------


@dataclass
class ConfigData:
    """Everything in the configuration store."""

    controller: ControllerConfig
    zones: dict[str, ZoneProfile] = field(default_factory=dict)
    programs: dict[str, Program] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Driver surface
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DriverCapabilities:
    """What an irrigation driver can do."""

    stack_manual_runs: bool = False
    read_current_queue: bool = False


@dataclass(frozen=True)
class CommandResult:
    """Result of one driver command."""

    ok: bool
    error: str | None = None

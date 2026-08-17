# Revised implementation plan: Rain Bird Scheduler for Home Assistant

Kimi’s seven substantive corrections are valid. The revised design adopts all of them, with one qualification: an event entity should expose run lifecycle events for automations and UI discovery, but it should not be treated as the authoritative history database. The bounded history store remains authoritative.

I would alter one minor recommendation. Instead of using only the source Rain Bird config-entry ID as the scheduler’s unique ID, use the Rain Bird entry’s own `unique_id`—currently the controller MAC address—when available, with the config-entry ID as a fallback. That preserves the scheduler identity if the Rain Bird entry is recreated. The scheduler still stores the current source config-entry ID for runtime lookup. The core integration explicitly migrates Rain Bird entries to a MAC-based unique ID.

## 1. Final architecture

Build `rainbird_scheduler` as a companion helper integration.

The core `rainbird` integration remains responsible for:

* Controller authentication and discovery.
* Local LNK communication.
* Request serialization.
* Zone switches and state.
* Rain-sensor state.
* Rain delay.
* Reading the native schedule.
* Starting a single zone.
* Stopping irrigation.

The scheduler integration owns:

* Programs and recurrence.
* Automatic serialization of overlapping requested starts.
* Runtime adjustment.
* Soil profiles and Cycle+Soak.
* Execution state and restart recovery.
* Conflict detection.
* Run history.
* The program editor and timeline UI.

The scheduler must not:

* Import or use `entry.runtime_data.controller`.
* Open a second `pyrainbird` connection.
* Store the Rain Bird password.
* Write native Rain Bird schedules in the first release.
* Treat a zone switch’s off action as a zone-specific stop.

This boundary matters because the current integration limits Rain Bird communication to one connection/request at a time, and the official documentation warns that simultaneous app and Home Assistant use may cause command failures. The current integration polls valve state every minute and the schedule every 15 minutes. ([Home Assistant][1])

## 2. Target user behavior

A program should look like this:

```text
Program: Morning Lawn
Days: Monday, Wednesday, Saturday
Requested start: 9:00 AM
Front lawn       12 min
Side lawn         8 min
Back lawn        15 min
```

All three zones may be assigned a requested start of 9:00 AM. The compiler produces:

```text
Front lawn     9:00:00–9:12:00
Side lawn      9:12:05–9:20:05
Back lawn      9:20:10–9:35:10
```

The five-second gaps are derived controller constraints, not artificial start times that the user must maintain.

The user interface must always distinguish:

* Requested start.
* Planned actual start.
* Actual observed start.
* Base runtime.
* Adjusted exact runtime.
* Controller-quantized runtime.
* Reason for any delay, skip, or adjustment.

That is the central product feature: the user describes irrigation intent, while the scheduler quietly produces a valid one-zone-at-a-time execution plan.

## 3. Execution backends

Define a driver interface at the start, even though only one driver ships enabled initially.

```python
class IrrigationDriver(Protocol):
    @property
    def capabilities(self) -> DriverCapabilities:
        ...

    async def async_start_zone(
        self,
        zone: ZoneReference,
        duration_minutes: int,
        command_id: str,
    ) -> CommandResult:
        ...

    async def async_stop_controller(self) -> CommandResult:
        ...

    async def async_observe(self) -> ControllerObservation:
        ...

    async def async_request_observation(self) -> None:
        ...
```

### 3.1 Home Assistant entity driver

This is the production backend for version 1.

It calls:

* `rainbird.start_irrigation` for a single zone.
* `switch.turn_off` on a Rain Bird zone to stop the controller.
* Existing entity states for observation.
* `number.set_value` when explicitly setting native rain delay.

It never communicates directly with the controller.

### 3.2 Native queue driver

This exists as an interface and disabled implementation in version 1. It becomes available only when Home Assistant and `pyrainbird` expose a stable native queue action.

The known local protocol contains commands for:

* Reading the queue: `CurrentQueueRequest`, `0x3B`.
* Stacking a station: `StackManuallyRunStationRequest`, `0x4B`.
* Writing schedule pages: `0x21`.
* Setting water-budget values: `0x31`.
* Setting per-zone adjustments: `0x33`.

However, current `pyrainbird` exposes single-zone irrigation and schedule reading but not stable public methods for stacked runs or schedule writing. Its own unimplemented-features document identifies stacked manual runs and queue decoding as unfinished work.

The scheduler must therefore be complete without the native backend.

## 4. Home Assistant manifest and installation metadata

Use:

```json
{
  "domain": "rainbird_scheduler",
  "name": "Rain Bird Scheduler",
  "version": "0.1.0",
  "config_flow": true,
  "integration_type": "helper",
  "iot_class": "calculated",
  "dependencies": [
    "rainbird",
    "http",
    "frontend"
  ],
  "requirements": [],
  "codeowners": [
    "@your-github-name"
  ],
  "documentation": "https://github.com/your-org/rainbird-scheduler",
  "issue_tracker": "https://github.com/your-org/rainbird-scheduler/issues"
}
```

Do not include:

```json
"after_dependencies": ["frontend"]
```

`frontend` is an actual dependency because the integration registers a panel. `http` is needed to serve the bundled frontend. `websocket_api` does not have to be declared merely because the integration registers custom commands. Home Assistant explicitly permits components to register WebSocket commands without adding it as a manifest dependency. ([Home Assistant][2])

The HACS metadata should declare the minimum supported Home Assistant version:

```json
{
  "name": "Rain Bird Scheduler",
  "homeassistant": "2026.8.0",
  "render_readme": true
}
```

HACS supports the `homeassistant` key as a minimum-version declaration. The 2026.8 floor is appropriate because the helper-device relationship uses the device-linking behavior enforced in that release. ([Home Assistant][3])

## 5. Config flow and controller identity

The user flow should:

1. Enumerate loaded or configured Rain Bird config entries.
2. Exclude Rain Bird entries already attached to a scheduler.
3. Ask the user to choose one controller.
4. Set a unique ID.
5. Create one scheduler config entry for that controller.
6. Initialize the scheduler store with safe defaults.

```python
source_entry = selected_rainbird_entry
source_identity = source_entry.unique_id or source_entry.entry_id

await self.async_set_unique_id(f"rainbird:{source_identity}")
self._abort_if_unique_id_configured()
```

Home Assistant recommends setting a stable unique ID and aborting duplicate setup rather than allowing colliding config entries. ([Home Assistant][4])

Store both:

```python
{
    "source_config_entry_id": source_entry.entry_id,
    "source_unique_id": source_entry.unique_id
}
```

If the source Rain Bird entry disappears:

* Mark the scheduler unavailable.
* Cancel pending callbacks.
* Preserve programs and history.
* Create a repair issue.
* Offer a reconfigure flow that binds the scheduler to a compatible replacement Rain Bird entry.
* Require the replacement entry’s unique ID to match unless the user explicitly chooses a migration path.

The initial config flow should remain small. It should ask only for:

* Source Rain Bird controller.
* Initial authority mode.
* Confirmation that native and Home Assistant schedules can conflict.

Program creation and controller-level policy editing belong in the scheduler panel.

## 6. Schedule-authority modes

### 6.1 Home Assistant authoritative

This is the default and recommended mode.

Home Assistant owns automatic watering. Native Rain Bird automatic programs should be disabled or set to zero runtimes. Rain Bird supports retaining program configuration while suppressing watering through zero runtimes, depending on controller family.

The Rain Bird app remains useful for:

* Initial provisioning.
* Wi-Fi and controller settings.
* Station names and photos.
* Rain-sensor activation or bypass.
* Manual diagnostics.
* Firmware information.
* Emergency manual watering.

Changing to Home Assistant authoritative mode must not automatically rewrite native schedules in version 1. The integration should instead present a checklist and native-calendar conflict warning.

### 6.2 Native authoritative

The native Rain Bird program owns automatic watering.

The scheduler may:

* Display the native calendar.
* Offer manual custom runs.
* Show rain delay and rain-sensor state.
* Calculate suggested runtimes.

It must not automatically launch scheduler programs.

### 6.3 Coexistence

This is an advanced mode.

Before a scheduler run, the integration checks:

* Current active-zone states.
* Native Rain Bird calendar.
* Rain delay.
* External manual activity.
* Whether a scheduler run is already active.

Because the native calendar is refreshed only every 15 minutes, it must be treated as advisory rather than an authoritative real-time lock. ([Home Assistant][1])

### 6.4 Authority-mode changes

Add both:

```text
rainbird_scheduler/config/get
rainbird_scheduler/config/update
```

`config/update` requires:

* Administrator access.
* Current config revision.
* New values.
* `acknowledge_authority_change: true` when changing authority mode.

Changing authority mode should perform these actions:

* **To native authoritative:** cancel pending HA automatic occurrences after preserving active-run state.
* **To HA authoritative:** do not edit native schedules; warn until native events are absent or acknowledged.
* **To coexistence:** require explicit acceptance of stale-calendar and app-conflict limitations.

## 7. Exact integration points with core Rain Bird

| Requirement         | Core integration surface      | Scheduler behavior                                                     |
| ------------------- | ----------------------------- | ---------------------------------------------------------------------- |
| Find controllers    | Rain Bird config entries      | One scheduler entry per source controller                              |
| Find zones          | Entity registry               | Match platform `rainbird`, domain `switch`, and source config-entry ID |
| Station number      | Zone switch state attribute   | Read `zone`; persist station number and source unique ID               |
| Start zone          | `rainbird.start_irrigation`   | Always target exactly one entity                                       |
| Stop watering       | `switch.turn_off`             | Label as **Stop controller**                                           |
| Observe active zone | Rain Bird switch states       | Use observation timestamp and freshness                                |
| Detect rain         | Rain Bird binary sensor       | Apply program policy and early-stop classification                     |
| Read/set delay      | Rain Bird number entity       | Treat value explicitly as days                                         |
| Native schedule     | Rain Bird calendar            | Read-only conflict and preview input                                   |
| Device association  | Source entity/device registry | Link helper entities with `self.device_entry`                          |
| Entity rename       | Entity registry update        | Resolve current entity ID at execution time                            |

The public Rain Bird action accepts durations from 1 to 1440 minutes. The core switch implementation converts duration to `int`, and turning off a Rain Bird zone switch calls `stop_irrigation()` for the whole controller. Therefore, the scheduler must quantize explicitly and must not expose a misleading per-zone stop control. ([Home Assistant][5])

Always target one entity:

```python
await hass.services.async_call(
    "rainbird",
    "start_irrigation",
    {
        "entity_id": zone_entity_id,
        "duration": duration_minutes,
    },
    blocking=True,
)
```

Do not target a device, area, floor, or label, even though the public action supports broad targets. A broad target could resolve to multiple Rain Bird switches and generate exactly the simultaneous-command condition the scheduler is intended to prevent. ([Home Assistant][5])

Stop the controller with:

```python
await hass.services.async_call(
    "switch",
    "turn_off",
    {"entity_id": known_rainbird_zone_entity_id},
    blocking=True,
)
```

The user-facing label everywhere must be **Stop controller**. “Interrupt current zone” should also state that it stops all controller watering.

## 8. Source entity references

Do not store only mutable entity IDs such as:

```text
switch.front_lawn
```

Persist:

```python
@dataclass(frozen=True)
class ZoneReference:
    source_unique_id: str
    source_config_entry_id: str
    entity_registry_id: str
    station_number: int
    last_known_entity_id: str
```

At execution time:

1. Resolve `entity_registry_id`.
2. Obtain the current entity ID.
3. Confirm that its platform remains `rainbird`.
4. Confirm it belongs to the expected source config entry.
5. Confirm its `zone` attribute still matches the stored station number.
6. Refuse execution and create a repair issue if those checks fail.

Subscribe to entity-registry updates so renames and removals can be reflected promptly.

## 9. Helper-device linking

Helper entities should link to the source Rain Bird device by assigning `device_entry`; they must not add the scheduler config entry to the Rain Bird device.

```python
from homeassistant.helpers.device import async_entity_id_to_device


class RainBirdSchedulerEntity(Entity):
    def __init__(
        self,
        hass: HomeAssistant,
        source_entity_id: str,
    ) -> None:
        self.device_entry = async_entity_id_to_device(
            hass,
            source_entity_id,
        )
```

Home Assistant’s revised helper guidance requires this relationship, and adding the helper config entry to another integration’s device stops being supported in Core 2026.8. ([Home Assistant][3])

Controller-level scheduler entities should link to the Rain Bird controller device, using the rain-delay, rain-sensor, or calendar entity when possible. Zone-specific helper entities, if later added, should link to the corresponding Rain Bird zone device.

## 10. Domain model

### 10.1 Controller configuration

```python
@dataclass
class ControllerConfig:
    id: str
    revision: int
    source_config_entry_id: str
    source_unique_id: str | None
    enabled: bool
    authority_mode: AuthorityMode
    inter_zone_gap_seconds: int
    start_observation_timeout_seconds: int
    early_end_tolerance_seconds: int
    overrun_confirmation_seconds: int
    missed_run_tolerance_minutes: int
    external_conflict_policy: ConflictPolicy
    default_rain_policy: RainPolicy
    default_freeze_policy: FreezePolicy
    freeze_guard: FreezeGuardConfig
    minimum_runtime_policy: MinimumRuntimePolicy
    rain_sensor_reference: EntityReference | None
    rain_sensor_override_entity_id: str | None
    rain_delay_reference: EntityReference | None
    native_calendar_reference: EntityReference | None
    manual_run_budget_behavior: ManualBudgetBehavior
```

`FreezeGuardConfig` holds the controller-level freeze guard: `enabled`, a
`temperature_entity_id` (a `sensor.*` or `weather.*` source, since the Rain
Bird LNK module exposes only a rain/freeze boolean and no temperature), a
`threshold` with its `TemperatureUnit`, and a `when_unavailable` policy
(`ALLOW_WATERING` default, or `BLOCK_WATERING`). `FreezePolicy` is the
per-program half (`skip_when_freezing`, `freeze_cut_behavior`), mirroring
`RainPolicy`. `rain_sensor_override_entity_id` lets a user point rain skips at
a different binary sensor; discovery only ever writes `rain_sensor_reference`,
so the override survives registry re-scans.

The renamed timing fields avoid the earlier ambiguous use of “grace”:

* `start_observation_timeout_seconds`: how long to wait for evidence that a start command was accepted.
* `early_end_tolerance_seconds`: how early a zone may appear off without classifying an interruption.
* `overrun_confirmation_seconds`: how long fresh contradictory evidence must persist before escalation.
* `inter_zone_gap_seconds`: deliberate hydraulic/command gap.

### 10.2 Zone profile

```python
@dataclass
class ZoneProfile:
    id: str
    revision: int
    reference: ZoneReference
    display_name: str
    enabled: bool
    base_runtime_minutes: Decimal
    precipitation_rate_mm_per_hour: Decimal | None
    flow_rate_lpm: Decimal | None
    irrigated_area_m2: Decimal | None
    irrigation_efficiency: Decimal
    soil_type: SoilType
    slope_class: SlopeClass
    root_depth_mm: Decimal | None
    max_cycle_minutes: int | None
    minimum_soak_minutes: int | None
    minimum_runtime_policy: MinimumRuntimePolicy | None
```

### 10.3 Program

```python
@dataclass
class Program:
    id: str
    revision: int
    name: str
    enabled: bool
    priority: int
    recurrence: RecurrenceRule
    nominal_start_times: list[LocalStartTime]
    zone_steps: list[ProgramZoneStep]
    adjustment_provider: AdjustmentProviderConfig
    rain_policy: RainPolicy
    missed_run_policy: MissedRunPolicy
    external_interruption_policy: InterruptionPolicy
    watering_window: WateringWindow | None
```

### 10.4 Program zone step

```python
@dataclass
class ProgramZoneStep:
    zone_id: str
    position: int
    enabled: bool
    requested_offset_seconds: int
    base_runtime_override_minutes: Decimal | None
    max_cycle_minutes_override: int | None
    minimum_soak_minutes_override: int | None
```

Most users will leave `requested_offset_seconds` at zero. Ordering then determines which zone receives the shared requested start first.

### 10.5 Immutable run plan

```python
@dataclass(frozen=True)
class RunPlan:
    run_id: str
    occurrence_id: str
    program_id: str
    requested_start_utc: datetime
    compiled_at_utc: datetime
    controller_config_revision: int
    program_revision: int
    adjustment_snapshot: AdjustmentSnapshot
    steps: tuple[RunStep, ...]
```

Once a run starts, editing the program does not mutate the active run plan.

## 11. Persistent storage

Use three versioned `Store` objects.

### 11.1 Configuration store

Contains:

* Controller configuration.
* Zone profiles.
* Programs.
* Provider configuration.
* Revisions.

Writes are infrequent and immediate.

### 11.2 Execution journal

Contains only the information needed to recover safely:

* Claimed occurrence ID.
* Active run plan.
* Executor state.
* Current step.
* Pending command intent.
* Expected end time.
* Last confirmed observation.
* Retry count.
* Stop request.
* Previous completed occurrence IDs within a bounded deduplication window.

This store should be small and use:

```python
Store(
    hass,
    version=1,
    key=f"rainbird_scheduler.{entry_id}.journal",
    atomic_writes=True,
)
```

### 11.3 History store

Contains a bounded ring buffer, for example the most recent:

* 250 runs.
* 2,000 zone/cycle records.
* 500 failures and interventions.

This is the authoritative detailed history.

### 11.4 Correct save API

There is no `Store.async_save_critical_state()` API. Current Home Assistant exposes `async_save()` and `async_delay_save()`. `async_save()` performs the immediate save path, while delayed saves register final-write handling for shutdown.

Define an integration-owned wrapper:

```python
class SchedulerStorage:
    def __init__(self, journal_store: Store[dict[str, Any]]) -> None:
        self._journal_store = journal_store
        self._journal_lock = asyncio.Lock()

    async def async_write_journal_now(
        self,
        journal: ExecutionJournal,
    ) -> None:
        snapshot = journal.to_json()
        async with self._journal_lock:
            await self._journal_store.async_save(snapshot)
```

Immediate journal writes occur:

1. When an occurrence is claimed.
2. Before a controller command is sent.
3. When command acceptance is observed or inferred.
4. When a step is completed, skipped, or interrupted.
5. When the executor changes run state.
6. Before and after a controller-wide stop.
7. When a run completes or fails.

Use `async_delay_save()` only for noncritical data such as intermediate history details or UI preferences.

On config-entry unload or Home Assistant shutdown:

* Cancel all scheduled callbacks.
* Prevent new commands.
* Snapshot the current journal.
* Call `async_save()` on the journal.
* Flush pending configuration/history snapshots.
* Then unload entities and the panel subscription.

`Store` already participates in Home Assistant’s final-write lifecycle, but explicit integration unload is still needed because a config-entry reload is not necessarily a complete Home Assistant shutdown.

## 12. Command-intent journaling

To handle a crash or timeout around a command:

```python
@dataclass
class PendingCommand:
    command_id: str
    command_type: CommandType
    zone_id: str | None
    duration_minutes: int | None
    intended_at_utc: datetime
    attempt_number: int
    disposition: CommandDisposition
```

The sequence is:

1. Create a unique `command_id`.
2. Persist `PendingCommand(disposition=INTENDED)`.
3. Call the Rain Bird action.
4. Persist `SENT`.
5. Observe controller state.
6. Persist `ACCEPTED`, `REJECTED`, or `UNCERTAIN`.

If Home Assistant crashes:

* **Before the action:** no zone is active; the command may be retried.
* **After the controller accepted but before HA saved success:** restart observation sees the intended zone active and treats it as accepted.
* **After the zone completed:** restart compares expected end and observed idle state before deciding whether to continue.

This does not produce mathematically perfect exactly-once delivery—the LNK protocol does not supply that—but it prevents blind duplicate starts.

## 13. Recurrence engine

Implement recurrence without APScheduler.

Supported recurrence:

* Selected weekdays.
* Odd calendar days.
* Even calendar days.
* Every N days from an anchor date.
* Multiple start times per qualifying day.
* Optional start/end dates.
* Optional permitted months.
* Optional watering window.

Store recurrence in local wall-clock time. Convert each occurrence to UTC when compiling it.

### Daylight-saving behavior

Make the rule explicit:

* Spring-forward nonexistent time: move to the next valid local instant by default.
* Optional policy: skip a nonexistent time.
* Fall-back repeated time: run the first wall-clock instance only, equivalent to `fold=0`.
* Occurrence ID uses the selected UTC instant.
* Never run both fall-back instances unless a later explicit policy is added.

```text
occurrence_id = <program UUID>:<scheduled UTC ISO timestamp>
```

The occurrence ID is the deduplication key across restarts.

## 14. Pure planner

The planner package must not import Home Assistant.

Input:

```python
PlannerInput(
    controller_config=...,
    programs=...,
    zone_profiles=...,
    candidate_occurrences=...,
    adjustment_snapshots=...,
)
```

Output:

```python
CompiledControllerTimeline(
    runs=...,
    conflicts=...,
    warnings=...,
)
```

The controller is modeled as a resource with capacity one.

For basic sequential steps:

```python
cursor = occurrence_start

for step in ordered_steps:
    requested_start = occurrence_start + step.requested_offset
    actual_start = max(
        requested_start,
        cursor,
    )
    actual_end = actual_start + step.quantized_duration
    emit(step, actual_start, actual_end)
    cursor = actual_end + inter_zone_gap
```

When multiple programs overlap, merge all candidate steps into a controller-wide queue ordered by:

1. Requested start.
2. Program priority.
3. Occurrence creation timestamp.
4. Zone position.
5. Stable zone ID.

This creates deterministic plans.

Supported overlap policies:

* Delay later work and preserve duration.
* Skip work that cannot begin within its permitted window.
* Truncate the last step at the window end.
* Defer the whole occurrence.
* Mark the conflict and require intervention.

The default should be delay-and-preserve.

## 15. Runtime quantization

This is an architectural rule, not a UI detail.

The Rain Bird public action accepts integer minutes, and the core implementation itself calls `int()`. The scheduler must not allow incidental truncation to decide watering time. ([Home Assistant][5])

### 15.1 Quantize once per zone

Use `Decimal` and round-half-up:

```python
from decimal import Decimal, ROUND_HALF_UP


def quantize_zone_minutes(exact_minutes: Decimal) -> int:
    return int(
        exact_minutes.quantize(
            Decimal("1"),
            rounding=ROUND_HALF_UP,
        )
    )
```

Processing order:

1. Calculate the exact adjusted zone total.
2. Apply any rain credit or water-balance deficit.
3. Clamp negative values to zero.
4. Quantize the zone total once.
5. Split the resulting integer total into cycles.
6. Never quantize each cycle independently.

### 15.2 Positive duration that rounds to zero

A positive exact runtime below 0.5 minute must not silently disappear.

Support three explicit policies:

```python
class MinimumRuntimePolicy(StrEnum):
    SKIP_WITH_WARNING = "skip_with_warning"
    CLAMP_TO_ONE_MINUTE = "clamp_to_one_minute"
    CARRY_FORWARD = "carry_forward"
```

Version 1 default:

```text
SKIP_WITH_WARNING
```

The plan records:

```text
Exact runtime:        0.4 min
Controller runtime:   0 min
Outcome:              Skipped
Reason:               Below controller's one-minute resolution
```

`CARRY_FORWARD` is appropriate only for a provider that maintains a water-deficit account. It should not be simulated by silently modifying future fixed programs.

### 15.3 Cycle allocation

Suppose:

```text
Quantized total: 11 min
Maximum cycle:    4 min
```

Calculate:

```python
cycle_count = math.ceil(total_minutes / max_cycle_minutes)
base, remainder = divmod(total_minutes, cycle_count)
cycles = [
    base + 1 if index < remainder else base
    for index in range(cycle_count)
]
```

Result:

```text
4, 4, 3
```

The invariant is:

```text
sum(cycle durations) == quantized zone total
```

It is not:

```text
sum(cycle durations) == pre-quantized exact runtime
```

## 16. Cycle+Soak planner

Rain Bird Cycle+Soak uses a maximum cycle runtime and minimum soak time. During the soak period, other zones can run, after which the controller returns to the earlier zone. ([Rain Bird Connected Device Support][6])

Represent each zone as:

```python
@dataclass
class CycleWorkItem:
    zone_id: str
    remaining_minutes: int
    cycle_durations: deque[int]
    next_eligible_at: datetime
    position: int
```

Compilation:

```python
while any(item.cycle_durations for item in work_items):
    ready = [
        item
        for item in work_items
        if item.cycle_durations
        and item.next_eligible_at <= cursor
    ]

    if not ready:
        cursor = min(
            item.next_eligible_at
            for item in work_items
            if item.cycle_durations
        )
        continue

    item = choose_ready_item(ready)
    duration = item.cycle_durations.popleft()
    emit_cycle(
        zone_id=item.zone_id,
        start=cursor,
        duration_minutes=duration,
    )
    cursor += timedelta(minutes=duration)
    item.next_eligible_at = (
        cursor + timedelta(minutes=item.minimum_soak_minutes)
    )
    cursor += inter_zone_gap
```

Soil types should populate editable suggestions, not determine invisible behavior:

* Clay: shorter cycle, longer soak.
* Loam: intermediate.
* Sand: longer cycle, shorter soak.
* Slope: reduce suggested cycle.
* High precipitation-rate heads: reduce suggested cycle.

The user must see the exact cycle and soak values.

## 17. Executor state machine

Use one executor and one execution lock per controller.

```python
class ExecutorState(StrEnum):
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
```

### 17.1 Single-flight behavior

Only one active run may exist per controller.

If a WebSocket or service request attempts to start another run:

```json
{
  "code": "controller_busy",
  "message": "A watering run is already active",
  "active_run_id": "...",
  "active_program": "Morning Lawn"
}
```

Version 1 should reject the request. It should not implicitly queue, merge, or replace a running occurrence.

A later explicit operation may support:

```text
Stop current run and replace
```

but replacement should never be the default.

Automatic occurrences that arrive during a manual scheduler run follow the program’s missed-run policy.

## 18. Normal step execution

The commanded duration is the primary clock.

```python
async def async_start_step(self, step: RunStep) -> None:
    command = PendingCommand.for_step(step)

    self._journal.state = ExecutorState.STARTING
    self._journal.pending_command = command
    await self._storage.async_write_journal_now(self._journal)

    try:
        await self._driver.async_start_zone(
            step.zone_reference,
            step.duration_minutes,
            command.command_id,
        )
    except HomeAssistantError as err:
        await self._async_reconcile_uncertain_start(step, err)
        return

    now = dt_util.utcnow()
    self._journal.state = ExecutorState.WATERING
    self._journal.current_step_actual_start = now
    self._journal.current_step_expected_end = (
        now + timedelta(minutes=step.duration_minutes)
    )
    self._journal.pending_command.disposition = CommandDisposition.SENT
    await self._storage.async_write_journal_now(self._journal)

    self._schedule_expected_end_callback()
```

Do not build one long coroutine containing repeated `asyncio.sleep()` calls. Each transition persists state, schedules one future callback, and returns to Home Assistant.

## 19. Post-step observation and the five-second gap

Do not synchronously refresh Rain Bird after every step.

A refresh is itself another controller request and can collide with:

* The core one-minute poll.
* App activity.
* A start command.
* A stop command.

The normal path is:

1. Trust the commanded duration as the execution clock.
2. Listen passively for source-state changes.
3. At expected end, enter `INTER_ZONE_GAP`.
4. Start the next zone after the configured gap.
5. Do not block because a stale switch still reports on.

State observations carry:

```python
@dataclass
class ControllerObservation:
    observed_at_utc: datetime
    active_zone_ids: frozenset[str]
    rain_sensor_active: bool | None
    rain_delay_days: int | None
    source_available: bool
    freshness: ObservationFreshness
```

Only fresh contradictory evidence blocks progression.

Example:

* Switch says zone is active.
* State was last updated 40 seconds before expected end.
* That observation does not prove an overrun.
* Proceed according to the commanded clock.

Request an active refresh only for:

* Uncertain command result.
* Restart reconciliation.
* Fresh evidence of an overrun.
* External-zone conflict.
* Manual diagnostic request.
* Rain-sensor ambiguity.

Such refreshes are best effort. A device-busy or timeout response does not immediately fail the run.

## 20. Overrun handling

At expected end:

* If there is no fresh contradictory observation, progress after the gap.
* If a fresh observation after expected end still shows the same zone active, enter `RECONCILING`.
* Trigger one best-effort refresh.
* Wait up to `overrun_confirmation_seconds`.
* If fresh observations continue to show the zone active, issue a controller-wide stop and mark the step `OVERRUN_STOPPED`.
* Create a repair issue only after the overrun is confirmed, not merely because an old state is stale.

This prevents the five-second inter-zone gap from becoming a hidden synchronous polling loop.

## 21. Uncertain command handling

Do not blindly repeat a timed-out start command.

After an error:

1. Persist `UNCERTAIN`.
2. Wait briefly without holding the controller execution lock.
3. Inspect passive state changes.
4. If needed, request one best-effort observation.
5. Classify:

```text
Requested zone active       Treat command as accepted
Different zone active       External conflict
No zone active              Eligible for retry
Controller unavailable      Delay and retry within lateness limit
```

Suggested retry delays:

```text
2, 5, 10, 20 seconds
```

Retries stop when:

* The latest permissible start is exceeded.
* Another zone is active.
* Rain or delay policy blocks the run.
* The user stops the run.
* The maximum attempt count is reached.

## 22. Early termination and rain-sensor cuts

The executor needs a distinct early-end path.

An early end occurs when the intended zone transitions to off before:

```text
expected_end - early_end_tolerance
```

Classify it using current evidence:

### `SENSOR_CUT`

Conditions:

* Intended zone ended early.
* Rain sensor is active or became active near the transition.
* No user stop was issued.
* No replacement zone appeared.

Default policy:

* Mark the current step `SENSOR_CUT`.
* Mark the run `ABORTED_SENSOR`.
* Skip remaining steps.
* Do not automatically restart when the sensor dries.

Alternative policies:

* Pause until dry if still inside the watering window.
* Resume remaining zones on the same day.
* Defer the entire remaining requirement.

Rain Bird states that an active rain or rain/freeze sensor can immediately cancel watering, but its documentation is clearest about scheduled watering. Whether all supported controllers similarly terminate a locally requested LNK single-zone manual run must be tested on hardware. ([Rain Bird Connected Device Support][7])

### Software freeze cut

A rain/freeze combo sensor reports rain and freeze on the same LNK boolean and its threshold is set on the sensor hardware, so a *software* freeze guard reads an independent temperature entity and enforces a user-set threshold. Unlike a rain cut — where the hardware has already closed the valve and the executor only classifies the early end — a freeze detected mid-run finds the zone still watering, so the executor issues an explicit stop before applying `freeze_cut_behavior`. The cut reuses `StepStatus.SENSOR_CUT` and `RunOutcome.ABORTED_SENSOR` with a `low_temperature` reason, and a pause reuses `PAUSED_SENSOR` disambiguated by `paused_reason`. A paused run resumes only once the temperature rises a fixed 1 °C above the threshold (hysteresis), and a rain pause never resumes into a freeze nor a freeze pause into a wet sensor. If Home Assistant restarts mid-pause, recovery keeps the pause and a post-recover reading resumes it if the condition cleared during downtime. The same threshold check runs before every occurrence and every zone step (as with the rain delay, §23); when the temperature source is unknown, the `when_unavailable` policy decides between watering and blocking, defaulting to water-and-note.

### `EXTERNAL_STOP`

Conditions:

* Zone ended early.
* No rain evidence.
* No scheduler stop context.
* No controller failure evidence.

Likely causes:

* Rain Bird app stop.
* Physical controller stop.
* Another Home Assistant automation.
* Controller reset.

Default policy: abort the run and require a new explicit start.

### `CONTROLLER_POWER_LOSS`

Use when the controller becomes unavailable and all state disappears. Rain Bird states that active watering stops on power loss and the remaining active watering session is cancelled. ([Rain Bird Connected Device Support][8])

## 23. Rain-delay semantics

The Rain Bird number entity is measured in days. The panel must label it:

```text
Native Rain Bird rain delay: 2 days
```

It must not say only “Delay: 2”.

More importantly, native Rain Bird rain delay suspends automatic irrigation but does not prevent manual station or program runs. Home Assistant’s scheduler uses manual zone commands, so it must explicitly enforce the rain-delay state itself. ([Rain Bird Connected Device Support][9])

Before every occurrence and every zone step:

```python
if rain_delay_days > 0 and program.rain_policy.honor_native_delay:
    skip_or_defer(...)
```

Setting native rain delay through the scheduler also affects native automatic programs. The confirmation text should say:

```text
This sets the Rain Bird controller’s rain delay in days.
It suppresses native automatic programs but does not itself block manual
zone runs. Rain Bird Scheduler will separately honor the value according
to each program’s rain policy.
```

## 24. Seasonal adjustment

Do not claim to reproduce Rain Bird’s proprietary automatic seasonal-adjust algorithm.

Rain Bird describes its adjustment as using historical weather, recent observed weather, and forecast information. It changes watering duration as a percentage and recommends programming base runtimes for the hottest, driest period. ([Rain Bird Connected Device Support][7])

Implement transparent providers.

```python
class DurationProvider(Protocol):
    async def async_calculate(
        self,
        zone: ZoneProfile,
        program: Program,
        occurrence: ProgramOccurrence,
    ) -> AdjustmentResult:
        ...
```

```python
@dataclass(frozen=True)
class AdjustmentResult:
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
```

Initial providers:

1. Fixed 100%.
2. Manual percentage.
3. Twelve-month seasonal curve.
4. External percentage entity.
5. External calculated-runtime entity.
6. Optional Smart Irrigation adapter.

No other irrigation integration should be a hard dependency.

## 25. Manual-run seasonal-budget uncertainty

Do not assume that controller seasonal adjustment is or is not applied to a single-station LNK manual run.

Rain Bird documentation shows that seasonal adjustment changes zone/program runtimes, but it does not clearly document the exact behavior of the local LNK manual-station command across controller families. ([Rain Bird Connected Device Support][10])

Represent the capability as:

```python
class ManualBudgetBehavior(StrEnum):
    UNKNOWN = "unknown"
    IGNORED = "ignored"
    APPLIED = "applied"
```

During commissioning, offer a guided hardware test:

1. Choose a safe test zone.
2. Set native seasonal adjustment to 100%.
3. Command a 10-minute Home Assistant run.
4. Record actual active time.
5. Set native adjustment to 50%.
6. Repeat the 10-minute command.
7. Restore the native setting.
8. Classify behavior.

Expected interpretations:

```text
~10 min at both settings       IGNORED
~10 min then ~5 min            APPLIED
Inconsistent/unavailable       UNKNOWN
```

Until the behavior is known, onboarding should recommend leaving native adjustment at its neutral value for deterministic HA-controlled durations. For common program-based Rain Bird controllers, that neutral value is 100%. ([Rain Bird Connected Device Support][11])

The integration should never silently compensate by dividing by the native percentage. If native adjustment is confirmed to affect manual runs, the supported choices are:

* Keep it at neutral.
* Use the native controller as schedule authority.
* Later expose a validated native-budget integration.

## 26. Internal water-balance provider

This can be added after the fixed and external providers are stable.

Conceptually:

```text
ETc = ET0 × landscape coefficient

new deficit =
    old deficit
    + ETc
    - effective rainfall
    - effective irrigation

required depth =
    min(deficit, permitted refill depth)

runtime =
    required depth
    ÷ precipitation rate
```

Roles remain separate:

* Weather determines demand.
* Soil and root depth determine storage.
* Precipitation rate determines runtime.
* Slope and infiltration determine Cycle+Soak.
* Irrigation efficiency modifies delivered water.
* Actual completed watering updates the deficit.
* Skipped sub-minute demand can be carried forward.

Do not infer sprinkler precipitation rate from soil type.

## 27. App and external-control conflicts

The scheduler must assume the Rain Bird app or physical controller may start watering independently.

When an unplanned Rain Bird zone becomes active:

```text
No scheduler run active:
    Mark controller externally active.
    Prevent scheduler starts.

Scheduler run waiting:
    Delay or skip according to missed-run policy.

Scheduler zone active and different zone appears:
    Mark scheduler run interrupted.
    Pause or abort according to policy.
```

The scheduler must not repeatedly issue commands to reclaim the controller.

Because the LNK device accepts one incoming request at a time, the diagnostics panel should show:

```text
Controller busy or app activity may delay commands.
Avoid leaving the Rain Bird app actively communicating during scheduled runs.
```

That limitation is documented by Home Assistant. ([Home Assistant][1])

## 28. Restart recovery

At setup:

1. Load controller configuration.
2. Load the execution journal.
3. Resolve all source entities.
4. Cancel obsolete callbacks.
5. Inspect the active journal state.
6. Obtain a controller observation, using one best-effort refresh if needed.
7. Reconcile.

Cases:

### Journal says `WATERING`, intended zone active

Continue waiting until the persisted expected end.

### Journal says `WATERING`, controller idle, expected end passed recently

Mark the step complete and continue to the next step if still within the run’s missed-run tolerance.

### Journal says `STARTING`, intended zone active

Treat the uncertain command as accepted.

### Journal says `STARTING`, controller idle

Retry only if the latest permissible start has not passed.

### Different zone active

Enter `PAUSED_EXTERNAL`.

### Run too old

Mark remaining work skipped with:

```text
RESTART_MISSED_TOLERANCE
```

### No journal but a Rain Bird zone is active

Classify it as external watering.

Never execute an occurrence whose occurrence ID is already:

* Active.
* Completed.
* Skipped.
* Aborted within the deduplication retention period.

## 29. Lifecycle event entity and history

Create one event entity per controller:

```text
event.<controller>_irrigation_lifecycle
```

Event types:

```python
[
    "run_started",
    "zone_started",
    "zone_completed",
    "run_completed",
    "run_skipped",
    "run_interrupted",
    "run_failed",
    "sensor_cut",
    "controller_overrun",
]
```

Example:

```python
self._trigger_event(
    "zone_started",
    {
        "run_id": run_id,
        "program_id": program_id,
        "zone_id": zone_id,
        "duration_minutes": duration,
    },
)
self.async_write_ha_state()
```

Home Assistant recommends event entities over directly emitting arbitrary bus events because event entities make available event types easier to identify and automate. The event entity retains the timestamp, event type, and optional event data for its most recent event. ([Home Assistant][12])

The event entity is not the detailed run database. The Store history remains authoritative.

Do not promise that every lifecycle detail will appear optimally in every History or Logbook view. The event entity exists for:

* Automation triggers.
* Last-event state.
* UI discovery.
* Basic recorder state retention where enabled.

Optional raw bus events may be added for backward compatibility, but they are not required for version 1.

## 30. Other entities

Create:

```text
calendar.<controller>_irrigation_plan
sensor.<controller>_next_irrigation
sensor.<controller>_active_zone
sensor.<controller>_expected_end
sensor.<controller>_seasonal_adjustment
sensor.<controller>_last_run
binary_sensor.<controller>_scheduler_running
binary_sensor.<controller>_native_schedule_conflict
binary_sensor.<controller>_external_watering
switch.<controller>_scheduler_enabled
button.<controller>_stop_controller
event.<controller>_irrigation_lifecycle
```

The calendar should expose compiled zone and cycle events, not merely one program-wide block.

Example event:

```text
Summary: Morning Lawn — Back Lawn — Cycle 2/3
Start:   9:36 AM
End:     9:40 AM
```

## 31. Full-screen panel

A full-screen panel is justified because the interface must edit recurrence, zones, soil, adjustment sources, and timelines together.

Sections:

### Overview

* Controller availability.
* Authority mode.
* Active source.
* Active zone.
* Expected completion.
* Rain sensor.
* Native rain delay in days.
* Current adjustment.
* Next compiled occurrence.
* Native conflict warning.
* Stop controller.

### Programs

* Enabled state.
* Recurrence summary.
* Requested start.
* Planned start/end.
* Zone order.
* Base total.
* Adjusted total.
* Run now.
* Duplicate.
* Disable.

### Program editor

* Weekdays, odd, even, cyclic.
* Multiple start times.
* Drag-and-drop zone order.
* Runtime override.
* Requested offset.
* Cycle and soak values.
* Adjustment provider.
* Rain policy.
* Missed-run policy.
* Watering window.
* Timeline preview.

### Zones

* Rain Bird entity.
* Station number.
* Base runtime.
* Soil.
* Slope.
* Precipitation rate.
* Flow.
* Cycle+Soak.
* Minimum-runtime behavior.
* Programs using the zone.

### Adjustments

* Provider.
* Input entities.
* Input age.
* Exact calculation.
* Quantized result.
* Staleness.
* Seven-day preview.

### History

* Requested start.
* Planned start.
* Actual start.
* Runtime calculation.
* Cycles.
* Sensor stops.
* External interruptions.
* Retries.
* Command uncertainty.
* Final outcome.

### Diagnostics

* Rain Bird source entry.
* Controller model.
* Source entities.
* Latest observation timestamps.
* Core polling age.
* Active callbacks.
* Journal state.
* Native capability flags.
* Manual seasonal-budget classification.
* App/contention warnings.

## 32. Panel serving and cache busting

Register static files using the current asynchronous static-path API:

```python
from homeassistant.components.http import StaticPathConfig

await hass.http.async_register_static_paths(
    [
        StaticPathConfig(
            "/rainbird_scheduler/frontend",
            str(frontend_directory),
            True,
        )
    ]
)
```

The old synchronous static-path registration was deprecated and removed; Home Assistant directs integrations to use `async_register_static_paths`. ([Home Assistant][13])

Register the panel with a versioned or content-hashed module URL:

```python
frontend.async_register_built_in_panel(
    hass,
    component_name="custom",
    sidebar_title="Irrigation",
    sidebar_icon="mdi:sprinkler-variant",
    frontend_url_path="rainbird-scheduler",
    config={
        "_panel_custom": {
            "name": "rainbird-scheduler-panel",
            "embed_iframe": False,
            "trust_external": False,
            "module_url": (
                "/rainbird_scheduler/frontend/panel.js"
                f"?v={FRONTEND_BUILD_HASH}"
            ),
        }
    },
    require_admin=True,
)
```

Use:

* `cache_headers=True` for release builds with a changed query hash.
* `cache_headers=False` in frontend development mode.
* One global panel registration, regardless of controller count.
* Panel removal when the final scheduler entry unloads.

The same cache-busted approach is used by established Home Assistant custom integrations such as HACS.

## 33. WebSocket API

Register:

```text
rainbird_scheduler/config/get
rainbird_scheduler/config/update
rainbird_scheduler/program/list
rainbird_scheduler/program/create
rainbird_scheduler/program/update
rainbird_scheduler/program/delete
rainbird_scheduler/program/duplicate
rainbird_scheduler/zone/list
rainbird_scheduler/zone/update
rainbird_scheduler/plan/preview
rainbird_scheduler/run/start
rainbird_scheduler/run/stop
rainbird_scheduler/run/pause
rainbird_scheduler/run/resume
rainbird_scheduler/run/skip_current
rainbird_scheduler/history/list
rainbird_scheduler/diagnostics/get
rainbird_scheduler/subscribe
```

Home Assistant provides a supported path for integrations to register custom WebSocket commands and for panels to call them through the authenticated `hass` connection. ([Home Assistant][2])

### Authorization

Reads:

* Any authenticated user, unless controller visibility is later restricted.

Mutations:

* Administrator only.

### Optimistic concurrency

Every mutable object contains a revision.

Update request:

```json
{
  "type": "rainbird_scheduler/program/update",
  "entry_id": "...",
  "program_id": "...",
  "expected_revision": 8,
  "patch": {
    "name": "Morning Lawn",
    "enabled": true
  }
}
```

Conflict response:

```json
{
  "code": "revision_conflict",
  "current_revision": 9,
  "current_value": {}
}
```

Use the same mechanism for `config/update`, including authority-mode changes.

## 34. Public actions

Expose:

```text
rainbird_scheduler.run_program
rainbird_scheduler.run_zones
rainbird_scheduler.stop_controller
rainbird_scheduler.pause
rainbird_scheduler.resume
rainbird_scheduler.skip_current
rainbird_scheduler.recalculate
rainbird_scheduler.set_program_enabled
rainbird_scheduler.set_rain_delay
```

`run_zones` requires an ordered set of:

```yaml
zones:
  - entity_id: switch.front_lawn
    duration: 12
  - entity_id: switch.side_lawn
    duration: 8
```

The action validates all zones before starting. It must reject a target list spanning more than one Rain Bird controller.

## 35. Native queue upstream work

### 35.1 `pyrainbird`

Add:

```python
@dataclass(frozen=True)
class ControllerCapabilities:
    stack_manual_runs: bool
    read_current_queue: bool
    write_schedule: bool
    write_water_budget: bool
    write_zone_adjustment: bool
```

Then implement:

```python
async def supports_command(self, command: int) -> bool
async def stack_irrigate_zone(self, zone: int, minutes: int) -> None
async def get_current_queue(self) -> IrrigationQueue
async def set_water_budget(self, program: int, percent: int) -> None
```

Tests must use captured protocol fixtures from specific controller models.

### 35.2 Home Assistant core

Expose stable actions:

```text
rainbird.run_sequence
rainbird.get_queue
rainbird.get_schedule_snapshot
rainbird.patch_schedule
rainbird.set_water_budget
```

Add stable translated errors:

```text
device_busy
unsupported_command
controller_unavailable
schedule_verification_failed
```

### 35.3 Scheduler activation

The native driver is selected only when required capabilities are present.

```python
if capabilities.stack_manual_runs and capabilities.read_current_queue:
    driver = NativeQueueDriver(...)
else:
    driver = HomeAssistantEntityDriver(...)
```

The driver abstraction ships in version 1 but the native implementation remains disabled. Therefore, upstream support can land later without restructuring the planner or executor.

Cycle+Soak plans should continue using the Home Assistant driver unless the native queue’s delay semantics are fully decoded and tested.

## 36. Native schedule writing

Treat schedule writing as a separate later project.

Begin with one controller-family codec:

```text
EspMeTm2ScheduleCodec
```

Reverse-engineered documentation describes ESP-ME/TM2 schedule pages for program information, start times, station runtimes, and queue formats. That information is useful for test development but should not be treated as a universal controller protocol.

Every write operation must:

1. Read the complete current schedule.
2. Validate controller model and firmware.
3. Save a pre-write snapshot.
4. Calculate the minimal changed pages.
5. Write one page at a time.
6. Read the complete schedule again.
7. Compare logical and raw results.
8. Restore prior pages after verification failure.
9. Read again after restoration.
10. Report exact page-level failure details.

Do not expose schedule writing for unknown controllers.

## 37. Synchronization with the Rain Bird app

Version 1 does not promise bidirectional app synchronization.

Potential native schedule synchronization later requires:

```text
last synchronized native hash
current native hash
last synchronized HA revision
current HA revision
```

Rules:

```text
Native changed, HA unchanged:
    Import native change.

HA changed, native unchanged:
    Write HA change.

Both changed:
    Show conflict; do not overwrite.

Neither changed:
    Do nothing.
```

Required validation:

* Local write followed by reopening the app.
* App edit followed by local read.
* App open but idle during write.
* App actively communicating during write.
* Cloud synchronization after local write.
* Controller restart.
* Home Assistant interruption during multi-page update.

Until those tests pass, the app and Home Assistant should not both edit the same native schedule.

## 38. Runtime dependencies

Keep runtime dependencies at zero beyond Home Assistant:

```json
"requirements": []
```

Use Home Assistant and standard-library facilities:

* `Store`.
* Entity registry.
* Device registry.
* Calendar entity.
* Event entity.
* WebSocket API.
* Repairs.
* Scheduling helpers.
* Dataclasses.
* Enums.
* `Decimal`.
* Voluptuous.

Do not add:

* APScheduler.
* A second Rain Bird library instance.
* A separate SQLite database.
* A weather API library.
* Pydantic solely for storage.
* A hard dependency on another irrigation integration.

Optional external runtime providers communicate through Home Assistant entities.

## 39. Development dependencies

Backend:

```text
pytest
pytest-asyncio
pytest-homeassistant-custom-component
hypothesis
ruff
mypy
coverage
pre-commit
```

Frontend:

```text
typescript
lit
vite
vitest
playwright
eslint
prettier
```

Use `hypothesis` heavily for schedule invariants.

## 40. Repository layout

```text
rainbird-scheduler/
├── custom_components/
│   └── rainbird_scheduler/
│       ├── __init__.py
│       ├── manifest.json
│       ├── const.py
│       ├── config_flow.py
│       ├── models.py
│       ├── recurrence.py
│       ├── planner.py
│       ├── executor.py
│       ├── observations.py
│       ├── conditions.py
│       ├── storage.py
│       ├── services.py
│       ├── services.yaml
│       ├── websocket.py
│       ├── frontend.py
│       ├── calendar.py
│       ├── event.py
│       ├── sensor.py
│       ├── binary_sensor.py
│       ├── switch.py
│       ├── button.py
│       ├── diagnostics.py
│       ├── repairs.py
│       ├── driver/
│       │   ├── __init__.py
│       │   ├── base.py
│       │   ├── ha_entity.py
│       │   ├── native_queue.py
│       │   └── native_schedule.py
│       ├── adjustment/
│       │   ├── __init__.py
│       │   ├── base.py
│       │   ├── fixed.py
│       │   ├── manual.py
│       │   ├── monthly.py
│       │   ├── entity.py
│       │   └── water_balance.py
│       ├── translations/
│       │   └── en.json
│       ├── brand/
│       │   ├── icon.png
│       │   └── logo.png
│       └── frontend/
│           └── panel.js
├── frontend/
│   ├── src/
│   │   ├── panel.ts
│   │   ├── api.ts
│   │   ├── models.ts
│   │   ├── overview.ts
│   │   ├── program-editor.ts
│   │   ├── timeline-preview.ts
│   │   ├── zone-editor.ts
│   │   ├── adjustment-view.ts
│   │   ├── history-view.ts
│   │   └── diagnostics-view.ts
│   ├── package.json
│   ├── tsconfig.json
│   └── vite.config.ts
├── tests/
│   ├── conftest.py
│   ├── test_config_flow.py
│   ├── test_storage.py
│   ├── test_storage_migrations.py
│   ├── test_recurrence.py
│   ├── test_dst.py
│   ├── test_planner.py
│   ├── test_quantization.py
│   ├── test_overlap.py
│   ├── test_cycle_soak.py
│   ├── test_adjustment.py
│   ├── test_executor.py
│   ├── test_uncertain_commands.py
│   ├── test_restart_recovery.py
│   ├── test_sensor_cut.py
│   ├── test_external_conflict.py
│   ├── test_single_flight.py
│   └── test_websocket.py
├── hacs.json
├── pyproject.toml
├── README.md
└── LICENSE
```

## 41. Planner invariants

Property-based tests must prove:

```text
No two compiled steps overlap on one controller.
Actual start is never earlier than requested start.
Every command duration is an integer from 1 through 1440.
A positive sub-resolution runtime is never silently removed.
Cycle durations sum exactly to the quantized zone total.
No cycle begins before its minimum soak interval.
Identical inputs produce identical timelines.
A completed occurrence is never executed twice.
No occurrence begins after its maximum lateness.
Calendar preview and executor use the same compiled plan.
Every skipped step has a stored reason.
Every runtime adjustment has provenance.
Every program revision conflict is rejected rather than overwritten.
```

## 42. Executor tests

Test:

* Normal multi-zone run.
* All zones with the same requested start.
* Five-second gaps.
* Core one-minute poll during a transition.
* Best-effort refresh returning device busy.
* Command timeout before controller acceptance.
* Command timeout after controller acceptance.
* Controller unavailable.
* Source entity renamed.
* Source entity removed.
* Different Rain Bird zone externally activated.
* Rain Bird app stop.
* Physical controller stop.
* Rain sensor activates mid-zone.
* Zone ends early without rain.
* Rain delay changes during a run.
* User presses Stop controller.
* Manual `run/start` while automatic run active.
* Automatic occurrence while manual run active.
* Home Assistant restart before a command.
* Restart immediately after command acceptance.
* Restart during watering.
* Restart during inter-zone gap.
* Restart after expected completion.
* Duplicate callback delivery.
* Power loss.
* Storage migration.
* Corrupted or incomplete history data.
* Stale WebSocket revision.

## 43. Hardware matrix

At minimum:

| Test                                      |     LNK1 |     LNK2 |
| ----------------------------------------- | -------: | -------: |
| Single-zone start                         | Required | Required |
| Stop controller                           | Required | Required |
| App closed                                | Required | Required |
| App open and idle                         | Required | Required |
| App manual activity                       | Required | Required |
| Five-second gaps                          | Required | Required |
| Core poll during transition               | Required | Required |
| Network loss                              | Required | Required |
| Controller power cycle                    | Required | Required |
| Rain delay active during HA manual run    | Required | Required |
| Physical sensor trip during HA manual run | Required | Required |
| Seasonal 100% versus 50% manual run       | Required | Required |
| Command latency distribution              | Required | Required |
| Device-busy behavior                      | Required | Required |

For native queue support later:

* Stack-command latency.
* Maximum queue depth.
* Queue persistence after HA disconnect.
* App visibility of queued runs.
* LNK1 versus LNK2 queue format.
* Skip/stop behavior.
* Queue behavior during sensor trip.

Controller-family coverage should include at least one ESP-TM2-family device before claiming general support. Other families remain supported only through the existing public HA action until explicitly tested.

The first release applies only to devices that remain supported by the local Home Assistant Rain Bird integration. The official integration currently states that migration to Rain Bird 2.0/IQ4 firmware is incompatible with that local integration. ([Home Assistant][1])

## 44. Implementation sequence

### PR 1 — Integration contract and discovery

Implement:

* Manifest.
* Config flow.
* One-entry-per-controller enforcement.
* Source entity discovery.
* Stable zone references.
* HA entity driver.
* Device linkage.
* Diagnostics.
* Basic developer actions for starting and stopping.

Expected result:

* A selected Rain Bird controller and all its zones are resolved.
* A single zone can run for an explicit integer duration.
* Stop controller works.
* No private Rain Bird APIs or second connections are used.

There is no polished user program editor yet. Testing is through Developer Tools, actions, and automated fixtures.

### PR 2 — Recurrence, planner, and quantization

Implement:

* Recurrence.
* DST rules.
* Same-time serialization.
* Controller-wide overlap handling.
* Exact-to-quantized runtime conversion.
* Sub-resolution policy.
* Pure planner tests.
* Calendar preview data model.

Expected result:

* Multiple zones may all request 9:00 AM.
* One deterministic nonoverlapping plan is produced.
* Cycle totals and quantized totals are mathematically consistent.

Still no polished frontend.

### PR 3 — Journaled executor

Implement:

* State machine.
* Execution journal.
* Immediate `Store.async_save()` transitions.
* Command-intent records.
* Single-flight behavior.
* Retry and uncertainty handling.
* Passive observations.
* Nonblocking post-step behavior.
* Restart recovery.
* External conflict handling.
* Early-stop classification.
* Event entity.

Expected result:

* A full multi-zone plan runs reliably.
* Restart does not duplicate a command.
* A busy controller does not create blind retries.
* Concurrent manual starts are explicitly rejected.

This backend is demonstrable through services and Developer Tools, but not yet comfortable for ordinary users.

### PR 4 — Scheduler panel and WebSocket CRUD

Implement:

* Static-path registration.
* Cache-busted panel.
* Overview.
* Program list.
* Program editor.
* Zone editor.
* Timeline preview.
* `config/update`.
* Optimistic concurrency.
* Compact Lovelace status card.

Expected result:

* Programs can be created and edited without YAML.
* Controller defaults remain editable after setup.
* Requested and compiled start times are visible.

### PR 5 — Rain and adjustment providers

Implement:

* Fixed provider.
* Manual percentage.
* Monthly curve.
* External percentage entity.
* External runtime entity.
* Native rain-delay policy.
* Rain-sensor cut handling.
* Freeze hooks — delivered: a software freeze guard reads a temperature/`weather.*` entity and enforces a user-set threshold at every occurrence and zone step, with a mid-run cut, hysteresis resume, and rain-vs-freeze disambiguation on a shared combo sensor (§22).
* Adjustment provenance.

Expected result:

* Every adjusted duration can be explained.
* Rain delay is correctly treated as days and manually enforced.
* Sensor-cut runs stop according to explicit policy.

### PR 6 — Cycle+Soak and soil profiles

Implement:

* Quantized cycle allocation.
* Minimum soak constraints.
* Interleaving.
* Soil presets.
* Slope adjustments.
* Calendar cycle events.
* UI cycle preview.

Expected result:

* Cycle durations sum to the quantized total.
* Soak intervals are respected.
* Other ready zones fill soak periods.

### PR 7 — Release hardening

Implement:

* Repairs.
* Diagnostics downloads.
* Config and storage migration tests.
* HACS metadata.
* Minimum HA version.
* Local brand assets.
* Translation coverage.
* Frontend build reproducibility.
* Playwright tests.
* Hardware commissioning workflow.
* Guided seasonal-budget test.

Expected result:

* Renamed or missing source entities generate actionable repairs.
* Storage upgrades preserve programs and active-run recovery.
* Frontend cache changes correctly between releases.

### PR 8 — Upstream native queue support

Implement upstream:

* Stack station command.
* Current queue decoding.
* Capability discovery.
* Stable Home Assistant actions.
* Model-specific fixtures.

Then enable `NativeQueueDriver` through capability detection.

Expected result:

* Compatible controllers receive an entire simple sequence before the first zone starts.
* Home Assistant can reconcile its run plan against the native queue.

No scheduler architecture refactor should be necessary because the driver interface shipped in PR 1.

### PR 9 — Native schedule writing

Implement:

* Model-specific schedule codec.
* Read-before-write snapshot.
* Minimal page patch.
* Read-after-write verification.
* Restoration.
* App synchronization testing.
* Conflict hashes.

Expected result:

* A supported controller can store an HA-authored schedule locally.
* App and HA edits cannot silently overwrite each other.
* Unknown controller families remain read-only.

## 45. Version 1 completion criteria

Version 1 is complete when all of the following are true:

1. Every zone in a program may share the same requested start time.
2. The compiled plan never opens two scheduler-controlled zones simultaneously.
3. Durations are explicitly rounded once, never incidentally truncated.
4. Sub-minute adjusted results are visible and policy-controlled.
5. Five-second transitions do not require synchronous controller polling.
6. A timed-out command is reconciled before retry.
7. Restart recovery does not duplicate a zone start.
8. Rain delay is honored despite manual Rain Bird runs bypassing it.
9. Rain-sensor early termination has its own recorded outcome.
10. A second manual or automatic run cannot silently overlap an active run.
11. Every runtime has calculation provenance.
12. Every skip, interruption, and failure has a structured reason.
13. Program and controller updates use revisions.
14. The integration never imports private Rain Bird runtime data.
15. The integration never opens a second Rain Bird connection.
16. Native queue and native schedule features remain optional and capability-detected.
17. The entire scheduler is usable without native schedule writing.

The resulting version 1 is not merely an automation generator. It is a persistent, resource-constrained irrigation scheduler that uses the current Rain Bird integration as its controller driver while compensating for the core integration’s actual limits: integer-minute commands, controller-wide stop semantics, slow observation polling, one-request-at-a-time communication, and app contention.

[1]: https://www.home-assistant.io/integrations/rainbird "https://www.home-assistant.io/integrations/rainbird"
[2]: https://developers.home-assistant.io/docs/frontend/extending/websocket-api/ "https://developers.home-assistant.io/docs/frontend/extending/websocket-api/"
[3]: https://developers.home-assistant.io/blog/2025/07/18/updated-pattern-for-helpers-linking-to-devices/ "https://developers.home-assistant.io/blog/2025/07/18/updated-pattern-for-helpers-linking-to-devices/"
[4]: https://developers.home-assistant.io/docs/core/integration/config_flow/ "https://developers.home-assistant.io/docs/core/integration/config_flow/"
[5]: https://www.home-assistant.io/actions/rainbird.start_irrigation/ "https://www.home-assistant.io/actions/rainbird.start_irrigation/"
[6]: https://wifi.rainbird.com/articles/advanced-water-saving-features-with-the-rc2-in-the-rain-bird-2-0-app/ "https://wifi.rainbird.com/articles/advanced-water-saving-features-with-the-rc2-in-the-rain-bird-2-0-app/"
[7]: https://wifi.rainbird.com/articles/what-is-automatic-seasonal-adjust/ "https://wifi.rainbird.com/articles/what-is-automatic-seasonal-adjust/"
[8]: https://wifi.rainbird.com/articles/if-the-power-goes-out-do-i-lose-the-schedule-on-my-controller/ "https://wifi.rainbird.com/articles/if-the-power-goes-out-do-i-lose-the-schedule-on-my-controller/"
[9]: https://wifi.rainbird.com/articles/delay-watering-for-days/ "https://wifi.rainbird.com/articles/delay-watering-for-days/"
[10]: https://wifi.rainbird.com/rb_faq/controller-not-watering-run-times-set-zones/ "https://wifi.rainbird.com/rb_faq/controller-not-watering-run-times-set-zones/"
[11]: https://wifi.rainbird.com/articles/how-to-enable-automatic-rain-delay-in-rain-bird-2-0-app/ "https://wifi.rainbird.com/articles/how-to-enable-automatic-rain-delay-in-rain-bird-2-0-app/"
[12]: https://developers.home-assistant.io/docs/core/entity/event/ "https://developers.home-assistant.io/docs/core/entity/event/"
[13]: https://developers.home-assistant.io/blog/2024/06/18/async_register_static_paths/ "https://developers.home-assistant.io/blog/2024/06/18/async_register_static_paths/"

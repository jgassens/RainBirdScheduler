"""Constants for the Rain Bird Scheduler integration."""

from __future__ import annotations

DOMAIN = "rainbird_scheduler"
SOURCE_DOMAIN = "rainbird"

INTEGRATION_VERSION = "0.6.3"

# Config entry data keys.
CONF_SOURCE_CONFIG_ENTRY_ID = "source_config_entry_id"
CONF_SOURCE_UNIQUE_ID = "source_unique_id"
CONF_AUTHORITY_MODE = "authority_mode"
CONF_ACKNOWLEDGE_CONFLICT = "acknowledge_conflict"

# Storage.
STORAGE_VERSION_CONFIG = 1
STORAGE_VERSION_JOURNAL = 1
STORAGE_VERSION_HISTORY = 1


def config_store_key(entry_id: str) -> str:
    """Return the Store key for the configuration store."""
    return f"{DOMAIN}.{entry_id}.config"


def journal_store_key(entry_id: str) -> str:
    """Return the Store key for the execution journal store."""
    return f"{DOMAIN}.{entry_id}.journal"


def history_store_key(entry_id: str) -> str:
    """Return the Store key for the history store."""
    return f"{DOMAIN}.{entry_id}.history"


# Controller timing defaults (seconds/minutes).
DEFAULT_INTER_ZONE_GAP_SECONDS = 5
DEFAULT_START_OBSERVATION_TIMEOUT_SECONDS = 45
DEFAULT_EARLY_END_TOLERANCE_SECONDS = 45
DEFAULT_OVERRUN_CONFIRMATION_SECONDS = 30
DEFAULT_MISSED_RUN_TOLERANCE_MINUTES = 30

# Command handling.
COMMAND_RETRY_DELAYS_SECONDS = (2, 5, 10, 20)
MAX_COMMAND_ATTEMPTS = len(COMMAND_RETRY_DELAYS_SECONDS) + 1

# Evidence newer than this counts as fresh when contradicting the commanded
# clock (the core integration polls valve state every minute).
OBSERVATION_FRESHNESS_WINDOW_SECONDS = 90

# Freeze guard: a paused run resumes only once the temperature climbs this far
# back above the threshold (fixed hysteresis, applied in Celsius). A software
# temperature source is polled far less often than valve state, so it is
# considered stale — and treated as "temperature unknown" — after this age.
FREEZE_RESUME_HYSTERESIS_C = "1.0"
FREEZE_TEMP_STALE_AFTER_SECONDS = 3600

# The Rain Bird public action accepts integer minutes in this range.
MIN_COMMAND_MINUTES = 1
MAX_COMMAND_MINUTES = 1440

# History ring-buffer bounds.
HISTORY_MAX_RUNS = 250
HISTORY_MAX_ZONE_RECORDS = 2000
HISTORY_MAX_INTERVENTIONS = 500

# Occurrence deduplication window: the journal keeps the most recent
# finished occurrences and trims by count only, never by age.
DEDUP_MAX_OCCURRENCES = 500

# How far ahead the planner compiles candidate occurrences.
PLAN_HORIZON_DAYS = 7

# Lifecycle event entity.
EVENT_RUN_STARTED = "run_started"
EVENT_ZONE_STARTED = "zone_started"
EVENT_ZONE_COMPLETED = "zone_completed"
EVENT_RUN_COMPLETED = "run_completed"
EVENT_RUN_SKIPPED = "run_skipped"
EVENT_RUN_INTERRUPTED = "run_interrupted"
EVENT_RUN_FAILED = "run_failed"
EVENT_SENSOR_CUT = "sensor_cut"
EVENT_CONTROLLER_OVERRUN = "controller_overrun"

LIFECYCLE_EVENT_TYPES = [
    EVENT_RUN_STARTED,
    EVENT_ZONE_STARTED,
    EVENT_ZONE_COMPLETED,
    EVENT_RUN_COMPLETED,
    EVENT_RUN_SKIPPED,
    EVENT_RUN_INTERRUPTED,
    EVENT_RUN_FAILED,
    EVENT_SENSOR_CUT,
    EVENT_CONTROLLER_OVERRUN,
]

# Dispatcher signals are defined in coordinator.py next to their consumers.

# Frontend panel.
PANEL_URL_PATH = "rainbird-scheduler"
PANEL_TITLE = "Irrigation"
PANEL_ICON = "mdi:sprinkler-variant"
FRONTEND_STATIC_URL = f"/{DOMAIN}/frontend"

# Services.
SERVICE_RUN_PROGRAM = "run_program"
SERVICE_RUN_ZONES = "run_zones"
SERVICE_STOP_CONTROLLER = "stop_controller"
SERVICE_PAUSE = "pause"
SERVICE_RESUME = "resume"
SERVICE_SKIP_CURRENT = "skip_current"
SERVICE_RECALCULATE = "recalculate"
SERVICE_SET_PROGRAM_ENABLED = "set_program_enabled"
SERVICE_SET_RAIN_DELAY = "set_rain_delay"

ATTR_PROGRAM_ID = "program_id"
ATTR_ZONES = "zones"
ATTR_DURATION = "duration"
ATTR_ENTITY_ID = "entity_id"
ATTR_ENABLED = "enabled"
ATTR_DAYS = "days"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"

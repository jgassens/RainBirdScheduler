"""Irrigation driver package.

Version 1 always selects the Home Assistant entity driver. The native queue
driver ships as a disabled interface and is chosen only when a future
upstream stack exposes the required capabilities (plan §35.3).
"""

from __future__ import annotations

from .base import (
    CommandUncertainError,
    DriverError,
    IrrigationDriver,
    ZoneValidationError,
)

__all__ = [
    "CommandUncertainError",
    "DriverError",
    "IrrigationDriver",
    "ZoneValidationError",
    "select_driver",
]

# Native queue support is capability-detected and disabled in version 1.
NATIVE_QUEUE_ENABLED = False


def select_driver(
    ha_entity_driver: IrrigationDriver,
    native_queue_driver: IrrigationDriver | None = None,
) -> IrrigationDriver:
    """Pick the execution backend from advertised capabilities."""
    if (
        NATIVE_QUEUE_ENABLED
        and native_queue_driver is not None
        and native_queue_driver.capabilities.stack_manual_runs
        and native_queue_driver.capabilities.read_current_queue
    ):
        return native_queue_driver
    return ha_entity_driver

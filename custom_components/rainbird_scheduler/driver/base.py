"""Driver interface (plan §3). Pure module: no Home Assistant imports."""

from __future__ import annotations

from typing import Protocol

from ..models import (
    CommandResult,
    ControllerObservation,
    DriverCapabilities,
    ZoneReference,
)


class DriverError(Exception):
    """Base class for driver failures."""


class ZoneValidationError(DriverError):
    """The target zone failed pre-send validation; nothing was sent.

    This is definitive: the command never reached the controller, so the
    executor may treat it as REJECTED without reconciliation.
    """


class CommandUncertainError(DriverError):
    """The command may or may not have reached the controller.

    Raised when the underlying action errors or times out after dispatch;
    the executor must reconcile against observed state before retrying.
    """


class IrrigationDriver(Protocol):
    """Execution backend for one controller."""

    @property
    def capabilities(self) -> DriverCapabilities:
        """Advertised backend capabilities."""
        ...

    async def async_start_zone(
        self,
        zone: ZoneReference,
        duration_minutes: int,
        command_id: str,
    ) -> CommandResult:
        """Start one zone for an integer number of minutes."""
        ...

    async def async_stop_controller(self) -> CommandResult:
        """Stop all watering on the controller."""
        ...

    async def async_observe(self) -> ControllerObservation:
        """Return the current passive observation of the controller."""
        ...

    async def async_request_observation(self) -> None:
        """Request a best-effort active refresh of source entities."""
        ...

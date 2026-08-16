"""Native queue driver: interface present, implementation disabled (plan §3.2).

The known local protocol exposes queue reads (0x3B) and stacked station
starts (0x4B), but current ``pyrainbird`` and Home Assistant core do not
provide stable public surfaces for them. This driver becomes selectable only
once upstream support lands and capability detection confirms it; until
then, every method fails loudly rather than pretending to work.
"""

from __future__ import annotations

from ..models import (
    CommandResult,
    ControllerObservation,
    DriverCapabilities,
    ZoneReference,
)
from .base import DriverError


class NativeQueueDriver:
    """Disabled native-queue backend placeholder."""

    @property
    def capabilities(self) -> DriverCapabilities:
        """No native capabilities are available in version 1."""
        return DriverCapabilities(
            stack_manual_runs=False, read_current_queue=False
        )

    async def async_start_zone(
        self,
        zone: ZoneReference,
        duration_minutes: int,
        command_id: str,
    ) -> CommandResult:
        raise DriverError("Native queue driver is not available in version 1")

    async def async_stop_controller(self) -> CommandResult:
        raise DriverError("Native queue driver is not available in version 1")

    async def async_observe(self) -> ControllerObservation:
        raise DriverError("Native queue driver is not available in version 1")

    async def async_request_observation(self) -> None:
        raise DriverError("Native queue driver is not available in version 1")

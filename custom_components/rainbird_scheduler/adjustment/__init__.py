"""Duration adjustment providers (plan §24).

Every provider returns an :class:`AdjustmentResult` whose provenance fields
(`explanation`, `input_timestamps`, `stale_inputs`) make the calculation
fully explainable. No provider claims to reproduce Rain Bird's proprietary
seasonal-adjust algorithm.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from ..models import AdjustmentProviderConfig, ProviderKind, ZoneProfile
from .base import DurationProvider, StateReader
from .entity import EntityPercentProvider, EntityRuntimeProvider
from .fixed import FixedProvider
from .manual import ManualPercentProvider
from .monthly import MonthlyCurveProvider
from .seasonal import SeasonalAutoProvider

__all__ = [
    "DurationProvider",
    "EntityPercentProvider",
    "EntityRuntimeProvider",
    "FixedProvider",
    "ManualPercentProvider",
    "MonthlyCurveProvider",
    "SeasonalAutoProvider",
    "StateReader",
    "create_provider",
]


def create_provider(
    config: AdjustmentProviderConfig,
    *,
    get_state: StateReader,
    now_fn: Callable[[], datetime],
    get_zone: Callable[[str], ZoneProfile | None] | None = None,
    location: tuple[float, float] | None = None,
) -> DurationProvider:
    """Build the provider described by a program's configuration."""
    providers: dict[ProviderKind, Callable[[], DurationProvider]] = {
        ProviderKind.FIXED: FixedProvider,
        ProviderKind.MANUAL_PERCENT: lambda: ManualPercentProvider(
            config.percent
        ),
        ProviderKind.MONTHLY_CURVE: lambda: MonthlyCurveProvider(
            config.monthly_percents
        ),
        ProviderKind.SEASONAL_AUTO: lambda: SeasonalAutoProvider(location),
        ProviderKind.ENTITY_PERCENT: lambda: EntityPercentProvider(
            config.entity_id, get_state=get_state, now_fn=now_fn
        ),
        ProviderKind.ENTITY_RUNTIME: lambda: EntityRuntimeProvider(
            config.entity_id,
            get_state=get_state,
            now_fn=now_fn,
            get_zone=get_zone,
        ),
    }
    return providers.get(config.kind, FixedProvider)()

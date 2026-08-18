"""Diagnostics payload redaction."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from homeassistant.components.diagnostics.const import REDACTED
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rainbird_scheduler.diagnostics import build_diagnostics
from custom_components.rainbird_scheduler.models import (
    ConfigData,
    ExecutionJournal,
)

from .conftest import SOURCE_UNIQUE_ID
from .helpers import make_controller, make_program, make_zone


@pytest.fixture(autouse=True)
def _custom_integrations(enable_custom_integrations: None) -> None:
    """Allow loading the custom integration in this module."""


async def test_diagnostics_redacts_source_unique_id(
    hass: HomeAssistant, source_entry: MockConfigEntry
) -> None:
    """The controller's unique id (typically its MAC) is never emitted."""
    coordinator = SimpleNamespace(
        hass=hass,
        source_entry_id=source_entry.entry_id,
        config=ConfigData(
            controller=make_controller(source_unique_id=SOURCE_UNIQUE_ID),
            zones={"z": make_zone("z", 1)},
            programs={"p": make_program("p", ["z"])},
        ),
        executor=SimpleNamespace(journal=ExecutionJournal()),
        _zone_to_entity={"z": "switch.rain_bird_sprinkler_1"},
        last_observation=None,
        timeline=SimpleNamespace(runs=[], conflicts=[], warnings=[]),
        history=SimpleNamespace(
            history=SimpleNamespace(runs=[], zone_records=[], interventions=[])
        ),
    )
    payload = build_diagnostics(coordinator)
    assert payload["source"]["unique_id"] == REDACTED
    assert payload["controller_config"]["source_unique_id"] == REDACTED
    # The rest of the payload survives untouched.
    assert payload["source"]["config_entry_id"] == source_entry.entry_id
    assert payload["source"]["title"] == "Rain Bird"
    assert payload["zones"]["z"]["display_name"] == "Z"

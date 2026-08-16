"""Config flow (plan §5)."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rainbird_scheduler.const import (
    CONF_ACKNOWLEDGE_CONFLICT,
    CONF_AUTHORITY_MODE,
    CONF_SOURCE_CONFIG_ENTRY_ID,
    CONF_SOURCE_UNIQUE_ID,
    DOMAIN,
)

from .conftest import SOURCE_UNIQUE_ID


@pytest.fixture(autouse=True)
def _custom_integrations(enable_custom_integrations: None) -> None:
    """Allow loading the custom integration in this module."""


async def test_no_controllers_aborts(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == "abort"
    assert result["reason"] == "no_controllers"


async def test_full_flow_creates_entry(
    hass: HomeAssistant, source_entry: MockConfigEntry
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"source": source_entry.entry_id}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "authority"

    # The conflict acknowledgment is required.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_AUTHORITY_MODE: "ha_authoritative",
            CONF_ACKNOWLEDGE_CONFLICT: False,
        },
    )
    assert result["type"] == "form"
    assert result["errors"] == {"base": "acknowledge_required"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_AUTHORITY_MODE: "ha_authoritative",
            CONF_ACKNOWLEDGE_CONFLICT: True,
        },
    )
    assert result["type"] == "create_entry"
    assert result["title"] == "Rain Bird Scheduler"
    assert result["data"] == {
        CONF_SOURCE_CONFIG_ENTRY_ID: source_entry.entry_id,
        CONF_SOURCE_UNIQUE_ID: SOURCE_UNIQUE_ID,
        CONF_AUTHORITY_MODE: "ha_authoritative",
    }
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    # Stable unique id derived from the source's unique id (plan §5).
    assert entry.unique_id == f"rainbird:{SOURCE_UNIQUE_ID}"


async def test_already_attached_controller_not_offered(
    hass: HomeAssistant,
    source_entry: MockConfigEntry,
    scheduler_entry: MockConfigEntry,
) -> None:
    # The only controller already has a scheduler: nothing to offer.
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == "abort"
    assert result["reason"] == "no_controllers"

"""Config flow: bind one scheduler to one Rain Bird controller (plan §5)."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult
from homeassistant.helpers.selector import (
    BooleanSelector,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_ACKNOWLEDGE_CONFLICT,
    CONF_AUTHORITY_MODE,
    CONF_SOURCE_CONFIG_ENTRY_ID,
    CONF_SOURCE_UNIQUE_ID,
    DOMAIN,
    SOURCE_DOMAIN,
)
from .models import AuthorityMode


class RainBirdSchedulerConfigFlow(ConfigFlow, domain=DOMAIN):
    """One scheduler entry per source Rain Bird controller."""

    VERSION = 1

    def __init__(self) -> None:
        self._source: ConfigEntry | None = None

    def _available_sources(self) -> dict[str, str]:
        attached = {
            entry.data.get(CONF_SOURCE_CONFIG_ENTRY_ID)
            for entry in self._async_current_entries(include_ignore=False)
        }
        return {
            entry.entry_id: entry.title
            for entry in self.hass.config_entries.async_entries(SOURCE_DOMAIN)
            if entry.entry_id not in attached
        }

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the source Rain Bird controller."""
        sources = self._available_sources()
        if not sources:
            return self.async_abort(reason="no_controllers")

        if user_input is not None:
            self._source = self.hass.config_entries.async_get_entry(
                user_input["source"]
            )
            assert self._source is not None
            identity = self._source.unique_id or self._source.entry_id
            await self.async_set_unique_id(f"rainbird:{identity}")
            self._abort_if_unique_id_configured()
            return await self.async_step_authority()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("source"): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(value=key, label=label)
                                for key, label in sources.items()
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_authority(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the initial authority mode and acknowledge conflicts."""
        assert self._source is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get(CONF_ACKNOWLEDGE_CONFLICT):
                errors["base"] = "acknowledge_required"
            else:
                return self.async_create_entry(
                    title=f"{self._source.title} Scheduler",
                    data={
                        CONF_SOURCE_CONFIG_ENTRY_ID: self._source.entry_id,
                        CONF_SOURCE_UNIQUE_ID: self._source.unique_id,
                        CONF_AUTHORITY_MODE: user_input[CONF_AUTHORITY_MODE],
                    },
                )

        return self.async_show_form(
            step_id="authority",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_AUTHORITY_MODE,
                        default=AuthorityMode.HA_AUTHORITATIVE.value,
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(
                                    value=mode.value,
                                    label=mode.value,
                                )
                                for mode in AuthorityMode
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                            translation_key="authority_mode",
                        )
                    ),
                    vol.Required(
                        CONF_ACKNOWLEDGE_CONFLICT, default=False
                    ): BooleanSelector(),
                }
            ),
        )

"""Panel serving with cache busting (plan §32)."""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components import frontend
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant, callback

from .const import (
    FRONTEND_STATIC_URL,
    INTEGRATION_VERSION,
    PANEL_ICON,
    PANEL_TITLE,
    PANEL_URL_PATH,
)

_LOGGER = logging.getLogger(__name__)


async def async_register_panel(hass: HomeAssistant) -> None:
    """Serve the bundled frontend and register one global panel."""
    frontend_dir = Path(__file__).parent / "frontend"
    # The version lives in the PATH, not a query string: Safari serves
    # cached ES modules for query-only changes, so each upgrade must produce
    # a URL Safari has never seen.
    static_url = f"{FRONTEND_STATIC_URL}/{INTEGRATION_VERSION}"
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                static_url,
                str(frontend_dir),
                True,  # long cache headers are safe on a versioned path
            )
        ]
    )
    frontend.async_register_built_in_panel(
        hass,
        component_name="custom",
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        frontend_url_path=PANEL_URL_PATH,
        config={
            "_panel_custom": {
                "name": "rainbird-scheduler-panel",
                "embed_iframe": False,
                "trust_external": False,
                "module_url": f"{static_url}/panel.js",
            }
        },
        require_admin=True,
    )


@callback
def async_unregister_panel(hass: HomeAssistant) -> None:
    """Remove the panel when the final scheduler entry unloads."""
    frontend.async_remove_panel(hass, PANEL_URL_PATH)

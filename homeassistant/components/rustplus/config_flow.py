"""Config flow for Rust+ integration."""
from __future__ import annotations

import json
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from rustplus import ServerDetails

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("fcm_credentials"): str,
        vol.Required("server_ip"): str,
        vol.Required("server_port"): int,
        vol.Required("player_id"): int,
        vol.Required("player_token"): int,
    }
)

async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect.

    Data has the keys from STEP_USER_DATA_SCHEMA with values provided by the user.
    """
    try:
        fcm_creds = json.loads(data["fcm_credentials"])
    except json.JSONDecodeError as err:
        raise InvalidAuth from err

    server_details = ServerDetails(
        data["server_ip"],
        data["server_port"],
        data["player_id"],
        data["player_token"]
    )

    # We could theoretically test the websocket connection here,
    # but the rustplus library socket connection can be tested in setup
    # For now, we just validate the format.

    return {"title": f"Rust+ {data['server_ip']}:{data['server_port']}"}

class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Rust+."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        if user_input is None:
            return self.async_show_form(
                step_id="user", data_schema=STEP_USER_DATA_SCHEMA
            )

        errors = {}

        try:
            info = await validate_input(self.hass, user_input)
        except InvalidAuth:
            errors["base"] = "invalid_auth"
        except CannotConnect:
            errors["base"] = "cannot_connect"
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Unexpected exception")
            errors["base"] = "unknown"
        else:
            await self.async_set_unique_id(f"{user_input['server_ip']}_{user_input['server_port']}_{user_input['player_id']}")
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=info["title"], data=user_input)

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""

class RustPlusOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle Rust+ options."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        if user_input is not None:
            # We separate the input string by commas to allow multiple device additions
            switches = self.config_entry.options.get("switches", {})
            monitors = self.config_entry.options.get("storage_monitors", {})
            alarms = self.config_entry.options.get("smart_alarms", {})

            # Very basic string parsing for the MVP options flow
            # Format expected: "eid:name, eid:name"
            for line in user_input.get("add_switches", "").split(","):
                if ":" in line:
                    eid, name = line.split(":", 1)
                    switches[eid.strip()] = name.strip()

            for line in user_input.get("add_monitors", "").split(","):
                if ":" in line:
                    eid, name = line.split(":", 1)
                    monitors[eid.strip()] = name.strip()

            for line in user_input.get("add_alarms", "").split(","):
                if ":" in line:
                    eid, name = line.split(":", 1)
                    alarms[eid.strip()] = name.strip()

            new_options = {
                "switches": switches,
                "storage_monitors": monitors,
                "smart_alarms": alarms,
            }
            return self.async_create_entry(title="", data=new_options)

        options_schema = vol.Schema(
            {
                vol.Optional("add_switches", description={"suggested_value": ""}): str,
                vol.Optional("add_monitors", description={"suggested_value": ""}): str,
                vol.Optional("add_alarms", description={"suggested_value": ""}): str,
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=options_schema,
        )

# Add standard core options property to ConfigFlow
def async_get_options_flow(
    config_entry: config_entries.ConfigEntry,
) -> RustPlusOptionsFlowHandler:
    """Get the options flow for this handler."""
    return RustPlusOptionsFlowHandler(config_entry)

ConfigFlow.async_get_options_flow = async_get_options_flow

"""Config flow for Rust+ integration."""
from __future__ import annotations

import json
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("fcm_credentials"): str,
    }
)

STEP_DISCOVERY_SCHEMA = vol.Schema({})


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect.

    Data has the keys from STEP_USER_DATA_SCHEMA with values provided by the user.
    """
    try:
        fcm_creds = json.loads(data["fcm_credentials"])
    except json.JSONDecodeError as err:
        raise InvalidAuth from err

    return {"title": "Rust+ Account"}

class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Rust+."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "RustPlusOptionsFlowHandler":
        """Get the options flow for this handler."""
        return RustPlusOptionsFlowHandler()

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
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Unexpected exception")
            errors["base"] = "unknown"
        else:
            await self.async_set_unique_id("rustplus_account")
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=info["title"], data=user_input)

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_discovery(
        self, discovery_info: dict[str, Any]
    ) -> FlowResult:
        """Handle discovering a new server."""
        self.context["title_placeholders"] = {"name": discovery_info.get("name", "Unknown Server")}
        self.discovery_info = discovery_info
        
        await self.async_set_unique_id(f"{discovery_info['server_ip']}_{discovery_info['server_port']}_{discovery_info['player_id']}")
        self._abort_if_unique_id_configured()
        
        return await self.async_step_confirm_discovery()

    async def async_step_confirm_discovery(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm adding the discovered server."""
        if user_input is not None:
            return self.async_create_entry(
                title=self.context["title_placeholders"]["name"], 
                data=self.discovery_info
            )

        return self.async_show_form(
            step_id="confirm_discovery",
            data_schema=STEP_DISCOVERY_SCHEMA,
            description_placeholders={"name": self.context["title_placeholders"]["name"]}
        )


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""

class RustPlusOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle Rust+ options."""

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

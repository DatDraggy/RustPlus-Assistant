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
        json.loads(data["fcm_credentials"])
    except json.JSONDecodeError as err:
        raise InvalidAuth from err

    return {"title": "Rust+ Account"}


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

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Refresh the account's FCM credentials without losing paired devices.

        Regenerating credentials in the browser extension invalidates the old
        ones, so this lets you paste the new JSON into the existing account
        entry. Paired devices live on the separate server entries, so they are
        left completely untouched.
        """
        reconfigure_entry = self._get_reconfigure_entry()
        if "server_ip" in reconfigure_entry.data:
            # Only the account entry carries FCM credentials; servers don't.
            return self.async_abort(reason="reconfigure_account_only")

        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await validate_input(self.hass, user_input)
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    reconfigure_entry, data_updates=user_input
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
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

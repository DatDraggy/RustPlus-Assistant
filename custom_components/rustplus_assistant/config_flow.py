"""Config flow for Rust+ integration."""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import time
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import instance_id

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("fcm_credentials"): str,
    }
)

STEP_DISCOVERY_SCHEMA = vol.Schema({})

# How long to wait for the user to scan + approve the Steam QR code (seconds).
QR_TIMEOUT = 280


def _qr_data_uri(challenge: str) -> str:
    """Render a Steam QR challenge URL as a base64 PNG data URI for the flow UI."""
    import qrcode

    img = qrcode.make(challenge, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate manually-pasted FCM credentials JSON."""
    try:
        json.loads(data["fcm_credentials"])
    except json.JSONDecodeError as err:
        raise InvalidAuth from err

    return {"title": "Rust+ Account"}


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Rust+."""

    VERSION = 1

    def __init__(self) -> None:
        self._auth = None
        self._auth_task: asyncio.Task | None = None
        self._qr_uri: str | None = None
        self._creds: dict | None = None
        self._device_id: str | None = None
        self._reconfigure_entry = None
        self.discovery_info: dict | None = None

    # ----------------------------------------------------------------- entry #
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Let the user pick: scan a Steam QR (recommended) or paste credentials."""
        return self.async_show_menu(step_id="user", menu_options=["qr", "manual"])

    # --------------------------------------------------------- QR login flow #
    async def async_step_qr(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Display the Steam QR code (a form step renders the image; progress steps don't)."""
        from .auth import RustPlusQRAuth

        if user_input is None:
            if self._auth is None:
                self._auth = RustPlusQRAuth()
                try:
                    challenge = await self.hass.async_add_executor_job(self._auth.begin)
                except Exception:  # pylint: disable=broad-except
                    _LOGGER.exception("Failed to start Steam QR session")
                    return self.async_abort(reason="qr_begin_failed")
                self._qr_uri = await self.hass.async_add_executor_job(_qr_data_uri, challenge)
                # Stable per-install DeviceId: unique per HA instance (so instances don't
                # invalidate each other's Facepunch push slot), reused on re-auth.
                self._device_id = await instance_id.async_get(self.hass)
            return self.async_show_form(
                step_id="qr",
                data_schema=vol.Schema({}),
                description_placeholders={"qr_image": f"![Steam QR]({self._qr_uri})"},
            )
        # User pressed Submit after scanning -> wait for approval + register.
        return await self.async_step_qr_wait()

    async def async_step_qr_wait(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Poll for approval and finish registration (progress spinner)."""
        if self._auth_task is None:
            self._auth_task = self.hass.async_create_task(self._run_qr_auth())

        if not self._auth_task.done():
            return self.async_show_progress(
                step_id="qr_wait",
                progress_action="awaiting_scan",
                progress_task=self._auth_task,
            )

        try:
            self._creds = self._auth_task.result()
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Steam QR authentication failed")
            self._auth_task = None  # allow the user to try again
            return self.async_show_progress_done(next_step_id="qr_failed")

        return self.async_show_progress_done(next_step_id="qr_finish")

    async def _run_qr_auth(self) -> dict:
        """Poll for approval, then complete the auth + FCM registration (executor)."""
        from .auth import RustPlusAuthError

        deadline = time.time() + QR_TIMEOUT
        while time.time() < deadline:
            await asyncio.sleep(3)
            refresh = await self.hass.async_add_executor_job(self._auth.poll)
            if refresh:
                return await self.hass.async_add_executor_job(
                    self._auth.complete, refresh, self._device_id
                )
        raise RustPlusAuthError("Timed out waiting for the Steam QR code to be approved")

    async def async_step_qr_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Store the freshly-acquired credentials (new entry, or update on reconfigure)."""
        data = {"fcm_credentials": json.dumps(self._creds)}
        if self._reconfigure_entry is not None:
            return self.async_update_reload_and_abort(self._reconfigure_entry, data_updates=data)
        await self.async_set_unique_id("rustplus_account")
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="Rust+ Account", data=data)

    async def async_step_qr_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """QR auth failed or timed out — offer to start over."""
        if user_input is not None:
            self._auth = None
            self._auth_task = None
            return await self.async_step_qr()
        return self.async_show_form(step_id="qr_failed", data_schema=vol.Schema({}))

    # ----------------------------------------------------- manual JSON paste #
    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Fallback: paste the FCM credentials JSON (e.g. from the browser extension)."""
        if user_input is None:
            return self.async_show_form(step_id="manual", data_schema=STEP_USER_DATA_SCHEMA)

        errors: dict[str, str] = {}
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
            step_id="manual", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    # -------------------------------------------------------- reconfigure ## #
    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Refresh the account's credentials (QR or manual) without losing paired devices."""
        entry = self._get_reconfigure_entry()
        if "server_ip" in entry.data:
            return self.async_abort(reason="reconfigure_account_only")
        return self.async_show_menu(
            step_id="reconfigure", menu_options=["reconfigure_qr", "reconfigure_manual"]
        )

    async def async_step_reconfigure_qr(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Re-auth via Steam QR, updating the existing account entry."""
        self._reconfigure_entry = self._get_reconfigure_entry()
        return await self.async_step_qr()

    async def async_step_reconfigure_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Re-auth by pasting credentials JSON, updating the existing account entry."""
        entry = self._get_reconfigure_entry()
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
                return self.async_update_reload_and_abort(entry, data_updates=user_input)

        return self.async_show_form(
            step_id="reconfigure_manual", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    # ----------------------------------------------------- server discovery #
    async def async_step_discovery(
        self, discovery_info: dict[str, Any]
    ) -> FlowResult:
        """Handle discovering a new server."""
        self.context["title_placeholders"] = {"name": discovery_info.get("name", "Rust Server")}
        self.discovery_info = discovery_info

        await self.async_set_unique_id(
            f"{discovery_info['server_ip']}_{discovery_info['server_port']}_{discovery_info['player_id']}"
        )
        self._abort_if_unique_id_configured()

        return await self.async_step_confirm_discovery()

    async def async_step_confirm_discovery(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm adding the discovered server (the discovery card already shows its name)."""
        if user_input is not None:
            return self.async_create_entry(
                title=self.context["title_placeholders"]["name"],
                data=self.discovery_info,
            )

        return self.async_show_form(
            step_id="confirm_discovery",
            data_schema=STEP_DISCOVERY_SCHEMA,
        )


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""

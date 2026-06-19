"""The Rust+ integration."""
from __future__ import annotations

import json
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from rustplus import RustSocket, ServerDetails, FCMListener

from .const import DOMAIN
from .coordinator import RustPlusDataCoordinator
from .fcm_manager import RustPlusFCMManager

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["switch", "sensor", "binary_sensor"]

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Rust+ from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    server_ip = entry.data["server_ip"]
    server_port = entry.data["server_port"]
    player_id = entry.data["player_id"]
    player_token = entry.data["player_token"]

    server_details = ServerDetails(server_ip, server_port, player_id, player_token)
    socket = RustSocket(server_details)


    try:
        await socket.connect()
    except Exception as err:
        raise ConfigEntryNotReady(f"Failed to connect to Rust+ server: {err}") from err

    coordinator = RustPlusDataCoordinator(hass, socket)
    await coordinator.async_config_entry_first_refresh()

    fcm_creds = entry.data.get("fcm_credentials")
    fcm_manager = None
    if fcm_creds:
        fcm_manager = RustPlusFCMManager(hass, fcm_creds)
        fcm_manager.start()

    hass.data[DOMAIN][entry.entry_id] = {
        "socket": socket,
        "coordinator": coordinator,
        "fcm_manager": fcm_manager
    }


    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        data = hass.data[DOMAIN].pop(entry.entry_id)
        socket = data["socket"]
        await socket.disconnect()

    return unload_ok

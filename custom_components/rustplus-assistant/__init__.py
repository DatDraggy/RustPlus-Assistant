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

PLATFORMS: list[str] = ["switch", "sensor", "binary_sensor", "camera"]

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Rust+ from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    if "server_ip" not in entry.data:
        # This is an Account entry
        fcm_creds = entry.data.get("fcm_credentials")
        fcm_manager = None
        if fcm_creds:
            fcm_manager = RustPlusFCMManager(hass, fcm_creds)
            fcm_manager.start()

        hass.data[DOMAIN][entry.entry_id] = {
            "fcm_manager": fcm_manager,
            "type": "account"
        }
        return True

    # This is a Server entry
    server_ip = entry.data["server_ip"]
    server_port = entry.data["server_port"]
    player_id = entry.data["player_id"]
    player_token = entry.data["player_token"]

    server_details = ServerDetails(server_ip, server_port, player_id, player_token)
    socket = RustSocket(server_details)

    try:
        from rustplus.remote.proxy.proxy_value_grabber import ProxyValueGrabber
        
        # Pre-fetch the Proxy value in a worker thread so the internal requests.get
        # doesn't block the Home Assistant asyncio event loop during socket.connect()
        def prefetch_proxy_value():
            ProxyValueGrabber.get_value()
            
        await hass.async_add_executor_job(prefetch_proxy_value)
        
        await socket.connect()
    except Exception as err:
        raise ConfigEntryNotReady(f"Failed to connect to Rust+ server: {err}") from err

    from rustplus.remote.websocket.ws import RustWebsocket
    if not hasattr(RustWebsocket, "_original_handle_message"):
        RustWebsocket._original_handle_message = RustWebsocket.handle_message
        
        async def safe_handle_message(self, app_message):
            try:
                await self._original_handle_message(app_message)
            except Exception as e:
                if "RequestError" in type(e).__name__:
                    self.logger.warning("Suppressed unhandled RequestError in websocket: %s", e)
                else:
                    raise e
        
        RustWebsocket.handle_message = safe_handle_message

    coordinator = RustPlusDataCoordinator(hass, socket)
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {
        "socket": socket,
        "coordinator": coordinator,
        "type": "server"
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if hass.data[DOMAIN][entry.entry_id].get("type") == "account":
        data = hass.data[DOMAIN].pop(entry.entry_id)
        fcm_manager = data.get("fcm_manager")
        if fcm_manager and hasattr(fcm_manager, 'listener'):
            try:
                fcm_manager.listener.close()
            except Exception:
                _LOGGER.debug("Failed to stop FCM listener cleanly")
        return True

    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        data = hass.data[DOMAIN].pop(entry.entry_id)
        socket = data["socket"]
        await socket.disconnect()

    return unload_ok

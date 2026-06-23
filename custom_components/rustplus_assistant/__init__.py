"""The Rust+ integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr

from rustplus import RustSocket, ServerDetails

from .camera_session import RustPlusCameraSession
from .const import DOMAIN
from .coordinator import RustPlusDataCoordinator
from .fcm_manager import RustPlusFCMManager

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["switch", "sensor", "binary_sensor", "event", "camera", "button"]

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

    # NOTE: reaches into rustplus private internals (pinned via manifest
    # requirements). Wrapped so a library change degrades gracefully instead of
    # breaking setup entirely.
    try:
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
    except Exception as err:
        _LOGGER.debug("Could not install RustWebsocket error guard: %s", err)

    coordinator = RustPlusDataCoordinator(hass, socket, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {
        "socket": socket,
        "coordinator": coordinator,
        # Cameras stream on their own isolated socket so they can't take down the
        # data socket (map/poll/events); created lazily on first use.
        "camera_session": RustPlusCameraSession(hass, server_details),
        "type": "server"
    }

    # Register the per-server hub device so the map, cameras and paired devices
    # nest under it (their device_info points here via `via_device`).
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{server_ip}_{server_port}")},
        name=entry.title,
        manufacturer="Facepunch",
        model="Rust Server",
    )

    # Reload when options change (e.g. a camera is added/removed, or a device is
    # auto-discovered on pairing) so the affected entities are (re)created.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True

async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload a server entry after its options change."""
    await hass.config_entries.async_reload(entry.entry_id)

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if hass.data[DOMAIN][entry.entry_id].get("type") == "account":
        data = hass.data[DOMAIN].pop(entry.entry_id)
        fcm_manager = data.get("fcm_manager")
        if fcm_manager:
            fcm_manager.close()
        return True

    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        data = hass.data[DOMAIN].pop(entry.entry_id)
        camera_session = data.get("camera_session")
        if camera_session is not None:
            await camera_session.close()
        socket = data["socket"]
        await socket.disconnect()

    return unload_ok

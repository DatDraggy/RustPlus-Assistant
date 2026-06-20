"""FCM Listener Manager for Rust+."""
from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.issue_registry import IssueSeverity, async_create_issue

from rustplus import FCMListener

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

class RustPlusFCMManager:
    """Manager for FCM Listener."""

    def __init__(self, hass: HomeAssistant, fcm_credentials: str) -> None:
        """Initialize the FCM Manager."""
        self.hass = hass
        try:
            creds = json.loads(fcm_credentials)
            # Unwrap if the user pasted the full wrapper JSON from the extension
            if "fcm_credentials" in creds:
                creds = creds["fcm_credentials"]
        except json.JSONDecodeError:
            creds = {}
            _LOGGER.error("Invalid FCM credentials format.")

        data = {"fcm_credentials": creds}
        self.listener = FCMListener(data)
        self.listener.on_notification = self._on_notification

    def start(self) -> None:
        """Start listening to FCM notifications."""
        self.listener.start(daemon=True)

    def _on_notification(self, obj: Any, notification: dict, data_message: dict) -> None:
        """Handle incoming FCM notification."""
        # Dispatch to the main event loop
        self.hass.loop.call_soon_threadsafe(
            self._handle_notification_threadsafe, notification, data_message
        )

    @callback
    def _handle_notification_threadsafe(self, notification: dict, data_message: dict) -> None:
        """Handle incoming FCM notification safely on the event loop."""
        if hasattr(data_message, "app_data"):
            data_message = {getattr(item, "key", ""): getattr(item, "value", "") for item in data_message.app_data}
        elif type(data_message) is not dict:
            try:
                data_message = dict(data_message)
            except Exception:
                pass
                
        _LOGGER.warning("Received FCM data message: %s", data_message)

        title = data_message.get("title") or (notification.get("title", "") if isinstance(notification, dict) else "")
        message = data_message.get("message") or (notification.get("body", "") if isinstance(notification, dict) else "")
        
        if message.startswith("{"):
            message = "An event occurred on the server."

        channel_id = data_message.get("channelId")
        if channel_id == "pairing":
            body_str = data_message.get("body")
            body_data = {}
            if body_str:
                try:
                    body_data = json.loads(body_str)
                except Exception:
                    pass

            # If it's a server pairing
            if body_data.get("type") == "server":
                ip = body_data.get("ip")
                port = body_data.get("port")
                player_id = body_data.get("playerId")
                player_token = body_data.get("playerToken")
                name = body_data.get("name", "Rust Server")

                if ip and port and player_id and player_token:
                    self.hass.async_create_task(
                        self.hass.config_entries.flow.async_init(
                            DOMAIN,
                            context={"source": "discovery"},
                            data={
                                "server_ip": str(ip),
                                "server_port": int(port),
                                "player_id": int(player_id),
                                "player_token": int(player_token),
                                "name": name,
                            }
                        )
                    )
                return

            # Otherwise, it's a Smart Device pairing
            entity_id = body_data.get("entityId") or data_message.get("entityId")
            entity_type = body_data.get("entityType") or data_message.get("entityType", "unknown")
            entity_name = body_data.get("entityName") or data_message.get("entityName", "Rust+ Device")

            if entity_id and entity_name in ["Smart Switch", "Smart Alarm", "Storage Monitor", "Rust+ Device"]:
                entity_name = f"{entity_name} ({str(entity_id)[-4:]})"

            if entity_id:
                self.hass.async_create_task(self._async_auto_discover_device(entity_id, entity_type, entity_name))

        else:
            _LOGGER.warning("Rust+ Notification Event Fired: %s - %s", title, message)
            
            event_data = {
                "title": title,
                "message": message,
                "channel_id": channel_id,
            }
            
            body_str = data_message.get("body")
            if body_str:
                try:
                    b_data = json.loads(body_str)
                    if "ip" in b_data:
                        event_data["server_ip"] = b_data["ip"]
                    if "port" in b_data:
                        event_data["server_port"] = b_data["port"]
                        
                    # If this is an alarm, tell all binary sensors for this server to poll instantly
                    if channel_id == "alarm" and "ip" in b_data:
                        from homeassistant.helpers.dispatcher import async_dispatcher_send
                        
                        entity_id = data_message.get("entityId")
                        if not entity_id and isinstance(notification, dict):
                            entity_id = notification.get("entityId")
                            
                        _LOGGER.warning("Dispatching alarm refresh for IP %s with title %s and entity_id %s", b_data['ip'], title, entity_id)
                        async_dispatcher_send(self.hass, f"rustplus_alarm_refresh_{b_data['ip']}", title, message, entity_id)
                except Exception as e:
                    _LOGGER.error("Error processing FCM body: %s", e)

            self.hass.bus.async_fire("rustplus_notification", event_data)
            
            self.hass.async_create_task(
                self.hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "message": message,
                        "title": title
                    }
                )
            )

    async def _async_auto_discover_device(self, entity_id: str, entity_type: str, entity_name: str) -> None:
        """Attempt to auto-discover which server a device belongs to."""
        from homeassistant.helpers.issue_registry import async_create_issue, IssueSeverity
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            data = self.hass.data[DOMAIN].get(entry.entry_id)
            if data and data.get("type") == "server":
                socket = data.get("socket")
                if socket:
                    try:
                        info = await socket.get_entity_info(int(entity_id))
                        if type(info).__name__ != "RustError":
                            options = dict(entry.options)
                            if str(entity_type) in ["1", "Switch", "Smart Switch"]:
                                switches = dict(options.get("switches", {}))
                                switches[str(entity_id)] = entity_name
                                options["switches"] = switches
                            elif str(entity_type) in ["2", "Alarm", "Smart Alarm"]:
                                alarms = dict(options.get("smart_alarms", {}))
                                alarms[str(entity_id)] = entity_name
                                options["smart_alarms"] = alarms
                            else:
                                monitors = dict(options.get("storage_monitors", {}))
                                monitors[str(entity_id)] = entity_name
                                options["storage_monitors"] = monitors
                            
                            self.hass.config_entries.async_update_entry(entry, options=options)
                            
                            await self.hass.services.async_call(
                                "persistent_notification",
                                "create",
                                {
                                    "message": f"Successfully discovered and added '{entity_name}' to {entry.title}!",
                                    "title": "Rust+ Device Paired"
                                }
                            )
                            type_str = "Smart Device"
                            if str(entity_type) in ["1", "Switch", "Smart Switch"]:
                                type_str = "Smart Switch"
                            elif str(entity_type) in ["2", "Alarm", "Smart Alarm"]:
                                type_str = "Smart Alarm"
                                
                            async_create_issue(
                                self.hass,
                                DOMAIN,
                                f"rename_device_{entity_id}",
                                is_fixable=False,
                                severity=IssueSeverity.WARNING,
                                translation_key="rename_device",
                                translation_placeholders={
                                    "name": entity_name,
                                    "id": str(entity_id),
                                    "type": type_str
                                }
                            )
                            
                            self.hass.async_create_task(
                                self.hass.config_entries.async_reload(entry.entry_id)
                            )
                            return
                    except Exception as err:
                        _LOGGER.error("Failed to query entity info: %s", err)

        # Fall back to repair issue if we couldn't match it
        async_create_issue(
            self.hass,
            DOMAIN,
            f"pair_{entity_id}",
            is_fixable=False,
            severity=IssueSeverity.WARNING,
            translation_key="device_paired",
            translation_placeholders={
                "name": entity_name,
                "type": entity_type,
                "id": str(entity_id)
            },
        )

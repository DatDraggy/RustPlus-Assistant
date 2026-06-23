"""FCM Listener Manager for Rust+."""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.issue_registry import IssueSeverity, async_create_issue

from rustplus import FCMListener

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# How often the watchdog fires to verify the FCM listener is healthy.
_WATCHDOG_INTERVAL = timedelta(hours=1)

# If no FCM notification has been received for this many seconds, the watchdog
# assumes the connection is silently dead and forces a reconnect.  Google's FCM
# connections to mtalk.google.com typically survive 1-7 days but can drop at
# any time without signalling the client.  25 hours is conservative enough to
# avoid false positives while still catching stale connections within a day.
_MAX_SILENCE_SECONDS = 25 * 60 * 60  # 25 hours

class RustPlusFCMManager:
    """Manager for FCM Listener."""

    def __init__(self, hass: HomeAssistant, fcm_credentials: str) -> None:
        """Initialize the FCM Manager."""
        self.hass = hass
        self._watchdog_cancel = None
        self._last_activity: float = time.monotonic()
        try:
            creds = json.loads(fcm_credentials)
            # Unwrap if the user pasted the full wrapper JSON from the extension
            if "fcm_credentials" in creds:
                creds = creds["fcm_credentials"]
        except json.JSONDecodeError:
            creds = {}
            _LOGGER.error("Invalid FCM credentials format.")

        self._fcm_data = {"fcm_credentials": creds}
        self._build_listener()

    def _build_listener(self) -> None:
        """Create and wire up a fresh FCMListener instance."""
        self.listener = FCMListener(self._fcm_data)
        self.listener.on_notification = self._on_notification

    def start(self) -> None:
        """Start listening to FCM notifications and arm the watchdog."""
        self.listener.start(daemon=True)
        self._watchdog_cancel = async_track_time_interval(
            self.hass, self._async_watchdog, _WATCHDOG_INTERVAL
        )
        _LOGGER.debug("FCM listener started; watchdog armed (interval: %s).", _WATCHDOG_INTERVAL)

    @callback
    def _async_watchdog(self, _now: Any) -> None:
        """Periodically verify the FCM listener is healthy; restart if not.

        Two failure modes are checked:

        1. **Thread death** - the push_receiver listen() call returned or raised,
           causing the daemon thread to exit.  Detected via thread.is_alive().

        2. **Silent socket death** - the thread is still alive (blocked on
           recv() on the mtalk.google.com SSL socket) but Google has dropped
           the connection.  No exception is raised and no data arrives.  This is
           the most common real-world failure mode.  Detected by tracking the
           time since the last received notification; if it exceeds
           _MAX_SILENCE_SECONDS we proactively tear down and rebuild.
        """
        # --- Check 1: thread death ---
        thread: threading.Thread | None = None
        for attr in ("thread", "_thread", "_listener_thread", "listener_thread"):
            candidate = getattr(self.listener, attr, None)
            if isinstance(candidate, threading.Thread):
                thread = candidate
                break

        if thread is not None and not thread.is_alive():
            _LOGGER.warning(
                "FCM listener thread is no longer alive - reconnecting."
            )
            self._restart_listener()
            return

        # --- Check 2: silent socket death ---
        silence = time.monotonic() - self._last_activity
        if silence > _MAX_SILENCE_SECONDS:
            _LOGGER.warning(
                "No FCM activity for %.1f hours - assuming the connection is "
                "silently dead.  Reconnecting.",
                silence / 3600,
            )
            self._restart_listener()
            return

        _LOGGER.debug(
            "FCM watchdog: healthy (thread alive=%s, silence=%.0fs).",
            thread.is_alive() if thread else "unknown",
            silence,
        )

    def _restart_listener(self) -> None:
        """Tear down the current listener and start a fresh one."""
        try:
            self.listener.close()
        except Exception:  # noqa: BLE001
            pass
        self._build_listener()
        self.listener.start(daemon=True)
        self._last_activity = time.monotonic()
        _LOGGER.info("FCM listener successfully restarted.")

    def close(self) -> None:
        """Cancel the watchdog and stop the FCM listener."""
        if self._watchdog_cancel is not None:
            self._watchdog_cancel()
            self._watchdog_cancel = None
        try:
            self.listener.close()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Failed to stop FCM listener cleanly.")

    def _on_notification(self, obj: Any, notification: dict, data_message: dict) -> None:
        """Handle incoming FCM notification."""
        self._last_activity = time.monotonic()
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
                
        # NOTE: data_message can carry the server's playerToken/playerId in its
        # "body" field during pairing. Never log it verbatim — log only
        # non-sensitive routing metadata, and only at debug level.
        _LOGGER.debug(
            "Received FCM notification (channel=%s, title=%s)",
            data_message.get("channelId"),
            data_message.get("title"),
        )

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
            _LOGGER.debug("Rust+ Notification Event Fired: %s - %s", title, message)
            
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
                        
                    # If this is an alarm, notify every alarm entity for this
                    # server. Facepunch's alarm push does NOT include an entityId
                    # (confirmed via logging), so we cannot tell which specific
                    # alarm fired — all alarm entities on the server react, and
                    # disambiguation is left to each entity and the title/message.
                    if channel_id == "alarm" and "ip" in b_data:
                        _LOGGER.debug(
                            "Dispatching alarm refresh for IP %s (title=%s)",
                            b_data['ip'], title,
                        )
                        async_dispatcher_send(
                            self.hass, f"rustplus_alarm_refresh_{b_data['ip']}", title, message
                        )
                except Exception as e:
                    _LOGGER.error("Error processing FCM body: %s", e)

            self.hass.bus.async_fire("rustplus_notification", event_data)

            # Only surface a persistent notification when there's something to show —
            # some pushes (e.g. the one that arrives alongside a server pairing) carry
            # no title or message and would otherwise create an empty notification.
            if title or message:
                self.hass.async_create_task(
                    self.hass.services.async_call(
                        "persistent_notification",
                        "create",
                        {
                            "message": message,
                            "title": title,
                        },
                    )
                )

    async def _async_auto_discover_device(self, entity_id: str, entity_type: str, entity_name: str) -> None:
        """Attempt to auto-discover which server a device belongs to."""
        from homeassistant.helpers.issue_registry import async_create_issue, IssueSeverity
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            data = self.hass.data[DOMAIN].get(entry.entry_id)
            if data and data.get("type") == "server":
                socket = data.get("socket")
                coordinator = data.get("coordinator")
                if socket and coordinator:
                    try:
                        # Serialize with the coordinator's polling on the shared websocket.
                        async with coordinator.api_lock:
                            info = await socket.get_entity_info(int(entity_id))
                        if type(info).__name__ == "RustError":
                            _LOGGER.warning(
                                "Entity %s not found on server entry '%s' (RustError: %s). "
                                "The socket may be stale — try reloading the server entry if "
                                "pairing is not auto-discovered.",
                                entity_id, entry.title, info,
                            )
                        else:
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
                            
                            # The async_update_entry above changes options, which
                            # fires the entry's update listener and reloads it so
                            # the new device entity is created.
                            return
                    except Exception as err:
                        _LOGGER.error("Failed to query entity info for entity %s on server '%s': %s", entity_id, entry.title, err)

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

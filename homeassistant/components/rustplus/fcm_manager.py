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
        _LOGGER.debug("Received FCM notification: %s", notification)
        _LOGGER.debug("Received FCM data message: %s", data_message)

        title = notification.get("title", "")
        body = notification.get("body", "")

        channel_id = data_message.get("channelId")
        if channel_id == "pairing":
            entity_id = data_message.get("entityId")
            entity_type = data_message.get("entityType", "unknown")
            entity_name = data_message.get("entityName", "Rust+ Device")

            issue_id = f"pair_{entity_id}" if entity_id else "pair_unknown"

            async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=IssueSeverity.INFO,
                translation_key="device_paired",
                translation_placeholders={
                    "name": entity_name,
                    "type": entity_type,
                    "id": str(entity_id)
                },
            )
        else:
            _LOGGER.info("Rust+ Notification: %s - %s", title, body)

"""Event platform for Rust+ Smart Alarms.

Rust Smart Alarms are *momentary* triggers, so each one is modelled as a Home
Assistant ``event`` entity. It is driven by the server's websocket entity-change
events: every event carries the alarm's own ``entity_id``, so each entity fires
only for its own alarm — no polling, and no guessing which alarm fired from the
(entity-id-less) FCM push.

This is shipped alongside the binary_sensor so both can coexist on the same
alarm device; the event entity is the idiomatic primitive for a stateless,
momentary trigger (doorbell/button/alarm).
"""
from __future__ import annotations

import logging

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import RustPlusEntity

_LOGGER = logging.getLogger(__name__)

EVENT_TRIGGERED = "triggered"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Rust+ alarm event platform."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]

    entities_to_add = []
    paired_alarms = entry.options.get("smart_alarms", {})
    for eid, name in paired_alarms.items():
        entities_to_add.append(RustPlusSmartAlarmEvent(coordinator, int(eid), name))

    async_add_entities(entities_to_add)


class RustPlusSmartAlarmEvent(RustPlusEntity, EventEntity):
    """A Rust+ Smart Alarm modelled as a momentary event entity."""

    _attr_event_types = [EVENT_TRIGGERED]

    def __init__(self, coordinator, entity_id: int, name: str) -> None:
        """Initialize."""
        super().__init__(coordinator, entity_id, "smart_alarm", name)
        # Distinct unique_id so this can coexist with the binary_sensor on the
        # same alarm device (RustPlusEntity.__init__ built device_info from the
        # un-suffixed unique_id, so both entities attach to the same device).
        self._attr_unique_id = f"{self._attr_unique_id}_event"
        self._attr_name = "Event"  # device-relative; the device carries the alarm name
        self._last_value = False

    async def async_added_to_hass(self) -> None:
        """Subscribe to this alarm's websocket entity events."""
        await super().async_added_to_hass()
        from rustplus.identification import RegisteredListener
        from rustplus.events import EntityEventPayload

        self.hass.async_create_task(
            self.coordinator.async_subscribe_entity(self.rust_entity_id)
        )

        async def handle_event(event: EntityEventPayload):
            self.hass.async_create_task(self._async_handle_event(event.value))

        self._listener = RegisteredListener(str(self.rust_entity_id), handle_event)
        EntityEventPayload.HANDLER_LIST.register(
            self._listener, self.coordinator.socket.server_details
        )
        self.async_on_remove(self._async_remove_listener)

    async def _async_handle_event(self, value: bool) -> None:
        """Fire a momentary event on the alarm's rising edge (off -> on)."""
        triggered = bool(value) and not self._last_value
        self._last_value = bool(value)
        if triggered:
            _LOGGER.debug("Smart alarm %s event entity: triggered", self.rust_entity_id)
            self._trigger_event(EVENT_TRIGGERED)
            self.async_write_ha_state()

    @callback
    def _async_remove_listener(self):
        """Clean up the listener and release the server subscription."""
        from rustplus.events import EntityEventPayload
        EntityEventPayload.HANDLER_LIST.unregister(
            self._listener, self.coordinator.socket.server_details
        )
        self.hass.async_create_task(
            self.coordinator.async_unsubscribe_entity(self.rust_entity_id)
        )

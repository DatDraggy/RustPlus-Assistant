"""Event platform for Rust+ Smart Alarms (PROTOTYPE).

Rust Smart Alarms are *momentary* triggers: they emit a push notification at
the instant they fire and have no meaningful persistent on/off state. Home
Assistant's ``event`` entity is the idiomatic primitive for exactly this kind
of stateless trigger (doorbells, button presses, alarms), so it is a better fit
than the current ``binary_sensor`` with its hard-coded 5-second auto-reset.

This platform is intentionally shipped *alongside* the binary_sensor so the two
approaches can be compared on a live setup. Key differences vs. binary_sensor:

* It does NOT poll the server, so there is no race on the momentary ``value``
  (the binary_sensor can miss an alarm if ``value`` has already reset by the
  time it polls, or light up the wrong alarm).
* If the FCM push identifies the specific alarm that fired (see the entityId
  diagnostic in ``fcm_manager.py``), only the matching entity reacts. If it does
  not, every alarm on the server fires — same as today's broadcast behaviour,
  but still without the poll.

NOTE: a real migration would replace the binary_sensor (a breaking change for
existing automations/dashboards), not run both. This file is a prototype.
"""
from __future__ import annotations

import logging

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
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
        # Distinct unique_id so this can coexist with the binary_sensor during
        # the comparison. The device is shared: RustPlusEntity.__init__ already
        # built device_info from the un-suffixed unique_id, so both entities
        # attach to the same alarm device.
        self._attr_unique_id = f"{self._attr_unique_id}_event"
        self._attr_name = f"{name} Event"

    async def async_added_to_hass(self) -> None:
        """Subscribe to this server's alarm push signal."""
        await super().async_added_to_hass()
        ip = self.coordinator.socket.server_details.ip
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, f"rustplus_alarm_refresh_{ip}", self._handle_alarm
            )
        )

    @callback
    def _handle_alarm(self, title: str, message: str, entity_id: str | None = None) -> None:
        """Fire an event when this alarm is triggered (no polling)."""
        # If the push identifies a specific alarm, only react if it is ours.
        if entity_id is not None and str(entity_id) != str(self.rust_entity_id):
            return
        self._trigger_event(EVENT_TRIGGERED, {"title": title, "message": message})
        self.async_write_ha_state()

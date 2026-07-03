"""Binary sensor platform for Rust+."""
from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .camera import server_device_info
from .const import DOMAIN
from .coordinator import RustPlusDataCoordinator
from .entity import RustPlusEntity
from .event_cadence import MAP_EVENTS, get_event_trackers

_LOGGER = logging.getLogger(__name__)

# NOTE on "explosive type awareness": the in-game Seismic Sensor outputs power by
# explosive tier (3 rW = MLRS/Rocket/C4, 2 rW = Satchel/Expl. ammo/40mm/Torpedo/
# Mortar tier, 1 rW = grenades etc.) as a SINGLE 3-second pulse. Type can therefore
# only be distinguished in-game, by branching that wattage into up to three
# tier-specific Smart Alarms — HA just sees each alarm fire. Do NOT try to infer
# type from pulse patterns here; there is exactly one pulse per detonation.


def _to_hours(value) -> float | None:
    """Convert a Rust time value ('HH:MM' or a number) to float hours."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    try:
        if ":" in s:
            h, m = s.split(":")[:2]
            return int(h) + int(m) / 60.0
        return float(s)
    except (ValueError, TypeError):
        return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Rust+ binary sensor platform."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]

    trackers = get_event_trackers(hass, entry.entry_id)
    entities_to_add = [RustPlusDaytimeBinarySensor(coordinator)]
    for key, name, marker_type, icon in MAP_EVENTS:
        entities_to_add.append(
            RustPlusEventBinarySensor(coordinator, key, name, marker_type, icon, trackers[key])
        )

    paired_alarms = entry.options.get("smart_alarms", {})
    for eid, name in paired_alarms.items():
        entities_to_add.append(RustPlusSmartAlarm(coordinator, int(eid), name))

    async_add_entities(entities_to_add)


class RustPlusEventBinarySensor(CoordinatorEntity[RustPlusDataCoordinator], BinarySensorEntity):
    """On while a given map event (Cargo Ship, Patrol Heli, ...) is on the map."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: RustPlusDataCoordinator, key: str, name: str, marker_type: int, icon: str, tracker) -> None:
        """Initialize."""
        super().__init__(coordinator)
        sd = coordinator.socket.server_details
        self._marker_type = marker_type
        self._tracker = tracker
        self._attr_name = name
        self._attr_unique_id = f"{sd.ip}_{sd.port}_event_{key}"
        self._attr_icon = icon
        self._attr_device_info = server_device_info(coordinator)

    @property
    def is_on(self) -> bool | None:
        """Whether at least one marker of this event type is present."""
        return self._tracker.event_present((self.coordinator.data or {}).get("markers"))

    @callback
    def _handle_coordinator_update(self) -> None:
        """Record spawn rising edges before writing state."""
        self._tracker.observe((self.coordinator.data or {}).get("markers"))
        super()._handle_coordinator_update()

    @property
    def extra_state_attributes(self):
        """Surface the next-occurrence estimate alongside the live state."""
        nxt = self._tracker.next_estimate
        cadence = self._tracker.cadence
        return {
            "name": self._attr_name,
            "next_estimated": nxt.isoformat() if nxt else None,
            "cadence_minutes": round(cadence.total_seconds() / 60, 1) if cadence else None,
            "samples": self._tracker.sample_count,
        }


class RustPlusDaytimeBinarySensor(CoordinatorEntity[RustPlusDataCoordinator], BinarySensorEntity):
    """On during the in-game day (between sunrise and sunset)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: RustPlusDataCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator)
        sd = coordinator.socket.server_details
        self._attr_name = "Daytime"
        self._attr_unique_id = f"{sd.ip}_{sd.port}_daytime"
        self._attr_device_info = server_device_info(coordinator)

    @property
    def is_on(self) -> bool | None:
        """Whether it is currently daytime in-game."""
        t = (self.coordinator.data or {}).get("time")
        if t is None:
            return None
        now = _to_hours(getattr(t, "time", None))
        sunrise = _to_hours(getattr(t, "sunrise", None))
        sunset = _to_hours(getattr(t, "sunset", None))
        if now is None or sunrise is None or sunset is None:
            return None
        return sunrise <= now < sunset

    @property
    def icon(self) -> str:
        """Sun when it's day, moon when it's night."""
        return "mdi:weather-sunny" if self.is_on else "mdi:weather-night"

class RustPlusSmartAlarm(RustPlusEntity, BinarySensorEntity):
    """Representation of a Rust+ Smart Alarm.

    Driven directly by the server's websocket entity-change events (like the
    Smart Switch). Each event carries the alarm's own ``entity_id`` and current
    ``value``, so every alarm reflects exactly its own state — no polling, and
    no guessing which alarm fired from the (entity-id-less) FCM push.
    """

    _attr_device_class = BinarySensorDeviceClass.SAFETY

    def __init__(self, coordinator, entity_id: int, name: str) -> None:
        """Initialize."""
        super().__init__(coordinator, entity_id, "smart_alarm", name)
        self._attr_is_on = False

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
        """Reflect the alarm's state from a websocket entity event."""
        _LOGGER.debug("Smart alarm %s entity event: value=%s", self.rust_entity_id, value)
        self._attr_is_on = bool(value)
        self.async_write_ha_state()

    @property
    def should_poll(self) -> bool:
        """Return False; the alarm is driven by websocket events, not polling."""
        return False

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

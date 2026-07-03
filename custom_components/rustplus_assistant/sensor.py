"""Sensor platform for Rust+."""
from __future__ import annotations

import logging
import time
from datetime import timedelta

from homeassistant.components.sensor import SensorEntity, SensorStateClass, SensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.core import callback

from rustplus.utils import translate_id_to_stack

from .camera import server_device_info
from .const import DOMAIN
from .entity import RustPlusEntity
from .event_cadence import MAP_EVENTS, get_event_trackers
from .coordinator import RustPlusDataCoordinator

_LOGGER = logging.getLogger(__name__)

def _usable_entity_info(info):
    """Return ``info`` only if it's a real entity-info payload.

    During a dead-socket window a stale ``RustError`` (or None) can sit in the
    coordinator data; touching attributes on a RustError raises (and error-logs)
    via its ``__getattr__``, which crashed sensor updates on live. Treat anything
    that isn't a proper payload as "no data" and keep the last value instead.
    """
    if not info or type(info).__name__ == "RustError":
        return None
    return info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Rust+ sensor platform."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]

    entities_to_add = []

    # Add Server Sensors
    entities_to_add.append(RustPlusServerSensor(coordinator, "players", "Players Online"))
    entities_to_add.append(RustPlusServerSensor(coordinator, "queued_players", "Players Queued"))
    entities_to_add.append(RustPlusServerSensor(coordinator, "max_players", "Max Players"))

    # Server metadata (name, map, seed, wipe time, banner images, …) as a single
    # sensor — the Server Status card reads it all from here.
    entities_to_add.append(RustPlusServerInfoSensor(coordinator))

    # Add Team Sensor
    entities_to_add.append(RustPlusTeamSensor(coordinator))

    # In-game time of day
    entities_to_add.append(RustPlusTimeSensor(coordinator))

    # Last team chat message (also fires team-chat / command bus events).
    from .team import DEFAULT_COMMAND_PREFIX

    command_prefix = entry.options.get("command_prefix", DEFAULT_COMMAND_PREFIX)
    entities_to_add.append(RustPlusLastChatSensor(coordinator, command_prefix))

    # "Next occurrence" estimate per recurring map event (Cargo Ship, ...).
    trackers = get_event_trackers(hass, entry.entry_id)
    for key, name, _marker_type, icon in MAP_EVENTS:
        entities_to_add.append(
            RustPlusEventEstimateSensor(coordinator, key, name, icon, trackers[key])
        )

    # Add paired Storage Monitors
    paired_monitors = entry.options.get("storage_monitors", {})
    for eid, name in paired_monitors.items():
        entities_to_add.append(RustPlusStorageMonitor(coordinator, int(eid), name))
        
        # Add material sensors
        for material in ["Wood", "Stones", "Metal Fragments", "High Quality Metal"]:
            entities_to_add.append(RustPlusTCMaterialSensor(coordinator, int(eid), name, material))
        # Add upkeep sensor
        entities_to_add.append(RustPlusTCUpkeepSensor(coordinator, int(eid), name))

    async_add_entities(entities_to_add)

    # Per-teammate sensors, added now and as teammates appear.
    from .team import add_team_member_sensors

    add_team_member_sensors(hass, entry, coordinator, async_add_entities)

class RustPlusServerSensor(CoordinatorEntity[RustPlusDataCoordinator], SensorEntity):
    """Representation of a Rust+ Server Sensor."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: RustPlusDataCoordinator, sensor_type: str, name: str) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self.sensor_type = sensor_type
        self._attr_name = name

        server_ip = coordinator.socket.server_details.ip
        server_port = coordinator.socket.server_details.port
        self._attr_unique_id = f"{server_ip}_{server_port}_{sensor_type}"
        self._attr_native_unit_of_measurement = "players"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_device_info = server_device_info(coordinator)

    @property
    def native_value(self):
        """Return the state of the sensor."""
        if not self.coordinator.data or not self.coordinator.data.get("info"):
            return None

        info = self.coordinator.data["info"]
        return getattr(info, self.sensor_type, None)

class RustPlusServerInfoSensor(CoordinatorEntity[RustPlusDataCoordinator], SensorEntity):
    """Server metadata in one entity.

    State is the server name; the rest of the ``info`` payload (map, size, seed,
    wipe time, player counts and the Facepunch banner/logo image URLs) is exposed
    as attributes so a single card can render a full server-status banner.
    """

    _attr_icon = "mdi:server"
    _attr_has_entity_name = True

    def __init__(self, coordinator: RustPlusDataCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator)
        sd = coordinator.socket.server_details
        self._attr_name = "Server"
        self._attr_unique_id = f"{sd.ip}_{sd.port}_server_info"
        self._attr_device_info = server_device_info(coordinator)

    @property
    def native_value(self):
        """Return the server name."""
        info = (self.coordinator.data or {}).get("info")
        return getattr(info, "name", None) if info else None

    @property
    def extra_state_attributes(self):
        """Expose the full server info payload for the Server Status card."""
        info = (self.coordinator.data or {}).get("info")
        if not info:
            return {}
        return {
            "url": getattr(info, "url", None),
            "map": getattr(info, "map", None),
            "map_size": getattr(info, "size", None),
            "seed": getattr(info, "seed", None),
            "wipe_time": getattr(info, "wipe_time", None),
            "header_image": getattr(info, "header_image", None),
            "logo_image": getattr(info, "logo_image", None),
            "players": getattr(info, "players", None),
            "max_players": getattr(info, "max_players", None),
            "queued_players": getattr(info, "queued_players", None),
        }


class RustPlusTeamSensor(CoordinatorEntity[RustPlusDataCoordinator], SensorEntity):
    """Representation of a Rust+ Team Sensor."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: RustPlusDataCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_name = "Team Size"

        server_ip = coordinator.socket.server_details.ip
        server_port = coordinator.socket.server_details.port
        self._attr_unique_id = f"{server_ip}_{server_port}_team_size"
        self._attr_native_unit_of_measurement = "members"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_device_info = server_device_info(coordinator)

    @property
    def native_value(self):
        """Return the state of the sensor."""
        if not self.coordinator.data or not self.coordinator.data.get("team_info"):
            return 0

        team_info = self.coordinator.data["team_info"]
        return len(team_info.members) if team_info.members else 0

class RustPlusTimeSensor(CoordinatorEntity[RustPlusDataCoordinator], SensorEntity):
    """The in-game time of day (e.g. ``13:45``)."""

    _attr_icon = "mdi:clock-time-four-outline"
    _attr_has_entity_name = True

    def __init__(self, coordinator: RustPlusDataCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator)
        sd = coordinator.socket.server_details
        self._attr_name = "Time"
        self._attr_unique_id = f"{sd.ip}_{sd.port}_time"
        self._attr_device_info = server_device_info(coordinator)

    @property
    def native_value(self):
        """Return the in-game clock string."""
        t = (self.coordinator.data or {}).get("time")
        return getattr(t, "time", None) if t else None

    @property
    def extra_state_attributes(self):
        """Expose sunrise/sunset and the day-length scaling."""
        t = (self.coordinator.data or {}).get("time")
        if not t:
            return {}
        return {
            "sunrise": getattr(t, "sunrise", None),
            "sunset": getattr(t, "sunset", None),
            "day_length": getattr(t, "day_length", None),
            "time_scale": getattr(t, "time_scale", None),
            "raw_time": getattr(t, "raw_time", None),
        }


class RustPlusStorageMonitor(RustPlusEntity, SensorEntity):
    """Representation of a Rust+ Storage Monitor."""

    def __init__(self, coordinator, entity_id: int, name: str) -> None:
        """Initialize."""
        super().__init__(coordinator, entity_id, "storage_monitor", name)
        self._attr_native_value = None
        self._attr_native_unit_of_measurement = "items"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_extra_state_attributes = {}
        self.coordinator.entities_to_poll.add(self.rust_entity_id)

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""
        await super().async_added_to_hass()
        
        async def subscribe():
            try:
                async with self.coordinator.api_lock:
                    if not hasattr(self.coordinator.socket.ws, "open") or not self.coordinator.socket.ws.open:
                        await self.coordinator.socket.connect()
                    await self.coordinator.socket.set_subscription_to_entity(self.rust_entity_id, True)
                    info = await self.coordinator.socket.get_entity_info(self.rust_entity_id)
                    self._update_state_from_info(info)
            except Exception as e:
                _LOGGER.debug("Failed to subscribe to storage monitor %s: %s", self.rust_entity_id, e)
                
        self.hass.async_create_task(subscribe())
        
        from rustplus.identification import RegisteredListener
        from rustplus.events import EntityEventPayload
        
        async def handle_event(event: EntityEventPayload):
            self.hass.async_create_task(self._async_handle_event(event))
            
        self._listener = RegisteredListener(str(self.rust_entity_id), handle_event)
        EntityEventPayload.HANDLER_LIST.register(self._listener, self.coordinator.socket.server_details)
        
        self.async_on_remove(self._async_remove_listener)

    async def _async_handle_event(self, info) -> None:
        """Handle state change from websocket."""
        try:
            # Update coordinator data so sub-sensors get the latest info
            if "entities" not in self.coordinator.data:
                self.coordinator.data["entities"] = {}
            
            # Preserve upkeep data from previous poll if the event doesn't include it
            old_info = self.coordinator.data["entities"].get(self.rust_entity_id)
            if old_info and not getattr(info, 'has_protection', False):
                # Create a simple wrapper to carry forward protection data
                # without mutating the library's internal object
                from types import SimpleNamespace
                merged = SimpleNamespace(
                    items=info.items,
                    has_protection=getattr(old_info, 'has_protection', False),
                    protection_expiry=getattr(old_info, 'protection_expiry', 0),
                )
                info = merged

            self.coordinator.data["entities"][self.rust_entity_id] = info
            
            # This triggers all coordinator listeners (including sub-sensors)
            self.coordinator.async_set_updated_data(self.coordinator.data)
            
            self._update_state_from_info(info)
        except Exception as err:
            _LOGGER.error("Failed to parse event for storage monitor: %s", err)

    def _async_remove_listener(self):
        """Clean up listener."""
        from rustplus.events import EntityEventPayload
        EntityEventPayload.HANDLER_LIST.unregister(self._listener, self.coordinator.socket.server_details)
        
        async def unsubscribe():
            try:
                async with self.coordinator.api_lock:
                    await self.coordinator.socket.set_subscription_to_entity(self.rust_entity_id, False)
            except Exception as e:
                _LOGGER.debug("Failed to unsubscribe from storage monitor %s: %s", self.rust_entity_id, e)
        
        self.hass.async_create_task(unsubscribe())

    def _update_state_from_info(self, info):
        """Update state using info object."""
        try:
            self._attr_native_value = len(info.items)
            item_counts = self._attr_extra_state_attributes.copy() if self._attr_extra_state_attributes else {}
            
            # Reset counts for all items currently in our dictionary so we can update them accurately
            for k in list(item_counts.keys()):
                if k != "Upkeep Duration":
                    item_counts[k] = 0

            for item in info.items:
                try:
                    name = translate_id_to_stack(item.item_id)
                    item_counts[name] = item_counts.get(name, 0) + item.quantity
                except Exception:
                    pass
                    
            if getattr(info, "has_protection", False) and getattr(info, "protection_expiry", 0) > 0:

                duration_seconds = max(0, info.protection_expiry - int(time.time()))
                item_counts["Upkeep Duration"] = str(timedelta(seconds=duration_seconds))
                
            _LOGGER.debug("Storage monitor parsed attributes: %s", item_counts)
            self._attr_extra_state_attributes = item_counts
            self.async_write_ha_state()
        except Exception as err:
            _LOGGER.debug("Failed storage monitor state update: %s", err)


    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        info = _usable_entity_info(
            self.coordinator.data.get("entities", {}).get(self.rust_entity_id)
        )
        if info is not None:
            self._update_state_from_info(info)
        super()._handle_coordinator_update()

    @property
    def extra_state_attributes(self):
        """Return entity specific state attributes."""
        return self._attr_extra_state_attributes

class RustPlusTCMaterialSensor(RustPlusEntity, SensorEntity):
    """Representation of a material inside a Rust+ Storage Monitor."""

    def __init__(self, coordinator, entity_id: int, monitor_name: str, material_name: str) -> None:
        """Initialize."""
        super().__init__(coordinator, entity_id, f"storage_monitor_{material_name.lower().replace(' ', '_')}", monitor_name, device_model="Storage Monitor")
        self._attr_unique_id = f"{self._attr_unique_id}_{material_name.lower().replace(' ', '_')}"
        self._attr_name = material_name
        self.material_name = material_name
        self._attr_native_value = 0
        self._attr_native_unit_of_measurement = "items"
        self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self):
        """Return the state of the sensor."""
        info = _usable_entity_info(
            self.coordinator.data.get("entities", {}).get(self.rust_entity_id)
        )
        if info is None:
            return self._attr_native_value

        count = 0
        for item in getattr(info, "items", None) or []:
            try:
                name = translate_id_to_stack(item.item_id)
                if name == self.material_name:
                    count += item.quantity
            except Exception:
                pass

        self._attr_native_value = count
        return count

class RustPlusTCUpkeepSensor(RustPlusEntity, SensorEntity):
    """Representation of the upkeep duration of a Rust+ Storage Monitor."""

    def __init__(self, coordinator, entity_id: int, monitor_name: str) -> None:
        """Initialize."""
        super().__init__(coordinator, entity_id, "storage_monitor_upkeep", monitor_name, device_model="Storage Monitor")
        self._attr_unique_id = f"{self._attr_unique_id}_upkeep"
        self._attr_name = "Upkeep"
        self._attr_native_value = "Unknown"

    @property
    def native_value(self):
        """Return the state of the sensor."""
        info = _usable_entity_info(
            self.coordinator.data.get("entities", {}).get(self.rust_entity_id)
        )
        if info is None or not getattr(info, "has_protection", False) or getattr(info, "protection_expiry", 0) == 0:
            return self._attr_native_value

        duration_seconds = max(0, info.protection_expiry - int(time.time()))

        self._attr_native_value = str(timedelta(seconds=duration_seconds))
        return self._attr_native_value


class RustPlusEventEstimateSensor(CoordinatorEntity[RustPlusDataCoordinator], RestoreEntity, SensorEntity):
    """Estimated next spawn time for a recurring map event.

    A ``timestamp`` sensor so the frontend renders a live countdown. The estimate
    is last spawn + average cadence (see :class:`EventCadenceTracker`); the spawn
    ring is shared with the event's binary_sensor and persisted here across
    restarts. Blank until at least two spawns have been observed.
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator, key: str, name: str, icon: str, tracker) -> None:
        """Initialize."""
        super().__init__(coordinator)
        sd = coordinator.socket.server_details
        self._tracker = tracker
        self._attr_name = f"{name} Next"
        self._attr_unique_id = f"{sd.ip}_{sd.port}_event_{key}_next"
        self._attr_icon = icon
        self._attr_device_info = server_device_info(coordinator)

    async def async_added_to_hass(self) -> None:
        """Restore the persisted spawn ring so cadence survives a restart."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None:
            self._tracker.restore(last.attributes.get("spawn_history"))

    @property
    def native_value(self):
        """Projected next spawn (None until cadence is known)."""
        return self._tracker.next_estimate

    @callback
    def _handle_coordinator_update(self) -> None:
        """Record spawn rising edges before writing state."""
        self._tracker.observe((self.coordinator.data or {}).get("markers"))
        super()._handle_coordinator_update()

    @property
    def extra_state_attributes(self):
        """Persist the spawn ring + expose the cadence used for the estimate."""
        cadence = self._tracker.cadence
        last = self._tracker.last_spawn
        return {
            "spawn_history": self._tracker.serialize(),
            "cadence_minutes": round(cadence.total_seconds() / 60, 1) if cadence else None,
            "samples": self._tracker.sample_count,
            "last_spawn": last.isoformat() if last else None,
        }


def parse_command(text: str, prefix: str):
    """Split a chat message into (command, args) if it starts with ``prefix``.

    Returns ``(None, None)`` for a normal (non-command) message.
    """
    if not text or not prefix or not text.startswith(prefix):
        return None, None
    body = text[len(prefix):].strip()
    parts = body.split()
    return (parts[0].lower() if parts else ""), parts[1:]


class RustPlusLastChatSensor(CoordinatorEntity[RustPlusDataCoordinator], SensorEntity):
    """The most recent team-chat message.

    State is the message text; the sender and metadata are attributes. Each message
    also fires ``rustplus_team_chat`` on the HA bus, and messages starting with the
    command prefix (default ``!``) additionally fire ``rustplus_command`` with the
    parsed command + args, so users can drive automations from in-game chat.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:chat"

    def __init__(self, coordinator, command_prefix: str) -> None:
        """Initialize."""
        super().__init__(coordinator)
        sd = coordinator.socket.server_details
        self._command_prefix = command_prefix or "!"
        self._attr_name = "Last Team Message"
        self._attr_unique_id = f"{sd.ip}_{sd.port}_last_chat"
        self._attr_device_info = server_device_info(coordinator)
        self._attr_native_value = None
        self._attr_extra_state_attributes = {}

    async def async_added_to_hass(self) -> None:
        """Subscribe to team-chat websocket events."""
        await super().async_added_to_hass()
        from rustplus.identification import RegisteredListener
        from rustplus.events import ChatEventPayload

        async def handle_chat(event):
            self.hass.async_create_task(self._async_handle_chat(event.message))

        self._listener = RegisteredListener("ha_team_chat", handle_chat)
        ChatEventPayload.HANDLER_LIST.register(
            self._listener, self.coordinator.socket.server_details
        )
        self.async_on_remove(self._async_remove_listener)

    async def _async_handle_chat(self, msg) -> None:
        """Update state and fire chat / command bus events."""
        from .team import CHAT_EVENT, COMMAND_EVENT

        text = getattr(msg, "message", "") or ""
        sender = getattr(msg, "name", None)
        steam_id = getattr(msg, "steam_id", None)
        colour = getattr(msg, "colour", None)
        ts = getattr(msg, "time", None)
        command, args = parse_command(text, self._command_prefix)

        self._attr_native_value = text[:255]
        self._attr_extra_state_attributes = {
            "sender_name": sender,
            "sender_steam_id": steam_id,
            "colour": colour,
            "time": ts,
            "is_command": command is not None,
            "command": command,
            "args": args,
        }
        self.async_write_ha_state()

        sd = self.coordinator.socket.server_details
        payload = {
            "server": f"{sd.ip}:{sd.port}",
            "sender_name": sender,
            "sender_steam_id": steam_id,
            "message": text,
            "colour": colour,
            "time": ts,
        }
        self.hass.bus.async_fire(CHAT_EVENT, payload)
        if command is not None:
            self.hass.bus.async_fire(COMMAND_EVENT, {**payload, "command": command, "args": args})

    @callback
    def _async_remove_listener(self):
        """Unregister the chat listener."""
        from rustplus.events import ChatEventPayload
        ChatEventPayload.HANDLER_LIST.unregister(
            self._listener, self.coordinator.socket.server_details
        )

    @property
    def native_value(self):
        return self._attr_native_value

    @property
    def extra_state_attributes(self):
        return self._attr_extra_state_attributes



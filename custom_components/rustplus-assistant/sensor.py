"""Sensor platform for Rust+."""
from __future__ import annotations

import logging
import time
from datetime import timedelta

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.core import callback

from .const import DOMAIN
from .entity import RustPlusEntity
from .coordinator import RustPlusDataCoordinator

_LOGGER = logging.getLogger(__name__)

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

    # Add Team Sensor
    entities_to_add.append(RustPlusTeamSensor(coordinator))

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

class RustPlusServerSensor(CoordinatorEntity[RustPlusDataCoordinator], SensorEntity):
    """Representation of a Rust+ Server Sensor."""

    def __init__(self, coordinator: RustPlusDataCoordinator, sensor_type: str, name: str) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self.sensor_type = sensor_type
        self._attr_name = f"Rust+ {name}"

        server_ip = coordinator.socket.server_details.ip
        server_port = coordinator.socket.server_details.port
        self._attr_unique_id = f"{server_ip}_{server_port}_{sensor_type}"
        self._attr_native_unit_of_measurement = "players"

    @property
    def native_value(self):
        """Return the state of the sensor."""
        if not self.coordinator.data or not self.coordinator.data.get("info"):
            return None

        info = self.coordinator.data["info"]
        return getattr(info, self.sensor_type, None)

class RustPlusTeamSensor(CoordinatorEntity[RustPlusDataCoordinator], SensorEntity):
    """Representation of a Rust+ Team Sensor."""

    def __init__(self, coordinator: RustPlusDataCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_name = "Rust+ Team Size"

        server_ip = coordinator.socket.server_details.ip
        server_port = coordinator.socket.server_details.port
        self._attr_unique_id = f"{server_ip}_{server_port}_team_size"
        self._attr_native_unit_of_measurement = "members"

    @property
    def native_value(self):
        """Return the state of the sensor."""
        if not self.coordinator.data or not self.coordinator.data.get("team_info"):
            return 0

        team_info = self.coordinator.data["team_info"]
        return len(team_info.members) if team_info.members else 0

class RustPlusStorageMonitor(RustPlusEntity, SensorEntity):
    """Representation of a Rust+ Storage Monitor."""

    def __init__(self, coordinator, entity_id: int, name: str) -> None:
        """Initialize."""
        super().__init__(coordinator, entity_id, "storage_monitor", name)
        self._attr_native_value = None
        self._attr_native_unit_of_measurement = "items"
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
            from rustplus.utils import translate_id_to_stack
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
                    
            if info.has_protection and info.protection_expiry > 0:

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
        info = self.coordinator.data.get("entities", {}).get(self.rust_entity_id)
        if info:
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
        super().__init__(coordinator, entity_id, f"storage_monitor_{material_name.lower().replace(' ', '_')}", f"{monitor_name} {material_name}")
        self._attr_unique_id = f"{self._attr_unique_id}_{material_name.lower().replace(' ', '_')}"
        self.material_name = material_name
        self._attr_native_value = 0
        self._attr_native_unit_of_measurement = ""

    @property
    def native_value(self):
        """Return the state of the sensor."""
        info = self.coordinator.data.get("entities", {}).get(self.rust_entity_id)
        if not info:
            return self._attr_native_value

        from rustplus.utils import translate_id_to_stack
        count = 0
        for item in info.items:
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
        super().__init__(coordinator, entity_id, "storage_monitor_upkeep", f"{monitor_name} Upkeep")
        self._attr_unique_id = f"{self._attr_unique_id}_upkeep"
        self._attr_native_value = "Unknown"

    @property
    def native_value(self):
        """Return the state of the sensor."""
        info = self.coordinator.data.get("entities", {}).get(self.rust_entity_id)
        if not info or not info.has_protection or info.protection_expiry == 0:
            return self._attr_native_value


        duration_seconds = max(0, info.protection_expiry - int(time.time()))
        
        self._attr_native_value = str(timedelta(seconds=duration_seconds))
        return self._attr_native_value



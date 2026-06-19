"""Sensor platform for Rust+."""
from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

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

    async def async_update(self) -> None:
        """Update the entity."""
        try:
            info = await self.coordinator.socket.get_entity_info(self.rust_entity_id)
            self._attr_native_value = info.capacity
        except Exception as err:
            _LOGGER.error("Failed to update storage monitor state: %s", err)

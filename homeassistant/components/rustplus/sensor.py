"""Sensor platform for Rust+."""
from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import RustPlusEntity

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
    paired_monitors = entry.options.get("storage_monitors", {})
    for eid, name in paired_monitors.items():
        entities_to_add.append(RustPlusStorageMonitor(coordinator, int(eid), name))

    async_add_entities(entities_to_add)

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

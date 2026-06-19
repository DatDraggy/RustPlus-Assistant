"""Binary sensor platform for Rust+."""
from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorDeviceClass
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
    """Set up Rust+ binary sensor platform."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]

    entities_to_add = []
    paired_alarms = entry.options.get("smart_alarms", {})
    for eid, name in paired_alarms.items():
        entities_to_add.append(RustPlusSmartAlarm(coordinator, int(eid), name))

    async_add_entities(entities_to_add)

class RustPlusSmartAlarm(RustPlusEntity, BinarySensorEntity):
    """Representation of a Rust+ Smart Alarm."""

    def __init__(self, coordinator, entity_id: int, name: str) -> None:
        """Initialize."""
        super().__init__(coordinator, entity_id, "smart_alarm", name)
        self._attr_is_on = False
        self._attr_device_class = BinarySensorDeviceClass.SAFETY

    async def async_update(self) -> None:
        """Update the entity."""
        try:
            info = await self.coordinator.socket.get_entity_info(self.rust_entity_id)
            self._attr_is_on = info.value
        except Exception as err:
            _LOGGER.error("Failed to update smart alarm state: %s", err)

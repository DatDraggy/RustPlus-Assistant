"""Switch platform for Rust+."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
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
    """Set up Rust+ switch platform."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]
    socket = data["socket"]

    # Retrieve all paired Smart Switches
    try:
        entities = await socket.get_entity_info() # Normally rustplus API requires specific ID. We'll check if get_markers or server has list
    except Exception as err:
        _LOGGER.error("Failed to query initial switches: %s", err)

    # Since rustplus library requires exact EID to query state, we'll initially load
    # an empty list until devices are paired via FCM, but we can provide a mechanism
    # to load from config options if the user knows their IDs.

    entities_to_add = []

    # We load paired devices from the config entry options (if saved by config flow/options flow)
    paired_switches = entry.options.get("switches", {})
    for eid, name in paired_switches.items():
        entities_to_add.append(RustPlusSmartSwitch(coordinator, int(eid), name))

    async_add_entities(entities_to_add)

class RustPlusSmartSwitch(RustPlusEntity, SwitchEntity):
    """Representation of a Rust+ Smart Switch."""

    def __init__(self, coordinator, entity_id: int, name: str) -> None:
        """Initialize."""
        super().__init__(coordinator, entity_id, "switch", name)
        self._attr_is_on = False

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        await self.coordinator.socket.turn_smart_switch_on(self.rust_entity_id)
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        await self.coordinator.socket.turn_smart_switch_off(self.rust_entity_id)
        self._attr_is_on = False
        self.async_write_ha_state()

    async def async_update(self) -> None:
        """Update the entity."""
        try:
            info = await self.coordinator.socket.get_entity_info(self.rust_entity_id)
            self._attr_is_on = info.value
        except Exception as err:
            _LOGGER.error("Failed to update switch state: %s", err)

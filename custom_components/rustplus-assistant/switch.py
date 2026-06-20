"""Switch platform for Rust+."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
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
        await self.coordinator.socket.set_entity_value(self.rust_entity_id, True)
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        await self.coordinator.socket.set_entity_value(self.rust_entity_id, False)
        self._attr_is_on = False
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""
        async def subscribe():
            try:
                async with self.coordinator.api_lock:
                    if not hasattr(self.coordinator.socket.ws, "open") or not self.coordinator.socket.ws.open:
                        await self.coordinator.socket.connect()
                    await self.coordinator.socket.set_subscription_to_entity(self.rust_entity_id, True)
            except Exception as e:
                _LOGGER.debug("Failed to subscribe to switch %s: %s", self.rust_entity_id, e)
                
        self.hass.async_create_task(subscribe())
        
        from rustplus.identification import RegisteredListener
        from rustplus.events import EntityEventPayload
        
        async def handle_event(event: EntityEventPayload):
            self.hass.async_create_task(self._async_handle_event(event.value))
            
        self._listener = RegisteredListener(str(self.rust_entity_id), handle_event)
        EntityEventPayload.HANDLER_LIST.register(self._listener, self.coordinator.socket.server_details)
        
        self.async_on_remove(self._async_remove_listener)

    async def _async_handle_event(self, value: bool) -> None:
        """Handle state change from websocket."""
        self._attr_is_on = value
        self.async_write_ha_state()

    @callback
    def _async_remove_listener(self):
        """Clean up listener."""
        from rustplus.events import EntityEventPayload
        EntityEventPayload.HANDLER_LIST.unregister(self._listener, self.coordinator.socket.server_details)
        
        async def unsubscribe():
            try:
                async with self.coordinator.api_lock:
                    await self.coordinator.socket.set_subscription_to_entity(self.rust_entity_id, False)
            except Exception as e:
                _LOGGER.debug("Failed to unsubscribe from switch %s: %s", self.rust_entity_id, e)
        
        self.hass.async_create_task(unsubscribe())



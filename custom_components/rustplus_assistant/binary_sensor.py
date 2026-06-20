"""Binary sensor platform for Rust+."""
from __future__ import annotations

import asyncio
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
        self._reset_task: asyncio.Task | None = None

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""
        await super().async_added_to_hass()
        from homeassistant.helpers.dispatcher import async_dispatcher_connect
        
        ip = self.coordinator.socket.server_details.ip
        self.async_on_remove(
            async_dispatcher_connect(self.hass, f"rustplus_alarm_refresh_{ip}", self._async_force_refresh)
        )

    async def _async_force_refresh(self, title: str, message: str, entity_id: str = None) -> None:
        """Force a state refresh by polling the server."""
        try:
            info = await self.coordinator.socket.get_entity_info(self.rust_entity_id)
            if info and hasattr(info, 'value') and info.value:
                # Cancel any pending reset from a previous alarm
                if self._reset_task and not self._reset_task.done():
                    self._reset_task.cancel()

                self._attr_is_on = True
                self.async_write_ha_state()

                async def reset_alarm():
                    await asyncio.sleep(5)
                    self._attr_is_on = False
                    self.async_write_ha_state()

                self._reset_task = self.hass.async_create_task(reset_alarm())
        except Exception as e:
            _LOGGER.error("Failed to poll smart alarm state: %s", e)

    @property
    def should_poll(self) -> bool:
        """Return False, as Smart Alarms cannot be polled."""
        return False

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

    # Paired devices are loaded from the config entry options (populated by
    # auto-discovery when a device is paired in the Rust+ app).
    paired_switches = entry.options.get("switches", {})
    for eid, name in paired_switches.items():
        entities_to_add.append(RustPlusSmartSwitch(coordinator, int(eid), name))

    # One "Control" switch per turret camera — taking control holds the camera
    # subscription (which disables the turret's auto-aim), so it's opt-in.
    session = data.get("camera_session")
    if session is not None:
        from .camera import CONTROLLABLE_TYPES, camera_device_info, camera_type

        for cam_id, meta in (entry.options.get("cameras") or {}).items():
            meta = meta if isinstance(meta, dict) else {}
            if camera_type(meta) in CONTROLLABLE_TYPES:
                cam_name = meta.get("name") or cam_id
                entities_to_add.append(
                    RustPlusTurretControlSwitch(
                        coordinator, session, cam_id, cam_name,
                        camera_device_info(coordinator, cam_id, meta),
                    )
                )

    async_add_entities(entities_to_add)


class RustPlusTurretControlSwitch(SwitchEntity):
    """Takes manual control of a controllable camera (turret/ptz).

    On = subscribed/controlled (live feed + aim/fire buttons work); off = released.
    For a turret, control disables its auto-aim, so this is opt-in. State is
    intentionally not restored across restarts — after a restart the camera is left
    alone (a turret auto-aims) until you take control again.
    """

    _attr_icon = "mdi:remote"

    def __init__(self, coordinator, session, cam_id: str, cam_name: str, device_info=None) -> None:
        """Initialize."""
        self._session = session
        self._cam_id = cam_id
        server_ip = coordinator.socket.server_details.ip
        server_port = coordinator.socket.server_details.port
        self._attr_name = f"Rust+ {cam_name} Control"
        self._attr_unique_id = f"{server_ip}_{server_port}_cam_{cam_id}_control"
        self._attr_device_info = device_info

    @property
    def is_on(self) -> bool:
        """Whether the turret is currently under active control."""
        return self._session.is_active(self._cam_id)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Take control of the turret (holds the subscription open)."""
        await self._session.activate(self._cam_id)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Release control so the turret resumes auto-aim."""
        await self._session.deactivate(self._cam_id)
        self.async_write_ha_state()

class RustPlusSmartSwitch(RustPlusEntity, SwitchEntity):
    """Representation of a Rust+ Smart Switch.

    Driven by the server's websocket entity-change events; the coordinator owns
    the (ref-counted) server subscription so the switch is re-subscribed after a
    reconnect along with the alarms.
    """

    def __init__(self, coordinator, entity_id: int, name: str) -> None:
        """Initialize."""
        super().__init__(coordinator, entity_id, "switch", name)
        self._attr_is_on = False

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        async with self.coordinator.api_lock:
            await self.coordinator.socket.set_entity_value(self.rust_entity_id, True)
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        async with self.coordinator.api_lock:
            await self.coordinator.socket.set_entity_value(self.rust_entity_id, False)
        self._attr_is_on = False
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Subscribe to this switch's websocket entity events."""
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
        """Handle state change from a websocket entity event."""
        _LOGGER.debug("Smart switch %s entity event: value=%s", self.rust_entity_id, value)
        self._attr_is_on = value
        self.async_write_ha_state()

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

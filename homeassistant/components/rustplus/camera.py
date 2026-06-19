"""Camera platform for Rust+ to display the map."""
from __future__ import annotations

import logging

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import RustPlusDataCoordinator

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Rust+ camera platform."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]

    async_add_entities([RustPlusMapCamera(coordinator)])

class RustPlusMapCamera(Camera):
    """Representation of the Rust+ server map as a camera."""

    def __init__(self, coordinator: RustPlusDataCoordinator) -> None:
        """Initialize."""
        super().__init__()
        self.coordinator = coordinator

        server_ip = coordinator.socket.server_details.ip
        server_port = coordinator.socket.server_details.port
        self._attr_name = "Rust+ Map"
        self._attr_unique_id = f"{server_ip}_{server_port}_map"
        self._attr_is_on = True

        self._last_image: bytes | None = None

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return image response."""
        try:
            # get_map() returns a RustMap object which has jpg_image property
            rust_map = await self.coordinator.socket.get_map()
            if rust_map and hasattr(rust_map, "jpg_image"):
                self._last_image = rust_map.jpg_image
            return self._last_image
        except Exception as err:
            _LOGGER.error("Failed to fetch map image: %s", err)
            return self._last_image

    @property
    def frame_interval(self) -> float:
        """Return the interval between frames of the mjpeg stream."""
        # The map doesn't change frequently, so we set a slow polling rate.
        return 60.0

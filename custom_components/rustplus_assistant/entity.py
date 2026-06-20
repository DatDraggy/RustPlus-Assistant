"""Base entity for Rust+."""
from __future__ import annotations

from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import RustPlusDataCoordinator

class RustPlusEntity(CoordinatorEntity[RustPlusDataCoordinator]):
    """Defines a base Rust+ entity."""

    def __init__(self, coordinator: RustPlusDataCoordinator, entity_id: int, entity_type: str, name: str) -> None:
        """Initialize the Rust+ entity."""
        super().__init__(coordinator)
        self.rust_entity_id = entity_id
        self.entity_type = entity_type
        self._attr_name = name

        server_ip = coordinator.socket.server_details.ip
        server_port = coordinator.socket.server_details.port

        self._attr_unique_id = f"{server_ip}_{server_port}_{entity_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._attr_unique_id)},
            name=name,
            manufacturer="Facepunch",
            model=entity_type.capitalize(),
            via_device=(DOMAIN, f"{server_ip}_{server_port}")
        )

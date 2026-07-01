"""Base entity for Rust+."""
from __future__ import annotations

from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .camera import server_label
from .const import DOMAIN
from .coordinator import RustPlusDataCoordinator


class RustPlusEntity(CoordinatorEntity[RustPlusDataCoordinator]):
    """Base Rust+ entity for a paired in-game device (switch / alarm / storage).

    Each paired device is its own HA device nested under the per-server hub.
    ``name`` is the in-game device name; the HA device is named
    ``"{server_label} {name}"`` so entity_ids are server-scoped. Entities use
    ``has_entity_name``: a single-entity device leaves ``_attr_name`` None (the
    entity inherits the device name), while sub-entities (e.g. storage materials)
    set a device-relative ``_attr_name`` after ``super().__init__``.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: RustPlusDataCoordinator,
        entity_id: int,
        entity_type: str,
        name: str,
        device_model: str | None = None,
    ) -> None:
        """Initialize the Rust+ entity."""
        super().__init__(coordinator)
        self.rust_entity_id = entity_id
        self.entity_type = entity_type
        self._attr_name = None

        # Monitor every paired in-game entity so the coordinator can detect when it
        # is destroyed in-game (get_entity_info -> not_found) and flag it.
        coordinator.entities_to_poll.add(entity_id)

        server_ip = coordinator.socket.server_details.ip
        server_port = coordinator.socket.server_details.port

        self._attr_unique_id = f"{server_ip}_{server_port}_{entity_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._attr_unique_id)},
            name=f"{server_label(coordinator)} {name}",
            manufacturer="Facepunch",
            model=device_model or entity_type.replace("_", " ").title(),
            via_device=(DOMAIN, f"{server_ip}_{server_port}"),
        )

    @property
    def available(self) -> bool:
        """Unavailable when the coordinator is down or the device was destroyed."""
        return super().available and not self.coordinator.is_destroyed(self.rust_entity_id)

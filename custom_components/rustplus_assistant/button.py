"""Button platform for Rust+ — camera aim / turret fire controls.

Controllable cameras (turret, ptz) get directional aim buttons; turrets also get
a fire button. Aiming is a one-shot mouse-delta nudge per press; firing sends a
press then a release so it's a discrete shot. All inputs go through the shared,
isolated camera session and only act while the camera is under active control.
"""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from rustplus import MovementControls, Vector

from .camera import (
    CONTROLLABLE_TYPES,
    FIRE_TYPES,
    camera_device_info,
    camera_type,
)
from .const import DOMAIN
from .coordinator import RustPlusDataCoordinator

_LOGGER = logging.getLogger(__name__)

# --- Aim calibration -------------------------------------------------------- #
# One aim-button press turns the camera by this many degrees, so a full
# revolution is 360 / 11.25 = 32 clicks — the on-hardware acceptance test.
_AIM_DEGREES_PER_CLICK = 11.25

# rustplus sends the joystick vector straight through as a raw server-side
# mouse_delta — there is NO degrees conversion in the library; the server applies
# the rotation. So degrees only become real once this factor is calibrated: raw
# mouse-delta units per degree of rotation. CALIBRATE ON HARDWARE — take Control
# of a turret, tap "Aim Right" 32 times and tune this until that is exactly one
# 360° turn (raise it if a click turns too little, lower it if too much).
_MOUSE_DELTA_PER_DEGREE = 0.30

# Mouse-delta magnitude applied per aim press (degrees -> raw delta).
_AIM_STEP = _AIM_DEGREES_PER_CLICK * _MOUSE_DELTA_PER_DEGREE

# Pitch (up/down) sign. If "Aim Up"/"Aim Down" come out inverted on your
# hardware, set this to -1 (yaw / left-right is unaffected).
_PITCH_SIGN = 1


def _controls(can_fire: bool = True) -> list[tuple]:
    """(key, label, icon, buttons, joystick, release_after) for each control."""
    pitch = _AIM_STEP * _PITCH_SIGN
    specs = [
        ("aim_left",  "Aim Left",  "mdi:arrow-left-bold-outline",  None, Vector(x=-_AIM_STEP, y=0), False),
        ("aim_right", "Aim Right", "mdi:arrow-right-bold-outline", None, Vector(x=_AIM_STEP, y=0),  False),
        ("aim_up",    "Aim Up",    "mdi:arrow-up-bold-outline",    None, Vector(x=0, y=pitch),  False),
        ("aim_down",  "Aim Down",  "mdi:arrow-down-bold-outline",  None, Vector(x=0, y=-pitch), False),
    ]
    if can_fire:
        specs.append(("fire", "Fire", "mdi:pistol", [MovementControls.FIRE_PRIMARY], None, True))
    return specs


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up camera control buttons (controllable cameras only)."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]
    session = data["camera_session"]

    entities: list[ButtonEntity] = []
    for cam_id, meta in (entry.options.get("cameras") or {}).items():
        meta = meta if isinstance(meta, dict) else {}
        ctype = camera_type(meta)
        if ctype not in CONTROLLABLE_TYPES:
            continue
        cam_name = meta.get("name") or cam_id
        device_info = camera_device_info(coordinator, cam_id, meta)
        for spec in _controls(can_fire=ctype in FIRE_TYPES):
            entities.append(
                RustPlusTurretButton(coordinator, session, cam_id, cam_name, spec, device_info)
            )
    async_add_entities(entities)


class RustPlusTurretButton(ButtonEntity):
    """A single aim/fire control for a Rust+ controllable camera."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: RustPlusDataCoordinator, session, cam_id, cam_name, spec, device_info=None) -> None:
        """Initialize."""
        key, label, icon, buttons, joystick, release_after = spec
        self._session = session
        self._cam_id = cam_id
        self._buttons = buttons
        self._joystick = joystick
        self._release_after = release_after

        server_ip = coordinator.socket.server_details.ip
        server_port = coordinator.socket.server_details.port
        self._attr_name = label  # device-relative ("Aim Left", "Fire", ...)
        self._attr_unique_id = f"{server_ip}_{server_port}_cam_{cam_id}_{key}"
        self._attr_icon = icon
        self._attr_device_info = device_info

    async def async_press(self) -> None:
        """Send the aim/fire input to the camera via the session."""
        try:
            await self._session.send_movement(
                self._cam_id,
                buttons=self._buttons,
                joystick=self._joystick,
                release_after=self._release_after,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Camera control '%s' failed for '%s': %s",
                self._attr_name, self._cam_id, err,
            )

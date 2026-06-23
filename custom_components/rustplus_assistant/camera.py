"""Camera platform for Rust+ (server map + CCTV / turret camera feeds)."""
from __future__ import annotations

import io
import logging
import time

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .camera_session import CameraUnavailable, RustPlusCameraSession
from .const import DOMAIN
from .coordinator import RustPlusDataCoordinator

_LOGGER = logging.getLogger(__name__)

# The camera session holds its subscription open between calls, so this just
# bounds how often a single entity asks for a fresh ~2s-accumulated frame.
_MIN_REFRESH_SECONDS = 5.0

# The annotated map (get_map) fetches markers + team + Steam avatars and composites
# them, so it's heavy — only re-render it this often.
_ANNOTATED_MAP_MIN_REFRESH = 60.0

# Camera capability model:
#   cctv   — view only
#   ptz    — view + aim (Control switch + aim buttons), no fire
#   turret — view + aim + fire (Control switch protects the turret's auto-aim)
CONTROLLABLE_TYPES = frozenset({"turret", "ptz"})
FIRE_TYPES = frozenset({"turret"})
_ICONS = {"turret": "mdi:crosshairs-gps", "ptz": "mdi:cctv", "cctv": "mdi:cctv"}
_MODELS = {"turret": "Auto Turret", "ptz": "PTZ Camera", "cctv": "CCTV Camera"}


def camera_type(meta) -> str:
    """Resolve a camera's type, tolerating the legacy ``is_turret`` field."""
    meta = meta if isinstance(meta, dict) else {}
    t = meta.get("type")
    if t in ("turret", "ptz", "cctv"):
        return t
    return "turret" if meta.get("is_turret") else "cctv"


def server_device_info(coordinator: RustPlusDataCoordinator) -> DeviceInfo:
    """DeviceInfo for the per-server hub device."""
    sd = coordinator.socket.server_details
    return DeviceInfo(identifiers={(DOMAIN, f"{sd.ip}_{sd.port}")})


def camera_device_info(coordinator: RustPlusDataCoordinator, cam_id: str, meta) -> DeviceInfo:
    """DeviceInfo grouping a camera's feed + control entities into one device."""
    sd = coordinator.socket.server_details
    name = (meta or {}).get("name") or cam_id
    return DeviceInfo(
        identifiers={(DOMAIN, f"{sd.ip}_{sd.port}_cam_{cam_id}")},
        name=f"Rust+ {name}",
        manufacturer="Facepunch",
        model=_MODELS.get(camera_type(meta), "Camera"),
        via_device=(DOMAIN, f"{sd.ip}_{sd.port}"),
    )


def _render_annotated_map(server_details) -> bytes | None:
    """Render the overlaid map (runs in an executor thread).

    ``get_map`` does blocking file/SSL/HTTP I/O (icon files, Steam avatars), so it
    must stay off the event loop. We give it its own short-lived socket so it never
    touches the data socket either.
    """
    import asyncio

    from rustplus import RustSocket

    async def _go():
        socket = RustSocket(server_details)
        await socket.connect()
        try:
            return await socket.get_map(
                add_icons=True,
                add_events=True,
                add_vending_machines=True,
                add_team_positions=True,
                add_grid=True,
            )
        finally:
            try:
                await socket.disconnect()
            except Exception:  # noqa: BLE001
                pass

    img = asyncio.run(_go())
    if img is None or type(img).__name__ == "RustError":
        _LOGGER.warning("Annotated map render returned no image: %s", img)
        return None
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG")
    return buf.getvalue()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Rust+ camera platform."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]
    session = data["camera_session"]

    entities: list[Camera] = [
        RustPlusMapCamera(coordinator),
        RustPlusAnnotatedMapCamera(coordinator),
    ]
    for cam_id, meta in (entry.options.get("cameras") or {}).items():
        entities.append(RustPlusSubscribedCamera(coordinator, session, cam_id, meta))
    async_add_entities(entities)

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
        self._attr_device_info = server_device_info(coordinator)

        self._last_image: bytes | None = None

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return image response."""
        try:
            # get_map_info() returns a RustMap object which has jpg_image property.
            # Serialize with the coordinator's polling on the shared websocket.
            async with self.coordinator.api_lock:
                rust_map = await self.coordinator.socket.get_map_info()
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


class RustPlusAnnotatedMapCamera(Camera):
    """The server map with monuments, live events, vending machines, the grid and
    teammates drawn on by the library (`get_map`). Heavier than the plain map, so
    it renders at most once a minute."""

    def __init__(self, coordinator: RustPlusDataCoordinator) -> None:
        """Initialize."""
        super().__init__()
        self.coordinator = coordinator
        sd = coordinator.socket.server_details
        self._attr_name = "Rust+ Map (Events)"
        self._attr_unique_id = f"{sd.ip}_{sd.port}_map_annotated"
        self._attr_icon = "mdi:map-marker-radius"
        self._attr_device_info = server_device_info(coordinator)
        self._last_image: bytes | None = None
        self._last_fetch: float = 0.0

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Render the map with all overlays (cached for a minute).

        ``get_map`` does blocking file/SSL/HTTP work (icon files, Steam avatars),
        so it can't run on the event loop — render it in an executor on its own
        short-lived socket.
        """
        now = time.monotonic()
        if self._last_image is not None and (now - self._last_fetch) < _ANNOTATED_MAP_MIN_REFRESH:
            return self._last_image
        try:
            jpeg = await self.hass.async_add_executor_job(
                _render_annotated_map, self.coordinator.socket.server_details
            )
            if jpeg is not None:
                self._last_image = jpeg
                self._last_fetch = now
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Failed to fetch annotated map: %s", err)
        return self._last_image

    @property
    def frame_interval(self) -> float:
        """Slow refresh — the render is expensive."""
        return _ANNOTATED_MAP_MIN_REFRESH


class RustPlusSubscribedCamera(Camera):
    """A Rust+ CCTV camera or Auto Turret camera, served from the camera session.

    Frames come from the dedicated, isolated camera socket (see
    :class:`RustPlusCameraSession`) so viewing a camera can never destabilize the
    data socket / map / poll. The session keeps the subscription open between
    calls and lets ray samples accumulate, so the image isn't pixelated.
    """

    def __init__(
        self,
        coordinator: RustPlusDataCoordinator,
        session: RustPlusCameraSession,
        cam_id: str,
        meta,
    ) -> None:
        """Initialize."""
        super().__init__()
        self._session = session
        self._cam_id = cam_id

        meta = meta if isinstance(meta, dict) else {}
        ctype = camera_type(meta)
        self._controllable = ctype in CONTROLLABLE_TYPES
        friendly = meta.get("name") or cam_id

        server_ip = coordinator.socket.server_details.ip
        server_port = coordinator.socket.server_details.port
        self._attr_name = f"Rust+ {friendly}"
        self._attr_unique_id = f"{server_ip}_{server_port}_cam_{cam_id}"
        self._attr_icon = _ICONS.get(ctype, "mdi:cctv")
        self._attr_device_info = camera_device_info(coordinator, cam_id, meta)
        self._last_image: bytes | None = None
        self._last_fetch: float = 0.0

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a freshly-accumulated JPEG from the camera session."""
        now = time.monotonic()
        # Throttle every attempt — including failures — so a frequently-refreshing
        # frontend (or an unreachable camera) doesn't spin the session.
        if (now - self._last_fetch) < _MIN_REFRESH_SECONDS:
            return self._last_image
        # A controllable camera (turret/ptz) must NOT be subscribed unless the user
        # has taken control via its Control switch — for a turret that would disable
        # its auto-aim. Show the last frame until then.
        if self._controllable and not self._session.is_active(self._cam_id):
            return self._last_image
        self._last_fetch = now

        try:
            image = await self._session.snapshot(self._cam_id)
            if image is not None:
                self._last_image = image
        except CameraUnavailable as err:
            _LOGGER.warning("Camera '%s' unavailable: %s", self._cam_id, err)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Failed to fetch camera '%s' frame: %s", self._cam_id, err)
        return self._last_image

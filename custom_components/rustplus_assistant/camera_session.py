"""A dedicated Rust+ websocket used only for camera streaming/control.

Camera subscriptions don't coexist with the entity-event subscriptions on the
main data socket: a sustained camera subscription makes the server drop the whole
connection (taking the map and polling down with it). So cameras run on their own
isolated ``RustSocket`` — if a camera ever drops *its* connection, the data socket
is untouched.

Only one camera can be active per socket (``CameraManager.ACTIVE_INSTANCE``), so
every camera on a server shares this one session, serialized by a lock. The
subscription is kept open for a short idle window so rapid frame refreshes and
control inputs reuse it instead of re-subscribing each time.

Frames are rendered only on demand per snapshot request, and a held turret is kept
live with a low-rate (~2/s) re-subscribe. Both are cheap and loop-safe; the things
that previously hung HA's loop were a high-rate input pump and rendering on every
incoming packet — neither is used here.
"""
from __future__ import annotations

import asyncio
import io
import logging
import time

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# How long to let ray-sample packets accumulate before taking a frame (the image
# de-noises as samples build up — one packet alone is heavily pixelated). Used for
# a passive/CCTV view, where we want a clean first frame.
_ACCUMULATE_SECONDS = 2.0
_FRAME_POLL_INTERVAL = 0.2
# Keep the camera subscribed this long after the last use so repeat views and
# control inputs reuse it instead of re-subscribing each time.
_IDLE_EXIT_SECONDS = 20.0
# While a held turret is being viewed, re-subscribe at most this often to keep its
# stream live (aiming otherwise stalls it). Driven by the human-paced viewing poll,
# so ~1-2 Hz — cheap and loop-safe.
_LIVE_RESUBSCRIBE_INTERVAL = 0.5


class CameraUnavailable(Exception):
    """Raised when a camera id can't be subscribed to."""


class RustPlusCameraSession:
    """Owns the isolated camera socket and the currently-active CameraManager."""

    def __init__(self, hass: HomeAssistant, server_details) -> None:
        self._hass = hass
        self._server_details = server_details
        self._socket = None
        self._manager = None
        self._active_cam: str | None = None
        # A turret the user has explicitly taken control of: its subscription is
        # held open (auto-aim disabled) and never idle-exits until released.
        self._held_cam: str | None = None
        self._lock = asyncio.Lock()
        self._idle_handle = None
        self._last_resubscribe = 0.0

    # ---- connection / subscription lifecycle ------------------------------- #
    async def _ensure_socket(self) -> None:
        from rustplus import RustSocket
        from rustplus.remote.proxy.proxy_value_grabber import ProxyValueGrabber

        if self._socket is None:
            self._socket = RustSocket(self._server_details)
        if not getattr(self._socket.ws, "open", False):
            # connect() does a blocking proxy fetch; keep it off the event loop.
            await self._hass.async_add_executor_job(ProxyValueGrabber.get_value)
            await self._socket.connect()

    async def _reconnect(self) -> None:
        try:
            if self._socket is not None:
                await self._socket.disconnect()
        except Exception:  # noqa: BLE001
            pass
        self._manager = None
        self._active_cam = None
        await self._ensure_socket()

    async def _exit_manager(self) -> None:
        if self._manager is not None:
            try:
                await self._manager.exit_camera()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("exit_camera failed: %s", err)
            self._manager = None
            self._active_cam = None

    async def _ensure_subscribed(self, cam_id: str, _retry: bool = True):
        if self._manager is not None and self._active_cam == cam_id:
            return self._manager
        # Switching cameras (only one active per socket) — release the old one.
        await self._exit_manager()
        await self._ensure_socket()
        cam = await self._socket.get_camera_manager(cam_id)
        if type(cam).__name__ == "RustError":
            # The isolated socket may have died (rustplus leaves ws.open stale);
            # reconnect once and retry before giving up.
            if _retry:
                await self._reconnect()
                return await self._ensure_subscribed(cam_id, _retry=False)
            raise CameraUnavailable(str(cam))
        self._manager = cam
        self._active_cam = cam_id
        return cam

    # ---- public operations ------------------------------------------------- #
    async def snapshot(self, cam_id: str) -> bytes | None:
        """Return a JPEG frame from the camera's accumulated ray buffer."""
        async with self._lock:
            # Don't steal the single camera slot from a turret that's under active
            # control just to snapshot a different camera.
            if self._held_cam is not None and self._held_cam != cam_id:
                return None
            self._cancel_idle()
            cam = await self._ensure_subscribed(cam_id)

            if self._held_cam == cam_id:
                # Aiming stalls the turret's stream; re-subscribe at ~1-2 Hz (gated
                # by this human-paced viewing poll) to kick it back to live, then
                # render the latest buffered frame.
                now = time.monotonic()
                if now - self._last_resubscribe >= _LIVE_RESUBSCRIBE_INTERVAL:
                    self._last_resubscribe = now
                    try:
                        await cam.resubscribe()
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.debug("Live-control resubscribe failed: %s", err)
                frame = await cam.get_frame(render_entities=True) if cam.has_frame_data() else None
                self._schedule_idle()
                if frame is None:
                    return None
                buf = io.BytesIO()
                frame.convert("RGB").save(buf, format="JPEG")
                return buf.getvalue()

            # Passive/CCTV: let a couple seconds of samples build for a clean first
            # frame.
            frame = None
            elapsed = 0.0
            while elapsed < _ACCUMULATE_SECONDS:
                await asyncio.sleep(_FRAME_POLL_INTERVAL)
                elapsed += _FRAME_POLL_INTERVAL
                if cam.has_frame_data():
                    frame = await cam.get_frame(render_entities=False)
            if cam.has_frame_data():
                frame = await cam.get_frame(render_entities=True)
            self._schedule_idle()
            if frame is None:
                return None
            buf = io.BytesIO()
            frame.convert("RGB").save(buf, format="JPEG")
            return buf.getvalue()

    async def send_movement(
        self, cam_id: str, buttons=None, joystick=None, release_after: bool = False
    ) -> None:
        """Send a movement/aim/fire input to the (turret) camera.

        ``release_after`` sends a follow-up empty input so a held button (e.g.
        FIRE) becomes a discrete tap instead of staying pressed.
        """
        async with self._lock:
            # Controls only act on a turret that's actively under control (its
            # "Control" switch is on) — otherwise a button press would silently
            # take the turret out of auto-aim.
            if self._held_cam != cam_id:
                _LOGGER.debug(
                    "Ignoring control for '%s' — turret is not under active control.",
                    cam_id,
                )
                return
            self._cancel_idle()
            cam = await self._ensure_subscribed(cam_id)
            await cam.send_combined_movement(buttons or [], joystick)
            if release_after:
                await asyncio.sleep(0.1)
                await cam.send_combined_movement([], None)
            self._schedule_idle()

    async def activate(self, cam_id: str) -> None:
        """Take manual control of a turret and hold its subscription open.

        While active the turret is under our control and its auto-aim is disabled,
        so this is opt-in via the per-turret Control switch.
        """
        async with self._lock:
            self._cancel_idle()
            await self._ensure_subscribed(cam_id)  # raises CameraUnavailable if bad
            self._held_cam = cam_id

    async def deactivate(self, cam_id: str) -> None:
        """Release control so the turret resumes auto-aim."""
        async with self._lock:
            if self._held_cam == cam_id:
                self._held_cam = None
            if self._active_cam == cam_id:
                await self._exit_manager()

    def is_active(self, cam_id: str) -> bool:
        """Whether the given camera is currently held under active control."""
        return self._held_cam == cam_id

    async def close(self) -> None:
        """Tear down on unload."""
        self._cancel_idle()
        async with self._lock:
            await self._exit_manager()
            if self._socket is not None:
                try:
                    await self._socket.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                self._socket = None

    # ---- idle timer -------------------------------------------------------- #
    def _schedule_idle(self) -> None:
        self._cancel_idle()
        if self._held_cam is not None:
            return  # keep a controlled turret subscribed indefinitely
        self._idle_handle = self._hass.loop.call_later(
            _IDLE_EXIT_SECONDS,
            lambda: self._hass.async_create_task(self._idle_exit()),
        )

    def _cancel_idle(self) -> None:
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None

    async def _idle_exit(self) -> None:
        async with self._lock:
            await self._exit_manager()

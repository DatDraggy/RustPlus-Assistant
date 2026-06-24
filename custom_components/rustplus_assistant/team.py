"""Rust+ team features: per-teammate sensors, team events, and the promote service.

Teammates are modelled as sensors (rather than device_trackers) so they nest under
the per-server hub device and pick up clean, server-scoped entity_ids like the rest
of the integration — `device_tracker.ScannerEntity` forbids a device and ignores a
custom unique_id, which would reintroduce the multi-server id collision.

Each teammate sensor's state is their status (alive / dead / offline) with map
position, grid and timestamps as attributes. Members are added dynamically as they
appear in the polled team info. Team changes also push the fresh team info into the
coordinator (so entities update immediately) and fire ``rustplus_team_event`` on the
HA bus for automations.
"""
from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .camera import server_device_info
from .const import DOMAIN
from .coordinator import RustPlusDataCoordinator

_LOGGER = logging.getLogger(__name__)

TEAM_EVENT = "rustplus_team_event"
CHAT_EVENT = "rustplus_team_chat"
COMMAND_EVENT = "rustplus_command"
DEFAULT_COMMAND_PREFIX = "!"
SERVICE_PROMOTE_LEADER = "promote_leader"
SERVICE_SEND_MESSAGE = "send_team_message"
_SERVICES_KEY = "team_services_registered"


def team_members(coordinator) -> list:
    """Current team members from the latest poll (empty if none/unknown)."""
    team = (coordinator.data or {}).get("team_info")
    return list(getattr(team, "members", None) or [])


def member_status(member) -> str:
    """alive / dead / offline for a team member."""
    if not member.is_online:
        return "offline"
    return "alive" if member.is_alive else "dead"


def member_grid(x, y, map_size) -> str | None:
    """Map grid cell (e.g. ``D12``) for a position, if the map size is known."""
    if map_size is None or x is None or y is None:
        return None
    try:
        from rustplus.utils import convert_coordinates

        letter, number = convert_coordinates((int(x), int(y)), int(map_size))
        return f"{letter}{number}"
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# Per-teammate sensors (added dynamically from the sensor platform).
# --------------------------------------------------------------------------- #
def add_team_member_sensors(hass, entry, coordinator, async_add_entities) -> None:
    """Create a sensor per teammate now and as new teammates appear."""
    known: set[int] = set()

    @callback
    def _sync() -> None:
        new = []
        for m in team_members(coordinator):
            if m.steam_id not in known:
                known.add(m.steam_id)
                new.append(RustPlusTeamMemberSensor(coordinator, m.steam_id, m.name))
        if new:
            async_add_entities(new)

    _sync()
    entry.async_on_unload(coordinator.async_add_listener(_sync))


class RustPlusTeamMemberSensor(CoordinatorEntity[RustPlusDataCoordinator], SensorEntity):
    """A Rust+ teammate: state is their status, with position/grid as attributes."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, steam_id: int, name: str) -> None:
        """Initialize."""
        super().__init__(coordinator)
        sd = coordinator.socket.server_details
        self._steam_id = steam_id
        self._attr_name = name
        self._attr_unique_id = f"{sd.ip}_{sd.port}_team_{steam_id}"
        self._attr_device_info = server_device_info(coordinator)

    def _member(self):
        for m in team_members(self.coordinator):
            if m.steam_id == self._steam_id:
                return m
        return None

    @property
    def native_value(self):
        """alive / dead / offline (or None if they've left the team)."""
        m = self._member()
        return member_status(m) if m is not None else None

    @property
    def icon(self) -> str:
        m = self._member()
        if m is None:
            return "mdi:account-question"
        if not m.is_online:
            return "mdi:account-off"
        return "mdi:account" if m.is_alive else "mdi:skull-outline"

    @property
    def extra_state_attributes(self):
        m = self._member()
        if m is None:
            return {"steam_id": self._steam_id, "in_team": False}
        data = self.coordinator.data or {}
        info = data.get("info")
        map_size = getattr(info, "size", None)
        leader = getattr(data.get("team_info"), "leader_steam_id", None)
        return {
            "name": m.name,
            "steam_id": m.steam_id,
            "in_team": True,
            "is_online": bool(m.is_online),
            "is_alive": bool(m.is_alive),
            "is_leader": m.steam_id == leader,
            "x": round(m.x, 1) if m.x is not None else None,
            "y": round(m.y, 1) if m.y is not None else None,
            "grid": member_grid(m.x, m.y, map_size),
            "spawn_time": m.spawn_time,
            "death_time": m.death_time,
        }


# --------------------------------------------------------------------------- #
# Team-change events -> HA bus + immediate coordinator refresh.
# --------------------------------------------------------------------------- #
async def async_setup_team_events(hass, entry, coordinator) -> None:
    """Fire ``rustplus_team_event`` and refresh members on every team change."""
    from rustplus.identification import RegisteredListener
    from rustplus.events import TeamEventPayload

    sd = coordinator.socket.server_details

    def _snapshot(team_info) -> dict:
        snap = {}
        for m in getattr(team_info, "members", None) or []:
            snap[m.steam_id] = (m.name, bool(m.is_online), bool(m.is_alive))
        return snap

    prev = {"members": _snapshot((coordinator.data or {}).get("team_info"))}

    async def handle_team_event(event) -> None:
        team_info = event.team_info
        # Push fresh team info so member sensors + Team Size update immediately.
        try:
            if coordinator.data is None:
                coordinator.data = {}
            coordinator.data["team_info"] = team_info
            coordinator.async_set_updated_data(coordinator.data)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Failed to push team info from event: %s", err)

        now = _snapshot(team_info)
        before = prev["members"]
        prev["members"] = now
        hass.bus.async_fire(
            TEAM_EVENT,
            {
                "server": f"{sd.ip}:{sd.port}",
                "leader_steam_id": getattr(team_info, "leader_steam_id", None),
                "member_count": len(now),
                "online_count": sum(1 for s in now if now[s][1]),
                "joined": [now[s][0] for s in now if s not in before],
                "left": [before[s][0] for s in before if s not in now],
                "came_online": [now[s][0] for s in now if s in before and now[s][1] and not before[s][1]],
                "went_offline": [now[s][0] for s in now if s in before and not now[s][1] and before[s][1]],
                "died": [now[s][0] for s in now if s in before and not now[s][2] and before[s][2]],
            },
        )

    listener = RegisteredListener("ha_team_event", handle_team_event)
    TeamEventPayload.HANDLER_LIST.register(listener, sd)

    @callback
    def _cleanup() -> None:
        try:
            TeamEventPayload.HANDLER_LIST.unregister(listener, sd)
        except Exception:  # noqa: BLE001
            pass

    entry.async_on_unload(_cleanup)


# --------------------------------------------------------------------------- #
# Services.
# --------------------------------------------------------------------------- #
@callback
def async_register_services(hass: HomeAssistant) -> None:
    """Register team services once for the integration."""
    if hass.data[DOMAIN].get(_SERVICES_KEY):
        return
    hass.data[DOMAIN][_SERVICES_KEY] = True

    def _server_coordinators():
        for store in hass.data.get(DOMAIN, {}).values():
            if isinstance(store, dict) and store.get("type") == "server" and store.get("coordinator"):
                yield store["coordinator"]

    async def _async_promote_leader(call) -> None:
        steam_id = call.data.get("steam_id")
        steam_id = int(steam_id) if steam_id is not None else None
        for coordinator in _server_coordinators():
            try:
                async with coordinator.api_lock:
                    await coordinator.socket.promote_to_team_leader(steam_id)
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("promote_to_team_leader failed: %s", err)

    async def _async_send_team_message(call) -> None:
        message = call.data.get("message")
        if not message:
            return
        for coordinator in _server_coordinators():
            try:
                async with coordinator.api_lock:
                    await coordinator.socket.send_team_message(str(message))
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("send_team_message failed: %s", err)

    hass.services.async_register(DOMAIN, SERVICE_PROMOTE_LEADER, _async_promote_leader)
    hass.services.async_register(DOMAIN, SERVICE_SEND_MESSAGE, _async_send_team_message)

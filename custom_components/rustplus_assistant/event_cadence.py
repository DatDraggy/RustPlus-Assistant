"""Tracks recurring map-event spawns to estimate the next occurrence.

Each recurring event (Cargo Ship, Patrol Heli, ...) shows up as a map marker. We
record the timestamp of every *rising edge* (the marker appears) into a small
rolling ring and estimate the cadence as the average gap between spawns, so the
next occurrence can be projected. Spawns are server-configured and randomized, so
this is a heuristic: it needs >=2 observations and the history resets on a wipe or
when the rolling window is cold (e.g. a fresh install).

A single tracker per event is shared between the event's binary_sensor (which
records the edges and exposes the estimate as an attribute) and its timestamp
"next occurrence" sensor (which persists the ring across restarts), so both agree.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta

from homeassistant.util import dt as dt_util

from rustplus import RustMarker

from .const import DOMAIN

# (key, friendly name, marker type, icon) for the recurring in-game events.
MAP_EVENTS = [
    ("cargo_ship", "Cargo Ship", RustMarker.CargoShipMarker, "mdi:ferry"),
    ("patrol_helicopter", "Patrol Helicopter", RustMarker.PatrolHelicopterMarker, "mdi:helicopter"),
    ("ch47_chinook", "CH47 Chinook", RustMarker.ChinookMarker, "mdi:helicopter"),
    ("traveling_vendor", "Traveling Vendor", RustMarker.TravelingVendor, "mdi:truck-delivery"),
]

# How many recent spawns to average the cadence over.
_MAX_SPAWNS = 6


class EventCadenceTracker:
    """Rolling spawn-time history and cadence estimate for one map event."""

    def __init__(self, marker_type: int) -> None:
        self._marker_type = marker_type
        self._spawns: deque[datetime] = deque(maxlen=_MAX_SPAWNS)
        self._was_on: bool | None = None

    def event_present(self, markers) -> bool | None:
        """Whether a marker of this event's type is currently on the map."""
        if markers is None:
            return None
        return any(getattr(m, "type", None) == self._marker_type for m in markers)

    def observe(self, markers) -> None:
        """Record a rising edge (event appears).

        Idempotent within a single coordinator update: both the binary_sensor and
        the estimate sensor call this with the same markers, but only the off->on
        transition appends, so a second call in the same update is a no-op.
        """
        present = self.event_present(markers)
        if present is None:
            return
        if self._was_on is None:
            # First observation establishes the baseline — an event already up at
            # startup wasn't witnessed spawning, so don't record it as a spawn.
            self._was_on = present
            return
        if present and not self._was_on:
            self._spawns.append(dt_util.utcnow())
        self._was_on = present

    @property
    def sample_count(self) -> int:
        return len(self._spawns)

    @property
    def last_spawn(self) -> datetime | None:
        return self._spawns[-1] if self._spawns else None

    @property
    def cadence(self) -> timedelta | None:
        """Average gap between consecutive spawns (needs >=2 observations)."""
        if len(self._spawns) < 2:
            return None
        spawns = list(self._spawns)
        gaps = [b - a for a, b in zip(spawns, spawns[1:])]
        return sum(gaps, timedelta()) / len(gaps)

    @property
    def next_estimate(self) -> datetime | None:
        """Projected next spawn = last spawn + average cadence."""
        last = self.last_spawn
        cadence = self.cadence
        if last is None or cadence is None:
            return None
        return last + cadence

    # --- persistence (RestoreEntity) -------------------------------------- #
    def serialize(self) -> list[str]:
        """ISO timestamps of the spawn ring, for RestoreEntity storage."""
        return [d.isoformat() for d in self._spawns]

    def restore(self, data) -> None:
        """Rebuild the ring from serialized timestamps (once, if still empty)."""
        if not data or self._spawns:
            return
        parsed = []
        for iso in data:
            d = dt_util.parse_datetime(iso) if isinstance(iso, str) else None
            if d is not None:
                parsed.append(d)
        self._spawns.extend(parsed)


def get_event_trackers(hass, entry_id: str) -> dict[str, EventCadenceTracker]:
    """Shared per-event trackers for a config entry (created once)."""
    store = hass.data[DOMAIN][entry_id]
    trackers = store.get("event_trackers")
    if trackers is None:
        trackers = {key: EventCadenceTracker(mtype) for key, _name, mtype, _icon in MAP_EVENTS}
        store["event_trackers"] = trackers
    return trackers

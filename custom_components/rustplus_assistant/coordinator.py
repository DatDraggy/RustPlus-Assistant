"""Data update coordinator for Rust+."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from rustplus import RustSocket, ServerDetails

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Consecutive "not_found" polls before a paired entity is treated as destroyed
# in-game (guards against a one-off transient).
_MISSING_THRESHOLD = 2


class RustPlusDataCoordinator(DataUpdateCoordinator):
    """Class to manage fetching Rust+ data."""

    def __init__(self, hass: HomeAssistant, socket: RustSocket, config_entry: ConfigEntry) -> None:
        """Initialize."""
        self.socket = socket
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=60), # Set to 60s for server info polling
            config_entry=config_entry,
        )
        self.api_lock = asyncio.Lock()
        self.entities_to_poll = set()
        # In-game entity ids with an active websocket event subscription, plus a
        # refcount per id so several HA entities can share one in-game entity
        # (an alarm has both a binary_sensor and an event entity). Re-affirmed
        # after a reconnect, since the server forgets subscriptions on drop.
        self.subscribed_entities: set[int] = set()
        self._subscription_refs: dict[int, int] = {}
        # Paired in-game entities the server reports as gone (destroyed in-game):
        # their HA entities go unavailable and raise a Repair offering removal.
        self.destroyed_entities: set[int] = set()
        self._missing_counts: dict[int, int] = {}

    async def _connect(self) -> None:
        """Connect the data socket, keeping the blocking proxy/SSL work off the loop.

        rustplus' ``connect()`` fetches a proxy value with a blocking ``requests.get``
        (which also loads SSL certs) — running that on the event loop freezes ALL of
        Home Assistant for the duration of every reconnect (HA flags it as a blocking
        call). Pre-fetch the value in an executor first so ``connect()`` hits the cache
        and never blocks the loop. Mirrors what __init__.py does for the first connect.
        """
        from rustplus.remote.proxy.proxy_value_grabber import ProxyValueGrabber

        await self.hass.async_add_executor_job(ProxyValueGrabber.get_value)
        await self.socket.connect()

    async def _async_update_data(self):
        """Fetch data from Rust+."""
        try:
            reconnected = False
            async with self.api_lock:
                if not hasattr(self.socket.ws, "open") or not self.socket.ws.open:
                    await self._connect()
                    reconnected = True
                info = await self.socket.get_info()
                if type(info).__name__ == "RustError":
                    # The socket dropped abnormally (e.g. server closed it during a
                    # camera subscription) but rustplus doesn't reset ws.open after a
                    # close with no close-frame, so the check above misses it and every
                    # send silently fails. Force a clean reconnect, then retry.
                    _LOGGER.warning(
                        "Rust+ connection looks dead (get_info failed); reconnecting."
                    )
                    try:
                        await self.socket.disconnect()
                    except Exception:  # noqa: BLE001
                        pass
                    await self._connect()
                    reconnected = True
                    info = await self.socket.get_info()
                time = await self.socket.get_time()
    
                # Get Team Info
                try:
                    team_info = await self.socket.get_team_info()
                except Exception as e:
                    _LOGGER.debug("Failed to get team info (possibly not in a team): %s", e)
                    team_info = None

                # Map markers (cargo, heli, CH47, crates, vendors, ...) — powers
                # the event binary sensors (and, later, an overlay card).
                try:
                    markers = await self.socket.get_markers()
                    if type(markers).__name__ == "RustError":
                        markers = None
                except Exception as e:
                    _LOGGER.debug("Failed to get map markers: %s", e)
                    markers = None
                
            # After a reconnect the server forgets our entity-event
            # subscriptions, so re-affirm them (alarms rely on these events).
            if reconnected and self.subscribed_entities:
                await self._async_resubscribe_all()

            # Fetch Smart Entities sequentially to avoid concurrent websocket errors
            entity_states = {}
            for eid in list(self.entities_to_poll):
                try:
                    async with self.api_lock:
                        result = await self.socket.get_entity_info(eid)
                except Exception as e:
                    # A connection-level failure here is transient — don't treat it
                    # as the device being destroyed.
                    _LOGGER.debug("Failed to get entity info for %s: %s", eid, e)
                    continue
                if type(result).__name__ == "RustError":
                    reason = (getattr(result, "reason", "") or "").lower()
                    if "not_found" in reason:
                        # Server says this in-game entity no longer exists.
                        self._note_entity_missing(eid)
                    # Other RustErrors (e.g. "No response received") are transient.
                    continue
                entity_states[eid] = result
                self._note_entity_present(eid)

            return {
                "info": info,
                "time": time,
                "team_info": team_info,
                "markers": markers,
                "entities": entity_states,
            }
        except Exception as err:
            raise UpdateFailed(f"Error communicating with Rust+: {err}") from err

    async def async_subscribe_entity(self, eid: int) -> None:
        """Ref-counted server-side subscription to an entity's websocket events.

        Several HA entities can share one in-game entity (an alarm has both a
        binary_sensor and an event entity), so only the first reference actually
        subscribes and only the last unsubscribes — removing one entity must not
        deafen the other.
        """
        self._subscription_refs[eid] = self._subscription_refs.get(eid, 0) + 1
        self.subscribed_entities.add(eid)
        if self._subscription_refs[eid] == 1:
            await self._async_set_subscription(eid, True)

    async def async_unsubscribe_entity(self, eid: int) -> None:
        """Drop a reference; unsubscribe on the server when the last one goes."""
        if eid not in self._subscription_refs:
            return
        self._subscription_refs[eid] -= 1
        if self._subscription_refs[eid] <= 0:
            self._subscription_refs.pop(eid, None)
            self.subscribed_entities.discard(eid)
            await self._async_set_subscription(eid, False)

    async def _async_set_subscription(self, eid: int, value: bool) -> None:
        """Tell the server to start/stop sending events for an entity."""
        try:
            async with self.api_lock:
                if not hasattr(self.socket.ws, "open") or not self.socket.ws.open:
                    await self._connect()
                await self.socket.set_subscription_to_entity(eid, value)
        except Exception as e:
            _LOGGER.debug("set_subscription_to_entity(%s, %s) failed: %s", eid, value, e)

    async def _async_resubscribe_all(self) -> None:
        """Re-affirm all active entity-event subscriptions after a reconnect."""
        for eid in list(self.subscribed_entities):
            try:
                async with self.api_lock:
                    await self.socket.set_subscription_to_entity(eid, True)
            except Exception as e:
                _LOGGER.debug("Re-subscribe failed for entity %s: %s", eid, e)

    # ---- destroyed-in-game detection -------------------------------------- #
    def is_destroyed(self, eid: int) -> bool:
        """Whether a paired in-game entity has been destroyed (server: not_found)."""
        return eid in self.destroyed_entities

    def _note_entity_present(self, eid: int) -> None:
        """A paired entity responded — clear any 'missing' state / Repair."""
        self._missing_counts.pop(eid, None)
        if eid in self.destroyed_entities:
            self.destroyed_entities.discard(eid)
            self._clear_destroyed_issue(eid)
            _LOGGER.info("Rust+ entity %s is back; clearing destroyed state.", eid)

    def _note_entity_missing(self, eid: int) -> None:
        """A paired entity reported not_found — flag destroyed after a few polls."""
        if eid in self.destroyed_entities:
            return
        self._missing_counts[eid] = self._missing_counts.get(eid, 0) + 1
        if self._missing_counts[eid] >= _MISSING_THRESHOLD:
            self.destroyed_entities.add(eid)
            _LOGGER.info("Rust+ entity %s appears destroyed in-game; marking unavailable.", eid)
            self._raise_destroyed_issue(eid)

    def _entity_label(self, eid: int) -> str:
        """User-facing name for a paired entity, from the config entry options."""
        options = self.config_entry.options if self.config_entry else {}
        for key in ("switches", "smart_alarms", "storage_monitors"):
            name = (options.get(key) or {}).get(str(eid))
            if name:
                return name
        return f"Entity {eid}"

    def _raise_destroyed_issue(self, eid: int) -> None:
        from homeassistant.helpers import issue_registry as ir

        sd = self.socket.server_details
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"destroyed_{eid}",
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="device_destroyed",
            translation_placeholders={"name": self._entity_label(eid)},
            data={
                "entity_id": eid,
                "config_entry_id": self.config_entry.entry_id if self.config_entry else None,
                "device_unique_id": f"{sd.ip}_{sd.port}_{eid}",
                "name": self._entity_label(eid),
            },
        )

    def _clear_destroyed_issue(self, eid: int) -> None:
        from homeassistant.helpers import issue_registry as ir

        ir.async_delete_issue(self.hass, DOMAIN, f"destroyed_{eid}")

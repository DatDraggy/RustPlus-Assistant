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

    async def _async_update_data(self):
        """Fetch data from Rust+."""
        try:
            reconnected = False
            async with self.api_lock:
                if not hasattr(self.socket.ws, "open") or not self.socket.ws.open:
                    await self.socket.connect()
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
                    await self.socket.connect()
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
                        entity_states[eid] = await self.socket.get_entity_info(eid)
                except Exception as e:
                    _LOGGER.debug("Failed to get entity info for %s: %s", eid, e)

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
                    await self.socket.connect()
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

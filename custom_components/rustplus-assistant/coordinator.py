"""Data update coordinator for Rust+."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from rustplus import RustSocket, ServerDetails

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

class RustPlusDataCoordinator(DataUpdateCoordinator):
    """Class to manage fetching Rust+ data."""

    def __init__(self, hass: HomeAssistant, socket: RustSocket) -> None:
        """Initialize."""
        self.socket = socket
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=60), # Set to 60s for server info polling
        )
        self.api_lock = asyncio.Lock()
        self.entities_to_poll = set()

    async def _async_update_data(self):
        """Fetch data from Rust+."""
        try:
            async with self.api_lock:
                if not hasattr(self.socket.ws, "open") or not self.socket.ws.open:
                    await self.socket.connect()
                info = await self.socket.get_info()
                time = await self.socket.get_time()
    
                # Get Team Info
                try:
                    team_info = await self.socket.get_team_info()
                except Exception as e:
                    _LOGGER.debug("Failed to get team info (possibly not in a team): %s", e)
                    team_info = None
                
            # Fetch Smart Entities sequentially to avoid concurrent websocket errors
            entity_states = {}
            for eid in list(self.entities_to_poll):
                try:
                    async with self.api_lock:
                        entity_states[eid] = await self.socket.get_entity_info(eid)
                except Exception as e:
                    _LOGGER.debug("Failed to get entity info for %s: %s", eid, e)

            return {"info": info, "time": time, "team_info": team_info, "entities": entity_states}
        except Exception as err:
            raise UpdateFailed(f"Error communicating with Rust+: {err}") from err

"""Repairs for Rust+ — remove a paired device that was destroyed in-game.

When the server reports a paired entity as ``not_found`` (the in-game device was
destroyed), the coordinator marks it unavailable and raises a fixable issue. This
flow lets the user confirm removal: it drops the entity from the config-entry
options (so a reload doesn't recreate it) and removes its device + entities. If the
device is rebuilt and re-paired in the Rust+ app, auto-discovery brings it back.
"""
from __future__ import annotations

import voluptuous as vol

from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN

_OPTION_KEYS = ("switches", "smart_alarms", "storage_monitors")


class DestroyedDeviceRepairFlow(RepairsFlow):
    """Confirm and perform removal of an in-game-destroyed device."""

    def __init__(self, data: dict | None) -> None:
        self._data = data or {}

    async def async_step_init(self, user_input: dict | None = None):
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict | None = None):
        if user_input is not None:
            self._remove()
            return self.async_create_entry(title="", data={})
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={"name": self._data.get("name", "this device")},
        )

    def _remove(self) -> None:
        hass = self.hass
        eid = self._data.get("entity_id")
        entry_id = self._data.get("config_entry_id")
        unique_id = self._data.get("device_unique_id")

        # 1) Drop it from the config-entry options so a reload won't recreate it.
        entry = hass.config_entries.async_get_entry(entry_id) if entry_id else None
        if entry is not None and eid is not None:
            options = {**entry.options}
            changed = False
            for key in _OPTION_KEYS:
                bucket = options.get(key)
                if isinstance(bucket, dict) and str(eid) in bucket:
                    options[key] = {k: v for k, v in bucket.items() if k != str(eid)}
                    changed = True
            if changed:
                hass.config_entries.async_update_entry(entry, options=options)

        # 2) Remove the device (and with it, its entities).
        if unique_id:
            dev_reg = dr.async_get(hass)
            device = dev_reg.async_get_device(identifiers={(DOMAIN, unique_id)})
            if device is not None:
                dev_reg.async_remove_device(device.id)


async def async_create_fix_flow(hass: HomeAssistant, issue_id: str, data: dict | None):
    """Create the repair flow for a destroyed device."""
    return DestroyedDeviceRepairFlow(data)

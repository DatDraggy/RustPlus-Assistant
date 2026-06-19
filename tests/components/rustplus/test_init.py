"""Test Rust+ setup process."""
from homeassistant.setup import async_setup_component
from homeassistant.components.rustplus.const import DOMAIN
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

async def test_setup_unload_entry(hass: HomeAssistant) -> None:
    """Test setup and unload of entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "fcm_credentials": '{"keys": {}}',
            "server_ip": "127.0.0.1",
            "server_port": 28015,
            "player_id": 123,
            "player_token": 456
        },
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert DOMAIN in hass.data
    assert entry.entry_id in hass.data[DOMAIN]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.entry_id not in hass.data[DOMAIN]

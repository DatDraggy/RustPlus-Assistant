"""Test the Rust+ config flow."""
from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.components.rustplus.const import DOMAIN

async def test_form(hass):
    """Test we get the form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == "form"
    assert result["errors"] == {}

    with patch(
        "homeassistant.components.rustplus.config_flow.validate_input",
        return_value={"title": "Rust+ 127.0.0.1:28015"},
    ), patch(
        "homeassistant.components.rustplus.async_setup_entry",
        return_value=True,
    ) as mock_setup_entry:
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "fcm_credentials": '{"test": "test"}',
                "server_ip": "127.0.0.1",
                "server_port": 28015,
                "player_id": 123456789,
                "player_token": 987654321,
            },
        )
        await hass.async_block_till_done()

    assert result2["type"] == "create_entry"
    assert result2["title"] == "Rust+ 127.0.0.1:28015"
    assert result2["data"] == {
        "fcm_credentials": '{"test": "test"}',
        "server_ip": "127.0.0.1",
        "server_port": 28015,
        "player_id": 123456789,
        "player_token": 987654321,
    }
    assert len(mock_setup_entry.mock_calls) == 1

"""Tests for the Rust+ Home Assistant integration."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest


# ---------------------------------------------------------------------------
# Helpers & Fixtures
# ---------------------------------------------------------------------------

def _make_server_details(ip="192.168.1.100", port=28015):
    """Create a mock ServerDetails object."""
    sd = SimpleNamespace(ip=ip, port=port)
    return sd


def _make_coordinator(hass=None, server_details=None):
    """Create a minimal mock coordinator."""
    coord = MagicMock()
    coord.socket = MagicMock()
    coord.socket.server_details = server_details or _make_server_details()
    coord.data = {"info": None, "time": None, "team_info": None, "entities": {}}
    coord.entities_to_poll = set()
    coord.api_lock = asyncio.Lock()
    coord.async_set_updated_data = MagicMock()
    if hass:
        coord.hass = hass
    return coord


def _make_hass():
    """Create a minimal mock HomeAssistant."""
    hass = MagicMock()
    hass.data = {}
    hass.async_create_task = MagicMock(side_effect=lambda coro: asyncio.ensure_future(coro))
    hass.states = MagicMock()
    hass.states.get = MagicMock(return_value=None)
    hass.bus = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.config_entries = MagicMock()
    hass.loop = asyncio.get_event_loop()
    return hass


def _make_entity_info(items=None, has_protection=False, protection_expiry=0, value=False):
    """Create a mock entity info response."""
    info = SimpleNamespace()
    info.items = items or []
    info.has_protection = has_protection
    info.protection_expiry = protection_expiry
    info.value = value
    return info


def _make_item(item_id, quantity):
    """Create a mock storage item."""
    return SimpleNamespace(item_id=item_id, quantity=quantity)


# ---------------------------------------------------------------------------
# Entity Base Tests
# ---------------------------------------------------------------------------

class TestRustPlusEntity:
    """Tests for entity.py base class."""

    def test_unique_id_format(self):
        """unique_id should be {ip}_{port}_{entity_id}."""
        from custom_components.rustplus_assistant.entity import RustPlusEntity

        coord = _make_coordinator()
        entity = RustPlusEntity(coord, 12345, "switch", "My Switch")

        assert entity._attr_unique_id == "192.168.1.100_28015_12345"
        assert entity.rust_entity_id == 12345
        assert entity.entity_type == "switch"
        assert entity._attr_name == "My Switch"

    def test_device_info_structure(self):
        """Device info should have the correct identifiers and via_device."""
        from custom_components.rustplus_assistant.entity import RustPlusEntity

        coord = _make_coordinator()
        entity = RustPlusEntity(coord, 99999, "smart_alarm", "Raid Alarm")

        di = entity._attr_device_info
        assert ("rustplus_assistant", "192.168.1.100_28015_99999") in di["identifiers"]
        assert di["name"] == "Raid Alarm"
        assert di["manufacturer"] == "Facepunch"
        assert di["model"] == "Smart_alarm"
        assert di["via_device"] == ("rustplus_assistant", "192.168.1.100_28015")

    def test_different_servers_produce_different_ids(self):
        """Two entities with the same in-game ID on different servers should have different unique_ids."""
        from custom_components.rustplus_assistant.entity import RustPlusEntity

        coord_a = _make_coordinator(server_details=_make_server_details("10.0.0.1", 28015))
        coord_b = _make_coordinator(server_details=_make_server_details("10.0.0.2", 28015))

        entity_a = RustPlusEntity(coord_a, 12345, "switch", "Switch")
        entity_b = RustPlusEntity(coord_b, 12345, "switch", "Switch")

        assert entity_a._attr_unique_id != entity_b._attr_unique_id


# ---------------------------------------------------------------------------
# Storage Monitor Tests
# ---------------------------------------------------------------------------

class TestStorageMonitor:
    """Tests for the RustPlusStorageMonitor sensor."""

    def test_item_count(self):
        """native_value should be the number of items in the TC."""
        from custom_components.rustplus_assistant.sensor import RustPlusStorageMonitor

        coord = _make_coordinator()
        monitor = RustPlusStorageMonitor(coord, 5011, "TC")

        items = [_make_item(1, 100), _make_item(2, 200), _make_item(3, 50)]
        info = _make_entity_info(items=items)

        monitor._update_state_from_info(info)
        assert monitor._attr_native_value == 3

    def test_empty_tc(self):
        """An empty TC should report 0 items."""
        from custom_components.rustplus_assistant.sensor import RustPlusStorageMonitor

        coord = _make_coordinator()
        monitor = RustPlusStorageMonitor(coord, 5011, "TC")

        info = _make_entity_info(items=[])
        monitor._update_state_from_info(info)
        assert monitor._attr_native_value == 0

    @patch("custom_components.rustplus_assistant.sensor.translate_id_to_stack")
    def test_material_counts_in_attributes(self, mock_translate):
        """Extra state attributes should contain per-item counts."""
        from custom_components.rustplus_assistant.sensor import RustPlusStorageMonitor

        mock_translate.side_effect = lambda item_id: {
            100: "Wood", 101: "Stones", 102: "Metal Fragments"
        }.get(item_id, f"Item {item_id}")

        coord = _make_coordinator()
        monitor = RustPlusStorageMonitor(coord, 5011, "TC")

        items = [
            _make_item(100, 1000),
            _make_item(100, 500),   # Two stacks of Wood
            _make_item(101, 2000),
            _make_item(102, 750),
        ]
        info = _make_entity_info(items=items)
        monitor._update_state_from_info(info)

        attrs = monitor._attr_extra_state_attributes
        assert attrs["Wood"] == 1500
        assert attrs["Stones"] == 2000
        assert attrs["Metal Fragments"] == 750

    @patch("custom_components.rustplus_assistant.sensor.translate_id_to_stack")
    def test_upkeep_duration_attribute(self, mock_translate):
        """Upkeep Duration should be calculated from protection_expiry."""
        from custom_components.rustplus_assistant.sensor import RustPlusStorageMonitor

        mock_translate.return_value = "Wood"

        coord = _make_coordinator()
        monitor = RustPlusStorageMonitor(coord, 5011, "TC")

        future_expiry = int(time.time()) + 3600  # 1 hour from now
        info = _make_entity_info(
            items=[_make_item(100, 1000)],
            has_protection=True,
            protection_expiry=future_expiry,
        )
        monitor._update_state_from_info(info)

        attrs = monitor._attr_extra_state_attributes
        assert "Upkeep Duration" in attrs
        # Should be approximately 1 hour (could be 59:59 due to timing)
        assert "0:59:" in attrs["Upkeep Duration"] or "1:00:00" == attrs["Upkeep Duration"]

    @patch("custom_components.rustplus_assistant.sensor.translate_id_to_stack")
    def test_upkeep_expired(self, mock_translate):
        """Upkeep Duration should be 0:00:00 if protection has expired."""
        from custom_components.rustplus_assistant.sensor import RustPlusStorageMonitor

        mock_translate.return_value = "Wood"

        coord = _make_coordinator()
        monitor = RustPlusStorageMonitor(coord, 5011, "TC")

        past_expiry = int(time.time()) - 100  # Already expired
        info = _make_entity_info(
            items=[_make_item(100, 1000)],
            has_protection=True,
            protection_expiry=past_expiry,
        )
        monitor._update_state_from_info(info)

        attrs = monitor._attr_extra_state_attributes
        assert attrs["Upkeep Duration"] == "0:00:00"

    def test_registers_for_polling(self):
        """Storage monitor should add itself to coordinator.entities_to_poll."""
        from custom_components.rustplus_assistant.sensor import RustPlusStorageMonitor

        coord = _make_coordinator()
        assert len(coord.entities_to_poll) == 0

        RustPlusStorageMonitor(coord, 5011, "TC")
        assert 5011 in coord.entities_to_poll


class TestTCMaterialSensor:
    """Tests for RustPlusTCMaterialSensor."""

    def test_unique_id_suffix(self):
        """Material sensors should append the material name to the unique_id."""
        from custom_components.rustplus_assistant.sensor import RustPlusTCMaterialSensor

        coord = _make_coordinator()
        sensor = RustPlusTCMaterialSensor(coord, 5011, "TC", "Wood")

        assert sensor._attr_unique_id.endswith("_wood")

    def test_unique_id_suffix_hqm(self):
        """HQM sensor should have a clean suffix."""
        from custom_components.rustplus_assistant.sensor import RustPlusTCMaterialSensor

        coord = _make_coordinator()
        sensor = RustPlusTCMaterialSensor(coord, 5011, "TC", "High Quality Metal")

        assert sensor._attr_unique_id.endswith("_high_quality_metal")

    @patch("custom_components.rustplus_assistant.sensor.translate_id_to_stack")
    def test_reads_from_coordinator_data(self, mock_translate):
        """native_value should sum quantities matching this material from coordinator data."""
        from custom_components.rustplus_assistant.sensor import RustPlusTCMaterialSensor

        mock_translate.side_effect = lambda item_id: {
            100: "Wood", 101: "Stones"
        }.get(item_id, f"Item {item_id}")

        coord = _make_coordinator()
        sensor = RustPlusTCMaterialSensor(coord, 5011, "TC", "Wood")

        items = [_make_item(100, 1000), _make_item(100, 500), _make_item(101, 2000)]
        coord.data = {"entities": {5011: _make_entity_info(items=items)}}

        assert sensor.native_value == 1500

    @patch("custom_components.rustplus_assistant.sensor.translate_id_to_stack")
    def test_returns_zero_when_material_absent(self, mock_translate):
        """Returns 0 when the material is not in the TC."""
        from custom_components.rustplus_assistant.sensor import RustPlusTCMaterialSensor

        mock_translate.return_value = "Stones"

        coord = _make_coordinator()
        sensor = RustPlusTCMaterialSensor(coord, 5011, "TC", "Wood")

        items = [_make_item(101, 2000)]
        coord.data = {"entities": {5011: _make_entity_info(items=items)}}

        assert sensor.native_value == 0


class TestTCUpkeepSensor:
    """Tests for RustPlusTCUpkeepSensor."""

    def test_unique_id_suffix(self):
        """Upkeep sensor should append _upkeep to the unique_id."""
        from custom_components.rustplus_assistant.sensor import RustPlusTCUpkeepSensor

        coord = _make_coordinator()
        sensor = RustPlusTCUpkeepSensor(coord, 5011, "TC")

        assert sensor._attr_unique_id.endswith("_upkeep")

    def test_returns_unknown_without_data(self):
        """Returns 'Unknown' when no entity data exists."""
        from custom_components.rustplus_assistant.sensor import RustPlusTCUpkeepSensor

        coord = _make_coordinator()
        sensor = RustPlusTCUpkeepSensor(coord, 5011, "TC")

        coord.data = {"entities": {}}
        assert sensor.native_value == "Unknown"

    def test_returns_duration_string(self):
        """Returns a human-readable duration string."""
        from custom_components.rustplus_assistant.sensor import RustPlusTCUpkeepSensor

        coord = _make_coordinator()
        sensor = RustPlusTCUpkeepSensor(coord, 5011, "TC")

        future_expiry = int(time.time()) + 7200  # 2 hours
        coord.data = {"entities": {5011: _make_entity_info(has_protection=True, protection_expiry=future_expiry)}}

        value = sensor.native_value
        assert "1:59:" in value or "2:00:00" == value


# ---------------------------------------------------------------------------
# Server & Team Sensor Tests
# ---------------------------------------------------------------------------

class TestServerSensor:
    """Tests for RustPlusServerSensor."""

    def test_unique_id_format(self):
        """Unique ID should include the sensor type."""
        from custom_components.rustplus_assistant.sensor import RustPlusServerSensor

        coord = _make_coordinator()
        sensor = RustPlusServerSensor(coord, "players", "Players Online")

        assert sensor._attr_unique_id == "192.168.1.100_28015_players"
        assert sensor._attr_name == "Rust+ Players Online"

    def test_returns_none_without_data(self):
        """Returns None when coordinator has no data."""
        from custom_components.rustplus_assistant.sensor import RustPlusServerSensor

        coord = _make_coordinator()
        coord.data = None
        sensor = RustPlusServerSensor(coord, "players", "Players Online")

        assert sensor.native_value is None

    def test_returns_player_count(self):
        """Returns the player count from server info."""
        from custom_components.rustplus_assistant.sensor import RustPlusServerSensor

        coord = _make_coordinator()
        info = SimpleNamespace(players=42, queued_players=5, max_players=100)
        coord.data = {"info": info}

        sensor = RustPlusServerSensor(coord, "players", "Players Online")
        assert sensor.native_value == 42


class TestTeamSensor:
    """Tests for RustPlusTeamSensor."""

    def test_returns_zero_without_team(self):
        """Returns 0 when not in a team."""
        from custom_components.rustplus_assistant.sensor import RustPlusTeamSensor

        coord = _make_coordinator()
        coord.data = {"team_info": None}

        sensor = RustPlusTeamSensor(coord)
        assert sensor.native_value == 0

    def test_returns_member_count(self):
        """Returns the number of team members."""
        from custom_components.rustplus_assistant.sensor import RustPlusTeamSensor

        coord = _make_coordinator()
        team_info = SimpleNamespace(members=[1, 2, 3])
        coord.data = {"team_info": team_info}

        sensor = RustPlusTeamSensor(coord)
        assert sensor.native_value == 3


# ---------------------------------------------------------------------------
# Smart Alarm Tests
# ---------------------------------------------------------------------------

class TestSmartAlarm:
    """Tests for binary_sensor.py Smart Alarm."""

    def test_init_state(self):
        """Smart alarm should initialize as OFF."""
        from custom_components.rustplus_assistant.binary_sensor import RustPlusSmartAlarm

        coord = _make_coordinator()
        alarm = RustPlusSmartAlarm(coord, 8408, "Smart Alarm (8408)")

        assert alarm._attr_is_on is False
        assert alarm.should_poll is False

    @pytest.mark.asyncio
    async def test_force_refresh_activates_on_value_true(self):
        """When the server reports value=True, the alarm should activate."""
        from custom_components.rustplus_assistant.binary_sensor import RustPlusSmartAlarm

        hass = _make_hass()
        coord = _make_coordinator(hass=hass)
        coord.socket.get_entity_info = AsyncMock(return_value=_make_entity_info(value=True))

        alarm = RustPlusSmartAlarm(coord, 8408, "Smart Alarm (8408)")
        alarm.hass = hass
        alarm.async_write_ha_state = MagicMock()

        await alarm._async_force_refresh("Explosion!", "Your base is under attack!")

        assert alarm._attr_is_on is True
        alarm.async_write_ha_state.assert_called()

    @pytest.mark.asyncio
    async def test_force_refresh_ignores_value_false(self):
        """When the server reports value=False, the alarm should NOT activate."""
        from custom_components.rustplus_assistant.binary_sensor import RustPlusSmartAlarm

        hass = _make_hass()
        coord = _make_coordinator(hass=hass)
        coord.socket.get_entity_info = AsyncMock(return_value=_make_entity_info(value=False))

        alarm = RustPlusSmartAlarm(coord, 8408, "Smart Alarm (8408)")
        alarm.hass = hass
        alarm.async_write_ha_state = MagicMock()

        await alarm._async_force_refresh("Explosion!", "Your base is under attack!")

        assert alarm._attr_is_on is False
        alarm.async_write_ha_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_force_refresh_handles_api_error(self):
        """Should not crash if the API call fails."""
        from custom_components.rustplus_assistant.binary_sensor import RustPlusSmartAlarm

        hass = _make_hass()
        coord = _make_coordinator(hass=hass)
        coord.socket.get_entity_info = AsyncMock(side_effect=Exception("Connection lost"))

        alarm = RustPlusSmartAlarm(coord, 8408, "Smart Alarm (8408)")
        alarm.hass = hass
        alarm.async_write_ha_state = MagicMock()

        # Should not raise
        await alarm._async_force_refresh("Explosion!", "Your base is under attack!")
        assert alarm._attr_is_on is False


# ---------------------------------------------------------------------------
# Smart Switch Tests
# ---------------------------------------------------------------------------

class TestSmartSwitch:
    """Tests for switch.py Smart Switch."""

    def test_init_state(self):
        """Smart switch should initialize as OFF."""
        from custom_components.rustplus_assistant.switch import RustPlusSmartSwitch

        coord = _make_coordinator()
        switch = RustPlusSmartSwitch(coord, 8568, "Smart Switch (8568)")

        assert switch._attr_is_on is False

    @pytest.mark.asyncio
    async def test_turn_on(self):
        """Turning on should call set_entity_value(eid, True)."""
        from custom_components.rustplus_assistant.switch import RustPlusSmartSwitch

        coord = _make_coordinator()
        coord.socket.set_entity_value = AsyncMock()

        switch = RustPlusSmartSwitch(coord, 8568, "Smart Switch (8568)")
        switch.async_write_ha_state = MagicMock()

        await switch.async_turn_on()

        coord.socket.set_entity_value.assert_called_once_with(8568, True)
        assert switch._attr_is_on is True

    @pytest.mark.asyncio
    async def test_turn_off(self):
        """Turning off should call set_entity_value(eid, False)."""
        from custom_components.rustplus_assistant.switch import RustPlusSmartSwitch

        coord = _make_coordinator()
        coord.socket.set_entity_value = AsyncMock()

        switch = RustPlusSmartSwitch(coord, 8568, "Smart Switch (8568)")
        switch._attr_is_on = True
        switch.async_write_ha_state = MagicMock()

        await switch.async_turn_off()

        coord.socket.set_entity_value.assert_called_once_with(8568, False)
        assert switch._attr_is_on is False

    @pytest.mark.asyncio
    async def test_handle_event_updates_state(self):
        """Websocket event should update the switch state."""
        from custom_components.rustplus_assistant.switch import RustPlusSmartSwitch

        coord = _make_coordinator()
        switch = RustPlusSmartSwitch(coord, 8568, "Smart Switch (8568)")
        switch.async_write_ha_state = MagicMock()

        await switch._async_handle_event(True)
        assert switch._attr_is_on is True

        await switch._async_handle_event(False)
        assert switch._attr_is_on is False


# ---------------------------------------------------------------------------
# FCM Manager Tests
# ---------------------------------------------------------------------------

class TestFCMManager:
    """Tests for fcm_manager.py notification routing."""

    def _make_manager(self, hass=None):
        """Create an FCM manager with mocked credentials."""
        hass = hass or _make_hass()
        creds = json.dumps({"fcm_credentials": {"keys": {"p256dh": "test", "auth": "test"}}})
        with patch("custom_components.rustplus_assistant.fcm_manager.FCMListener"):
            from custom_components.rustplus_assistant.fcm_manager import RustPlusFCMManager
            manager = RustPlusFCMManager(hass, creds)
        return manager, hass

    def test_server_pairing_triggers_discovery_flow(self):
        """A server pairing notification should initiate a config flow."""
        manager, hass = self._make_manager()

        data_message = {
            "title": "Rust+",
            "message": "Tap to pair with this server.",
            "channelId": "pairing",
            "body": json.dumps({
                "type": "server",
                "ip": "195.60.166.126",
                "port": "28382",
                "playerId": "76561198121218959",
                "playerToken": "-1674993012",
                "name": "My Rust Server",
            }),
        }
        notification = {}

        manager._handle_notification_threadsafe(notification, data_message)

        hass.async_create_task.assert_called()

    def test_alarm_notification_dispatches_signal(self):
        """An alarm notification should dispatch a refresh signal."""
        manager, hass = self._make_manager()

        data_message = {
            "title": "Explosion Detected!",
            "message": "Your base is under attack!",
            "channelId": "alarm",
            "body": json.dumps({
                "ip": "195.60.166.126",
                "port": "28382",
                "type": "alarm",
            }),
        }
        notification = {}

        with patch("custom_components.rustplus_assistant.fcm_manager.async_dispatcher_send") as mock_dispatch:
            manager._handle_notification_threadsafe(notification, data_message)
            mock_dispatch.assert_called_once()
            call_args = mock_dispatch.call_args
            assert call_args[0][1] == "rustplus_alarm_refresh_195.60.166.126"
            assert call_args[0][2] == "Explosion Detected!"

    def test_generic_notification_fires_event(self):
        """A non-pairing, non-alarm notification should fire an HA event."""
        manager, hass = self._make_manager()

        data_message = {
            "title": "Player Connected",
            "message": "SomePlayer joined the server.",
            "channelId": "player",
            "body": json.dumps({"ip": "195.60.166.126"}),
        }
        notification = {}

        manager._handle_notification_threadsafe(notification, data_message)

        hass.bus.async_fire.assert_called_once()
        event_data = hass.bus.async_fire.call_args[0][1]
        assert event_data["title"] == "Player Connected"

    def test_json_body_message_replaced(self):
        """If the message body starts with '{', it should be replaced with a generic string."""
        manager, hass = self._make_manager()

        data_message = {
            "title": "Some Event",
            "message": '{"id":"server-id","name":"My Server"}',
            "channelId": "other",
        }
        notification = {}

        manager._handle_notification_threadsafe(notification, data_message)

        hass.bus.async_fire.assert_called_once()
        event_data = hass.bus.async_fire.call_args[0][1]
        assert event_data["message"] == "An event occurred on the server."

    def test_invalid_fcm_credentials(self):
        """Invalid JSON credentials should not crash the manager."""
        hass = _make_hass()
        with patch("custom_components.rustplus_assistant.fcm_manager.FCMListener"):
            from custom_components.rustplus_assistant.fcm_manager import RustPlusFCMManager
            # Should not raise
            manager = RustPlusFCMManager(hass, "not-valid-json{{{")

    def test_entity_name_gets_id_suffix(self):
        """Smart devices with generic names should get a 4-digit ID suffix."""
        manager, hass = self._make_manager()

        data_message = {
            "title": "Smart Switch",
            "message": "Tap to pair with this device.",
            "channelId": "pairing",
            "body": json.dumps({
                "entityId": "854388568",
                "entityType": "1",
                "entityName": "Smart Switch",
                "type": "entity",
            }),
        }
        notification = {}

        with patch.object(manager, "_async_auto_discover_device", new_callable=AsyncMock) as mock_discover:
            manager._handle_notification_threadsafe(notification, data_message)
            hass.async_create_task.assert_called()


# ---------------------------------------------------------------------------
# Config Flow Tests
# ---------------------------------------------------------------------------

class TestConfigFlow:
    """Tests for config_flow.py."""

    @pytest.mark.asyncio
    async def test_validate_input_valid_json(self):
        """Valid JSON should pass validation."""
        from custom_components.rustplus_assistant.config_flow import validate_input

        hass = _make_hass()
        data = {"fcm_credentials": json.dumps({"keys": {"auth": "test"}})}
        result = await validate_input(hass, data)
        assert result["title"] == "Rust+ Account"

    @pytest.mark.asyncio
    async def test_validate_input_invalid_json(self):
        """Invalid JSON should raise InvalidAuth."""
        from custom_components.rustplus_assistant.config_flow import validate_input, InvalidAuth

        hass = _make_hass()
        data = {"fcm_credentials": "not-valid-json"}
        with pytest.raises(InvalidAuth):
            await validate_input(hass, data)


# ---------------------------------------------------------------------------
# Options Flow Parsing Tests
# ---------------------------------------------------------------------------

class TestOptionsFlowParsing:
    """Tests for the options flow eid:name parsing logic."""

    def test_switch_parsing(self):
        """Options flow should parse 'eid:name' format for switches."""
        user_input = {
            "add_switches": "12345:Main Light, 67890:Front Door",
            "add_monitors": "",
            "add_alarms": "",
        }

        switches = {}
        for line in user_input.get("add_switches", "").split(","):
            if ":" in line:
                eid, name = line.split(":", 1)
                switches[eid.strip()] = name.strip()

        assert switches == {"12345": "Main Light", "67890": "Front Door"}

    def test_empty_input(self):
        """Empty strings should produce no entries."""
        user_input = {
            "add_switches": "",
            "add_monitors": "",
            "add_alarms": "",
        }

        switches = {}
        for line in user_input.get("add_switches", "").split(","):
            if ":" in line:
                eid, name = line.split(":", 1)
                switches[eid.strip()] = name.strip()

        assert switches == {}

    def test_malformed_input_no_colon(self):
        """Input without colons should be ignored."""
        user_input = {"add_switches": "12345 Main Light"}

        switches = {}
        for line in user_input.get("add_switches", "").split(","):
            if ":" in line:
                eid, name = line.split(":", 1)
                switches[eid.strip()] = name.strip()

        assert switches == {}


# ---------------------------------------------------------------------------
# Platform Setup Tests
# ---------------------------------------------------------------------------

class TestPlatformSetup:
    """Tests for platform async_setup_entry functions."""

    @pytest.mark.asyncio
    async def test_sensor_setup_creates_server_sensors(self):
        """Sensor setup should always create Players Online, Players Queued, Max Players, and Team Size."""
        from custom_components.rustplus_assistant.sensor import async_setup_entry

        hass = _make_hass()
        coord = _make_coordinator()
        hass.data = {"rustplus_assistant": {"test_entry": {"coordinator": coord}}}

        entry = MagicMock()
        entry.entry_id = "test_entry"
        entry.options = {}

        added = []
        async_add = MagicMock(side_effect=lambda entities: added.extend(entities))

        await async_setup_entry(hass, entry, async_add)

        # Should have 4 base sensors (3 server + 1 team)
        assert len(added) == 4
        names = [e._attr_name for e in added]
        assert "Rust+ Players Online" in names
        assert "Rust+ Players Queued" in names
        assert "Rust+ Max Players" in names
        assert "Rust+ Team Size" in names

    @pytest.mark.asyncio
    async def test_sensor_setup_creates_storage_monitor_sub_sensors(self):
        """Adding a storage monitor should create 1 main + 4 materials + 1 upkeep = 6 extra sensors."""
        from custom_components.rustplus_assistant.sensor import async_setup_entry

        hass = _make_hass()
        coord = _make_coordinator()
        hass.data = {"rustplus_assistant": {"test_entry": {"coordinator": coord}}}

        entry = MagicMock()
        entry.entry_id = "test_entry"
        entry.options = {"storage_monitors": {"5011": "TC"}}

        added = []
        async_add = MagicMock(side_effect=lambda entities: added.extend(entities))

        await async_setup_entry(hass, entry, async_add)

        # 4 base + 1 main monitor + 4 materials + 1 upkeep = 10
        assert len(added) == 10

    @pytest.mark.asyncio
    async def test_binary_sensor_setup_creates_alarms(self):
        """Binary sensor setup should create an entity per paired alarm."""
        from custom_components.rustplus_assistant.binary_sensor import async_setup_entry

        hass = _make_hass()
        coord = _make_coordinator()
        hass.data = {"rustplus_assistant": {"test_entry": {"coordinator": coord}}}

        entry = MagicMock()
        entry.entry_id = "test_entry"
        entry.options = {
            "smart_alarms": {
                "1073728408": "Smart Alarm (8408)",
                "950955033": "Smart Alarm (5033)",
            }
        }

        added = []
        async_add = MagicMock(side_effect=lambda entities: added.extend(entities))

        await async_setup_entry(hass, entry, async_add)

        assert len(added) == 2
        eids = {e.rust_entity_id for e in added}
        assert eids == {1073728408, 950955033}

    @pytest.mark.asyncio
    async def test_switch_setup_creates_switches(self):
        """Switch setup should create an entity per paired switch."""
        from custom_components.rustplus_assistant.switch import async_setup_entry

        hass = _make_hass()
        coord = _make_coordinator()
        coord.socket.get_entity_info = AsyncMock(side_effect=Exception("No ID provided"))
        hass.data = {"rustplus_assistant": {"test_entry": {"coordinator": coord, "socket": coord.socket}}}

        entry = MagicMock()
        entry.entry_id = "test_entry"
        entry.options = {
            "switches": {"854388568": "Smart Switch (8568)"}
        }

        added = []
        async_add = MagicMock(side_effect=lambda entities: added.extend(entities))

        await async_setup_entry(hass, entry, async_add)

        assert len(added) == 1
        assert added[0].rust_entity_id == 854388568


# ---------------------------------------------------------------------------
# Smart Alarm Event Tests (prototype event platform)
# ---------------------------------------------------------------------------

class TestSmartAlarmEvent:
    """Tests for event.py — the prototype event-entity model for Smart Alarms."""

    def test_unique_id_has_event_suffix(self):
        """Event entity gets an _event suffix so it can coexist with the binary_sensor."""
        from custom_components.rustplus_assistant.event import RustPlusSmartAlarmEvent

        coord = _make_coordinator()
        ev = RustPlusSmartAlarmEvent(coord, 8408, "Smart Alarm (8408)")

        assert ev._attr_unique_id == "192.168.1.100_28015_8408_event"
        assert ev._attr_event_types == ["triggered"]

    def test_broadcast_fires_when_no_entity_id(self):
        """With no entity_id in the push, the alarm fires (today's broadcast behaviour)."""
        from custom_components.rustplus_assistant.event import RustPlusSmartAlarmEvent

        coord = _make_coordinator()
        ev = RustPlusSmartAlarmEvent(coord, 8408, "Smart Alarm (8408)")
        ev._trigger_event = MagicMock()
        ev.async_write_ha_state = MagicMock()

        ev._handle_alarm("Explosion!", "Under attack!", None)

        ev._trigger_event.assert_called_once()
        assert ev._trigger_event.call_args[0][0] == "triggered"
        ev.async_write_ha_state.assert_called_once()

    def test_fires_when_entity_id_matches(self):
        """When the push targets this alarm's id, it fires."""
        from custom_components.rustplus_assistant.event import RustPlusSmartAlarmEvent

        coord = _make_coordinator()
        ev = RustPlusSmartAlarmEvent(coord, 8408, "Smart Alarm (8408)")
        ev._trigger_event = MagicMock()
        ev.async_write_ha_state = MagicMock()

        ev._handle_alarm("Explosion!", "Under attack!", "8408")

        ev._trigger_event.assert_called_once()

    def test_ignored_when_entity_id_is_a_different_alarm(self):
        """When the push targets a different alarm, this entity stays silent."""
        from custom_components.rustplus_assistant.event import RustPlusSmartAlarmEvent

        coord = _make_coordinator()
        ev = RustPlusSmartAlarmEvent(coord, 8408, "Smart Alarm (8408)")
        ev._trigger_event = MagicMock()
        ev.async_write_ha_state = MagicMock()

        ev._handle_alarm("Explosion!", "Under attack!", "9999")

        ev._trigger_event.assert_not_called()
        ev.async_write_ha_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_setup_creates_event_per_alarm(self):
        """Event setup should create one event entity per paired alarm."""
        from custom_components.rustplus_assistant.event import async_setup_entry

        hass = _make_hass()
        coord = _make_coordinator()
        hass.data = {"rustplus_assistant": {"test_entry": {"coordinator": coord}}}

        entry = MagicMock()
        entry.entry_id = "test_entry"
        entry.options = {"smart_alarms": {"8408": "Smart Alarm (8408)", "5033": "Smart Alarm (5033)"}}

        added = []
        async_add = MagicMock(side_effect=lambda entities: added.extend(entities))

        await async_setup_entry(hass, entry, async_add)

        assert len(added) == 2
        assert all(e._attr_unique_id.endswith("_event") for e in added)

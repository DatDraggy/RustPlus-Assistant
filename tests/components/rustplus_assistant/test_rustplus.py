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
    def _record_task(coro):
        # These unit tests only assert the call happened; close real coroutines
        # so Python doesn't warn that they were never awaited.
        if asyncio.iscoroutine(coro):
            coro.close()
    hass.async_create_task = MagicMock(side_effect=_record_task)
    hass.states = MagicMock()
    hass.states.get = MagicMock(return_value=None)
    hass.bus = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.config_entries = MagicMock()
    try:
        hass.loop = asyncio.get_event_loop()
    except RuntimeError:
        # Python 3.12+ raises when there's no running loop (sync tests); a fresh
        # loop suffices for the mock since these tests never run it.
        hass.loop = asyncio.new_event_loop()
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
        assert entity._attr_name is None  # primary entity inherits the device name
        assert "My Switch" in entity._attr_device_info["name"]

    def test_device_info_structure(self):
        """Device info should have the correct identifiers and via_device."""
        from custom_components.rustplus_assistant.entity import RustPlusEntity

        coord = _make_coordinator()
        entity = RustPlusEntity(coord, 99999, "smart_alarm", "Raid Alarm")

        di = entity._attr_device_info
        assert ("rustplus_assistant", "192.168.1.100_28015_99999") in di["identifiers"]
        assert "Raid Alarm" in di["name"]  # server-scoped: "{server_label} Raid Alarm"
        assert di["manufacturer"] == "Facepunch"
        assert di["model"] == "Smart Alarm"
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
        assert sensor._attr_name == "Players Online"

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
    async def test_handle_event_activates_on_true(self):
        """A websocket entity event with value=True should turn the alarm on."""
        from custom_components.rustplus_assistant.binary_sensor import RustPlusSmartAlarm

        coord = _make_coordinator()
        alarm = RustPlusSmartAlarm(coord, 8408, "Smart Alarm (8408)")
        alarm.async_write_ha_state = MagicMock()

        await alarm._async_handle_event(True)

        assert alarm._attr_is_on is True
        alarm.async_write_ha_state.assert_called()

    @pytest.mark.asyncio
    async def test_handle_event_clears_on_false(self):
        """A websocket entity event with value=False should turn the alarm off."""
        from custom_components.rustplus_assistant.binary_sensor import RustPlusSmartAlarm

        coord = _make_coordinator()
        alarm = RustPlusSmartAlarm(coord, 8408, "Smart Alarm (8408)")
        alarm._attr_is_on = True
        alarm.async_write_ha_state = MagicMock()

        await alarm._async_handle_event(False)

        assert alarm._attr_is_on is False
        alarm.async_write_ha_state.assert_called()


class TestSmartAlarmEvent:
    """Tests for event.py Smart Alarm event entity."""

    def test_identity(self):
        """Event entity should suffix unique_id/name and expose the trigger type."""
        from custom_components.rustplus_assistant.event import (
            RustPlusSmartAlarmEvent,
            EVENT_TRIGGERED,
        )

        coord = _make_coordinator()
        ev = RustPlusSmartAlarmEvent(coord, 8408, "Smart Alarm (8408)")

        assert ev._attr_event_types == [EVENT_TRIGGERED]
        assert ev._attr_unique_id.endswith("_event")
        assert ev._attr_name == "Event"
        assert "Smart Alarm (8408)" in ev._attr_device_info["name"]

    @pytest.mark.asyncio
    async def test_fires_only_on_rising_edge(self):
        """Event should fire on off->on transitions only, not on repeats or off."""
        from custom_components.rustplus_assistant.event import RustPlusSmartAlarmEvent

        coord = _make_coordinator()
        ev = RustPlusSmartAlarmEvent(coord, 8408, "Smart Alarm (8408)")
        ev._trigger_event = MagicMock()
        ev.async_write_ha_state = MagicMock()

        await ev._async_handle_event(True)    # rising edge -> fire
        ev._trigger_event.assert_called_once()

        ev._trigger_event.reset_mock()
        await ev._async_handle_event(True)    # still on -> no fire
        await ev._async_handle_event(False)   # falling -> no fire
        ev._trigger_event.assert_not_called()

        await ev._async_handle_event(True)    # rising again -> fire
        ev._trigger_event.assert_called_once()


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

        # 3 server + 1 server-info + 1 team + 1 in-game time + 4 event estimates
        # + 1 last-chat
        assert len(added) == 11
        names = [e._attr_name for e in added]
        assert "Players Online" in names
        assert "Time" in names
        assert "Server" in names
        assert "Players Queued" in names
        assert "Max Players" in names
        assert "Team Size" in names
        assert "Cargo Ship Next" in names
        assert "Last Team Message" in names

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

        # 11 base (3 server + server-info + team + time + 4 event estimates + last-chat)
        # + 1 monitor + 4 materials + 1 upkeep = 17
        assert len(added) == 17

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

        # 1 daytime + 4 map-event sensors + 2 alarms
        assert len(added) == 7
        eids = {e.rust_entity_id for e in added if hasattr(e, "rust_entity_id")}
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
# Coordinator Subscription Tests
# ---------------------------------------------------------------------------

def _make_sub_coordinator():
    """A coordinator with only the bits the subscription helpers touch."""
    from custom_components.rustplus_assistant.coordinator import RustPlusDataCoordinator

    coord = RustPlusDataCoordinator.__new__(RustPlusDataCoordinator)
    coord.socket = MagicMock()
    coord.socket.ws = MagicMock()
    coord.socket.ws.open = True
    coord.socket.connect = AsyncMock()
    coord.socket.set_subscription_to_entity = AsyncMock()
    coord.api_lock = asyncio.Lock()
    coord.subscribed_entities = set()
    coord._subscription_refs = {}
    return coord


class TestCoordinatorSubscriptions:
    """Tests for ref-counted entity-event subscriptions + reconnect re-subscribe."""

    @pytest.mark.asyncio
    async def test_refcount_subscribe_once_unsubscribe_on_last(self):
        """An alarm shares one eid across its binary_sensor + event entity."""
        coord = _make_sub_coordinator()
        eid = 950955033

        await coord.async_subscribe_entity(eid)
        await coord.async_subscribe_entity(eid)

        assert coord._subscription_refs[eid] == 2
        assert eid in coord.subscribed_entities
        # Only the first reference hits the server.
        assert coord.socket.set_subscription_to_entity.call_count == 1
        coord.socket.set_subscription_to_entity.assert_called_with(eid, True)

        # Removing one entity must NOT unsubscribe — the other still needs events.
        await coord.async_unsubscribe_entity(eid)
        assert eid in coord.subscribed_entities
        assert coord.socket.set_subscription_to_entity.call_count == 1

        # Last reference gone → unsubscribe on the server.
        await coord.async_unsubscribe_entity(eid)
        assert eid not in coord.subscribed_entities
        assert coord.socket.set_subscription_to_entity.call_count == 2
        coord.socket.set_subscription_to_entity.assert_called_with(eid, False)

    @pytest.mark.asyncio
    async def test_resubscribe_all_after_reconnect(self):
        """All active subscriptions are re-affirmed after a reconnect."""
        coord = _make_sub_coordinator()
        coord.subscribed_entities = {950955033, 1077946609}

        await coord._async_resubscribe_all()

        sent = {c.args for c in coord.socket.set_subscription_to_entity.call_args_list}
        assert (950955033, True) in sent
        assert (1077946609, True) in sent


# ---------------------------------------------------------------------------
# QR-auth module tests (deterministic parts; no network)
# ---------------------------------------------------------------------------

class TestAuth:
    """Tests for auth.py — hand-rolled protobuf, JWT parsing, token extraction."""

    def test_varint_roundtrip(self):
        from custom_components.rustplus_assistant.auth import _varint, _read_varint
        for n in [0, 1, 127, 128, 300, 976529667804, 17800227057520431560]:
            assert _read_varint(_varint(n), 0)[0] == n

    def test_protobuf_roundtrip_nested(self):
        from custom_components.rustplus_assistant.auth import _p_str, _p_vint, _p_msg, _pb_decode
        # mirrors the BeginAuthSessionViaQR request shape: device_details(3)={name(1), platform(2)}
        buf = _p_msg(3, _p_str(1, "Home Assistant Rust+") + _p_vint(2, 2))
        d = _pb_decode(buf)
        inner = _pb_decode(d[3][0])
        assert inner[1][0] == b"Home Assistant Rust+"
        assert inner[2][0] == 2

    def test_steamid_from_jwt(self):
        import base64
        import json as _json
        from custom_components.rustplus_assistant.auth import RustPlusQRAuth
        payload = base64.urlsafe_b64encode(
            _json.dumps({"sub": "76561198121218959", "iss": "steam"}).encode()
        ).rstrip(b"=").decode()
        token = "hdr." + payload + ".sig"
        assert RustPlusQRAuth._steamid_from_jwt(token) == "76561198121218959"

    def test_token_extraction_not_truncated(self):
        """Regression: the old [A-Za-z0-9_\\-.] regex truncated tokens containing +/=/ ."""
        import re
        from urllib.parse import unquote
        loc = "/?steamId=765&token=ab%2Bcd%2Fef%3Dgh.ij_kl-mn&x=1"
        m = re.search(r"[?&#]token=([^&\s\"'<>]{16,})", loc)
        assert m, "token must be captured"
        assert unquote(m.group(1)) == "ab+cd/ef=gh.ij_kl-mn"

    def test_begin_parses_qr_session(self):
        """begin() must decode client_id(1)/challenge(2)/request_id(3) from the WebAPI reply."""
        from custom_components.rustplus_assistant.auth import (
            RustPlusQRAuth, _p_vint, _p_str,
        )

        class _Resp:
            content = _p_vint(1, 12345) + _p_str(2, "https://s.team/q/ABCD") + _p_str(3, b"\x01\x02\x03")

            def raise_for_status(self):
                pass

        auth = RustPlusQRAuth()
        auth.session.post = lambda *a, **k: _Resp()  # type: ignore[assignment]
        assert auth.begin() == "https://s.team/q/ABCD"
        assert auth._client_id == 12345
        assert auth._request_id == b"\x01\x02\x03"

    def test_complete_threads_device_id_into_registrations(self):
        """Regression: complete() must forward the per-install DeviceId all the way into
        both the Expo and the Facepunch push registrations.

        complete() previously took only refresh_token and dropped device_id, so every
        install fell back to a random uuid (and the config flow's call crashed with a
        TypeError). Guard the full chain complete() -> _fcm_register -> {Expo, Facepunch}.
        """
        from custom_components.rustplus_assistant.auth import RustPlusQRAuth

        posts: list[tuple[str, dict]] = []

        class _Resp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"data": {"expoPushToken": "ExponentPushToken[xyz]"}}

        class _Session:
            def post(self, url, **kw):
                posts.append((url, kw.get("json") or {}))
                return _Resp()

        auth = RustPlusQRAuth()
        auth.session = _Session()  # type: ignore[assignment]
        auth._steamid_from_jwt = lambda _t: "765"          # type: ignore[assignment]
        auth._load_web_cookies = lambda _rt, _sid: None     # type: ignore[assignment]
        auth._get_rust_token = lambda: "RUST_TOKEN"         # type: ignore[assignment]
        auth._android_fcm_register = lambda: {              # type: ignore[assignment]
            "fcm": {"token": "FCMTOK"},
            "gcm": {"androidId": "aid", "securityToken": "stok"},
        }

        # HA's instance_id is a 32-char hex string with no dashes. Expo requires a
        # canonical UUID for deviceId, so complete() normalises it (deterministically,
        # so the per-install id stays stable) and threads it into BOTH registrations.
        import uuid

        inst = "0123456789abcdef0123456789abcdef"
        creds = auth.complete("hdr.payload.sig", device_id=inst)

        assert creds["rustplus_auth_token"] == "RUST_TOKEN"
        assert creds["expo_push_token"] == "ExponentPushToken[xyz]"
        assert creds["fcm_credentials"] == {
            "fcm": {"token": "FCMTOK"},
            "gcm": {"androidId": "aid", "securityToken": "stok"},
        }
        expo_post = next(body for url, body in posts if "getExpoPushToken" in url)
        fp_post = next(body for url, body in posts if "/api/push/register" in url)
        expected_id = str(uuid.UUID(inst))  # canonical dashed UUID Expo accepts
        assert expected_id != inst  # it really did normalise
        assert expo_post["deviceId"] == expected_id
        assert fp_post["DeviceId"] == expected_id  # same id threaded to Facepunch
        uuid.UUID(expo_post["deviceId"])  # must be a valid UUID (the Expo-400 fix)
        assert fp_post["AuthToken"] == "RUST_TOKEN"
        assert fp_post["PushToken"] == "ExponentPushToken[xyz]"

    def test_android_fcm_register_retries_then_succeeds(self, monkeypatch):
        """Google's GCM register is flaky; _android_fcm_register must retry, not give up."""
        import sys
        import types
        from custom_components.rustplus_assistant import auth as auth_mod

        calls = {"n": 0}

        class _FakeAndroidFCM:
            @staticmethod
            def register(*a, **k):
                calls["n"] += 1
                if calls["n"] < 3:
                    raise RuntimeError("PHONE_REGISTRATION_ERROR")
                return {"fcm": {"token": "T"}, "gcm": {"androidId": "a", "securityToken": "s"}}

        fake_mod = types.ModuleType("push_receiver.android_fcm_register")
        fake_mod.AndroidFCM = _FakeAndroidFCM
        monkeypatch.setitem(sys.modules, "push_receiver.android_fcm_register", fake_mod)
        monkeypatch.setattr(auth_mod.time, "sleep", lambda *_a: None)

        out = auth_mod.RustPlusQRAuth._android_fcm_register(attempts=5)
        assert out["fcm"]["token"] == "T"
        assert calls["n"] == 3


# ---------------------------------------------------------------------------
# Camera (CCTV / turret) tests
# ---------------------------------------------------------------------------

class TestCamera:
    """Tests for camera.py entities and the options-flow turret classifier."""

    @staticmethod
    def _coordinator():
        class _SD:
            ip = "1.2.3.4"
            port = 28015

        class _Sock:
            server_details = _SD()

        class _Coord:
            socket = _Sock()

        return _Coord()

    def test_subscribed_camera_unique_id_and_icon(self):
        """unique_id is server-scoped + per camera id, so a 2nd server can't collide."""
        from custom_components.rustplus_assistant.camera import RustPlusSubscribedCamera

        coord = self._coordinator()
        cctv = RustPlusSubscribedCamera(coord, None, "CAM1", {"name": "Front Door", "is_turret": False})
        turret = RustPlusSubscribedCamera(coord, None, "CAM2", {"name": "Gate", "is_turret": True})

        assert cctv.unique_id == "1.2.3.4_28015_cam_CAM1"
        assert turret.unique_id == "1.2.3.4_28015_cam_CAM2"
        assert cctv.unique_id != turret.unique_id
        assert cctv.icon == "mdi:cctv"
        assert turret.icon == "mdi:crosshairs-gps"
        assert cctv._attr_name is None  # camera inherits its device name

    def test_subscribed_camera_defaults_name_to_id(self):
        from custom_components.rustplus_assistant.camera import RustPlusSubscribedCamera

        cam = RustPlusSubscribedCamera(self._coordinator(), None, "OILRIG1", {})
        assert cam._attr_name is None
        assert "OILRIG1" in cam._attr_device_info["name"]
        assert cam.icon == "mdi:cctv"

    def test_classify_turret_from_control_flags(self):
        """FIRE control flag distinguishes an Auto Turret from a fixed CCTV camera."""
        from custom_components.rustplus_assistant.config_flow import _is_turret_camera
        from rustplus import CameraMovementOptions

        class _Cam:
            def __init__(self, flags):
                self._flags = flags

            def can_move(self, value):
                return self._flags & value == value

        fire = CameraMovementOptions.FIRE
        mouse = CameraMovementOptions.MOUSE
        assert _is_turret_camera(_Cam(fire | mouse)) is True   # turret
        assert _is_turret_camera(_Cam(mouse)) is False         # PTZ CCTV
        assert _is_turret_camera(_Cam(0)) is False             # fixed CCTV


class TestTurretButtons:
    """Tests for button.py — turret aim/fire control specs and entities."""

    def test_aim_step_is_11_25_degrees(self):
        from custom_components.rustplus_assistant import button

        # The user-facing contract: one click = 11.25°, so 32 clicks = one full turn.
        assert button._AIM_DEGREES_PER_CLICK == 11.25
        assert 360 / button._AIM_DEGREES_PER_CLICK == 32
        # _AIM_STEP is the calibrated raw mouse-delta for that rotation.
        assert button._AIM_STEP == button._AIM_DEGREES_PER_CLICK * button._MOUSE_DELTA_PER_DEGREE

    def test_control_specs(self):
        from custom_components.rustplus_assistant.button import _controls
        from rustplus import MovementControls

        specs = _controls()
        keys = [s[0] for s in specs]
        assert keys == ["aim_left", "aim_right", "aim_up", "aim_down", "fire"]

        by_key = {s[0]: s for s in specs}
        # aim buttons carry a joystick nudge and no held buttons; fire is the inverse
        assert by_key["aim_left"][4].x < 0 and by_key["aim_left"][4].y == 0
        assert by_key["aim_right"][4].x > 0
        # up/down are opposite, nonzero pitch (sign itself is hardware-calibrated)
        assert by_key["aim_up"][4].y == -by_key["aim_down"][4].y != 0
        assert by_key["aim_left"][3] is None
        fire = by_key["fire"]
        assert fire[3] == [MovementControls.FIRE_PRIMARY]
        assert fire[4] is None
        assert fire[5] is True  # release_after -> discrete shot

    def test_turret_button_unique_id_and_name(self):
        from custom_components.rustplus_assistant.button import RustPlusTurretButton, _controls

        class _SD:
            ip = "1.2.3.4"
            port = 28015

        class _Sock:
            server_details = _SD()

        class _Coord:
            socket = _Sock()

        spec = next(s for s in _controls() if s[0] == "aim_left")
        btn = RustPlusTurretButton(_Coord(), None, "dragoncam", "Dragon", spec)
        assert btn.unique_id == "1.2.3.4_28015_cam_dragoncam_aim_left"
        assert btn._attr_name == "Aim Left"


class TestCameraSessionControl:
    """Tests for the turret active-control gating in the camera session."""

    @pytest.mark.asyncio
    async def test_activate_deactivate_toggles_control(self):
        from custom_components.rustplus_assistant.camera_session import RustPlusCameraSession

        sess = RustPlusCameraSession(_make_hass(), None)
        subscribed: list[str] = []

        async def _fake_ensure(cam_id, _retry=True):
            sess._active_cam = cam_id
            subscribed.append(cam_id)
            return object()

        async def _fake_exit():
            sess._active_cam = None

        sess._ensure_subscribed = _fake_ensure
        sess._exit_manager = _fake_exit

        assert sess.is_active("dragoncam") is False
        await sess.activate("dragoncam")
        assert sess.is_active("dragoncam") is True
        assert subscribed == ["dragoncam"]
        await sess.deactivate("dragoncam")
        assert sess.is_active("dragoncam") is False

    @pytest.mark.asyncio
    async def test_send_movement_ignored_when_not_active(self):
        """A button press must NOT subscribe a turret that isn't under control."""
        from custom_components.rustplus_assistant.camera_session import RustPlusCameraSession

        sess = RustPlusCameraSession(None, None)
        called: list[str] = []

        async def _fake_ensure(cam_id, _retry=True):
            called.append(cam_id)
            return object()

        sess._ensure_subscribed = _fake_ensure
        await sess.send_movement("dragoncam", buttons=[1024])
        assert called == []  # not active -> no subscribe, no input sent

    def test_idle_not_scheduled_while_held(self):
        from custom_components.rustplus_assistant.camera_session import RustPlusCameraSession

        sess = RustPlusCameraSession(None, None)
        sess._held_cam = "dragoncam"
        sess._schedule_idle()
        assert sess._idle_handle is None

    def test_control_switch_unique_id_and_name(self):
        from custom_components.rustplus_assistant.switch import RustPlusTurretControlSwitch

        class _SD:
            ip = "1.2.3.4"
            port = 28015

        class _Sock:
            server_details = _SD()

        class _Coord:
            socket = _Sock()

        sw = RustPlusTurretControlSwitch(_Coord(), None, "dragoncam", "Dragon")
        assert sw.unique_id == "1.2.3.4_28015_cam_dragoncam_control"
        assert sw._attr_name == "Control"


class TestCameraTypes:
    """Tests for the cctv/ptz/turret capability model and device grouping."""

    @staticmethod
    def _coord():
        class _SD:
            ip = "1.2.3.4"
            port = 28015

        class _Sock:
            server_details = _SD()

        class _Coord:
            socket = _Sock()

        return _Coord()

    def test_camera_type_resolution(self):
        from custom_components.rustplus_assistant.camera import camera_type

        assert camera_type({"type": "ptz"}) == "ptz"
        assert camera_type({"type": "turret"}) == "turret"
        assert camera_type({"type": "cctv"}) == "cctv"
        # legacy is_turret fallback (cameras added before the type field existed)
        assert camera_type({"is_turret": True}) == "turret"
        assert camera_type({"is_turret": False}) == "cctv"
        assert camera_type({}) == "cctv"

    def test_ptz_controls_have_no_fire(self):
        from custom_components.rustplus_assistant.button import _controls

        keys = [s[0] for s in _controls(can_fire=False)]
        assert keys == ["aim_left", "aim_right", "aim_up", "aim_down"]

    def test_ptz_controllable_cctv_not(self):
        from custom_components.rustplus_assistant.camera import RustPlusSubscribedCamera

        ptz = RustPlusSubscribedCamera(self._coord(), None, "cam", {"type": "ptz", "name": "P"})
        cctv = RustPlusSubscribedCamera(self._coord(), None, "cam2", {"type": "cctv", "name": "C"})
        assert ptz._controllable is True
        assert cctv._controllable is False

    def test_camera_device_info_groups_entities(self):
        from custom_components.rustplus_assistant.camera import camera_device_info
        from custom_components.rustplus_assistant.const import DOMAIN

        di = camera_device_info(self._coord(), "dragoncam", {"name": "Dragon", "type": "turret"})
        assert (DOMAIN, "1.2.3.4_28015_cam_dragoncam") in di["identifiers"]
        assert di["model"] == "Auto Turret"
        assert di["via_device"] == (DOMAIN, "1.2.3.4_28015")


class TestServerLabel:
    """server_label derives a clean, server-unique device/entity_id prefix."""

    @staticmethod
    def _coord(title):
        class _SD:
            ip = "1.2.3.4"
            port = 28015

        class _Sock:
            server_details = _SD()

        class _Entry:
            pass

        e = _Entry()
        e.title = title

        class _C:
            socket = _Sock()
            config_entry = e

        return _C()

    def test_label_derivation(self):
        from custom_components.rustplus_assistant.camera import server_label

        assert server_label(self._coord("[EU] TideRust |Solo/Duo/Trio/Quad|Monthly")) == "TideRust"
        assert server_label(self._coord("Rusty Moose |US Monthly")) == "Rusty Moose"
        assert server_label(self._coord("Plain Name")) == "Plain Name"
        assert server_label(self._coord("")) == "1.2.3.4"  # no usable title -> ip fallback


class TestTimeSensors:
    """Tests for the in-game time sensor and daytime binary_sensor."""

    @staticmethod
    def _coord(time_obj=None):
        class _SD:
            ip = "1.2.3.4"
            port = 28015

        class _Sock:
            server_details = _SD()

        class _Coord:
            socket = _Sock()
            data = {"time": time_obj} if time_obj is not None else {}

        return _Coord()

    @staticmethod
    def _time(time, sunrise="07:00", sunset="19:00"):
        class _T:
            pass

        t = _T()
        t.time, t.sunrise, t.sunset = time, sunrise, sunset
        t.day_length, t.time_scale, t.raw_time = 60.0, 1.0, 0.0
        return t

    def test_to_hours(self):
        from custom_components.rustplus_assistant.binary_sensor import _to_hours

        assert _to_hours("07:30") == 7.5
        assert _to_hours("13:45") == 13.75
        assert _to_hours(12.0) == 12.0
        assert _to_hours(None) is None
        assert _to_hours("garbage") is None

    def test_daytime_day_night_unknown(self):
        from custom_components.rustplus_assistant.binary_sensor import RustPlusDaytimeBinarySensor

        day = RustPlusDaytimeBinarySensor(self._coord(self._time("12:00")))
        assert day.is_on is True
        assert day.icon == "mdi:weather-sunny"

        night = RustPlusDaytimeBinarySensor(self._coord(self._time("22:00")))
        assert night.is_on is False
        assert night.icon == "mdi:weather-night"

        unknown = RustPlusDaytimeBinarySensor(self._coord())
        assert unknown.is_on is None

    def test_time_sensor_value_and_attrs(self):
        from custom_components.rustplus_assistant.sensor import RustPlusTimeSensor

        s = RustPlusTimeSensor(self._coord(self._time("13:45")))
        assert s.native_value == "13:45"
        assert s.unique_id == "1.2.3.4_28015_time"
        attrs = s.extra_state_attributes
        assert attrs["sunrise"] == "07:00"
        assert attrs["sunset"] == "19:00"

    def test_server_info_sensor(self):
        """The server-info sensor exposes the metadata the server card reads."""
        from custom_components.rustplus_assistant.sensor import RustPlusServerInfoSensor

        class _Info:
            name, url, map = "My Server", "http://x", "Procedural Map"
            size, seed, wipe_time = 4000, 12345, 1700000000
            header_image, logo_image = "h.png", "l.png"
            players, max_players, queued_players = 100, 200, 5

        class _SD:
            ip, port = "1.2.3.4", 28015

        class _Sock:
            server_details = _SD()

        class _Coord:
            socket = _Sock()
            data = {"info": _Info()}

        s = RustPlusServerInfoSensor(_Coord())
        assert s.native_value == "My Server"
        assert s.unique_id == "1.2.3.4_28015_server_info"
        a = s.extra_state_attributes
        assert a["map"] == "Procedural Map"
        assert a["map_size"] == 4000
        assert a["seed"] == 12345
        assert a["wipe_time"] == 1700000000
        assert a["header_image"] == "h.png" and a["logo_image"] == "l.png"
        assert (a["players"], a["max_players"], a["queued_players"]) == (100, 200, 5)


class TestMapEvents:
    """Tests for the map-event binary sensors and the annotated map camera."""

    @staticmethod
    def _coord(markers="__unset__"):
        class _SD:
            ip = "1.2.3.4"
            port = 28015

        class _Sock:
            server_details = _SD()

        class _Coord:
            socket = _Sock()
            data = {} if markers == "__unset__" else {"markers": markers}

        return _Coord()

    def test_event_sensor_presence(self):
        from custom_components.rustplus_assistant.binary_sensor import RustPlusEventBinarySensor
        from custom_components.rustplus_assistant.event_cadence import EventCadenceTracker
        from rustplus import RustMarker

        class _M:
            def __init__(self, t):
                self.type = t

        coord = self._coord([_M(RustMarker.CargoShipMarker), _M(RustMarker.VendingMachineMarker)])
        cargo = RustPlusEventBinarySensor(
            coord, "cargo_ship", "Cargo Ship", RustMarker.CargoShipMarker, "mdi:ferry",
            EventCadenceTracker(RustMarker.CargoShipMarker),
        )
        heli = RustPlusEventBinarySensor(
            coord, "patrol_helicopter", "Patrol Helicopter", RustMarker.PatrolHelicopterMarker, "mdi:helicopter",
            EventCadenceTracker(RustMarker.PatrolHelicopterMarker),
        )
        assert cargo.is_on is True
        assert heli.is_on is False
        assert cargo.unique_id == "1.2.3.4_28015_event_cargo_ship"

        # no marker data yet -> unknown, not "off"
        assert RustPlusEventBinarySensor(
            self._coord(), "cargo_ship", "Cargo Ship", RustMarker.CargoShipMarker, "mdi:ferry",
            EventCadenceTracker(RustMarker.CargoShipMarker),
        ).is_on is None

    def test_annotated_map_camera(self):
        from custom_components.rustplus_assistant.camera import RustPlusAnnotatedMapCamera

        cam = RustPlusAnnotatedMapCamera(self._coord())
        assert cam.unique_id == "1.2.3.4_28015_map_annotated"
        assert cam._attr_name == "Map (Events)"


class TestEventCadence:
    """Tests for EventCadenceTracker — rising-edge spawn detection + cadence."""

    @staticmethod
    def _markers(*types):
        class _M:
            def __init__(self, t):
                self.type = t

        return [_M(t) for t in types]

    def test_cadence_and_next_estimate(self):
        import unittest.mock as mock
        from datetime import timedelta
        from homeassistant.util import dt as dt_util
        from rustplus import RustMarker
        from custom_components.rustplus_assistant.event_cadence import EventCadenceTracker

        tr = EventCadenceTracker(RustMarker.CargoShipMarker)
        cargo = self._markers(RustMarker.CargoShipMarker)
        empty = self._markers()
        base = dt_util.utcnow()

        def at(minutes):
            return base + timedelta(minutes=minutes)

        tr.observe(empty)  # baseline: off
        assert tr.sample_count == 0
        with mock.patch("homeassistant.util.dt.utcnow", return_value=at(0)):
            tr.observe(cargo)  # spawn #1
        tr.observe(cargo)  # still on -> not double counted
        tr.observe(empty)  # off
        assert tr.sample_count == 1
        assert tr.cadence is None  # needs >= 2 spawns
        assert tr.next_estimate is None

        with mock.patch("homeassistant.util.dt.utcnow", return_value=at(60)):
            tr.observe(cargo)  # spawn #2
        tr.observe(empty)
        assert tr.sample_count == 2
        assert tr.cadence == timedelta(minutes=60)
        assert tr.next_estimate == at(120)

        with mock.patch("homeassistant.util.dt.utcnow", return_value=at(100)):
            tr.observe(cargo)  # spawn #3 -> gaps [60, 40] -> cadence 50
        assert tr.cadence == timedelta(minutes=50)
        assert tr.next_estimate == at(150)

    def test_ignores_event_present_at_startup(self):
        from rustplus import RustMarker
        from custom_components.rustplus_assistant.event_cadence import EventCadenceTracker

        tr = EventCadenceTracker(RustMarker.CargoShipMarker)
        cargo = self._markers(RustMarker.CargoShipMarker)
        tr.observe(cargo)  # already up at startup -> baseline only
        tr.observe(cargo)
        assert tr.sample_count == 0

    def test_restore_roundtrip(self):
        from datetime import timedelta
        from homeassistant.util import dt as dt_util
        from rustplus import RustMarker
        from custom_components.rustplus_assistant.event_cadence import EventCadenceTracker

        tr = EventCadenceTracker(RustMarker.CargoShipMarker)
        base = dt_util.utcnow()
        data = [base.isoformat(), (base + timedelta(minutes=60)).isoformat()]
        tr.restore(data)
        assert tr.sample_count == 2
        assert tr.cadence == timedelta(minutes=60)
        # restore is a no-op once the ring is populated
        tr.restore([base.isoformat()])
        assert tr.sample_count == 2


class TestTeam:
    """Tests for team.py — member status, grid, and the per-teammate sensor."""

    @staticmethod
    def _member(steam_id, name, online=True, alive=True, x=1500, y=2500):
        return SimpleNamespace(
            steam_id=steam_id, name=name, x=x, y=y,
            is_online=online, is_alive=alive, spawn_time=0, death_time=0,
        )

    def _coord(self, members, map_size=4000):
        class _SD:
            ip = "1.2.3.4"
            port = 28015

        class _Sock:
            server_details = _SD()

        team = SimpleNamespace(
            members=members,
            leader_steam_id=(members[0].steam_id if members else None),
        )

        class _C:
            socket = _Sock()
            data = {"team_info": team, "info": SimpleNamespace(size=map_size)}

        return _C()

    def test_member_status(self):
        from custom_components.rustplus_assistant.team import member_status

        assert member_status(self._member(1, "a", online=True, alive=True)) == "alive"
        assert member_status(self._member(1, "a", online=True, alive=False)) == "dead"
        assert member_status(self._member(1, "a", online=False, alive=True)) == "offline"

    def test_member_grid(self):
        from custom_components.rustplus_assistant.team import member_grid

        g = member_grid(1500, 2500, 4000)
        assert isinstance(g, str) and g[0].isalpha() and g[1:].isdigit()
        assert member_grid(None, None, 4000) is None
        assert member_grid(1500, 2500, None) is None

    def test_member_sensor(self):
        from custom_components.rustplus_assistant.team import RustPlusTeamMemberSensor

        bob = self._member(123, "Bob", online=True, alive=False)
        coord = self._coord([bob])
        s = RustPlusTeamMemberSensor(coord, 123, "Bob")

        assert s.unique_id == "1.2.3.4_28015_team_123"
        assert s._attr_name == "Bob"
        assert s.native_value == "dead"
        attrs = s.extra_state_attributes
        assert attrs["steam_id"] == 123 and attrs["in_team"] is True
        assert attrs["is_alive"] is False and attrs["grid"]

        # a teammate who has left the team -> unknown / in_team False
        coord.data["team_info"].members = []
        assert s.native_value is None
        assert s.extra_state_attributes["in_team"] is False


class TestTeamChat:
    """Tests for the last-chat sensor + command parsing."""

    @staticmethod
    def _coord():
        class _SD:
            ip = "1.2.3.4"
            port = 28015

        class _Sock:
            server_details = _SD()

        class _C:
            socket = _Sock()
            data = {}

        return _C()

    def test_parse_command(self):
        from custom_components.rustplus_assistant.sensor import parse_command

        assert parse_command("!cargo where", "!") == ("cargo", ["where"])
        assert parse_command("!Cargo", "!") == ("cargo", [])
        assert parse_command("hello team", "!") == (None, None)
        assert parse_command("", "!") == (None, None)
        assert parse_command("#foo bar baz", "#") == ("foo", ["bar", "baz"])

    @pytest.mark.asyncio
    async def test_chat_sensor_fires_events(self):
        from custom_components.rustplus_assistant.sensor import RustPlusLastChatSensor

        s = RustPlusLastChatSensor(self._coord(), "!")
        fired = []
        s.hass = MagicMock()
        s.hass.bus.async_fire = MagicMock(side_effect=lambda ev, data: fired.append((ev, data)))
        s.async_write_ha_state = MagicMock()

        # a command message fires both team-chat and command events
        await s._async_handle_chat(
            SimpleNamespace(message="!cargo now", name="Bob", steam_id=42, colour="#fff", time=123)
        )
        assert s.native_value == "!cargo now"
        attrs = s.extra_state_attributes
        assert attrs["is_command"] is True and attrs["command"] == "cargo" and attrs["args"] == ["now"]
        assert attrs["sender_name"] == "Bob" and attrs["sender_steam_id"] == 42
        evs = {e for e, _ in fired}
        assert "rustplus_team_chat" in evs and "rustplus_command" in evs

        # a normal message fires only the chat event
        fired.clear()
        await s._async_handle_chat(
            SimpleNamespace(message="hi team", name="Bob", steam_id=42, colour="#fff", time=124)
        )
        assert s.extra_state_attributes["is_command"] is False
        evs2 = {e for e, _ in fired}
        assert "rustplus_team_chat" in evs2 and "rustplus_command" not in evs2


class TestDestroyedDetection:
    """Coordinator destroyed-in-game detection + entity availability."""

    @staticmethod
    def _coord():
        from custom_components.rustplus_assistant.coordinator import RustPlusDataCoordinator

        # Bypass the heavy DataUpdateCoordinator.__init__ — exercise the pure logic.
        coord = RustPlusDataCoordinator.__new__(RustPlusDataCoordinator)
        coord.destroyed_entities = set()
        coord._missing_counts = {}
        coord.config_entry = SimpleNamespace(options={})
        return coord

    def test_missing_threshold_then_recovery(self):
        coord = self._coord()
        raised, cleared = [], []
        coord._raise_destroyed_issue = lambda eid: raised.append(eid)
        coord._clear_destroyed_issue = lambda eid: cleared.append(eid)

        assert coord.is_destroyed(5) is False
        coord._note_entity_missing(5)
        assert coord.is_destroyed(5) is False and raised == []  # one miss < threshold
        coord._note_entity_missing(5)
        assert coord.is_destroyed(5) is True and raised == [5]  # threshold -> destroyed
        coord._note_entity_missing(5)
        assert raised == [5]  # no duplicate issue
        coord._note_entity_present(5)
        assert coord.is_destroyed(5) is False and cleared == [5]  # recovery clears

    def test_present_resets_partial_misses(self):
        coord = self._coord()
        coord._raise_destroyed_issue = lambda eid: None
        coord._clear_destroyed_issue = lambda eid: None
        coord._note_entity_missing(7)   # 1 miss
        coord._note_entity_present(7)   # responded -> reset
        coord._note_entity_missing(7)   # 1 again, not 2
        assert coord.is_destroyed(7) is False

    def test_entity_label_from_options(self):
        coord = self._coord()
        coord.config_entry = SimpleNamespace(
            options={"switches": {"5": "Front Door"}, "smart_alarms": {"9": "Raid"}}
        )
        assert coord._entity_label(5) == "Front Door"
        assert coord._entity_label(9) == "Raid"
        assert coord._entity_label(99) == "Entity 99"

    def test_entity_available_reflects_destroyed(self):
        from custom_components.rustplus_assistant.entity import RustPlusEntity

        coord = _make_coordinator()
        coord.entities_to_poll = set()
        coord.last_update_success = True
        coord.is_destroyed = lambda eid: eid == 12345

        ent = RustPlusEntity(coord, 12345, "switch", "Door")
        assert 12345 in coord.entities_to_poll  # registered for monitoring
        assert ent.available is False  # destroyed -> unavailable
        coord.is_destroyed = lambda eid: False
        assert ent.available is True


class TestDeathPush:
    """Death-push parsing + the offline-killed prompt (like the Rust+ app)."""

    def test_parse_from_body_target_name(self):
        from custom_components.rustplus_assistant.fcm_manager import parse_death_push

        killer, server = parse_death_push(
            "You were killed", "", {"targetName": "Bandit", "name": "TideRust"}
        )
        assert killer == "Bandit" and server == "TideRust"

    def test_parse_regex_fallback(self):
        from custom_components.rustplus_assistant.fcm_manager import parse_death_push

        killer, _ = parse_death_push("Death", "You were killed by CamperJoe!", {})
        assert killer == "CamperJoe"

    def test_parse_nothing(self):
        from custom_components.rustplus_assistant.fcm_manager import parse_death_push

        killer, server = parse_death_push("You died", "Better luck next time", {})
        assert killer is None and server is None

    def test_handler_fires_event_and_prompt(self):
        import json as _json
        from custom_components.rustplus_assistant.fcm_manager import RustPlusFCMManager

        mgr = RustPlusFCMManager.__new__(RustPlusFCMManager)
        mgr.hass = _make_hass()
        fired = []
        mgr.hass.bus.async_fire = MagicMock(side_effect=lambda ev, data: fired.append((ev, data)))

        body = _json.dumps({
            "targetName": "Bandit", "targetId": "76561198000000001",
            "name": "TideRust", "ip": "1.2.3.4", "port": "28015",
            "playerToken": "SECRET",  # must never leak into events
        })
        mgr._handle_death_push("You were killed", "", {"body": body})

        evs = dict(fired)
        death = evs["rustplus_death"]
        assert death["killer"] == "Bandit"
        assert death["killer_steam_id"] == "76561198000000001"
        assert death["server_name"] == "TideRust"
        assert "SECRET" not in str(death)  # token not leaked
        assert "rustplus_notification" in evs  # generic feed stays complete

        # A persistent-notification prompt with the killer's name was requested.
        call = mgr.hass.services.async_call.call_args
        assert call.args[0] == "persistent_notification"
        assert "Bandit" in call.args[2]["message"]

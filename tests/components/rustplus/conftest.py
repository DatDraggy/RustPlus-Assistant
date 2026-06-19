"""Fixtures for Rust+ tests."""
from unittest.mock import AsyncMock, patch
import pytest

@pytest.fixture(autouse=True)
def auto_mock_rustsocket():
    """Mock RustSocket."""
    with patch("homeassistant.components.rustplus.RustSocket") as mock_socket:
        mock_instance = mock_socket.return_value
        mock_instance.connect = AsyncMock()
        mock_instance.disconnect = AsyncMock()
        mock_instance.get_info = AsyncMock(return_value={"name": "Test Server"})
        mock_instance.get_time = AsyncMock(return_value={"time": "12:00"})
        yield mock_instance

@pytest.fixture(autouse=True)
def auto_mock_fcm_listener():
    """Mock FCMListener."""
    with patch("homeassistant.components.rustplus.fcm_manager.FCMListener") as mock_listener:
        yield mock_listener

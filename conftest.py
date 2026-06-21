"""Pytest bootstrap for the Rust+ Assistant test suite.

The tests import the integration directly (e.g.
``custom_components.rustplus_assistant.sensor``), so the repository root must be
importable. A root-level ``conftest.py`` is enough for pytest's default import
mode; we also insert the root explicitly to stay robust regardless of mode.

When the suite is migrated to the standard Home Assistant fixture style, enable
the ``enable_custom_integrations`` fixture from
``pytest_homeassistant_custom_component`` for tests that load the integration
through a real ``hass`` instance.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

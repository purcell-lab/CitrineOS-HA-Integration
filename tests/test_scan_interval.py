"""Polling interval comes from the options, else the value chosen at setup."""

from custom_components.citrine_ha.const import CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
from custom_components.citrine_ha.coordinator import scan_interval_seconds


def test_setup_value_is_used_when_options_are_empty():
    # A new entry has empty options; the setup form stored 5 s in the data.
    assert scan_interval_seconds({CONF_SCAN_INTERVAL: 5}, {}) == 5


def test_options_override_setup_value():
    assert scan_interval_seconds({CONF_SCAN_INTERVAL: 5}, {CONF_SCAN_INTERVAL: 1}) == 1


def test_default_and_floor():
    assert scan_interval_seconds({}, {}) == DEFAULT_SCAN_INTERVAL
    assert scan_interval_seconds({}, {CONF_SCAN_INTERVAL: 0}) == 1
    assert scan_interval_seconds({}, {CONF_SCAN_INTERVAL: 2.0}) == 2

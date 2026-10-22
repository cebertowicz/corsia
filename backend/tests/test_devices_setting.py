"""Tests for the pure parser shared by the CLI and settings helpers."""

import pytest

from infra.devices_setting import DEVICE_SETTING_KEY, parse_devices_setting


def test_parse_devices_setting_keeps_the_canonical_key_and_valid_address():
    enabled, address = parse_devices_setting(
        '{"enabled": true, "outside_address": "home.example.com:8443"}'
    )

    assert DEVICE_SETTING_KEY == "devices"
    assert (enabled, address) == (True, "home.example.com:8443")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, (False, None)),
        ("not-json", (False, None)),
        ("[]", (False, None)),
        ({}, (False, None)),
        ({"enabled": False, "outside_address": "home.example.com"}, (False, "home.example.com")),
        ({"enabled": True, "outside_address": ""}, (True, None)),
        ({"enabled": True, "outside_address": 42}, (True, None)),
    ],
)
def test_parse_devices_setting_preserves_enabled_and_address_values(value, expected):
    assert parse_devices_setting(value) == expected

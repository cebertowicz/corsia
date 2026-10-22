"""Pure parsing for the persisted Devices setting."""

import json


DEVICE_SETTING_KEY = "devices"


def parse_devices_setting(value: object) -> tuple[bool, str | None]:
    """Parse stored Devices settings into the enabled flag and outside address."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            value = None
    if not isinstance(value, dict):
        return False, None
    address = value.get("outside_address")
    return value.get("enabled") is True, address if isinstance(address, str) and address else None

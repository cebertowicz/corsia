"""The desktop app declares each macOS privacy permission it may ask for.

A usage string without its hardened-runtime entitlement (or the reverse) makes
macOS refuse the access without asking, so each pair is checked together.
"""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

SRC_TAURI = Path(__file__).resolve().parents[2] / "desktop" / "src-tauri"

# Usage-string key -> the hardened-runtime entitlement the same access needs (None: none exists).
DECLARED = {
    "NSMicrophoneUsageDescription": "com.apple.security.device.audio-input",
    "NSAudioCaptureUsageDescription": None,
    "NSAppleEventsUsageDescription": "com.apple.security.automation.apple-events",
}


def _plist(name: str) -> dict:
    return plistlib.loads((SRC_TAURI / name).read_bytes())


@pytest.mark.parametrize("usage_key,entitlement", sorted(DECLARED.items()))
def test_each_permission_has_its_usage_string_and_entitlement(usage_key: str, entitlement: str | None) -> None:
    info = _plist("Info.plist")
    assert info.get(usage_key, "").startswith("CORSIA "), usage_key
    if entitlement is not None:
        assert _plist("Entitlements.plist").get(entitlement) is True, entitlement

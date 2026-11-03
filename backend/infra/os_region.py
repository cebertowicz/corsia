"""The computer's region setting, as a two-letter country code."""

from __future__ import annotations

import os
import plistlib
import re
import sys
from pathlib import Path

#: The 27 EU member states plus Iceland, Liechtenstein and Norway.
EU_EEA_REGIONS = frozenset({
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT",
    "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE", "IS", "LI", "NO",
})

_MAC_GLOBAL_PREFERENCES = Path.home() / "Library" / "Preferences" / ".GlobalPreferences.plist"


def _region_of(locale_id: str) -> str | None:
    """`de_DE.UTF-8`, `zh-Hans_DE` or `en_US@rg=dezzzz` (macOS region set apart from language) to `DE`."""
    if override := re.search(r"@rg=([A-Za-z]{2})", locale_id):
        return override.group(1).upper()
    subtags = re.split(r"[_-]", re.split(r"[.@]", locale_id, maxsplit=1)[0])[1:]
    return next((tag.upper() for tag in reversed(subtags) if len(tag) == 2 and tag.isalpha()), None)


def _locale_id() -> str:
    if sys.platform == "darwin":
        try:
            with _MAC_GLOBAL_PREFERENCES.open("rb") as handle:
                value = plistlib.load(handle).get("AppleLocale")
            if isinstance(value, str):
                return value
        except (OSError, plistlib.InvalidFileException, ValueError):
            pass
    return os.environ.get("LC_ALL") or os.environ.get("LANG") or ""


def os_region() -> str | None:
    """The Region in macOS settings, else the region part of LC_ALL or LANG; None when unreadable."""
    return _region_of(_locale_id())

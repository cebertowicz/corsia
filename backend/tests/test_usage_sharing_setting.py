"""Usage sharing: on until the person turns it off (off by default in the EU/EEA), saved on the settings PATCH."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from auth_fixtures import OWNER_SESSION_TOKEN

pytestmark = pytest.mark.usefixtures('signed_in_defaults')
HEADERS = {"Authorization": f"Bearer {OWNER_SESSION_TOKEN}"}


def _client() -> TestClient:
    from api.system import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.fixture
def region(monkeypatch):
    def set_region(code):
        monkeypatch.setattr("infra.settings.os_region", lambda: code)
    set_region("US")
    return set_region


def test_sharing_is_on_until_the_person_turns_it_off(isolated_db, region):
    with _client() as client:
        initial = client.get("/api/settings", headers=HEADERS)
        off = client.patch("/api/settings", headers=HEADERS, json={"usage_sharing": False})
        after_off = client.get("/api/settings", headers=HEADERS)
        on = client.patch("/api/settings", headers=HEADERS, json={"usage_sharing": True})
        after_on = client.get("/api/settings", headers=HEADERS)

    assert initial.json()["usage_sharing"] is True
    assert off.json()["usage_sharing"] is False
    assert after_off.json()["usage_sharing"] is False
    assert on.json()["usage_sharing"] is True
    assert after_on.json()["usage_sharing"] is True


@pytest.mark.parametrize("code,default", [("DE", False), ("NO", False), ("US", True), ("GB", True), (None, True)])
def test_the_default_is_off_only_in_the_eu_or_eea(isolated_db, region, code, default):
    from infra.settings import get_usage_sharing, save_usage_sharing

    region(code)
    assert get_usage_sharing() is default
    save_usage_sharing(not default)
    assert get_usage_sharing() is (not default)


@pytest.mark.parametrize("locale_id,code", [
    ("en_US", "US"), ("de_DE.UTF-8", "DE"), ("en_US@rg=frzzzz", "FR"), ("zh-Hans_CN", "CN"), ("en_US_POSIX", "US"),
    ("en-GB", "GB"), ("C.UTF-8", None), ("", None), ("en", None),
])
def test_the_region_is_read_from_the_locale(locale_id, code):
    from infra.os_region import _region_of

    assert _region_of(locale_id) == code


@pytest.fixture
def mac_preferences(tmp_path, monkeypatch):
    """os_region() on a Mac whose preferences file is `tmp_path`'s, with no locale variables."""
    from infra import os_region as module

    path = tmp_path / ".GlobalPreferences.plist"
    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(module, "_MAC_GLOBAL_PREFERENCES", path)
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LANG", raising=False)
    module.os_region.cache_clear()
    yield path
    module.os_region.cache_clear()


def test_the_mac_region_setting_wins_over_lang(mac_preferences, monkeypatch):
    import plistlib

    from infra.os_region import os_region

    mac_preferences.write_bytes(plistlib.dumps({"AppleLocale": "en_US@rg=dezzzz"}))
    monkeypatch.setenv("LANG", "en_US.UTF-8")

    assert os_region() == "DE"


def test_preferences_without_a_region_fall_back_to_lang(mac_preferences, monkeypatch):
    import plistlib

    from infra.os_region import os_region

    mac_preferences.write_bytes(plistlib.dumps({"AppleLanguages": ["fr"]}))
    monkeypatch.setenv("LANG", "fr_FR.UTF-8")

    assert os_region() == "FR"


def test_an_unreadable_preferences_file_is_logged_and_falls_back(mac_preferences, monkeypatch, caplog):
    from infra.os_region import os_region

    mac_preferences.write_bytes(b"not a plist")
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")

    with caplog.at_level("WARNING", logger="infra.os_region"):
        assert os_region() == "DE"
    assert "macOS region setting unreadable" in caplog.text


def test_the_region_is_read_once_per_process(mac_preferences):
    import plistlib

    from infra.os_region import os_region

    mac_preferences.write_bytes(plistlib.dumps({"AppleLocale": "de_DE"}))
    assert os_region() == "DE"
    mac_preferences.unlink()

    assert os_region() == "DE"


def test_a_non_boolean_is_refused_and_nothing_is_saved(isolated_db):
    from infra.settings import USAGE_SHARING_KEY, get_setting

    with _client() as client:
        invalid = client.patch("/api/settings", headers=HEADERS, json={"usage_sharing": "no"})

    assert invalid.status_code == 400
    assert get_setting(USAGE_SHARING_KEY) is None


@pytest.mark.parametrize("raw", ['{"enabled": "off"}', "not json", "[false]"])
def test_a_damaged_saved_value_reads_as_off(isolated_db, raw):
    from infra.database import get_db
    from infra.settings import USAGE_SHARING_KEY, get_usage_sharing

    db = get_db()
    db.execute("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, '2026-11-03T00:00:00Z')",
               (USAGE_SHARING_KEY, raw))
    db.commit()

    assert get_usage_sharing() is False

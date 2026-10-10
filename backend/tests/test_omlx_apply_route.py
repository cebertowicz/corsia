"""Applying a foreign oMLX's settings on the person's yes: one flat write, verified by a re-read."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import httpx
import pytest

from llm import local_setup, local_setup_ownership, omlx_global_settings
from llm.local_profiles import OMLX_GLOBAL_SETTINGS
from llm.providers import omlx_settings
from tests.test_provider_sources import client  # noqa: F401

URL = "/api/providers/sources/omlx/settings/apply"
KEY = "sk-admin-secret-0123"
SERVER = {"id": "srv", "provider": "omlx", "kind": "local", "status": "online", "api_key": KEY}
REAL_OWNERSHIP_CHECK = local_setup_ownership.existing_install_is_foreign
RECOMMENDED = OMLX_GLOBAL_SETTINGS["max_concurrent_requests"]


class FakeOmlx:
    """A stand-in for oMLX (0.4.1 and 0.7.0 agree here): flat POST, nested GET, runtime_applied only for live settings."""

    def __init__(self):
        self.value = 8
        self.posts: list[dict] = []
        self.reads = 0
        self.post_status = 200
        self.post_body: object = {"runtime_applied": []}
        self.post_error: Exception | None = None
        self.persist = True
        self.read_status = 200

    async def admin_request(self, _cfg, method, path, *, json=None, timeout=None):
        """The shared admin call: sign-in is its job, so the fake sees only the route."""
        assert path == "/global-settings"
        if method == "POST":
            if self.post_error:
                raise self.post_error
            self.posts.append(json)
            if self.post_status == 200 and self.persist:
                self.value = json["max_concurrent_requests"]
            return _Response(self.post_status, self.post_body)
        self.reads += 1
        return _Response(self.read_status, {"scheduler": {"max_concurrent_requests": self.value}})


class _Response:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.content = b"x"

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


@pytest.fixture
def omlx(monkeypatch):
    fake = FakeOmlx()
    monkeypatch.setattr(omlx_settings, "admin_request", fake.admin_request)
    monkeypatch.setattr(omlx_global_settings, "_observed", {"srv": {"scheduler": {"max_concurrent_requests": 8}}})
    monkeypatch.setattr("api.provider_sources.network_discovery.get_servers", lambda **_kw: [dict(SERVER)])
    monkeypatch.setattr("llm.local_setup_ownership.existing_install_is_foreign", lambda: True)
    return fake


def _fingerprint():
    return omlx_global_settings.fingerprint("srv", omlx_global_settings.differences(["srv"]))


def _apply(client, body=None):  # noqa: F811
    return client.post(URL, json={"fingerprint": _fingerprint()} if body is None else body)


def test_a_yes_sends_one_flat_write_then_rereads_and_saves(client, omlx):  # noqa: F811
    omlx.post_body = {"runtime_applied": []}

    response = _apply(client)

    assert response.status_code == 200
    assert omlx.posts == [{"max_concurrent_requests": RECOMMENDED}]
    assert omlx.reads == 1
    body = response.json()
    assert body["results"] == [{"setting": "max_concurrent_requests", "saved": True, "active": False}]
    assert body["verified"] is True
    assert body["omlx_settings"]["differences"] == []


def test_a_fingerprint_mismatch_is_409_and_writes_nothing(client, omlx):  # noqa: F811
    response = _apply(client, {"fingerprint": "0" * 64})

    assert response.status_code == 409
    assert omlx.posts == []


@pytest.mark.parametrize("body", [
    {}, {"fingerprint": ""}, {"fingerprint": 5}, {"fingerprint": "x", "setting": "host"},
    {"fingerprint": "x", "max_concurrent_requests": 99}, {"other": "x"},
])
def test_any_body_but_exactly_a_fingerprint_is_400_and_writes_nothing(client, omlx, body):  # noqa: F811
    response = client.post(URL, json=body)

    assert response.status_code == 400
    assert omlx.posts == []


@pytest.mark.parametrize("servers", [[], [SERVER, {**SERVER, "id": "other"}], [{**SERVER, "status": "offline"}]])
def test_not_exactly_one_online_foreign_server_is_409(client, omlx, monkeypatch, servers):  # noqa: F811
    fingerprint = _fingerprint()
    monkeypatch.setattr("api.provider_sources.network_discovery.get_servers", lambda **_kw: servers)

    assert client.post(URL, json={"fingerprint": fingerprint}).status_code == 409
    assert omlx.posts == []


def test_a_corsia_installed_omlx_is_refused(client, omlx, monkeypatch, tmp_path):  # noqa: F811
    marker = tmp_path / local_setup_ownership.MARKER_NAME
    marker.write_text("installed")
    monkeypatch.setattr("llm.local_setup_ownership.existing_install_is_foreign",
                        REAL_OWNERSHIP_CHECK)
    monkeypatch.setattr(local_setup_ownership, "marker_path", lambda: marker)
    monkeypatch.setattr(local_setup_ownership.shutil, "which", lambda _name: "/opt/homebrew/bin/omlx")
    monkeypatch.setattr(local_setup_ownership, "setup_owns_current_omlx", lambda *_args: True)

    assert REAL_OWNERSHIP_CHECK() is False
    assert client.post(URL, json={"fingerprint": _fingerprint()}).status_code == 409
    assert omlx.posts == []


def test_unknown_ownership_is_not_foreign_and_is_refused(client, omlx, monkeypatch, tmp_path):  # noqa: F811
    """No oMLX on PATH while a server is online: ownership is unknown, and the check returns False."""
    monkeypatch.setattr("llm.local_setup_ownership.existing_install_is_foreign",
                        REAL_OWNERSHIP_CHECK)
    monkeypatch.setattr(local_setup_ownership.shutil, "which", lambda _name: None)
    monkeypatch.setattr(local_setup_ownership, "marker_path", lambda: tmp_path / "absent")

    assert REAL_OWNERSHIP_CHECK() is False
    assert client.post(URL, json={"fingerprint": _fingerprint()}).status_code == 409
    assert omlx.posts == []


@pytest.mark.parametrize("error", [httpx.ConnectError(f"refused with {KEY}"), RuntimeError(KEY)])
def test_unreachable_oMLX_gets_a_fixed_message(client, omlx, error):  # noqa: F811
    omlx.post_error = error

    response = _apply(client)

    assert response.status_code == 503
    assert response.json()["detail"] == "oMLX is unreachable"


def test_a_200_that_changes_nothing_is_not_saved(client, omlx):  # noqa: F811
    omlx.persist = False

    body = _apply(client).json()

    assert body["results"] == [{"setting": "max_concurrent_requests", "saved": False, "active": False}]
    assert body["verified"] is True
    assert body["omlx_settings"]["differences"][0]["current"] == 8


def test_a_setting_missing_from_runtime_applied_is_not_active(client, omlx):  # noqa: F811
    omlx.post_body = {"runtime_applied": ["log_level"]}

    result = _apply(client).json()["results"][0]

    assert result["saved"] is True
    assert result["active"] is False


def test_a_setting_in_runtime_applied_is_active(client, omlx):  # noqa: F811
    omlx.post_body = {"runtime_applied": ["max_concurrent_requests"]}

    assert _apply(client).json()["results"][0]["active"] is True


def test_a_non_json_200_still_relies_on_the_reread(client, omlx):  # noqa: F811
    omlx.post_body = None

    response = _apply(client)

    assert response.status_code == 200
    assert response.json()["results"] == [{"setting": "max_concurrent_requests", "saved": True, "active": False}]


def test_a_failed_reread_is_unverified_and_never_saved_or_active(client, omlx):  # noqa: F811
    omlx.read_status = 500
    omlx.post_body = {"runtime_applied": ["max_concurrent_requests"]}

    body = _apply(client).json()

    assert body["verified"] is False
    assert body["results"] == [{"setting": "max_concurrent_requests", "saved": False, "active": False}]


@pytest.mark.parametrize("status", [401, 403, 500])
def test_omlx_statuses_are_never_passed_through(client, omlx, status):  # noqa: F811
    omlx.post_status = status
    omlx.post_body = {"detail": f"raw body with {KEY}"}

    response = _apply(client)

    assert response.status_code == 502
    assert response.json()["detail"] == f"oMLX could not save settings (oMLX HTTP {status})"
    assert KEY not in response.text and "raw body" not in response.text


def test_the_admin_key_never_appears_in_responses_or_logs(client, omlx, caplog):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    texts = [_apply(client).text]
    omlx.post_status = 401
    omlx.post_body = {"detail": KEY}
    texts.append(_apply(client, {"fingerprint": "0" * 64}).text)
    texts.append(_apply(client).text)
    omlx.post_error = httpx.ConnectError(KEY)
    texts.append(_apply(client).text)

    assert all(KEY not in text for text in texts)
    assert KEY not in caplog.text


def test_the_installed_path_still_raises_through_the_shared_writer(omlx, monkeypatch):
    asyncio.run(local_setup._configure_global("srv"))
    assert omlx.posts == [{**OMLX_GLOBAL_SETTINGS, "skip_api_key_verification": True}]

    omlx.post_status = 500
    with pytest.raises(RuntimeError, match="oMLX could not save settings"):
        asyncio.run(local_setup._configure_global("srv"))

    omlx.post_error = httpx.ConnectError("down")
    with pytest.raises(RuntimeError, match="oMLX is unreachable"):
        asyncio.run(local_setup._configure_global("srv"))


def test_update_settings_still_refuses_writes():
    assert asyncio.run(omlx_settings.update_settings({}, {"max_concurrent_requests": 1}))["ok"] is False

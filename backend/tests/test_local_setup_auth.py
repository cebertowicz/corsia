"""Authentication boundaries for CORSIA's private managed oMLX server."""

from __future__ import annotations

import asyncio
import json
import subprocess
from types import SimpleNamespace

import httpx
import pytest

from api.network_servers import _public_server_view
from llm import local_setup_auth
from llm.providers.omlx_endpoints import OmlxAdminError
from omlx_admin_fixtures import KEY


@pytest.mark.parametrize("output, returncode, expected", [
    ("0.7.0.dev4", 0, False), ("0.7.0rc1", 0, True), ("0.7.0", 0, True), ("0.6.4", 0, False),
    ("not-a-version", 0, False), ("0.7.0", 1, False),
])
def test_only_a_compatible_omlx_release_passes_the_auth_version_check(monkeypatch, output, returncode, expected):
    monkeypatch.setattr(
        local_setup_auth.subprocess, "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], returncode, stdout=output),
    )

    assert local_setup_auth._omlx_supports_loopback_auth() is expected


def test_managed_version_uses_the_bundled_binary_not_path(monkeypatch):
    bundled = "/Applications/CORSIA.app/Contents/Resources/bin/omlx"
    commands: list[list[str]] = []
    monkeypatch.setattr(local_setup_auth, "managed_omlx_executable", lambda: bundled)
    monkeypatch.setattr(
        local_setup_auth.subprocess,
        "run",
        lambda command, **_kwargs: (commands.append(command) or subprocess.CompletedProcess(command, 0, stdout="0.7.0rc1")),
    )

    assert local_setup_auth.omlx_version() == "0.7.0rc1"
    assert commands == [[bundled, "--version"]]


def test_existing_omlx_version_uses_only_the_persons_path_binary(monkeypatch):
    person_omlx = "/opt/homebrew/bin/omlx"
    commands: list[list[str]] = []
    monkeypatch.setattr(local_setup_auth, "installed_omlx", lambda: person_omlx)
    monkeypatch.setattr(
        local_setup_auth.subprocess,
        "run",
        lambda command, **_kwargs: (commands.append(command) or subprocess.CompletedProcess(command, 0, stdout="0.7.0rc1")),
    )

    assert local_setup_auth.existing_omlx_version() == "0.7.0rc1"
    assert commands == [[person_omlx, "--version"]]


def test_an_unqueryable_omlx_version_fails_closed(monkeypatch):
    monkeypatch.setattr(
        local_setup_auth.subprocess, "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired("omlx", 5)),
    )

    assert local_setup_auth._omlx_supports_loopback_auth() is False


@pytest.mark.parametrize("hosts, expected", [
    ("127.0.0.1", True), ("localhost", True), ("::1", True), ("127.0.0.1, ::1", True),
    ("::ffff:127.0.0.1", True), ("0.0.0.0", False), ("::", False), ("192.168.1.5", False),
    ("127.0.0.1,0.0.0.0", False), ("", False), (None, False), ("my-mac.local", False),
])
def test_only_a_loopback_bind_counts_as_this_mac_only(hosts, expected):
    assert local_setup_auth._loopback_only(hosts) is expected


def test_managed_server_auth_never_configures_a_non_loopback_bind(monkeypatch):
    async def setup_key(_cfg, _key):
        return None

    async def settings(_cfg, _method, _path):
        return SimpleNamespace(status_code=200, json=lambda: {"server": {"host": "0.0.0.0"}})

    monkeypatch.setattr(local_setup_auth, "setup_admin_key", setup_key)
    monkeypatch.setattr(local_setup_auth, "admin_request", settings)

    with pytest.raises(OmlxAdminError, match=local_setup_auth.NOT_LOOPBACK):
        asyncio.run(local_setup_auth.secure_local_omlx("managed", KEY))


def test_managed_server_auth_rejects_a_failed_admin_sign_in(monkeypatch):
    async def setup_key(_cfg, _key):
        return None

    async def denied(_cfg, _method, _path):
        return SimpleNamespace(status_code=401)

    monkeypatch.setattr(local_setup_auth, "setup_admin_key", setup_key)
    monkeypatch.setattr(local_setup_auth, "admin_request", denied)

    with pytest.raises(OmlxAdminError, match="HTTP 401"):
        asyncio.run(local_setup_auth.secure_local_omlx("managed", KEY))


def test_the_public_server_view_carries_no_key():
    row = {"id": "ns-1", "metadata_json": json.dumps({"api_key": KEY, "token": "t0ken-value", "note": "ok"})}

    view = _public_server_view(dict(row), {"ns-1": KEY})

    assert KEY not in json.dumps(view) and "t0ken-value" not in json.dumps(view)
    assert view["has_key"] is True and view["masked_key"] != KEY and view["metadata"] == {"note": "ok"}


@pytest.mark.parametrize("api_key", [KEY, None])
def test_the_add_probe_sends_the_key_when_it_has_one(monkeypatch, api_key):
    from api import network_server_helpers
    from api.network_server_helpers import _add_omlx_server
    from llm import network_discovery

    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("authorization")))
        return httpx.Response(200, json={"data": []})

    real_init = httpx.AsyncClient.__init__
    monkeypatch.setattr(
        httpx.AsyncClient, "__init__",
        lambda self, *a, **k: real_init(self, *a, **{**k, "transport": httpx.MockTransport(handler)}),
    )
    monkeypatch.setattr(network_discovery, "upsert_server", lambda **_kw: "ns-1")
    monkeypatch.setattr(network_server_helpers, "set_server_key", lambda *_a, **_kw: None)

    async def discovered(_sid):
        return None

    monkeypatch.setattr(network_discovery, "discover_and_notify", discovered)

    asyncio.run(_add_omlx_server(SimpleNamespace(hostname="127.0.0.1", port=8000, name="Mac", api_key=api_key)))

    health = [auth for path, auth in seen if path == "/health"]
    models = [auth for path, auth in seen if path == "/v1/models"]
    expected = [f"Bearer {api_key}"] if api_key else [None]
    assert health == expected
    assert models == expected

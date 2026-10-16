"""Local setup signs in to the oMLX it installs, and never touches the person's own.

The fake oMLX (``omlx_admin_fixtures``) mirrors 0.7.0. Setup registers a generated
key first, gives it to oMLX through ``setup-api-key``, then writes
``skip_api_key_verification`` once, in the same call as the starter settings.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess

import httpx
import pytest

from api.network_servers import _public_server_view
from llm import local_setup, local_setup_auth, local_setup_operation
from llm.providers import omlx_endpoints as ep
from omlx_admin_fixtures import KEY, OTHER_KEY, omlx  # noqa: F401

REAL_VERSION_GUARD = local_setup._omlx_supports_loopback_auth
REAL_OMLX_VERSION = local_setup_auth.omlx_version
STARTER_WRITE = {
    "max_concurrent_requests": 1, "prefill_priority": "context", "skip_api_key_verification": True,
}
SAFE_LEAKS = ("session-", "setup-cookie", "omlx_admin_session=")


class Wiring:
    def __init__(self):
        self.registry: dict[str, str] = {}
        self.registered: list[tuple[str | None, str | None]] = []  # (key CORSIA sent, key oMLX then held)
        self.emitted: list[str] = []


@pytest.fixture
def wiring(monkeypatch, omlx):  # noqa: F811
    """A fresh oMLX, an in-memory registry, and every stage but the oMLX calls stubbed."""
    omlx.key = None
    local_setup_operation.clear()
    wired = Wiring()

    async def register(*, port=8000, api_key=None):
        wired.registered.append((api_key, omlx.key))
        if api_key:
            wired.registry["key"] = api_key
        return "local-server"

    async def stored(_host, _port):
        return wired.registry.get("key")

    async def admin_key(cfg):
        return cfg.get("api_key") or wired.registry.get("key")

    async def discover(_server_id):
        return {"ok": True}

    async def healthy(port=8000):
        return None

    async def download(_server_id, _progress, _profile=None):
        return None

    async def model_settings(_server_id, _window, _profile=None):
        return None

    monkeypatch.setattr(local_setup, "_flight", local_setup._Flight())
    monkeypatch.setattr(local_setup, "_emit", lambda action, **_p: wired.emitted.append(action))
    monkeypatch.setattr(local_setup, "profile_eligibility", lambda *_args: local_setup.Eligibility(40960, True))
    monkeypatch.setattr(local_setup, "existing_install_is_foreign", lambda: False)
    monkeypatch.setattr(local_setup, "_ensure_omlx", lambda _progress, **_kwargs: None)
    monkeypatch.setattr(local_setup, "_omlx_supports_loopback_auth", lambda: True)
    monkeypatch.setattr(local_setup, "omlx_version", lambda: "0.7.0")
    monkeypatch.setattr(local_setup_auth, "omlx_version", lambda: "0.7.0")
    monkeypatch.setattr(local_setup, "_run", lambda _command: None)
    monkeypatch.setattr(local_setup, "_wait_for_health", healthy)
    monkeypatch.setattr(local_setup, "_wait_for_download", download)
    monkeypatch.setattr(local_setup, "_configure_model_settings", model_settings)
    monkeypatch.setattr(local_setup, "_register_loopback_server", register)
    monkeypatch.setattr(local_setup, "registered_local_key", stored)
    monkeypatch.setattr(ep, "_admin_key", admin_key)
    monkeypatch.setattr(ep, "_resolve_endpoint", lambda _cfg: ("127.0.0.1", 8000))
    monkeypatch.setattr("llm.catalog_network_models.discover_network_models", discover)
    monkeypatch.setattr("infra.state.ws.notify_models_changed", lambda: None)
    return wired


async def _finished(**kwargs) -> dict:
    first = await local_setup.begin_local_setup(**kwargs)
    task = local_setup._flight.task
    return await task if task is not None else first


def _run_setup() -> dict:
    local_setup._flight = local_setup._Flight()
    return asyncio.run(_finished())


def _no_secret(text: str, *secrets: str) -> None:
    for secret in (*secrets, *SAFE_LEAKS):
        assert secret not in text


def test_a_fresh_install_stores_the_key_first_then_sets_it_and_the_skip_flag_once(wiring, omlx, caplog):  # noqa: F811
    caplog.set_level(logging.DEBUG)

    result = _run_setup()

    assert result["ok"] is True
    generated, held_by_omlx = wiring.registered[0]
    assert generated and len(generated) >= 32 and held_by_omlx is None  # stored before oMLX knew it
    assert omlx.key == generated == wiring.registry["key"]
    assert omlx.paths.count("/admin/api/setup-api-key") == 1
    assert omlx.paths.index("/admin/api/setup-api-key") < omlx.paths.index("/admin/api/login")
    assert omlx.writes("/global-settings") == [STARTER_WRITE]  # one write: starter settings and the flag
    assert omlx.skip is True
    assert wiring.emitted == ["started", "completed"]
    _no_secret(caplog.text + json.dumps(result, default=str), generated)


def test_a_retry_reuses_the_stored_key_and_overwrites_nothing(wiring, omlx):  # noqa: F811
    assert _run_setup()["ok"] is True
    first_key = omlx.key

    result = _run_setup()

    assert result["ok"] is True
    assert wiring.registered[1][0] == first_key  # the stored key, not a new one
    assert omlx.key == first_key
    assert omlx.paths.count("/admin/api/setup-api-key") == 2  # the retry asked and got 400
    assert omlx.writes("/global-settings") == [STARTER_WRITE, STARTER_WRITE]


def test_a_failed_skip_write_is_not_success_and_the_retry_completes(wiring, omlx):  # noqa: F811
    omlx.settings_status = 500

    failed = _run_setup()

    assert failed["ok"] is False
    assert failed["message"] == "oMLX could not save settings (oMLX HTTP 500)"
    assert omlx.skip is False and omlx.key == wiring.registry["key"]  # the key is set; the flag is not
    assert wiring.emitted == ["started", "failed"]
    _no_secret(json.dumps(failed, default=str), omlx.key)

    omlx.settings_status = 200
    retried = _run_setup()

    assert retried["ok"] is True and omlx.skip is True
    assert wiring.registered[1][0] == wiring.registered[0][0]


@pytest.mark.parametrize("output, returncode, expected", [
    ("0.7.0.dev4", 0, False), ("0.7.0rc1", 0, True), ("0.7.0", 0, True), ("0.6.4", 0, False),
    ("not-a-version", 0, False), ("0.7.0", 1, False),
])
def test_only_the_guarded_omlx_release_and_newer_pass_version_check(monkeypatch, output, returncode, expected):
    monkeypatch.setattr(
        local_setup_auth.subprocess, "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], returncode, stdout=output),
    )

    assert local_setup._omlx_supports_loopback_auth() is expected


def test_an_unqueryable_omlx_version_fails_closed(monkeypatch):
    monkeypatch.setattr(
        local_setup_auth.subprocess, "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired("omlx", 5)),
    )

    assert local_setup._omlx_supports_loopback_auth() is False


@pytest.mark.parametrize("output, returncode", [("0.6.4", 0), ("not-a-version", 0)])
def test_an_old_or_unknown_omlx_makes_no_admin_or_skip_write(wiring, omlx, monkeypatch, output, returncode):  # noqa: F811
    monkeypatch.setattr(local_setup, "_omlx_supports_loopback_auth", REAL_VERSION_GUARD)
    monkeypatch.setattr(local_setup_auth, "omlx_version", REAL_OMLX_VERSION)
    monkeypatch.setattr(
        local_setup_auth.subprocess, "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], returncode, stdout=output),
    )

    result = _run_setup()

    assert result["ok"] is False and result["message"] == local_setup.UPDATE_OMLX_MESSAGE
    assert wiring.registered == [] and omlx.requests == [] and omlx.writes("/global-settings") == []
    assert omlx.skip is False and wiring.emitted == ["started", "failed"]


def test_an_unqueryable_omlx_makes_no_admin_or_skip_write(wiring, omlx, monkeypatch):  # noqa: F811
    monkeypatch.setattr(local_setup, "_omlx_supports_loopback_auth", REAL_VERSION_GUARD)
    monkeypatch.setattr(local_setup_auth, "omlx_version", REAL_OMLX_VERSION)
    monkeypatch.setattr(
        local_setup_auth.subprocess, "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired("omlx", 5)),
    )

    result = _run_setup()

    assert result["ok"] is False and result["message"] == local_setup.UPDATE_OMLX_MESSAGE
    assert wiring.registered == [] and omlx.requests == [] and omlx.writes("/global-settings") == []
    assert omlx.skip is False


def test_an_oMLX_holding_a_key_corsia_does_not_have_stops_before_any_write(wiring, omlx):  # noqa: F811
    omlx.key = OTHER_KEY

    result = _run_setup()

    assert result["ok"] is False
    assert result["message"] == "oMLX admin sign-in failed (oMLX HTTP 401)"
    assert omlx.key == OTHER_KEY  # never overwritten
    assert omlx.writes("/global-settings") == [] and omlx.skip is False
    _no_secret(json.dumps(result, default=str), OTHER_KEY, wiring.registry["key"])


def test_an_oMLX_listening_beyond_this_mac_is_left_without_the_skip_flag(wiring, omlx):  # noqa: F811
    omlx.host = "0.0.0.0"

    result = _run_setup()

    assert result["ok"] is False and result["message"] == local_setup_auth.NOT_LOOPBACK
    assert omlx.writes("/global-settings") == [] and omlx.skip is False


@pytest.mark.parametrize("hosts, expected", [
    ("127.0.0.1", True), ("localhost", True), ("::1", True), ("127.0.0.1, ::1", True),
    ("::ffff:127.0.0.1", True), ("0.0.0.0", False), ("::", False), ("192.168.1.5", False),
    ("127.0.0.1,0.0.0.0", False), ("", False), (None, False), ("my-mac.local", False),
])
def test_only_a_loopback_bind_counts_as_this_mac_only(hosts, expected):
    assert local_setup_auth._loopback_only(hosts) is expected


def test_the_persons_own_omlx_is_never_given_a_key_or_the_skip_flag(wiring, omlx, monkeypatch):  # noqa: F811
    omlx.key = OTHER_KEY  # the person's own key
    wiring.registry["key"] = OTHER_KEY  # they registered it themselves
    monkeypatch.setattr(local_setup, "existing_install_is_foreign", lambda: True)
    monkeypatch.setattr("llm.catalog.list_models", lambda **_k: [{"litellm_model": "omlx@local-server/m"}])

    async def inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr("infra.db_async.run_in_db", inline)

    adopted = asyncio.run(_finished(automatic=True))
    assert adopted["ok"] is True and adopted["existing"] is True
    assert omlx.requests == []  # adopting writes, and even reads, nothing

    # The explicit download-only flight talks to oMLX only to read where it downloads, then download.
    from llm import local_setup_existing

    monkeypatch.setattr(local_setup, "_wait_for_download", _real_download)
    async def add_starter() -> dict:
        started = await local_setup_existing.begin_add_starter_model()
        assert started["in_progress"] is True
        return await local_setup._flight.task

    local_setup._flight = local_setup._Flight()
    added = asyncio.run(add_starter())

    assert added["ok"] is True
    assert omlx.key == OTHER_KEY and omlx.skip is False
    assert "/admin/api/setup-api-key" not in omlx.paths
    assert omlx.writes("/global-settings") == []
    assert set(omlx.paths) <= {
        "/admin/api/login", "/admin/api/global-settings", "/admin/api/hf/download", "/admin/api/hf/tasks"}


def test_the_persons_own_omlx_is_adopted_even_from_a_nonautomatic_setup(wiring, omlx, monkeypatch):  # noqa: F811
    omlx.key = OTHER_KEY
    wiring.registry["key"] = OTHER_KEY
    monkeypatch.setattr(local_setup, "existing_install_is_foreign", lambda: True)
    monkeypatch.setattr("llm.catalog.list_models", lambda **_k: [{"litellm_model": "omlx@local-server/m"}])

    async def inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr("infra.db_async.run_in_db", inline)

    adopted = asyncio.run(_finished())

    assert adopted["ok"] is True and adopted["existing"] is True
    assert omlx.requests == []
    assert omlx.writes("/global-settings") == [] and omlx.skip is False


@pytest.mark.parametrize(("version", "expected"), [
    ("0.6.2", "Your oMLX is 0.6.2; CORSIA needs 0.7.0 or newer. Update it, then Try again."),
    (None, "oMLX didn't answer in time. Try again."),
])
def test_an_unowned_incompatible_omlx_is_never_configured(wiring, omlx, monkeypatch, version, expected):  # noqa: F811
    monkeypatch.setattr(local_setup, "existing_install_is_foreign", lambda: True)
    monkeypatch.setattr(local_setup_auth, "omlx_version", lambda: version)
    monkeypatch.setattr(local_setup_auth, "installed_omlx", lambda: "/opt/homebrew/bin/omlx")

    result = _run_setup()

    assert result["ok"] is False and result["message"] == expected
    assert wiring.registered == [] and omlx.requests == [] and omlx.writes("/global-settings") == []


@pytest.mark.parametrize(("initial_version", "broken"), [("0.6.2", False), (None, True)])
def test_an_incompatible_omlx_owned_by_setup_is_repaired_then_setup_continues(
        wiring, monkeypatch, initial_version, broken):
    state = {"version": initial_version}
    repairs: list[dict[str, bool]] = []

    def repair(_progress, **kwargs):
        repairs.append(kwargs)
        state["version"] = "0.7.0"

    monkeypatch.setattr(local_setup, "existing_install_is_foreign", lambda: False)
    monkeypatch.setattr(local_setup, "omlx_version", lambda: state["version"])
    monkeypatch.setattr(local_setup, "_omlx_supports_loopback_auth",
                        lambda: state["version"] == "0.7.0")
    monkeypatch.setattr(local_setup, "_ensure_omlx", repair)

    result = _run_setup()

    assert result["ok"] is True
    assert repairs == [{"needs_repair": True, "broken": broken}]


def test_a_failed_owned_omlx_repair_stops_before_any_omlx_write(wiring, omlx, monkeypatch):  # noqa: F811
    monkeypatch.setattr(local_setup, "existing_install_is_foreign", lambda: False)
    monkeypatch.setattr(local_setup, "omlx_version", lambda: None)
    monkeypatch.setattr(local_setup, "_omlx_supports_loopback_auth", lambda: False)

    def failed_repair(_progress, **_kwargs):
        raise RuntimeError("Homebrew could not reinstall CORSIA's oMLX: exit 1")

    monkeypatch.setattr(local_setup, "_ensure_omlx", failed_repair)

    result = _run_setup()

    assert result["ok"] is False and "could not reinstall CORSIA's oMLX" in result["message"]
    assert wiring.registered == [] and omlx.requests == [] and omlx.writes("/global-settings") == []


_real_download = local_setup._wait_for_download


def test_the_public_server_view_carries_no_key():
    row = {"id": "ns-1", "metadata_json": json.dumps({"api_key": KEY, "token": "t0ken-value", "note": "ok"})}

    view = _public_server_view(dict(row), {"ns-1": KEY})

    assert KEY not in json.dumps(view) and "t0ken-value" not in json.dumps(view)
    assert view["has_key"] is True and view["masked_key"] != KEY and view["metadata"] == {"note": "ok"}


@pytest.mark.parametrize("api_key", [KEY, None])
def test_the_add_probe_sends_the_key_when_it_has_one(monkeypatch, api_key):
    from types import SimpleNamespace

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

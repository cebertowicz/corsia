"""Letting CORSIA manage the person's own oMLX: only the shown settings change, and stopping puts them back.

The fake stands in for oMLX's admin routes behind the shared ``admin_request``,
so every request the real writers send is observed as oMLX would receive it.
"""

from __future__ import annotations

import asyncio

import pytest

from llm import local_sources, omlx_global_settings, own_omlx_management
from llm.local_model import LocalModel
from llm.local_profiles import MODEL_SETTINGS
from llm.providers import omlx_settings
from tests.test_provider_sources import client  # noqa: F401

APPLY = "/api/providers/sources/omlx/settings/apply"
STOP = "/api/providers/sources/omlx/management/stop"
START = "/api/providers/sources/omlx/management/start"
MODEL = LocalModel("Jundot/Qwen3.8-27B-oQ4e-mtp", 16_971_681_558, 65_536)
OTHER = "their-own-model"
SERVER = {"id": "srv", "provider": "omlx", "kind": "local", "status": "online"}
GLOBALS_BEFORE = {"max_concurrent_requests": 4, "prefill_priority": "decode"}
MODEL_BEFORE = {"max_tokens": 4096}
OTHER_BEFORE = {"max_tokens": 2048, "is_default": True}
ALLOWED = omlx_global_settings.MANAGED_GLOBAL_SETTINGS | omlx_global_settings.MANAGED_MODEL_SETTINGS


class _Response:
    def __init__(self, status: int, body: object):
        self.status_code = status
        self._body = body
        self.text = "body"
        self.content = b"x"

    def json(self):
        return self._body


class FakeOmlx:
    """oMLX's admin routes: nested global GET, flat global POST, model list GET, model PUT (null unsets).

    A PUT for a model oMLX doesn't have answers 404 ``Model not found``, as upstream
    oMLX v0.7.1rc1 does (``omlx/admin/routes.py`` 2723). Each listed model carries
    the ``thinking_default`` its chat template gives it (None unless a test sets one).
    """

    def __init__(self):
        self.globals = {**GLOBALS_BEFORE, "port": 8000, "host": "127.0.0.1", "model_dir": "/opt/omlx-models"}
        self.models = {MODEL.model: dict(MODEL_BEFORE), OTHER: dict(OTHER_BEFORE)}
        self.thinking: dict[str, bool | None] = {}
        self.requests: list[tuple[str, str, dict | None]] = []
        self.fail_writes = False
        self.fail_models: set[str] = set()

    @property
    def writes(self) -> list[tuple[str, str, dict | None]]:
        return [request for request in self.requests if request[0] != "GET"]

    async def admin_request(self, _cfg, method, path, *, json=None, timeout=None):
        self.requests.append((method, path, json))
        if path == "/global-settings" and method == "GET":
            return _Response(200, {
                "scheduler": {key: self.globals[key] for key in GLOBALS_BEFORE},
                "server": {"port": self.globals["port"], "host": self.globals["host"]},
                "model": {"model_dir": self.globals["model_dir"]},
            })
        if path == "/global-settings" and method == "POST":
            if self.fail_writes:
                return _Response(500, {})
            self.globals.update(json)
            return _Response(200, {"runtime_applied": list(json)})
        if path == "/models" and method == "GET":
            return _Response(200, {"models": [{"id": mid, "settings": dict(s), "thinking_default": self.thinking.get(mid)}
                                               for mid, s in self.models.items()]})
        if path.startswith("/models/") and path.endswith("/settings") and method == "PUT":
            model_id = path.split("/")[2]
            if self.fail_writes or model_id in self.fail_models:
                return _Response(500, {})
            if model_id not in self.models:
                return _Response(404, {"detail": f"Model not found: {model_id}"})
            settings = self.models[model_id]
            for key, value in json.items():
                if value is None:
                    settings.pop(key, None)
                else:
                    settings[key] = value
            return _Response(200, {"settings": dict(settings)})
        raise AssertionError(f"unexpected oMLX route {method} {path}")


@pytest.fixture
def omlx(monkeypatch, tmp_path):
    fake = FakeOmlx()
    monkeypatch.setattr(omlx_settings, "admin_request", fake.admin_request)
    monkeypatch.setattr(own_omlx_management, "record_path", lambda: tmp_path / "own-omlx-management.json")
    monkeypatch.setattr(omlx_global_settings, "_observed", {})
    monkeypatch.setattr(omlx_global_settings, "_observed_model", {})
    monkeypatch.setattr("api.provider_sources.network_discovery.get_servers", lambda **_kw: [dict(SERVER)])
    monkeypatch.setattr("llm.local_setup_ownership.existing_install_is_foreign", lambda: True)
    removed: list[str] = []
    monkeypatch.setattr("infra.key_store_servers.remove_server_key", lambda server_id: removed.append(server_id))
    fake.removed_keys = removed
    rearms: list[int] = []

    async def rearm():
        rearms.append(1)
        return True

    monkeypatch.setattr("onboarding.omlx_settings_wake.rearm", rearm)
    fake.rearms = rearms
    return fake


def _manage(model: LocalModel | None = MODEL, *, key_registered: bool = True) -> None:
    own_omlx_management.consent()
    own_omlx_management.update(model=model, key_registered=key_registered)
    asyncio.run(omlx_global_settings.record_observation(SERVER))


def _fingerprint() -> str:
    return omlx_global_settings.fingerprint("srv", omlx_global_settings.differences(["srv"]))


def _recommended() -> dict:
    values = {key: value for key, value in MODEL_SETTINGS.items() if key in omlx_global_settings.MANAGED_MODEL_SETTINGS}
    return {**values, "max_context_window": MODEL.context_window, "mtp_enabled": True}


def _written_keys(fake: FakeOmlx) -> set[str]:
    return {key for _method, _path, body in fake.writes for key in body or {}}


def test_apply_then_stop_sends_only_the_shown_values_and_puts_every_one_back(client, omlx):  # noqa: F811
    _manage()
    shown = omlx_global_settings.differences(["srv"])
    assert {row["setting"] for row in shown if "model" not in row} == {"max_concurrent_requests", "prefill_priority"}
    assert {row["model"] for row in shown if "model" in row} == {MODEL.model}

    applied = client.post(APPLY, json={"fingerprint": _fingerprint()})

    assert applied.status_code == 200
    assert all(result["saved"] for result in applied.json()["results"])
    assert omlx.writes == [
        ("POST", "/global-settings", {"max_concurrent_requests": 1, "prefill_priority": "context"}),
        ("PUT", f"/models/{MODEL.model}/settings", {key: _recommended()[key] for key in sorted(_recommended())
                                                    if _recommended()[key] != MODEL_BEFORE.get(key)}),
    ]
    assert omlx.models[OTHER] == OTHER_BEFORE
    assert applied.json()["omlx_settings"]["differences"] == []

    omlx.requests.clear()
    stopped = client.post(STOP)

    assert stopped.status_code == 200
    assert omlx.writes[0] == ("POST", "/global-settings", GLOBALS_BEFORE)
    method, path, restored = omlx.writes[1]
    assert (method, path) == ("PUT", f"/models/{MODEL.model}/settings")
    assert restored == {key: MODEL_BEFORE.get(key) for key in _recommended() if _recommended()[key] != MODEL_BEFORE.get(key)}
    assert {key: omlx.globals[key] for key in GLOBALS_BEFORE} == GLOBALS_BEFORE
    assert omlx.models == {MODEL.model: MODEL_BEFORE, OTHER: OTHER_BEFORE}
    assert omlx.removed_keys == ["srv"]
    assert own_omlx_management.read() is None


def test_no_write_ever_names_a_key_network_folder_default_or_other_model(client, omlx):  # noqa: F811
    _manage()
    client.post(APPLY, json={"fingerprint": _fingerprint()})
    client.post(STOP)

    assert _written_keys(omlx) <= ALLOWED
    assert not {"is_default", "port", "host", "model_dir"} & _written_keys(omlx)
    assert all(OTHER not in path for _method, path, _body in omlx.writes)


@pytest.mark.parametrize("thinking_default, shown", [(False, True), (True, False), (None, False)])
def test_only_a_model_that_does_not_think_by_default_gets_a_thinking_row(omlx, thinking_default, shown):
    omlx.thinking[MODEL.model] = thinking_default
    _manage()

    rows = [row for row in omlx_global_settings.differences(["srv"]) if row["setting"] == "enable_thinking"]

    assert rows == ([{"setting": "enable_thinking", "current": None, "recommended": True, "model": MODEL.model}]
                    if shown else [])


def test_thinking_is_turned_on_as_shown_and_stopping_sends_null_to_reset_it(client, omlx):  # noqa: F811
    omlx.thinking[MODEL.model] = False
    _manage()

    assert client.post(APPLY, json={"fingerprint": _fingerprint()}).status_code == 200
    assert omlx.writes[1][2]["enable_thinking"] is True
    assert f"Thinking for {MODEL.model} to oMLX default" in local_sources.omlx_management()["restores"]

    omlx.requests.clear()
    assert client.post(STOP).status_code == 200

    assert omlx.writes[1][2]["enable_thinking"] is None
    assert omlx.models[MODEL.model] == MODEL_BEFORE


def test_the_values_kept_are_the_ones_before_the_first_change(client, omlx):  # noqa: F811
    _manage()
    client.post(APPLY, json={"fingerprint": _fingerprint()})
    omlx.globals["max_concurrent_requests"] = 6
    asyncio.run(omlx_global_settings.record_observation(SERVER))
    client.post(APPLY, json={"fingerprint": _fingerprint()})

    assert own_omlx_management.read().originals_global == GLOBALS_BEFORE


def test_a_failed_apply_has_recorded_the_values_first_and_changes_nothing(client, omlx):  # noqa: F811
    _manage()
    omlx.fail_writes = True

    response = client.post(APPLY, json={"fingerprint": _fingerprint()})

    assert response.status_code == 502
    assert own_omlx_management.read().originals_global == GLOBALS_BEFORE
    assert omlx.models[MODEL.model] == MODEL_BEFORE
    assert [path for _method, path, _body in omlx.writes] == ["/global-settings"]


def test_a_failed_restore_keeps_the_record_and_try_again_finishes_it(client, omlx):  # noqa: F811
    _manage()
    client.post(APPLY, json={"fingerprint": _fingerprint()})
    omlx.fail_writes = True

    failed = client.post(STOP)

    assert failed.status_code == 502
    assert failed.json()["detail"] == "CORSIA couldn't put your values back. Try again."
    assert local_sources.omlx_management()["state"] == "restore_failed"
    assert omlx_global_settings.writable_global_settings() == omlx_global_settings.WRITABLE_GLOBAL_SETTINGS
    assert omlx.removed_keys == []

    omlx.fail_writes = False
    assert client.post(STOP).status_code == 200
    assert {key: omlx.globals[key] for key in GLOBALS_BEFORE} == GLOBALS_BEFORE
    assert local_sources.omlx_management()["state"] == "not_managed"


def test_a_key_the_person_had_stored_already_stays_after_stopping(client, omlx):  # noqa: F811
    _manage(key_registered=False)

    assert local_sources.omlx_management()["forgets_key"] is False
    assert client.post(STOP).status_code == 200
    assert omlx.removed_keys == []


def test_a_changed_diff_is_refused_and_writes_nothing(client, omlx):  # noqa: F811
    _manage()
    shown = _fingerprint()
    omlx.models[MODEL.model]["max_tokens"] = 8192
    asyncio.run(omlx_global_settings.record_observation(SERVER))

    assert client.post(APPLY, json={"fingerprint": shown}).status_code == 409
    assert omlx.writes == []


def test_rows_for_a_model_corsia_no_longer_manages_are_refused(client, omlx):  # noqa: F811
    _manage()
    shown = _fingerprint()
    own_omlx_management.update(model=LocalModel("someone/" + OTHER, 1, 40_960))

    assert client.post(APPLY, json={"fingerprint": shown}).status_code == 409
    assert omlx.writes == []


def test_without_management_only_concurrent_requests_is_ever_offered(client, omlx):  # noqa: F811
    asyncio.run(omlx_global_settings.record_observation(SERVER))

    shown = omlx_global_settings.differences(["srv"])

    assert shown == [{"setting": "max_concurrent_requests", "current": 4, "recommended": 1}]
    assert [path for _method, path, _body in omlx.requests] == ["/global-settings"]
    assert client.post(APPLY, json={"fingerprint": _fingerprint()}).status_code == 200
    assert omlx.writes == [("POST", "/global-settings", {"max_concurrent_requests": 1})]


def test_an_approved_change_without_management_is_not_consent_to_manage(client, omlx):  # noqa: F811
    asyncio.run(omlx_global_settings.record_observation(SERVER))
    assert client.post(APPLY, json={"fingerprint": _fingerprint()}).status_code == 200

    record = own_omlx_management.read()
    assert record.originals_global == {"max_concurrent_requests": 4}
    assert not own_omlx_management.active()
    assert omlx_global_settings.writable_global_settings() == omlx_global_settings.WRITABLE_GLOBAL_SETTINGS
    assert local_sources.omlx_management()["state"] == "not_managed"
    own_omlx_management.update(model=MODEL)
    assert own_omlx_management.read().model is None

    own_omlx_management.consent()
    assert own_omlx_management.active()
    assert own_omlx_management.read().originals_global == {"max_concurrent_requests": 4}


def test_a_model_corsia_did_not_set_up_gets_only_the_oMLX_wide_settings(client, omlx):  # noqa: F811
    _manage(model=None)

    assert all("model" not in row for row in omlx_global_settings.differences(["srv"]))


def test_first_reading_the_managed_model_raises_its_decision_once(omlx):
    _manage()
    asyncio.run(omlx_global_settings.record_observation(SERVER))

    assert omlx.rearms == [1]


def test_stopping_when_not_managed_is_refused(client, omlx):  # noqa: F811
    assert client.post(STOP).status_code == 409
    assert omlx.writes == []


def test_settings_start_records_consent_then_runs_setup(client, omlx, monkeypatch):  # noqa: F811
    async def begin():
        return {"ok": True, "in_progress": True}

    monkeypatch.setattr("llm.local_setup.begin_local_setup", begin)
    monkeypatch.setattr("llm.local_setup_ownership.consent_marker_path", lambda: own_omlx_management.record_path().with_name("consent"))

    assert client.post(START).json() == {"ok": True, "in_progress": True}
    assert own_omlx_management.active()


def test_settings_start_is_refused_for_the_omlx_corsia_installed(client, omlx, monkeypatch):  # noqa: F811
    monkeypatch.setattr("llm.local_setup_ownership.existing_install_is_foreign", lambda: False)

    assert client.post(START).status_code == 409
    assert own_omlx_management.read() is None


def test_the_source_row_names_what_stopping_puts_back(client, omlx):  # noqa: F811
    _manage()
    client.post(APPLY, json={"fingerprint": _fingerprint()})

    management = local_sources.omlx_management()

    assert management["state"] == "managed"
    assert "Max Concurrent Requests to 4" in management["restores"]
    assert "Prefill Priority to decode" in management["restores"]
    assert f"Ctx Window for {MODEL.model} to oMLX default" in management["restores"]
    assert f"Native MTP for {MODEL.model} to oMLX default" in management["restores"]

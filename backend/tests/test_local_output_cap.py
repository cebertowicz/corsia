"""CORSIA's old 16,384 reply cap is cleared from the models setup wrote it on, once, and nothing else is."""

from __future__ import annotations

from llm import local_output_cap

_OLD = {"max_tokens": 16384, "turboquant_kv_enabled": True, "turboquant_kv_bits": 8,
        "turboquant_skip_last": True, "is_default": True}


class _Response:
    def __init__(self, status_code: int, body: object = None):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def _seat(monkeypatch, models: list[dict], *, server: dict | None, put_status: int = 200) -> list[tuple]:
    calls: list[tuple] = []

    async def admin_request(_cfg, method, path, *, json=None, timeout=None):
        calls.append((method, path, json))
        if method == "GET":
            return _Response(200, {"models": models})
        return _Response(put_status, {"settings": {}})

    monkeypatch.setattr("llm.local_sources.corsia_omlx_server", lambda: server)
    monkeypatch.setattr("llm.providers.omlx_settings.admin_request", admin_request)
    monkeypatch.setattr("llm.providers.omlx_endpoints.admin_request", admin_request)
    return calls


async def test_only_the_cap_setup_wrote_is_reset_and_only_once(isolated_db, monkeypatch):
    from infra.settings import get_setting

    calls = _seat(monkeypatch, [
        {"id": "setup-model", "settings": dict(_OLD)},
        {"id": "persons-limit", "settings": {"max_tokens": 16384}},  # the same number, without CORSIA's KV block
        {"id": "persons-choice", "settings": {**_OLD, "max_tokens": 32768}},
        {"id": "uncapped", "settings": {**_OLD, "max_tokens": None}},
    ], server={"id": "seat", "status": "online"})

    assert await local_output_cap.clear_old_output_cap() is True
    assert calls == [("GET", "/models", None), ("PUT", "/models/setup-model/settings", {"max_tokens": None})]
    assert get_setting(local_output_cap.DONE_KEY) == {"models": ["setup-model"]}

    # Once it has run, the person's limits are theirs for good.
    calls.clear()
    assert await local_output_cap.clear_old_output_cap() is True
    assert calls == []


async def test_a_fresh_install_touches_nothing_and_learns_to_ask_never(isolated_db, monkeypatch):
    from infra.settings import get_setting

    calls = _seat(monkeypatch, [{"id": "later-model", "settings": dict(_OLD)}], server=None)

    assert await local_output_cap.clear_old_output_cap() is True  # startup runs before setup registers a server
    assert calls == []
    assert get_setting(local_output_cap.DONE_KEY) == {"models": [], "reason": "no_managed_server"}

    # Setup then registers the server; a cap present afterwards is CORSIA's current write or the person's, not ours.
    calls.clear()
    monkeypatch.setattr("llm.local_sources.corsia_omlx_server", lambda: {"id": "seat", "status": "online"})
    assert await local_output_cap.clear_old_output_cap() is True
    assert calls == []


async def test_a_busy_or_offline_seat_is_tried_again_at_the_next_startup(isolated_db, monkeypatch):
    from infra.settings import get_setting

    calls = _seat(monkeypatch, [{"id": "setup-model", "settings": dict(_OLD)}], server={"id": "seat", "status": "offline"})

    assert await local_output_cap.clear_old_output_cap() is False
    assert calls == []
    assert get_setting(local_output_cap.DONE_KEY) is None

    monkeypatch.setattr("llm.local_sources.corsia_omlx_server", lambda: {"id": "seat", "status": "online"})
    assert await local_output_cap.clear_old_output_cap() is True
    assert calls[-1] == ("PUT", "/models/setup-model/settings", {"max_tokens": None})


async def test_a_failed_reset_is_not_recorded_as_done(isolated_db, monkeypatch):
    from infra.settings import get_setting

    calls = _seat(monkeypatch, [{"id": "setup-model", "settings": dict(_OLD)}],
                 server={"id": "seat", "status": "online"}, put_status=500)

    assert await local_output_cap.clear_old_output_cap() is False
    assert calls[-1] == ("PUT", "/models/setup-model/settings", {"max_tokens": None})
    assert get_setting(local_output_cap.DONE_KEY) is None

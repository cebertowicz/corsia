"""CORSIA's old 16,384 reply cap is cleared once from an existing install's local models, and nothing else is."""

from __future__ import annotations

from llm import local_output_cap


class _Response:
    def __init__(self, status_code: int, body: object = None):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def _seat(monkeypatch, models: list[dict], *, online: bool = True, put_status: int = 200) -> list[tuple]:
    calls: list[tuple] = []

    async def admin_request(_cfg, method, path, *, json=None, timeout=None):
        calls.append((method, path, json))
        if method == "GET":
            return _Response(200, {"models": models})
        return _Response(put_status, {"settings": {}})

    monkeypatch.setattr("llm.local_sources.corsia_omlx_server",
                        lambda: {"id": "seat", "status": "online" if online else "offline"})
    monkeypatch.setattr("llm.providers.omlx_settings.admin_request", admin_request)
    monkeypatch.setattr("llm.providers.omlx_endpoints.admin_request", admin_request)
    return calls


async def test_only_corsias_old_cap_is_reset_and_only_once(isolated_db, monkeypatch):
    from infra.settings import get_setting

    calls = _seat(monkeypatch, [
        {"id": "capped", "settings": {"max_tokens": 16384}},
        {"id": "persons", "settings": {"max_tokens": 32768}},
        {"id": "unset", "settings": {"max_tokens": None}},
        {"id": "no-settings"},
    ])

    assert await local_output_cap.clear_old_output_cap() is True
    assert calls == [("GET", "/models", None), ("PUT", "/models/capped/settings", {"max_tokens": None})]
    assert get_setting(local_output_cap.DONE_KEY) == {"models": ["capped"]}

    # Once it has run, a 16,384 the person sets later is theirs.
    calls.clear()
    assert await local_output_cap.clear_old_output_cap() is True
    assert calls == []


async def test_an_offline_seat_is_tried_again_at_the_next_startup(isolated_db, monkeypatch):
    from infra.settings import get_setting

    calls = _seat(monkeypatch, [{"id": "capped", "settings": {"max_tokens": 16384}}], online=False)

    assert await local_output_cap.clear_old_output_cap() is False
    assert calls == []
    assert get_setting(local_output_cap.DONE_KEY) is None


async def test_a_failed_reset_is_not_recorded_as_done(isolated_db, monkeypatch):
    from infra.settings import get_setting

    calls = _seat(monkeypatch, [{"id": "capped", "settings": {"max_tokens": 16384}}], put_status=500)

    assert await local_output_cap.clear_old_output_cap() is False
    assert calls[-1] == ("PUT", "/models/capped/settings", {"max_tokens": None})
    assert get_setting(local_output_cap.DONE_KEY) is None

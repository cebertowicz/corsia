"""Faked-provider contract tests for Microsoft Graph milestone 2."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import pytest

from connections.connected_accounts import ConnectedAccountError
from connections.microsoft import calendar, client, files, mail
from connections.microsoft.errors import MicrosoftError, PROVIDER_UNAVAILABLE
from events.connectors import microsoft_poller as poller
from tools import microsoft_actions as actions


@dataclass
class Response:
    status_code: int
    body: object
    headers: dict[str, str] | None = None
    def json(self) -> object: return self.body


class FakeHttp:
    responses: list[Response] = []
    calls: list[dict[str, Any]] = []
    async def __aenter__(self): return self
    async def __aexit__(self, *args: object): return None
    async def request(self, method: str, url: str, **kwargs: object) -> Response:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.responses.pop(0)
    async def post(self, url: str, **kwargs: object) -> Response:
        return await self.request("POST", url, **kwargs)


@pytest.mark.asyncio
async def test_pagination_and_retry_after_are_internal(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeHttp.calls, FakeHttp.responses = [], [
        Response(429, {}, {"Retry-After": "0"}),
        Response(200, {"value": [{"id": "first"}], "@odata.nextLink": "next"}),
        Response(200, {"value": [{"id": "second"}]}),
    ]
    monkeypatch.setattr(client.httpx, "AsyncClient", lambda **_kwargs: FakeHttp())
    async def token(_account: str) -> str: return "token"
    monkeypatch.setattr(client, "access_token", token)
    out = await client.graph_pages("start", account_id="m")
    assert [row["id"] for row in out] == ["first", "second"]
    assert [call["url"] for call in FakeHttp.calls] == ["start", "start", "next"]


@pytest.mark.asyncio
async def test_refresh_uses_recorded_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    stored = {"tenant_id": "consumers", "client_id": "client", "refresh_token": "refresh", "scopes": ["User.Read"]}
    async def db(*_args: object, **_kwargs: object) -> object: return stored
    monkeypatch.setattr(client, "run_in_db", db)
    FakeHttp.calls, FakeHttp.responses = [], [Response(200, {"access_token": "access"})]
    monkeypatch.setattr(client.httpx, "AsyncClient", lambda **_kwargs: FakeHttp())
    assert await client.access_token("m") == "access"
    assert FakeHttp.calls[0]["url"] == client.authority_url("consumers")


@pytest.mark.asyncio
async def test_tools_check_reach_before_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refused(_kwargs: dict[str, Any]) -> str: raise ConnectedAccountError("Microsoft account is unavailable to this thread")
    monkeypatch.setattr(actions, "_account", refused)
    result = await actions.MicrosoftMailSearchTool().execute(_thread_id="t", account_id="m", query="x")
    assert result.startswith("Error:")


@pytest.mark.asyncio
async def test_actions_use_their_adapters(monkeypatch: pytest.MonkeyPatch) -> None:
    async def account(_kwargs: dict[str, Any]) -> str: return "m"
    async def search(_account: str, _query: str, _maximum: int) -> list[mail.Message]:
        return [mail.Message("id", "thread", {"from": "a", "to": "b", "date": "d", "subject": "s"}, "body")]
    monkeypatch.setattr(actions, "_account", account); monkeypatch.setattr(actions.mail, "search", search)
    output = json.loads(await actions.MicrosoftMailSearchTool().execute(_thread_id="t", account_id="m", query="x"))
    assert output["messages"][0]["thread_id"] == "microsoft:m:thread:thread"


@pytest.mark.asyncio
async def test_unscreened_message_withholds_provider_body(monkeypatch: pytest.MonkeyPatch) -> None:
    async def account(_kwargs: dict[str, Any]) -> str: return "m"
    async def fetch(_account: str, _message: object) -> mail.Message: return mail.Message("id", "thread", {}, "provider secret")
    class Screen: flagged = False; clean = False
    async def screen(*_args: object) -> Screen: return Screen()
    monkeypatch.setattr(actions, "_account", account); monkeypatch.setattr(actions.mail, "fetch", fetch); monkeypatch.setattr(actions.email_screen, "screen_message", screen)
    result = await actions.MicrosoftMailReadTool().execute(_thread_id="t", account_id="m", message_id="id")
    assert actions.UNSCREENED in result and "provider secret" not in result


@pytest.mark.asyncio
async def test_poller_initializes_then_advances_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    saved: list[dict[str, object]] = []
    async def cursor(_account: str) -> dict[str, object] | None: return None if not saved else saved[-1]
    async def save(_account: str, value: dict[str, object]) -> bool: saved.append(value); return True
    async def search(_account: str, _query: str, _maximum: int) -> list[mail.Message]: return [mail.Message("latest", None, {}, "body")]
    async def db(work: object, *args: object) -> object: return await work(*args)  # type: ignore[misc]
    monkeypatch.setattr(poller, "_cursor", cursor); monkeypatch.setattr(poller, "_save_cursor", save); monkeypatch.setattr(poller, "run_in_db", db); monkeypatch.setattr(poller.mail, "search", search)
    await poller.poll_once("m")
    assert saved[-1]["last_message_id"] == "latest"


@pytest.mark.asyncio
async def test_remove_during_microsoft_poll_does_not_touch_gmail() -> None:
    task = asyncio.get_running_loop().create_future()
    poller._inflight["m"] = task  # type: ignore[assignment]
    poller.cancel_account("m")
    assert "m" not in poller._inflight
    assert "m" not in getattr(__import__("events.connectors.gmail_poller", fromlist=["_inflight"]), "_inflight")


@pytest.mark.asyncio
async def test_graph_adapters_cover_mail_calendar_and_drive_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    page_calls: list[tuple[str, dict[str, object] | None]] = []
    json_calls: list[tuple[str, str, dict[str, object] | None]] = []
    byte_calls: list[tuple[str, str, bytes]] = []

    async def pages(url: str, *, account_id: str, params: dict[str, object] | None = None) -> list[dict[str, object]]:
        assert account_id == "m"
        page_calls.append((url, params))
        return [{"id": "id", "subject": "subject", "start": {}, "end": {}, "name": "name"}]

    async def graph_json(method: str, url: str, *, account_id: str, body: dict[str, object] | None = None) -> dict[str, object]:
        assert account_id == "m"
        json_calls.append((method, url, body))
        return {"id": "id", "subject": "subject", "start": {}, "end": {}, "name": "name"}

    async def graph_bytes(method: str, url: str, *, account_id: str, content: bytes) -> dict[str, object]:
        assert account_id == "m"
        byte_calls.append((method, url, content))
        return {"id": "id", "name": "name"}

    monkeypatch.setattr(client, "graph_pages", pages)
    monkeypatch.setattr(client, "graph_json", graph_json)
    monkeypatch.setattr(client, "graph_bytes", graph_bytes)

    await mail.search("m", "inbox", 10)
    await mail.fetch("m", "message/id")
    await mail.send("m", ["person@example.test"], "subject", "body")
    await calendar.list_events("m", "2026-10-19T00:00:00Z", "2026-10-20T00:00:00Z")
    await calendar.create_event("m", {"subject": "new"})
    await calendar.update_event("m", "event/id", {"subject": "updated"})
    await files.list_items("m")
    await files.list_items("m", "folder/id")
    await files.read_item("m", "file/id")
    await files.upload_item("m", "note.txt", "contents")

    assert page_calls[0][0] == mail.MESSAGES
    assert page_calls[1][0] == calendar.EVENTS
    assert page_calls[2][0] == f"{files.DRIVE}/root/children"
    assert page_calls[3][0] == f"{files.DRIVE}/items/folder%2Fid/children"
    assert any(method == "POST" and url == f"{mail.MESSAGES}/send" and body is not None for method, url, body in json_calls)
    assert ("POST", calendar.EVENTS, {"subject": "new"}) in json_calls
    assert ("PATCH", f"{calendar.EVENTS}/event%2Fid", {"subject": "updated"}) in json_calls
    assert ("PUT", f"{files.DRIVE}/root:/note.txt:/content", b"contents") in byte_calls

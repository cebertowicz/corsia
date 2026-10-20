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
from events.connectors import mail_poller as poller
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
async def test_pagination_stops_after_the_requested_items(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeHttp.calls, FakeHttp.responses = [], [Response(200, {"value": [{"id": "first"}], "@odata.nextLink": "next"})]
    monkeypatch.setattr(client.httpx, "AsyncClient", lambda **_kwargs: FakeHttp())
    async def token(_account: str) -> str: return "token"
    monkeypatch.setattr(client, "access_token", token)
    assert [row["id"] for row in await client.graph_pages("start", account_id="m", max_items=1)] == ["first"]
    assert [call["url"] for call in FakeHttp.calls] == ["start"]


@pytest.mark.asyncio
async def test_upload_conflict_is_a_fixed_named_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    from connections.microsoft.errors import FILE_NAME_CONFLICT

    FakeHttp.calls, FakeHttp.responses = [], [Response(409, {"error": {"message": "provider-canary"}})]
    monkeypatch.setattr(client.httpx, "AsyncClient", lambda **_kwargs: FakeHttp())
    async def token(_account: str) -> str: return "token"
    monkeypatch.setattr(client, "access_token", token)
    with pytest.raises(MicrosoftError) as raised:
        await client.graph_bytes("PUT", "upload", account_id="m", content=b"note", params={"@microsoft.graph.conflictBehavior": "fail"}, conflict_cause=FILE_NAME_CONFLICT)
    assert (raised.value.cause, raised.value.message) == (FILE_NAME_CONFLICT, "A file with that name already exists; choose another name.")
    assert FakeHttp.calls[0]["params"] == {"@microsoft.graph.conflictBehavior": "fail"}


@pytest.mark.asyncio
async def test_send_mail_accepts_empty_202_without_decoding_json(monkeypatch):
    class EmptyAccepted(Response):
        def json(self):
            pytest.fail("Graph sendMail 202 has no JSON body")

    FakeHttp.calls, FakeHttp.responses = [], [EmptyAccepted(202, None)]
    monkeypatch.setattr(client.httpx, "AsyncClient", lambda **_: FakeHttp())

    async def token(_account):
        return "access"

    monkeypatch.setattr(client, "access_token", token)
    await mail.send("m", ["person@example.test"], "subject", "body")
    assert FakeHttp.calls[0]["method"] == "POST"
    assert FakeHttp.calls[0]["url"] == "https://graph.microsoft.com/v1.0/me/sendMail"
    assert FakeHttp.calls[0]["json"]["message"]["subject"] == "subject"


@pytest.mark.asyncio
async def test_graph_expired_delta_has_a_specific_recovery_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeHttp.calls, FakeHttp.responses = [], [Response(410, {})]
    monkeypatch.setattr(client.httpx, "AsyncClient", lambda **_kwargs: FakeHttp())
    async def token(_account: str) -> str: return "token"
    monkeypatch.setattr(client, "access_token", token)
    with pytest.raises(client.DeltaExpired):
        await client.graph_delta("expired-delta", account_id="m")


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
async def test_poller_initializes_then_advances_cursor(isolated_db, monkeypatch: pytest.MonkeyPatch) -> None:
    from connections.connected_accounts import create_account
    from connections.microsoft.provider import MICROSOFT

    account = str(create_account(
        MICROSOFT.name, "m@example.test", reach={"mode": "all", "project_ids": [], "outside_projects": False},
        created_by=("system", "test"), account_kind="personal", provider_subject="tenant:object",
    )["id"])
    async def changes(_link: str, *, account_id: str, params=None):
        return [], "floor" if params else "next"
    monkeypatch.setattr(client, "graph_delta", changes)
    await poller.poll_once(account)
    assert poller._load_stored_cursor(account).history_id == "floor"
    await poller.poll_once(account)
    assert poller._load_stored_cursor(account).history_id == "next"


@pytest.mark.asyncio
async def test_remove_during_microsoft_poll_does_not_touch_gmail() -> None:
    task = asyncio.get_running_loop().create_future()
    gmail = asyncio.get_running_loop().create_future()
    monkeypatch_map = poller._inflight
    monkeypatch_map["m"] = task  # type: ignore[assignment]
    monkeypatch_map["g"] = gmail  # type: ignore[assignment]
    try:
        poller.cancel_account("m")
        assert "m" not in poller._inflight and task.cancelled()
        assert poller._inflight["g"] is gmail and not gmail.cancelled()
    finally:
        monkeypatch_map.pop("g", None)
        gmail.cancel()


@pytest.mark.asyncio
async def test_graph_adapters_cover_mail_calendar_and_drive_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    page_calls: list[tuple[str, dict[str, object] | None]] = []
    json_calls: list[tuple[str, str, dict[str, object] | None]] = []
    byte_calls: list[tuple[str, str, bytes]] = []

    async def pages(url: str, *, account_id: str, params: dict[str, object] | None = None, max_items: int | None = None) -> list[dict[str, object]]:
        assert account_id == "m"
        page_calls.append((url, params))
        return [{"id": "id", "subject": "subject", "start": {}, "end": {}, "name": "name"}]

    async def graph_json(method: str, url: str, *, account_id: str, body: dict[str, object] | None = None) -> dict[str, object]:
        assert account_id == "m"
        json_calls.append((method, url, body))
        return {"id": "id", "subject": "subject", "start": {}, "end": {}, "name": "name"}

    async def graph_bytes(method: str, url: str, *, account_id: str, content: bytes, **_kwargs: object) -> dict[str, object]:
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
    assert page_calls[1][0] == calendar.CALENDAR_VIEW
    assert page_calls[2][0] == f"{files.DRIVE}/root/children"
    assert page_calls[3][0] == f"{files.DRIVE}/items/folder%2Fid/children"
    assert any(method == "POST" and url == "https://graph.microsoft.com/v1.0/me/sendMail" and body is not None for method, url, body in json_calls)
    assert ("POST", calendar.EVENTS, {"subject": "new"}) in json_calls
    assert ("PATCH", f"{calendar.EVENTS}/event%2Fid", {"subject": "updated"}) in json_calls
    assert ("PUT", f"{files.DRIVE}/root:/note.txt:/content", b"contents") in byte_calls

"""Microsoft gated delivery lifecycle tests with Graph mocked at the HTTP boundary."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from connections.connected_accounts import store_credentials
from connections.microsoft import calendar, client
from connections.microsoft.errors import MicrosoftError, PROVIDER_UNAVAILABLE
from infra.database import get_db
from delegation_fixtures import deleg_db  # noqa: F401
from tests.test_action_gate import THREAD, _admit, _bind, workspace  # noqa: F401
from tests.test_microsoft_refresh import Response, connect
from tools import action_delivery, microsoft_actions
from tools.action_gate import Approved
from tools.action_drafts import create_draft
from tools.microsoft_actions import (
    CALENDAR_STILL_SENDING,
    MAIL_STILL_SENDING,
    MAIL_UNCONFIRMED,
    MicrosoftCalendarCreateTool,
    MicrosoftCalendarDraftTool,
    MicrosoftCalendarListTool,
    MicrosoftCalendarUpdateTool,
    MicrosoftMailDraftTool,
    MicrosoftMailSendTool,
)


@dataclass
class GraphHttp:
    responses: list[object]
    calls: list[dict[str, Any]]

    async def __aenter__(self) -> GraphHttp:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def request(self, method: str, url: str, **kwargs: Any) -> Response:
        self.calls.append({"method": method, "url": url, **kwargs})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        assert isinstance(response, Response)
        return response


def _row(draft_id: str) -> dict[str, Any]:
    return dict(get_db().execute("SELECT * FROM action_drafts WHERE id=?", (draft_id,)).fetchone())


async def _no_designated(_kwargs: dict[str, Any]) -> None:
    return None


async def _token(_account: str) -> str:
    return "access"


@pytest.fixture
def microsoft(deleg_db, workspace, monkeypatch):  # noqa: F811
    from tools import sandbox_config

    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [workspace])
    account = connect()
    monkeypatch.setattr(microsoft_actions, "_designated", _no_designated)
    monkeypatch.setattr(client, "access_token", _token)
    return account


def _graph(monkeypatch: pytest.MonkeyPatch, responses: list[object]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    http = GraphHttp(responses, calls)
    monkeypatch.setattr(client.httpx, "AsyncClient", lambda **_kwargs: http)
    return calls


async def _mail_draft(account: str) -> dict[str, Any]:
    result = await MicrosoftMailDraftTool().execute(
        _thread_id=THREAD, account_id=account, to=["to@example.test"], cc=["cc@example.test"],
        subject="Subject", body="Body",
    )
    assert not result.startswith("Error:"), result
    return json.loads(result)


async def _calendar_draft(account: str, *, operation: str, event_id: str | None = None) -> dict[str, Any]:
    result = await MicrosoftCalendarDraftTool().execute(
        _thread_id=THREAD, account_id=account, operation=operation, event_id=event_id,
        event={"subject": "Planning", "start": {"dateTime": "2026-10-20T09:00:00-07:00"}},
    )
    assert not result.startswith("Error:"), result
    return json.loads(result)


def _approve(monkeypatch: pytest.MonkeyPatch) -> None:
    async def approved(_kind: str, draft_id: str, _revision: str) -> Approved:
        row = _row(draft_id)
        from infra import config
        return Approved(row, (config.WORKSPACE / row["path"]).read_bytes())

    monkeypatch.setattr(microsoft_actions, "check_action", approved)


async def test_mail_send_carries_cc_and_refuses_mismatched_from_before_graph(microsoft, monkeypatch):
    _approve(monkeypatch)
    calls = _graph(monkeypatch, [Response(202, {})])
    saved = await _mail_draft(microsoft)

    sent = json.loads(await MicrosoftMailSendTool().execute(
        _thread_id=THREAD, account_id=microsoft, draft_id=saved["draft_id"], revision=saved["revision"],
    ))
    assert sent == {"sent": True, "account_id": microsoft}
    assert calls[0]["json"]["message"]["ccRecipients"] == [{"emailAddress": {"address": "cc@example.test"}}]

    saved = await _mail_draft(microsoft)
    from infra import config
    path = config.WORKSPACE / saved["path"]
    path.write_text(path.read_text().replace("From: mail@example.test", "From: other@example.test"))
    assert await MicrosoftMailSendTool().execute(
        _thread_id=THREAD, account_id=microsoft, draft_id=saved["draft_id"], revision=saved["revision"],
    ) == "Error: Not sent: the From line does not match the Microsoft account this draft was written from."
    assert len(calls) == 1


async def test_mail_timeout_stays_claimed_and_second_send_refuses(microsoft, monkeypatch):
    _approve(monkeypatch)
    calls = _graph(monkeypatch, [httpx.ReadTimeout("late response")])
    saved = await _mail_draft(microsoft)
    tool = MicrosoftMailSendTool()
    first = await tool.execute(_thread_id=THREAD, account_id=microsoft, draft_id=saved["draft_id"], revision=saved["revision"])
    assert first == f"Error: {MAIL_UNCONFIRMED}"
    assert _row(saved["draft_id"])["state"] == "sending"
    second = await tool.execute(_thread_id=THREAD, account_id=microsoft, draft_id=saved["draft_id"], revision=saved["revision"])
    assert second == f"Error: {MAIL_STILL_SENDING}"
    assert len(calls) == 1


async def test_mail_graph_400_releases_claim_with_named_refusal(microsoft, monkeypatch):
    _approve(monkeypatch)
    _graph(monkeypatch, [Response(400, {})])
    saved = await _mail_draft(microsoft)
    result = await MicrosoftMailSendTool().execute(
        _thread_id=THREAD, account_id=microsoft, draft_id=saved["draft_id"], revision=saved["revision"],
    )
    assert result == "Error: Not sent: Microsoft refused this request. Check the draft or event details and try again."
    assert _row(saved["draft_id"])["state"] == "open"


async def test_mail_delivery_succeeds_when_mark_sent_fails(microsoft, monkeypatch):
    _approve(monkeypatch)
    _graph(monkeypatch, [Response(202, {})])
    saved = await _mail_draft(microsoft)

    def fail_mark_sent(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(action_delivery, "mark_sent", fail_mark_sent)
    assert json.loads(await MicrosoftMailSendTool().execute(
        _thread_id=THREAD, account_id=microsoft, draft_id=saved["draft_id"], revision=saved["revision"],
    )) == {"sent": True, "account_id": microsoft}
    assert _row(saved["draft_id"])["state"] == "sending"


async def test_calendar_create_claims_before_graph_and_update_refuses_blank_event_id(microsoft, workspace, monkeypatch):
    _approve(monkeypatch)
    calls = _graph(monkeypatch, [httpx.ReadTimeout("late response")])
    saved = await _calendar_draft(microsoft, operation="create")
    create = MicrosoftCalendarCreateTool()
    first = await create.execute(_thread_id=THREAD, account_id=microsoft, draft_id=saved["draft_id"], revision=saved["revision"])
    assert first.startswith("Error:") and _row(saved["draft_id"])["state"] == "sending"
    second = await create.execute(_thread_id=THREAD, account_id=microsoft, draft_id=saved["draft_id"], revision=saved["revision"])
    assert second == f"Error: {CALENDAR_STILL_SENDING}"
    assert len(calls) == 1

    data = b'{"operation":"update","event_id":" ","event":{"subject":"Planning"}}'
    (workspace / "blank-event.json").write_bytes(data)
    update = create_draft(THREAD, "invite", "blank-event.json", data, {"account_id": microsoft})
    get_db().commit()
    assert await MicrosoftCalendarUpdateTool().execute(
        _thread_id=THREAD, account_id=microsoft, draft_id=update["draft_id"], revision=update["revision"],
    ) == ("Error: This saved calendar draft is invalid or incomplete; nothing changed. Save a new draft "
          "with microsoft_calendar_draft, show the person, and wait for their reply.")
    assert len(calls) == 1


async def test_calendar_list_uses_bounded_calendar_view_and_refuses_injection(microsoft, monkeypatch):
    calls = _graph(monkeypatch, [Response(200, {"value": []})])
    result = json.loads(await MicrosoftCalendarListTool().execute(
        _thread_id=THREAD, account_id=microsoft, start="2026-10-20T00:00:00-07:00", end="2026-10-21T00:00:00-07:00",
    ))
    assert result == {"events": []}
    assert calls[0]["url"] == calendar.CALENDAR_VIEW
    assert calls[0]["params"]["startDateTime"] == "2026-10-20T00:00:00-07:00"
    assert calls[0]["params"]["endDateTime"] == "2026-10-21T00:00:00-07:00"
    assert "$filter" not in calls[0]["params"]

    refused = await MicrosoftCalendarListTool().execute(
        _thread_id=THREAD, account_id=microsoft, start="x' or 1 eq 1", end="2026-10-21T00:00:00-07:00",
    )
    assert refused == "Error: start and end must be ISO-8601 datetimes."
    assert len(calls) == 1


async def test_refresh_lost_race_uses_winner_token_without_disabling_account(deleg_db, monkeypatch):
    microsoft = connect()
    calls: list[tuple[str, dict[str, Any]]] = []

    class TokenHttp:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response(200, {"access_token": "candidate", "expires_in": 3600})

    monkeypatch.setattr(client.httpx, "AsyncClient", lambda **_kwargs: TokenHttp())

    def lose_race(account_id: str, _held: str, _updated: dict[str, Any], *, db) -> bool:
        current = client._usable_credentials(account_id, db=db)
        assert current is not None
        store_credentials(account_id, {**current, "access_token": "winner", "access_token_expires_at": str(time.time() + 3600)}, db=db)
        return False

    monkeypatch.setattr(client, "_store_refreshed", lose_race)
    import connections.account_state as account_state
    disabled: list[str] = []
    monkeypatch.setattr(account_state, "mark_needs_sign_in", lambda account_id, *args: disabled.append(account_id))
    assert await client.access_token(microsoft) == "winner"
    assert len(calls) == 1 and disabled == []


@pytest.mark.parametrize(("raised", "logged"), [(RuntimeError("db fault"), True), (MicrosoftError(PROVIDER_UNAVAILABLE), False)])
async def test_delivery_logs_only_unexpected_credential_failures(monkeypatch, caplog, raised, logged):
    async def db_call(*_args: object) -> None: return None
    async def acquire() -> str: raise raised
    async def send(_token: str) -> str: raise AssertionError("must not send without credentials")
    monkeypatch.setattr(action_delivery, "run_in_db", db_call)
    out = await action_delivery.deliver_claimed(
        "draft", acquire=acquire, provider_errors=(MicrosoftError,), send=send, result_id=lambda r: r,
        success=lambda _r, _i: "sent", sign_in_error=lambda _e: "sign in", request_error=str,
        never_sent=lambda _e: False, still_sending="busy", claim_error="claim", unconfirmed="unknown", label="test",
    )
    assert out == "Error: Not sent: sign in"
    assert ("could not acquire delivery credentials" in caplog.text) is logged
